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


# How far a write got, reported from what was OBSERVED rather than requested.
#
# ``accepted`` describes a REQUEST: it is True as soon as ``cognee.add()``
# returns. ``add`` writes the relational and vector stores; only ``cognify``
# writes the Kuzu graph, and that is what makes a document retrievable. Cognify
# never runs synchronously inside a write, so no write can report "indexed" at
# the moment it returns. Saying so out loud is the point of this field: a caller
# gets a state it can branch on instead of reading ``accepted`` as "landed".
INDEX_STATE_UNKNOWN = "unknown"
"""Nothing was observed. Never a success claim; the safe default."""

INDEX_STATE_REJECTED = "rejected"
"""The write never happened (filtered, duplicate, blocked)."""

INDEX_STATE_PENDING = "pending_cognify"
"""Stored, and a graph write is owed. NOT yet retrievable, and not a failure."""

INDEX_STATE_NOT_SCHEDULED = "cognify_not_scheduled"
"""Observed: no cognify was scheduled, so nothing will index this write."""

INDEX_STATE_FAILED = "cognify_failed"
"""Observed: the cognify covering this write ran and failed."""

INDEX_STATE_INDEXED = "indexed"
"""Observed: a cognify covering this write completed successfully."""

# States that positively answer "is this write NOT in the graph?". Everything
# outside these and INDEX_STATE_INDEXED is genuinely undetermined.
_INDEX_STATES_NOT_INDEXED = frozenset(
    {INDEX_STATE_REJECTED, INDEX_STATE_NOT_SCHEDULED, INDEX_STATE_FAILED}
)


def index_flag(index_state: str) -> bool | None:
    """Tri-state view of an index state: True / False / None for "not observed".

    ``None`` is deliberate and load-bearing. A caller that forgets to branch and
    writes ``bool(flag)`` gets ``False`` for an unobserved write, so the failure
    mode of forgetting is pessimism, not a fabricated success.
    """
    if index_state == INDEX_STATE_INDEXED:
        return True
    if index_state in _INDEX_STATES_NOT_INDEXED:
        return False
    return None


def resolve_index_state(index_state: str, cognify_status: str | None) -> str:
    """Refine a write's index state with an OBSERVED cognify outcome.

    ``cognify_status`` is what the caller watched happen to the cognify covering
    this write: ``"ok"``, ``"failed"``, ``"not_scheduled"``, or ``None`` when it
    watched nothing. Only a status promotes the state; ``None`` leaves it where
    it was, because "I did not look" is not evidence of success.

    A write that never happened is neither indexed nor a cognify failure, so a
    rejected one keeps its state whatever the cognify did. An ``unknown`` write
    can still be moved in the pessimistic direction — a cognify observed to
    fail means nothing landed in the graph either way — but never to
    ``indexed``, because there is no evidence the write itself reached the
    store.
    """
    if index_state == INDEX_STATE_REJECTED:
        return index_state
    if index_state == INDEX_STATE_UNKNOWN and cognify_status == "ok":
        return index_state
    if cognify_status == "ok":
        return INDEX_STATE_INDEXED
    if cognify_status == "failed":
        return INDEX_STATE_FAILED
    if cognify_status == "not_scheduled":
        return INDEX_STATE_NOT_SCHEDULED
    return index_state


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    reason: str
    dataset: str
    tags: tuple[str, ...]
    cognee_result: Any = None
    # What was OBSERVED about the graph write, as opposed to what was requested.
    index_state: str = INDEX_STATE_UNKNOWN
    # The ids cognee assigned, so a later pass can check this claim against the
    # index instead of trusting its own bookkeeping. Empty is not proof of
    # failure — some clients return a shape we cannot read ids from.
    cognee_data_ids: tuple[str, ...] = ()

    @property
    def indexed(self) -> bool | None:
        """True only when a cognify covering this write was observed to finish.

        ``None`` means not yet observed. Read :func:`index_flag` for why that
        matters more than it looks.
        """
        return index_flag(self.index_state)


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
