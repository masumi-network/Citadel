"""Durable, redacted feedback events and bounded autonomous decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from kb.security_scan import redact_secrets
FEEDBACK_SCHEMA_VERSION = 1
DEFAULT_PROCESS_LIMIT = 100
MAX_PROCESS_LIMIT = 1000
MAX_ID_CHARS = 256
MAX_KIND_CHARS = 64
MAX_DATASET_CHARS = 128
MAX_TRUST_CHARS = 64
MAX_REASON_CHARS = 256
_ALLOWED_DECISIONS = frozenset(
    {"no_action", "ranking_eval_candidate", "projection_repair_candidate"}
)


class FeedbackStoreError(RuntimeError):
    """Base error for durable feedback operations."""


class FeedbackSchemaError(FeedbackStoreError):
    """Raised when a feedback database uses an unsupported schema version."""


class FeedbackValidationError(FeedbackStoreError, ValueError):
    """Raised when an event or decision input is malformed."""


class FeedbackConflictError(FeedbackStoreError):
    """Raised when an event already has a durable decision."""


class FeedbackNotFoundError(FeedbackStoreError):
    """Raised when a decision references no durable event."""


@dataclass(frozen=True)
class FeedbackEvent:
    event_id: str
    event_key: str
    kind: str
    search_id: str | None
    result_id: str | None
    actor_id: str | None
    dataset: str
    score: float | None
    trust_tier: str | None
    occurred_at: str
    created_at: str
    reason: str | None
    source_revision_id: str | None = None
    source_dataset: str | None = None

    @property
    def trust(self) -> str | None:
        return self.trust_tier

    @property
    def timestamp(self) -> str:
        return self.occurred_at


@dataclass(frozen=True)
class FeedbackDecision:
    decision_id: str
    event_id: str
    decision: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class FeedbackProcessResult:
    decisions: tuple[FeedbackDecision, ...]

    @property
    def processed_count(self) -> int:
        return len(self.decisions)

    @property
    def processed(self) -> int:
        return self.processed_count

    @property
    def count(self) -> int:
        return self.processed_count



def _utc_text(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


def _text(
    value: Any,
    *,
    name: str,
    limit: int | None,
    required: bool = False,
    redact: bool = True,
) -> str | None:
    if value is None:
        if required:
            raise FeedbackValidationError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise FeedbackValidationError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise FeedbackValidationError(f"{name} must be a non-empty string")
    if not value:
        return None
    if redact:
        value = redact_secrets(value)
    return value if limit is None else value[:limit]


def _score(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeedbackValidationError("score must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or not -1.0 <= normalized <= 1.0:
        raise FeedbackValidationError("score must be between -1 and 1")
    return normalized


def _event_values(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise FeedbackValidationError("event must be a mapping")
    kind_full = _text(event.get("kind"), name="kind", limit=None, required=True)
    assert kind_full is not None
    if len(kind_full) > MAX_KIND_CHARS:
        raise FeedbackValidationError(
            f"kind must be at most {MAX_KIND_CHARS} characters"
        )
    search_full = _text(event.get("search_id"), name="search_id", limit=None)
    result_full = _text(event.get("result_id"), name="result_id", limit=None)
    actor_full = _text(event.get("actor_id"), name="actor_id", limit=None)
    if search_full is None and result_full is None:
        raise FeedbackValidationError("search_id or result_id is required")
    dataset_full = _text(
        event.get("dataset"),
        name="dataset",
        limit=None,
        required=True,
    )
    assert dataset_full is not None
    trust_full = _text(
        event.get("trust_tier", event.get("trust")),
        name="trust_tier",
        limit=None,
    )
    source_revision_full = _text(
        event.get("source_revision_id"),
        name="source_revision_id",
        limit=None,
    )
    source_dataset_full = _text(
        event.get("source_dataset"),
        name="source_dataset",
        limit=None,
    )
    reason = _text(event.get("reason"), name="reason", limit=MAX_REASON_CHARS)
    occurred_at_value = event.get("occurred_at", event.get("timestamp"))
    if isinstance(occurred_at_value, datetime):
        occurred_at = _utc_text(occurred_at_value)
    elif occurred_at_value is None:
        occurred_at = _utc_text()
    else:
        if not isinstance(occurred_at_value, str) or not occurred_at_value.strip():
            raise FeedbackValidationError("occurred_at must be an ISO timestamp")
        try:
            occurred_at = _utc_text(datetime.fromisoformat(occurred_at_value.strip()))
        except ValueError as exc:
            raise FeedbackValidationError("occurred_at must be an ISO timestamp") from exc
    score = _score(event.get("score"))
    event_key = _canonical_event_key(
        kind_full,
        search_full,
        actor_full,
        result_full,
        score,
    )
    event_id = f"feedback-event:{hashlib.sha256(event_key.encode('utf-8')).hexdigest()[:32]}"
    return {
        "event_id": event_id,
        "event_key": event_key,
        "kind": kind_full[:MAX_KIND_CHARS],
        "search_id": search_full[:MAX_ID_CHARS] if search_full is not None else None,
        "result_id": result_full[:MAX_ID_CHARS] if result_full is not None else None,
        "actor_id": actor_full[:MAX_ID_CHARS] if actor_full is not None else None,
        "dataset": dataset_full[:MAX_DATASET_CHARS],
        "score": score,
        "trust_tier": trust_full[:MAX_TRUST_CHARS] if trust_full is not None else None,
        "source_revision_id": (
            source_revision_full[:MAX_ID_CHARS]
            if source_revision_full is not None
            else None
        ),
        "source_dataset": (
            source_dataset_full[:MAX_DATASET_CHARS]
            if source_dataset_full is not None
            else None
        ),
        "occurred_at": occurred_at,
        "reason": reason,
    }


def _identity_component(value: str | None) -> list[str]:
    if value is None:
        return ["none"]
    if len(value) <= MAX_ID_CHARS:
        return ["raw", value]
    return ["sha256", hashlib.sha256(value.encode("utf-8")).hexdigest()]


def _canonical_event_key(
    kind: str,
    search_id: str | None,
    actor_id: str | None,
    result_id: str | None,
    score: float | None,
) -> str:
    identity = json.dumps(
        [
            kind,
            _identity_component(search_id),
            _identity_component(actor_id),
            _identity_component(result_id),
            score,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"feedback:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


class FeedbackStore:
    """Small SQLite ledger for feedback events and one decision per event."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > FEEDBACK_SCHEMA_VERSION:
                    raise FeedbackSchemaError(
                        f"feedback schema {version} is newer than supported version "
                        f"{FEEDBACK_SCHEMA_VERSION}"
                    )
                if version == FEEDBACK_SCHEMA_VERSION:
                    connection.execute("COMMIT")
                    return
                if version != 0:
                    raise FeedbackSchemaError(
                        f"feedback schema {version} has no migration to version "
                        f"{FEEDBACK_SCHEMA_VERSION}"
                    )
                self._create_schema(connection)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE feedback_events (
                schema_version INTEGER NOT NULL,
                event_id TEXT PRIMARY KEY,
                event_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                search_id TEXT,
                result_id TEXT,
                actor_id TEXT,
                dataset TEXT NOT NULL,
                source_revision_id TEXT,
                source_dataset TEXT,
                score REAL,
                trust_tier TEXT,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reason TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE feedback_decisions (
                schema_version INTEGER NOT NULL,
                decision_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE
                    REFERENCES feedback_events(event_id) ON DELETE RESTRICT,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX feedback_events_unprocessed_idx "
            "ON feedback_events(created_at, event_id)"
        )
        connection.execute(f"PRAGMA user_version = {FEEDBACK_SCHEMA_VERSION}")



    @staticmethod
    def _event(row: sqlite3.Row) -> FeedbackEvent:
        return FeedbackEvent(
            event_id=str(row["event_id"]),
            event_key=str(row["event_key"]),
            kind=str(row["kind"]),
            search_id=(str(row["search_id"]) if row["search_id"] is not None else None),
            result_id=(str(row["result_id"]) if row["result_id"] is not None else None),
            actor_id=(str(row["actor_id"]) if row["actor_id"] is not None else None),
            dataset=str(row["dataset"]),
            source_revision_id=(
                str(row["source_revision_id"])
                if row["source_revision_id"] is not None
                else None
            ),
            source_dataset=(
                str(row["source_dataset"]) if row["source_dataset"] is not None else None
            ),
            score=(float(row["score"]) if row["score"] is not None else None),
            trust_tier=(str(row["trust_tier"]) if row["trust_tier"] is not None else None),
            occurred_at=str(row["occurred_at"]),
            created_at=str(row["created_at"]),
            reason=(str(row["reason"]) if row["reason"] is not None else None),
        )

    @staticmethod
    def _decision(row: sqlite3.Row) -> FeedbackDecision:
        return FeedbackDecision(
            decision_id=str(row["decision_id"]),
            event_id=str(row["event_id"]),
            decision=str(row["decision"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
        )

    def record_event(self, event: Mapping[str, Any]) -> str:
        """Insert one redacted event, returning its deterministic ID on duplicates."""
        values = _event_values(event)
        created_at = _utc_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO feedback_events (
                        schema_version, event_id, event_key, kind, search_id, result_id,
                        actor_id, dataset, source_revision_id, source_dataset,
                        score, trust_tier, occurred_at, created_at, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_key) DO NOTHING
                    """,
                    (
                        FEEDBACK_SCHEMA_VERSION,
                        values["event_id"],
                        values["event_key"],
                        values["kind"],
                        values["search_id"],
                        values["result_id"],
                        values["actor_id"],
                        values["dataset"],
                        values["source_revision_id"],
                        values["source_dataset"],
                        values["score"],
                        values["trust_tier"],
                        values["occurred_at"],
                        created_at,
                        values["reason"],
                    ),
                )
                row = connection.execute(
                    "SELECT event_id FROM feedback_events WHERE event_key = ?",
                    (values["event_key"],),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        if row is None:
            raise FeedbackStoreError("feedback event insert returned no event")
        return str(row["event_id"])

    def list_unprocessed(self, *, limit: int = DEFAULT_PROCESS_LIMIT) -> tuple[FeedbackEvent, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PROCESS_LIMIT:
            raise FeedbackValidationError(
                f"limit must be between 1 and {MAX_PROCESS_LIMIT}"
            )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event.*
                FROM feedback_events AS event
                LEFT JOIN feedback_decisions AS decision
                  ON decision.event_id = event.event_id
                WHERE decision.event_id IS NULL
                ORDER BY event.created_at, event.event_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def record_decision(self, event_id: str, decision: str, reason: str) -> FeedbackDecision:
        normalized_event_id = _text(
            event_id,
            name="event_id",
            limit=MAX_ID_CHARS,
            required=True,
            redact=False,
        )
        normalized_decision = _text(
            decision,
            name="decision",
            limit=64,
            required=True,
            redact=False,
        )
        normalized_reason = _text(
            reason,
            name="reason",
            limit=MAX_REASON_CHARS,
            required=True,
        )
        assert normalized_event_id is not None
        assert normalized_decision is not None
        assert normalized_reason is not None
        if normalized_decision not in _ALLOWED_DECISIONS:
            raise FeedbackValidationError(f"unsupported decision: {normalized_decision}")
        decision_id = f"feedback-decision:{hashlib.sha256((normalized_event_id + '|' + normalized_decision).encode('utf-8')).hexdigest()[:32]}"
        created_at = _utc_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    "SELECT 1 FROM feedback_events WHERE event_id = ?",
                    (normalized_event_id,),
                ).fetchone()
                if exists is None:
                    raise FeedbackNotFoundError(f"feedback event not found: {normalized_event_id}")
                existing = connection.execute(
                    "SELECT 1 FROM feedback_decisions WHERE event_id = ?",
                    (normalized_event_id,),
                ).fetchone()
                if existing is not None:
                    raise FeedbackConflictError(
                        f"feedback event already has a decision: {normalized_event_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO feedback_decisions (
                        schema_version, decision_id, event_id, decision, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        FEEDBACK_SCHEMA_VERSION,
                        decision_id,
                        normalized_event_id,
                        normalized_decision,
                        normalized_reason,
                        created_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM feedback_decisions WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if row is None:
            raise FeedbackStoreError("feedback decision insert returned no decision")
        return self._decision(row)

    def list_decisions(self, *, limit: int = MAX_PROCESS_LIMIT) -> tuple[FeedbackDecision, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PROCESS_LIMIT:
            raise FeedbackValidationError(
                f"limit must be between 1 and {MAX_PROCESS_LIMIT}"
            )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM feedback_decisions ORDER BY created_at, decision_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._decision(row) for row in rows)


def _operation_has_missing_receipt(operation: Any) -> bool:
    receipts = {
        str(receipt.backend)
        for receipt in getattr(operation, "receipts", ())
    }
    required = tuple(
        getattr(
            getattr(operation, "job", None),
            "required_backends",
            ("relational", "vector", "graph"),
        )
    )
    return any(backend not in receipts for backend in required)


def _has_lifecycle_receipt_gap(lifecycle: Any, event: FeedbackEvent) -> bool:
    """Return true only when lifecycle APIs attest a current receipt gap."""
    source_revision_id = event.source_revision_id or event.result_id
    source_dataset = event.source_dataset or event.dataset
    if not source_revision_id or not source_dataset:
        return False
    for name in (
        "has_missing_searchable_receipt",
        "has_projection_receipt_gap",
        "projection_receipt_gap",
    ):
        checker = getattr(lifecycle, name, None)
        if not callable(checker):
            continue
        try:
            return bool(checker(dataset=source_dataset, result_id=source_revision_id))
        except TypeError:
            try:
                return bool(checker(source_dataset, source_revision_id))
            except (TypeError, ValueError, KeyError, LookupError, RuntimeError, sqlite3.Error):
                return False
        except (ValueError, KeyError, LookupError, RuntimeError, sqlite3.Error):
            return False

    exact_lookup = getattr(lifecycle, "get_operation_for_source_revision", None)
    if callable(exact_lookup):
        try:
            operation = exact_lookup(source_revision_id, dataset=source_dataset)
        except TypeError:
            operation = exact_lookup(source_revision_id, source_dataset)
        except (AttributeError, KeyError, LookupError, RuntimeError, sqlite3.Error):
            return False
        if operation is not None:
            return _operation_has_missing_receipt(operation)

    get_revision = getattr(lifecycle, "get_source_revision", None)
    current_revisions = getattr(lifecycle, "current_revisions_for_source", None)
    latest_operations = getattr(lifecycle, "latest_operations_for_dataset", None)
    if (
        not callable(get_revision)
        or not callable(current_revisions)
        or not callable(latest_operations)
    ):
        return False
    try:
        revision = get_revision(source_revision_id)
        if revision is None:
            current_candidates = current_revisions(
                source_dataset,
                source_revision_id,
                include_chunks=False,
            )
            if not current_candidates:
                return False
            revision = get_revision(current_candidates[0].source_revision_id)
        if revision is None or revision.dataset != source_dataset or revision.tombstone:
            return False
        current = current_revisions(source_dataset, revision.source_key, include_chunks=False)
        if not current or current[0].source_revision_id != revision.source_revision_id:
            return False
        operations = latest_operations(source_dataset, limit=50)
        operation = next(
            (
                candidate
                for candidate in operations
                if candidate.source_revision.source_revision_id == revision.source_revision_id
            ),
            None,
        )
        if operation is None:
            return False
        return _operation_has_missing_receipt(operation)
    except (
        AttributeError,
        IndexError,
        TypeError,
        ValueError,
        KeyError,
        LookupError,
        RuntimeError,
        sqlite3.Error,
    ):
        return False


def _decision_for_event(event: FeedbackEvent, lifecycle: Any) -> tuple[str, str]:
    if _has_lifecycle_receipt_gap(lifecycle, event):
        return (
            "projection_repair_candidate",
            "current lifecycle revision is missing a required searchable receipt",
        )
    if (
        event.kind in {"explicit", "explicit_feedback", "explicit_rating"}
        and event.score in {-1.0, 1.0}
    ):
        return (
            "ranking_eval_candidate",
            "explicit rating recorded for bounded ranking evaluation",
        )
    return "no_action", "telemetry is recorded without autonomous mutation"


def process_feedback_events(
    store: FeedbackStore,
    lifecycle: Any,
    *,
    limit: int = DEFAULT_PROCESS_LIMIT,
) -> FeedbackProcessResult:
    """Process a bounded batch without LLM, promotion, or memory mutation."""
    decisions: list[FeedbackDecision] = []
    for event in store.list_unprocessed(limit=limit):
        decision, reason = _decision_for_event(event, lifecycle)
        try:
            decisions.append(store.record_decision(event.event_id, decision, reason))
        except FeedbackConflictError:
            continue
    return FeedbackProcessResult(decisions=tuple(decisions))
