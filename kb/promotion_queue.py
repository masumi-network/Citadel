"""ADR-0007 P6: pending promotion approval queue stored in the access JSON."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import logging
from typing import Any
from uuid import uuid4

from kb.llm_enrichment import redacted_preview
from kb.promotion_refs import ReferenceAssessment

logger = logging.getLogger(__name__)

PENDING_STATUS = "pending"
CANDIDATE_SCAN_SOURCE = "promotion_candidate"
APPROVED_STATUS = "approved"
REJECTED_STATUS = "rejected"
VALID_PENDING_STATUSES = frozenset({PENDING_STATUS, APPROVED_STATUS, REJECTED_STATUS})


@dataclass(frozen=True)
class PromotionPendingItem:
    id: str
    seat_slug: str
    seat_dataset: str
    candidate_text: str
    candidate_hash: str
    preview: str
    reference_status: str
    reference_reason: str
    repo_hints: tuple[str, ...] = ()
    status: str = PENDING_STATUS
    created_at: str = ""
    decided_at: str | None = None
    decided_by: str | None = None
    decided_by_name: str | None = None
    delegate: bool = False
    score: float | None = None
    relevant: bool | None = None
    sensitive: bool | None = None
    # Result of the real secret scan over candidate_text. None means the item
    # predates the scan and has not been checked; it must never be rendered as
    # "passed". `sensitive` above is unrelated: it is an LLM judgement.
    secret_scan: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["repo_hints"] = list(self.repo_hints)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PromotionPendingItem":
        status = data.get("status") or PENDING_STATUS
        if status not in VALID_PENDING_STATUSES:
            raise ValueError(f"Unsupported promotion pending status: {status}")
        return cls(
            id=str(data["id"]),
            seat_slug=str(data["seat_slug"]),
            seat_dataset=str(data["seat_dataset"]),
            candidate_text=str(data["candidate_text"]),
            candidate_hash=str(data["candidate_hash"]),
            preview=str(data.get("preview") or ""),
            reference_status=str(data.get("reference_status") or ""),
            reference_reason=str(data.get("reference_reason") or ""),
            repo_hints=tuple(data.get("repo_hints") or ()),
            status=status,
            created_at=str(data.get("created_at") or ""),
            decided_at=data.get("decided_at"),
            decided_by=data.get("decided_by"),
            decided_by_name=data.get("decided_by_name"),
            delegate=bool(data.get("delegate")),
            score=data.get("score"),
            relevant=data.get("relevant"),
            sensitive=data.get("sensitive"),
            secret_scan=data.get("secret_scan"),
        )


def candidate_hash(text: str) -> str:
    normalized = text.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def scan_candidate(text: str, *, block_severity: str = "high") -> dict[str, Any]:
    """Run the secret scanner over a promotion candidate.

    The Review view states "secret scan passed" per candidate. Until this
    existed nothing scanned a candidate at all: the queue carried `sensitive`
    from LLM enrichment, which is a model's opinion about tone, not a check for
    credentials. Asserting a security property the system never tested is worse
    than asserting nothing, so the claim is now backed by the same scanner the
    GitHub sync path uses.

    The returned dict is safe to serve to a reviewer. `scan_text_entries` keeps
    every matched value out of its output: the raw match goes only into a
    SHA-256 fingerprint, `evidence` is metadata such as `pattern=<category>`,
    and `summary` is a fixed string. The findings are reduced further here,
    dropping `summary` because the scanner's prose says "in GitHub metadata",
    which is the wrong provenance for a candidate note and would mislead the
    person reading it.
    """
    from kb.security_scan import SecurityScanEntry, scan_text_entries

    result = scan_text_entries(
        [SecurityScanEntry(source=CANDIDATE_SCAN_SOURCE, location="candidate", text=text or "")],
        block_severity=block_severity,
    )
    findings = [
        {
            "severity": finding.get("severity"),
            "category": finding.get("category"),
            "evidence": finding.get("evidence"),
            "fingerprint": finding.get("fingerprint"),
        }
        for finding in (result.get("findings") or [])
    ]
    return {
        "ok": bool(result.get("ok")),
        "blocked": bool(result.get("blocked")),
        "highest_severity": result.get("highest_severity"),
        "finding_count": int(result.get("finding_count") or 0),
        "findings": findings,
        "scanned_at": None,
    }


def build_pending_item(
    *,
    seat_slug: str,
    seat_dataset: str,
    candidate_text: str,
    assessment: ReferenceAssessment,
    created_at: str,
    score: float | None = None,
    relevant: bool | None = None,
    sensitive: bool | None = None,
    block_severity: str = "high",
) -> PromotionPendingItem:
    # Scan at enqueue time so the result is stored with the candidate and a
    # reviewer sees the same verdict every time they open the queue. A scanner
    # fault must not stop a candidate being queued, so a failure leaves
    # secret_scan as None, which reads as "not scanned" rather than "passed".
    try:
        scan = scan_candidate(candidate_text, block_severity=block_severity)
        scan["scanned_at"] = created_at
    except Exception:  # pragma: no cover - defensive; the scanner is pure regex
        logger.exception("Secret scan failed for a promotion candidate")
        scan = None

    return PromotionPendingItem(
        id=f"promo_{uuid4().hex}",
        seat_slug=seat_slug,
        seat_dataset=seat_dataset,
        candidate_text=candidate_text,
        candidate_hash=candidate_hash(candidate_text),
        preview=redacted_preview(candidate_text),
        reference_status=assessment.status,
        reference_reason=assessment.reason,
        repo_hints=assessment.repo_hints,
        created_at=created_at,
        score=score,
        relevant=relevant,
        sensitive=sensitive,
        secret_scan=scan,
    )
