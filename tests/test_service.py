from __future__ import annotations

import json
from typing import Any

import pytest

from kb.config import CitadelConfig
from kb.models import FeedbackRequest
from kb.security_scan import SecretContentError
from kb.service import MAX_SEARCH_TOP_K, Citadel
import kb.service as service


class FakeCognee:
    def __init__(self) -> None:
        self.remember_calls: list[dict[str, Any]] = []
        self.feedback_calls: list[dict[str, Any]] = []
        self.improve_calls: list[dict[str, Any]] = []
        self.cognify_calls: list[dict[str, Any]] = []
        self.nodes: list[Any] = []
        self.edges: list[Any] = []
        self._pending: list[Any] = []

    async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.remember_calls.append({"data": data, **kwargs})
        # Cognee.add stores data, but it only enters the graph once cognify
        # runs — the modern remember path does not cognify inline.
        self._pending.append(data)
        return {"ok": True}

    async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"query": query, **kwargs}]

    async def add_feedback(self, **kwargs: Any) -> bool:
        self.feedback_calls.append(kwargs)
        return True

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        self.improve_calls.append(kwargs)
        return {"improved": True}

    async def cognify(self, **kwargs: Any) -> dict[str, Any]:
        self.cognify_calls.append(kwargs)
        # Cognify turns added-but-uncognified data into graph nodes.
        self.nodes.extend(self._pending)
        self._pending.clear()
        return {"cognified": True}

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        return list(self.nodes), list(self.edges)


class EmptyCognee(FakeCognee):
    async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_ingest_applies_tags_and_dataset() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes", default_tags=("personal",)), cognee=fake)

    result = await kb.ingest("A useful note", tags=["AI"])

    assert result.accepted
    assert result.tags == ("personal", "ai")
    assert fake.remember_calls[0]["dataset_name"] == "notes"
    assert fake.remember_calls[0]["tags"] == ("personal", "ai")


@pytest.mark.asyncio
async def test_ingest_blocks_high_severity_secret() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    with pytest.raises(SecretContentError) as exc_info:
        await kb.ingest("AWS key AKIAIOSFODNN7EXAMPLE leaked here")

    error = exc_info.value
    assert error.highest_severity in {"critical", "high"}
    assert error.findings  # carries redacted finding metadata
    # The raw secret must never appear in what we surface to callers.
    assert "AKIAIOSFODNN7EXAMPLE" not in error.public_message
    # Nothing reached the vault.
    assert fake.remember_calls == []


@pytest.mark.asyncio
async def test_ingest_allows_clean_content() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = await kb.ingest("A perfectly ordinary engineering note about caching.")

    assert result.accepted
    assert len(fake.remember_calls) == 1


@pytest.mark.asyncio
async def test_ingest_scan_can_be_disabled() -> None:
    fake = FakeCognee()
    kb = Citadel(
        CitadelConfig(default_dataset="notes", content_scan_enabled=False),
        cognee=fake,
    )

    result = await kb.ingest("AWS key AKIAIOSFODNN7EXAMPLE leaked here")

    assert result.accepted
    assert len(fake.remember_calls) == 1


@pytest.mark.asyncio
async def test_ingest_rejects_duplicate_in_process() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(), cognee=fake)

    first = await kb.ingest("same note")
    second = await kb.ingest("same note")

    assert first.accepted
    assert not second.accepted
    assert second.reason == "duplicate_in_process"
    assert len(fake.remember_calls) == 1


@pytest.mark.asyncio
async def test_ingest_allows_same_content_to_different_datasets() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(), cognee=fake)

    node = await kb.ingest("shared trace body", dataset="seat:alice")
    shared = await kb.ingest("shared trace body", dataset="session-traces")

    assert node.accepted
    assert shared.accepted
    assert len(fake.remember_calls) == 2
    assert fake.remember_calls[0]["dataset_name"] == "seat:alice"
    assert fake.remember_calls[1]["dataset_name"] == "session-traces"


@pytest.mark.asyncio
async def test_search_uses_github_sync_session_for_github_dataset() -> None:
    fake = FakeCognee()
    kb = Citadel(
        CitadelConfig(
            github_sync_dataset="masumi-network",
            github_sync_session="masumi-github-daily",
        ),
        cognee=fake,
    )

    result = await kb.search("weekly updates", dataset="masumi-network")

    assert result[0]["session_id"] == "masumi-github-daily"


