"""A write may only report an outcome it OBSERVED.

``IngestResult.accepted`` describes a REQUEST: it is True the instant
``cognee.add()`` returns. The graph write that makes a document retrievable is
``cognify``, and ``remember`` never runs it synchronously — every branch either
suppresses it, defers it to the caller, or hands it to a background task. So a
sync whose cognify silently never ran and a sync that indexed everything used to
emit byte-identical ``{"ok": true, "ingested": true}``.

These tests exist to make those two cases distinguishable. Each one simulates a
cognify that does not land and asserts the reported counter does not claim
success. Every one of them passes with cognify entirely broken if the
``index_state`` plumbing is removed, which is the defect stated as a test.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from kb.cognee_client import CogneePublicClient
from kb.config import CitadelConfig
from kb.models import (
    INDEX_STATE_INDEXED,
    INDEX_STATE_NOT_SCHEDULED,
    INDEX_STATE_PENDING,
    INDEX_STATE_REJECTED,
    INDEX_STATE_UNKNOWN,
    IngestResult,
    resolve_index_state,
)


# --------------------------------------------------------------------------
# The tri-state itself. `indexed` must never be True on an unobserved write,
# and `bool(...)` on the pending value must be falsy so a caller that forgets
# to branch fails closed instead of inheriting the optimistic answer.
# --------------------------------------------------------------------------


def test_pending_write_is_not_reported_as_indexed() -> None:
    result = IngestResult(True, "accepted", "ds", (), None, index_state=INDEX_STATE_PENDING)
    assert result.accepted is True  # the request was accepted
    assert result.indexed is None  # the outcome was NOT observed
    assert bool(result.indexed) is False  # a forgetful caller fails closed


def test_unknown_index_state_is_not_reported_as_indexed() -> None:
    # The default. A construction site that never learned about index_state
    # must not silently claim its writes are retrievable.
    result = IngestResult(True, "accepted", "ds", ())
    assert result.index_state == INDEX_STATE_UNKNOWN
    assert result.indexed is None


def test_rejected_write_is_reported_as_not_indexed() -> None:
    result = IngestResult(False, "duplicate_in_process", "ds", (), index_state=INDEX_STATE_REJECTED)
    assert result.indexed is False


def test_unscheduled_cognify_is_reported_as_not_indexed() -> None:
    # The silent death: the data is stored and nothing will ever index it.
    result = IngestResult(True, "accepted", "ds", (), index_state=INDEX_STATE_NOT_SCHEDULED)
    assert result.accepted is True
    assert result.indexed is False


def test_resolve_index_state_only_promotes_on_an_observed_cognify() -> None:
    assert resolve_index_state(INDEX_STATE_PENDING, "ok") == INDEX_STATE_INDEXED
    assert resolve_index_state(INDEX_STATE_PENDING, "failed") == "cognify_failed"
    assert resolve_index_state(INDEX_STATE_PENDING, "not_scheduled") == INDEX_STATE_NOT_SCHEDULED
    # Nothing observed leaves the state where it was — never promoted.
    assert resolve_index_state(INDEX_STATE_PENDING, None) == INDEX_STATE_PENDING
    # A successful cognify cannot rescue a write that never landed.
    assert resolve_index_state(INDEX_STATE_REJECTED, "ok") == INDEX_STATE_REJECTED


# --------------------------------------------------------------------------
# The one fix, in cognee_client.remember, that all four connectors inherit.
# --------------------------------------------------------------------------


def _fake_cognee(
    *,
    add_result: Any,
    cognify: Any = None,
) -> SimpleNamespace:
    async def run_startup_migrations() -> None:
        return None

    async def add(data: Any, **kwargs: Any) -> Any:
        return add_result

    async def default_cognify(*, datasets: Any, incremental_loading: bool) -> dict[str, Any]:
        return {"ok": True}

    return SimpleNamespace(
        run_startup_migrations=run_startup_migrations,
        add=add,
        cognify=cognify or default_cognify,
    )


@pytest.mark.asyncio
async def test_remember_reports_pending_never_indexed_for_a_backgrounded_cognify(
    monkeypatch: Any,
) -> None:
    # The normal production path. cognee.add() has returned; the Kuzu write has
    # NOT happened yet. "pending_cognify" is the whole point: it is the state a
    # caller can branch on instead of reading `accepted` as "retrievable".
    monkeypatch.delenv("CITADEL_SUPPRESS_INLINE_COGNIFY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setitem(sys.modules, "cognee", _fake_cognee(add_result={"ok": True}))

    client = CogneePublicClient()
    result = await client.remember("note", dataset_name="seat:sarthi", tags=())

    assert result["index_state"] == INDEX_STATE_PENDING
    assert result["index_state"] != INDEX_STATE_INDEXED
    import kb.cognee_client as cc

    await asyncio.gather(*list(cc._BACKGROUND_COGNIFY_TASKS), return_exceptions=True)


@pytest.mark.asyncio
async def test_remember_reports_not_scheduled_when_the_cognify_is_never_scheduled(
    monkeypatch: Any,
) -> None:
    # THE LOAD-BEARING CASE at the client. schedule_cognify can decline to
    # schedule (no running loop). Before index_state, remember returned
    # {"added": ..., "background_cognify": True} either way, so a write nothing
    # would ever index was indistinguishable from one that got indexed.
    monkeypatch.delenv("CITADEL_SUPPRESS_INLINE_COGNIFY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setitem(sys.modules, "cognee", _fake_cognee(add_result={"ok": True}))

    client = CogneePublicClient()
    monkeypatch.setattr(client, "schedule_cognify", lambda datasets: False)

    result = await client.remember("note", dataset_name="seat:sarthi", tags=())
    assert result["index_state"] == INDEX_STATE_NOT_SCHEDULED


@pytest.mark.asyncio
async def test_remember_reports_pending_when_cognify_is_suppressed(monkeypatch: Any) -> None:
    # Evolve Phase 1 is add-only by design; Phase 2 owes the graph write. That
    # is still "not yet indexed", not "indexed".
    monkeypatch.setenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "true")
    monkeypatch.setitem(sys.modules, "cognee", _fake_cognee(add_result={"ok": True}))

    client = CogneePublicClient()
    result = await client.remember("note", dataset_name="ds", tags=())
    assert result["cognify"] == "suppressed"
    assert result["index_state"] == INDEX_STATE_PENDING


@pytest.mark.asyncio
async def test_remember_reports_pending_when_cognify_is_deferred(monkeypatch: Any) -> None:
    monkeypatch.delenv("CITADEL_SUPPRESS_INLINE_COGNIFY", raising=False)
    monkeypatch.setitem(sys.modules, "cognee", _fake_cognee(add_result={"ok": True}))

    client = CogneePublicClient()
    result = await client.remember("note", dataset_name="ds", tags=(), defer_cognify=True)
    assert result["cognify"] == "deferred"
    assert result["index_state"] == INDEX_STATE_PENDING


@pytest.mark.asyncio
async def test_remember_surfaces_the_data_ids_cognee_assigned(monkeypatch: Any) -> None:
    # The real cognee return type, imported rather than hand-rolled: a fake that
    # invents a third party's shape only proves the parser parses the fake.
    from uuid import NAMESPACE_URL, uuid5

    from cognee.modules.pipelines.models.PipelineRunInfo import PipelineRunCompleted

    data_id = str(uuid5(NAMESPACE_URL, "data:note"))
    added = PipelineRunCompleted(
        pipeline_run_id=uuid5(NAMESPACE_URL, "run:note"),
        dataset_id=uuid5(NAMESPACE_URL, "ds"),
        dataset_name="ds",
        payload=None,
        data_ingestion_info=[{"data_id": data_id}],
    )
    monkeypatch.delenv("CITADEL_SUPPRESS_INLINE_COGNIFY", raising=False)
    monkeypatch.setitem(sys.modules, "cognee", _fake_cognee(add_result=added))

    client = CogneePublicClient()
    result = await client.remember("note", dataset_name="ds", tags=(), defer_cognify=True)
    assert result["data_ids"] == (data_id,)


@pytest.mark.asyncio
async def test_schedule_cognify_reports_whether_it_scheduled_anything() -> None:
    client = CogneePublicClient()
    scheduled: list[list[str]] = []

    async def fake_cognify(*, datasets: Any, force: bool = False) -> dict[str, Any]:
        scheduled.append(list(datasets))
        return {"ok": True}

    setattr(client, "cognify", fake_cognify)
    assert client.schedule_cognify([]) is False  # nothing to do is not a success
    assert client.schedule_cognify(["ds"]) is True

    import kb.cognee_client as cc

    await asyncio.gather(*list(cc._BACKGROUND_COGNIFY_TASKS), return_exceptions=True)
    assert scheduled == [["ds"]]
    assert client.cognify_status("ds") == "ok"


@pytest.mark.asyncio
async def test_background_cognify_failure_is_recorded_for_the_next_pass() -> None:
    # A background cognify that raises is logged and dropped; the write that
    # scheduled it has already been reported. Recording the outcome per dataset
    # is what makes the silent drop detectable on the NEXT pass.
    client = CogneePublicClient()

    async def failing_cognify(*, datasets: Any, force: bool = False) -> dict[str, Any]:
        raise RuntimeError("kuzu lock held")

    setattr(client, "cognify", failing_cognify)
    assert client.schedule_cognify(["ds"]) is True

    import kb.cognee_client as cc

    await asyncio.gather(*list(cc._BACKGROUND_COGNIFY_TASKS), return_exceptions=True)
    assert client.cognify_status("ds") == "failed"


# --------------------------------------------------------------------------
# Citadel.ingest carries the observed state through to every connector.
# --------------------------------------------------------------------------


class _StubCognee:
    """Minimal CogneeClient stand-in that returns whatever remember() should."""

    def __init__(self, remember_result: Any) -> None:
        self._remember_result = remember_result
        self.scheduled: list[list[str]] = []

    async def remember(self, data: Any, **kwargs: Any) -> Any:
        return self._remember_result

    def schedule_cognify(self, datasets: list[str]) -> bool:
        self.scheduled.append(list(datasets))
        return True

    def cognify_status(self, dataset: str) -> str | None:
        return None


def _citadel(remember_result: Any, tmp_path: Any) -> Any:
    from kb.service import Citadel

    config = CitadelConfig(
        access_store_path=str(tmp_path / "a.json"),
        content_scan_enabled=False,
    )
    citadel = Citadel(config)
    citadel.cognee = _StubCognee(remember_result)  # type: ignore[assignment]
    return citadel


@pytest.mark.asyncio
async def test_ingest_propagates_the_observed_index_state(tmp_path: Any) -> None:
    citadel = _citadel(
        {"added": {"ok": True}, "index_state": INDEX_STATE_NOT_SCHEDULED, "data_ids": ()},
        tmp_path,
    )
    result = await citadel.ingest("some durable note")
    assert result.accepted is True  # the request landed in the store
    assert result.index_state == INDEX_STATE_NOT_SCHEDULED
    assert result.indexed is False  # and nothing will ever index it


@pytest.mark.asyncio
async def test_ingest_propagates_data_ids(tmp_path: Any) -> None:
    citadel = _citadel(
        {"added": {"ok": True}, "index_state": INDEX_STATE_PENDING, "data_ids": ("d1", "d2")},
        tmp_path,
    )
    result = await citadel.ingest("another durable note")
    assert result.cognee_data_ids == ("d1", "d2")
    assert result.indexed is None


@pytest.mark.asyncio
async def test_ingest_of_an_unreadable_cognee_result_is_unknown_not_indexed(
    tmp_path: Any,
) -> None:
    # A client whose return value we cannot read is an absence of evidence, and
    # absence of evidence must never resolve to the optimistic value.
    citadel = _citadel(object(), tmp_path)
    result = await citadel.ingest("a third durable note")
    assert result.index_state == INDEX_STATE_UNKNOWN
    assert result.indexed is None


@pytest.mark.asyncio
async def test_rejected_ingest_reports_rejected_index_state(tmp_path: Any) -> None:
    citadel = _citadel({"index_state": INDEX_STATE_PENDING}, tmp_path)
    first = await citadel.ingest("duplicate note body")
    assert first.accepted is True
    second = await citadel.ingest("duplicate note body")
    assert second.accepted is False
    assert second.index_state == INDEX_STATE_REJECTED
    assert second.indexed is False


# --------------------------------------------------------------------------
# The connectors. Each one published `accepted` as its headline success, so a
# sync whose cognify silently died emitted the same {"ok": true,
# "ingested": true} as one that indexed everything. These are the counters.
# --------------------------------------------------------------------------


class _IndexStateCitadel:
    """A Citadel whose writes land in the store and are never indexed."""

    def __init__(self, config: CitadelConfig, index_state: str) -> None:
        self.config = config
        self._index_state = index_state
        self.ingest_calls: list[dict[str, Any]] = []

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        return IngestResult(
            True,
            "accepted",
            kwargs["dataset"],
            tuple(kwargs.get("tags") or ()),
            index_state=self._index_state,
            cognee_data_ids=("data-id-1",),
        )

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index_state", "expected_indexed"),
    [
        (INDEX_STATE_NOT_SCHEDULED, False),  # cognify silently never scheduled
        (INDEX_STATE_PENDING, None),  # queued, outcome not yet observed
    ],
)
async def test_github_sync_never_claims_an_unobserved_index(
    tmp_path: Any, index_state: str, expected_indexed: bool | None
) -> None:
    # THE LOAD-BEARING TEST for the GitHub connector. Before index_state both
    # rows produced {"ingested": true} and nothing else, so a digest that never
    # became retrievable was indistinguishable from one that did. That is very
    # likely the mechanism behind "a pass ran and its content never became
    # retrievable".
    import json

    from kb.github_sync import GitHubOrgSyncer
    from tests.test_github_sync import FakeGitHubClient

    config = CitadelConfig(
        github_sync_dataset="masumi-network",
        github_sync_session="masumi-github-daily",
        github_sync_state_path=str(tmp_path / "github_state.json"),
        content_scan_enabled=False,
    )
    citadel = _IndexStateCitadel(config, index_state)
    syncer = GitHubOrgSyncer(citadel, client=FakeGitHubClient(), org="masumi-network")  # type: ignore[arg-type]

    result = await syncer.run()

    assert result["ingested"] is True  # the write was accepted, and that is all
    assert result["index_state"] == index_state
    assert result["indexed"] is expected_indexed
    assert result["indexed"] is not True

    # And the ids cognee assigned are persisted, so the NEXT pass can check this
    # claim against the index instead of trusting its own bookkeeping.
    state = json.loads((tmp_path / "github_state.json").read_text())
    assert state["last_digest_index"]["index_state"] == index_state
    assert state["last_digest_index"]["cognee_data_ids"] == ["data-id-1"]


def _one_issue() -> list[dict[str, Any]]:
    return [
        {
            "id": "issue-1",
            "identifier": "ENG-1",
            "title": "Ship it",
            "description": "body",
            "url": "https://linear.app/acme/issue/ENG-1",
            "priority": 2,
            "updatedAt": "2026-06-25T10:00:00Z",
            "state": {"name": "In Progress", "type": "started"},
            "team": {"key": "ENG", "name": "Engineering"},
        }
    ]


def _pending_learn() -> Any:
    async def fake_learn(self: Any, data: str, **kwargs: Any) -> Any:
        class Outcome:
            ingest = IngestResult(
                True,
                "accepted",
                str(kwargs.get("dataset") or "masumi-network"),
                (),
                index_state=INDEX_STATE_PENDING,
                cognee_data_ids=("data-id-1",),
            )

        return Outcome()

    return fake_learn


@pytest.mark.asyncio
async def test_linear_sync_reports_a_failed_coalesced_cognify(
    tmp_path: Any, monkeypatch: Any
) -> None:
    # THE LOAD-BEARING TEST for Linear. The standalone run AWAITS its one
    # coalesced cognify and swallows the exception, because the rows already
    # landed. Swallowing it is fine; reporting central_ingested:true afterwards
    # and nothing else is not — the failure was observed and then discarded.
    from kb.linear_sync import LinearSyncer
    from kb.service import Citadel
    from tests.test_linear_sync import FakeLinearClient

    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "s.json"),
        access_store_path=str(tmp_path / "a.json"),
    )
    citadel = Citadel(config)

    async def failing_cognify(*, datasets: Any, force: bool = False) -> dict[str, Any]:
        raise RuntimeError("kuzu lock held by another process")

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", _pending_learn())
    monkeypatch.setattr(citadel.cognee, "cognify", failing_cognify)

    syncer = LinearSyncer(citadel, client=FakeLinearClient(_one_issue()))
    result = await syncer.run(force=True, await_cognify=True)

    assert result["ok"] is True
    assert result["central_ingested"] is True  # the rows landed
    assert result["central_index_state"] == "cognify_failed"
    assert result["central_indexed"] is False  # and none of it is retrievable
    assert result["cognify_status"] == "failed"


@pytest.mark.asyncio
async def test_linear_sync_reports_indexed_only_after_an_observed_cognify(
    tmp_path: Any, monkeypatch: Any
) -> None:
    # The other direction: a cognify this run WATCHED succeed is the one case
    # where "indexed" is a claim about something observed.
    from kb.linear_sync import LinearSyncer
    from kb.service import Citadel
    from tests.test_linear_sync import FakeLinearClient

    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "s.json"),
        access_store_path=str(tmp_path / "a.json"),
    )
    citadel = Citadel(config)

    async def ok_cognify(*, datasets: Any, force: bool = False) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", _pending_learn())
    monkeypatch.setattr(citadel.cognee, "cognify", ok_cognify)

    syncer = LinearSyncer(citadel, client=FakeLinearClient(_one_issue()))
    result = await syncer.run(force=True, await_cognify=True)

    assert result["cognify_status"] == "ok"
    assert result["central_index_state"] == INDEX_STATE_INDEXED
    assert result["central_indexed"] is True


@pytest.mark.asyncio
async def test_repo_content_sync_counts_unindexed_files_separately(
    tmp_path: Any, monkeypatch: Any
) -> None:
    # repo_content_sync already refuses to treat a file as `unchanged` without
    # the ids cognee assigned, which repairs the SKIP decision. Its own
    # files_ingested counter still counted a request. Split the counters so a
    # pass that stored two files and indexed none says so.
    from kb.repo_content_sync import RepoContentSyncer
    from tests.test_repo_content_sync import (
        FakeCitadel,
        FakeLearningProcess,
        FakeRepoContentClient,
    )

    config = CitadelConfig(
        github_org="masumi-network",
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_state_path=str(tmp_path / "repo_state.json"),
        repo_content_sync_dataset="masumi-network",
    )
    learning = FakeLearningProcess()
    original_learn = learning.learn

    async def learn_without_index(data: str, **kwargs: Any) -> Any:
        outcome = await original_learn(data, **kwargs)
        ingest = outcome.ingest
        outcome.ingest = IngestResult(  # type: ignore[misc]
            ingest.accepted,
            ingest.reason,
            ingest.dataset,
            ingest.tags,
            ingest.cognee_result,
            index_state=INDEX_STATE_NOT_SCHEDULED,
            cognee_data_ids=ingest.cognee_data_ids,
        )
        return outcome

    monkeypatch.setattr(learning, "learn", learn_without_index)
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=config.repo_content_sync_state_path,
        learning=learning,  # type: ignore[arg-type]
    )

    result = await syncer.run()

    assert result["files_ingested"] == 2  # stored
    assert result["files_indexed"] == 0  # and retrievable: none of them
    assert result["files_index_failed"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index_state", "expected_status", "expects_graph_edge"),
    [
        (INDEX_STATE_INDEXED, "indexed", True),
        (INDEX_STATE_PENDING, "pending", False),
        (INDEX_STATE_NOT_SCHEDULED, "unindexed", False),
        (INDEX_STATE_UNKNOWN, "pending", False),
    ],
)
async def test_mesh_document_status_follows_the_observed_index_state(
    index_state: str, expected_status: str, expects_graph_edge: bool
) -> None:
    # The mesh drew every accepted write as status "indexed" with an edge to
    # index:graph — a picture of a graph write that had not happened, and in
    # the 892-document case would never happen. The badge and the edge are
    # claims about the graph, so both wait for the observation.
    from kb.mesh import MeshState

    config = CitadelConfig(tenant_id="test", default_dataset="notes")
    mesh = MeshState()
    result = IngestResult(True, "accepted", "notes", ("ops",), index_state=index_state)

    await mesh.record_ingest(config, result, data="Runbook", dataset="notes", tags=["ops"])
    snapshot = await mesh.snapshot(config)

    document = next(node for node in snapshot["nodes"] if node["type"] == "document")
    assert document["status"] == expected_status
    graph_edges = [
        edge
        for edge in snapshot["edges"]
        if edge["target"] == "index:graph" and edge["source"] == document["id"]
    ]
    assert bool(graph_edges) is expects_graph_edge
    assert snapshot["events"][0]["details"]["index_state"] == index_state
