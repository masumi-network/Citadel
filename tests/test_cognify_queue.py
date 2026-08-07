"""Focused contract tests for the standalone cognify retry queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import multiprocessing
from pathlib import Path

import pytest

from kb.cognify_queue import (
    CognifyLeaseError,
    CognifyQueueStateError,
    CognifyRetryQueue,
)


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _enqueue_from_process(path: str, datasets: tuple[str, ...], start: object) -> None:
    start.wait()
    CognifyRetryQueue(path).enqueue(datasets, now=T0)


def test_enqueue_is_content_free_and_merges_duplicate_dataset_sets(tmp_path: Path) -> None:
    path = tmp_path / "cognify-queue.json"
    queue = CognifyRetryQueue(path)

    first = queue.enqueue(("seat:alice", "central", "central"), now=T0)
    second = queue.enqueue(("seat:bob", "central"), now=T0)

    assert second.job_id == first.job_id
    assert second.datasets == ("central", "seat:alice", "seat:bob")
    assert len(queue.snapshot()) == 1
    persisted = json.loads(path.read_text(encoding="utf-8"))
    record = next(iter(persisted["jobs"].values()))
    assert record["datasets"] == ["central", "seat:alice", "seat:bob"]
    assert "content" not in record
    assert "document_ids" not in record


def test_claim_and_acknowledge_remove_completed_work(tmp_path: Path) -> None:
    queue = CognifyRetryQueue(tmp_path / "queue.json", lease_seconds=30)
    queue.enqueue(("central",), now=T0)

    lease = queue.claim(now=T0)

    assert lease is not None
    assert lease.datasets == ("central",)
    assert lease.attempt == 1
    assert queue.claim(now=T0) is None

    queue.acknowledge(lease, now=T0 + timedelta(seconds=1))

    assert queue.snapshot() == ()


def test_renew_extends_lease_and_delays_reclaim(tmp_path: Path) -> None:
    queue = CognifyRetryQueue(tmp_path / "queue.json", lease_seconds=10)
    queue.enqueue(("central",), now=T0)
    lease = queue.claim(now=T0)
    assert lease is not None

    renewed = queue.renew(lease, now=T0 + timedelta(seconds=5))

    assert _at(renewed.leased_until) == T0 + timedelta(seconds=15)
    assert queue.claim(now=T0 + timedelta(seconds=12)) is None
    assert queue.next_wakeup_delay(now=T0 + timedelta(seconds=12)) == pytest.approx(3)

    reclaimed = queue.claim(now=T0 + timedelta(seconds=16))
    assert reclaimed is not None
    assert reclaimed.attempt == 2


def test_enqueue_during_lease_survives_acknowledgement(tmp_path: Path) -> None:
    queue = CognifyRetryQueue(tmp_path / "queue.json", lease_seconds=30)
    queue.enqueue(("central",), now=T0)
    lease = queue.claim(now=T0)
    assert lease is not None

    queue.enqueue(("seat:alice",), now=T0 + timedelta(seconds=1))
    queue.acknowledge(lease, now=T0 + timedelta(seconds=2))

    pending = queue.snapshot()
    assert len(pending) == 1
    assert pending[0].datasets == ("seat:alice",)
    assert pending[0].leased is False


def test_failure_reschedules_with_bounded_exponential_backoff(tmp_path: Path) -> None:
    queue = CognifyRetryQueue(
        tmp_path / "queue.json",
        backoff_seconds=5,
        max_backoff_seconds=12,
    )
    queue.enqueue(("central",), now=T0)

    first = queue.claim(now=T0)
    assert first is not None
    failed = queue.reschedule(first, error="temporary graph failure", now=T0)
    assert failed.attempt == 1
    assert failed.last_error == "temporary graph failure"
    assert _at(failed.available_at) == T0 + timedelta(seconds=5)
    assert queue.claim(now=T0 + timedelta(seconds=4)) is None

    second = queue.claim(now=T0 + timedelta(seconds=5))
    assert second is not None
    failed_again = queue.reschedule(second, error="still unavailable", now=T0 + timedelta(seconds=5))
    assert _at(failed_again.available_at) == T0 + timedelta(seconds=15)

    third = queue.claim(now=T0 + timedelta(seconds=15))
    assert third is not None
    bounded = queue.reschedule(third, error="again", now=T0 + timedelta(seconds=15))
    assert _at(bounded.available_at) == T0 + timedelta(seconds=27)


def test_expired_lease_is_recovered_and_old_ack_is_rejected(tmp_path: Path) -> None:
    queue = CognifyRetryQueue(tmp_path / "queue.json", lease_seconds=10)
    queue.enqueue(("central",), now=T0)
    old_lease = queue.claim(now=T0)
    assert old_lease is not None

    assert queue.recover_stale_leases(now=T0 + timedelta(seconds=11)) == 1
    recovered = queue.snapshot()[0]
    assert recovered.leased is False
    assert recovered.available_at == recovered.updated_at

    with pytest.raises(CognifyLeaseError, match="no longer active"):
        queue.acknowledge(old_lease, now=T0 + timedelta(seconds=11))


def test_expired_lease_can_be_reclaimed(tmp_path: Path) -> None:
    queue = CognifyRetryQueue(tmp_path / "queue.json", lease_seconds=10)
    queue.enqueue(("central",), now=T0)
    first = queue.claim(now=T0)
    assert first is not None

    second = queue.claim(now=T0 + timedelta(seconds=11))

    assert second is not None
    assert second.job_id == first.job_id
    assert second.lease_id != first.lease_id
    assert second.attempt == 2


def test_malformed_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    path.write_text("{\"version\": 1,", encoding="utf-8")
    queue = CognifyRetryQueue(path)

    with pytest.raises(CognifyQueueStateError, match="refusing to treat"):
        queue.enqueue(("central",), now=T0)


@pytest.mark.parametrize(
    "record_update",
    [
        {"datasets": None},
        {"lease_id": "lease", "leased_until": None, "lease_datasets": None},
    ],
)
def test_malformed_record_types_fail_closed(tmp_path: Path, record_update: dict[str, object]) -> None:
    path = tmp_path / "queue.json"
    timestamp = T0.isoformat().replace("+00:00", "Z")
    record: dict[str, object] = {
        "job_id": "job",
        "datasets": ["central"],
        "created_at": timestamp,
        "updated_at": timestamp,
        "available_at": timestamp,
        "attempt": 0,
        "lease_id": None,
        "leased_until": None,
        "lease_datasets": None,
        "last_error": None,
    }
    record.update(record_update)
    path.write_text(json.dumps({"version": 1, "jobs": {"job": record}}), encoding="utf-8")

    with pytest.raises(CognifyQueueStateError):
        CognifyRetryQueue(path).snapshot()


def test_state_with_unknown_content_field_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    timestamp = T0.isoformat().replace("+00:00", "Z")
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": {
                    "job": {
                        "job_id": "job",
                        "datasets": ["central"],
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "available_at": timestamp,
                        "attempt": 0,
                        "lease_id": None,
                        "leased_until": None,
                        "lease_datasets": None,
                        "last_error": None,
                        "content": "must never be persisted",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CognifyQueueStateError):
        CognifyRetryQueue(path).snapshot()


def test_atomic_write_failure_keeps_previous_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "queue.json"
    queue = CognifyRetryQueue(path)
    queue.enqueue(("central",), now=T0)
    before = path.read_text(encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("rename interrupted")

    monkeypatch.setattr("kb.cognify_queue.os.replace", fail_replace)
    with pytest.raises(OSError, match="rename interrupted"):
        queue.enqueue(("seat:alice",), now=T0)

    assert path.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("queue.json.*.tmp"))


def test_cross_process_enqueue_does_not_lose_updates(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(target=_enqueue_from_process, args=(str(path), (dataset,), start))
        for dataset in ("central", "seat:alice")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    jobs = CognifyRetryQueue(path).snapshot()
    assert len(jobs) == 1
    assert jobs[0].datasets == ("central", "seat:alice")