@pytest.mark.asyncio
async def test_search_falls_back_to_persisted_github_digest(tmp_path: Any) -> None:
    state_path = tmp_path / "github_state.json"
    state_path.write_text(
        json.dumps(
            {
                "org": "masumi-network",
                "last_checked_at": "2026-06-01T14:27:10Z",
                "last_digest_at": "2026-06-01T14:27:10Z",
                "last_digest": (
                    "# masumi-network GitHub daily update\n\n"
                    "New commits observed: 1\n\n"
                    "## Recent commits\n"
                    "- 2026-06-01T13:15:28Z: mrgrauel committed 434cec44e6af "
                    "to masumi-network/sokosumi: organization seat assignment."
                ),
            }
        ),
        encoding="utf-8",
    )
    kb = Citadel(
        CitadelConfig(
            github_sync_dataset="masumi-network",
            github_sync_session="masumi-github-daily",
            github_sync_state_path=str(state_path),
        ),
        cognee=EmptyCognee(),
    )

    result = await kb.search("what were the new updates all week in the org", dataset="masumi-network")

    assert result[0]["source"] == "github_sync_state"
    assert result[0]["metadata"]["org"] == "masumi-network"
    assert any("organization seat assignment" in item["content"] for item in result)


@pytest.mark.asyncio
async def test_cognify_dataset_reports_graph_growth() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset()

    assert result["ok"]
    assert result["dataset"] == "masumi-network"
    assert result["verify"] is False
    assert fake.cognify_calls == [{"datasets": ["masumi-network"], "force": False}]
    assert result["graph_before"] == {"nodes": 0, "edges": 0}


@pytest.mark.asyncio
async def test_cognify_dataset_verify_ingests_marker_and_confirms_hit() -> None:
    class RecallingCognee(FakeCognee):
        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"content": query}]

    fake = RecallingCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset(verify=True)

    assert result["verify"] is True
    marker = fake.remember_calls[0]["data"]
    assert marker.startswith("COGNIFY_TEST_MARKER_")
    assert result["verification"]["search_hit"] is True
    assert result["verification"]["graph_grew"] is True
    assert result["verification"]["ok"] is True
    assert result["ok"] is True
    # verify is a superset: recovery cognify + an explicit cognify of the marker
    # (remember does not cognify inline on the modern Cognee path).
    assert fake.cognify_calls == [
        {"datasets": ["masumi-network"], "force": False},
        {"datasets": ["masumi-network"], "force": False},
    ]


