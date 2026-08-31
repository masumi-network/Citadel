"""Bounded barriers for exact lifecycle projection jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import inspect
import math
from uuid import uuid4
from typing import Any


_REQUIRED_BACKENDS = frozenset({"relational", "vector", "graph"})
_POLL_SECONDS = 0.5
_TERMINAL_FAILURE_STATES = frozenset({"failed", "stale"})


@dataclass(frozen=True)
class ProjectionBarrierResult:
    job_ids: tuple[str, ...]
    searchable_job_ids: tuple[str, ...]
    pending_job_ids: tuple[str, ...]
    failed_job_ids: tuple[str, ...]
    complete: bool


def _supports_keyword(callable_obj: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind is parameter.VAR_KEYWORD
        or (
            parameter.name == name
            and parameter.kind is not parameter.POSITIONAL_ONLY
        )
        for parameter in parameters
    )


async def _resume_lifecycle_queue(
    citadel: Any,
    *,
    job_ids: Sequence[str] | None,
    job_filter_owner: str | None,
) -> None:
    resume = getattr(citadel, "resume_lifecycle_queue", None)
    if not callable(resume):
        return
    supports_job_ids = _supports_keyword(resume, "job_ids")
    supports_owner = _supports_keyword(resume, "job_filter_owner")
    if job_ids is None and not supports_job_ids:
        return
    kwargs: dict[str, Any] = {"include_deferred": True}
    if supports_job_ids:
        kwargs["job_ids"] = job_ids
    if supports_owner:
        kwargs["job_filter_owner"] = job_filter_owner
    try:
        result = resume(**kwargs)
    except TypeError as exc:
        message = str(exc)
        if "job_ids" not in message and "job_filter_owner" not in message:
            if "include_deferred" not in message:
                raise
        if job_ids is None:
            return
        try:
            result = resume(include_deferred=True)
        except TypeError as fallback_exc:
            if "include_deferred" not in str(fallback_exc):
                raise
            result = resume()
    if inspect.isawaitable(result):
        await result

async def _read_operation(citadel: Any, job_id: str, remaining: float) -> Any:
    read = getattr(citadel, "lifecycle_operation", None)
    if callable(read):
        result = read(job_id)
        if inspect.isawaitable(result):
            return await result
        return result

    wait = getattr(citadel, "wait_for_lifecycle_operation", None)
    if not callable(wait):
        raise AttributeError("citadel has no lifecycle operation reader")
    try:
        result = wait(job_id, timeout_seconds=max(remaining, 0.001))
    except TypeError as exc:
        if "timeout_seconds" not in str(exc):
            raise
        result = wait(job_id)
    if inspect.isawaitable(result):
        return await result
    return result


def _operation_state(operation: Any) -> str:
    if not isinstance(operation, Mapping):
        return "pending"
    state = str(operation.get("state") or "").strip().lower()
    receipts = operation.get("receipts")
    receipt_states: dict[str, str] = {}
    if isinstance(receipts, Mapping):
        receipt_states = {
            str(backend).strip().lower(): str(receipt_state).strip().lower()
            for backend, receipt_state in receipts.items()
            if str(backend).strip()
        }
    elif isinstance(receipts, Sequence) and not isinstance(receipts, (str, bytes)):
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            backend = str(receipt.get("backend") or "").strip().lower()
            if backend:
                receipt_states[backend] = str(
                    receipt.get("state") or ""
                ).strip().lower()

    if state in _TERMINAL_FAILURE_STATES or any(
        receipt_states.get(backend) in _TERMINAL_FAILURE_STATES
        for backend in _REQUIRED_BACKENDS
    ):
        return "failed"
    if (
        _REQUIRED_BACKENDS.issubset(receipt_states)
        and all(
            receipt_states[backend] == "searchable"
            for backend in _REQUIRED_BACKENDS
        )
    ):
        return "searchable"
    return "pending"


def _batch_state_reader(citadel: Any) -> Any | None:
    read = getattr(citadel, "projection_states_for_job_ids", None)
    if callable(read):
        return read
    store = getattr(citadel, "lifecycle_store", None)
    read = getattr(store, "projection_states_for_job_ids", None)
    if callable(read):
        return read
    if store is not None:
        raise AttributeError("citadel has no batch projection state reader")
    return None


async def _read_projection_states(
    read: Any,
    job_ids: tuple[str, ...],
) -> Mapping[str, Any]:
    result = read(job_ids)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Mapping):
        raise TypeError("batch projection state reader must return a mapping")
    return result


def _job_ids(job_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(job_ids, (str, bytes)):
        raise TypeError("job_ids must be a sequence of strings")
    values = tuple(job_ids)
    if any(not isinstance(job_id, str) or not job_id.strip() for job_id in values):
        raise ValueError("job_ids must contain only non-empty strings")
    return tuple(dict.fromkeys(values))


async def _poll_projection_jobs(
    citadel: Any,
    ordered_job_ids: tuple[str, ...],
    *,
    timeout_seconds: float,
) -> ProjectionBarrierResult:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    pending = list(ordered_job_ids)
    searchable: set[str] = set()
    failed: set[str] = set()
    batch_read = _batch_state_reader(citadel)

    while pending:
        next_pending: list[str] = []
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        if batch_read is not None:
            try:
                async with asyncio.timeout(remaining):
                    states = await _read_projection_states(
                        batch_read,
                        tuple(pending),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                next_pending.extend(pending)
            else:
                for job_id in pending:
                    operation = states.get(job_id)
                    state = _operation_state(operation)
                    if state == "searchable":
                        searchable.add(job_id)
                    elif state == "failed":
                        failed.add(job_id)
                    else:
                        next_pending.append(job_id)
        else:
            for index, job_id in enumerate(pending):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    next_pending.extend(pending[index:])
                    break
                try:
                    async with asyncio.timeout(remaining):
                        operation = await _read_operation(citadel, job_id, remaining)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    next_pending.append(job_id)
                    continue
                except Exception as exc:
                    if isinstance(exc, RuntimeError) and any(
                        marker in str(exc).lower() for marker in _TERMINAL_FAILURE_STATES
                    ):
                        failed.add(job_id)
                    else:
                        next_pending.append(job_id)
                    continue
                state = _operation_state(operation)
                if state == "searchable":
                    searchable.add(job_id)
                elif state == "failed":
                    failed.add(job_id)
                else:
                    next_pending.append(job_id)

        pending = next_pending
        if not pending:
            break
        remaining = deadline - loop.time()
        if remaining <= _POLL_SECONDS:
            break
        await asyncio.sleep(_POLL_SECONDS)

    pending_ids = tuple(job_id for job_id in ordered_job_ids if job_id in pending)
    searchable_ids = tuple(
        job_id for job_id in ordered_job_ids if job_id in searchable
    )
    failed_ids = tuple(job_id for job_id in ordered_job_ids if job_id in failed)
    return ProjectionBarrierResult(
        job_ids=ordered_job_ids,
        searchable_job_ids=searchable_ids,
        pending_job_ids=pending_ids,
        failed_job_ids=failed_ids,
        complete=not pending_ids and not failed_ids,
    )




async def wait_for_projection_barrier(
    citadel: Any,
    job_ids: Sequence[str],
    *,
    timeout_seconds: float,
) -> ProjectionBarrierResult:
    """Wait for exact jobs to become searchable across every required backend."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    ordered_job_ids = _job_ids(job_ids)
    if not ordered_job_ids:
        return ProjectionBarrierResult((), (), (), (), True)

    job_filter_owner = f"projection-barrier:{uuid4().hex}"
    try:
        await _resume_lifecycle_queue(
            citadel,
            job_ids=ordered_job_ids,
            job_filter_owner=job_filter_owner,
        )
        return await _poll_projection_jobs(
            citadel,
            ordered_job_ids,
            timeout_seconds=timeout_seconds,
        )
    finally:
        await _resume_lifecycle_queue(
            citadel,
            job_ids=None,
            job_filter_owner=job_filter_owner,
        )
