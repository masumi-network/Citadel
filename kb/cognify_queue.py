"""Durable, content-free queue for deferred Cognee cognify work.

The queue stores dataset names and execution metadata only. It deliberately has
no Cognee or service dependency so a later integration can choose how to run a
claimed dataset set without making the queue responsible for that work.

State mutations use a sidecar ``flock`` across the complete read-modify-write
and an atomic, fsynced replacement. Existing state is validated strictly;
corruption raises instead of being interpreted as an empty queue.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
from uuid import uuid4


STATE_VERSION = 1
DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_BACKOFF_SECONDS = 5.0
DEFAULT_MAX_BACKOFF_SECONDS = 3600.0
MAX_ERROR_LENGTH = 512

_EXECUTION_HANDLES_LOCK = threading.Lock()
_EXECUTION_HANDLES: dict[int, Any] = {}


def _before_fork() -> None:
    _EXECUTION_HANDLES_LOCK.acquire()


def _after_fork_parent() -> None:
    _EXECUTION_HANDLES_LOCK.release()


def _after_fork_child() -> None:
    try:
        for handle in _EXECUTION_HANDLES.values():
            try:
                handle.close()
            except OSError:
                # Child cleanup is best-effort; continue closing other handles.
                pass
        _EXECUTION_HANDLES.clear()
    finally:
        _EXECUTION_HANDLES_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )

_RECORD_FIELDS = frozenset(
    {
        "job_id",
        "datasets",
        "created_at",
        "updated_at",
        "available_at",
        "attempt",
        "lease_id",
        "leased_until",
        "lease_datasets",
        "last_error",
    }
)


class CognifyQueueError(RuntimeError):
    """Base error for queue and lease failures."""


class CognifyQueueStateError(CognifyQueueError):
    """The queue file exists but is not a valid queue state."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            f"Cognify queue state {path} is invalid ({reason}); "
            "refusing to treat it as an empty queue."
        )
        self.path = path
        self.reason = reason


class CognifyLeaseError(CognifyQueueError):
    """A lease is unknown, stale, or does not match its queue record."""


@dataclass(slots=True)
class CognifyExecutionGuard:
    """Owned cross-process execution lock for one queue drain."""

    _handle: Any
    _released: bool = False

    def release(self) -> None:
        """Release the execution lock and close its owned descriptor once."""
        if self._released:
            return
        self._released = True
        with _EXECUTION_HANDLES_LOCK:
            if self._handle.closed:
                return
            _EXECUTION_HANDLES.pop(self._handle.fileno(), None)
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()

    def __enter__(self) -> CognifyExecutionGuard:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class CognifyJob:
    """A content-free queued dataset set and its retry metadata."""

    job_id: str
    datasets: tuple[str, ...]
    created_at: str
    updated_at: str
    available_at: str
    attempt: int
    lease_id: str | None
    leased_until: str | None
    last_error: str | None

    @property
    def id(self) -> str:
        """Short alias for callers that name queue records by ``id``."""
        return self.job_id

    @property
    def leased(self) -> bool:
        return self.lease_id is not None


@dataclass(frozen=True, slots=True)
class CognifyLease:
    """A claim token and immutable dataset snapshot for one worker attempt."""

    job_id: str
    lease_id: str
    datasets: tuple[str, ...]
    attempt: int
    leased_until: str


Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO-8601 timestamp") from exc
    return _as_utc(parsed, field_name=field_name)


def _normalize_datasets(value: Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("datasets must be an iterable of names, not one string")
    try:
        values = list(value)
    except TypeError as exc:
        raise ValueError("datasets must be an iterable of names") from exc
    normalized: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise ValueError("dataset names must be strings")
        name = item.strip()
        if not name:
            raise ValueError("dataset names must be non-empty")
        if any(ord(character) < 0x20 for character in name):
            raise ValueError("dataset names must not contain control characters")
        normalized.add(name)
    if not normalized:
        raise ValueError("at least one dataset name is required")
    return tuple(sorted(normalized))


def _bounded_error(error: str) -> str:
    if not isinstance(error, str):
        raise ValueError("error must be a string")
    normalized = " ".join(error.split())
    if not normalized:
        normalized = "unspecified cognify failure"
    return normalized[:MAX_ERROR_LENGTH]


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "jobs": {}}