@pytest.mark.asyncio
async def test_cognify_dataset_verify_failure_propagates_top_level_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed verify canary must set top-level ok=False (CLI exit code)."""
    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)

    class StuckCognee(FakeCognee):
        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return []  # the marker is never retrievable

        async def cognify(self, **kwargs: Any) -> dict[str, Any]:
            self.cognify_calls.append(kwargs)
            return {"cognified": True}  # ...and the graph never grows

    fake = StuckCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset(verify=True)

    assert result["verification"]["ok"] is False
    assert result["verification"]["search_attempts"] == service.CANARY_SEARCH_ATTEMPTS
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_cognify_canary_is_not_ok_on_graph_growth_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#114: a growing graph is not evidence the marker became retrievable.

    Production ran "grew=True canary_ok=True" every hour while github_sync and
    linear_sync were dead on the Kuzu lock. Any concurrent ingest grows the
    graph, so growth must stay diagnostic detail and never a pass condition.
    """
    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)

    class GrowsButUnsearchable(FakeCognee):
        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return []  # never retrievable...

    fake = GrowsButUnsearchable()  # ...but FakeCognee's graph still grows
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset(verify=True)

    assert result["verification"]["graph_grew"] is True
    assert result["verification"]["search_hit"] is False
    assert result["verification"]["ok"] is False
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_cognify_canary_accepts_a_late_search_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cognify settles in the background, so a bounded retry must be allowed."""
    monkeypatch.setattr(service, "CANARY_SEARCH_BACKOFF_SECONDS", 0)

    class SlowCognee(FakeCognee):
        def __init__(self) -> None:
            super().__init__()
            self.recall_calls = 0

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            self.recall_calls += 1
            # Miss once, then settle — a healthy node that is merely slow.
            return [] if self.recall_calls < 2 else [{"content": query}]

    fake = SlowCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    result = await kb.cognify_dataset(verify=True)

    assert result["verification"]["search_hit"] is True
    assert result["verification"]["search_attempts"] == 2
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_search_clamps_top_k_to_safe_bounds() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    huge = await kb.search("anything", top_k=100_000)
    negative = await kb.search("anything", top_k=-1)

    assert huge[0]["top_k"] == MAX_SEARCH_TOP_K
    assert negative[0]["top_k"] == 1


@pytest.mark.asyncio
async def test_cognify_dataset_force_passes_incremental_loading_false() -> None:
    """force=True must propagate as incremental_loading=False so Cognee reprocesses
    a dataset it has marked "already processed" (the empty-graph recovery case)."""
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(default_dataset="masumi-network"), cognee=fake)

    await kb.cognify_dataset(force=True)

    assert fake.cognify_calls == [{"datasets": ["masumi-network"], "force": True}]


@pytest.mark.asyncio
async def test_feedback_can_auto_improve() -> None:
    fake = FakeCognee()
    kb = Citadel(CitadelConfig(auto_improve=True), cognee=fake)

    result = await kb.feedback(FeedbackRequest(qa_id="qa-1", score=1, text="useful"))

    assert result.recorded
    assert result.improved
    assert fake.feedback_calls[0]["qa_id"] == "qa-1"
    assert fake.improve_calls[0]["session_ids"] == ["personal-session"]


class _SessionMissCognee(FakeCognee):
    """add_feedback finds no matching qa_id in the session cache (post-#54 norm)."""

    async def add_feedback(self, **kwargs: Any) -> bool:
        self.feedback_calls.append(kwargs)
        return False


@pytest.mark.asyncio
async def test_feedback_falls_back_to_durable_write_when_session_cache_misses() -> None:
    # #40: a session-cache miss must not be a silent no-op — persist durably.
    fake = _SessionMissCognee()
    kb = Citadel(CitadelConfig(), cognee=fake)

    result = await kb.feedback(FeedbackRequest(qa_id="qa-9", score=-1, text="wrong answer"))

    assert result.recorded is True
    assert result.ok is True
    assert result.reason is None
    note = fake.remember_calls[-1]
    assert "qa-9" in note["data"]
    assert "wrong answer" in note["data"]
    assert "feedback" in note["tags"]
    assert "qa:qa-9" in note["tags"]


@pytest.mark.asyncio
async def test_feedback_reports_reason_when_not_recorded() -> None:
    # #40: when even the durable write is rejected, report ok:False + a reason
    # (so the CLI exits nonzero) instead of recorded:false, exit 0.
    fake = _SessionMissCognee()
    kb = Citadel(CitadelConfig(min_chars=100_000), cognee=fake)  # forces filter rejection

    result = await kb.feedback(FeedbackRequest(qa_id="qa-9", score=0))

    assert result.recorded is False
    assert result.ok is False
    assert result.reason is not None and "not recorded" in result.reason


def test_legacy_garbage_kind_classifies_safely() -> None:
    # #15: the classifier must purge only well-identified garbage and NEVER real
    # content — this is the safety gate for a destructive admin operation.
    from kb.service import _legacy_garbage_kind

    hex32 = "a" * 32
    assert _legacy_garbage_kind("n1", {"text": f"COGNIFY_TEST_MARKER_{hex32}"}) == "marker"
    assert _legacy_garbage_kind("n2", {"text": "[DataItem]"}) == "dataitem"
    assert (
        _legacy_garbage_kind("n3", {"text": "Session ID: x\n\nQuestion: \n\nAnswer: [DataItem]"})
        == "dataitem"
    )
    assert _legacy_garbage_kind("n4", {"type": "user_sessions_from_cache"}) == "session_cache"

    # SAFETY — real content is never classified:
    assert _legacy_garbage_kind("r1", {"text": "We fixed the [DataItem] bug in #26."}) is None
    assert _legacy_garbage_kind("r2", {"text": "COGNIFY_TEST_MARKER is a concept."}) is None
    assert _legacy_garbage_kind("r3", {"text": "Question: how?\n\nAnswer: hold a lock"}) is None
    assert _legacy_garbage_kind("r4", {"text": "A genuine project decision."}) is None
    assert _legacy_garbage_kind("r5", {"type": "TextSummary"}) is None


class _GraphGateway(FakeCognee):
    def __init__(self, graph_nodes: list[Any]) -> None:
        super().__init__()
        self._graph_nodes = graph_nodes
        self.deleted: list[str] = []

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        return self._graph_nodes, []

    async def delete_graph_nodes(self, node_ids: list[str]) -> int:
        self.deleted.extend(node_ids)
        return len(node_ids)


@pytest.mark.asyncio
async def test_cleanup_legacy_nodes_dry_run_then_delete() -> None:
    nodes = [
        ("g1", {"text": "COGNIFY_TEST_MARKER_" + "b" * 32}),
        ("g2", {"text": "[DataItem]"}),
        ("real1", {"text": "A genuine project decision."}),
    ]
    gw = _GraphGateway(nodes)
    kb = Citadel(CitadelConfig(), cognee=gw)

    dry = await kb.cleanup_legacy_nodes(dry_run=True)
    assert dry["dry_run"] is True
    assert dry["deleted"] == 0
    assert gw.deleted == []  # dry run deletes nothing
    assert {c["id"] for c in dry["candidates"]} == {"g1", "g2"}
    assert dry["counts_by_kind"] == {"marker": 1, "dataitem": 1}

    res = await kb.cleanup_legacy_nodes(dry_run=False)
    assert res["deleted"] == 2
    assert set(gw.deleted) == {"g1", "g2"}  # real1 is never deleted


@pytest.mark.asyncio
async def test_cleanup_sweeps_orphaned_vector_chunks() -> None:
    # #15: garbage cognified into the chunk vector store is found via the search
    # sweep even when its graph node is already gone (graph is empty here).
    class SweepGateway(_GraphGateway):
        def __init__(self) -> None:
            super().__init__([])  # empty graph

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {"id": "chunk-1", "text": "Session ID: x\n\nQuestion: \n\nAnswer: [DataItem]"},
                {"id": "real-1", "text": "A genuine note about payments."},
            ]

    gw = SweepGateway()
    kb = Citadel(CitadelConfig(), cognee=gw)

    res = await kb.cleanup_legacy_nodes(dry_run=False)
    assert "chunk-1" in gw.deleted  # garbage chunk purged via the sweep
    assert "real-1" not in gw.deleted  # real content untouched
    assert res["counts_by_kind"].get("dataitem", 0) >= 1


