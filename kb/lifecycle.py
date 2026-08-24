"""Citadel-owned source revisions and projection receipts.

The SQLite transaction in :meth:`LifecycleStore.accept_source` is the durable
acceptance boundary. Source bytes, the current source head, projection work,
and initial backend receipts commit together.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from kb.search_format import linear_issue_url_identifier, parse_content_header


LIFECYCLE_SCHEMA_VERSION = 1
REQUIRED_BACKENDS = ("relational", "vector", "graph")
LIFECYCLE_CHUNK_SOURCE_PREFIX = "citadel:chunk:v1:"
_RECEIPT_STATES = {"pending", "running", "completed", "searchable", "failed", "stale"}
_DEFERRED_GRAPH_ERROR = "graph_enrichment_deferred"
_LEXICAL_TOKEN_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_LEXICAL_PASSAGE_CHARS = 2400


def _lexical_passages(text: str) -> tuple[str, ...]:
    passages: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        passages.extend(
            paragraph[start : start + _LEXICAL_PASSAGE_CHARS]
            for start in range(0, len(paragraph), _LEXICAL_PASSAGE_CHARS)
        )
    return tuple(passages)


class LifecycleError(RuntimeError):
    """Base error for the durable lifecycle boundary."""


class LifecycleSchemaError(LifecycleError):
    """Raised when the store uses an unsupported schema version."""


class LifecycleConflictError(LifecycleError):
    """Raised when one deterministic operation ID has conflicting inputs."""


class LifecycleRequeueIdentityMismatchError(LifecycleConflictError):
    """Raised when a recovery request does not match the active worker."""

    def __init__(self, projection: ProjectionRequest) -> None:
        self.generation_id = projection.generation_id
        self.projection_version = projection.projection_version
        self.config_digest = projection.config_digest
        super().__init__("recovery identity does not match the active lifecycle worker")


class LifecycleRequeueDriftError(LifecycleConflictError):
    """Raised when failed jobs changed after the recovery preview."""

    def __init__(
        self,
        *,
        expected_count: int,
        actual_candidate_ids: tuple[str, ...],
    ) -> None:
        self.expected_count = expected_count
        self.actual_candidate_ids = actual_candidate_ids
        super().__init__("failed projection candidates changed after preview")


class LifecycleNotFoundError(LifecycleError):
    """Raised when a requested lifecycle record does not exist."""


class ProjectionLeaseError(LifecycleError):
    """Raised when a worker no longer owns a projection job."""


@dataclass(frozen=True)
class CaptureContext:
    dataset: str
    source_key: str
    source_locator: str | None
    media_type: str
    capture_actor_id: str
    capture_run_id: str | None
    captured_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectionRequest:
    generation_id: str
    projection_version: str
    config_digest: str
    providers: Mapping[str, str]


@dataclass(frozen=True)
class SourceRevision:
    schema_version: int
    source_revision_id: str
    source_key: str
    dataset: str
    content_sha256: str
    byte_length: int
    retained_content_ref: str
    source_locator: str | None
    media_type: str
    previous_revision_id: str | None
    capture_actor_id: str
    capture_run_id: str | None
    capture_metadata: Mapping[str, Any]
    captured_at: str
    accepted_at: str
    tombstone: bool


@dataclass(frozen=True)
class ProjectionJob:
    schema_version: int
    projection_job_id: str
    source_revision_id: str
    generation_id: str
    dataset: str
    projection_version: str
    config_digest: str
    required_backends: tuple[str, ...]
    idempotency_key: str
    state: str
    attempt: int
    lease_id: str | None
    lease_owner: str | None
    leased_until: str | None
    available_at: str
    created_at: str
    updated_at: str
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True)
class ProjectionReceipt:
    schema_version: int
    projection_receipt_id: str
    projection_job_id: str
    source_revision_id: str
    generation_id: str
    dataset: str
    backend: str
    provider: str
    projection_version: str
    state: str
    attempt: int
    provider_operation_id: str | None
    affected_ids: tuple[str, ...]
    affected_count: int | None
    model: str | None
    dimensions: int | None
    metadata: Mapping[str, Any]
    created_at: str
    updated_at: str
    completed_at: str | None
    searchable_at: str | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class ProjectionOperation:
    source_revision: SourceRevision
    job: ProjectionJob
    receipts: tuple[ProjectionReceipt, ...]
    state: str


@dataclass(frozen=True)
class SourceAcceptance:
    accepted: bool
    source_revision_id: str
    projection_job_id: str
    operation: ProjectionOperation


@dataclass(frozen=True)
class LifecycleCensus:
    source_revisions: int
    current_sources: int
    retained_bytes: int
    projection_jobs: int
    projection_receipts: int
    job_states: Mapping[str, int]
    receipt_states: Mapping[str, int]


@dataclass(frozen=True)
class GenerationCensus:
    generation_id: str
    projection_version: str
    config_digest: str
    current_sources: int
    current_projection_jobs: int
    current_projection_receipts: int
    current_job_states: Mapping[str, int]
    current_receipt_states: Mapping[str, int]
    current_receipts_by_backend: Mapping[str, int]
    current_searchable_by_backend: Mapping[str, int]


@dataclass(frozen=True)
class CurrentHeadReceiptEvidence:
    projection_receipt_id: str
    backend: str
    provider: str
    state: str


@dataclass(frozen=True)
class CurrentHeadProjectionEvidence:
    source_key: str
    dataset: str
    source_revision_id: str
    projection_job_id: str
    generation_id: str
    projection_version: str
    config_digest: str
    state: str
    receipts: tuple[CurrentHeadReceiptEvidence, ...]


@dataclass(frozen=True)
class CurrentHeadEvidenceError:
    code: str
    source_key: str
    source_revision_id: str | None = None
    projection_job_ids: tuple[str, ...] = ()
    job_state: str | None = None
    backend_states: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CurrentHeadEvidenceResult:
    ok: bool
    dataset: str
    generation_id: str
    projection_version: str
    config_digest: str
    evidence: tuple[CurrentHeadProjectionEvidence, ...]
    errors: tuple[CurrentHeadEvidenceError, ...]


@dataclass(frozen=True)
class ProjectionLease:
    projection_job_id: str
    lease_id: str
    worker_id: str
    leased_until: str
    attempt: int


@dataclass(frozen=True)
class RetrievalBinding:
    source_revision: SourceRevision
    current: bool
    receipt: ProjectionReceipt | None


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("lifecycle timestamps must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise LifecycleSchemaError(f"lifecycle timestamp has no timezone: {value!r}")
    return parsed.astimezone(UTC)


def _stable_id(kind: str, *parts: str) -> str:
    material = json.dumps(
        ["citadel", "lifecycle", "v1", kind, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return str(uuid5(NAMESPACE_URL, material))


def _source_revision_id(
    dataset: str,
    source_key: str,
    content_sha256: str,
    *,
    tombstone: bool,
) -> str:
    return _stable_id(
        "source",
        dataset,
        source_key,
        content_sha256,
        "tombstone" if tombstone else "content",
    )


def lifecycle_chunk_source_key(source_key: str, chunk_index: int) -> str:
    """Return one reserved deterministic child key for a logical source chunk."""
    if not source_key:
        raise ValueError("source_key must be a non-empty string")
    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")
    parent_id = _stable_id("chunk-parent", source_key)
    return f"{LIFECYCLE_CHUNK_SOURCE_PREFIX}{parent_id}:{chunk_index}"


def _projection_job_id(
    source_revision_id: str,
    generation_id: str,
    projection_version: str,
) -> str:
    return _stable_id("job", source_revision_id, generation_id, projection_version)


def _projection_receipt_id(projection_job_id: str, backend: str) -> str:
    return _stable_id("receipt", projection_job_id, backend)


class LifecycleStore:
    """SQLite lifecycle ledger with deterministic retry identities."""

    def __init__(
        self,
        path: str | Path,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._fault_injector = fault_injector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _inject_fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=120,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 120000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > LIFECYCLE_SCHEMA_VERSION:
                raise LifecycleSchemaError(
                    f"lifecycle schema {version} is newer than supported version "
                    f"{LIFECYCLE_SCHEMA_VERSION}"
                )
            if version == LIFECYCLE_SCHEMA_VERSION:
                return
            if version != 0:
                raise LifecycleSchemaError(
                    f"lifecycle schema {version} has no migration to version "
                    f"{LIFECYCLE_SCHEMA_VERSION}"
                )
            self._create_schema(connection)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        try:
            schema = """
                BEGIN IMMEDIATE;
                CREATE TABLE source_revisions (
                    schema_version INTEGER NOT NULL,
                    source_revision_id TEXT PRIMARY KEY,
                    source_key TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
                    retained_content BLOB NOT NULL,
                    retained_content_ref TEXT NOT NULL,
                    source_locator TEXT,
                    media_type TEXT NOT NULL,
                    previous_revision_id TEXT REFERENCES source_revisions(source_revision_id),
                    capture_actor_id TEXT NOT NULL,
                    capture_run_id TEXT,
                    capture_metadata_json TEXT NOT NULL DEFAULT '{}',
                    captured_at TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    tombstone INTEGER NOT NULL CHECK (tombstone IN (0, 1)),
                    UNIQUE(dataset, source_key, content_sha256)
                );

                CREATE TABLE source_heads (
                    dataset TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    source_revision_id TEXT NOT NULL REFERENCES source_revisions(source_revision_id),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (dataset, source_key)
                );

                CREATE TABLE projection_jobs (
                    schema_version INTEGER NOT NULL,
                    projection_job_id TEXT PRIMARY KEY,
                    source_revision_id TEXT NOT NULL REFERENCES source_revisions(source_revision_id),
                    generation_id TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    projection_version TEXT NOT NULL,
                    config_digest TEXT NOT NULL,
                    required_backends_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                    lease_id TEXT,
                    lease_owner TEXT,
                    leased_until TEXT,
                    available_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error_code TEXT,
                    last_error_message TEXT,
                    UNIQUE(source_revision_id, generation_id, projection_version)
                );

                CREATE TABLE projection_receipts (
                    schema_version INTEGER NOT NULL,
                    projection_receipt_id TEXT PRIMARY KEY,
                    projection_job_id TEXT NOT NULL REFERENCES projection_jobs(projection_job_id),
                    source_revision_id TEXT NOT NULL REFERENCES source_revisions(source_revision_id),
                    generation_id TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    projection_version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                    provider_operation_id TEXT,
                    affected_ids_json TEXT NOT NULL DEFAULT '[]',
                    affected_count INTEGER,
                    model TEXT,
                    dimensions INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    searchable_at TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    UNIQUE(projection_job_id, backend)
                );

                CREATE INDEX projection_jobs_due_idx
                    ON projection_jobs(state, available_at, created_at);
                CREATE INDEX projection_receipts_source_idx
                    ON projection_receipts(source_revision_id, state);
                PRAGMA user_version = __LIFECYCLE_SCHEMA_VERSION__;
                COMMIT;
                """
            connection.executescript(
                schema.replace(
                    "__LIFECYCLE_SCHEMA_VERSION__",
                    str(LIFECYCLE_SCHEMA_VERSION),
                )
            )
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def accept_source(
        self,
        content: bytes,
        *,
        capture: CaptureContext,
        projection: ProjectionRequest,
        now: datetime | None = None,
        tombstone: bool = False,
    ) -> SourceAcceptance:
        """Atomically retain a source revision and queue its projection work."""
        if not isinstance(content, bytes):
            raise TypeError("lifecycle content must be bytes")
        self._validate_capture(capture)
        providers = self._validate_projection(projection)
        accepted_at = _utc_text(now or datetime.now(UTC))
        captured_at = _utc_text(capture.captured_at)
        capture_metadata_json = json.dumps(
            dict(capture.metadata),
            sort_keys=True,
            separators=(",", ":"),
        )
        content_digest = sha256(content).hexdigest()
        revision_id = _source_revision_id(
            capture.dataset,
            capture.source_key,
            content_digest,
            tombstone=tombstone,
        )
        job_id = _projection_job_id(
            revision_id,
            projection.generation_id,
            projection.projection_version,
        )
        retained_ref = f"citadel-sqlite:source-revisions/{revision_id}/content"
        idempotency_key = _stable_id(
            "projection",
            revision_id,
            projection.generation_id,
            projection.projection_version,
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_generation_binding(
                    connection,
                    projection,
                    providers,
                )
                head = connection.execute(
                    """
                    SELECT source_revision_id
                    FROM source_heads
                    WHERE dataset = ? AND source_key = ?
                    """,
                    (capture.dataset, capture.source_key),
                ).fetchone()
                previous_revision_id = str(head[0]) if head is not None else None
                existing_revision = connection.execute(
                    """
                    SELECT dataset, source_key, content_sha256, tombstone
                    FROM source_revisions
                    WHERE source_revision_id = ?
                    """,
                    (revision_id,),
                ).fetchone()
                if existing_revision is not None and (
                    str(existing_revision["dataset"]) != capture.dataset
                    or str(existing_revision["source_key"]) != capture.source_key
                    or str(existing_revision["content_sha256"]) != content_digest
                    or bool(existing_revision["tombstone"]) != tombstone
                ):
                    raise LifecycleConflictError(
                        "deterministic source revision ID has conflicting inputs"
                    )
                existing = connection.execute(
                    """
                    SELECT projection_job_id, config_digest, required_backends_json, state
                    FROM projection_jobs
                    WHERE projection_job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if existing is not None:
                    self._assert_idempotent_job(
                        connection,
                        existing,
                        projection,
                        providers,
                    )
                    if (
                        previous_revision_id == revision_id
                        and str(existing["state"]) != "failed"
                    ):
                        connection.execute("COMMIT")
                        return self._acceptance(job_id)

                if existing_revision is None:
                    connection.execute(
                        """
                        INSERT INTO source_revisions (
                            schema_version, source_revision_id, source_key, dataset,
                            content_sha256, byte_length, retained_content, retained_content_ref,
                            source_locator, media_type, previous_revision_id, capture_actor_id,
                            capture_run_id, capture_metadata_json, captured_at, accepted_at,
                            tombstone
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            LIFECYCLE_SCHEMA_VERSION,
                            revision_id,
                            capture.source_key,
                            capture.dataset,
                            content_digest,
                            len(content),
                            content,
                            retained_ref,
                            capture.source_locator,
                            capture.media_type,
                            previous_revision_id,
                            capture.capture_actor_id,
                            capture.capture_run_id,
                            capture_metadata_json,
                            captured_at,
                            accepted_at,
                            int(tombstone),
                        ),
                    )
                    self._inject_fault("after_source_revision")
                if previous_revision_id is not None and previous_revision_id != revision_id:
                    connection.execute(
                        """
                        UPDATE projection_receipts
                        SET state = 'stale', updated_at = ?
                        WHERE source_revision_id = ? AND state != 'stale'
                        """,
                        (accepted_at, previous_revision_id),
                    )
                    connection.execute(
                        """
                        UPDATE projection_jobs
                        SET state = 'stale', lease_id = NULL, lease_owner = NULL,
                            leased_until = NULL, updated_at = ?
                        WHERE source_revision_id = ? AND state != 'stale'
                        """,
                        (accepted_at, previous_revision_id),
                    )
                connection.execute(
                    """
                    INSERT INTO source_heads (dataset, source_key, source_revision_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(dataset, source_key) DO UPDATE SET
                        source_revision_id = excluded.source_revision_id,
                        updated_at = excluded.updated_at
                    """,
                    (capture.dataset, capture.source_key, revision_id, accepted_at),
                )
                self._inject_fault("after_source_head")
                if existing is not None:
                    connection.execute(
                        """
                        UPDATE projection_jobs
                        SET state = 'pending', attempt = 0, lease_id = NULL,
                            lease_owner = NULL, leased_until = NULL,
                            available_at = ?, updated_at = ?, last_error_code = NULL,
                            last_error_message = NULL
                        WHERE projection_job_id = ?
                        """,
                        (accepted_at, accepted_at, job_id),
                    )
                    connection.execute(
                        """
                        UPDATE projection_receipts
                        SET state = 'pending', attempt = 0,
                            provider_operation_id = NULL, affected_ids_json = '[]',
                            affected_count = NULL, model = NULL, dimensions = NULL,
                            metadata_json = '{}', updated_at = ?, completed_at = NULL,
                            searchable_at = NULL, error_code = NULL, error_message = NULL
                        WHERE projection_job_id = ?
                        """,
                        (accepted_at, job_id),
                    )
                    connection.execute("COMMIT")
                    return self._acceptance(job_id)
                connection.execute(
                    """
                    INSERT INTO projection_jobs (
                        schema_version, projection_job_id, source_revision_id,
                        generation_id, dataset, projection_version, config_digest,
                        required_backends_json, idempotency_key, state, attempt,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (
                        LIFECYCLE_SCHEMA_VERSION,
                        job_id,
                        revision_id,
                        projection.generation_id,
                        capture.dataset,
                        projection.projection_version,
                        projection.config_digest,
                        json.dumps(list(providers), separators=(",", ":")),
                        idempotency_key,
                        accepted_at,
                        accepted_at,
                        accepted_at,
                    ),
                )
                self._inject_fault("after_projection_job")
                for backend, provider in providers.items():
                    connection.execute(
                        """
                        INSERT INTO projection_receipts (
                            schema_version, projection_receipt_id, projection_job_id,
                            source_revision_id, generation_id, dataset, backend, provider,
                            projection_version, state, attempt, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                        """,
                        (
                            LIFECYCLE_SCHEMA_VERSION,
                            _projection_receipt_id(job_id, backend),
                            job_id,
                            revision_id,
                            projection.generation_id,
                            capture.dataset,
                            backend,
                            provider,
                            projection.projection_version,
                            accepted_at,
                            accepted_at,
                        ),
                    )
                    self._inject_fault(f"after_projection_receipt:{backend}")
                self._inject_fault("before_accept_commit")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self._acceptance(job_id)

    def accept_tombstone(
        self,
        *,
        reason: str,
        capture: CaptureContext,
        projection: ProjectionRequest,
        now: datetime | None = None,
    ) -> SourceAcceptance:
        """Write an immutable current-head tombstone without editing history."""
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("tombstone reason must be a non-empty string")
        retained_evidence = json.dumps(
            {
                "reason": normalized_reason,
                "schema_version": LIFECYCLE_SCHEMA_VERSION,
                "tombstone": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        tombstone_capture = CaptureContext(
            dataset=capture.dataset,
            source_key=capture.source_key,
            source_locator=capture.source_locator,
            media_type="application/vnd.citadel.tombstone+json",
            capture_actor_id=capture.capture_actor_id,
            capture_run_id=capture.capture_run_id,
            captured_at=capture.captured_at,
            metadata={
                **dict(capture.metadata),
                "tombstone_reason_sha256": sha256(
                    normalized_reason.encode("utf-8")
                ).hexdigest(),
            },
        )
        return self.accept_source(
            retained_evidence,
            capture=tombstone_capture,
            projection=projection,
            now=now,
            tombstone=True,
        )

    @staticmethod
    def _validate_capture(capture: CaptureContext) -> None:
        required = {
            "dataset": capture.dataset,
            "source_key": capture.source_key,
            "media_type": capture.media_type,
            "capture_actor_id": capture.capture_actor_id,
        }
        for field_name, value in required.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")

    @staticmethod
    def _validate_projection(projection: ProjectionRequest) -> dict[str, str]:
        for field_name, value in {
            "generation_id": projection.generation_id,
            "projection_version": projection.projection_version,
            "config_digest": projection.config_digest,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if set(projection.providers) != set(REQUIRED_BACKENDS):
            raise ValueError(
                "lifecycle v1 providers must define relational, vector, and graph"
            )
        providers: dict[str, str] = {}
        for backend in REQUIRED_BACKENDS:
            provider = projection.providers[backend]
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError(f"provider for {backend} must be a non-empty string")
            providers[backend] = provider.strip()
        return providers

    @staticmethod
    def _assert_generation_binding(
        connection: sqlite3.Connection,
        projection: ProjectionRequest,
        providers: Mapping[str, str],
    ) -> None:
        job_bindings = connection.execute(
            """
            SELECT DISTINCT projection_version, config_digest, required_backends_json
            FROM projection_jobs
            WHERE generation_id = ?
            """,
            (projection.generation_id,),
        ).fetchall()
        for binding in job_bindings:
            if (
                str(binding["projection_version"]) != projection.projection_version
                or str(binding["config_digest"]) != projection.config_digest
                or tuple(json.loads(binding["required_backends_json"]))
                != tuple(providers)
            ):
                raise LifecycleConflictError(
                    "projection configuration changed within an existing generation; "
                    "set a new CITADEL_GENERATION_ID"
                )
        stored_providers = {
            (str(row["backend"]), str(row["provider"]))
            for row in connection.execute(
                """
                SELECT DISTINCT receipt.backend, receipt.provider
                FROM projection_receipts AS receipt
                JOIN projection_jobs AS job
                  ON job.projection_job_id = receipt.projection_job_id
                WHERE job.generation_id = ?
                """,
                (projection.generation_id,),
            ).fetchall()
        }
        if stored_providers and stored_providers != set(providers.items()):
            raise LifecycleConflictError(
                "projection providers changed within an existing generation; "
                "set a new CITADEL_GENERATION_ID"
            )

    def assert_generation_binding(self, projection: ProjectionRequest) -> None:
        """Reject projection drift inside one physical provider generation."""
        providers = self._validate_projection(projection)
        with self._connect() as connection:
            self._assert_generation_binding(connection, projection, providers)

    @staticmethod
    def _assert_idempotent_job(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        projection: ProjectionRequest,
        providers: Mapping[str, str],
    ) -> None:
        if row["config_digest"] != projection.config_digest:
            raise LifecycleConflictError(
                "projection config changed for an existing deterministic job"
            )
        expected = tuple(json.loads(row["required_backends_json"]))
        if expected != tuple(providers):
            raise LifecycleConflictError(
                "required backends changed for an existing deterministic job"
            )
        stored_providers = {
            str(receipt["backend"]): str(receipt["provider"])
            for receipt in connection.execute(
                """
                SELECT backend, provider
                FROM projection_receipts
                WHERE projection_job_id = ?
                """,
                (row["projection_job_id"],),
            ).fetchall()
        }
        if stored_providers != dict(providers):
            raise LifecycleConflictError(
                "provider identity changed for an existing deterministic job"
            )

    def _acceptance(self, job_id: str) -> SourceAcceptance:
        operation = self.get_operation(job_id)
        return SourceAcceptance(
            accepted=True,
            source_revision_id=operation.source_revision.source_revision_id,
            projection_job_id=operation.job.projection_job_id,
            operation=operation,
        )

    def queue_generation_rebuild(
        self,
        projection: ProjectionRequest,
        *,
        now: datetime | None = None,
    ) -> tuple[ProjectionOperation, ...]:
        """Queue one idempotent projection job for every current source head."""
        providers = self._validate_projection(projection)
        queued_at = _utc_text(now or datetime.now(UTC))
        job_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_generation_binding(
                    connection,
                    projection,
                    providers,
                )
                current_sources = connection.execute(
                    """
                    SELECT revision.source_revision_id, revision.dataset
                    FROM source_heads AS head
                    JOIN source_revisions AS revision
                      ON revision.source_revision_id = head.source_revision_id
                    ORDER BY revision.dataset, revision.source_key,
                             revision.source_revision_id
                    """
                ).fetchall()
                for source in current_sources:
                    source_revision_id = str(source["source_revision_id"])
                    dataset = str(source["dataset"])
                    job_id = _projection_job_id(
                        source_revision_id,
                        projection.generation_id,
                        projection.projection_version,
                    )
                    job_ids.append(job_id)
                    existing = connection.execute(
                        """
                        SELECT projection_job_id, config_digest,
                               required_backends_json
                        FROM projection_jobs
                        WHERE projection_job_id = ?
                        """,
                        (job_id,),
                    ).fetchone()
                    if existing is not None:
                        self._assert_idempotent_job(
                            connection,
                            existing,
                            projection,
                            providers,
                        )
                        continue
                    idempotency_key = _stable_id(
                        "projection",
                        source_revision_id,
                        projection.generation_id,
                        projection.projection_version,
                    )
                    connection.execute(
                        """
                        INSERT INTO projection_jobs (
                            schema_version, projection_job_id, source_revision_id,
                            generation_id, dataset, projection_version, config_digest,
                            required_backends_json, idempotency_key, state, attempt,
                            available_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                        """,
                        (
                            LIFECYCLE_SCHEMA_VERSION,
                            job_id,
                            source_revision_id,
                            projection.generation_id,
                            dataset,
                            projection.projection_version,
                            projection.config_digest,
                            json.dumps(list(providers), separators=(",", ":")),
                            idempotency_key,
                            queued_at,
                            queued_at,
                            queued_at,
                        ),
                    )
                    self._inject_fault("after_rebuild_projection_job")
                    for backend, provider in providers.items():
                        connection.execute(
                            """
                            INSERT INTO projection_receipts (
                                schema_version, projection_receipt_id,
                                projection_job_id, source_revision_id, generation_id,
                                dataset, backend, provider, projection_version, state,
                                attempt, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                            """,
                            (
                                LIFECYCLE_SCHEMA_VERSION,
                                _projection_receipt_id(job_id, backend),
                                job_id,
                                source_revision_id,
                                projection.generation_id,
                                dataset,
                                backend,
                                provider,
                                projection.projection_version,
                                queued_at,
                                queued_at,
                            ),
                        )
                        self._inject_fault(f"after_rebuild_projection_receipt:{backend}")
                self._inject_fault("before_rebuild_commit")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return tuple(self.get_operation(job_id) for job_id in job_ids)

    @staticmethod
    def _failed_projection_candidates(
        connection: sqlite3.Connection,
        projection: ProjectionRequest,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT DISTINCT job.projection_job_id
            FROM source_heads AS head
            JOIN projection_jobs AS job
              ON job.source_revision_id = head.source_revision_id
            WHERE job.state = 'failed'
              AND job.generation_id = ?
              AND job.projection_version = ?
              AND job.config_digest = ?
            ORDER BY job.projection_job_id
            """,
            (
                projection.generation_id,
                projection.projection_version,
                projection.config_digest,
            ),
        ).fetchall()
        return tuple(str(row["projection_job_id"]) for row in rows)

    def failed_projection_candidates(
        self,
        projection: ProjectionRequest,
    ) -> tuple[str, ...]:
        """Preview failed current-head jobs for one projection identity."""
        with self._connect() as connection:
            return self._failed_projection_candidates(connection, projection)

    def failed_projection_records(
        self,
        projection: ProjectionRequest,
        *,
        error_code: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Failed current-head jobs, optionally filtered by last_error_code."""
        wanted = None if error_code is None else str(error_code).strip()
        if error_code is not None and not wanted:
            raise ValueError("error_code must be a non-empty string when provided")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job.projection_job_id, job.dataset, revision.source_key,
                       job.last_error_code, job.last_error_message
                FROM source_heads AS head
                JOIN projection_jobs AS job
                  ON job.source_revision_id = head.source_revision_id
                JOIN source_revisions AS revision
                  ON revision.source_revision_id = head.source_revision_id
                WHERE job.state = 'failed'
                  AND job.generation_id = ?
                  AND job.projection_version = ?
                  AND job.config_digest = ?
                  AND (? IS NULL OR job.last_error_code = ?)
                ORDER BY job.projection_job_id
                """,
                (
                    projection.generation_id,
                    projection.projection_version,
                    projection.config_digest,
                    wanted,
                    wanted,
                ),
            ).fetchall()
        return tuple(
            {
                "projection_job_id": str(row["projection_job_id"]),
                "dataset": str(row["dataset"]),
                "source_key": str(row["source_key"]),
                "error_code": row["last_error_code"],
                "last_error_code": row["last_error_code"],
                "error_message": row["last_error_message"],
                "last_error_message": row["last_error_message"],
            }
            for row in rows
        )

    def failed_missing_path_candidates(
        self,
        projection: ProjectionRequest,
    ) -> tuple[dict[str, Any], ...]:
        """Failed current-head jobs whose last error is FileNotFoundError."""
        return self.failed_projection_records(
            projection,
            error_code="FileNotFoundError",
        )

    def requeue_failed_projections(
        self,
        projection: ProjectionRequest,
        *,
        expected_count: int,
        candidate_ids: tuple[str, ...],
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Reset one exact preview of active failed jobs to pending."""
        if expected_count < 0:
            raise ValueError("expected_count must not be negative")
        if any(not candidate_id.strip() for candidate_id in candidate_ids):
            raise ValueError("candidate_ids must contain non-empty strings")
        if candidate_ids != tuple(sorted(set(candidate_ids))):
            raise ValueError("candidate_ids must be sorted and unique")
        changed_text = _utc_text(now or datetime.now(UTC))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                failed_jobs = self._failed_projection_candidates(connection, projection)
                if expected_count != len(failed_jobs) or candidate_ids != failed_jobs:
                    raise LifecycleRequeueDriftError(
                        expected_count=expected_count,
                        actual_candidate_ids=failed_jobs,
                    )
                for job_id in failed_jobs:
                    connection.execute(
                        """
                        UPDATE projection_jobs
                        SET state = 'pending', attempt = 0, lease_id = NULL,
                            lease_owner = NULL, leased_until = NULL,
                            available_at = ?, updated_at = ?, last_error_code = NULL,
                            last_error_message = NULL
                        WHERE projection_job_id = ?
                        """,
                        (changed_text, changed_text, job_id),
                    )
                    self._inject_fault("after_requeue_projection_job")
                    connection.execute(
                        """
                        UPDATE projection_receipts
                        SET state = 'pending', attempt = 0,
                            provider_operation_id = NULL, affected_ids_json = '[]',
                            affected_count = NULL, model = NULL, dimensions = NULL,
                            metadata_json = '{}', updated_at = ?, completed_at = NULL,
                            searchable_at = NULL, error_code = NULL, error_message = NULL
                        WHERE projection_job_id = ?
                        """,
                        (changed_text, job_id),
                    )
                    self._inject_fault("after_requeue_projection_receipts")
                self._inject_fault("before_requeue_commit")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return failed_jobs

    def read_retained_content(self, source_revision_id: str) -> bytes:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT retained_content FROM source_revisions WHERE source_revision_id = ?",
                (source_revision_id,),
            ).fetchone()
        if row is None:
            raise LifecycleNotFoundError(f"source revision not found: {source_revision_id}")
        return bytes(row[0])

    def get_source_revision(self, source_revision_id: str) -> SourceRevision | None:
        """Return retained-source metadata without exposing its content."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_revisions WHERE source_revision_id = ?",
                (source_revision_id,),
            ).fetchone()
        return None if row is None else self._source_revision(row)

    def has_completed_projection_other_than(
        self,
        *,
        source_revision_id: str,
        projection_job_id: str,
    ) -> bool:
        """Return whether this source already completed under another projection."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM projection_jobs
                WHERE source_revision_id = ?
                  AND projection_job_id != ?
                  AND state = 'completed'
                LIMIT 1
                """,
                (source_revision_id, projection_job_id),
            ).fetchone()
        return row is not None

    def get_current_revision(self, dataset: str, source_key: str) -> SourceRevision:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT revision.*
                FROM source_heads AS head
                JOIN source_revisions AS revision
                  ON revision.source_revision_id = head.source_revision_id
                WHERE head.dataset = ? AND head.source_key = ?
                """,
                (dataset, source_key),
            ).fetchone()
        if row is None:
            raise LifecycleNotFoundError(
                f"current source revision not found: dataset={dataset!r} source_key={source_key!r}"
            )
        return self._source_revision(row)

    def current_head_evidence(
        self,
        dataset: str,
        source_keys: tuple[str, ...] | list[str],
        *,
        generation_id: str,
        projection_version: str,
        config_digest: str,
    ) -> CurrentHeadEvidenceResult:
        """Attest exact current source heads against one projection identity."""
        for field_name, value in {
            "dataset": dataset,
            "generation_id": generation_id,
            "projection_version": projection_version,
            "config_digest": config_digest,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(source_keys, (tuple, list)) or not source_keys:
            raise ValueError("source_keys must be a non-empty list or tuple")
        if any(not isinstance(source_key, str) or not source_key.strip() for source_key in source_keys):
            raise ValueError("source_keys must contain only non-empty strings")

        seen: set[str] = set()
        duplicate: str | None = None
        for source_key in source_keys:
            if source_key in seen:
                duplicate = source_key
                break
            seen.add(source_key)
        if duplicate is not None:
            return CurrentHeadEvidenceResult(
                ok=False,
                dataset=dataset,
                generation_id=generation_id,
                projection_version=projection_version,
                config_digest=config_digest,
                evidence=(),
                errors=(
                    CurrentHeadEvidenceError(
                        code="SOURCE_KEY_DUPLICATE",
                        source_key=duplicate,
                    ),
                ),
            )

        evidence: list[CurrentHeadProjectionEvidence] = []
        errors: list[CurrentHeadEvidenceError] = []
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                for source_key in source_keys:
                    source_row = connection.execute(
                        """
                        SELECT revision.*
                        FROM source_heads AS head
                        JOIN source_revisions AS revision
                          ON revision.source_revision_id = head.source_revision_id
                         AND revision.dataset = head.dataset
                         AND revision.source_key = head.source_key
                        WHERE head.dataset = ? AND head.source_key = ?
                        """,
                        (dataset, source_key),
                    ).fetchone()
                    if source_row is None:
                        errors.append(
                            CurrentHeadEvidenceError(
                                code="CURRENT_HEAD_MISSING",
                                source_key=source_key,
                            )
                        )
                        continue

                    source = self._source_revision(source_row)
                    if source.tombstone:
                        errors.append(
                            CurrentHeadEvidenceError(
                                code="CURRENT_HEAD_TOMBSTONED",
                                source_key=source_key,
                                source_revision_id=source.source_revision_id,
                            )
                        )
                        continue

                    job_rows = connection.execute(
                        """
                        SELECT *
                        FROM projection_jobs
                        WHERE source_revision_id = ?
                        ORDER BY created_at, projection_job_id
                        """,
                        (source.source_revision_id,),
                    ).fetchall()
                    if not job_rows:
                        errors.append(
                            CurrentHeadEvidenceError(
                                code="CURRENT_JOB_MISSING",
                                source_key=source_key,
                                source_revision_id=source.source_revision_id,
                            )
                        )
                        continue

                    matching_job_rows = [
                        row
                        for row in job_rows
                        if str(row["dataset"]) == dataset
                        and str(row["generation_id"]) == generation_id
                        and str(row["projection_version"]) == projection_version
                        and str(row["config_digest"]) == config_digest
                    ]
                    if not matching_job_rows:
                        errors.append(
                            CurrentHeadEvidenceError(
                                code="CURRENT_JOB_MISMATCH",
                                source_key=source_key,
                                source_revision_id=source.source_revision_id,
                                projection_job_ids=tuple(
                                    str(row["projection_job_id"])
                                    for row in job_rows[:10]
                                ),
                            )
                        )
                        continue
                    if len(matching_job_rows) != 1:
                        errors.append(
                            CurrentHeadEvidenceError(
                                code="CURRENT_JOB_AMBIGUOUS",
                                source_key=source_key,
                                source_revision_id=source.source_revision_id,
                                projection_job_ids=tuple(
                                    str(row["projection_job_id"])
                                    for row in matching_job_rows[:10]
                                ),
                            )
                        )
                        continue

                    job = self._projection_job(matching_job_rows[0])
                    receipt_rows = connection.execute(
                        """
                        SELECT *
                        FROM projection_receipts
                        WHERE projection_job_id = ?
                        ORDER BY CASE backend
                            WHEN 'relational' THEN 1
                            WHEN 'vector' THEN 2
                            WHEN 'graph' THEN 3
                            ELSE 4
                        END, backend, projection_receipt_id
                        LIMIT 4
                        """,
                        (job.projection_job_id,),
                    ).fetchall()
                    receipts = tuple(
                        self._projection_receipt(row) for row in receipt_rows
                    )
                    backend_states = {
                        receipt.backend: receipt.state for receipt in receipts
                    }
                    receipt_set_matches = (
                        job.required_backends == REQUIRED_BACKENDS
                        and tuple(receipt.backend for receipt in receipts)
                        == REQUIRED_BACKENDS
                        and all(
                            receipt.source_revision_id == source.source_revision_id
                            and receipt.generation_id == generation_id
                            and receipt.dataset == dataset
                            and receipt.projection_version == projection_version
                            for receipt in receipts
                        )
                    )
                    if not receipt_set_matches:
                        errors.append(
                            CurrentHeadEvidenceError(
                                code="RECEIPT_SET_MISMATCH",
                                source_key=source_key,
                                source_revision_id=source.source_revision_id,
                                projection_job_ids=(job.projection_job_id,),
                                job_state=job.state,
                                backend_states=backend_states,
                            )
                        )
                        continue

                    state = self._operation_state(job, receipts)
                    if state != "searchable" or any(
                        receipt.state != "searchable" for receipt in receipts
                    ):
                        errors.append(
                            CurrentHeadEvidenceError(
                                code="RECEIPT_NOT_SEARCHABLE",
                                source_key=source_key,
                                source_revision_id=source.source_revision_id,
                                projection_job_ids=(job.projection_job_id,),
                                job_state=job.state,
                                backend_states=backend_states,
                            )
                        )
                        continue

                    evidence.append(
                        CurrentHeadProjectionEvidence(
                            source_key=source_key,
                            dataset=dataset,
                            source_revision_id=source.source_revision_id,
                            projection_job_id=job.projection_job_id,
                            generation_id=job.generation_id,
                            projection_version=job.projection_version,
                            config_digest=job.config_digest,
                            state=state,
                            receipts=tuple(
                                CurrentHeadReceiptEvidence(
                                    projection_receipt_id=receipt.projection_receipt_id,
                                    backend=receipt.backend,
                                    provider=receipt.provider,
                                    state=receipt.state,
                                )
                                for receipt in receipts
                            ),
                        )
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return CurrentHeadEvidenceResult(
            ok=not errors,
            dataset=dataset,
            generation_id=generation_id,
            projection_version=projection_version,
            config_digest=config_digest,
            evidence=tuple(evidence),
            errors=tuple(errors),
        )

    def current_revisions_for_source(
        self,
        dataset: str,
        source_key: str,
        *,
        include_chunks: bool = True,
    ) -> tuple[SourceRevision, ...]:
        """Return current exact and optional chunk revisions for one logical source."""
        with self._connect() as connection:
            if include_chunks:
                rows = connection.execute(
                    """
                    SELECT revision.*
                    FROM source_heads AS head
                    JOIN source_revisions AS revision
                      ON revision.source_revision_id = head.source_revision_id
                    WHERE head.dataset = ?
                      AND (
                          head.source_key = ?
                          OR json_extract(
                              revision.capture_metadata_json,
                              '$.lifecycle_parent_source_key'
                          ) = ?
                      )
                    ORDER BY head.source_key
                    """,
                    (dataset, source_key, source_key),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT revision.*
                    FROM source_heads AS head
                    JOIN source_revisions AS revision
                      ON revision.source_revision_id = head.source_revision_id
                    WHERE head.dataset = ? AND head.source_key = ?
                    """,
                    (dataset, source_key),
                ).fetchall()
        return tuple(self._source_revision(row) for row in rows)

    def get_operation(self, projection_job_id: str) -> ProjectionOperation:
        with self._connect() as connection:
            job_row = connection.execute(
                "SELECT * FROM projection_jobs WHERE projection_job_id = ?",
                (projection_job_id,),
            ).fetchone()
            if job_row is None:
                raise LifecycleNotFoundError(
                    f"projection job not found: {projection_job_id}"
                )
            source_row = connection.execute(
                "SELECT * FROM source_revisions WHERE source_revision_id = ?",
                (job_row["source_revision_id"],),
            ).fetchone()
            receipt_rows = connection.execute(
                """
                SELECT * FROM projection_receipts
                WHERE projection_job_id = ?
                ORDER BY CASE backend
                    WHEN 'relational' THEN 1
                    WHEN 'vector' THEN 2
                    WHEN 'graph' THEN 3
                    ELSE 4
                END, backend
                """,
                (projection_job_id,),
            ).fetchall()
        if source_row is None:
            raise LifecycleSchemaError(
                f"projection job has no source revision: {projection_job_id}"
            )
        job = self._projection_job(job_row)
        receipts = tuple(self._projection_receipt(row) for row in receipt_rows)
        return ProjectionOperation(
            source_revision=self._source_revision(source_row),
            job=job,
            receipts=receipts,
            state=self._operation_state(job, receipts),
        )

    def latest_operations_for_dataset(
        self, dataset: str, *, limit: int = 10
    ) -> tuple[ProjectionOperation, ...]:
        """Return the newest projection operations for one dataset."""
        if not dataset.strip():
            raise ValueError("dataset must be a non-empty string")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT projection_job_id
                FROM projection_jobs
                WHERE dataset = ?
                ORDER BY created_at DESC, projection_job_id DESC
                LIMIT ?
                """,
                (dataset, limit),
            ).fetchall()
        return tuple(self.get_operation(str(row["projection_job_id"])) for row in rows)

    def retrieval_binding(
        self,
        source_revision_id: str,
        *,
        generation_id: str,
        projection_version: str,
        config_digest: str,
        backend: str = "vector",
    ) -> RetrievalBinding | None:
        """Resolve one provider candidate against the current source head."""
        with self._connect() as connection:
            source_row = connection.execute(
                "SELECT * FROM source_revisions WHERE source_revision_id = ?",
                (source_revision_id,),
            ).fetchone()
            if source_row is None:
                return None
            current = bool(
                connection.execute(
                    """
                    SELECT 1
                    FROM source_heads
                    WHERE source_revision_id = ?
                    LIMIT 1
                    """,
                    (source_revision_id,),
                ).fetchone()
            )
            receipt_row = None
            if current and not bool(source_row["tombstone"]):
                receipt_row = connection.execute(
                    """
                    SELECT receipt.*
                    FROM projection_receipts AS receipt
                    JOIN projection_jobs AS job
                      ON job.projection_job_id = receipt.projection_job_id
                    WHERE receipt.source_revision_id = ?
                      AND receipt.generation_id = ?
                      AND receipt.projection_version = ?
                      AND job.config_digest = ?
                      AND receipt.backend = ?
                      AND receipt.state = 'searchable'
                      AND job.state IN ('pending', 'running', 'completed', 'failed', 'deferred')
                    LIMIT 1
                    """,
                    (
                        source_revision_id,
                        generation_id,
                        projection_version,
                        config_digest,
                        backend,
                    ),
                ).fetchone()
        return RetrievalBinding(
            source_revision=self._source_revision(source_row),
            current=current,
            receipt=(
                self._projection_receipt(receipt_row)
                if receipt_row is not None
                else None
            ),
        )

    def searchable_source_revision_ids(
        self,
        *,
        dataset: str,
        generation_id: str,
        projection_version: str,
        config_digest: str,
        backend: str = "vector",
    ) -> tuple[str, ...]:
        """Enumerate current provider ids eligible for one scoped retrieval."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT revision.source_revision_id
                FROM source_heads AS head
                JOIN source_revisions AS revision
                  ON revision.source_revision_id = head.source_revision_id
                JOIN projection_jobs AS job
                  ON job.source_revision_id = revision.source_revision_id
                JOIN projection_receipts AS receipt
                  ON receipt.projection_job_id = job.projection_job_id
                WHERE head.dataset = ?
                  AND revision.tombstone = 0
                  AND job.generation_id = ?
                  AND job.projection_version = ?
                  AND job.config_digest = ?
                  AND job.state IN ('pending', 'running', 'completed', 'failed', 'deferred')
                  AND receipt.backend = ?
                  AND receipt.state = 'searchable'
                ORDER BY revision.source_revision_id
                """,
                (
                    dataset,
                    generation_id,
                    projection_version,
                    config_digest,
                    backend,
                ),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def lexical_search(
        self,
        query: str,
        *,
        dataset: str,
        projection: ProjectionRequest,
        top_k: int = 10,
        required_linear_issue_identifier: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search retained current heads without a vector or language model."""
        if not query.strip():
            return []
        if not dataset.strip():
            raise ValueError("dataset must be a non-empty string")
        if top_k < 1:
            raise ValueError("top_k must be positive")

        query_terms = tuple(_LEXICAL_TOKEN_RE.findall(query.casefold()))
        if not query_terms:
            return []
        query_set = set(query_terms)
        normalized_query = " ".join(query.casefold().split())

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision.*, job.projection_job_id,
                       job.state AS projection_state,
                       GROUP_CONCAT(
                           receipt.backend || '=' || receipt.state, '|'
                       ) AS receipt_states
                FROM source_heads AS head
                JOIN source_revisions AS revision
                  ON revision.source_revision_id = head.source_revision_id
                LEFT JOIN projection_jobs AS job
                  ON job.source_revision_id = revision.source_revision_id
                 AND job.generation_id = ?
                 AND job.projection_version = ?
                 AND job.config_digest = ?
                LEFT JOIN projection_receipts AS receipt
                  ON receipt.projection_job_id = job.projection_job_id
                WHERE head.dataset = ?
                  AND revision.tombstone = 0
                GROUP BY revision.source_revision_id
                ORDER BY revision.source_key, revision.source_revision_id
                """,
                (
                    projection.generation_id,
                    projection.projection_version,
                    projection.config_digest,
                    dataset,
                ),
            ).fetchall()

        ranked: list[tuple[float, str, int, dict[str, Any]]] = []
        for row in rows:
            try:
                text = bytes(row["retained_content"]).decode("utf-8")
            except UnicodeDecodeError:
                continue
            revision = self._source_revision(row)
            capture_metadata = revision.capture_metadata
            if required_linear_issue_identifier is not None:
                tags = capture_metadata.get("tags")
                required_tag = f"linear:{required_linear_issue_identifier}".casefold()
                tag_matches = isinstance(tags, list) and required_tag in {
                    str(tag).casefold() for tag in tags
                }
                locator_matches = (
                    linear_issue_url_identifier(revision.source_locator)
                    == required_linear_issue_identifier
                )
                if not tag_matches and not locator_matches:
                    continue
            title = capture_metadata.get("title")
            if not isinstance(title, str) or not title.strip():
                header = parse_content_header(text, chunk_index=0)
                if header.get("kind") == "linear-issue" and header.get("issue"):
                    issue_title = header.get("title")
                    title = (
                        f"{header['issue']}: {issue_title}"
                        if issue_title
                        else header["issue"]
                    )
                elif header.get("kind") == "repo-content" and header.get("path"):
                    title = Path(header["path"]).name
                else:
                    title = header.get("title") or revision.source_key
            projection_state = str(row["projection_state"] or "not_started")
            receipt_states: dict[str, str] = {}
            for item in str(row["receipt_states"] or "").split("|"):
                if "=" not in item:
                    continue
                backend, state = item.split("=", 1)
                if backend and state:
                    receipt_states[backend] = state

            for passage_index, passage in enumerate(_lexical_passages(text)):
                passage_terms = _LEXICAL_TOKEN_RE.findall(passage.casefold())
                passage_counts = Counter(passage_terms)
                matched_terms = query_set.intersection(passage_counts)
                if not matched_terms:
                    continue
                score = len(matched_terms) / len(query_set)
                score += min(sum(passage_counts[item] for item in matched_terms), 5) / 100
                if normalized_query in " ".join(passage.casefold().split()):
                    score += 0.25

                result_metadata: dict[str, Any] = {
                    "source_revision_id": revision.source_revision_id,
                    "source_key": revision.source_key,
                    "source_locator": revision.source_locator,
                    "media_type": revision.media_type,
                    "content_sha256": revision.content_sha256,
                    "captured_at": revision.captured_at,
                    "accepted_at": revision.accepted_at,
                    "retrieval_mode": "lexical_fallback",
                    "projection_state": projection_state,
                    "vector_state": receipt_states.get("vector", "not_started"),
                    "graph_state": receipt_states.get("graph", "not_started"),
                }
                for key in ("tags", "session_id", "lifecycle_parent_source_key", "lifecycle_chunk_index"):
                    if key in capture_metadata:
                        result_metadata[key] = capture_metadata[key]
                reference: dict[str, str] = {
                    "document_id": revision.source_revision_id,
                    "title": str(title),
                    "snippet": passage[:500],
                }
                if revision.source_locator:
                    reference["source_locator"] = revision.source_locator
                ranked.append(
                    (
                        score,
                        revision.source_key,
                        passage_index,
                        {
                            "id": _stable_id(
                                "lexical-hit",
                                revision.source_revision_id,
                                str(passage_index),
                            ),
                            "document_id": revision.source_revision_id,
                            "source": "lifecycle",
                            "source_type": "lifecycle",
                            "dataset": dataset,
                            "title": title,
                            "text": passage,
                            "score": round(score, 6),
                            "metadata": result_metadata,
                            "references": [reference],
                            "_lifecycle": {
                                "schema_version": LIFECYCLE_SCHEMA_VERSION,
                                "source_revision_id": revision.source_revision_id,
                                "generation_id": projection.generation_id,
                                "config_digest": projection.config_digest,
                                "backend": "lexical",
                                "provider": "sqlite",
                                "projection_version": projection.projection_version,
                                "state": "searchable",
                                "retrieval_mode": "lexical_fallback",
                                "projection_state": projection_state,
                                "vector_state": receipt_states.get("vector", "not_started"),
                                "graph_state": receipt_states.get("graph", "not_started"),
                            },
                        },
                    )
                )

        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [item[3] for item in ranked[:top_k]]

    def projection_source_revision_states(
        self,
        source_revision_ids: list[str],
        *,
        generation_id: str,
        projection_version: str,
        config_digest: str,
    ) -> tuple[set[str], set[str]]:
        """Partition revisions into active and completed-searchable projections."""
        candidates = sorted({str(value) for value in source_revision_ids if value})
        if not candidates:
            return set(), set()
        placeholders = ",".join("?" for _ in candidates)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job.source_revision_id, job.state, "
                "MAX(CASE WHEN receipt.backend = 'vector' "
                "AND receipt.state = 'searchable' THEN 1 ELSE 0 END) "
                "AS vector_searchable "
                "FROM projection_jobs AS job "
                "LEFT JOIN projection_receipts AS receipt "
                "ON receipt.projection_job_id = job.projection_job_id "
                "WHERE job.generation_id = ? "
                "AND job.projection_version = ? "
                "AND job.config_digest = ? "
                f"AND job.source_revision_id IN ({placeholders}) "
                "GROUP BY job.source_revision_id, job.state",
                (generation_id, projection_version, config_digest, *candidates),
            ).fetchall()
        active: set[str] = set()
        completed_searchable: set[str] = set()
        for row in rows:
            source_revision_id = str(row["source_revision_id"])
            if row["state"] in {"pending", "running"}:
                active.add(source_revision_id)
            if row["state"] in {"pending", "running", "completed", "failed", "deferred"} and bool(
                row["vector_searchable"]
            ):
                completed_searchable.add(source_revision_id)
        return active, completed_searchable

    def claim_next_job(
        self,
        *,
        worker_id: str,
        generation_id: str | None = None,
        projection_version: str | None = None,
        config_digest: str | None = None,
        dataset: str | None = None,
        now: datetime | None = None,
        lease_seconds: float = 120,
        include_deferred: bool = False,
        deferred_only: bool = False,
    ) -> ProjectionLease | None:
        """Claim one due job and recover an expired lease in the same transaction."""
        if not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        for value, label in (
            (generation_id, "generation_id"),
            (projection_version, "projection_version"),
            (config_digest, "config_digest"),
            (dataset, "dataset"),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{label} must be non-empty when provided")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claimed_at = now or datetime.now(UTC)
        claimed_text = _utc_text(claimed_at)
        leased_until = _utc_text(claimed_at + timedelta(seconds=lease_seconds))
        lease_id = str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT *
                    FROM projection_jobs
                    WHERE (? IS NULL OR generation_id = ?)
                      AND (? IS NULL OR projection_version = ?)
                      AND (? IS NULL OR config_digest = ?)
                      AND (? IS NULL OR dataset = ?)
                      AND (? = 0 OR state = 'deferred')
                      AND ((
                          state = 'pending' AND available_at <= ?
                      ) OR (
                          state = 'running' AND leased_until IS NOT NULL AND leased_until <= ?
                      ) OR (
                          ? = 1 AND state = 'deferred' AND available_at <= ?
                      ))
                    -- Seat projections must stay responsive while a generation
                    -- rebuild drains Central and sync datasets. Keep the
                    -- durable order inside each class so older work still
                    -- completes after seat work.
                    ORDER BY CASE
                                 WHEN ? = 1 THEN 0
                                 WHEN dataset LIKE 'seat:%' THEN 0
                                 ELSE 1
                             END,
                             CASE WHEN ? = 1 THEN available_at END ASC,
                             CASE
                                 WHEN ? = 0 AND dataset LIKE 'seat:%' THEN created_at
                             END DESC,
                             available_at, created_at, projection_job_id
                    LIMIT 1
                    """,
                    (
                        generation_id,
                        generation_id,
                        projection_version,
                        projection_version,
                        config_digest,
                        config_digest,
                        dataset,
                        dataset,
                        int(deferred_only),
                        claimed_text,
                        claimed_text,
                        int(include_deferred),
                        claimed_text,
                        int(deferred_only),
                        int(deferred_only),
                        int(deferred_only),
                    ),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                job_id = str(row["projection_job_id"])
                if row["state"] == "running":
                    connection.execute(
                        """
                        UPDATE projection_receipts
                        SET state = 'pending', updated_at = ?
                        WHERE projection_job_id = ? AND state = 'running'
                        """,
                        (claimed_text, job_id),
                    )
                attempt = int(row["attempt"]) + 1
                connection.execute(
                    """
                    UPDATE projection_jobs
                    SET state = 'running', attempt = ?, lease_id = ?, lease_owner = ?,
                        leased_until = ?, updated_at = ?, last_error_code = NULL,
                        last_error_message = NULL
                    WHERE projection_job_id = ?
                    """,
                    (
                        attempt,
                        lease_id,
                        worker_id,
                        leased_until,
                        claimed_text,
                        job_id,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return ProjectionLease(
            projection_job_id=job_id,
            lease_id=lease_id,
            worker_id=worker_id,
            leased_until=leased_until,
            attempt=attempt,
        )

    def renew_lease(
        self,
        lease: ProjectionLease,
        *,
        now: datetime | None = None,
        lease_seconds: float = 120,
    ) -> None:
        """Extend one active lease while a provider operation is still running."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        renewed_at = now or datetime.now(UTC)
        renewed_text = _utc_text(renewed_at)
        leased_until = _utc_text(renewed_at + timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_lease(connection, lease, renewed_text)
                connection.execute(
                    """
                    UPDATE projection_jobs
                    SET leased_until = ?, updated_at = ?
                    WHERE projection_job_id = ?
                    """,
                    (leased_until, renewed_text, lease.projection_job_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def begin_backend(
        self,
        lease: ProjectionLease,
        backend: str,
        *,
        now: datetime | None = None,
    ) -> ProjectionReceipt:
        """Mark one backend attempt running under the active job lease."""
        changed_at = _utc_text(now or datetime.now(UTC))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_lease(connection, lease, changed_at)
                row = self._receipt_row(connection, lease.projection_job_id, backend)
                state = str(row["state"])
                if state in {"stale", "searchable"}:
                    connection.execute("COMMIT")
                    return self._projection_receipt(row)
                if state == "completed":
                    connection.execute("COMMIT")
                    return self._projection_receipt(row)
                attempt = int(row["attempt"]) + 1
                connection.execute(
                    """
                    UPDATE projection_receipts
                    SET state = 'running', attempt = ?, updated_at = ?,
                        error_code = NULL, error_message = NULL
                    WHERE projection_receipt_id = ?
                    """,
                    (attempt, changed_at, row["projection_receipt_id"]),
                )
                updated = self._receipt_row(connection, lease.projection_job_id, backend)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self._projection_receipt(updated)

    def complete_backend(
        self,
        lease: ProjectionLease,
        backend: str,
        *,
        provider_operation_id: str | None = None,
        affected_ids: tuple[str, ...] = (),
        affected_count: int | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ProjectionReceipt:
        """Record that one provider call returned without claiming searchability."""
        changed_at = _utc_text(now or datetime.now(UTC))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_lease(connection, lease, changed_at)
                row = self._receipt_row(connection, lease.projection_job_id, backend)
                if row["state"] not in {"running", "completed"}:
                    raise LifecycleConflictError(
                        f"backend {backend!r} cannot complete from state {row['state']!r}"
                    )
                connection.execute(
                    """
                    UPDATE projection_receipts
                    SET state = 'completed', provider_operation_id = ?,
                        affected_ids_json = ?, affected_count = ?, model = ?, dimensions = ?,
                        metadata_json = ?, updated_at = ?, completed_at = ?,
                        error_code = NULL, error_message = NULL
                    WHERE projection_receipt_id = ?
                    """,
                    (
                        provider_operation_id,
                        json.dumps(list(affected_ids), separators=(",", ":")),
                        affected_count,
                        model,
                        dimensions,
                        json.dumps(dict(metadata or {}), sort_keys=True, separators=(",", ":")),
                        changed_at,
                        changed_at,
                        row["projection_receipt_id"],
                    ),
                )
                updated = self._receipt_row(connection, lease.projection_job_id, backend)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self._projection_receipt(updated)

    def mark_backend_searchable(
        self,
        lease: ProjectionLease,
        backend: str,
        *,
        now: datetime | None = None,
    ) -> ProjectionReceipt:
        """Attest a bounded read check and close the job after every backend passes."""
        changed_at = _utc_text(now or datetime.now(UTC))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_lease(connection, lease, changed_at)
                row = self._receipt_row(connection, lease.projection_job_id, backend)
                if row["state"] not in {"completed", "searchable"}:
                    raise LifecycleConflictError(
                        f"backend {backend!r} cannot become searchable from state "
                        f"{row['state']!r}"
                    )
                connection.execute(
                    """
                    UPDATE projection_receipts
                    SET state = 'searchable', updated_at = ?, searchable_at = ?
                    WHERE projection_receipt_id = ?
                    """,
                    (changed_at, changed_at, row["projection_receipt_id"]),
                )
                remaining = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM projection_receipts
                        WHERE projection_job_id = ? AND state != 'searchable'
                        """,
                        (lease.projection_job_id,),
                    ).fetchone()[0]
                )
                if remaining == 0:
                    connection.execute(
                        """
                        UPDATE projection_jobs
                        SET state = 'completed', lease_id = NULL, lease_owner = NULL,
                            leased_until = NULL, updated_at = ?
                        WHERE projection_job_id = ?
                        """,
                        (changed_at, lease.projection_job_id),
                    )
                updated = self._receipt_row(connection, lease.projection_job_id, backend)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self._projection_receipt(updated)

    def defer_graph_enrichment(
        self,
        lease: ProjectionLease,
        *,
        now: datetime | None = None,
        backoff_seconds: float = 0,
    ) -> ProjectionJob:
        """Release a vector-ready job until the scheduled graph lane runs.

        Vector search is useful before graph enrichment finishes. The durable
        deferred state keeps the baseline worker from claiming the same job and
        makes the remaining graph work visible to the scheduled lane.
        """
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        changed_at = now or datetime.now(UTC)
        changed_text = _utc_text(changed_at)
        available_at = _utc_text(changed_at + timedelta(seconds=backoff_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_lease(connection, lease, changed_text)
                rows = connection.execute(
                    """
                    SELECT backend, state
                    FROM projection_receipts
                    WHERE projection_job_id = ?
                    """,
                    (lease.projection_job_id,),
                ).fetchall()
                states = {str(row["backend"]): str(row["state"]) for row in rows}
                if states.get("relational") != "searchable":
                    raise LifecycleConflictError(
                        "graph enrichment cannot be deferred before relational searchability"
                    )
                if states.get("vector") != "searchable":
                    raise LifecycleConflictError(
                        "graph enrichment cannot be deferred before vector searchability"
                    )
                connection.execute(
                    """
                    UPDATE projection_jobs
                    SET state = 'deferred', lease_id = NULL, lease_owner = NULL,
                        leased_until = NULL, available_at = ?, updated_at = ?,
                        last_error_code = ?, last_error_message = ?
                    WHERE projection_job_id = ?
                    """,
                    (
                        available_at,
                        changed_text,
                        _DEFERRED_GRAPH_ERROR,
                        "vector and relational projection are searchable; graph enrichment is scheduled",
                        lease.projection_job_id,
                    ),
                )
                updated = self._projection_job(
                    connection.execute(
                        "SELECT * FROM projection_jobs WHERE projection_job_id = ?",
                        (lease.projection_job_id,),
                    ).fetchone()
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return updated

    def reschedule_job(
        self,
        lease: ProjectionLease,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
        backoff_seconds: float = 5,
        deferred: bool = False,
    ) -> ProjectionJob:
        """Release failed work for a bounded retry without losing receipt history."""
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        changed_at = now or datetime.now(UTC)
        changed_text = _utc_text(changed_at)
        available_at = _utc_text(changed_at + timedelta(seconds=backoff_seconds))
        bounded_code = str(error_code)[:100]
        bounded_message = str(error_message)[:1000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_lease(connection, lease, changed_text)
                connection.execute(
                    """
                    UPDATE projection_receipts
                    SET state = 'pending', updated_at = ?, error_code = ?, error_message = ?
                    WHERE projection_job_id = ? AND state = 'running'
                    """,
                    (
                        changed_text,
                        bounded_code,
                        bounded_message,
                        lease.projection_job_id,
                    ),
                )
                next_state = "deferred" if deferred else "pending"
                connection.execute(
                    """
                    UPDATE projection_jobs
                    SET state = ?, lease_id = NULL, lease_owner = NULL,
                        leased_until = NULL, available_at = ?, updated_at = ?,
                        last_error_code = ?, last_error_message = ?
                    WHERE projection_job_id = ?
                    """,
                    (
                        next_state,
                        available_at,
                        changed_text,
                        bounded_code,
                        bounded_message,
                        lease.projection_job_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM projection_jobs WHERE projection_job_id = ?",
                    (lease.projection_job_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            raise LifecycleSchemaError(
                f"rescheduled projection job disappeared: {lease.projection_job_id}"
            )
        return self._projection_job(updated)

    def fail_job(
        self,
        lease: ProjectionLease,
        *,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
    ) -> ProjectionJob:
        """Stop retrying poison work and retain its bounded terminal error."""
        changed_text = _utc_text(now or datetime.now(UTC))
        bounded_code = str(error_code)[:100]
        bounded_message = str(error_message)[:1000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_lease(connection, lease, changed_text)
                connection.execute(
                    """
                    UPDATE projection_receipts
                    SET state = 'failed', updated_at = ?, error_code = ?,
                        error_message = ?
                    WHERE projection_job_id = ?
                      AND state NOT IN ('searchable', 'stale')
                    """,
                    (
                        changed_text,
                        bounded_code,
                        bounded_message,
                        lease.projection_job_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE projection_jobs
                    SET state = 'failed', lease_id = NULL, lease_owner = NULL,
                        leased_until = NULL, updated_at = ?, last_error_code = ?,
                        last_error_message = ?
                    WHERE projection_job_id = ?
                    """,
                    (
                        changed_text,
                        bounded_code,
                        bounded_message,
                        lease.projection_job_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM projection_jobs WHERE projection_job_id = ?",
                    (lease.projection_job_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            raise LifecycleSchemaError(
                f"failed projection job disappeared: {lease.projection_job_id}"
            )
        return self._projection_job(updated)

    @staticmethod
    def _require_lease(
        connection: sqlite3.Connection,
        lease: ProjectionLease,
        now_text: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM projection_jobs WHERE projection_job_id = ?",
            (lease.projection_job_id,),
        ).fetchone()
        if row is None:
            raise LifecycleNotFoundError(
                f"projection job not found: {lease.projection_job_id}"
            )
        if (
            row["state"] != "running"
            or row["lease_id"] != lease.lease_id
            or row["lease_owner"] != lease.worker_id
            or row["leased_until"] is None
            or str(row["leased_until"]) <= now_text
        ):
            raise ProjectionLeaseError(
                f"projection lease is no longer active: {lease.projection_job_id}"
            )
        return row

    @staticmethod
    def _receipt_row(
        connection: sqlite3.Connection,
        projection_job_id: str,
        backend: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM projection_receipts
            WHERE projection_job_id = ? AND backend = ?
            """,
            (projection_job_id, backend),
        ).fetchone()
        if row is None:
            raise LifecycleNotFoundError(
                f"projection receipt not found: job={projection_job_id!r} backend={backend!r}"
            )
        return row

    def census(self) -> LifecycleCensus:
        """Return exact relational counts for lifecycle reconciliation."""
        with self._connect() as connection:
            source_row = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(SUM(byte_length), 0) AS retained_bytes
                FROM source_revisions
                """
            ).fetchone()
            current_sources = int(
                connection.execute("SELECT COUNT(*) FROM source_heads").fetchone()[0]
            )
            projection_jobs = int(
                connection.execute("SELECT COUNT(*) FROM projection_jobs").fetchone()[0]
            )
            projection_receipts = int(
                connection.execute("SELECT COUNT(*) FROM projection_receipts").fetchone()[0]
            )
            job_states = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM projection_jobs GROUP BY state"
                ).fetchall()
            }
            receipt_states = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM projection_receipts GROUP BY state"
                ).fetchall()
            }
        if source_row is None:
            raise LifecycleSchemaError("source census query returned no row")
        return LifecycleCensus(
            source_revisions=int(source_row["count"]),
            current_sources=current_sources,
            retained_bytes=int(source_row["retained_bytes"]),
            projection_jobs=projection_jobs,
            projection_receipts=projection_receipts,
            job_states=job_states,
            receipt_states=receipt_states,
        )

    def generation_census(
        self,
        *,
        generation_id: str,
        projection_version: str,
        config_digest: str,
    ) -> GenerationCensus:
        """Return exact projection counts for current heads in one generation."""
        if not generation_id.strip():
            raise ValueError("generation_id must be a non-empty string")
        if not projection_version.strip():
            raise ValueError("projection_version must be a non-empty string")
        if not config_digest.strip():
            raise ValueError("config_digest must be a non-empty string")
        parameters = (generation_id, projection_version, config_digest)
        with self._connect() as connection:
            current_sources = int(
                connection.execute("SELECT COUNT(*) FROM source_heads").fetchone()[0]
            )
            current_projection_jobs = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM source_heads AS head
                    JOIN projection_jobs AS job
                      ON job.source_revision_id = head.source_revision_id
                    WHERE job.generation_id = ? AND job.projection_version = ?
                      AND job.config_digest = ?
                    """,
                    parameters,
                ).fetchone()[0]
            )
            job_rows = connection.execute(
                """
                SELECT job.state, COUNT(*) AS count
                FROM source_heads AS head
                JOIN projection_jobs AS job
                  ON job.source_revision_id = head.source_revision_id
                WHERE job.generation_id = ? AND job.projection_version = ?
                  AND job.config_digest = ?
                GROUP BY job.state
                """,
                parameters,
            ).fetchall()
            receipt_rows = connection.execute(
                """
                SELECT receipt.backend, receipt.state, COUNT(*) AS count
                FROM source_heads AS head
                JOIN projection_jobs AS job
                  ON job.source_revision_id = head.source_revision_id
                JOIN projection_receipts AS receipt
                  ON receipt.projection_job_id = job.projection_job_id
                WHERE job.generation_id = ? AND job.projection_version = ?
                  AND job.config_digest = ?
                GROUP BY receipt.backend, receipt.state
                """,
                parameters,
            ).fetchall()
        job_states = {
            str(row["state"]): int(row["count"])
            for row in job_rows
        }
        receipt_states: dict[str, int] = {}
        receipts_by_backend: dict[str, int] = {}
        searchable_by_backend: dict[str, int] = {}
        for row in receipt_rows:
            backend = str(row["backend"])
            state = str(row["state"])
            count = int(row["count"])
            receipts_by_backend[backend] = receipts_by_backend.get(backend, 0) + count
            receipt_states[state] = receipt_states.get(state, 0) + count
            if state == "searchable":
                searchable_by_backend[backend] = count
        return GenerationCensus(
            generation_id=generation_id,
            projection_version=projection_version,
            config_digest=config_digest,
            current_sources=current_sources,
            current_projection_jobs=current_projection_jobs,
            current_projection_receipts=sum(receipts_by_backend.values()),
            current_job_states=dict(sorted(job_states.items())),
            current_receipt_states=dict(sorted(receipt_states.items())),
            current_receipts_by_backend=dict(sorted(receipts_by_backend.items())),
            current_searchable_by_backend=dict(sorted(searchable_by_backend.items())),
        )

    def next_wakeup_delay(
        self,
        *,
        generation_id: str | None = None,
        projection_version: str | None = None,
        config_digest: str | None = None,
        now: datetime | None = None,
        include_deferred: bool = False,
        deferred_only: bool = False,
    ) -> float | None:
        """Return seconds until the next pending job or expired lease is claimable."""
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("lifecycle timestamps must include a timezone")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT CASE
                    WHEN state = 'pending' THEN available_at
                    WHEN state = 'running' THEN leased_until
                    WHEN state = 'deferred' THEN available_at
                END AS due_at
                FROM projection_jobs
                WHERE (? = 0 OR state = 'deferred')
                  AND (
                    state IN ('pending', 'running')
                    OR (? = 1 AND state = 'deferred')
                )
                  AND (? IS NULL OR generation_id = ?)
                  AND (? IS NULL OR projection_version = ?)
                  AND (? IS NULL OR config_digest = ?)
                """,
                (
                    int(deferred_only),
                    int(include_deferred),
                    generation_id,
                    generation_id,
                    projection_version,
                    projection_version,
                    config_digest,
                    config_digest,
                ),
            ).fetchall()
        due_times = [
            _parse_utc(str(row["due_at"]))
            for row in rows
            if row["due_at"] is not None
        ]
        if not due_times:
            return None
        return max(0.0, (min(due_times) - current.astimezone(UTC)).total_seconds())

    @staticmethod
    def _operation_state(
        job: ProjectionJob,
        receipts: tuple[ProjectionReceipt, ...],
    ) -> str:
        states = {receipt.state for receipt in receipts}
        if "failed" in states or job.state == "failed":
            return "failed"
        if "stale" in states or job.state == "stale":
            return "stale"
        if receipts and states == {"searchable"}:
            return "searchable"
        if receipts and states <= {"completed", "searchable"}:
            return "completed"
        if "running" in states or job.state == "running":
            return "running"
        return "pending"

    @staticmethod
    def _record_schema_version(row: sqlite3.Row, record_kind: str) -> int:
        version = int(row["schema_version"])
        if version != LIFECYCLE_SCHEMA_VERSION:
            raise LifecycleSchemaError(
                f"unknown lifecycle record schema {version} for {record_kind}; "
                f"supported version is {LIFECYCLE_SCHEMA_VERSION}"
            )
        return version

    @classmethod
    def _source_revision(cls, row: sqlite3.Row) -> SourceRevision:
        return SourceRevision(
            schema_version=cls._record_schema_version(row, "source revision"),
            source_revision_id=str(row["source_revision_id"]),
            source_key=str(row["source_key"]),
            dataset=str(row["dataset"]),
            content_sha256=str(row["content_sha256"]),
            byte_length=int(row["byte_length"]),
            retained_content_ref=str(row["retained_content_ref"]),
            source_locator=row["source_locator"],
            media_type=str(row["media_type"]),
            previous_revision_id=row["previous_revision_id"],
            capture_actor_id=str(row["capture_actor_id"]),
            capture_run_id=row["capture_run_id"],
            capture_metadata=json.loads(row["capture_metadata_json"]),
            captured_at=str(row["captured_at"]),
            accepted_at=str(row["accepted_at"]),
            tombstone=bool(row["tombstone"]),
        )

    @classmethod
    def _projection_job(cls, row: sqlite3.Row) -> ProjectionJob:
        return ProjectionJob(
            schema_version=cls._record_schema_version(row, "projection job"),
            projection_job_id=str(row["projection_job_id"]),
            source_revision_id=str(row["source_revision_id"]),
            generation_id=str(row["generation_id"]),
            dataset=str(row["dataset"]),
            projection_version=str(row["projection_version"]),
            config_digest=str(row["config_digest"]),
            required_backends=tuple(json.loads(row["required_backends_json"])),
            idempotency_key=str(row["idempotency_key"]),
            state=str(row["state"]),
            attempt=int(row["attempt"]),
            lease_id=row["lease_id"],
            lease_owner=row["lease_owner"],
            leased_until=row["leased_until"],
            available_at=str(row["available_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
        )

    @classmethod
    def _projection_receipt(cls, row: sqlite3.Row) -> ProjectionReceipt:
        state = str(row["state"])
        if state not in _RECEIPT_STATES:
            raise LifecycleSchemaError(f"unknown projection receipt state: {state}")
        return ProjectionReceipt(
            schema_version=cls._record_schema_version(row, "projection receipt"),
            projection_receipt_id=str(row["projection_receipt_id"]),
            projection_job_id=str(row["projection_job_id"]),
            source_revision_id=str(row["source_revision_id"]),
            generation_id=str(row["generation_id"]),
            dataset=str(row["dataset"]),
            backend=str(row["backend"]),
            provider=str(row["provider"]),
            projection_version=str(row["projection_version"]),
            state=state,
            attempt=int(row["attempt"]),
            provider_operation_id=row["provider_operation_id"],
            affected_ids=tuple(json.loads(row["affected_ids_json"])),
            affected_count=row["affected_count"],
            model=row["model"],
            dimensions=row["dimensions"],
            metadata=json.loads(row["metadata_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=row["completed_at"],
            searchable_at=row["searchable_at"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )
