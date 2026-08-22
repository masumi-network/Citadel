"""Self-improvement loop: a bounded, periodic optimization pass.

One pass (a) collects recent feedback and ingest activity from the mesh
projection, (b) runs the existing Cognee improve step, (c) optionally asks the
LLM (same OpenRouter client as enrichment) to propose better tags/summaries
for the most recent ingest records and applies them additively via re-ingest
through the Learning Process, and (d) records an audit event plus a mesh
event summarizing what was optimized.

Hard guarantees: strictly bounded by ``CITADEL_SELF_IMPROVE_MAX_ITEMS``
(default 10), the LLM is optional with a deterministic no-op fallback, and
nothing is ever deleted — every applied optimization is an additive write.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from kb.learning import LearningProcess
from kb.llm_enrichment import (
    openrouter_api_key,
    openrouter_chat,
    parse_json_payload,
    redacted_preview,
)
from kb.improvement_policy import central_improvement_dataset
from kb.model_routing import route_for
from kb.mesh import MeshState
from kb.service import Citadel

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITEMS = 10
MAX_ITEMS_CEILING = 50
OPTIMIZE_TAG = "self-improvement"

OPTIMIZE_SYSTEM_PROMPT = (
    "You curate tags and summaries for an organization knowledge index. "
    "For each supplied item (a label plus its current tags), propose a "
    "one-line summary and 3-6 better lowercase tags. Return ONLY JSON shaped "
    'as {"items": [{"label": "...", "summary": "...", "tags": ["..."]}]}. '
    "Keep labels unchanged. Never invent facts and never include secrets."
)


def self_improve_enabled() -> bool:
    return (os.getenv("CITADEL_SELF_IMPROVE_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def self_improve_max_items() -> int:
    raw = os.getenv("CITADEL_SELF_IMPROVE_MAX_ITEMS")
    try:
        value = int(raw) if raw else DEFAULT_MAX_ITEMS
    except ValueError:
        value = DEFAULT_MAX_ITEMS
    return min(max(1, value), MAX_ITEMS_CEILING)


def propose_optimizations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask the LLM for better tags/summaries; deterministic no-op on failure."""
    if not items or not openrouter_api_key():
        return []
    route = route_for("self_improve")
    model = route.model
    content = openrouter_chat(
        [
            {"role": "system", "content": OPTIMIZE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "\n".join(
                    f"- label: {item['label']} | tags: {', '.join(item.get('tags') or []) or 'none'}"
                    for item in items
                ),
            },
        ],
        model=model,
        operation="self_improve.propose",
        max_tokens=900,
        plugins=route.plugins,
    )
    if content is None:
        return []
    parsed = parse_json_payload(content)
    raw_items = parsed.get("items") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_items, list):
        logger.warning(
            "Self-improvement proposal output was unusable: %s",
            redacted_preview(content),
        )
        return []
    known_labels = {item["label"] for item in items}
    known_items = {item["label"]: item for item in items}
    proposals: list[dict[str, Any]] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        if not isinstance(label, str) or label not in known_labels:
            continue
        summary = entry.get("summary")
        tags = entry.get("tags")
        clean_tags = [
            tag.strip().lower()[:60]
            for tag in (tags if isinstance(tags, list) else [])
            if isinstance(tag, str) and tag.strip()
        ][:6]
        if not clean_tags and not isinstance(summary, str):
            continue
        proposals.append(
            {
                "label": label,
                "summary": " ".join(summary.split())[:200] if isinstance(summary, str) else None,
                "tags": clean_tags,
                "document_id": known_items[label].get("document_id"),
                "source_key": known_items[label].get("source_key"),
                "source_locator": known_items[label].get("source_locator"),
                "source_revision_id": known_items[label].get("source_revision_id"),
            }
        )
    return proposals