@pytest.mark.asyncio
async def test_cognify_verify_deletes_its_marker_node() -> None:
    # #15 backprop: the verify canary must not leave a marker node behind.
    class MarkerGateway(_GraphGateway):
        def __init__(self) -> None:
            super().__init__([])

        async def graph_data(self) -> tuple[list[Any], list[Any]]:
            return [(f"node-{i}", {"text": text}) for i, text in enumerate(self.nodes)], []

        async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"text": query}]

    gw = MarkerGateway()
    kb = Citadel(CitadelConfig(), cognee=gw)

    await kb.cognify_dataset(verify=True)
    assert gw.deleted, "the cognify verify marker node should be deleted"


@pytest.mark.asyncio
async def test_improve_short_circuits_on_empty_graph() -> None:
    # #41: an empty graph yields a clean no-op, not a raw EntityNotFoundError.
    fake = FakeCognee()  # nodes/edges empty
    kb = Citadel(CitadelConfig(), cognee=fake)

    result = await kb.improve()

    assert result["ok"] is True
    assert result["skipped"] == "empty_graph"
    assert fake.improve_calls == []


@pytest.mark.asyncio
async def test_improve_runs_when_graph_has_data() -> None:
    fake = FakeCognee()
    fake.nodes = ["n1"]
    kb = Citadel(CitadelConfig(), cognee=fake)

    await kb.improve()

    assert fake.improve_calls, "cognee.improve should run on a non-empty graph"
