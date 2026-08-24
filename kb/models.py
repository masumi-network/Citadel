from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IngestDecision:
    accepted: bool
    reason: str = "accepted"


@dataclass(frozen=True)
class IngestRequest:
    data: str
    dataset: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    session_id: str | None = None


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    reason: str
    dataset: str
    tags: tuple[str, ...]
    cognee_result: Any = None
    source_revision_id: str | None = None
    projection_job_id: str | None = None
    projection_state: str | None = None
    source_searchable: bool | None = None


@dataclass(frozen=True)
class FeedbackRequest:
    qa_id: str
    score: int | None = None
    text: str | None = None
    session_id: str | None = None
    dataset: str | None = None


@dataclass(frozen=True)
class FeedbackResult:
    recorded: bool
    improved: bool
    # ok mirrors recorded so _result_exit maps a dropped feedback to a nonzero
    # CLI exit / honest API payload instead of a silent recorded:false, exit 0.
    ok: bool = True
    reason: str | None = None
