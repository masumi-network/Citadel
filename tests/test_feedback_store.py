"""Focused contracts for durable feedback and bounded decisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import pytest


def test_feedback_store_path_defaults_to_state_root_and_honors_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kb.config import CitadelConfig

    state_root = tmp_path / "state"
    monkeypatch.setenv("CITADEL_STATE_DIRECTORY", str(state_root))
    monkeypatch.delenv("CITADEL_FEEDBACK_STORE_PATH", raising=False)
    config = CitadelConfig.from_env(env_file=None)
    assert config.feedback_store_path == str(state_root / "feedback.sqlite3")

    explicit = tmp_path / "explicit-feedback.sqlite3"
    monkeypatch.setenv("CITADEL_FEEDBACK_STORE_PATH", str(explicit))
    assert CitadelConfig.from_env(env_file=None).feedback_store_path == str(explicit)



def test_direct_config_feedback_path_uses_state_root_and_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kb.config import CitadelConfig

    state_root = tmp_path / "state"
    monkeypatch.setenv("CITADEL_STATE_DIRECTORY", str(state_root))
    assert CitadelConfig().feedback_store_path == str(state_root / "feedback.sqlite3")

    explicit = tmp_path / "explicit.sqlite3"
    assert CitadelConfig(feedback_store_path=str(explicit)).feedback_store_path == str(explicit)


_EVENT = {
    "event_key": "search:test|result:test|actor:canary",
    "kind": "implicit_search",
    "search_id": "search:test",
    "result_id": "result:test",
    "actor_id": "actor:canary",
    "dataset": "seat:canary",
    "score": 0.1,
    "trust_tier": "unattested",
    "reason": "low_score",
    "occurred_at": "2026-08-31T12:00:00+00:00",
}


def test_feedback_event_is_idempotent_and_survives_reopen(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore

    path = tmp_path / "feedback.sqlite3"
    first = FeedbackStore(path).record_event(_EVENT)
    second = FeedbackStore(path).record_event(_EVENT)

    assert first == second
    reopened = FeedbackStore(path)
    pending = reopened.list_unprocessed()
    assert len(pending) == 1
    assert pending[0].event_id == first
    assert pending[0].dataset == "seat:canary"


def test_feedback_store_rejects_malformed_events_and_drops_secret_fields(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore, FeedbackValidationError

    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    with pytest.raises(FeedbackValidationError):
        store.record_event({"kind": "implicit_search", "dataset": "seat:canary"})

    event_id = store.record_event(
        {
            **_EVENT,
            "event_key": "search:secret|result:safe|actor:canary",
            "search_id": "search:secret",
            "result_id": "result:safe",
            "reason": "token=ctdl_abcdefghijklmnopqrstuvwxyz012345",
            "query": "private source body must not persist",
            "source_text": "private source body must not persist",
            "provider_body": {"secret": "sk_live_1234567890123456"},
        }
    )
    row = store.list_unprocessed()[0]
    assert row.event_id == event_id
    assert "query" not in row.__dict__
    assert "source" not in row.__dict__
    assert "provider" not in row.__dict__
    assert "ctdl_" not in (row.reason or "")
    with sqlite3.connect(tmp_path / "feedback.sqlite3") as connection:
        columns = {str(item[1]) for item in connection.execute("PRAGMA table_info(feedback_events)")}
    assert not {"query", "source_text", "provider_body", "token"} & columns

def test_feedback_store_rejects_kind_longer_than_bound(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore, FeedbackValidationError

    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    with pytest.raises(FeedbackValidationError, match="kind"):
        store.record_event({**_EVENT, "kind": "k" * 65})


def test_feedback_store_rejects_newer_schema_version(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackSchemaError, FeedbackStore

    path = tmp_path / "feedback.sqlite3"
    FeedbackStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(FeedbackSchemaError, match="newer"):
        FeedbackStore(path)


def test_feedback_schema_is_single_v1_without_alias_tables(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore

    path = tmp_path / "feedback.sqlite3"
    FeedbackStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "feedback_event_aliases" not in tables


def test_feedback_event_domain_separates_long_id_from_digest(tmp_path: Path) -> None:
    from hashlib import sha256

    from kb.feedback_store import FeedbackStore

    long_result_id = "result:" + "r" * 300
    digest_result_id = sha256(long_result_id.encode("utf-8")).hexdigest()
    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    long_id = store.record_event({**_EVENT, "result_id": long_result_id})
    digest_id = store.record_event({**_EVENT, "result_id": digest_result_id})

    assert long_id != digest_id
    store.record_decision(long_id, "no_action", "long identity")
    store.record_decision(digest_id, "no_action", "digest identity")
    assert {decision.event_id for decision in store.list_decisions()} == {long_id, digest_id}


def test_feedback_store_bounds_durable_strings_and_scores(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore

    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    event_id = store.record_event(
        {
            **_EVENT,
            "event_key": "bounded-event",
            "search_id": "s" * 10_000,
            "result_id": "r" * 10_000,
            "actor_id": "a" * 10_000,
            "dataset": "seat:canary",
            "trust_tier": "t" * 10_000,
            "reason": "reason " * 10_000,
        }
    )
    event = store.list_unprocessed()[0]
    assert event.event_id == event_id
    assert len(event.search_id or "") <= 256
    assert len(event.result_id or "") <= 256
    assert len(event.actor_id or "") <= 256
    assert len(event.trust_tier or "") <= 64
    assert len(event.reason or "") <= 256


def test_feedback_event_identity_keeps_delimiters_and_full_ids(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore

    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    delimiter_a = store.record_event(
        {
            **_EVENT,
            "kind": "a|b",
            "search_id": "c",
            "actor_id": "d",
            "result_id": "e",
        }
    )
    delimiter_b = store.record_event(
        {
            **_EVENT,
            "kind": "a",
            "search_id": "b|c",
            "actor_id": "d",
            "result_id": "e",
        }
    )
    long_a = store.record_event(
        {
            **_EVENT,
            "search_id": "s" * 256 + "a",
            "result_id": "result:long",
        }
    )
    long_b = store.record_event(
        {
            **_EVENT,
            "search_id": "s" * 256 + "b",
            "result_id": "result:long",
        }
    )

    assert delimiter_a != delimiter_b
    assert long_a != long_b


def test_feedback_event_identity_includes_normalized_score(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore

    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    positive = store.record_event({**_EVENT, "score": 1})
    retry = store.record_event({**_EVENT, "score": 1.0})
    negative = store.record_event({**_EVENT, "score": -1})

    assert positive == retry
    assert negative != positive


def test_feedback_store_allows_one_durable_decision_per_event(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore, FeedbackConflictError

    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    event_id = store.record_event(_EVENT)
    first = store.record_decision(event_id, "no_action", "implicit telemetry")
    assert first.event_id == event_id
    with pytest.raises(FeedbackConflictError):
        store.record_decision(event_id, "ranking_eval_candidate", "duplicate decision")
    assert store.list_unprocessed() == ()
    assert FeedbackStore(tmp_path / "feedback.sqlite3").list_unprocessed() == ()


@dataclass
class ReceiptLifecycle:
    missing: set[str]

    def has_missing_searchable_receipt(self, *, dataset: str, result_id: str) -> bool:
        return dataset == "seat:canary" and result_id in self.missing




def test_feedback_consumer_uses_private_source_identity_for_central_hits(
    tmp_path: Path,
) -> None:
    from kb.feedback_store import FeedbackStore, process_feedback_events

    class Lifecycle:
        def has_missing_searchable_receipt(self, *, dataset: str, result_id: str) -> bool:
            return dataset == "masumi-network" and result_id == "revision:central"

    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    private_id = store.record_event(
        {
            **_EVENT,
            "event_key": "private-central-hit",
            "dataset": "seat:alice",
            "actor_id": "actor:alice",
            "result_id": "result:central",
            "source_revision_id": "revision:central",
            "source_dataset": "masumi-network",
        }
    )
    shared_id = store.record_event(
        {
            **_EVENT,
            "event_key": "shared-central-presence",
            "dataset": "masumi-network",
            "actor_id": None,
            "result_id": None,
            "source_revision_id": None,
            "source_dataset": None,
        }
    )

    decisions = process_feedback_events(store, Lifecycle()).decisions
    by_event = {decision.event_id: decision.decision for decision in decisions}

    assert by_event[private_id] == "projection_repair_candidate"
    assert by_event[shared_id] == "no_action"
def test_feedback_consumer_repairs_only_lifecycle_missing_receipts(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore, process_feedback_events

    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    missing_id = store.record_event(
        {
            **_EVENT,
            "event_key": "missing-receipt",
            "result_id": "result:missing",
            "reason": "explicit result linked to lifecycle receipt gap",
        }
    )
    ranking_id = store.record_event(
        {
            **_EVENT,
            "event_key": "negative-rating",
            "kind": "explicit_feedback",
            "result_id": "result:ready",
            "score": -1,
            "reason": "rating",
        }
    )
    no_action_id = store.record_event(
        {
            **_EVENT,
            "event_key": "pending-provider",
            "result_id": "result:pending",
            "reason": "provider operation pending",
        }
    )

    result = process_feedback_events(
        store,
        ReceiptLifecycle({"result:missing"}),
        limit=10,
    )
    decisions = {decision.event_id: decision.decision for decision in result.decisions}
    assert decisions[missing_id] == "projection_repair_candidate"
    assert decisions[ranking_id] == "ranking_eval_candidate"
    assert decisions[no_action_id] == "no_action"
    assert process_feedback_events(store, ReceiptLifecycle({"result:missing"})).decisions == ()


def test_feedback_consumer_is_bounded_and_deterministic_for_1000_events(tmp_path: Path) -> None:
    from kb.feedback_store import FeedbackStore, process_feedback_events

    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path)
    for index in range(1000):
        store.record_event(
            {
                **_EVENT,
                "event_key": f"event-{index:04d}",
                "search_id": f"search:{index:04d}",
                "result_id": f"result:{index:04d}",
                "score": 1 if index % 2 else 0,
            }
        )
    first = process_feedback_events(store, ReceiptLifecycle(set()), limit=100)
    assert len(first.decisions) == 100
    reopened = FeedbackStore(path)
    second = process_feedback_events(reopened, ReceiptLifecycle(set()), limit=1000)
    assert len(second.decisions) == 900
    assert process_feedback_events(reopened, ReceiptLifecycle(set())).decisions == ()


def test_feedback_consumer_uses_lifecycle_receipt_rows_for_repairs(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from kb.feedback_store import FeedbackStore, process_feedback_events
    from kb.lifecycle import CaptureContext, LifecycleStore, ProjectionRequest

    lifecycle = LifecycleStore(tmp_path / "lifecycle.sqlite3")
    projection = ProjectionRequest(
        generation_id="generation:test",
        projection_version="projection:test",
        config_digest="digest:test",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    acceptance = lifecycle.accept_source(
        b"canary source",
        capture=CaptureContext(
            dataset="seat:canary",
            source_key="manual:canary",
            source_locator=None,
            media_type="text/plain",
            capture_actor_id="actor:canary",
            capture_run_id="capture:test",
            captured_at=datetime.now(UTC),
        ),
        projection=projection,
    )
    pending_feedback = FeedbackStore(tmp_path / "pending-feedback.sqlite3")
    pending_feedback.record_event(
        {
            **_EVENT,
            "event_key": "pending-receipt",
            "result_id": acceptance.source_revision_id,
        }
    )
    assert process_feedback_events(pending_feedback, lifecycle).decisions[0].decision == "no_action"
    with sqlite3.connect(tmp_path / "lifecycle.sqlite3") as connection:
        connection.execute(
            "DELETE FROM projection_receipts WHERE projection_job_id = ? AND backend = 'graph'",
            (acceptance.projection_job_id,),
        )

    feedback = FeedbackStore(tmp_path / "feedback.sqlite3")
    feedback.record_event(
        {
            **_EVENT,
            "event_key": "actual-lifecycle-gap",
            "result_id": acceptance.source_revision_id,
        }
    )
    result = process_feedback_events(feedback, lifecycle)

    assert result.decisions[0].decision == "projection_repair_candidate"


def test_feedback_consumer_uses_exact_lifecycle_lookup_past_fifty_operations(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from kb.feedback_store import FeedbackStore, process_feedback_events
    from kb.lifecycle import CaptureContext, LifecycleStore, ProjectionRequest

    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    lifecycle = LifecycleStore(lifecycle_path)
    projection = ProjectionRequest(
        generation_id="generation:old",
        projection_version="projection:test",
        config_digest="digest:test",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    target = None
    for index in range(51):
        acceptance = lifecycle.accept_source(
            f"source-{index}".encode(),
            capture=CaptureContext(
                dataset="seat:canary",
                source_key=f"manual:{index}",
                source_locator=None,
                media_type="text/plain",
                capture_actor_id="actor:canary",
                capture_run_id="capture:test",
                captured_at=datetime.now(UTC),
            ),
            projection=projection,
        )
        if index == 0:
            target = acceptance
    assert target is not None
    with sqlite3.connect(lifecycle_path) as connection:
        connection.execute(
            "DELETE FROM projection_receipts WHERE projection_job_id = ? AND backend = 'graph'",
            (target.projection_job_id,),
        )

    feedback = FeedbackStore(tmp_path / "feedback.sqlite3")
    feedback.record_event(
        {
            **_EVENT,
            "event_key": "old-current-source",
            "result_id": target.source_revision_id,
        }
    )

    decisions = process_feedback_events(feedback, lifecycle).decisions
    assert decisions[0].decision == "projection_repair_candidate"


def test_feedback_consumer_ignores_tombstoned_source_receipt_gaps(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from kb.feedback_store import FeedbackStore, process_feedback_events
    from kb.lifecycle import CaptureContext, LifecycleStore, ProjectionRequest

    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    lifecycle = LifecycleStore(lifecycle_path)
    projection = ProjectionRequest(
        generation_id="generation:tombstone",
        projection_version="projection:test",
        config_digest="digest:test",
        providers={"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"},
    )
    capture = CaptureContext(
        dataset="seat:canary",
        source_key="manual:tombstone",
        source_locator=None,
        media_type="text/plain",
        capture_actor_id="actor:canary",
        capture_run_id="capture:test",
        captured_at=datetime.now(UTC),
    )
    lifecycle.accept_source(b"current source", capture=capture, projection=projection)
    tombstone = lifecycle.accept_tombstone(
        reason="removed",
        capture=capture,
        projection=projection,
    )
    with sqlite3.connect(lifecycle_path) as connection:
        connection.execute(
            "DELETE FROM projection_receipts WHERE projection_job_id = ? AND backend = 'graph'",
            (tombstone.projection_job_id,),
        )

    feedback = FeedbackStore(tmp_path / "feedback.sqlite3")
    feedback.record_event(
        {
            **_EVENT,
            "event_key": "tombstone-gap",
            "result_id": tombstone.source_revision_id,
        }
    )

    decision = process_feedback_events(feedback, lifecycle).decisions[0]
    assert decision.decision == "no_action"




def test_feedback_store_concurrent_first_use_initializes_once(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from kb.feedback_store import FeedbackStore

    path = tmp_path / "feedback.sqlite3"
    with ThreadPoolExecutor(max_workers=4) as executor:
        stores = list(executor.map(lambda _: FeedbackStore(path), range(4)))

    assert len(stores) == 4
    event_id = stores[0].record_event(_EVENT)
    assert stores[-1].record_event(_EVENT) == event_id








