"""ADR-0007 P5: GitHub org + Central reference checks for the Promotion Agent."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GITHUB_REPO_URL_RE = re.compile(
    r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/\s#]|$)",
    re.IGNORECASE,
)
CAPTURE_REMOTE_RE = re.compile(r"^\s*-\s*Remote:\s*`?([^`\n]+)`?\s*$", re.MULTILINE | re.IGNORECASE)
CAPTURE_TAGS_RE = re.compile(r"Capture Root Tags:\s*([^\n]+)", re.IGNORECASE)

ReferenceStatus = str  # known_org_work | new_org_project | no_reference_signal

CENTRAL_MATCH_QUERY_CHARS = 500


@dataclass(frozen=True)
class ReferenceAssessment:
    status: ReferenceStatus
    matched_repos: tuple[str, ...] = ()
    central_hits: int = 0
    repo_hints: tuple[str, ...] = ()
    reason: str = ""


def extract_repo_hints(text: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for org, repo in GITHUB_REPO_URL_RE.findall(text):
        seen.setdefault(f"{org}/{repo}".lower(), None)
        seen.setdefault(repo.lower(), None)
    for remote in CAPTURE_REMOTE_RE.findall(text):
        cleaned = remote.strip().strip("`")
        for org, repo in GITHUB_REPO_URL_RE.findall(cleaned):
            seen.setdefault(f"{org}/{repo}".lower(), None)
            seen.setdefault(repo.lower(), None)
    return tuple(seen)


def load_tracked_org_repos(state_path: Path, org: str) -> frozenset[str]:
    if not state_path.exists():
        return frozenset()
    try:
        with state_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("promotion_refs: could not read GitHub sync state at %s", state_path)
        return frozenset()
    repos = data.get("repos") or {}
    if not isinstance(repos, dict):
        return frozenset()
    org_lower = org.strip().lower()
    names: set[str] = set()
    for full_name in repos:
        if not isinstance(full_name, str):
            continue
        lowered = full_name.lower()
        names.add(lowered)
        if "/" in lowered:
            owner, repo = lowered.split("/", 1)
            names.add(repo)
            if owner == org_lower:
                names.add(f"{owner}/{repo}")
    return frozenset(names)


def _hint_matches_org(hint: str, org_repos: frozenset[str], org: str) -> bool:
    normalized = hint.strip().lower()
    if not normalized:
        return False
    if normalized in org_repos:
        return True
    prefixed = f"{org.strip().lower()}/{normalized}"
    return prefixed in org_repos


async def _central_search(
    citadel: Any,
    query: str,
    *,
    central_dataset: str,
    top_k: int = 3,
) -> list[Any]:
    try:
        return await citadel.search(query, dataset=central_dataset, top_k=top_k)
    except Exception as exc:  # pragma: no cover - depends on Cognee runtime.
        logger.warning(
            "promotion_refs central search failed for %r: %s",
            query[:80],
            exc.__class__.__name__,
        )
        return []


async def assess_org_reference(
    citadel: Any,
    *,
    candidate_text: str,
    central_dataset: str,
    github_state_path: Path,
    github_org: str,
) -> ReferenceAssessment:
    hints = extract_repo_hints(candidate_text)
    if not hints:
        query = candidate_text.strip()[:CENTRAL_MATCH_QUERY_CHARS]
        if not query:
            return ReferenceAssessment(status="no_reference_signal", reason="empty_candidate")
        hits = await _central_search(citadel, query, central_dataset=central_dataset)
        if hits:
            return ReferenceAssessment(
                status="known_org_work",
                central_hits=len(hits),
                reason="central_match_no_repo",
            )
        return ReferenceAssessment(
            status="no_reference_signal",
            reason="no_repo_or_central_match",
        )

    org_repos = load_tracked_org_repos(github_state_path, github_org)
    matched = tuple(h for h in hints if _hint_matches_org(h, org_repos, github_org))
    if matched:
        return ReferenceAssessment(
            status="known_org_work",
            matched_repos=matched,
            repo_hints=hints,
            reason="github_org_match",
        )

    for hint in hints[:5]:
        hits = await _central_search(citadel, hint, central_dataset=central_dataset)
        if hits:
            return ReferenceAssessment(
                status="known_org_work",
                central_hits=len(hits),
                repo_hints=hints,
                reason="central_match",
            )

    return ReferenceAssessment(
        status="new_org_project",
        repo_hints=hints,
        reason="no_org_or_central_match",
    )


def parse_capture_tags_from_text(text: str) -> tuple[str, ...]:
    match = CAPTURE_TAGS_RE.search(text)
    if not match:
        return ()
    return tuple(tag.strip().lower() for tag in match.group(1).split(",") if tag.strip())