class CognifyRetryQueue:
    """Cross-process durable queue for dataset-level cognify retries."""

    def __init__(
        self,
        path: Path | str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.lease_seconds = self._positive_duration(lease_seconds, "lease_seconds")
        self.backoff_seconds = self._positive_duration(backoff_seconds, "backoff_seconds")
        self.max_backoff_seconds = self._positive_duration(
            max_backoff_seconds, "max_backoff_seconds"
        )
        if self.max_backoff_seconds < self.backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= backoff_seconds")
        self._clock = clock or _default_clock
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_file: Any | None = None

    def try_acquire_execution(self) -> CognifyExecutionGuard | None:
        """Acquire queue-wide execution ownership without waiting.

        This lock is separate from the short state-mutation lock. Callers must
        acquire it before claiming work and retain it until active cognify work
        and its child-task cleanup have stopped.
        """
        lock_path = self.path.with_suffix(f"{self.path.suffix}.execute.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        with _EXECUTION_HANDLES_LOCK:
            descriptor = os.open(lock_path, flags, 0o600)
            try:
                handle = os.fdopen(descriptor, "a+")
            except BaseException:
                os.close(descriptor)
                raise
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                guard = CognifyExecutionGuard(handle)
                _EXECUTION_HANDLES[handle.fileno()] = handle
            except BlockingIOError:
                handle.close()
                return None
            except BaseException:
                handle.close()
                raise
        return guard

    @staticmethod
    def _positive_duration(value: float, field_name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{field_name} must be a positive number")
        return float(value)

    def enqueue(self, datasets: Iterable[str], *, now: datetime | None = None) -> CognifyJob:
        """Add dataset names, merging them into existing unleased work.

        A dataset set currently leased by a worker is extended only when the
        new enqueue overlaps that leased set. The lease keeps its original
        snapshot, and acknowledgement leaves any newly added names pending.
        """
        names = _normalize_datasets(datasets)
        current = self._resolve_now(now)
        current_text = _timestamp(current)
        with self._exclusive():
            state = self._load()
            self._recover_stale_in_state(state, current)
            jobs = state["jobs"]
            unleased = [record for record in jobs.values() if record["lease_id"] is None]
            leased_overlaps = [
                record
                for record in jobs.values()
                if record["lease_id"] is not None
                and set(record["datasets"]).intersection(names)
            ]

            if leased_overlaps:
                target = min(leased_overlaps, key=self._record_order)
                merged = set(target["datasets"]) | set(names)
                remove_ids: set[str] = set()
                # Absorb unleased records that became part of this same dataset
                # set, preventing duplicates around an active lease.
                absorbed = True
                while absorbed:
                    absorbed = False
                    for record in unleased:
                        if record["job_id"] in remove_ids:
                            continue
                        if set(record["datasets"]).intersection(merged):
                            merged.update(record["datasets"])
                            remove_ids.add(record["job_id"])
                            absorbed = True
                target["datasets"] = sorted(merged)
                for job_id in remove_ids:
                    jobs.pop(job_id, None)
                target["updated_at"] = current_text
                result = self._job_from_record(target)
            elif unleased:
                target = min(unleased, key=self._record_order)
                merged = set(names)
                for record in unleased:
                    merged.update(record["datasets"])
                for record in unleased:
                    if record is not target:
                        jobs.pop(record["job_id"], None)
                target.update(
                    {
                        "datasets": sorted(merged),
                        "available_at": current_text,
                        "attempt": max(record["attempt"] for record in unleased),
                        "last_error": None,
                        "updated_at": current_text,
                    }
                )
                result = self._job_from_record(target)
            else:
                job_id = f"cognify_{uuid4().hex}"
                record = {
                    "job_id": job_id,
                    "datasets": list(names),
                    "created_at": current_text,
                    "updated_at": current_text,
                    "available_at": current_text,
                    "attempt": 0,
                    "lease_id": None,
                    "leased_until": None,
                    "lease_datasets": None,
                    "last_error": None,
                }
                jobs[job_id] = record
                result = self._job_from_record(record)

            self._save(state)
            return result

    def claim(self, *, now: datetime | None = None, lease_seconds: float | None = None) -> CognifyLease | None:
        """Claim the oldest due record, or return ``None`` when none is due."""
        leases = self.claim_due(now=now, limit=1, lease_seconds=lease_seconds)
        return leases[0] if leases else None

    def claim_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1,
        lease_seconds: float | None = None,
    ) -> list[CognifyLease]:
        """Claim up to ``limit`` due records with unique lease tokens."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        duration = (
            self.lease_seconds
            if lease_seconds is None
            else self._positive_duration(lease_seconds, "lease_seconds")
        )
        current = self._resolve_now(now)
        current_text = _timestamp(current)
        leased_until = _timestamp(current + timedelta(seconds=duration))
        with self._exclusive():
            state = self._load()
            changed = self._recover_stale_in_state(state, current)
            due = [
                record
                for record in state["jobs"].values()
                if record["lease_id"] is None
                and _parse_timestamp(record["available_at"], field_name="available_at") <= current
            ]
            due.sort(key=self._record_order)
            leases: list[CognifyLease] = []
            for record in due[:limit]:
                lease_id = f"lease_{uuid4().hex}"
                dataset_snapshot = tuple(record["datasets"])
                record.update(
                    {
                        "attempt": record["attempt"] + 1,
                        "lease_id": lease_id,
                        "leased_until": leased_until,
                        "lease_datasets": list(dataset_snapshot),
                        "updated_at": current_text,
                    }
                )
                leases.append(
                    CognifyLease(
                        job_id=record["job_id"],
                        lease_id=lease_id,
                        datasets=dataset_snapshot,
                        attempt=record["attempt"],
                        leased_until=leased_until,
                    )
                )
                changed = True
            if changed:
                self._save(state)
            return leases

    def renew(
        self,
        lease: CognifyLease,
        *,
        now: datetime | None = None,
        lease_seconds: float | None = None,
    ) -> CognifyLease:
        """Extend one active lease without changing its dataset snapshot."""
        duration = (
            self.lease_seconds
            if lease_seconds is None
            else self._positive_duration(lease_seconds, "lease_seconds")
        )
        current = self._resolve_now(now)
        renewed_until = _timestamp(current + timedelta(seconds=duration))
        with self._exclusive():
            state = self._load()
            record = self._leased_record(state, lease, current)
            record.update({"leased_until": renewed_until, "updated_at": _timestamp(current)})
            self._save(state)
            return CognifyLease(
                job_id=record["job_id"],
                lease_id=record["lease_id"],
                datasets=tuple(record["lease_datasets"]),
                attempt=record["attempt"],
                leased_until=renewed_until,
            )

    def acknowledge(self, lease: CognifyLease, *, now: datetime | None = None) -> None:
        """Acknowledge one lease without losing datasets enqueued mid-lease."""
        current = self._resolve_now(now)
        with self._exclusive():
            state = self._load()
            record = self._leased_record(state, lease, current)
            claimed = set(record["lease_datasets"])
            remaining = [dataset for dataset in record["datasets"] if dataset not in claimed]
            if remaining:
                record.update(
                    {
                        "datasets": remaining,
                        "available_at": _timestamp(current),
                        "attempt": 0,
                        "lease_id": None,
                        "leased_until": None,
                        "lease_datasets": None,
                        "last_error": None,
                        "updated_at": _timestamp(current),
                    }
                )
            else:
                state["jobs"].pop(record["job_id"])
            self._save(state)

    def ack(self, lease: CognifyLease, *, now: datetime | None = None) -> None:
        """Alias for :meth:`acknowledge`."""
        self.acknowledge(lease, now=now)

    def reschedule(
        self,
        lease: CognifyLease,
        *,
        error: str,
        now: datetime | None = None,
    ) -> CognifyJob:
        """Return a failed lease to the queue with bounded exponential backoff."""
        failure = _bounded_error(error)
        current = self._resolve_now(now)
        with self._exclusive():
            state = self._load()
            record = self._leased_record(state, lease, current)
            delay = self._backoff(record["attempt"])
            record.update(
                {
                    "available_at": _timestamp(current + timedelta(seconds=delay)),
                    "lease_id": None,
                    "leased_until": None,
                    "lease_datasets": None,
                    "last_error": failure,
                    "updated_at": _timestamp(current),
                }
            )
            self._save(state)
            return self._job_from_record(record)

    def recover_stale_leases(self, *, now: datetime | None = None) -> int:
        """Make expired leases due again and return the number recovered."""
        current = self._resolve_now(now)
        with self._exclusive():
            state = self._load()
            count = self._recover_stale_in_state(state, current)
            if count:
                self._save(state)
            return count

    def next_wakeup_delay(self, *, now: datetime | None = None) -> float | None:
        """Return seconds until the next queued job can be claimed."""
        current = self._resolve_now(now)
        with self._exclusive():
            state = self._load()
            delays: list[float] = []
            for record in state["jobs"].values():
                field_name = "available_at" if record["lease_id"] is None else "leased_until"
                timestamp = _parse_timestamp(record[field_name], field_name=field_name)
                delays.append(max(0.0, (timestamp - current).total_seconds()))
            return min(delays) if delays else None

    def snapshot(self) -> tuple[CognifyJob, ...]:
        """Return all records without changing their lease state."""
        state = self._load()
        records = sorted(state["jobs"].values(), key=self._record_order)
        return tuple(self._job_from_record(record) for record in records)

    def list_jobs(self) -> tuple[CognifyJob, ...]:
        """Alias for :meth:`snapshot`."""
        return self.snapshot()

    def _resolve_now(self, value: datetime | None) -> datetime:
        return _as_utc(self._clock() if value is None else value, field_name="now")

    @staticmethod
    def _record_order(record: dict[str, Any]) -> tuple[str, str]:
        return str(record["available_at"]), str(record["job_id"])

    def _backoff(self, attempt: int) -> float:
        delay = self.backoff_seconds
        remaining_doublings = max(0, attempt - 1)
        while remaining_doublings and delay < self.max_backoff_seconds:
            delay = min(self.max_backoff_seconds, delay * 2)
            remaining_doublings -= 1
        return delay

    @staticmethod
    def _recover_stale_in_state(state: dict[str, Any], now: datetime) -> int:
        now_text = _timestamp(now)
        recovered = 0
        for record in state["jobs"].values():
            leased_until = record["leased_until"]
            if (
                record["lease_id"] is not None
                and leased_until is not None
                and _parse_timestamp(leased_until, field_name="leased_until") <= now
            ):
                record.update(
                    {
                        "available_at": now_text,
                        "lease_id": None,
                        "leased_until": None,
                        "lease_datasets": None,
                        "updated_at": now_text,
                    }
                )
                recovered += 1
        return recovered

    def _leased_record(
        self,
        state: dict[str, Any],
        lease: CognifyLease,
        now: datetime,
    ) -> dict[str, Any]:
        if not isinstance(lease, CognifyLease):
            raise TypeError("lease must be a CognifyLease")
        record = state["jobs"].get(lease.job_id)
        if record is None or record["lease_id"] != lease.lease_id:
            raise CognifyLeaseError(f"lease {lease.lease_id} is no longer active")
        leased_until = record["leased_until"]
        if leased_until is None or _parse_timestamp(leased_until, field_name="leased_until") <= now:
            raise CognifyLeaseError(f"lease {lease.lease_id} has expired")
        if tuple(record["lease_datasets"]) != lease.datasets:
            raise CognifyLeaseError(f"lease {lease.lease_id} dataset snapshot does not match")
        return record

    @staticmethod
    def _job_from_record(record: dict[str, Any]) -> CognifyJob:
        return CognifyJob(
            job_id=record["job_id"],
            datasets=tuple(record["datasets"]),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
            available_at=record["available_at"],
            attempt=record["attempt"],
            lease_id=record["lease_id"],
            leased_until=record["leased_until"],
            last_error=record["last_error"],
        )

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_state()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise CognifyQueueStateError(self.path, exc.__class__.__name__) from exc
        try:
            self._validate_state(data)
        except ValueError as exc:
            raise CognifyQueueStateError(self.path, str(exc)) from exc
        return data

    @classmethod
    def _validate_state(cls, data: Any) -> None:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        if set(data) != {"version", "jobs"}:
            raise ValueError("expected exactly version and jobs fields")
        if data["version"] != STATE_VERSION:
            raise ValueError(f"unsupported version {data['version']!r}")
        jobs = data["jobs"]
        if not isinstance(jobs, dict):
            raise ValueError("jobs must be an object")
        for job_id, record in jobs.items():
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("job keys must be non-empty strings")
            if not isinstance(record, dict):
                raise ValueError(f"job {job_id!r} must be an object")
            if set(record) != _RECORD_FIELDS:
                raise ValueError(f"job {job_id!r} has an invalid field set")
            if record["job_id"] != job_id:
                raise ValueError(f"job {job_id!r} has a mismatched job_id")
            datasets = record["datasets"]
            if not isinstance(datasets, list):
                raise ValueError(f"job {job_id!r} datasets must be a list")
            if tuple(datasets) != _normalize_datasets(datasets):
                raise ValueError(f"job {job_id!r} datasets are not canonical")
            if not isinstance(record["attempt"], int) or isinstance(record["attempt"], bool):
                raise ValueError(f"job {job_id!r} attempt must be an integer")
            if record["attempt"] < 0:
                raise ValueError(f"job {job_id!r} attempt cannot be negative")
            for field_name in ("created_at", "updated_at", "available_at"):
                _parse_timestamp(record[field_name], field_name=field_name)
            lease_id = record["lease_id"]
            leased_until = record["leased_until"]
            lease_datasets = record["lease_datasets"]
            if lease_id is not None and (not isinstance(lease_id, str) or not lease_id):
                raise ValueError(f"job {job_id!r} lease_id must be null or a string")
            if lease_id is None:
                if leased_until is not None or lease_datasets is not None:
                    raise ValueError(f"job {job_id!r} has lease metadata without a lease")
            else:
                _parse_timestamp(leased_until, field_name="leased_until")
                if not isinstance(lease_datasets, list):
                    raise ValueError(f"job {job_id!r} lease datasets must be a list")
                if tuple(lease_datasets) != _normalize_datasets(lease_datasets):
                    raise ValueError(f"job {job_id!r} lease datasets are not canonical")
                if not set(lease_datasets).issubset(set(datasets)):
                    raise ValueError(f"job {job_id!r} lease datasets exceed job datasets")
            error = record["last_error"]
            if error is not None and (
                not isinstance(error, str) or len(error) > MAX_ERROR_LENGTH
            ):
                raise ValueError(f"job {job_id!r} last_error is invalid")

    def _save(self, data: dict[str, Any]) -> None:
        self._validate_state(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, temp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f"{self.path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        descriptor_open = True
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                descriptor_open = False
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if descriptor_open:
                os.close(handle_fd)
            temp_path.unlink(missing_ok=True)
            raise

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Lock a sidecar across every read-modify-write operation."""
        with self._thread_lock:
            if self._lock_depth:
                self._lock_depth += 1
                try:
                    yield
                finally:
                    self._lock_depth -= 1
                return
            lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._lock_handle(lock_path)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _lock_handle(self, lock_path: Path) -> Any:
        existing = self._lock_file
        if existing is not None and not existing.closed:
            return existing
        self._lock_file = lock_path.open("a+")
        return self._lock_file


__all__ = [
    "CognifyExecutionGuard",
    "CognifyJob",
    "CognifyLease",
    "CognifyLeaseError",
    "CognifyQueueError",
    "CognifyQueueStateError",
    "CognifyRetryQueue",
]
