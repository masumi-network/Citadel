"""ADR-0005 step 2: selective promotion of seat-node content to Central.

A :class:`PromotionEngine` enumerates a personal seat node's content, decides
per item whether it is org-relevant and non-sensitive, and (when not a dry run)
promotes the qualifying items into the curated Central vault by reusing the
existing org-ready dual-write path. Every gate is conservative: an item is only
promoted when it is secret-clean AND classified relevant AND not sensitive AND
scores at/above the configured threshold. On ANY uncertainty — a blocked secret
scan, an LLM failure, unparseable output, or a missing field — the item is
SKIPPED. Promotion never happens on uncertainty.

``dry_run`` defaults to ``True``: the engine proposes promotions and writes
nothing. A human flips ``dry_run=False`` to actually promote.

The module mirrors the best-effort, no-raise style of :mod:`kb.llm_enrichment`:
classification failures degrade to a safe SKIP rather than raising.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import logging
from pathlib import Path
from typing import Any

from kb.access import AccessIdentity, AccessStore, is_seat_dataset, now_iso
from kb.config import CitadelConfig
from kb.learning import LearningProcess
from kb.llm_enrichment import (
    default_llm_model,
    openrouter_chat,
    parse_json_payload,
    redacted_preview,
)
from kb.promotion_queue import (
    APPROVED_STATUS,
    PENDING_STATUS,
    REJECTED_STATUS,
    build_pending_item,
    candidate_hash,
)
from kb.promotion_refs import ReferenceAssessment, assess_org_reference, parse_capture_tags_from_text
from kb.security_scan import SecurityScanEntry, scan_text_entries
from kb.service import Citadel

logger = logging.getLogger(__name__)

PROMOTION_TAG = "org-ready"
PERSONAL_CAPTURE_TAG = "personal"
ORG_WORK_CAPTURE_TAG = "org-work"
CAPTURE_SUMMARY_MARKER = "# capture summary:"

# Broad seed queries used to enumerate a seat node. cognee.recall is semantic,
# not an exhaustive listing, so a few complementary seeds widen coverage; the
# results are deduped and capped. This is best-effort top-N by design.
DEFAULT_SEED_QUERIES: tuple[str, ...] = (
    "notable knowledge, decisions, facts, and information",
    "project work, technical notes, and learnings",
)

CLASSIFIER_SYSTEM_PROMPT = (
    "You triage one piece of a person's private notes for promotion into a "
    "shared organization knowledge vault. Decide whether the content is "
    "organization-relevant (useful to teammates, about the company's projects, "
    "products, or shared work) and whether it is sensitive (personal, private, "
    "secret, credentials, financial, health, or otherwise unsafe to share "
    "org-wide). Return ONLY a JSON object shaped as "
    '{"relevant": true|false, "sensitive": true|false, "score": 0.0, '
    '"reason": "..."} where score is your 0..1 confidence that the content is '
    "both relevant AND safe to promote. Never include the original text or any "
    "secret in the reason."
)

CLASSIFIER_MAX_INPUT_CHARS = 6000


@dataclass(frozen=True)
class Classification:
    """A strict, validated classifier verdict for one candidate."""

    relevant: bool
    sensitive: bool
    score: float
    reason: str


@dataclass(frozen=True)
class PromotionCandidate:
    text: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProposedPromotion:
    """One candidate plus the promote/skip decision and why."""

    candidate: str
    decision: str  # "promote" | "skip" | "pending_approval"
    reason: str
    relevant: bool | None = None
    sensitive: bool | None = None
    score: float | None = None
    secret_blocked: bool = False
    promoted: bool = False
    reference_status: str | None = None
    capture_tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "relevant": self.relevant,
            "sensitive": self.sensitive,
            "score": self.score,
            "secret_blocked": self.secret_blocked,
            "promoted": self.promoted,
            "preview": redacted_preview(self.candidate),
            "reference_status": self.reference_status,
            "capture_tags": list(self.capture_tags),
        }


def _candidate_from_result(result: Any) -> PromotionCandidate | None:
    text = _candidate_text(result)
    if not text:
        return None
    tags: list[str] = []
    if isinstance(result, dict):
        raw_tags = result.get("tags")
        if isinstance(raw_tags, list):
            tags.extend(str(tag) for tag in raw_tags)
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            meta_tags = metadata.get("tags")
            if isinstance(meta_tags, list):
                tags.extend(str(tag) for tag in meta_tags)
    tags.extend(parse_capture_tags_from_text(text))
    normalized_tags = tuple(
        dict.fromkeys(tag.strip().lower() for tag in tags if tag.strip())
    )
    return PromotionCandidate(text=text, tags=normalized_tags)


def _candidate_text(result: Any) -> str:
    """Extract real node text using the same field priority as search dedup.

    Mirrors :func:`kb.server.search_result_dedup_key` so promotion reads the
    same body text the rest of the system treats as a node's content.
    """
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        for key in ("text", "content", "chunk", "body", "summary", "title"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def is_capture_root_content(candidate: PromotionCandidate) -> bool:
    lower = candidate.text.lower()
    if CAPTURE_SUMMARY_MARKER in lower or "capture root tags:" in lower:
        return True
    if "git-push" in candidate.tags or "capture" in candidate.tags:
        return True
    return False


def capture_auto_promote_block_reason(candidate: PromotionCandidate) -> str | None:
    """Return a skip reason when capture-root content may not auto-promote."""
    if not is_capture_root_content(candidate):
        return None
    if PERSONAL_CAPTURE_TAG in candidate.tags:
        return "capture_tag_personal"
    if ORG_WORK_CAPTURE_TAG in candidate.tags:
        return None
    return "capture_tag_not_org_work"


def _coerce_classification(parsed: Any) -> Classification | None:
    """Strictly validate the classifier JSON. Any deviation -> None (skip)."""
    if not isinstance(parsed, dict):
        return None
    relevant = parsed.get("relevant")
    sensitive = parsed.get("sensitive")
    score = parsed.get("score")
    reason = parsed.get("reason")
    if not isinstance(relevant, bool) or not isinstance(sensitive, bool):
        return None
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    score_value = float(score)
    if not 0.0 <= score_value <= 1.0:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return Classification(
        relevant=relevant,
        sensitive=sensitive,
        score=score_value,
        reason=reason.strip()[:300],
    )


def _central_write_key(text: str) -> str:
    """sha256 of the raw text: exactly the key ``Citadel.ingest`` dedupes on.

    NOT :func:`candidate_hash`, which strips and lowercases first. A key
    coarser than the guard it models suppresses writes that guard would have
    accepted: a case-variant of already-promoted content hashes differently
    at ingest but identically after normalization, so the normalized key
    changed a promotion decision. Promotion submits the candidate text
    verbatim, so this key and the ingest key agree byte for byte (with LLM
    enrichment enabled a full-tier write is chunked and ingest keys the
    chunks; the skip then still stops re-delivery of identical source text).
    """
    return sha256(text.encode("utf-8")).hexdigest()


class PromotionEnumerationError(RuntimeError):
    """Every seed query for a seat failed, so its content could not be read.

    Distinct from "this seat has nothing to promote": that is an empty list and
    a legitimate result. This means the search path itself is broken, and the
    caller must not record the seat as successfully processed.
    """


class SeatDatasetMissingError(PromotionEnumerationError):
    """The seat has no cognee Dataset row at all, so nothing can read it (#147).

    A subclass, not a sibling, so anything already catching
    PromotionEnumerationError keeps working. The point is the name: callers log
    ``exc.__class__.__name__``, and "every search failed" and "this seat was
    never provisioned" want different responses. The first is an outage to
    investigate. The second is one backfill run, and the seat has never worked
    for its holder even once.
    """


class PromotionEngine:
    """Selective seat-to-Central promotion (ADR-0005 step 2)."""

    def __init__(
        self,
        citadel: Citadel,
        learning: LearningProcess,
        access_store: AccessStore,
        config: CitadelConfig,
    ) -> None:
        self.citadel = citadel
        self.learning = learning
        self.access_store = access_store
        self.config = config
        # _central_write_key hashes of candidate text this engine has already
        # landed in Central (written and accepted, or confirmed already-present
        # by the ingest dedupe). Lets decide() skip the classifier for content
        # whose Central write can only be rejected as duplicate_in_process.
        # Keyed on the raw text so it is never coarser than the ingest key it
        # models, and instance-scoped on purpose: a fresh engine starts empty,
        # so it can only UNDER-report what the ingest dedupe knows; a stale
        # entry is impossible and a miss just costs one honest write.
        self._delivered_central_hashes: set[str] = set()

    async def enumerate(self, seat_dataset: str, max_items: int) -> list[PromotionCandidate]:
        """Best-effort list of a seat node's promotable content, capped at ``max_items``."""
        cap = max(1, max_items)
        seen: set[str] = set()
        candidates: list[PromotionCandidate] = []
        attempted = 0
        errors: list[str] = []
        for query in DEFAULT_SEED_QUERIES:
            if len(candidates) >= cap:
                break
            attempted += 1
            try:
                results = await self.citadel.search(
                    query, dataset=seat_dataset, top_k=cap
                )
            except Exception as exc:  # pragma: no cover - depends on Cognee runtime.
                logger.warning(
                    "promotion.enumerate search failed for %s: %s",
                    seat_dataset,
                    exc.__class__.__name__,
                )
                errors.append(exc.__class__.__name__)
                continue
            for result in results:
                parsed = _candidate_from_result(result)
                if not parsed:
                    continue
                key = parsed.text.lower()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(parsed)
                if len(candidates) >= cap:
                    break
        if attempted and len(errors) == attempted:
            # Every search we actually ran failed, so an empty candidate list
            # here means "could not look", not "nothing to promote". Returning []
            # made the caller count the seat as a clean zero: production logged
            # "seats=11 promoted=0 failures=0" while all 22 searches were dying
            # on the Kuzu lock and on DatasetNotFoundError.
            distinct = sorted(set(errors))
            if distinct == ["DatasetNotFoundError"]:
                # Matched on the class NAME because no kb module imports cognee
                # at module scope, and an except/isinstance check needs the
                # symbol there. enumerate already records names, not exceptions.
                #
                # The cost is that a cognee rename would silently stop the
                # discrimination without failing anything, so
                # test_promotion_matches_cognees_real_exception_name pins the
                # real class name to this literal.
                #
                # Both search-reachable raise sites, search.py:294 and
                # recall.py:595, import DatasetNotFoundError from
                # cognee.modules.data.exceptions. cognee 1.2.2 does define a
                # second class of the same name in api/v1/exceptions, but no
                # path a search reaches raises it.
                raise SeatDatasetMissingError(
                    f"{seat_dataset} has no cognee dataset row: all {attempted} "
                    "seed queries raised DatasetNotFoundError. The seat predates "
                    "dataset provisioning at creation; backfill it (#147)."
                )
            raise PromotionEnumerationError(
                f"all {attempted} seed queries failed for {seat_dataset}: "
                f"{', '.join(distinct)}"
            )
        return candidates[:cap]

    def classify(self, text: str) -> Classification | None:
        """Classify one candidate via the OpenRouter direct-HTTP helper.

        Returns ``None`` on ANY failure (missing key, HTTP/URL/timeout error,
        unparseable output, or a missing/malformed field) so the caller can
        deterministically SKIP. Never raises.

        Synchronous by design (plain urllib under run_with_retries, a 60s
        timeout per attempt plus backoff sleeps). Callers running on the event
        loop must dispatch it via ``asyncio.to_thread``; awaiting it inline
        stalls every other request for the duration of the call.
        """
        try:
            content = openrouter_chat(
                [
                    {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                    {"role": "user", "content": text[:CLASSIFIER_MAX_INPUT_CHARS]},
                ],
                model=default_llm_model(),
                operation="promotion.classify",
                max_tokens=300,
            )
        except Exception as exc:  # pragma: no cover - openrouter_chat is itself guarded.
            logger.warning(
                "promotion.classify call raised %s; skipping candidate",
                exc.__class__.__name__,
            )
            return None
        if content is None:
            return None
        return _coerce_classification(parse_json_payload(content))

    def _secret_blocked(self, seat_dataset: str, text: str) -> bool:
        """True when the candidate trips the blocking-severity secret scanner."""
        try:
            scan = scan_text_entries(
                [
                    SecurityScanEntry(
                        source="promotion", location=seat_dataset, text=text
                    )
                ],
                block_severity=self.config.content_scan_block_severity,
            )
        except Exception:  # pragma: no cover - scan is best-effort; fail closed.
            return True
        return bool(scan.get("blocked"))

    async def decide(
        self,
        seat_dataset: str,
        candidate: PromotionCandidate,
    ) -> ProposedPromotion:
        """Apply capture tags, secret scan, org refs, then LLM classifier."""
        capture_block = capture_auto_promote_block_reason(candidate)
        if capture_block == "capture_tag_personal":
            return ProposedPromotion(
                candidate=candidate.text,
                decision="skip",
                reason="capture_tag_personal",
                capture_tags=candidate.tags,
            )
        if self._secret_blocked(seat_dataset, candidate.text):
            return ProposedPromotion(
                candidate=candidate.text,
                decision="skip",
                reason="secret_content",
                secret_blocked=True,
                capture_tags=candidate.tags,
            )

        central = self.config.github_sync_dataset or self.config.default_dataset
        reference = await assess_org_reference(
            self.citadel,
            candidate_text=candidate.text,
            central_dataset=central,
            github_state_path=Path(self.config.github_sync_state_path),
            github_org=self.config.github_org,
        )

        if (
            reference.status == "known_org_work"
            and capture_block is None
            and _central_write_key(candidate.text) in self._delivered_central_hashes
        ):
            # This exact text already reached Central through this engine, and
            # on this reference path the only outcome a verdict could unlock is
            # a Central write the ingest dedupe will reject as
            # duplicate_in_process. Skip before paying for the classifier call.
            # One pass re-classified and re-submitted identical cross-seat
            # content ~150 times; every write bounced. Other reference statuses
            # (pending-approval queueing, no_reference_signal) still classify,
            # so no selection path changes; only doomed re-deliveries stop.
            return ProposedPromotion(
                candidate=candidate.text,
                decision="skip",
                reason="duplicate_in_process",
                reference_status=reference.status,
                capture_tags=candidate.tags,
            )

        # classify() blocks in urllib for up to 60s per retry attempt. Off the
        # loop it goes: this is plain HTTP to OpenRouter, NOT a cognee call, so
        # a worker thread is safe. The cognee reads above (enumerate, the
        # reference searches) must stay on this loop: cognee binds its async
        # engine to the first loop that touches it and Kuzu is single-writer.
        # Nine consecutive evolve passes spent 16-27 minutes here with the call
        # inline, and every route on the node queued behind it.
        verdict = await asyncio.to_thread(self.classify, candidate.text)
        if verdict is None:
            return ProposedPromotion(
                candidate=candidate.text,
                decision="skip",
                reason="llm_unavailable",
                reference_status=reference.status,
                capture_tags=candidate.tags,
            )

        threshold = self.config.promotion_relevance_threshold
        qualifies = (
            verdict.relevant
            and not verdict.sensitive
            and verdict.score >= threshold
        )
        base_kwargs = {
            "candidate": candidate.text,
            "relevant": verdict.relevant,
            "sensitive": verdict.sensitive,
            "score": verdict.score,
            "reference_status": reference.status,
            "capture_tags": candidate.tags,
        }

        if not qualifies:
            if verdict.sensitive:
                reason = "sensitive"
            elif not verdict.relevant:
                reason = "not_relevant"
            else:
                reason = "below_threshold"
            return ProposedPromotion(decision="skip", reason=reason, **base_kwargs)

        seat_slug = seat_dataset.removeprefix("seat:")
        content_hash = candidate_hash(candidate.text)
        if self.access_store.is_promotion_rejected(seat_slug, content_hash):
            return ProposedPromotion(
                decision="skip",
                reason="previously_rejected",
                **base_kwargs,
            )

        if reference.status == "new_org_project":
            return ProposedPromotion(
                decision="pending_approval",
                reason="new_org_project",
                **base_kwargs,
            )

        if reference.status == "known_org_work":
            if capture_block == "capture_tag_not_org_work":
                return ProposedPromotion(
                    decision="skip",
                    reason="capture_tag_not_org_work",
                    **base_kwargs,
                )
            return ProposedPromotion(
                decision="promote",
                reason=verdict.reason,
                **base_kwargs,
            )

        return ProposedPromotion(
            decision="skip",
            reason="no_org_reference",
            **base_kwargs,
        )

    def _promotion_identity(self, seat_dataset: str) -> AccessIdentity:
        """Synthetic admin/env identity whose default node IS the seat.

        ``resolve_write_targets`` only fires the seat-light + Central-full
        dual-write when ``identity.default_dataset`` is the seat node (the
        ``is_promotion`` branch). An ``env`` source + ``admin`` role bypasses the
        dataset allowlist so the engine can write both targets.
        """
        return AccessIdentity(
            role="admin",
            actor_id="promotion-engine",
            actor_kind="service_account",
            actor_name="promotion-engine",
            source="env",
            default_dataset=seat_dataset,
        )

    async def _promote(
        self,
        seat_dataset: str,
        identity: AccessIdentity,
        proposal: ProposedPromotion,
    ) -> str:
        """Promote one qualifying item via the org-ready dual-write path.

        Reuses ``resolve_write_targets`` (is_promotion -> [seat light, Central
        full]) + ``execute_learning_writes`` so the ADR-0005 step-1 secret gate
        re-runs inside ``learning.learn``. Records one audit event per promotion.
        On a secret block at write time the item is recorded as skipped, not
        promoted. Never raises out of the run loop.

        Returns ``"promoted"`` only when the Central write was actually
        accepted, ``"write_blocked"`` on a secret block or write error, and
        otherwise the ingest rejection reason (e.g. ``duplicate_in_process``).
        The outcome of ``execute_learning_writes`` used to be discarded, so a
        pass whose Central writes all bounced as duplicate_in_process still
        logged every one as ``success=True, accepted=True`` and inflated the
        promoted= count.
        """
        # Imported lazily to avoid a circular import (server imports promotion).
        from kb.security_scan import SecretContentError
        from kb.server import execute_learning_writes, resolve_write_targets

        seat_slug = seat_dataset.removeprefix("seat:")
        promotion_tags = [
            PROMOTION_TAG,
            "promotion-agent",
            f"promotion-seat:{seat_slug}",
        ]
        if proposal.reference_status:
            promotion_tags.append(f"promotion-ref:{proposal.reference_status}")

        central = None
        try:
            targets = resolve_write_targets(
                identity, None, promotion_tags, self.config
            )
            central = next(
                (t.dataset for t in targets if t.tier == "full"),
                targets[-1].dataset,
            )
            primary, _outcomes = await execute_learning_writes(
                self.learning,
                data=proposal.candidate,
                targets=targets,
                tags=promotion_tags,
                session_id=None,
                operation="promotion",
            )
        except SecretContentError as exc:
            self.access_store.record_event(
                action="promotion.promote",
                actor=identity,
                success=False,
                dataset=central or seat_dataset,
                detail={
                    "seat": seat_dataset,
                    "blocked": "secret_content",
                    "highest_severity": exc.highest_severity,
                    "accepted": False,
                    "score": proposal.score,
                    "relevant": proposal.relevant,
                    "sensitive": proposal.sensitive,
                    "reason": proposal.reason,
                },
            )
            return "write_blocked"
        except Exception as exc:  # pragma: no cover - depends on Cognee runtime.
            self.access_store.record_event(
                action="promotion.promote",
                actor=identity,
                success=False,
                dataset=central or seat_dataset,
                detail={
                    "seat": seat_dataset,
                    "error_type": exc.__class__.__name__,
                    "accepted": False,
                    "reason": proposal.reason,
                },
            )
            return "write_blocked"

        # The full-tier (Central) outcome is what "promoted" attests, so read
        # it instead of assuming the write landed. ``learn`` returns rejections
        # (duplicate_in_process, filter refusals) as accepted=False, not raises.
        content_hash = _central_write_key(proposal.candidate)
        if not primary.ingest.accepted:
            write_reason = primary.ingest.reason or "not_accepted"
            if write_reason == "duplicate_in_process":
                # Central already holds this exact text in this process, so
                # later identical candidates can skip their classifier call.
                self._delivered_central_hashes.add(content_hash)
            self.access_store.record_event(
                action="promotion.promote",
                actor=identity,
                success=False,
                dataset=central,
                detail={
                    "seat": seat_dataset,
                    "accepted": False,
                    "write_reason": write_reason,
                    "score": proposal.score,
                    "relevant": proposal.relevant,
                    "sensitive": proposal.sensitive,
                    "reason": proposal.reason,
                },
            )
            return write_reason

        self._delivered_central_hashes.add(content_hash)
        self.access_store.record_event(
            action="promotion.promote",
            actor=identity,
            success=True,
            dataset=central,
            detail={
                "seat": seat_dataset,
                "score": proposal.score,
                "relevant": proposal.relevant,
                "sensitive": proposal.sensitive,
                "reason": proposal.reason,
                "accepted": True,
                "tags": promotion_tags,
                "reference_status": proposal.reference_status,
                "capture_tags": list(proposal.capture_tags),
            },
        )
        return "promoted"

    async def run(
        self,
        seat_dataset: str,
        *,
        dry_run: bool = True,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        """Enumerate, decide, and (when ``dry_run=False``) promote.

        ``dry_run`` defaults to ``True``: returns the proposed promotions and
        writes NOTHING. ``dry_run=False`` actually promotes qualifying items via
        the org-ready dual-write and records one audit event each. Gated on
        ``config.promotion_enabled`` (opt-in): when disabled, returns a disabled
        status and does nothing. ``promoted`` counts only writes Central
        actually accepted; a rejected write settles as a skip carrying the
        server's rejection reason.
        """
        if not self.config.promotion_enabled:
            return {
                "ok": True,
                "enabled": False,
                "dry_run": dry_run,
                "dataset": seat_dataset,
                "reason": "disabled",
                "candidates": 0,
                "promoted": 0,
                "proposals": [],
            }
        if not is_seat_dataset(seat_dataset):
            raise ValueError(f"Not a seat dataset: {seat_dataset}")

        cap = max_items if max_items and max_items > 0 else self.config.promotion_max_items
        candidates = await self.enumerate(seat_dataset, cap)
        proposals: list[ProposedPromotion] = []
        for candidate in candidates:
            proposals.append(await self.decide(seat_dataset, candidate))

        promoted = 0
        queued = 0
        seat_slug = seat_dataset.removeprefix("seat:")
        if not dry_run:
            identity = self._promotion_identity(seat_dataset)
            settled: list[ProposedPromotion] = []
            for proposal in proposals:
                if proposal.decision == "pending_approval":
                    self._enqueue_pending(seat_slug, seat_dataset, proposal)
                    queued += 1
                    settled.append(proposal)
                    continue
                if proposal.decision != "promote":
                    settled.append(proposal)
                    continue
                outcome = await self._promote(seat_dataset, identity, proposal)
                if outcome == "promoted":
                    promoted += 1
                    settled.append(
                        ProposedPromotion(
                            candidate=proposal.candidate,
                            decision="promote",
                            reason=proposal.reason,
                            relevant=proposal.relevant,
                            sensitive=proposal.sensitive,
                            score=proposal.score,
                            promoted=True,
                            reference_status=proposal.reference_status,
                            capture_tags=proposal.capture_tags,
                        )
                    )
                elif outcome == "write_blocked":
                    settled.append(
                        ProposedPromotion(
                            candidate=proposal.candidate,
                            decision="skip",
                            reason="write_blocked",
                            relevant=proposal.relevant,
                            sensitive=proposal.sensitive,
                            score=proposal.score,
                            secret_blocked=True,
                            reference_status=proposal.reference_status,
                            capture_tags=proposal.capture_tags,
                        )
                    )
                else:
                    # The write ran but Central refused the content (e.g.
                    # duplicate_in_process). Not a secret block, and not a
                    # delivery: record what the server actually returned.
                    settled.append(
                        ProposedPromotion(
                            candidate=proposal.candidate,
                            decision="skip",
                            reason=outcome,
                            relevant=proposal.relevant,
                            sensitive=proposal.sensitive,
                            score=proposal.score,
                            reference_status=proposal.reference_status,
                            capture_tags=proposal.capture_tags,
                        )
                    )
            proposals = settled

        return {
            "ok": True,
            "enabled": True,
            "dry_run": dry_run,
            "dataset": seat_dataset,
            "max_items": cap,
            "candidates": len(candidates),
            "proposed": sum(1 for p in proposals if p.decision == "promote"),
            "pending_approval": sum(
                1 for p in proposals if p.decision == "pending_approval"
            ),
            "promoted": promoted,
            "queued": queued,
            "proposals": [p.to_dict() for p in proposals],
        }

    def _enqueue_pending(
        self,
        seat_slug: str,
        seat_dataset: str,
        proposal: ProposedPromotion,
    ) -> None:
        assessment = ReferenceAssessment(
            status=proposal.reference_status or "new_org_project",
            reason=proposal.reason,
        )
        item = build_pending_item(
            seat_slug=seat_slug,
            seat_dataset=seat_dataset,
            candidate_text=proposal.candidate,
            assessment=assessment,
            created_at=now_iso(),
            score=proposal.score,
            relevant=proposal.relevant,
            sensitive=proposal.sensitive,
            # Same threshold the decide() gate used, so the verdict stored on
            # the item cannot disagree with the gate that let it through.
            block_severity=self.config.content_scan_block_severity,
        )
        self.access_store.add_promotion_pending(item)
        self.access_store.record_event(
            action="promotion.pending",
            actor=self._promotion_identity(seat_dataset),
            success=True,
            dataset=seat_dataset,
            detail={
                "item_id": item.id,
                "seat_slug": seat_slug,
                "reference_status": item.reference_status,
                "preview": item.preview,
            },
        )

    async def approve_pending(
        self,
        item_id: str,
        actor: AccessIdentity,
        *,
        delegate: bool = False,
    ) -> dict[str, Any]:
        item = self.access_store.get_promotion_pending(item_id)
        if item is None:
            raise ValueError(f"Promotion item not found: {item_id}")
        if item.status != PENDING_STATUS:
            raise ValueError(f"Promotion item is not pending: {item_id}")

        proposal = ProposedPromotion(
            candidate=item.candidate_text,
            decision="promote",
            reason="approved",
            relevant=item.relevant,
            sensitive=item.sensitive,
            score=item.score,
            reference_status=item.reference_status,
        )
        identity = self._promotion_identity(item.seat_dataset)
        outcome = await self._promote(item.seat_dataset, identity, proposal)
        promoted = outcome == "promoted"
        decided = self.access_store.decide_promotion_pending(
            item_id,
            decision=APPROVED_STATUS,
            actor_id=actor.actor_id,
            actor_name=actor.actor_name,
            delegate=delegate,
        )
        self.access_store.record_event(
            action="promotion.approve",
            actor=actor,
            success=promoted,
            dataset=item.seat_dataset,
            detail={
                "item_id": item_id,
                "seat_slug": item.seat_slug,
                "delegate": delegate,
                "promoted": promoted,
                "reference_status": item.reference_status,
            },
        )
        return {
            "ok": promoted,
            "item": decided.to_dict(),
            "promoted": promoted,
            # The item is consumed either way, so the caller needs the server's
            # reason to tell "the write failed" from "this content is already
            # in Central" (an approval of the second of two identical pending
            # items is the ordinary way to hit the latter).
            "write_reason": None if promoted else outcome,
        }

    async def reject_pending(
        self,
        item_id: str,
        actor: AccessIdentity,
        *,
        delegate: bool = False,
    ) -> dict[str, Any]:
        item = self.access_store.get_promotion_pending(item_id)
        if item is None:
            raise ValueError(f"Promotion item not found: {item_id}")
        if item.status != PENDING_STATUS:
            raise ValueError(f"Promotion item is not pending: {item_id}")
        decided = self.access_store.decide_promotion_pending(
            item_id,
            decision=REJECTED_STATUS,
            actor_id=actor.actor_id,
            actor_name=actor.actor_name,
            delegate=delegate,
        )
        self.access_store.record_event(
            action="promotion.reject",
            actor=actor,
            success=True,
            dataset=item.seat_dataset,
            detail={
                "item_id": item_id,
                "seat_slug": item.seat_slug,
                "delegate": delegate,
            },
        )
        return {"ok": True, "item": decided.to_dict()}

    def status(self) -> dict[str, Any]:
        """Read-only config/status snapshot for the GET endpoint."""
        return {
            "enabled": self.config.promotion_enabled,
            "relevance_threshold": self.config.promotion_relevance_threshold,
            "max_items": self.config.promotion_max_items,
            "dry_run_default": True,
            "promotion_tag": PROMOTION_TAG,
        }