class SelfImprovement:
    """One bounded optimization pass over recent vault activity."""

    def __init__(
        self,
        citadel: Citadel,
        *,
        mesh: MeshState | None = None,
        learning: LearningProcess | None = None,
        access_store: Any | None = None,
    ) -> None:
        self.citadel = citadel
        self.config = citadel.config
        self.mesh = mesh
        self.learning = learning or LearningProcess(citadel, mesh=mesh)
        self.access_store = access_store

    async def run(
        self,
        *,
        dry_run: bool = False,
        max_items: int | None = None,
        actor: Any | None = None,
    ) -> dict[str, Any]:
        limit = min(max(1, max_items or self_improve_max_items()), MAX_ITEMS_CEILING)
        dataset = central_improvement_dataset(self.config)

        recent = await self._recent_activity(limit, dataset=dataset)
        improve_result = await self._improve(dataset)
        improve_ok = not (
            isinstance(improve_result, dict) and improve_result.get("ok") is False
        )
        proposals = self._propose(recent["ingest_items"]) if improve_ok else []
        applied = 0
        if proposals and not dry_run:
            applied = await self._apply(proposals[:limit], dataset=dataset)

        result = {
            "ok": improve_ok,
            "dry_run": dry_run,
            "dataset": dataset,
            "max_items": limit,
            "recent_feedback": recent["feedback_count"],
            "reviewed": len(recent["ingest_items"]),
            "proposals": proposals[:limit],
            "optimized": applied,
            "llm_used": bool(proposals),
            "improve": improve_result,
        }
        await self._record(result)
        logger.info(
            "Self-improvement pass finished: reviewed=%d, proposals=%d, optimized=%d, "
            "llm_used=%s, dry_run=%s",
            result["reviewed"],
            len(result["proposals"]),
            applied,
            result["llm_used"],
            dry_run,
        )
        if self.access_store is not None:
            try:
                self.access_store.record_event(
                    action="learning_agent.optimize",
                    actor=actor,
                    success=result["ok"],
                    dataset=dataset,
                    detail={
                        "reviewed": result["reviewed"],
                        "optimized": applied,
                        "llm_used": result["llm_used"],
                        "dry_run": dry_run,
                        "max_items": limit,
                    },
                )
            except Exception:  # pragma: no cover - audit is best-effort here.
                logger.warning("Self-improvement audit event could not be recorded")
        return result

    async def _recent_activity(self, limit: int, *, dataset: str) -> dict[str, Any]:
        """Recent ingest records and feedback from the mesh projection."""
        if self.mesh is None:
            return {"ingest_items": [], "feedback_count": 0}
        snapshot = await self.mesh.snapshot(self.config)
        documents = [
            node
            for node in snapshot.get("nodes", [])
            if node.get("type") == "document"
            and node.get("label")
            and (node.get("metadata") or {}).get("dataset") == dataset
            # Never feed previous optimization notes back into the loop.
            and OPTIMIZE_TAG not in ((node.get("metadata") or {}).get("tags") or [])
        ]
        ingest_items = [
            {
                "document_id": node.get("id"),
                "label": str(node.get("label") or "")[:120],
                "tags": list((node.get("metadata") or {}).get("tags") or []),
                "dataset": dataset,
                "source_key": (node.get("metadata") or {}).get("source_key"),
                "source_locator": (node.get("metadata") or {}).get("source_locator"),
                "source_revision_id": (node.get("metadata") or {}).get("source_revision_id"),
            }
            for node in documents[-limit:]
        ]
        feedback_count = sum(
            1
            for event in snapshot.get("events", [])
            if event.get("type") == "feedback"
            and (event.get("details") or {}).get("dataset") == dataset
        )
        return {"ingest_items": ingest_items, "feedback_count": feedback_count}

    async def _improve(self, dataset: str) -> Any:
        try:
            return await self.citadel.improve(
                dataset=dataset,
                session_ids=None,
                automation=True,
            )
        except Exception as exc:
            logger.warning(
                "Self-improvement improve step failed with %s; continuing",
                exc.__class__.__name__,
            )
            return {"ok": False, "error": str(exc)}

    def _propose(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            return propose_optimizations(items)
        except Exception as exc:  # pragma: no cover - LLM is strictly optional.
            logger.warning(
                "Self-improvement proposal step failed with %s; no-op fallback",
                exc.__class__.__name__,
            )
            return []

    async def _apply(self, proposals: list[dict[str, Any]], *, dataset: str) -> int:
        """Apply proposals additively via re-ingest; never deletes knowledge."""
        applied = 0
        for proposal in proposals:
            summary = proposal.get("summary") or proposal["label"]
            source_key = " ".join(str(proposal.get("source_key") or "").split())[:240]
            if not source_key:
                source_key = "mesh-document:" + (
                    str(proposal.get("document_id") or proposal["label"]).strip()
                )[:180]
            source_locator = " ".join(
                str(proposal.get("source_locator") or "").split()
            )[:500]
            if not source_locator:
                source_locator = (
                    "mesh://document/"
                    f"{proposal.get('document_id') or proposal['label']}"
                )
            note = (
                f"Knowledge optimization note: {proposal['label']}\n\n"
                f"Source key: {source_key}\n"
                f"Source locator: {source_locator}\n"
                f"Source revision: {proposal.get('source_revision_id') or 'not recorded'}\n"
                f"Summary: {summary}\n"
                f"Proposed tags: {', '.join(proposal.get('tags') or []) or 'none'}"
            )
            try:
                outcome = await self.learning.learn(
                    note,
                    dataset=dataset,
                    tags=[*proposal.get("tags", []), OPTIMIZE_TAG],
                    operation="self_improve",
                    detect_conflicts=True,
                    source_key=f"self-improve:{source_key}",
                    source_locator=source_locator,
                    capture_actor_id="learning-agent",
                    capture_run_id=f"self-improvement:{dataset}",
                )
            except Exception as exc:
                logger.warning(
                    "Self-improvement re-ingest failed with %s; skipping item",
                    exc.__class__.__name__,
                )
                continue
            if outcome.ingest.accepted:
                applied += 1
        return applied

    async def _record(self, result: dict[str, Any]) -> None:
        if self.mesh is None:
            return
        await self.mesh.record_optimization(
            self.config,
            dataset=result["dataset"],
            reviewed=result["reviewed"],
            optimized=result["optimized"],
            used_llm=result["llm_used"],
            dry_run=result["dry_run"],
        )
