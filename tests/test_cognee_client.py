from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from kb import chunk_window
from kb.cognify_queue import CognifyRetryQueue
from kb.cognee_client import CogneePublicClient


COGNEE_ENV_KEYS = (
    "DB_PROVIDER",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USERNAME",
    "DB_PASSWORD",
    "VECTOR_DB_HOST",
    "VECTOR_DB_PORT",
    "VECTOR_DB_NAME",
    "VECTOR_DB_USERNAME",
    "VECTOR_DB_PASSWORD",
    "GRAPH_DATABASE_HOST",
    "GRAPH_DATABASE_PORT",
    "GRAPH_DATABASE_NAME",
    "GRAPH_DATABASE_USERNAME",
    "GRAPH_DATABASE_PASSWORD",
)


@pytest.fixture(autouse=True)
def clean_derived_cognee_env(monkeypatch: Any) -> None:
    for key in COGNEE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.asyncio
async def test_cognee_public_client_runs_migrations_once(monkeypatch: Any) -> None:
    calls: list[str] = []

    async def run_migrations() -> None:
        calls.append("migrate")

    async def add(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def recall(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"ok": True}]

    monkeypatch.setenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "true")  # add-only, no bg task
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=run_migrations,
            add=add,
            recall=recall,
        ),
    )
    client = CogneePublicClient()

    await client.remember("note", dataset_name="notes")
    await client.recall("note", dataset="notes", allow_generative=True)

    assert calls == ["migrate"]


@pytest.mark.asyncio
async def test_cognee_public_client_fails_closed_when_migrations_fail(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []

    async def run_migrations() -> None:
        calls.append("migrate")
        raise RuntimeError("unknown migration revision")

    async def add(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "true")  # add-only, no bg task
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=run_migrations,
            add=add,
        ),
    )
    client = CogneePublicClient()

    async def create_database() -> None:
        calls.append("create")

    monkeypatch.setattr(client, "_create_cognee_database", create_database)

    with pytest.raises(RuntimeError, match="unknown migration revision"):
        await client.remember("note", dataset_name="notes")

    assert calls == ["migrate"]


@pytest.mark.asyncio
async def test_cognee_public_client_rejects_reported_migration_failures(
    monkeypatch: Any,
) -> None:
    async def run_migrations() -> list[str]:
        return ["cognee_db"]

    async def add(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("add must not run after a migration failure")

    monkeypatch.setenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "true")
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, add=add),
    )

    with pytest.raises(RuntimeError, match="migrations failed for 1 database"):
        await CogneePublicClient().remember("note", dataset_name="notes")


@pytest.mark.asyncio
async def test_cognee_public_client_does_not_pass_external_metadata_keyword(
    monkeypatch: Any,
) -> None:
    received: dict[str, Any] = {}

    async def run_migrations() -> None:
        return None

    async def add(*args: Any, **kwargs: Any) -> dict[str, Any]:
        received["args"] = args
        received["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "true")  # add-only, no bg task
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=run_migrations,
            add=add,
        ),
    )
    client = CogneePublicClient()

    await client.remember("note", dataset_name="notes", tags=("github", "daily-sync"))

    # metadata rides in the DataItem, never as an add() keyword (external_metadata
    # is rejected by cognee.add); only dataset_name is passed.
    assert received["kwargs"] == {"dataset_name": "notes"}


@pytest.mark.asyncio
async def test_remember_passes_explicit_lifecycle_data_id_to_cognee(
    monkeypatch: Any,
) -> None:
    from dataclasses import dataclass, field

    @dataclass
    class DataItem:
        data: Any
        label: Any = None
        external_metadata: Any = field(default=None)
        data_id: Any = None

    received: dict[str, Any] = {}
    data_id = "508228e3-bd9d-59fb-a0cb-a69362976e9d"

    async def run_migrations() -> None:
        return None

    async def add(data: Any, **kwargs: Any) -> dict[str, Any]:
        received["data"] = data
        received["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "true")
    for parent in ("cognee.tasks", "cognee.tasks.ingestion"):
        monkeypatch.setitem(sys.modules, parent, SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "cognee.tasks.ingestion.data_item",
        SimpleNamespace(DataItem=DataItem),
    )
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, add=add),
    )

    await CogneePublicClient().remember(
        "retained lifecycle source",
        dataset_name="central",
        data_id=data_id,
        defer_cognify=True,
    )

    assert received["data"].data == "retained lifecycle source"
    assert received["data"].data_id == UUID(data_id)
    assert received["kwargs"] == {"dataset_name": "central"}


@pytest.mark.asyncio
async def test_cognify_raises_without_llm_key(monkeypatch: Any) -> None:
    """cognify must fail loud (not false-green) when no LLM key is configured."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    client = CogneePublicClient()

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        await client.cognify(datasets=["notes"])


@pytest.mark.asyncio
async def test_vector_project_uses_chunk_embedding_pipeline_without_llm(
    monkeypatch: Any,
) -> None:
    import cognee

    captured: list[dict[str, Any]] = []

    async def run_custom_pipeline(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"status": "completed"}

    async def ensure_ready(_cognee: Any) -> None:
        return None

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)
    monkeypatch.setattr(chunk_window, "require_bpe_encoding", lambda: None)
    monkeypatch.setattr(cognee, "run_custom_pipeline", run_custom_pipeline)

    result = await client.vector_project(datasets=["notes"], force=False)

    assert result == [{"status": "completed"}]
    assert len(captured) == 1
    assert captured[0]["skip_connection_test"] is True
    assert captured[0]["incremental_loading"] is True
    assert captured[0]["data_cache"] is True
    assert [task.executable.__name__ for task in captured[0]["tasks"]] == [
        "classify_documents",
        "extract_chunks_from_documents",
        "index_data_points",
    ]


@pytest.mark.asyncio
async def test_vector_project_limits_pipeline_to_requested_document_ids(
    monkeypatch: Any,
) -> None:
    import cognee

    captured: list[dict[str, Any]] = []
    selected = object()
    document_id = "508228e3-bd9d-59fb-a0cb-a69362976e9d"

    async def run_custom_pipeline(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"status": "completed"}

    async def ensure_ready(_cognee: Any) -> None:
        return None

    async def vector_projection_data(**kwargs: Any) -> list[Any]:
        assert kwargs == {"dataset": "notes", "document_ids": [document_id]}
        return [selected]

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)
    monkeypatch.setattr(client, "_vector_projection_data", vector_projection_data)
    monkeypatch.setattr(chunk_window, "require_bpe_encoding", lambda: None)
    monkeypatch.setattr(cognee, "run_custom_pipeline", run_custom_pipeline)

    await client.vector_project(
        datasets=["notes"],
        document_ids=[document_id],
    )

    assert captured[0]["data"] == [selected]
    assert captured[0]["incremental_loading"] is False
    assert captured[0]["data_cache"] is False


@pytest.mark.asyncio
async def test_vector_projection_data_reads_authorized_rows_in_requested_order(
    monkeypatch: Any,
) -> None:
    import cognee.infrastructure.databases.relational as relational
    import cognee.modules.users.methods as user_methods

    first_id = UUID("508228e3-bd9d-59fb-a0cb-a69362976e9d")
    second_id = UUID("608228e3-bd9d-59fb-a0cb-a69362976e9d")
    first = SimpleNamespace(id=first_id)
    second = SimpleNamespace(id=second_id)

    class FakeResult:
        def scalars(self) -> "FakeResult":
            return self

        def all(self) -> list[Any]:
            return [first, second]

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def execute(self, _query: Any) -> FakeResult:
            return FakeResult()

    class FakeEngine:
        def get_async_session(self) -> FakeSession:
            return FakeSession()

    user = SimpleNamespace(
        id=UUID("708228e3-bd9d-59fb-a0cb-a69362976e9d"),
        tenant_id=UUID("808228e3-bd9d-59fb-a0cb-a69362976e9d"),
    )
    monkeypatch.setattr(relational, "get_relational_engine", lambda: FakeEngine())

    async def get_default_user() -> Any:
        return user

    monkeypatch.setattr(user_methods, "get_default_user", get_default_user)

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def ensure_ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)

    selected = await client._vector_projection_data(
        dataset="notes",
        document_ids=[str(second_id), str(first_id)],
    )

    assert selected == [second, first]


@pytest.mark.asyncio
async def test_cognify_checks_tokenizer_before_backend_write(monkeypatch: Any) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    def unavailable() -> Any:
        raise chunk_window.ChunkBudgetValidationError("tokenizer unavailable")

    monkeypatch.setattr(
        chunk_window,
        "require_bpe_encoding",
        unavailable,
    )
    called = False

    async def cognify(**_: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace(cognify=cognify))

    with pytest.raises(chunk_window.ChunkBudgetValidationError, match="tokenizer"):
        await client.cognify(datasets=["notes"])

    assert called is False


@pytest.mark.asyncio
async def test_cognify_invalidates_cached_graph_snapshot(monkeypatch: Any) -> None:
    """Graph counts after cognify must observe the completed write."""
    client = CogneePublicClient()
    reads = 0

    async def read_graph() -> tuple[list[Any], list[Any]]:
        nonlocal reads
        reads += 1
        return ([(f"node-{reads}", {})], [])

    async def run_migrations() -> None:
        return None

    async def cognify(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {
            "datasets": ["notes"],
            "incremental_loading": True,
            "data_cache": True,
        }
        return {"ok": True}

    async def ensure_ready(_: Any) -> None:
        return None

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(client, "_read_graph_data", read_graph)
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=run_migrations,
            cognify=cognify,
        ),
    )

    await client.graph_data()
    await client.graph_data()
    assert reads == 1

    await client.cognify(datasets=["notes"])
    await client.graph_data()

    assert reads == 2


@pytest.mark.asyncio
async def test_lifecycle_cognify_does_not_change_provider_route_on_failure(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()
    client.lifecycle_projection_state_lookup = lambda _: (set(), set())
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("CITADEL_EMBEDDING_PROFILE", raising=False)
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def ensure_ready(_: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)

    async def cognify(**_: Any) -> dict[str, Any]:
        raise RuntimeError("embedding provider quota")

    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace(cognify=cognify))

    with pytest.raises(RuntimeError, match="new generation"):
        await client.cognify(datasets=["notes"])

    assert os.getenv("CITADEL_EMBEDDING_PROFILE") is None


@pytest.mark.asyncio
async def test_graph_data_merges_every_provisioned_dataset_store(
    monkeypatch: Any,
) -> None:
    """Org-wide totals come from the per-dataset loop, never one ambient store.

    Under backend access control each dataset lives in its own graph database
    and cognee leaves the LAST entered dataset context set after use. Pre-fix,
    ``_read_graph_data`` resolved that ambient engine, so /api/mesh/graph
    ``total_nodes``/``total_edges`` and the corpus-health ``indexed_docs``/
    ``indexed_edges`` published whichever store a prior operation leaked into
    the refilling task: 43,732 total nodes after a cognify refill vs 21 after a
    corpus-health census refill, live on 2026-08-13, on the endpoint the app
    graph page renders (kb/static/app.js /api/mesh/graph). ADR-0020: the
    org-wide read loops the provisioned dataset stores and merges, deduping
    nodes by id and edges by (source, target, relationship).
    """
    client = CogneePublicClient()
    owner = uuid4()
    dataset_a, dataset_b = sorted((uuid4(), uuid4()), key=str)
    stores: dict[UUID, tuple[list[Any], list[Any]]] = {
        dataset_a: (
            [("doc-a", {"name": "a"}), ("shared", {"name": "s"})],
            [("doc-a", "shared", "is_part_of", {})],
        ),
        dataset_b: (
            [("doc-b", {"name": "b"}), ("shared", {"name": "s"})],
            [
                ("doc-b", "shared", "is_part_of", {}),
                ("doc-a", "shared", "is_part_of", {}),
            ],
        ),
    }
    read_order: list[UUID] = []

    async def ensure_ready(_: Any) -> None:
        return None

    async def provisioned() -> list[tuple[UUID, UUID]]:
        return [(dataset_a, owner), (dataset_b, owner)]

    async def read_for_dataset(
        dataset_id: UUID, owner_id: UUID
    ) -> tuple[list[Any], list[Any]]:
        assert owner_id == owner
        read_order.append(dataset_id)
        scoped_nodes, scoped_edges = stores[dataset_id]
        return list(scoped_nodes), list(scoped_edges)

    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)
    monkeypatch.setattr(client, "_provisioned_dataset_databases", provisioned)
    monkeypatch.setattr(client, "_read_graph_data_for_dataset", read_for_dataset)
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace())

    nodes, edges = await client.graph_data()

    assert read_order == [dataset_a, dataset_b]
    # The mirrored "shared" node dedupes by id; the cross-store duplicate edge
    # dedupes by (source, target, relationship).
    assert sorted(str(node_id) for node_id, _ in nodes) == ["doc-a", "doc-b", "shared"]
    assert sorted((str(s), str(t)) for s, t, *_ in edges) == [
        ("doc-a", "shared"),
        ("doc-b", "shared"),
    ]


@pytest.mark.asyncio
async def test_graph_data_without_provisioned_stores_reads_current_context(
    monkeypatch: Any,
) -> None:
    """No dataset_database rows (access control off, local fakes): the single
    ambient-context read stands, byte-for-byte."""
    client = CogneePublicClient()

    async def ensure_ready(_: Any) -> None:
        return None

    async def provisioned() -> list[tuple[UUID, UUID]]:
        return []

    async def current_context() -> tuple[list[Any], list[Any]]:
        return ([("only", {})], [])

    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)
    monkeypatch.setattr(client, "_provisioned_dataset_databases", provisioned)
    monkeypatch.setattr(client, "_read_graph_data_current_context", current_context)
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace())

    nodes, edges = await client.graph_data()

    assert nodes == [("only", {})]
    assert edges == []


@pytest.mark.asyncio
async def test_graph_data_skips_failing_store_and_keeps_healthy_totals(
    monkeypatch: Any,
) -> None:
    """One broken store must not blank the org-wide mesh graph.

    A Kuzu lock timeout (or a store read racing a dataset delete) on ONE
    dataset previously propagated out of the merge loop, so knowledge_mesh
    served ``fallback: true`` — an empty canvas for every caller on every
    cache refill. The loop skips the failing store (logging dataset id and
    exception class only; ``dataset_database`` rows carry store credentials
    in other columns) and merges the healthy ones. Only when EVERY
    provisioned store fails does the read raise, keeping the honest fallback
    instead of presenting a dead graph layer as an empty vault (ADR-0020).
    """
    client = CogneePublicClient()
    owner = uuid4()
    dataset_a, dataset_b, dataset_c = sorted((uuid4(), uuid4(), uuid4()), key=str)

    async def ensure_ready(_: Any) -> None:
        return None

    async def provisioned() -> list[tuple[UUID, UUID]]:
        return [(dataset_a, owner), (dataset_b, owner), (dataset_c, owner)]

    async def read_for_dataset(
        dataset_id: UUID, owner_id: UUID
    ) -> tuple[list[Any], list[Any]]:
        if dataset_id == dataset_b:
            raise RuntimeError("store lock timeout")
        marker = "a" if dataset_id == dataset_a else "c"
        return (
            [(f"doc-{marker}", {})],
            [(f"doc-{marker}", "hub", "is_part_of", {})],
        )

    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)
    monkeypatch.setattr(client, "_provisioned_dataset_databases", provisioned)
    monkeypatch.setattr(client, "_read_graph_data_for_dataset", read_for_dataset)
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace())

    nodes, edges = await client.graph_data()

    assert sorted(str(node_id) for node_id, _ in nodes) == ["doc-a", "doc-c"]
    assert sorted((str(s), str(t)) for s, t, *_ in edges) == [
        ("doc-a", "hub"),
        ("doc-c", "hub"),
    ]

    async def every_store_fails(
        dataset_id: UUID, owner_id: UUID
    ) -> tuple[list[Any], list[Any]]:
        raise RuntimeError("all stores down")

    monkeypatch.setattr(client, "_read_graph_data_for_dataset", every_store_fails)
    client._graph_data_cache = None
    with pytest.raises(RuntimeError, match="all stores down"):
        await client.graph_data()


@pytest.mark.asyncio
async def test_per_dataset_graph_read_restores_ambient_context(
    monkeypatch: Any,
) -> None:
    """The org-wide loop must not leave the task pointed at the last store.

    cognee intentionally persists a dataset context after ``async with`` exit;
    ``cognify_dataset`` relies on that to delete its verify marker from the
    just-cognified store. The per-dataset read therefore puts the prior
    graph/vector/file-storage configs back after reading.
    """
    from cognee import context_global_variables as context_module

    client = CogneePublicClient()
    sentinel = {"graph": "prior"}
    prior_vector = context_module.vector_db_config.get(None)
    token = context_module.graph_db_config.set(sentinel)
    try:

        class FakeContext:
            def __init__(self, dataset_id: UUID, owner_id: UUID) -> None:
                pass

            async def __aenter__(self) -> "FakeContext":
                # Mimic cognee: the entered configs persist past __aexit__.
                context_module.graph_db_config.set({"graph": "leaked"})
                context_module.vector_db_config.set({"vector": "leaked"})
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

        async def current_context() -> tuple[list[Any], list[Any]]:
            return ([], [])

        monkeypatch.setattr(
            context_module, "set_database_global_context_variables", FakeContext
        )
        monkeypatch.setattr(
            client, "_read_graph_data_current_context", current_context
        )

        await client._read_graph_data_for_dataset(uuid4(), uuid4())

        assert context_module.graph_db_config.get(None) is sentinel
        assert context_module.vector_db_config.get(None) is prior_vector
    finally:
        context_module.graph_db_config.reset(token)


class _FakeGraphEngine:
    """Minimal cognee graph engine over in-memory nodes/edges (#28 drill-down).

    Mirrors KuzuAdapter.get_node (props or None) and get_connections (incident
    edges, queried node returned as the source of each tuple), so tests exercise
    the REAL targeted-read path (_document_graph) instead of stubbing graph_data.
    """

    def __init__(
        self, nodes: list[tuple[str, dict[str, Any]]], edges: list[tuple[Any, ...]]
    ) -> None:
        self._nodes = {str(nid): dict(props or {}) for nid, props in nodes}
        self._edges = [
            (str(src), str(tgt), rel) for src, tgt, rel, *_ in edges
        ]

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        props = self._nodes.get(str(node_id))
        return dict(props) if props is not None else None

    async def get_connections(
        self, node_id: str
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        nid = str(node_id)
        rows: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for src, tgt, rel in self._edges:
            if src == nid:
                other = tgt
            elif tgt == nid:
                other = src
            else:
                continue
            source = {"id": nid, **self._nodes.get(nid, {})}
            target = {"id": other, **self._nodes.get(other, {})}
            rows.append((source, {"relationship_name": rel}, target))
        return rows


def _use_fake_engine(
    monkeypatch: Any,
    client: CogneePublicClient,
    nodes: list[tuple[str, dict[str, Any]]],
    edges: list[tuple[Any, ...]],
) -> None:
    engine = _FakeGraphEngine(nodes, edges)

    async def fake_engine() -> _FakeGraphEngine:
        return engine

    monkeypatch.setattr(client, "_graph_engine", fake_engine)


@pytest.mark.asyncio
async def test_get_document_resolves_node_text(monkeypatch: Any) -> None:
    # #28: resolve a search-hit node id to its chunk text via a TARGETED graph
    # read (get_node + get_connections), not a whole-graph scan.
    client = CogneePublicClient()
    _use_fake_engine(
        monkeypatch,
        client,
        [("node-1", {"text": "hello world", "title": "Greeting", "extra": 1})],
        [],
    )

    doc = await client.get_document("node-1")
    assert doc is not None
    assert doc["id"] == "node-1"
    assert doc["body"] == "hello world"
    assert doc["title"] == "Greeting"
    assert doc["source_type"] == "cognee"
    assert doc["metadata"] == {"title": "Greeting", "extra": 1}  # text key excluded

    assert await client.get_document("missing") is None


@pytest.mark.asyncio
async def test_get_document_returns_none_for_textless_node(monkeypatch: Any) -> None:
    client = CogneePublicClient()
    _use_fake_engine(monkeypatch, client, [("node-2", {"title": "no body here"})], [])
    assert await client.get_document("node-2") is None


@pytest.mark.asyncio
async def test_get_document_assembles_document_from_chunks(monkeypatch: Any) -> None:
    # Document nodes carry no text — body is stitched from linked DocumentChunk
    # neighbors ordered by chunk_index (not edge order); textless entity
    # neighbors are skipped and edge direction doesn't matter.
    client = CogneePublicClient()

    doc_props = {"name": "text_abc123", "type": "TextDocument"}
    nodes = [
        ("doc-1", doc_props),
        ("chunk-b", {"text": "part two", "chunk_index": 1}),
        ("chunk-a", {"text": "part one", "chunk_index": 0}),
        ("ent-1", {"name": "Entity"}),
    ]
    edges = [
        ("chunk-b", "doc-1", "is_part_of", {}),
        ("doc-1", "ent-1", "mentions", {}),
        ("chunk-a", "doc-1", "is_part_of", {}),
    ]
    _use_fake_engine(monkeypatch, client, nodes, edges)

    doc = await client.get_document("doc-1")
    assert doc is not None
    assert doc["body"] == "part one\n\npart two"  # chunk_index wins over edge order
    assert doc["title"] == "text_abc123"
    assert doc["source_type"] == "cognee"
    assert doc["chunk_count"] == 2
    assert doc["metadata"] == doc_props


@pytest.mark.asyncio
async def test_get_document_returns_none_for_textless_entity_near_chunks(
    monkeypatch: Any,
) -> None:
    # Entity nodes (name/description only, no text) sit right next to
    # text-bearing DocumentChunk nodes via contains/mentions edges. Chunk
    # assembly must only follow is_part_of edges, or selecting an entity
    # fabricates a "document" stitched from every chunk that mentions it
    # (regressing the textless-node -> None / HTTP 404 contract).
    client = CogneePublicClient()
    nodes = [
        ("ent-1", {"name": "Kuzu", "description": "graph db", "is_a": "tool"}),
        ("chunk-a", {"text": "doc A part one", "chunk_index": 0}),
        ("chunk-b", {"text": "doc B part one", "chunk_index": 0}),
    ]
    edges = [
        ("chunk-a", "ent-1", "contains", {}),
        ("chunk-b", "ent-1", "contains", {}),
    ]
    _use_fake_engine(monkeypatch, client, nodes, edges)
    assert await client.get_document("ent-1") is None


@pytest.mark.asyncio
async def test_get_document_returns_none_for_document_with_textless_neighbors(
    monkeypatch: Any,
) -> None:
    # A document node whose only neighbors carry no text resolves to None,
    # matching the existing textless-node behavior.
    client = CogneePublicClient()
    nodes = [("doc-1", {"name": "text_abc123"}), ("ent-1", {"name": "Entity"})]
    _use_fake_engine(monkeypatch, client, nodes, [("doc-1", "ent-1", "mentions", {})])
    assert await client.get_document("doc-1") is None


@pytest.mark.asyncio
async def test_get_document_falls_back_to_full_graph_when_engine_lacks_primitives(
    monkeypatch: Any,
) -> None:
    # If the graph engine cannot do a targeted read (no get_connections), the
    # drill-down must degrade to the full graph_data() read, not 404.
    client = CogneePublicClient()

    class _BareEngine:
        pass

    async def bare_engine() -> _BareEngine:
        return _BareEngine()

    called = {"graph_data": 0}

    async def fake_graph_data() -> tuple[list[Any], list[Any]]:
        called["graph_data"] += 1
        return ([("node-1", {"text": "fallback body"})], [])

    monkeypatch.setattr(client, "_graph_engine", bare_engine)
    monkeypatch.setattr(client, "graph_data", fake_graph_data)

    doc = await client.get_document("node-1")
    assert doc is not None
    assert doc["body"] == "fallback body"
    assert called["graph_data"] == 1


@pytest.mark.asyncio
async def test_improve_raises_without_llm_key(monkeypatch: Any) -> None:
    """improve must fail loud like cognify — cognee swallows the keyless error (#41)."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    client = CogneePublicClient()

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        await client.improve(dataset="notes")


@pytest.mark.asyncio
async def test_delete_graph_nodes_clears_graph_and_vector(monkeypatch: Any) -> None:
    # #15: delete_graph_nodes removes ids from BOTH the graph and the chunk vector
    # collection (search reads the vector store, so graph-only deletion isn't enough).
    from uuid import UUID

    captured: dict[str, Any] = {}

    class FakeGraphEngine:
        async def delete_nodes(self, node_ids: list[str]) -> None:
            captured["graph"] = list(node_ids)

    class FakeVectorEngine:
        async def delete_data_points(self, collection: str, ids: list[UUID]) -> None:
            captured["collection"] = collection
            captured["vector"] = list(ids)

    async def get_graph_engine() -> FakeGraphEngine:
        return FakeGraphEngine()

    def get_vector_engine() -> FakeVectorEngine:
        return FakeVectorEngine()

    async def run_migrations() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations),
    )
    monkeypatch.setitem(sys.modules, "cognee.infrastructure", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "cognee.infrastructure.databases", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.graph",
        SimpleNamespace(get_graph_engine=get_graph_engine),
    )
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.vector",
        SimpleNamespace(get_vector_engine=get_vector_engine),
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    client._graph_data_cache = (0.0, ([], []))

    async def _ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    uuid_a = "9dbe579d-eccb-51b6-9bba-13982cbaf69f"
    uuid_b = "43fdc0c1-b319-51d3-8fc2-2b670c2acc54"
    assert await client.delete_graph_nodes([uuid_a, uuid_b]) == 2
    assert captured["graph"] == [uuid_a, uuid_b]
    assert captured["collection"] == "DocumentChunk_text"
    assert captured["vector"] == [UUID(uuid_a), UUID(uuid_b)]
    assert client._graph_data_cache is None
    assert await client.delete_graph_nodes([]) == 0  # no-op


@pytest.mark.asyncio
async def test_read_node_dataset_map_joined_query_over_real_models(
    monkeypatch: Any,
) -> None:
    # The joined query maps (data_id -> [dataset names]) using the REAL cognee
    # Dataset/DatasetData models (so a version bump that moves them fails here),
    # scoped to the default user's datasets, with mirrors giving multi-dataset
    # membership. Backed by a throwaway sqlite so no cognee wiring is needed.
    from contextlib import asynccontextmanager
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import cognee.infrastructure.databases.relational as relational_module
    import cognee.modules.users.methods as users_methods
    from cognee.modules.data.models import Dataset, DatasetData

    user_id = uuid4()
    other_user = uuid4()
    tenant_id = uuid4()
    foreign_tenant_id = uuid4()
    ds_alice, ds_bob, ds_foreign = uuid4(), uuid4(), uuid4()
    doc_id, mirrored_id, foreign_id = uuid4(), uuid4(), uuid4()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Dataset.__table__.create)
        await conn.run_sync(DatasetData.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Dataset(
                id=ds_alice,
                name="seat:alice",
                owner_id=user_id,
                tenant_id=tenant_id,
            )
        )
        session.add(
            Dataset(
                id=ds_bob,
                name="seat:bob",
                owner_id=user_id,
                tenant_id=tenant_id,
            )
        )
        # A dataset owned by a different user must NOT leak into the map.
        session.add(
            Dataset(
                id=ds_foreign,
                name="seat:carol",
                owner_id=other_user,
                tenant_id=foreign_tenant_id,
            )
        )
        session.add(DatasetData(dataset_id=ds_alice, data_id=doc_id))
        session.add(DatasetData(dataset_id=ds_alice, data_id=mirrored_id))
        session.add(DatasetData(dataset_id=ds_bob, data_id=mirrored_id))
        session.add(DatasetData(dataset_id=ds_foreign, data_id=foreign_id))
        await session.commit()

    class _FakeRelEngine:
        @asynccontextmanager
        async def get_async_session(self) -> Any:
            async with maker() as session:
                yield session

    async def get_default_user() -> Any:
        return SimpleNamespace(id=user_id, tenant_id=tenant_id)

    monkeypatch.setattr(
        relational_module, "get_relational_engine", lambda: _FakeRelEngine()
    )
    monkeypatch.setattr(users_methods, "get_default_user", get_default_user)

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def _ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    mapping = await client._read_node_dataset_map()
    membership = await client.dataset_membership_for_documents(
        [str(mirrored_id), str(doc_id), str(foreign_id)]
    )
    forced_scope = await client.dataset_document_ids(["seat:alice", "seat:bob", "seat:carol"])
    await engine.dispose()

    assert mapping == {
        str(doc_id): ["seat:alice"],
        str(mirrored_id): ["seat:alice", "seat:bob"],
    }
    assert membership == {
        str(doc_id): ["seat:alice"],
        str(mirrored_id): ["seat:alice", "seat:bob"],
        str(foreign_id): ["seat:carol"],
    }
    assert forced_scope == sorted([str(doc_id), str(mirrored_id)])


@pytest.mark.asyncio
async def test_source_manifest_requires_readable_matching_raw_source(
    monkeypatch: Any, tmp_path: Any
) -> None:
    from contextlib import asynccontextmanager
    from hashlib import md5
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import cognee.infrastructure.databases.relational as relational_module
    from cognee.modules.data.models import Data

    raw_bytes = b"stored source text\n"
    raw_path = tmp_path / "source.txt"
    raw_path.write_bytes(raw_bytes)
    document_id = uuid4()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Data.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Data(
                id=document_id,
                name="source",
                raw_data_location=str(raw_path),
                content_hash="original-hash",
                raw_content_hash=md5(raw_bytes).hexdigest(),
                data_size=18,
                updated_at=None,
            )
        )
        await session.commit()

    class _FakeRelEngine:
        @asynccontextmanager
        async def get_async_session(self) -> Any:
            async with maker() as session:
                yield session

    monkeypatch.setattr(
        relational_module, "get_relational_engine", lambda: _FakeRelEngine()
    )
    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def _ready(_: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    manifest = await client.source_manifest_for_documents([str(document_id)])

    await engine.dispose()
    assert manifest == {
        str(document_id): {
            "content_hash": "original-hash",
            "data_size": 18,
            "raw_content_hash": md5(raw_bytes).hexdigest(),
            "raw_data_size": len(raw_bytes),
            "source_readable": True,
        }
    }


@pytest.mark.asyncio
async def test_zero_chunk_census_walks_pages_and_filters_datasets(monkeypatch: Any) -> None:
    client = CogneePublicClient()
    rows = [
        {"id": "doc-a", "name": "a", "created_at": "2026-01-01T00:00:00+00:00", "datasets": ["alpha"]},
        {"id": "doc-b", "name": "b", "created_at": "2026-01-02T00:00:00+00:00", "datasets": ["beta"]},
        {"id": "doc-c", "name": "c", "created_at": "2026-01-03T00:00:00+00:00", "datasets": ["alpha", "beta"]},
    ]
    page_cursors: list[tuple[str | None, str | None, int]] = []

    async def corpus_totals() -> dict[str, Any]:
        return {"documents": 3}

    async def corpus_page(**kwargs: Any) -> list[dict[str, Any]]:
        page_cursors.append(
            (kwargs["after_created_at"], kwargs["after_id"], kwargs["limit"])
        )
        return rows[:2] if kwargs["after_id"] is None else rows[2:]

    async def corpus_chunk_counts(document_ids: list[str]) -> dict[str, int]:
        return {document_id: 0 for document_id in document_ids}

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "corpus_chunk_counts", corpus_chunk_counts)

    report = await client.corpus_zero_chunk_documents(dataset="alpha", page_limit=2)

    assert report["ok"] is True
    assert report["documents_scanned"] == 3
    assert report["pages"] == 2
    assert report["zero_chunk_count"] == 2
    assert [row["id"] for row in report["zero_chunk_documents"]] == ["doc-a", "doc-c"]
    assert report["repair_datasets"] == ["alpha"]
    assert report["unassigned_zero_chunk_count"] == 0
    assert page_cursors == [
        (None, None, 2),
        ("2026-01-02T00:00:00+00:00", "doc-b", 1),
    ]


@pytest.mark.asyncio
async def test_zero_chunk_census_fails_when_vectors_are_unavailable(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()

    async def corpus_totals() -> dict[str, Any]:
        return {"documents": 1}

    async def corpus_page(**_: Any) -> list[dict[str, Any]]:
        return [{"id": "doc-a", "created_at": "2026-01-01T00:00:00+00:00"}]

    async def corpus_chunk_counts(_: list[str]) -> None:
        return None

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "corpus_chunk_counts", corpus_chunk_counts)

    with pytest.raises(RuntimeError, match="vector chunk measurement is unavailable"):
        await client.corpus_zero_chunk_documents()


def test_auto_feedback_is_off_by_default_in_cognees_own_config(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    """cognee must actually agree the gate is off, not just see our env var (#50, #105).

    This asserts through cognee's own is_auto_feedback_enabled(), which is what
    session_turn.py:379 calls, so a cognee release that renames the variable or
    changes the default fails here instead of quietly restoring an LLM call to
    every search.

    That call is awaited on the FastAPI event loop, so it blocks every other
    request while it runs. It is the leading suspect for the 25-40s /healthz
    hangs in #105, not only the per-search latency in #50.
    """
    monkeypatch.delenv("AUTO_FEEDBACK", raising=False)
    monkeypatch.delenv("VECTOR_DB_PROVIDER", raising=False)
    for key, value in {
        "SYSTEM_ROOT_DIRECTORY": tmp_path / "system",
        "DATA_ROOT_DIRECTORY": tmp_path / "data",
        "CACHE_ROOT_DIRECTORY": tmp_path / "cache",
        "COGNEE_LOGS_DIR": tmp_path / "logs",
    }.items():
        monkeypatch.setenv(key, str(value))

    from cognee.base_config import get_base_config
    from cognee.infrastructure.databases.cache.config import get_cache_config
    from cognee.infrastructure.databases.cache.get_cache_engine import create_cache_engine
    from cognee.infrastructure.databases.relational.config import get_relational_config
    from cognee.infrastructure.session.get_session_manager import get_session_manager

    cached_factories = (
        create_cache_engine,
        get_cache_config,
        get_relational_config,
        get_base_config,
    )
    for factory in cached_factories:
        factory.cache_clear()
    try:
        CogneePublicClient()._prepare_cognee_environment()

        assert os.environ["AUTO_FEEDBACK"] == "false"
        assert get_session_manager().is_auto_feedback_enabled() is False
    finally:
        for factory in cached_factories:
            factory.cache_clear()


def test_auto_feedback_stays_disabled_even_when_environment_enables_it(
    monkeypatch: Any,
) -> None:
    """User retrieval must not regain a search-time LLM through environment drift."""
    monkeypatch.setenv("AUTO_FEEDBACK", "true")
    CogneePublicClient()._prepare_cognee_environment()

    assert os.environ["AUTO_FEEDBACK"] == "false"


def test_qdrant_provider_loads_citadel_adapter(monkeypatch: Any) -> None:
    import kb.qdrant_adapter as qdrant_adapter_module

    registered: list[str] = []
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setattr(
        qdrant_adapter_module,
        "register_qdrant_adapter",
        lambda: registered.append("citadel"),
    )

    CogneePublicClient()._ensure_qdrant_adapter_registered()

    assert registered == ["citadel"]


def test_non_qdrant_provider_does_not_load_qdrant_adapter(monkeypatch: Any) -> None:
    import kb.qdrant_adapter as qdrant_adapter_module

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")

    monkeypatch.setattr(
        qdrant_adapter_module,
        "register_qdrant_adapter",
        lambda: pytest.fail("Qdrant adapter registration must not run"),
    )

    CogneePublicClient()._ensure_qdrant_adapter_registered()


@pytest.mark.asyncio
async def test_ensure_dataset_creates_a_missing_row_and_is_idempotent(
    monkeypatch: Any,
) -> None:
    """ensure_dataset provisions a seat's Dataset row exactly once (#147).

    Against the REAL cognee Dataset model on a throwaway sqlite, so a version
    bump that moves or renames the columns the existence check reads (name,
    owner_id, tenant_id) fails here rather than silently re-provisioning every
    seat on every call.

    create_authorized_dataset itself is stubbed. It is cognee's function and
    cognee guards it; what is being pinned is our decision of WHEN to call it.
    """
    from contextlib import asynccontextmanager
    from importlib import import_module
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import cognee.modules.data.methods as methods_pkg
    import cognee.modules.users.methods as users_methods
    from cognee.modules.data.models import Dataset

    user_id, tenant_id = uuid4(), uuid4()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Dataset.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            Dataset(id=uuid4(), name="seat:alice", owner_id=user_id, tenant_id=tenant_id)
        )
        # Same NAME, different tenant. Must not read as already provisioned,
        # because the read path matches on tenant too.
        session.add(
            Dataset(id=uuid4(), name="seat:carol", owner_id=user_id, tenant_id=uuid4())
        )
        await session.commit()

    class _FakeRelEngine:
        @asynccontextmanager
        async def get_async_session(self) -> Any:
            async with maker() as session:
                yield session

    async def get_default_user() -> Any:
        return SimpleNamespace(id=user_id, tenant_id=tenant_id)

    created: list[str] = []

    async def fake_create_authorized_dataset(name: str, user: Any) -> Any:
        created.append(name)
        async with maker() as session:
            session.add(
                Dataset(id=uuid4(), name=name, owner_id=user.id, tenant_id=user.tenant_id)
            )
            await session.commit()

    # get_datasets binds get_relational_engine at ITS module import, so patching
    # the relational package would not reach it.
    monkeypatch.setattr(
        import_module("cognee.modules.data.methods.get_datasets"),
        "get_relational_engine",
        lambda: _FakeRelEngine(),
    )
    monkeypatch.setattr(users_methods, "get_default_user", get_default_user)
    monkeypatch.setattr(
        methods_pkg, "create_authorized_dataset", fake_create_authorized_dataset
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def _ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    # Already provisioned for this tenant.
    assert await client.ensure_dataset("seat:alice") is False
    # Missing entirely.
    assert await client.ensure_dataset("seat:dave") is True
    # Now present, so reported as not-new, which is what a backfill counts.
    assert await client.ensure_dataset("seat:dave") is False
    # Present under a DIFFERENT tenant, so still missing for this one. This is
    # the assertion the real-cognee tests below do not cover.
    assert await client.ensure_dataset("seat:carol") is True

    await engine.dispose()
    # Provisioning runs on EVERY call, including the two that reported False.
    # The return value describes what was found, not whether we called through:
    # an early return would strand a seat whose row exists but whose ACL rows
    # are missing, which test_ensure_dataset_repairs_a_row_that_has_no_acl pins.
    assert created == ["seat:alice", "seat:dave", "seat:dave", "seat:carol"]


@pytest.fixture
def cognee_sqlite(tmp_path: Any, monkeypatch: Any) -> Any:
    """Point cognee's REAL relational engine at a throwaway sqlite.

    The caches are lru_cached module-wide, so they are cleared on the way in AND
    on the way out; skipping the second clear poisons every later test in the
    session with this tmp_path.
    """
    from cognee.base_config import get_base_config
    from cognee.infrastructure.databases.relational.config import get_relational_config
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )

    for key, value in {
        "DB_PROVIDER": "sqlite",
        "DB_NAME": "citadel_test.db",
        "SYSTEM_ROOT_DIRECTORY": str(tmp_path / "system"),
        "DATA_ROOT_DIRECTORY": str(tmp_path / "data"),
        "CACHE_ROOT_DIRECTORY": str(tmp_path / "cache"),
        "COGNEE_LOGS_DIR": str(tmp_path / "logs"),
        "DEFAULT_USER_EMAIL": "citadel_test@example.com",
        "DEFAULT_USER_PASSWORD": "citadel-test-password",
        "LLM_API_KEY": "unused-by-this-test",
    }.items():
        monkeypatch.setenv(key, value)

    def _clear() -> None:
        get_base_config.cache_clear()
        get_relational_config.cache_clear()
        create_relational_engine.cache_clear()

    _clear()
    yield
    _clear()


def _real_cognee_client(monkeypatch: Any) -> CogneePublicClient:
    client = CogneePublicClient()

    async def _ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)
    return client


@pytest.mark.asyncio
async def test_ensure_dataset_makes_a_seat_resolvable_to_cognee(
    cognee_sqlite: Any, monkeypatch: Any
) -> None:
    """The claim that matters: cognee's own read-path resolver finds the seat (#147).

    The stubbed test above pins when we call through. This one pins that the
    call works, against the real relational stack, by asserting the exact
    resolver the search path uses flips from None to the dataset.
    """
    from cognee.infrastructure.databases.relational import create_db_and_tables
    from cognee.modules.data.methods import get_authorized_dataset_by_name
    from cognee.modules.users.methods import get_default_user

    await create_db_and_tables()
    user = await get_default_user()
    client = _real_cognee_client(monkeypatch)

    assert await get_authorized_dataset_by_name("seat:alice", user, "read") is None

    assert await client.ensure_dataset("seat:alice") is True
    resolved = await get_authorized_dataset_by_name("seat:alice", user, "read")
    assert resolved is not None and resolved.name == "seat:alice"

    assert await client.ensure_dataset("seat:alice") is False


@pytest.mark.asyncio
async def test_ensure_dataset_survives_losing_a_creation_race(
    cognee_sqlite: Any, monkeypatch: Any
) -> None:
    """A concurrent provisioner must not blow up this one (#147).

    create_dataset is SELECT-then-INSERT on a deterministic uuid5 id and
    handles no IntegrityError, so two writers for the same name collide on the
    primary key. Reachable rather than theoretical: Railway runs evolve and
    linear-sync as separate OS processes against one Postgres, and linear_sync
    writes into seat:<slug>, so a boot backfill can meet an in-flight
    cognee.add. Unhandled, the first collision aborts the whole sweep and every
    later seat is silently skipped.
    """
    from cognee.infrastructure.databases.relational import create_db_and_tables
    from cognee.modules.data.methods import get_authorized_dataset_by_name
    from cognee.modules.users.methods import get_default_user

    await create_db_and_tables()
    # Warm the default user first. get_default_user CREATES it lazily, and
    # racing that collides on users.email long before dataset creation is
    # reached. That is a separate cognee-wide race on every entry point, not
    # one this change introduces, and guarding it here would only hide which
    # race the test is actually about. In the boot backfill the user is warm
    # anyway: ensure_session_traces_dataset runs first, and the loop is
    # sequential within a process.
    user = await get_default_user()
    client = _real_cognee_client(monkeypatch)

    results = await asyncio.gather(
        *(client.ensure_dataset("seat:racer") for _ in range(3)),
        return_exceptions=True,
    )

    raised = [r for r in results if isinstance(r, BaseException)]
    assert not raised, f"a lost race must not propagate: {raised}"
    # The row is there regardless of who won, which is the point of the call.
    assert await get_authorized_dataset_by_name("seat:racer", user, "read") is not None


@pytest.mark.asyncio
async def test_ensure_dataset_repairs_a_row_that_has_no_acl(
    cognee_sqlite: Any, monkeypatch: Any
) -> None:
    """The half-provisioned seat: row present, ACL rows absent (#147).

    create_authorized_dataset commits the Dataset row and THEN writes four
    permission rows in separate sessions, and give_permission_on_dataset gives
    up after three attempts, so a partial write is reachable. The read path
    filters by ACL, so such a seat is exactly as broken as one with no row, and
    an ensure_dataset that returned early on a present row could never repair
    it.
    """
    from cognee.infrastructure.databases.relational import (
        create_db_and_tables,
        get_relational_engine,
    )
    from cognee.modules.data.methods import (
        get_authorized_dataset_by_name,
        get_unique_dataset_id,
    )
    from cognee.modules.data.models import Dataset
    from cognee.modules.users.methods import get_default_user

    await create_db_and_tables()
    user = await get_default_user()

    name = "seat:halfway"
    dataset_id = await get_unique_dataset_id(dataset_name=name, user=user)
    engine = get_relational_engine()
    async with engine.get_async_session() as session:
        session.add(
            Dataset(id=dataset_id, name=name, owner_id=user.id, tenant_id=user.tenant_id)
        )
        await session.commit()

    assert await get_authorized_dataset_by_name(name, user, "read") is None

    await _real_cognee_client(monkeypatch).ensure_dataset(name)

    assert (
        await get_authorized_dataset_by_name(name, user, "read")
    ) is not None, "seat still unsearchable after ensure_dataset"


@pytest.mark.asyncio
async def test_chunk_store_drilldown_never_creates_datasets_or_grants(
    cognee_sqlite: Any, monkeypatch: Any
) -> None:
    """A drill-down must not create a dataset, a grant, or a user.

    cognee's ``resolve_authorized_user_datasets`` is a WRITE helper: it calls
    ``load_or_create_datasets``, so a dataset name the caller cannot be
    authorized for is CREATED and granted to the default user. Pointing the
    read-only drill-down at it gave GET /api/documents, the /search owner hint
    and MCP citadel_get_document a relational write side effect, and under
    ENABLE_BACKEND_ACCESS_CONTROL=true (which register_qdrant_adapter mandates)
    a cross-tenant dataset-creation path: the foreign-owned name below grows a
    duplicate owned by the default user, and the reader then reads the empty
    copy. The real relational stack runs here on purpose — a fake resolver
    cannot show a create branch it does not have.

    The name is deliberately narrower than "never writes": entering the dataset
    database context DOES lazily insert one ``dataset_database`` registration row
    per (owner, dataset), which DEC-2026-08-10-05 accepts because it grants no
    access and is idempotent. That row is pinned below rather than left to be
    rediscovered, so a future change that starts granting access here fails.
    """
    from uuid import uuid4

    from sqlalchemy import func, select

    from cognee.infrastructure.databases.relational import (
        create_db_and_tables,
        get_relational_engine,
    )
    from cognee.modules.data.models import Data, Dataset, DatasetData
    from cognee.modules.users.models import ACL, DatasetDatabase, User
    from cognee.modules.users.methods import get_default_user

    await create_db_and_tables()
    # Warm the default user so the counts below measure the drill-down alone.
    await get_default_user()

    document_id = uuid4()
    foreign_dataset_id = uuid4()
    foreign_owner_id = uuid4()
    engine = get_relational_engine()
    async with engine.get_async_session() as session:
        session.add(Data(id=document_id, name="foreign-doc", owner_id=foreign_owner_id))
        session.add(
            Dataset(
                id=foreign_dataset_id,
                name="seat:foreign",
                owner_id=foreign_owner_id,
                tenant_id=None,
            )
        )
        session.add(
            DatasetData(dataset_id=foreign_dataset_id, data_id=document_id)
        )
        await session.commit()

    async def counts() -> dict[str, int]:
        async with engine.get_async_session() as session:
            measured = {}
            for label, model in (
                ("datasets", Dataset),
                ("acls", ACL),
                ("users", User),
                ("dataset_databases", DatasetDatabase),
            ):
                rows = await session.execute(select(func.count()).select_from(model))
                measured[label] = int(rows.scalar_one())
            return measured

    before = await counts()
    assert before == {
        "datasets": 1,
        "acls": 0,
        "users": 1,
        "dataset_databases": 0,
    }

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    # register_qdrant_adapter refuses to install without this, so it is the only
    # configuration the Qdrant drill-down can ever run under.
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    monkeypatch.setenv("VECTOR_DATASET_DATABASE_HANDLER", "qdrant")
    monkeypatch.setenv("VECTOR_DB_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("VECTOR_DB_KEY", "unused-by-this-test")
    monkeypatch.setenv("CITADEL_GENERATION_ID", "drilldown-generation")
    client = _real_cognee_client(monkeypatch)

    # No Qdrant is reachable, so the fallback degrades to None exactly as it
    # does in production. What is under test is what it wrote on the way.
    assert await client.get_document(str(document_id)) is None
    assert await client.resolve_document_owner_ids(str(document_id)) is None

    after = await counts()
    # MEASURED: not even the accepted dataset_database registration row appears
    # on this path. The owner here is a bare id with no User row, and entering
    # the dataset context calls get_user() first, which raises
    # EntityNotFoundError before get_or_create_dataset_database can insert. A
    # foreign owner therefore fails closed one step earlier than the same-owner
    # read pinned in the test below.
    assert after == before, (
        f"drill-down wrote to the relational store: {before} -> {after}"
    )

    async with engine.get_async_session() as session:
        rows = await session.execute(
            select(Dataset.owner_id).where(Dataset.name == "seat:foreign")
        )
        owners = [row[0] for row in rows.all()]
    assert owners == [foreign_owner_id], (
        "a duplicate seat:foreign dataset was created under another owner"
    )

    # The resolver still has to WORK: it returns the foreign dataset's own
    # identity columns, so the read is scoped to the dataset that actually holds
    # the document rather than to a same-named one under a different owner.
    assert await client._owning_datasets(str(document_id)) == [
        (foreign_dataset_id, foreign_owner_id)
    ]


async def test_chunk_store_drilldown_dataset_database_row_is_idempotent(
    cognee_sqlite: Any, monkeypatch: Any
) -> None:
    """A same-owner drill-down lazily writes exactly one dataset_database row.

    DEC-2026-08-10-05: entering the owning dataset's context under
    ENABLE_BACKEND_ACCESS_CONTROL=true materializes one dataset_database
    registration row per (owner, dataset). It grants no access and is
    idempotent, and this pins that accepted behavior so it cannot silently
    become a grant. The foreign-owner test above owns the access-grant question
    (zero acls, no duplicate dataset, and there the owner has no user row so the
    read fails closed before the registration row); this one gives the owner a
    real user, so the read reaches the context and the row appears exactly once
    and never grows on a repeat, while datasets, acls and users stay flat.
    """
    from uuid import uuid4

    from sqlalchemy import func, select

    from cognee.infrastructure.databases.relational import (
        create_db_and_tables,
        get_relational_engine,
    )
    from cognee.modules.data.models import Data, Dataset, DatasetData
    from cognee.modules.users.methods import create_user, get_default_user
    from cognee.modules.users.models import ACL, DatasetDatabase, User

    await create_db_and_tables()
    await get_default_user()
    owner = await create_user("drilldown-owner@example.com", "drilldown-pw")

    document_id = uuid4()
    dataset_id = uuid4()
    engine = get_relational_engine()
    async with engine.get_async_session() as session:
        session.add(Data(id=document_id, name="owned-doc", owner_id=owner.id))
        session.add(
            Dataset(id=dataset_id, name="seat:owned", owner_id=owner.id, tenant_id=None)
        )
        session.add(DatasetData(dataset_id=dataset_id, data_id=document_id))
        await session.commit()

    async def counts() -> dict[str, int]:
        async with engine.get_async_session() as session:
            measured = {}
            for label, model in (
                ("datasets", Dataset),
                ("acls", ACL),
                ("users", User),
                ("dataset_databases", DatasetDatabase),
            ):
                rows = await session.execute(select(func.count()).select_from(model))
                measured[label] = int(rows.scalar_one())
            return measured

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    monkeypatch.setenv("VECTOR_DATASET_DATABASE_HANDLER", "qdrant")
    monkeypatch.setenv("VECTOR_DB_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("VECTOR_DB_KEY", "unused-by-this-test")
    monkeypatch.setenv("CITADEL_GENERATION_ID", "drilldown-generation")
    client = _real_cognee_client(monkeypatch)

    before = await counts()

    # No Qdrant is reachable, so the read degrades to None; what is pinned is the
    # registration row that entering the owning dataset context wrote first.
    assert await client.get_document(str(document_id)) is None
    after_first = await counts()
    assert after_first == {
        **before,
        "dataset_databases": before["dataset_databases"] + 1,
    }, f"first read did not write exactly one registration row: {before} -> {after_first}"

    # A second read of the same (owner, dataset) reuses the row.
    assert await client.get_document(str(document_id)) is None
    after_second = await counts()
    assert after_second == after_first, (
        f"repeat read grew the relational store: {after_first} -> {after_second}"
    )


def test_assert_cognee_dataset_api_imports_real_symbols(monkeypatch: Any) -> None:
    # A cognee bump that moves the private dataset-attribution internals must
    # fail HERE (loud, in CI), not silently fail-closed in prod. This imports
    # the real symbols — the boot self-check calls the same function.
    from kb.cognee_client import assert_cognee_dataset_api

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    assert_cognee_dataset_api()


@pytest.mark.asyncio
async def test_node_dataset_map_caches_successful_read_within_ttl(
    monkeypatch: Any,
) -> None:
    # /api/mesh/graph calls this on every poll: a successful read must run once
    # per TTL, not once per request (#50).
    client = CogneePublicClient()
    reads = 0

    async def fake_read() -> dict[str, list[str]]:
        nonlocal reads
        reads += 1
        return {"doc": ["seat:alice"]}

    monkeypatch.setattr(client, "_read_node_dataset_map", fake_read)

    assert await client.node_dataset_map() == {"doc": ["seat:alice"]}
    assert await client.node_dataset_map() == {"doc": ["seat:alice"]}
    assert reads == 1


@pytest.mark.asyncio
async def test_node_dataset_map_reexpires_after_ttl(monkeypatch: Any) -> None:
    # A zero TTL guarantees the second call is a cache miss: proves the cache
    # actually expires (a broken/inverted TTL would latch a stale map forever).
    import kb.cognee_client as cognee_client_module

    monkeypatch.setattr(cognee_client_module, "NODE_DATASET_MAP_TTL_SECONDS", 0.0)
    client = CogneePublicClient()
    reads = 0

    async def fake_read() -> dict[str, list[str]]:
        nonlocal reads
        reads += 1
        return {"doc": ["seat:alice"]}

    monkeypatch.setattr(client, "_read_node_dataset_map", fake_read)

    await client.node_dataset_map()
    await client.node_dataset_map()
    assert reads == 2


@pytest.mark.asyncio
async def test_node_dataset_map_single_flight_collapses_cold_burst(
    monkeypatch: Any,
) -> None:
    # 15 seats opening the dashboard on a cold cache must not each fire their
    # own relational read (thundering herd). The single-flight lock collapses a
    # concurrent burst to one read (#50).
    client = CogneePublicClient()
    reads = 0

    async def fake_read() -> dict[str, list[str]]:
        nonlocal reads
        reads += 1
        await asyncio.sleep(0.05)
        return {"doc": ["seat:alice"]}

    monkeypatch.setattr(client, "_read_node_dataset_map", fake_read)

    results = await asyncio.gather(
        *[client.node_dataset_map() for _ in range(10)]
    )
    assert all(result == {"doc": ["seat:alice"]} for result in results)
    assert reads == 1


@pytest.mark.asyncio
async def test_node_dataset_map_failure_without_prior_good_is_empty(
    monkeypatch: Any,
) -> None:
    # First-ever read fails: degrade to {} (fail-closed for scoped callers) and
    # remember the failure for only the SHORT failure TTL, not the content TTL.
    client = CogneePublicClient()
    reads = 0

    async def fake_read() -> dict[str, list[str]]:
        nonlocal reads
        reads += 1
        raise RuntimeError("relational store offline")

    monkeypatch.setattr(client, "_read_node_dataset_map", fake_read)

    assert await client.node_dataset_map() == {}
    assert await client.node_dataset_map() == {}  # served from short failure cache
    assert reads == 1


@pytest.mark.asyncio
async def test_node_dataset_map_failure_prefers_last_good(monkeypatch: Any) -> None:
    # A transient stall after a good read must serve the last known-good map
    # (stale-while-error), NOT {} — otherwise fail-closed isolation would blank
    # every scoped caller's vault + 404 their own documents for a full minute
    # on one 5s overrun (#50).
    import kb.cognee_client as cognee_client_module

    client = CogneePublicClient()
    calls = {"n": 0}

    async def fake_read() -> dict[str, list[str]]:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"doc": ["seat:alice"]}
        raise RuntimeError("relational store stalled")

    monkeypatch.setattr(client, "_read_node_dataset_map", fake_read)

    assert await client.node_dataset_map() == {"doc": ["seat:alice"]}
    # Expire the success cache so the next call re-reads (and fails).
    monkeypatch.setattr(cognee_client_module, "NODE_DATASET_MAP_TTL_SECONDS", 0.0)
    assert await client.node_dataset_map() == {"doc": ["seat:alice"]}  # stale, not {}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_node_dataset_map_times_out_and_caches_the_failure(
    monkeypatch: Any,
) -> None:
    # A non-erroring relational outage (TCP blackhole, saturated pool) must not
    # stall /api/mesh/graph: the read is time-bounded, degrades to {}, and the
    # failure is remembered for the failure TTL instead of re-blocking every
    # poll (#50).
    import kb.cognee_client as cognee_client_module

    monkeypatch.setattr(cognee_client_module, "NODE_DATASET_MAP_TIMEOUT_SECONDS", 0.05)
    client = CogneePublicClient()
    reads = 0

    async def fake_read() -> dict[str, list[str]]:
        nonlocal reads
        reads += 1
        await asyncio.sleep(30)  # simulated blackhole: never errors, never returns
        return {}

    monkeypatch.setattr(client, "_read_node_dataset_map", fake_read)

    assert await client.node_dataset_map() == {}
    assert await client.node_dataset_map() == {}  # served from the failure cache
    assert reads == 1


@pytest.mark.asyncio
async def test_cognify_serializes_on_writer_lock(monkeypatch: Any) -> None:
    # #47: Kuzu is single-writer, so two overlapping cognify calls must serialize.
    monkeypatch.setenv("LLM_API_KEY", "k")
    concurrent = 0
    max_seen = 0

    async def fake_cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        nonlocal concurrent, max_seen
        concurrent += 1
        max_seen = max(max_seen, concurrent)
        await asyncio.sleep(0.02)
        concurrent -= 1
        return {"ok": True}

    async def run_migrations() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(cognify=fake_cognify, run_migrations=run_migrations),
    )
    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def _ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    await asyncio.gather(client.cognify(datasets=["a"]), client.cognify(datasets=["b"]))
    assert max_seen == 1  # the writer lock prevented concurrent graph writes


@pytest.mark.asyncio
async def test_durable_writes_bypass_session_cache(monkeypatch: Any) -> None:
    """Durable writes never route through cognee's session cache.

    Passing a session_id used to divert the write into the per-session cache,
    which stored the payload as the literal "[DataItem]" placeholder, never
    cognified it (ingest items_processed:0), and re-embedded a growing
    scaffolded blob each cycle. remember() now always sends the write to the
    permanent add+cognify path: cognee.remember is called WITHOUT a session_id,
    and the payload is DataItem-wrapped so citadel_tags metadata survives.
    """
    from dataclasses import dataclass, field

    @dataclass
    class DataItem:
        data: Any
        label: Any = None
        external_metadata: Any = field(default=None)
        data_id: Any = None

    for parent in ("cognee.tasks", "cognee.tasks.ingestion"):
        monkeypatch.setitem(sys.modules, parent, SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "cognee.tasks.ingestion.data_item",
        SimpleNamespace(DataItem=DataItem),
    )

    captured: dict[str, Any] = {}

    async def run_migrations() -> None:
        return None

    async def add(data: Any, **kwargs: Any) -> dict[str, Any]:
        captured["data"] = data
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, add=add),
    )
    # Suppress the background cognify so the test is deterministic (and so a real
    # cognify isn't scheduled against the mock); the bypass assertion is on add.
    monkeypatch.setenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "true")
    client = CogneePublicClient()

    # Even with a session_id supplied, the write must NOT be diverted into the
    # session cache: no session_id reaches cognee.add, and the payload is
    # DataItem-wrapped (carrying citadel_tags) for the permanent graph.
    result = await client.remember(
        "real digest",
        dataset_name="masumi-network",
        session_id="masumi-github-daily",
        tags=("github",),
        attestation={
            "promoted_by": "admin-7",
            "promoted_at": "2026-08-06T12:00:00+00:00",
        },
    )
    assert "session_id" not in captured["kwargs"]
    assert captured["kwargs"] == {"dataset_name": "masumi-network"}
    assert isinstance(captured["data"], DataItem)
    assert captured["data"].data == "real digest"
    assert captured["data"].external_metadata == {
        "citadel_tags": ["github"],
        "citadel_attestation": {
            "promoted_by": "admin-7",
            "promoted_at": "2026-08-06T12:00:00+00:00",
        },
    }
    assert result == {"added": {"ok": True}, "cognify": "suppressed"}


@pytest.mark.asyncio
async def test_remember_schedules_lock_guarded_background_cognify(
    monkeypatch: Any, tmp_path: Any
) -> None:
    # #47: outside the suppress flag, remember adds then schedules OUR background
    # cognify (lock-guarded), not cognee's fire-and-forget run_in_background.
    monkeypatch.delenv("CITADEL_SUPPRESS_INLINE_COGNIFY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("CITADEL_COGNIFY_QUEUE_PATH", str(tmp_path / "queue.json"))
    cognified: list[Any] = []

    async def run_migrations() -> None:
        return None

    async def add(data: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        cognified.append(list(datasets))
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, add=add, cognify=cognify),
    )
    client = CogneePublicClient()

    result = await client.remember("note", dataset_name="seat:sarthi", tags=())
    assert result == {"added": {"ok": True}, "background_cognify": True}
    # Drain the scheduled background cognify and confirm it ran via cognify().
    task = client._cognify_queue_task
    assert task is not None
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    finally:
        await client.stop_cognify_queue()
    assert cognified == [["seat:sarthi"]]


@pytest.mark.asyncio
async def test_schedule_cognify_runs_one_cognify_over_all_datasets(
    monkeypatch: Any, tmp_path: Any
) -> None:
    # #46/#52: the coalesced cognify is ONE background task over every dataset the
    # bulk write touched (de-duplicated), not one-per-write.
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("CITADEL_COGNIFY_QUEUE_PATH", str(tmp_path / "queue.json"))
    cognified: list[list[str]] = []

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        cognified.append(list(datasets))
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    client = CogneePublicClient()

    client.schedule_cognify(["central", "seat:a", "central"])  # duplicate central
    task = client._cognify_queue_task
    assert task is not None
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    finally:
        await client.stop_cognify_queue()
    assert cognified == [["central", "seat:a"]]  # one cognify, de-duplicated

    # No datasets → no task scheduled.
    cognified.clear()
    assert client.schedule_cognify([]) is False
    assert cognified == []


def test_schedule_cognify_persists_without_a_running_loop(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    monkeypatch.setenv("CITADEL_COGNIFY_QUEUE_PATH", str(path))

    CogneePublicClient().schedule_cognify(["central"])

    jobs = CognifyRetryQueue(path).snapshot()
    assert len(jobs) == 1
    assert jobs[0].datasets == ("central",)


@pytest.mark.asyncio
async def test_failed_background_cognify_is_rescheduled(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    monkeypatch.setenv("CITADEL_COGNIFY_QUEUE_PATH", str(path))
    monkeypatch.setenv("LLM_API_KEY", "k")

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        raise RuntimeError("node unavailable")

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    client = CogneePublicClient()
    client.schedule_cognify(["central"])
    task = client._cognify_queue_task
    assert task is not None
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)

        jobs = CognifyRetryQueue(path).snapshot()
        assert len(jobs) == 1
        assert jobs[0].leased is False
        assert jobs[0].last_error == "RuntimeError: node unavailable"
    finally:
        await client.stop_cognify_queue()


@pytest.mark.asyncio
async def test_failed_background_cognify_retries_without_new_ingest(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    queue = CognifyRetryQueue(path, backoff_seconds=0.01, max_backoff_seconds=0.05)
    monkeypatch.setenv("LLM_API_KEY", "k")
    calls: list[list[str]] = []
    retried = asyncio.Event()

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        calls.append(list(datasets))
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        retried.set()
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    client = CogneePublicClient(retry_queue=queue)
    client.schedule_cognify(["central"])

    await asyncio.wait_for(retried.wait(), timeout=3)

    async def wait_for_queue_empty() -> None:
        while queue.snapshot():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_for_queue_empty(), timeout=1)

    assert calls == [["central"], ["central"]]
    assert queue.snapshot() == ()
    await client.stop_cognify_queue()


@pytest.mark.asyncio
async def test_long_cognify_renews_queue_lease(monkeypatch: Any, tmp_path: Any) -> None:
    path = tmp_path / "queue.json"
    clock_now = datetime.now(UTC)

    def manual_clock() -> datetime:
        return clock_now

    queue = CognifyRetryQueue(path, lease_seconds=0.3, clock=manual_clock)
    monkeypatch.setenv("LLM_API_KEY", "k")
    started = asyncio.Event()
    renewed = asyncio.Event()
    renewal_deadlines: list[tuple[str, str]] = []
    heartbeat_delays: list[float] = []
    renew = queue.renew
    sleep = asyncio.sleep

    async def track_heartbeat_delay(delay: float) -> None:
        heartbeat_delays.append(delay)
        if len(heartbeat_delays) == 1:
            await sleep(0)
            return
        await asyncio.Event().wait()

    def track_renewal(lease: Any) -> Any:
        nonlocal clock_now
        clock_now += timedelta(seconds=0.2)
        renewed_lease = renew(lease)
        renewal_deadlines.append((lease.leased_until, renewed_lease.leased_until))
        renewed.set()
        return renewed_lease

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        nonlocal clock_now
        started.set()
        await renewed.wait()
        # Acknowledgement now occurs after the original deadline but before the
        # renewed deadline. A heartbeat that does not extend the lease fails.
        clock_now += timedelta(seconds=0.2)
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    monkeypatch.setattr(queue, "renew", track_renewal)
    monkeypatch.setattr(asyncio, "sleep", track_heartbeat_delay)
    client = CogneePublicClient(retry_queue=queue)
    client.schedule_cognify(["central"])
    task = client._cognify_queue_task
    assert task is not None

    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)

        assert renewal_deadlines
        assert renewal_deadlines[0][1] > renewal_deadlines[0][0]
        assert heartbeat_delays
        assert heartbeat_delays[0] == pytest.approx(0.1)
        assert all(0 < delay < queue.lease_seconds for delay in heartbeat_delays)
        assert queue.snapshot() == ()
    finally:
        await client.stop_cognify_queue()


@pytest.mark.asyncio
async def test_heartbeat_failure_wins_same_turn_cognify_completion(
    monkeypatch: Any, tmp_path: Any
) -> None:
    queue = CognifyRetryQueue(tmp_path / "queue.json")
    queue.enqueue(["central"])
    lease = queue.claim()
    assert lease is not None
    client = CogneePublicClient(retry_queue=queue)

    async def cognify(*, datasets: list[str], force: bool = False) -> dict[str, Any]:
        return {"ok": True}

    async def fail_heartbeat(lease: Any) -> None:
        raise OSError("queue volume unavailable")

    monkeypatch.setattr(client, "cognify", cognify)
    monkeypatch.setattr(client, "_renew_cognify_lease", fail_heartbeat)

    with pytest.raises(OSError, match="queue volume unavailable"):
        await client._cognify_with_lease(lease)


@pytest.mark.asyncio
async def test_failed_lease_renewal_cancels_cognify_before_retry(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    queue = CognifyRetryQueue(
        path,
        lease_seconds=1,
        backoff_seconds=30,
        max_backoff_seconds=30,
    )
    monkeypatch.setenv("LLM_API_KEY", "k")
    started = asyncio.Event()
    cancelled = asyncio.Event()
    blocked = asyncio.Event()

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        started.set()
        try:
            await blocked.wait()
        finally:
            cancelled.set()
        return {"ok": True}

    def fail_renewal(lease: Any) -> None:
        raise OSError("queue volume unavailable")

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    monkeypatch.setattr(queue, "renew", fail_renewal)
    client = CogneePublicClient(retry_queue=queue)
    client.schedule_cognify(["central"])
    task = client._cognify_queue_task
    assert task is not None

    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    await asyncio.wait_for(asyncio.shield(task), timeout=2)

    jobs = queue.snapshot()
    assert len(jobs) == 1
    assert jobs[0].leased is False
    assert jobs[0].last_error == "OSError: queue volume unavailable"
    assert client._cognify_queue_retry_handle is not None
    await client.stop_cognify_queue()


@pytest.mark.asyncio
async def test_execution_guard_blocks_reclaim_until_cancellation_cleanup_stops(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    # Expire attempt one under a controlled clock. A real subsecond lease also
    # makes the replacement claim and acknowledgement race runner load.
    clock_now = datetime.now(UTC)

    def manual_clock() -> datetime:
        return clock_now

    first_queue = CognifyRetryQueue(
        path,
        lease_seconds=3600.0,
        backoff_seconds=0.01,
        max_backoff_seconds=0.05,
        clock=manual_clock,
    )
    second_queue = CognifyRetryQueue(
        path,
        lease_seconds=3600.0,
        backoff_seconds=0.01,
        max_backoff_seconds=0.05,
        clock=manual_clock,
    )
    first_queue.enqueue(["central"])
    first_client = CogneePublicClient(retry_queue=first_queue)
    second_client = CogneePublicClient(retry_queue=second_queue)
    first_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    replacement_started = asyncio.Event()
    active_cognify = 0
    max_active_cognify = 0

    async def first_cognify(*, datasets: list[str], force: bool = False) -> None:
        nonlocal active_cognify, max_active_cognify
        active_cognify += 1
        max_active_cognify = max(max_active_cognify, active_cognify)
        first_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()
            raise
        finally:
            active_cognify -= 1

    async def fail_heartbeat(lease: Any) -> None:
        await first_started.wait()
        raise OSError("queue volume unavailable")

    async def replacement_cognify(*, datasets: list[str], force: bool = False) -> None:
        nonlocal active_cognify, max_active_cognify
        active_cognify += 1
        max_active_cognify = max(max_active_cognify, active_cognify)
        replacement_started.set()
        active_cognify -= 1

    monkeypatch.setattr(first_client, "cognify", first_cognify)
    monkeypatch.setattr(first_client, "_renew_cognify_lease", fail_heartbeat)
    monkeypatch.setattr(
        first_client,
        "_schedule_cognify_retry",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(second_client, "cognify", replacement_cognify)

    wait_timeout = 5.0
    first_client.resume_cognify_queue()
    first_task = first_client._cognify_queue_task
    assert first_task is not None
    await asyncio.wait_for(cleanup_started.wait(), timeout=wait_timeout)
    clock_now += timedelta(hours=2)

    second_client.resume_cognify_queue()
    contending_task = second_client._cognify_queue_task
    assert contending_task is not None
    await asyncio.wait_for(asyncio.shield(contending_task), timeout=wait_timeout)

    assert replacement_started.is_set() is False
    pending = second_queue.snapshot()
    assert len(pending) == 1
    assert pending[0].attempt == 1
    assert max_active_cognify == 1

    release_cleanup.set()
    await asyncio.wait_for(asyncio.shield(first_task), timeout=wait_timeout)
    second_client._cancel_cognify_retry()
    second_client.resume_cognify_queue()
    replacement_task = second_client._cognify_queue_task
    assert replacement_task is not None
    await asyncio.wait_for(replacement_started.wait(), timeout=wait_timeout)
    await asyncio.wait_for(asyncio.shield(replacement_task), timeout=wait_timeout)

    assert second_queue.snapshot() == ()
    await first_client.stop_cognify_queue()
    await second_client.stop_cognify_queue()


@pytest.mark.asyncio
async def test_execution_lock_contention_uses_bounded_poll(tmp_path: Any) -> None:
    path = tmp_path / "queue.json"
    owner_queue = CognifyRetryQueue(path, lease_seconds=300)
    contender_queue = CognifyRetryQueue(path, lease_seconds=300)
    owner_queue.enqueue(["central"])
    owner_guard = owner_queue.try_acquire_execution()
    assert owner_guard is not None
    lease = owner_queue.claim()
    assert lease is not None
    contender = CogneePublicClient(retry_queue=contender_queue)

    contender.resume_cognify_queue()
    task = contender._cognify_queue_task
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=1)

    handle = contender._cognify_queue_retry_handle
    assert handle is not None
    remaining = handle.when() - asyncio.get_running_loop().time()
    assert 0 < remaining <= 1.0
    assert contender_queue.snapshot()[0].attempt == 1

    await contender.stop_cognify_queue()
    owner_guard.release()


@pytest.mark.asyncio
async def test_failed_acknowledgement_retries_without_external_activity(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    # The retry after a failed acknowledgement is reclaim-on-expiry, so the
    # first lease must expire, while the second acknowledgement needs its lease
    # still live. Under a real 50ms lease both sides raced the runner: the 3.12
    # CI lane expired lease two mid-cycle, acknowledge raised
    # CognifyLeaseError, and a third retry broke the exact-two-calls assertion.
    # An hour-long lease with a manual two-hour clock jump at the failure point
    # forces exactly one expiry, exactly when the test intends it.
    clock_offset = timedelta()

    def manual_clock() -> datetime:
        return datetime.now(UTC) + clock_offset

    queue = CognifyRetryQueue(
        path,
        lease_seconds=3600.0,
        backoff_seconds=0.01,
        max_backoff_seconds=0.05,
        clock=manual_clock,
    )
    monkeypatch.setenv("LLM_API_KEY", "k")
    cognify_calls: list[list[str]] = []
    acknowledgement_calls = 0
    wakeup_calls = 0
    retried = asyncio.Event()

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        cognify_calls.append(list(datasets))
        if len(cognify_calls) == 2:
            retried.set()
        return {"ok": True}

    acknowledge = queue.acknowledge
    next_wakeup_delay = queue.next_wakeup_delay
    loop = asyncio.get_running_loop()
    call_later = loop.call_later
    retry_wakeups = 0

    def run_retry_next_tick(
        delay: float,
        callback: Any,
        *args: Any,
        context: Any = None,
    ) -> Any:
        nonlocal retry_wakeups
        if getattr(callback, "__name__", "") == "_wake":
            retry_wakeups += 1
            return loop.call_soon(callback, *args, context=context)
        return call_later(delay, callback, *args, context=context)

    def fail_first_acknowledgement(lease: Any) -> None:
        nonlocal acknowledgement_calls, clock_offset
        acknowledgement_calls += 1
        if acknowledgement_calls == 1:
            clock_offset = timedelta(hours=2)
            raise OSError("queue volume unavailable")
        acknowledge(lease)

    def fail_first_wakeup_read() -> float | None:
        nonlocal wakeup_calls
        wakeup_calls += 1
        if wakeup_calls == 1:
            raise OSError("queue volume unavailable")
        return next_wakeup_delay()

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    monkeypatch.setattr(queue, "acknowledge", fail_first_acknowledgement)
    monkeypatch.setattr(queue, "next_wakeup_delay", fail_first_wakeup_read)
    monkeypatch.setattr(loop, "call_later", run_retry_next_tick)
    client = CogneePublicClient(retry_queue=queue)
    client.schedule_cognify(["central"])

    try:
        await asyncio.wait_for(retried.wait(), timeout=5)
        retry_task = client._cognify_queue_task
        assert retry_task is not None
        await asyncio.wait_for(asyncio.shield(retry_task), timeout=5)

        assert cognify_calls == [["central"], ["central"]]
        assert acknowledgement_calls == 2
        assert wakeup_calls >= 2
        assert retry_wakeups == 1
        assert queue.snapshot() == ()
    finally:
        await client.stop_cognify_queue()


@pytest.mark.asyncio
async def test_child_cognify_cancellation_retries_without_external_activity(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    queue = CognifyRetryQueue(
        path,
        backoff_seconds=0.01,
        max_backoff_seconds=0.05,
    )
    monkeypatch.setenv("LLM_API_KEY", "k")
    cognify_calls: list[list[str]] = []
    retried = asyncio.Event()

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        cognify_calls.append(list(datasets))
        if len(cognify_calls) == 1:
            raise asyncio.CancelledError
        retried.set()
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    client = CogneePublicClient(retry_queue=queue)
    client.schedule_cognify(["central"])

    await asyncio.wait_for(retried.wait(), timeout=1)

    async def wait_for_queue_empty() -> None:
        while queue.snapshot():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_for_queue_empty(), timeout=1)

    assert cognify_calls == [["central"], ["central"]]
    assert queue.snapshot() == ()
    await client.stop_cognify_queue()


@pytest.mark.asyncio
async def test_stop_cognify_queue_reschedules_cancelled_work(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    queue = CognifyRetryQueue(path, lease_seconds=30, backoff_seconds=0.01)
    monkeypatch.setenv("LLM_API_KEY", "k")
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        started.set()
        await blocked.wait()
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    client = CogneePublicClient(retry_queue=queue)
    client.schedule_cognify(["central"])

    await asyncio.wait_for(started.wait(), timeout=1)
    await client.stop_cognify_queue()

    jobs = queue.snapshot()
    assert len(jobs) == 1
    assert jobs[0].leased is False
    assert jobs[0].last_error == "cognify worker cancelled"


@pytest.mark.asyncio
async def test_resume_cognify_queue_drains_pending_work(
    monkeypatch: Any, tmp_path: Any
) -> None:
    path = tmp_path / "queue.json"
    CognifyRetryQueue(path).enqueue(["central"])
    monkeypatch.setenv("LLM_API_KEY", "k")
    cognified: list[list[str]] = []

    async def run_migrations() -> None:
        return None

    async def cognify(*, datasets: Any, incremental_loading: bool, data_cache: bool) -> dict[str, Any]:
        cognified.append(list(datasets))
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    client = CogneePublicClient(queue_path=path)
    client.resume_cognify_queue()
    task = client._cognify_queue_task
    assert task is not None
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)

        assert cognified == [["central"]]
        assert CognifyRetryQueue(path).snapshot() == ()
    finally:
        await client.stop_cognify_queue()


@pytest.mark.asyncio
async def test_remember_reports_false_when_durable_queue_cannot_accept_work(
    monkeypatch: Any,
) -> None:
    class BrokenQueue:
        def enqueue(self, datasets: list[str]) -> None:
            raise OSError("queue volume unavailable")

    async def add(data: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def run_migrations() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, add=add),
    )
    result = await CogneePublicClient(retry_queue=BrokenQueue()).remember(
        "note", dataset_name="central"
    )

    assert result == {"added": {"ok": True}, "background_cognify": False}


@pytest.mark.asyncio
async def test_cognee_public_client_uses_chunk_search_by_default(monkeypatch: Any) -> None:
    received: dict[str, Any] = {}

    async def run_migrations() -> None:
        return None

    async def recall(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        received["recall"] = {"args": args, "kwargs": kwargs}
        return []

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        received["search"] = kwargs
        return [{"ok": True}]

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(
                CHUNKS="chunks",
                GRAPH_COMPLETION="graph_completion",
            ),
            run_migrations=run_migrations,
            recall=recall,
            search=search,
        ),
    )
    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setenv("CITADEL_COGNEE_SEARCH_TYPE", "GRAPH_COMPLETION")
    monkeypatch.setenv("AUTO_FEEDBACK", "true")

    result = await client.recall("note", dataset="notes")

    assert result == [{"ok": True}]
    assert "recall" not in received
    assert received["search"]["query_type"] == "chunks"
    assert received["search"]["datasets"] == ["notes"]


@pytest.mark.asyncio
async def test_cognee_public_client_honors_explicit_graph_search(
    monkeypatch: Any,
) -> None:
    received: dict[str, Any] = {}

    async def run_migrations() -> None:
        return None

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        received["search"] = kwargs
        return [{"ok": True}]

    monkeypatch.setenv("CITADEL_COGNEE_SEARCH_TYPE", "GRAPH_COMPLETION")
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(
                CHUNKS="chunks",
                GRAPH_COMPLETION="graph_completion",
            ),
            run_migrations=run_migrations,
            search=search,
        ),
    )
    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    result = await client.recall("note", dataset="notes", allow_generative=True)

    assert result == [{"ok": True}]
    assert received["search"]["query_type"] == "graph_completion"
    assert received["search"]["include_references"] is True


@pytest.mark.asyncio
async def test_cognee_public_client_unwraps_dataset_chunk_results(monkeypatch: Any) -> None:
    marker = "citadel-v050-final-acceptance"

    async def run_migrations() -> None:
        return None

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "dataset_id": "dataset-id",
                "dataset_name": "notes",
                "dataset_tenant_id": "tenant-id",
                "search_result": [{"id": "chunk-1", "text": marker}],
            }
        ]

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="chunks"),
            run_migrations=run_migrations,
            search=search,
        ),
    )

    result = await CogneePublicClient().recall("marker", dataset="notes")

    assert result == [{"id": "chunk-1", "text": marker}]


@pytest.mark.asyncio
async def test_session_recall_off_by_default_and_opt_in(monkeypatch: Any) -> None:
    # #15/#52: the per-session QA cache served stale "[DataItem]" garbage, so the
    # session-scoped recall is OFF by default — search goes straight to the durable
    # chunk store. It only runs when CITADEL_COGNEE_SESSION_RECALL is set.
    received: dict[str, Any] = {}

    async def run_migrations() -> None:
        return None

    async def recall(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        received["recall"] = {"args": args, "kwargs": kwargs}
        return [{"source": "session"}]

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        received["search"] = kwargs
        return [{"source": "graph"}]

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="chunks"),
            run_migrations=run_migrations,
            recall=recall,
            search=search,
        ),
    )
    client = CogneePublicClient()

    # Default OFF: the session cache is never read; the durable chunk search runs.
    monkeypatch.delenv("CITADEL_COGNEE_SESSION_RECALL", raising=False)
    result = await client.recall(
        "note",
        dataset="notes",
        session_id="source-session",
        allow_generative=True,
    )
    assert result == [{"source": "graph"}]
    assert "recall" not in received  # session QA cache never touched
    assert "search" in received

    # Opt-in: session recall runs first only when explicitly enabled.
    received.clear()
    monkeypatch.setenv("CITADEL_COGNEE_SESSION_RECALL", "true")
    result = await client.recall(
        "note",
        dataset="notes",
        session_id="source-session",
        allow_generative=True,
    )
    assert result == [{"source": "session"}]
    assert received["recall"]["kwargs"]["scope"] == "session"
    assert "search" not in received


@pytest.mark.asyncio
async def test_cognee_public_client_returns_empty_results_for_empty_store(
    monkeypatch: Any,
) -> None:
    class NoDataError(Exception):
        pass

    async def run_migrations() -> None:
        return None

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        raise NoDataError("No data found in the system, please add data first.")

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="chunks"),
            run_migrations=run_migrations,
            search=search,
        ),
    )
    client = CogneePublicClient()

    result = await client.recall("note", dataset="notes")

    assert result == []


@pytest.mark.asyncio
async def test_cognee_public_client_returns_empty_results_for_absent_dataset(
    monkeypatch: Any,
) -> None:
    class DatasetNotFoundError(Exception):
        pass

    async def run_migrations() -> None:
        return None

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        raise DatasetNotFoundError("No datasets found. (Status code: 404)")

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="chunks"),
            run_migrations=run_migrations,
            search=search,
        ),
    )

    result = await CogneePublicClient().recall("note", dataset="configured-but-absent")

    assert result == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "search_error",
    [
        PermissionError("dataset access denied"),
        RuntimeError("Qdrant unavailable"),
    ],
    ids=["permission", "provider"],
)
async def test_cognee_public_client_surfaces_non_empty_search_failures(
    monkeypatch: Any,
    search_error: Exception,
) -> None:
    async def run_migrations() -> None:
        return None

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        raise search_error

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="chunks"),
            run_migrations=run_migrations,
            search=search,
        ),
    )

    with pytest.raises(type(search_error), match=str(search_error)):
        await CogneePublicClient().recall("note", dataset="notes")


@pytest.mark.asyncio
async def test_cognee_public_client_falls_back_when_session_has_no_data(
    monkeypatch: Any,
) -> None:
    class NoDataError(Exception):
        pass

    received: dict[str, Any] = {}

    async def run_migrations() -> None:
        return None

    async def recall(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        received["recall"] = {"args": args, "kwargs": kwargs}
        raise NoDataError("No data found in the system, please add data first.")

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        received["search"] = kwargs
        return [{"source": "chunks"}]

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="chunks"),
            run_migrations=run_migrations,
            recall=recall,
            search=search,
        ),
    )
    client = CogneePublicClient()

    # The session-recall fallback only applies when session recall is opted in.
    monkeypatch.setenv("CITADEL_COGNEE_SESSION_RECALL", "true")
    result = await client.recall(
        "note",
        dataset="notes",
        session_id="source-session",
        allow_generative=True,
    )

    assert result == [{"source": "chunks"}]
    assert received["recall"]["kwargs"]["scope"] == "session"
    assert received["search"]["datasets"] == ["notes"]


@pytest.mark.asyncio
async def test_cognee_public_client_cognify_wraps_cognee_cognify(monkeypatch: Any) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    received: dict[str, Any] = {}

    async def run_migrations() -> None:
        return None

    async def cognify(**kwargs: Any) -> dict[str, Any]:
        received["kwargs"] = kwargs
        return {"cognified": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=run_migrations,
            cognify=cognify,
        ),
    )
    client = CogneePublicClient()

    result = await client.cognify(datasets=["masumi-network"])

    assert result == {"cognified": True}
    assert received["kwargs"] == {
        "datasets": ["masumi-network"],
        "incremental_loading": True,
        "data_cache": True,
    }


@pytest.mark.asyncio
async def test_cognify_checks_only_source_ids_processed_by_this_pass(monkeypatch: Any) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    run = SimpleNamespace(
        data_ingestion_info=[
            {"data_id": "doc-a"},
            {"data_id": "doc-a"},
            {"data_id": "doc-b"},
        ]
    )

    async def run_migrations() -> None:
        return None

    async def cognify(**_: Any) -> dict[str, Any]:
        return {"dataset-id": run}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=run_migrations,
            cognify=cognify,
        ),
    )
    client = CogneePublicClient()
    checked: list[list[str]] = []

    async def stored_chunk_budget_check(
        *,
        document_ids: list[str],
        datasets: list[str],
        document_ids_by_dataset: dict[str, list[str]],
    ) -> dict[str, Any]:
        checked.append(document_ids)
        assert datasets == ["notes"]
        assert document_ids_by_dataset == {"dataset-id": ["doc-a", "doc-b"]}
        return {"ok": True, "violation_count": 0, "chunks_scanned": 2}

    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)

    await client.cognify(datasets=["notes"])

    assert checked == [["doc-a", "doc-b"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"dataset-id": SimpleNamespace(data_ingestion_info=[{"data_id": "doc-a"}])},
    ],
    ids=["empty", "partial"],
)
async def test_forced_cognify_rejects_empty_or_partial_receipt(
    monkeypatch: Any, receipt: dict[str, Any]
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    async def run_migrations() -> None:
        return None

    received: dict[str, Any] = {}

    async def cognify(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return receipt

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=run_migrations,
            cognify=cognify,
        ),
    )
    client = CogneePublicClient()

    async def dataset_document_ids(_: list[str]) -> list[str]:
        return ["doc-a", "doc-b"]

    monkeypatch.setattr(client, "dataset_document_ids", dataset_document_ids)

    with pytest.raises(RuntimeError, match="receipt omitted [12] expected source id"):
        await client.cognify(datasets=["notes"], force=True)

    assert received == {
        "datasets": ["notes"],
        "incremental_loading": False,
        "data_cache": False,
    }


@pytest.mark.asyncio
async def test_cognify_fails_when_stored_chunk_check_is_red(monkeypatch: Any) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    run = SimpleNamespace(data_ingestion_info=[{"data_id": "doc-a"}])

    async def run_migrations() -> None:
        return None

    async def cognify(**_: Any) -> dict[str, Any]:
        return {"dataset-id": run}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=run_migrations,
            cognify=cognify,
        ),
    )
    client = CogneePublicClient()

    async def stored_chunk_budget_check(
        *,
        document_ids: list[str],
        datasets: list[str],
        document_ids_by_dataset: dict[str, list[str]],
    ) -> dict[str, Any]:
        assert document_ids == ["doc-a"]
        assert datasets == ["notes"]
        assert document_ids_by_dataset == {"dataset-id": ["doc-a"]}
        return {"ok": False, "violation_count": 1, "chunks_scanned": 1}

    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)

    with pytest.raises(RuntimeError, match="stored chunk budget check failed"):
        await client.cognify(datasets=["notes"])


@pytest.mark.asyncio
async def test_cognify_budget_failure_names_the_violating_chunk(monkeypatch: Any) -> None:
    """The raise must carry the violator's identity, not a bare count.

    The check collects chunk_id and document_id per violation, but the raise
    site used to drop them, so the 2026-08-13 readiness canary failed for hours
    on "1 violation(s) across 1951 persisted chunk(s)" with no surface anywhere
    naming the offending document.
    """
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    run = SimpleNamespace(data_ingestion_info=[{"data_id": "doc-a"}])

    async def run_migrations() -> None:
        return None

    async def cognify(**_: Any) -> dict[str, Any]:
        return {"dataset-id": run}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    client = CogneePublicClient()

    async def stored_chunk_budget_check(**_: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "violation_count": 1,
            "chunks_scanned": 1951,
            "budget": 256,
            "violations": [
                {
                    "reason": "chunk_over_budget",
                    "chunk_id": "chunk-9f1e",
                    "document_id": "doc-minified-js",
                    "measured_tokens": 1710,
                }
            ],
        }

    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)

    with pytest.raises(RuntimeError) as excinfo:
        await client.cognify(datasets=["notes"])
    message = str(excinfo.value)
    assert "document doc-minified-js" in message
    assert "chunk chunk-9f1e" in message
    assert "1710 tokens > budget 256" in message


@pytest.mark.asyncio
async def test_stored_chunk_budget_check_scans_exact_pgvector_payloads(
    monkeypatch: Any,
) -> None:
    from contextlib import asynccontextmanager

    from sqlalchemy import JSON, Column, MetaData, String, Table

    metadata = MetaData()
    table = Table(
        "DocumentChunk_text",
        metadata,
        Column("id", String),
        Column("payload", JSON),
    )

    class Result:
        def all(self) -> list[tuple[str, dict[str, Any]]]:
            return [
                (
                    "chunk-a",
                    {
                        "text": "ordinary words " * 20,
                        "document_id": "doc-a",
                        "chunk_index": 0,
                        "chunk_size": 1,
                    },
                )
            ]

    class Session:
        async def execute(self, _: Any) -> Result:
            return Result()

    class VectorEngine:
        async def get_table(self, _: str) -> Table:
            return table

        @asynccontextmanager
        async def get_async_session(self) -> Any:
            yield Session()

    def get_vector_engine() -> VectorEngine:
        return VectorEngine()

    class CollectionNotFoundError(Exception):
        pass

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "cognee.infrastructure", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "cognee.infrastructure.databases", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.vector",
        SimpleNamespace(get_vector_engine=get_vector_engine),
    )
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.vector.exceptions",
        SimpleNamespace(CollectionNotFoundError=CollectionNotFoundError),
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def _ready(_: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    report = await client.stored_chunk_budget_check(["doc-a"], budget=8)

    assert report is not None
    assert report["ok"] is False
    assert report["chunks_scanned"] == 1
    assert report["violation_count"] == 1
    assert report["violations"][0]["chunk_id"] == "chunk-a"
    assert report["violation_document_counts"] == {"doc-a": 1}
    assert report["violation_document_ids"] == ["doc-a"]
    assert report["violations_truncated"] is False


@pytest.mark.asyncio
async def test_stored_chunk_budget_check_qdrant_scans_multiple_pages_and_isolates_datasets(
    monkeypatch: Any,
) -> None:
    from contextlib import asynccontextmanager
    from importlib import import_module

    rows_alice: list[SimpleNamespace] = []
    rows_bob = [
        SimpleNamespace(id="a1", payload={"document_id": "doc-a", "text": "small-a"}),
        SimpleNamespace(id="a2", payload={"document_id": "doc-b", "text": "small-b"}),
        SimpleNamespace(id="a3", payload={"document_id": "doc-c", "text": "small-c"}),
    ]
    scroll_calls: list[Any] = []
    resolve_calls: list[str] = []

    class FakeEngine:
        def __init__(self, dataset_id: str) -> None:
            self.dataset_id = dataset_id
            self.calls = 0

        async def has_collection(self, _: str) -> bool:
            return True

        async def scroll_data_points(
            self,
            _: str,
            *,
            offset: str | None,
            limit: int,
            with_vectors: bool,
            document_ids: list[str] | None,
        ) -> tuple[list[SimpleNamespace], str | None]:
            scroll_calls.append(
                (self.dataset_id, offset, limit, with_vectors, document_ids)
            )
            if self.dataset_id == "seat:alice":
                self.calls += 1
                if self.calls == 1:
                    return rows_alice, "next-1"
                return [], None
            if self.calls == 0:
                return rows_bob, None
            return [], None

    @asynccontextmanager
    async def fake_context(dataset_id: str, owner_id: str):
        del owner_id
        resolve_calls.append(dataset_id)
        yield None

    async def fake_resolve_authorized_user_datasets(
        datasets: list[str] | None, _user: Any
    ) -> tuple[list[Any], list[SimpleNamespace]]:
        del datasets
        return [], [
            SimpleNamespace(id="seat:alice", owner_id="owner-a"),
            SimpleNamespace(id="seat:bob", owner_id="owner-b"),
        ]

    async def fake_vector_engine() -> FakeEngine:
        return FakeEngine(resolve_calls[-1])

    async def fake_cognee_ready(_: Any) -> None:
        return None

    vector_module = import_module("cognee.infrastructure.databases.vector")
    cognee_module = import_module("cognee")
    context_module = import_module("cognee.context_global_variables")
    authorization_module = import_module(
        "cognee.modules.pipelines.layers.resolve_authorized_user_datasets"
    )

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setattr(cognee_module, "run_migrations", lambda: None)
    monkeypatch.setattr(
        vector_module,
        "get_vector_engine_async",
        lambda: fake_vector_engine(),
    )
    monkeypatch.setattr(context_module, "set_database_global_context_variables", fake_context)
    monkeypatch.setattr(
        authorization_module,
        "resolve_authorized_user_datasets",
        fake_resolve_authorized_user_datasets,
    )

    monkeypatch.setattr(
        import_module("kb.chunk_window"),
        "check_stored_chunk_payload",
        lambda payload, chunk_id, budget: None,  # clean payload -> no violation
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", fake_cognee_ready)

    report = await client.stored_chunk_budget_check(
        ["doc-a", "doc-b", "doc-c"],
        budget=128,
        datasets=["seat:alice", "seat:bob"],
        document_ids_by_dataset={
            "seat:alice": ["doc-a"],
            "seat:bob": ["doc-a", "doc-b", "doc-c"],
        },
    )

    assert report["ok"] is False
    assert report["provider"] == "qdrant"
    assert report["collection_present"] is True
    assert report["scope_dataset_count"] == 2
    assert report["chunks_scanned"] == 3
    assert report["document_chunk_counts"] == {
        "doc-a": 1,
        "doc-b": 1,
        "doc-c": 1,
    }
    assert report["missing_document_ids"] == ["doc-a"]
    assert report["missing_dataset_document_ids"] == [
        {"dataset": "seat:alice", "document_id": "doc-a"}
    ]
    assert report["dataset_reports"]["seat:alice"]["missing_document_ids"] == ["doc-a"]
    assert report["dataset_reports"]["seat:bob"]["missing_document_ids"] == []
    assert report["scope_document_count"] == 4
    assert resolve_calls == ["seat:alice", "seat:bob"]
    assert len(scroll_calls) == 3


@pytest.mark.asyncio
async def test_stored_chunk_budget_check_qdrant_reports_over_budget_payloads_and_orphan_ids(
    monkeypatch: Any,
) -> None:
    from contextlib import asynccontextmanager
    from importlib import import_module

    from kb.chunk_window import StoredChunkBudgetViolation

    captured: list[tuple[str, Any]] = []
    rows = [
        SimpleNamespace(
            id="chunk-a",
            payload={"document_id": "doc-a", "text": "too-large", "chunk_size": 16},
        )
    ]

    class FakeEngine:
        async def has_collection(self, _: str) -> bool:
            return True

        async def scroll_data_points(self, *_: Any, **__: Any) -> tuple[list[Any], None]:
            return rows, None

    async def fake_vector_engine() -> FakeEngine:
        return FakeEngine()

    @asynccontextmanager
    async def fake_context(*_: Any, **__: Any):
        yield None

    async def fake_resolve_authorized_user_datasets(
        datasets: list[str] | None, _user: Any
    ) -> tuple[list[Any], list[SimpleNamespace]]:
        del datasets
        return [], [SimpleNamespace(id="seat:alice", owner_id="owner-a")]

    def fake_check(
        payload: dict[str, Any], chunk_id: str, budget: int | None
    ) -> StoredChunkBudgetViolation | None:
        captured.append(("chunk_id", chunk_id, payload["document_id"], budget))
        return StoredChunkBudgetViolation(
            reason="chunk_over_budget",
            chunk_id=chunk_id,
            document_id=payload["document_id"],
            chunk_index=0,
            configured_size=16,
            measured_tokens=1024,
            char_length=7,
            fingerprint="ignored",
        )

    async def fake_cognee_ready(_: Any) -> None:
        return None

    vector_module = import_module("cognee.infrastructure.databases.vector")
    cognee_module = import_module("cognee")
    authorization_module = import_module(
        "cognee.modules.pipelines.layers.resolve_authorized_user_datasets"
    )
    context_module = import_module("cognee.context_global_variables")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setattr(cognee_module, "run_migrations", lambda: None)
    monkeypatch.setattr(vector_module, "get_vector_engine_async", fake_vector_engine)
    monkeypatch.setattr(context_module, "set_database_global_context_variables", fake_context)
    monkeypatch.setattr(authorization_module, "resolve_authorized_user_datasets", fake_resolve_authorized_user_datasets)

    from kb import chunk_window as chunk_window_module

    monkeypatch.setattr(
        chunk_window_module,
        "check_stored_chunk_payload",
        fake_check,
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", fake_cognee_ready)

    report = await client.stored_chunk_budget_check(
        ["doc-a", "doc-miss"],
        budget=5,
        datasets=["seat:alice"],
    )

    assert report["ok"] is False
    assert report["provider"] == "qdrant"
    assert report["chunks_scanned"] == 1
    assert report["violation_count"] == 1
    assert report["violation_document_counts"] == {"doc-a": 1}
    assert report["violation_document_ids"] == ["doc-a"]
    assert report["missing_document_ids"] == ["doc-miss"]


@pytest.mark.asyncio
async def test_stored_chunk_budget_check_qdrant_zero_measurement_marks_missing_documents(
    monkeypatch: Any,
) -> None:
    from contextlib import asynccontextmanager
    from importlib import import_module

    @asynccontextmanager
    async def fake_context(*_: Any, **__: Any):
        yield None

    async def fake_resolve_authorized_user_datasets(
        datasets: list[str] | None, _user: Any
    ) -> tuple[list[Any], list[SimpleNamespace]]:
        del datasets
        return [], [SimpleNamespace(id="seat:alice", owner_id="owner-a")]

    class FakeEngine:
        async def has_collection(self, _: str) -> bool:
            return True

        async def scroll_data_points(self, *_: Any, **__: Any) -> tuple[list[Any], None]:
            return [], None

    async def fake_vector_engine() -> FakeEngine:
        return FakeEngine()

    async def fake_ready(_: Any) -> None:
        return None

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    authorization_module = import_module(
        "cognee.modules.pipelines.layers.resolve_authorized_user_datasets"
    )
    context_module = import_module("cognee.context_global_variables")
    vector_module = import_module("cognee.infrastructure.databases.vector")
    monkeypatch.setattr(vector_module, "get_vector_engine_async", fake_vector_engine)
    monkeypatch.setattr(context_module, "set_database_global_context_variables", fake_context)
    monkeypatch.setattr(
        authorization_module,
        "resolve_authorized_user_datasets",
        fake_resolve_authorized_user_datasets,
    )
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=lambda: None,
            infrastructure=SimpleNamespace(
                databases=SimpleNamespace(
                    vector=SimpleNamespace(
                        get_vector_engine_async=fake_vector_engine,
                    )
                )
            ),
        ),
    )

    from kb import chunk_window as chunk_window_module

    monkeypatch.setattr(
        chunk_window_module,
        "check_stored_chunk_payload",
        lambda payload, chunk_id, budget: None,
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", fake_ready)

    report = await client.stored_chunk_budget_check(
        ["doc-missing"],
        datasets=["seat:alice"],
    )

    assert report["ok"] is False
    assert report["provider"] == "qdrant"
    assert report["chunks_scanned"] == 0
    assert report["missing_document_ids"] == ["doc-missing"]


@pytest.mark.asyncio
async def test_stored_chunk_budget_check_qdrant_fails_on_repeated_scroll_offset(
    monkeypatch: Any,
) -> None:
    from contextlib import asynccontextmanager
    from importlib import import_module

    @asynccontextmanager
    async def fake_context(*_: Any, **__: Any):
        yield None

    async def fake_resolve_authorized_user_datasets(
        datasets: list[str] | None, _user: Any
    ) -> tuple[list[Any], list[SimpleNamespace]]:
        del datasets
        return [], [SimpleNamespace(id="seat:alice", owner_id="owner-a")]

    class FakeEngine:
        async def has_collection(self, _: str) -> bool:
            return True

        async def scroll_data_points(
            self,
            *_: Any,
            **__: Any,
        ) -> tuple[list[Any], str]:
            return [SimpleNamespace(id="chunk-a", payload={})], "repeat"

    async def fake_vector_engine() -> FakeEngine:
        return FakeEngine()

    async def fake_ready(_: Any) -> None:
        return None

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_migrations=lambda: None,
            infrastructure=SimpleNamespace(
                databases=SimpleNamespace(
                    vector=SimpleNamespace(
                        get_vector_engine_async=fake_vector_engine,
                    )
                )
            ),
        ),
    )
    authorization_module = import_module(
        "cognee.modules.pipelines.layers.resolve_authorized_user_datasets"
    )
    context_module = import_module("cognee.context_global_variables")
    vector_module = import_module("cognee.infrastructure.databases.vector")
    monkeypatch.setattr(vector_module, "get_vector_engine_async", fake_vector_engine)
    monkeypatch.setattr(context_module, "set_database_global_context_variables", fake_context)
    monkeypatch.setattr(
        authorization_module,
        "resolve_authorized_user_datasets",
        fake_resolve_authorized_user_datasets,
    )
    from kb import chunk_window as chunk_window_module

    monkeypatch.setattr(
        chunk_window_module,
        "check_stored_chunk_payload",
        lambda payload, chunk_id, budget: None,
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", fake_ready)

    with pytest.raises(RuntimeError, match="Qdrant chunk scroll repeated an offset"):
        await client.stored_chunk_budget_check(
            ["doc-a"],
            budget=8,
            datasets=["seat:alice"],
        )


@pytest.mark.asyncio
async def test_stored_chunk_budget_check_qdrant_bubbles_provider_exceptions(monkeypatch: Any) -> None:
    from contextlib import asynccontextmanager
    from importlib import import_module

    @asynccontextmanager
    async def fake_context(*_: Any, **__: Any):
        yield None

    async def fake_resolve_authorized_user_datasets(
        datasets: list[str] | None, _user: Any
    ) -> tuple[list[Any], list[SimpleNamespace]]:
        del datasets
        return [], [SimpleNamespace(id="seat:alice", owner_id="owner-a")]

    async def fake_vector_engine() -> None:
        raise RuntimeError("provider down")

    async def fake_ready(_: Any) -> None:
        return None

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    authorization_module = import_module(
        "cognee.modules.pipelines.layers.resolve_authorized_user_datasets"
    )
    context_module = import_module("cognee.context_global_variables")
    vector_module = import_module("cognee.infrastructure.databases.vector")
    monkeypatch.setattr(vector_module, "get_vector_engine_async", fake_vector_engine)
    monkeypatch.setattr(context_module, "set_database_global_context_variables", fake_context)
    monkeypatch.setattr(
        authorization_module,
        "resolve_authorized_user_datasets",
        fake_resolve_authorized_user_datasets,
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", fake_ready)
    with pytest.raises(RuntimeError, match="provider down"):
        await client.stored_chunk_budget_check(
            ["doc-a"],
            datasets=["seat:alice"],
        )


@pytest.mark.asyncio
async def test_corpus_oversized_chunk_documents_reports_repair_metadata(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()
    pages = [
        [
            {"id": "doc-a", "name": "A", "datasets": ["notes"], "created_at": "1"},
            {"id": "doc-b", "name": "B", "datasets": ["other"], "created_at": "2"},
        ]
    ]

    async def corpus_totals() -> dict[str, int]:
        return {"documents": 2}

    async def corpus_page(**_: Any) -> list[dict[str, Any]]:
        return pages.pop(0)

    async def stored_chunk_budget_check(
        document_ids: list[str] | None, *, budget: int | None = None
    ) -> dict[str, Any]:
        assert document_ids is None
        assert budget == 256
        return {
            "ok": False,
            "violation_count": 3,
            "violation_document_counts": {"doc-a": 3},
            "missing_document_id_violation_count": 0,
        }

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)
    monkeypatch.setattr("kb.chunk_window.resolve_chunk_budget", lambda: 256)

    report = await client.corpus_oversized_chunk_documents(dataset="notes")

    assert report["ok"] is True
    assert report["oversized_document_count"] == 1
    assert report["oversized_chunk_count"] == 3
    assert report["repair_document_ids"] == ["doc-a"]
    assert report["repair_datasets"] == ["notes"]
    assert report["unassigned_oversized_document_count"] == 0
    assert report["census_complete"] is True


@pytest.mark.asyncio
async def test_corpus_oversized_chunk_documents_surfaces_orphan_violations(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()

    async def corpus_totals() -> dict[str, int]:
        return {"documents": 1}

    async def corpus_page(**_: Any) -> list[dict[str, Any]]:
        return [{"id": "doc-a", "datasets": ["notes"]}]

    async def stored_chunk_budget_check(
        document_ids: list[str] | None, *, budget: int | None = None
    ) -> dict[str, Any]:
        assert document_ids is None
        assert budget == 256
        return {
            "ok": False,
            "violation_count": 3,
            "violation_document_counts": {"doc-a": 1, "ghost": 1},
            "missing_document_id_violation_count": 1,
        }

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)
    monkeypatch.setattr("kb.chunk_window.resolve_chunk_budget", lambda: 256)

    report = await client.corpus_oversized_chunk_documents()

    assert report["oversized_document_count"] == 2
    assert report["repair_document_ids"] == ["doc-a"]
    assert report["unassigned_oversized_document_count"] == 1
    assert report["orphan_oversized_document_count"] == 1
    assert report["missing_document_id_violation_count"] == 1


@pytest.mark.asyncio
async def test_corpus_reconciliation_census_combines_zero_and_oversized_scans(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()
    page_calls: list[dict[str, Any]] = []
    chunk_calls: list[list[str]] = []

    async def corpus_totals() -> dict[str, int]:
        return {"documents": 3}

    async def corpus_page(**kwargs: Any) -> list[dict[str, Any]]:
        page_calls.append(kwargs)
        return [
            {"id": "doc-zero", "name": "Zero", "datasets": ["notes"], "created_at": "1"},
            {"id": "doc-over", "name": "Over", "datasets": ["notes"], "created_at": "2"},
            {"id": "doc-ok", "name": "OK", "datasets": ["notes"], "created_at": "3"},
        ]

    async def corpus_chunk_counts(document_ids: list[str]) -> dict[str, int]:
        chunk_calls.append(document_ids)
        return {"doc-zero": 0, "doc-over": 2, "doc-ok": 1}

    async def stored_chunk_budget_check(
        document_ids: list[str] | None, *, budget: int | None = None
    ) -> dict[str, Any]:
        assert document_ids is None
        assert budget == 256
        return {
            "ok": False,
            "scope": "full",
            "violation_count": 2,
            "violation_document_counts": {"doc-over": 2},
            "missing_document_id_violation_count": 0,
        }

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "corpus_chunk_counts", corpus_chunk_counts)
    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)
    monkeypatch.setattr("kb.chunk_window.resolve_chunk_budget", lambda: 256)

    report = await client.corpus_reconciliation_census(dataset="notes")

    assert report["ok"] is True
    assert report["census_complete"] is True
    assert report["zero_chunk_document_ids"] == ["doc-zero"]
    assert report["oversized_document_ids"] == ["doc-over"]
    assert report["repair_document_ids"] == ["doc-over", "doc-zero"]
    assert report["repair_document_datasets"] == {
        "doc-over": ["notes"],
        "doc-zero": ["notes"],
    }
    assert report["stored_chunk_budget"]["violation_count"] == 2
    assert len(page_calls) == 1
    assert chunk_calls == [["doc-zero", "doc-over", "doc-ok"]]


@pytest.mark.asyncio
async def test_corpus_reconciliation_census_scopes_oversized_totals_to_dataset(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()

    async def corpus_totals() -> dict[str, int]:
        return {"documents": 3}

    async def corpus_page(**_: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": "doc-notes-over",
                "name": "Notes over",
                "datasets": ["notes"],
                "created_at": "1",
            },
            {
                "id": "doc-other-over",
                "name": "Other over",
                "datasets": ["other"],
                "created_at": "2",
            },
            {
                "id": "doc-notes-ok",
                "name": "Notes ok",
                "datasets": ["notes"],
                "created_at": "3",
            },
        ]

    async def corpus_chunk_counts(document_ids: list[str]) -> dict[str, int]:
        return {document_id: 1 for document_id in document_ids}

    async def stored_chunk_budget_check(
        document_ids: list[str] | None, *, budget: int | None = None
    ) -> dict[str, Any]:
        assert document_ids is None
        assert budget == 256
        return {
            "ok": False,
            "scope": "full",
            "violation_count": 3,
            "violation_document_counts": {
                "doc-notes-over": 1,
                "doc-other-over": 2,
            },
            "missing_document_id_violation_count": 0,
        }

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "corpus_chunk_counts", corpus_chunk_counts)
    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)
    monkeypatch.setattr("kb.chunk_window.resolve_chunk_budget", lambda: 256)

    report = await client.corpus_reconciliation_census(dataset="notes")

    assert report["ok"] is True
    assert report["oversized_document_count"] == 1
    assert report["oversized_chunk_count"] == 1
    assert report["oversized_document_ids"] == ["doc-notes-over"]
    assert report["repair_document_ids"] == ["doc-notes-over"]
    # The nested scan stays full-corpus evidence; the top-level repair totals
    # are the requested dataset scope.
    assert report["stored_chunk_budget"]["violation_count"] == 3


@pytest.mark.asyncio
async def test_corpus_reconciliation_census_passes_qdrant_dataset_scope(
    monkeypatch: Any,
) -> None:
    """Qdrant forbids an unscoped payload scan. Production reconcile 500'd
    with 'Qdrant chunk census requires explicit dataset scope' even when the
    caller passed dataset=masumi-network, because the census called
    stored_chunk_budget_check(None) with no datasets kwarg.
    """
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    client = CogneePublicClient()
    captured: dict[str, Any] = {}

    async def corpus_totals() -> dict[str, int]:
        return {"documents": 1}

    async def corpus_page(**_: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": "doc-zero",
                "name": "Zero",
                "datasets": ["masumi-network"],
                "created_at": "1",
            }
        ]

    async def corpus_chunk_counts(document_ids: list[str]) -> dict[str, int]:
        return {document_id: 0 for document_id in document_ids}

    async def stored_chunk_budget_check(
        document_ids: list[str] | None,
        *,
        budget: int | None = None,
        datasets: list[str] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        captured["document_ids"] = document_ids
        captured["datasets"] = datasets
        captured["budget"] = budget
        return {
            "ok": True,
            "scope": "full",
            "violation_count": 0,
            "violation_document_counts": {},
            "missing_document_id_violation_count": 0,
        }

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "corpus_chunk_counts", corpus_chunk_counts)
    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)
    monkeypatch.setattr("kb.chunk_window.resolve_chunk_budget", lambda: 256)

    report = await client.corpus_reconciliation_census(dataset="masumi-network")

    assert captured["document_ids"] is None
    assert captured["datasets"] == ["masumi-network"]
    assert report["ok"] is True
    assert report["zero_chunk_count"] == 1
    assert report["zero_chunk_document_ids"] == ["doc-zero"]


@pytest.mark.asyncio
async def test_corpus_reconciliation_census_passes_qdrant_org_wide_dataset_scope(
    monkeypatch: Any,
) -> None:
    """Org-wide Qdrant census has no single dataset argument. Scope comes from
    every dataset name seen on the relational walk."""
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    client = CogneePublicClient()
    captured: dict[str, Any] = {}

    async def corpus_totals() -> dict[str, int]:
        return {"documents": 2}

    async def corpus_page(**_: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": "doc-a",
                "name": "A",
                "datasets": ["masumi-network"],
                "created_at": "1",
            },
            {
                "id": "doc-b",
                "name": "B",
                "datasets": ["seat:alice"],
                "created_at": "2",
            },
        ]

    async def corpus_chunk_counts(document_ids: list[str]) -> dict[str, int]:
        return {document_id: 1 for document_id in document_ids}

    async def stored_chunk_budget_check(
        document_ids: list[str] | None,
        *,
        budget: int | None = None,
        datasets: list[str] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        captured["document_ids"] = document_ids
        captured["datasets"] = datasets
        return {
            "ok": True,
            "scope": "full",
            "violation_count": 0,
            "violation_document_counts": {},
            "missing_document_id_violation_count": 0,
        }

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "corpus_chunk_counts", corpus_chunk_counts)
    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)
    monkeypatch.setattr("kb.chunk_window.resolve_chunk_budget", lambda: 256)

    report = await client.corpus_reconciliation_census()

    assert captured["document_ids"] is None
    assert captured["datasets"] == ["masumi-network", "seat:alice"]
    assert report["ok"] is True


@pytest.mark.asyncio
async def test_graph_chunk_ids_use_source_document_property(monkeypatch: Any) -> None:
    client = CogneePublicClient()
    calls: list[tuple[str, dict[str, Any]]] = []

    class GraphEngine:
        async def query(self, query: str, params: dict[str, Any]) -> list[tuple[str, str, str]]:
            calls.append((query, params))
            return [("chunk-a", "DocumentChunk", json.dumps("doc-a"))]

    async def graph_engine() -> GraphEngine:
        return GraphEngine()

    monkeypatch.setattr(client, "_graph_engine", graph_engine)

    assert await client.graph_chunk_ids_for_documents(["doc-a"]) == {"chunk-a"}
    assert "node_type = 'DocumentChunk'" in calls[0][0]
    assert "RETURN node_id, node_type, document_id_json" in calls[0][0]


@pytest.mark.asyncio
async def test_delete_document_chunks_deletes_independent_graph_and_vector_ids(
    monkeypatch: Any,
) -> None:
    from uuid import UUID

    captured: dict[str, Any] = {}
    graph_id = "graph-node"
    vector_id = "9dbe579d-eccb-51b6-9bba-13982cbaf69f"

    class FakeGraphEngine:
        async def delete_nodes(self, node_ids: list[str]) -> None:
            captured["graph"] = list(node_ids)

    class FakeVectorEngine:
        async def delete_data_points(self, collection: str, ids: list[UUID]) -> None:
            captured["collection"] = collection
            captured["vector"] = list(ids)

    async def get_graph_engine() -> FakeGraphEngine:
        return FakeGraphEngine()

    def get_vector_engine() -> FakeVectorEngine:
        return FakeVectorEngine()

    async def run_migrations() -> None:
        return None

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace(run_migrations=run_migrations))
    monkeypatch.setitem(sys.modules, "cognee.infrastructure", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "cognee.infrastructure.databases", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.graph",
        SimpleNamespace(get_graph_engine=get_graph_engine),
    )
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.vector",
        SimpleNamespace(get_vector_engine=get_vector_engine),
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def _ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    async def stored_ids(_: list[str]) -> list[str]:
        return [vector_id]

    async def graph_ids(_: list[str]) -> set[str]:
        return {graph_id}

    monkeypatch.setattr(client, "stored_chunk_ids_for_documents", stored_ids)
    monkeypatch.setattr(client, "graph_chunk_ids_for_documents", graph_ids)

    async def snapshot_vector_rows(
        vector_engine: Any, vector_ids: list[UUID]
    ) -> list[dict[str, Any]]:
        del vector_engine, vector_ids
        return [{"id": UUID(vector_id), "payload": {"text": "private"}, "vector": [0.1]}]

    async def snapshot_graph_projection(
        graph_engine: Any, graph_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        del graph_engine, graph_ids
        return {
            "nodes": [
                {
                    "id": graph_id,
                    "name": "chunk",
                    "type": "DocumentChunk",
                    "properties": "{}",
                }
            ],
            "edges": [],
        }

    monkeypatch.setattr(client, "_snapshot_vector_rows", snapshot_vector_rows)
    monkeypatch.setattr(client, "_snapshot_graph_projection", snapshot_graph_projection)

    result = await client.delete_document_chunks(["doc-a"])

    assert result["vector_chunk_count"] == 1
    assert result["graph_node_count"] == 1
    assert captured["graph"] == [graph_id]
    assert captured["collection"] == "DocumentChunk_text"
    assert captured["vector"] == [UUID(vector_id)]
    assert result["snapshot_token"]
    assert "vector_rows" not in result
    assert "graph" not in result


@pytest.mark.asyncio
async def test_delete_document_chunks_reports_partial_delete_failure(
    monkeypatch: Any,
) -> None:
    from uuid import UUID

    captured: dict[str, Any] = {}
    graph_id = "graph-node"
    vector_id = "9dbe579d-eccb-51b6-9bba-13982cbaf69f"

    class FakeGraphEngine:
        async def delete_nodes(self, node_ids: list[str]) -> None:
            captured["graph"] = list(node_ids)
            raise RuntimeError("graph delete failed")

    class FakeVectorEngine:
        async def delete_data_points(self, collection: str, ids: list[UUID]) -> None:
            captured["collection"] = collection
            captured["vector"] = list(ids)

    async def get_graph_engine() -> FakeGraphEngine:
        return FakeGraphEngine()

    def get_vector_engine() -> FakeVectorEngine:
        return FakeVectorEngine()

    async def run_migrations() -> None:
        return None

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace(run_migrations=run_migrations))
    monkeypatch.setitem(sys.modules, "cognee.infrastructure", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "cognee.infrastructure.databases", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.graph",
        SimpleNamespace(get_graph_engine=get_graph_engine),
    )
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.vector",
        SimpleNamespace(get_vector_engine=get_vector_engine),
    )

    client = CogneePublicClient()
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def _ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    async def stored_ids(_: list[str]) -> list[str]:
        return [vector_id]

    async def graph_ids(_: list[str]) -> set[str]:
        return {graph_id}

    monkeypatch.setattr(client, "stored_chunk_ids_for_documents", stored_ids)
    monkeypatch.setattr(client, "graph_chunk_ids_for_documents", graph_ids)

    async def snapshot_vector_rows(
        vector_engine: Any, vector_ids: list[UUID]
    ) -> list[dict[str, Any]]:
        del vector_engine, vector_ids
        return [{"id": UUID(vector_id), "payload": {"text": "private"}, "vector": [0.1]}]

    async def snapshot_graph_projection(
        graph_engine: Any, graph_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        del graph_engine, graph_ids
        return {
            "nodes": [{"id": graph_id, "name": "chunk", "type": "DocumentChunk", "properties": "{}"}],
            "edges": [],
        }

    monkeypatch.setattr(client, "_snapshot_vector_rows", snapshot_vector_rows)
    monkeypatch.setattr(client, "_snapshot_graph_projection", snapshot_graph_projection)

    async def restore_snapshot(*_: Any, **__: Any) -> bool:
        captured["restore_attempted"] = True
        return False

    monkeypatch.setattr(client, "_restore_document_chunk_snapshot_locked", restore_snapshot)

    result = await client.delete_document_chunks(["doc-a"])

    assert result["ok"] is False
    assert result["document_ids"] == ["doc-a"]
    assert result["vector_chunk_count"] == 1
    assert result["graph_node_count"] == 1
    assert result["reason"] == "repair_delete_failed"
    assert result["error_type"] == "RuntimeError"
    assert result["projections_preserved"] is False
    assert result["snapshot_token"]
    assert captured["collection"] == "DocumentChunk_text"
    assert captured["vector"] == [UUID(vector_id)]
    assert captured["graph"] == [graph_id]
    assert captured["restore_attempted"] is True


@pytest.mark.asyncio
async def test_restore_document_chunks_uses_private_snapshot_once(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class FakeGraphEngine:
        pass

    class FakeVectorEngine:
        pass

    async def get_graph_engine() -> FakeGraphEngine:
        return FakeGraphEngine()

    def get_vector_engine() -> FakeVectorEngine:
        return FakeVectorEngine()

    async def run_migrations() -> None:
        return None

    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace(run_migrations=run_migrations))
    monkeypatch.setitem(sys.modules, "cognee.infrastructure", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "cognee.infrastructure.databases", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.graph",
        SimpleNamespace(get_graph_engine=get_graph_engine),
    )
    monkeypatch.setitem(
        sys.modules,
        "cognee.infrastructure.databases.vector",
        SimpleNamespace(get_vector_engine=get_vector_engine),
    )

    client = CogneePublicClient()
    client._repair_snapshots["opaque-token"] = {
        "document_ids": ["doc-a"],
        "vector_rows": [{"id": "chunk-a", "payload": {"text": "private"}, "vector": [0.1]}],
        "graph": {"nodes": [], "edges": []},
    }
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)

    async def _ready(_cognee: Any) -> None:
        return None

    monkeypatch.setattr(client, "_ensure_cognee_ready", _ready)

    async def current_vectors(_: list[str]) -> list[str]:
        return []

    async def current_graph(_: list[str]) -> set[str]:
        return set()

    monkeypatch.setattr(client, "stored_chunk_ids_for_documents", current_vectors)
    monkeypatch.setattr(client, "graph_chunk_ids_for_documents", current_graph)

    async def restore_vectors(_: Any, rows: list[dict[str, Any]]) -> None:
        captured["vectors"] = rows

    async def restore_graph(_: Any, graph: Mapping[str, Any]) -> None:
        captured["graph"] = graph

    monkeypatch.setattr(client, "_restore_vector_rows", restore_vectors)
    monkeypatch.setattr(client, "_restore_graph_projection", restore_graph)

    result = await client.restore_document_chunks(
        {"document_ids": ["doc-a"], "snapshot_token": "opaque-token"}
    )

    assert result is True
    assert captured["vectors"][0]["payload"]["text"] == "private"
    assert "opaque-token" not in client._repair_snapshots
    assert await client.restore_document_chunks(
        {"document_ids": ["doc-a"], "snapshot_token": "opaque-token"}
    ) is False


@pytest.mark.asyncio
async def test_recall_does_not_pass_only_context(monkeypatch: Any) -> None:
    # #50: cognee's only_context=True flips the CHUNKS result from the list-of-dicts
    # the callers rely on (result_provenance/_citadel envelope, dedup, drill-down) to
    # a single newline-joined string, and does NOT suppress the per-read history write
    # for CHUNKS. So it must never be passed on the read path — the result shape stays.
    received: dict[str, Any] = {}

    async def run_migrations() -> None:
        return None

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        received["search"] = kwargs
        return [{"ok": True}]

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="chunks"),
            run_migrations=run_migrations,
            search=search,
        ),
    )
    client = CogneePublicClient()

    result = await client.recall("note", dataset="notes")

    assert result == [{"ok": True}]  # list-of-dicts shape preserved
    assert "only_context" not in received["search"]


@pytest.mark.asyncio
async def test_search_timing_logs_only_when_enabled(monkeypatch: Any, caplog: Any) -> None:
    # #50: an opt-in, lightweight per-search wall-time line (setup/recall/total) so the
    # residual node latency can be attributed later. Off by default, INFO when enabled.
    import logging

    async def run_migrations() -> None:
        return None

    async def search(**kwargs: Any) -> list[dict[str, Any]]:
        return [{"ok": True}]

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            SearchType=SimpleNamespace(CHUNKS="chunks"),
            run_migrations=run_migrations,
            search=search,
        ),
    )
    client = CogneePublicClient()

    monkeypatch.delenv("CITADEL_SEARCH_TIMING", raising=False)
    with caplog.at_level(logging.INFO, logger="kb.cognee_client"):
        await client.recall("note", dataset="notes")
    assert "search timing:" not in caplog.text  # silent by default

    caplog.clear()
    monkeypatch.setenv("CITADEL_SEARCH_TIMING", "true")
    with caplog.at_level(logging.INFO, logger="kb.cognee_client"):
        await client.recall("note", dataset="notes", top_k=7)
    assert "search timing:" in caplog.text
    assert "query_type=chunks" in caplog.text
    assert "top_k=7" in caplog.text


def test_cognee_public_client_derives_db_env_from_database_url(monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://db_user:db%23pass@db.example:6543/citadel")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    for key in (
        "DB_PROVIDER",
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USERNAME",
        "DB_PASSWORD",
        "VECTOR_DB_HOST",
        "VECTOR_DB_PORT",
        "VECTOR_DB_NAME",
        "VECTOR_DB_USERNAME",
        "VECTOR_DB_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)

    CogneePublicClient()._prepare_cognee_environment()

    assert os.environ["DB_PROVIDER"] == "postgres"
    assert os.environ["DB_HOST"] == "db.example"
    assert os.environ["DB_PORT"] == "6543"
    assert os.environ["DB_NAME"] == "citadel"
    assert os.environ["DB_USERNAME"] == "db_user"
    assert os.environ["DB_PASSWORD"] == "db#pass"
    assert os.environ["VECTOR_DB_HOST"] == "db.example"
    assert os.environ["VECTOR_DB_PORT"] == "6543"
    assert os.environ["VECTOR_DB_NAME"] == "citadel"
    assert os.environ["VECTOR_DB_USERNAME"] == "db_user"
    assert os.environ["VECTOR_DB_PASSWORD"] == "db#pass"


def test_cognee_public_client_preserves_explicit_vector_db_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("DB_HOST", "relational.example")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "railway")
    monkeypatch.setenv("DB_USERNAME", "postgres")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    monkeypatch.setenv("VECTOR_DB_HOST", "vector.example")

    CogneePublicClient()._prepare_cognee_environment()

    assert os.environ["VECTOR_DB_HOST"] == "vector.example"
    assert os.environ["VECTOR_DB_PORT"] == "5432"
    assert os.environ["VECTOR_DB_NAME"] == "railway"
    assert os.environ["VECTOR_DB_USERNAME"] == "postgres"
    assert os.environ["VECTOR_DB_PASSWORD"] == "secret"


def test_cognee_public_client_derives_postgres_graph_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("DB_HOST", "postgres.example")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "railway")
    monkeypatch.setenv("DB_USERNAME", "postgres")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("GRAPH_DATABASE_PROVIDER", "postgres")

    CogneePublicClient()._prepare_cognee_environment()

    assert os.environ["GRAPH_DATABASE_HOST"] == "postgres.example"
    assert os.environ["GRAPH_DATABASE_PORT"] == "5432"
    assert os.environ["GRAPH_DATABASE_NAME"] == "railway"
    assert os.environ["GRAPH_DATABASE_USERNAME"] == "postgres"
    assert os.environ["GRAPH_DATABASE_PASSWORD"] == "secret"


async def _seed_corpus(user: Any) -> dict[str, Any]:
    """Three real Data rows: two owned by the default user sharing one
    created_at (so the id tie-break is exercised), one under ANOTHER owner —
    the row an owner-scoped read would silently drop."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from cognee.infrastructure.databases.relational import get_relational_engine
    from cognee.modules.data.models import Data, Dataset, DatasetData

    t_tied = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_late = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    tied_ids = sorted((uuid4(), uuid4()), key=lambda item: item.hex)
    other_owner = uuid4()
    other_data_id = uuid4()

    central = Dataset(
        id=uuid4(), name="masumi-network", owner_id=user.id, tenant_id=user.tenant_id
    )
    ghost = Dataset(
        id=uuid4(), name="seat:ghost", owner_id=other_owner, tenant_id=user.tenant_id
    )
    engine = get_relational_engine()
    async with engine.get_async_session() as session:
        session.add(central)
        session.add(ghost)
        session.add(
            Data(
                id=tied_ids[0],
                name="first",
                content_hash="hash-1",
                mime_type="text/plain",
                token_count=10,
                data_size=100,
                created_at=t_tied,
                owner_id=user.id,
                tenant_id=user.tenant_id,
                external_metadata={"citadel_tags": ["alpha", "beta"]},
            )
        )
        session.add(
            Data(
                id=tied_ids[1],
                name="second",
                content_hash="hash-2",
                mime_type="text/plain",
                created_at=t_tied,
                owner_id=user.id,
                tenant_id=user.tenant_id,
            )
        )
        session.add(
            Data(
                id=other_data_id,
                name="ghost-note",
                content_hash="hash-3",
                mime_type="text/plain",
                created_at=t_late,
                owner_id=other_owner,
                tenant_id=user.tenant_id,
            )
        )
        session.add(DatasetData(dataset_id=central.id, data_id=tied_ids[0]))
        session.add(DatasetData(dataset_id=central.id, data_id=tied_ids[1]))
        session.add(DatasetData(dataset_id=ghost.id, data_id=other_data_id))
        await session.commit()
    return {
        "t_tied": t_tied,
        "tied_ids": tied_ids,
        "other_owner": other_owner,
        "other_data_id": other_data_id,
    }


@pytest.mark.asyncio
async def test_corpus_page_enumerates_the_real_store_with_keyset_pages(
    cognee_sqlite: Any, monkeypatch: Any
) -> None:
    """The census read against the REAL relational stack.

    Pins the row shape, the (created_at, id) ordering with the id tie-break,
    the keyset boundary handoff, tag parsing out of external_metadata, and —
    what the owner-scoped attribution reads above deliberately never claim —
    that a row held by ANOTHER owner_id still enumerates.
    """
    from cognee.infrastructure.databases.relational import create_db_and_tables
    from cognee.modules.users.methods import get_default_user

    await create_db_and_tables()
    user = await get_default_user()
    seeded = await _seed_corpus(user)
    client = _real_cognee_client(monkeypatch)

    page_one = await client.corpus_page(limit=2)

    assert [row["id"] for row in page_one] == [str(uid) for uid in seeded["tied_ids"]]
    first = page_one[0]
    assert first["name"] == "first"
    assert first["content_hash"] == "hash-1"
    assert first["mime_type"] == "text/plain"
    assert first["token_count"] == 10
    assert first["data_size"] == 100
    assert first["created_at"] == seeded["t_tied"].isoformat()
    assert first["datasets"] == ["masumi-network"]
    assert first["citadel_tags"] == ["alpha", "beta"]

    boundary = page_one[-1]
    page_two = await client.corpus_page(
        after_created_at=boundary["created_at"],
        after_id=boundary["id"],
        limit=2,
    )

    assert [row["id"] for row in page_two] == [str(seeded["other_data_id"])]
    assert page_two[0]["datasets"] == ["seat:ghost"]
    assert page_two[0]["owner_id"] == str(seeded["other_owner"])

    page_three = await client.corpus_page(
        after_created_at=page_two[0]["created_at"],
        after_id=page_two[0]["id"],
        limit=2,
    )

    assert page_three == []


@pytest.mark.asyncio
async def test_corpus_totals_reports_the_owner_split(
    cognee_sqlite: Any, monkeypatch: Any
) -> None:
    """Both counts, so a row under another owner_id shows up as a difference
    instead of silently missing from an owner-scoped number."""
    from cognee.infrastructure.databases.relational import create_db_and_tables
    from cognee.modules.users.methods import get_default_user

    await create_db_and_tables()
    user = await get_default_user()
    await _seed_corpus(user)
    client = _real_cognee_client(monkeypatch)

    totals = await client.corpus_totals()

    assert totals == {
        "documents": 3,
        "documents_default_owner": 2,
        "documents_other_owners": 1,
        "by_dataset": {"masumi-network": 2, "seat:ghost": 1},
        "by_dataset_default_owner": {"masumi-network": 2},
    }


@pytest.mark.asyncio
async def test_corpus_chunk_counts_says_not_measured_off_pgvector(
    monkeypatch: Any,
) -> None:
    """None, never 0: on a node whose vector provider is not pgvector there is
    no chunk table to count, and a 0 would read as 'accepted but never
    indexed' — a claim nothing measured. An empty page needs no store at all
    and is an honest empty measurement."""
    monkeypatch.delenv("VECTOR_DB_PROVIDER", raising=False)
    client = _real_cognee_client(monkeypatch)

    assert await client.corpus_chunk_counts(["3b9c0d05-0000-0000-0000-000000000001"]) is None
    assert await client.corpus_chunk_counts([]) == {}


@pytest.mark.asyncio
async def test_corpus_chunk_counts_uses_dataset_scoped_qdrant_census(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    client = _real_cognee_client(monkeypatch)
    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    calls: list[tuple[list[str], list[str]]] = []

    async def dataset_membership_for_documents(
        document_ids: list[str],
    ) -> dict[str, list[str]]:
        assert document_ids == ["doc-a", "doc-b"]
        return {
            "doc-a": ["seat:alice"],
            "doc-b": ["seat:bob"],
        }

    async def qdrant_census(**kwargs: Any) -> dict[str, Any]:
        calls.append((kwargs["document_ids"], kwargs["datasets"]))
        document_id = kwargs["document_ids"][0]
        return {"document_chunk_counts": {document_id: 2}}

    monkeypatch.setattr(
        client,
        "dataset_membership_for_documents",
        dataset_membership_for_documents,
    )
    monkeypatch.setattr(client, "_stored_qdrant_chunk_budget_check", qdrant_census)

    counts = await client.corpus_chunk_counts(["doc-a", "doc-b"])

    assert counts == {"doc-a": 2, "doc-b": 2}
    assert calls == [
        (["doc-a"], ["seat:alice"]),
        (["doc-b"], ["seat:bob"]),
    ]


@pytest.mark.asyncio
async def test_corpus_health_walks_keyset_pages_and_unions_projection_checks(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()
    monkeypatch.delenv("CITADEL_CORPUS_HEALTH_MAX_DOCUMENTS", raising=False)
    pages = [
        [
            {"id": "doc-a", "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": "doc-b", "created_at": "2026-01-02T00:00:00+00:00"},
        ],
        [{"id": "doc-c", "created_at": "2026-01-03T00:00:00+00:00"}],
    ]
    page_calls: list[tuple[str | None, str | None, int]] = []
    chunk_calls: list[list[str]] = []
    graph_calls: list[tuple[list[str], list[str]]] = []

    async def corpus_totals() -> dict[str, Any]:
        return {"documents": 3}

    async def corpus_page(**kwargs: Any) -> list[dict[str, Any]]:
        page_calls.append(
            (kwargs["after_created_at"], kwargs["after_id"], kwargs["limit"])
        )
        return pages[len(page_calls) - 1]

    async def corpus_chunk_counts(document_ids: list[str]) -> dict[str, int]:
        chunk_calls.append(document_ids)
        return {document_id: 2 for document_id in document_ids}

    async def dataset_membership_for_documents(
        document_ids: list[str],
    ) -> dict[str, list[str]]:
        assert document_ids == ["doc-a", "doc-b", "doc-c"]
        return {
            "doc-a": ["central"],
            "doc-b": ["seat:alice"],
            "doc-c": ["central", "seat:alice"],
        }

    async def corpus_graph_presence(
        document_ids: list[str],
        *,
        datasets: list[str],
    ) -> set[str]:
        graph_calls.append((document_ids, datasets))
        return set(document_ids) | {"stale-graph-node"}

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "corpus_chunk_counts", corpus_chunk_counts)
    monkeypatch.setattr(
        client,
        "dataset_membership_for_documents",
        dataset_membership_for_documents,
    )
    monkeypatch.setattr(client, "corpus_graph_presence", corpus_graph_presence)

    health = await client.corpus_health(limit=2)

    assert page_calls == [
        (None, None, 2),
        ("2026-01-02T00:00:00+00:00", "doc-b", 1),
    ]
    assert chunk_calls == [["doc-a", "doc-b", "doc-c"]]
    assert graph_calls == [
        (["doc-a", "doc-c"], ["central"]),
        (["doc-b", "doc-c"], ["seat:alice"]),
    ]
    assert health == {
        "relational_documents": 3,
        "probe_limit": 2,
        "probe_max_documents": 10_000,
        "probe_documents": 3,
        "probe_pages": 2,
        "probe_complete": True,
        "probe_cap_exceeded": False,
        "probe_chunked_documents": 3,
        "probe_graph_documents": 3,
        "probe_fully_indexed_documents": 3,
        "probe_ok": True,
    }


@pytest.mark.asyncio
async def test_corpus_health_fails_closed_before_document_cap(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()
    monkeypatch.setenv("CITADEL_CORPUS_HEALTH_MAX_DOCUMENTS", "2")

    async def corpus_totals() -> dict[str, Any]:
        return {"documents": 3}

    async def corpus_page(**_: Any) -> list[dict[str, Any]]:
        raise AssertionError("cap must be checked before page enumeration")

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)

    health = await client.corpus_health()

    assert health["relational_documents"] == 3
    assert health["probe_max_documents"] == 2
    assert health["probe_documents"] == 0
    assert health["probe_cap_exceeded"] is True
    assert health["probe_complete"] is False
    assert health["probe_ok"] is False


@pytest.mark.asyncio
async def test_corpus_health_fails_closed_when_vector_measurement_is_unavailable(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()

    async def corpus_totals() -> dict[str, Any]:
        return {"documents": 1}

    async def corpus_page(**_: Any) -> list[dict[str, Any]]:
        return [{"id": "doc-a"}]

    async def corpus_chunk_counts(_: list[str]) -> None:
        return None

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_page", corpus_page)
    monkeypatch.setattr(client, "corpus_chunk_counts", corpus_chunk_counts)

    with pytest.raises(RuntimeError, match="vector chunk measurement is unavailable"):
        await client.corpus_health()


@pytest.mark.asyncio
async def test_standard_cognee_document_graph_contains_relational_data_id() -> None:
    from uuid import NAMESPACE_OID, uuid4, uuid5

    from cognee.modules.chunking.models import DocumentChunk
    from cognee.modules.data.processing.document_types import TextDocument
    from cognee.modules.graph.utils import get_graph_from_model

    data_id = uuid4()
    document = TextDocument(
        id=data_id,
        name="document",
        raw_data_location="document.txt",
        external_metadata="{}",
        mime_type="text/plain",
    )
    chunk = DocumentChunk(
        id=uuid5(NAMESPACE_OID, f"{data_id}-0"),
        text="document",
        chunk_size=1,
        chunk_index=0,
        cut_type="sentence_end",
        is_part_of=document,
        contains=[],
        document_id=str(data_id),
        document_name="document",
    )

    nodes, edges = await get_graph_from_model(chunk)

    assert str(data_id) in {str(node.id) for node in nodes}
    assert any(str(source) == str(chunk.id) and str(target) == str(data_id) for source, target, *_ in edges)


@pytest.mark.asyncio
async def test_corpus_health_empty_corpus_is_complete(monkeypatch: Any) -> None:
    client = CogneePublicClient()
    graph_calls = 0

    async def corpus_totals() -> dict[str, Any]:
        return {"documents": 0}

    async def corpus_graph_presence(_: list[str]) -> set[str]:
        nonlocal graph_calls
        graph_calls += 1
        return set()

    monkeypatch.setattr(client, "corpus_totals", corpus_totals)
    monkeypatch.setattr(client, "corpus_graph_presence", corpus_graph_presence)

    health = await client.corpus_health()

    assert health == {
        "relational_documents": 0,
        "probe_limit": 64,
        "probe_max_documents": 10_000,
        "probe_documents": 0,
        "probe_pages": 0,
        "probe_complete": True,
        "probe_cap_exceeded": False,
        "probe_chunked_documents": 0,
        "probe_graph_documents": 0,
        "probe_fully_indexed_documents": 0,
        "probe_ok": True,
    }
    assert graph_calls == 0


@pytest.mark.asyncio
async def test_corpus_graph_presence_uses_source_document_property(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()
    calls: list[tuple[str, dict[str, Any]]] = []

    class GraphEngine:
        async def query(self, query: str, params: dict[str, Any]) -> list[tuple[str]]:
            calls.append((query, params))
            return [(json.dumps("doc-a"),)]

        async def get_nodes(self, _: list[str]) -> list[dict[str, Any]]:
            raise AssertionError("source Data.id must not be used as graph node id")

    async def graph_engine() -> GraphEngine:
        return GraphEngine()

    monkeypatch.setattr(client, "_graph_engine", graph_engine)

    assert await client.corpus_graph_presence(["doc-a", "doc-b"]) == {"doc-a"}
    assert len(calls) == 1
    query, params = calls[0]
    assert "json_extract(n.properties, '$.document_id')" in query
    assert "node_type = 'DocumentChunk'" in query
    assert "RETURN DISTINCT document_id_json" in query
    assert set(params["document_ids_json"]) == {json.dumps("doc-a"), json.dumps("doc-b")}


@pytest.mark.asyncio
async def test_corpus_graph_presence_enters_explicit_dataset_context(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()
    active_dataset: list[str] = []
    contexts: list[tuple[str, str]] = []

    class Dataset:
        id = UUID("11111111-1111-1111-1111-111111111111")
        owner_id = UUID("22222222-2222-2222-2222-222222222222")

    class DatasetContext:
        async def __aenter__(self) -> None:
            active_dataset.append(str(Dataset.id))
            contexts.append((str(Dataset.id), str(Dataset.owner_id)))

        async def __aexit__(self, *_: Any) -> None:
            active_dataset.pop()

    async def resolve_datasets(
        names: list[str],
        _: Any,
    ) -> tuple[None, list[Dataset]]:
        assert names == ["lifecycle-live"]
        return None, [Dataset()]

    def set_context(dataset_id: UUID, owner_id: UUID) -> DatasetContext:
        assert (str(dataset_id), str(owner_id)) == (
            str(Dataset.id),
            str(Dataset.owner_id),
        )
        return DatasetContext()

    class GraphEngine:
        async def query(self, _: str, __: dict[str, Any]) -> list[tuple[str]]:
            assert active_dataset == [str(Dataset.id)]
            return [(json.dumps("doc-a"),)]

    async def graph_engine() -> GraphEngine:
        assert active_dataset == [str(Dataset.id)]
        return GraphEngine()

    import cognee.context_global_variables as context_module
    import cognee.modules.pipelines.layers.resolve_authorized_user_datasets as resolver_module

    monkeypatch.setattr(
        context_module,
        "set_database_global_context_variables",
        set_context,
    )
    monkeypatch.setattr(
        resolver_module,
        "resolve_authorized_user_datasets",
        resolve_datasets,
    )
    monkeypatch.setattr(client, "_graph_engine", graph_engine)

    assert await client.corpus_graph_presence(
        ["doc-a"],
        datasets=["lifecycle-live"],
    ) == {"doc-a"}
    assert contexts == [(str(Dataset.id), str(Dataset.owner_id))]


@pytest.mark.asyncio
async def test_corpus_graph_presence_without_query_is_unmeasured(
    monkeypatch: Any,
) -> None:
    client = CogneePublicClient()

    class GraphEngine:
        async def get_nodes(self, _: list[str]) -> list[dict[str, Any]]:
            return [{"id": "doc-a"}]

    async def graph_engine() -> GraphEngine:
        return GraphEngine()

    monkeypatch.setattr(client, "_graph_engine", graph_engine)

    assert await client.corpus_graph_presence(["doc-a"]) is None


# ---- drill-down under per-dataset stores (ADR-0020) ---------------------------


@pytest.mark.asyncio
async def test_document_graph_probes_each_provisioned_store(monkeypatch: Any) -> None:
    """The ambient-context targeted read misses ids living in other stores.

    Under ENABLE_BACKEND_ACCESS_CONTROL each dataset has its own graph
    database and ``get_node`` against whatever store the task last touched
    returned None — no error, no fallback — so /api/documents 404'd
    DocumentChunk and TextSummary ids the mesh itself displayed (verified
    live 2026-08-13, QdrantScopeError in the deploy log once the chunk-store
    fallback also failed). The targeted read must probe each provisioned
    store, first hit wins.
    """
    client = CogneePublicClient()
    owner = uuid4()
    dataset_a, dataset_b = sorted((uuid4(), uuid4()), key=str)
    probe_order: list[UUID] = []

    async def ensure_ready(_: Any) -> None:
        return None

    async def provisioned() -> list[tuple[UUID, UUID]]:
        return [(dataset_a, owner), (dataset_b, owner)]

    async def targeted(
        dataset_id: UUID, owner_id: UUID, document_id: str
    ) -> tuple[list[Any], list[Any]]:
        assert owner_id == owner
        assert document_id == "chunk-1"
        probe_order.append(dataset_id)
        if dataset_id == dataset_b:
            return (
                [("chunk-1", {"text": "chunk text", "type": "DocumentChunk"})],
                [("chunk-1", "doc-1", "is_part_of", {})],
            )
        return ([], [])

    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)
    monkeypatch.setattr(client, "_provisioned_dataset_databases", provisioned)
    monkeypatch.setattr(client, "_targeted_read_for_dataset", targeted, raising=False)
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace())

    nodes, edges = await client._document_graph("chunk-1")

    assert probe_order == [dataset_a, dataset_b]
    assert [str(node_id) for node_id, _ in nodes] == ["chunk-1"]
    assert edges == [("chunk-1", "doc-1", "is_part_of", {})]


@pytest.mark.asyncio
async def test_document_graph_store_miss_is_not_a_fallback(monkeypatch: Any) -> None:
    """An id no store resolves returns empty WITHOUT the full graph read — a
    full read cannot contain a node the stores lack; the chunk-store fallback
    owns what happens next."""
    client = CogneePublicClient()
    owner = uuid4()

    async def ensure_ready(_: Any) -> None:
        return None

    async def provisioned() -> list[tuple[UUID, UUID]]:
        return [(uuid4(), owner)]

    async def targeted(
        dataset_id: UUID, owner_id: UUID, document_id: str
    ) -> tuple[list[Any], list[Any]]:
        return ([], [])

    async def forbidden_graph_data() -> tuple[list[Any], list[Any]]:
        raise AssertionError("full graph read must not run on a store miss")

    monkeypatch.setattr(client, "_prepare_cognee_environment", lambda: None)
    monkeypatch.setattr(client, "_ensure_cognee_ready", ensure_ready)
    monkeypatch.setattr(client, "_provisioned_dataset_databases", provisioned)
    monkeypatch.setattr(client, "_targeted_read_for_dataset", targeted, raising=False)
    monkeypatch.setattr(client, "graph_data", forbidden_graph_data)
    monkeypatch.setitem(sys.modules, "cognee", SimpleNamespace())

    assert await client._document_graph("ghost-id") == ([], [])


@pytest.mark.asyncio
async def test_chunk_store_reader_probes_provisioned_stores_for_membershipless_ids(
    monkeypatch: Any,
) -> None:
    """A chunk id has no DatasetData row, so the reader fell through to the
    unbound engine — which refuses every unscoped operation under the qdrant
    provider (QdrantScopeError, the measured drill-down 404). With no
    membership rows the reader must probe the provisioned dataset stores
    instead of giving up."""
    client = CogneePublicClient()
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "qdrant")
    unbound_sentinel = object()

    async def owning(doc_id: str) -> list[tuple[Any, Any]]:
        return []

    async def provisioned() -> list[tuple[UUID, UUID]]:
        return [(uuid4(), uuid4())]

    async def unbound() -> Any:
        return unbound_sentinel

    monkeypatch.setattr(client, "_owning_datasets", owning)
    monkeypatch.setattr(client, "_provisioned_dataset_databases", provisioned)
    monkeypatch.setattr(client, "_unbound_chunk_store_reader", unbound)

    reader = await client._chunk_store_reader(
        "6ffe277d-be23-5ad3-8c09-045f9fce20cf"
    )

    assert reader is not unbound_sentinel
    assert callable(reader)


@pytest.mark.asyncio
async def test_get_document_summary_carries_mappable_owner_ids(
    monkeypatch: Any,
) -> None:
    """TextSummary -[made_from]-> chunk -[is_part_of]-> document: only the
    document id is a relational Data.id the read-scope map keys on. Without
    it in dataset_node_ids the ADR-0009 gate fail-closed on summaries the
    caller may read (the measured TextSummary 404)."""
    client = CogneePublicClient()
    graphs: dict[str, tuple[list[Any], list[Any]]] = {
        "summary-1": (
            [
                ("summary-1", {"type": "TextSummary", "text": "a summary"}),
                ("chunk-1", {"type": "DocumentChunk", "text": "chunk text"}),
            ],
            [("summary-1", "chunk-1", "made_from", {})],
        ),
        "chunk-1": (
            [
                ("chunk-1", {"type": "DocumentChunk", "text": "chunk text"}),
                ("doc-1", {"type": "TextDocument", "name": "parent doc"}),
            ],
            [("chunk-1", "doc-1", "is_part_of", {})],
        ),
    }

    async def fake_graph(document_id: str) -> tuple[list[Any], list[Any]]:
        return graphs.get(str(document_id), ([], []))

    monkeypatch.setattr(client, "_document_graph", fake_graph)

    document = await client.get_document("summary-1")

    assert document is not None
    assert document["body"] == "a summary"
    assert "chunk-1" in document["dataset_node_ids"]
    assert "doc-1" in document["dataset_node_ids"]


@pytest.mark.asyncio
async def test_get_document_summary_chunk_scope_serves_summary_text(
    monkeypatch: Any,
) -> None:
    """?scope=chunk on a TextSummary id: its own text is the trivially
    available result of the same read (summaries never assemble a parent), so
    body and owner ids match the default scope instead of erroring."""
    client = CogneePublicClient()
    graphs: dict[str, tuple[list[Any], list[Any]]] = {
        "summary-1": (
            [
                ("summary-1", {"type": "TextSummary", "text": "a summary"}),
                ("chunk-1", {"type": "DocumentChunk", "text": "chunk text"}),
            ],
            [("summary-1", "chunk-1", "made_from", {})],
        ),
        "chunk-1": (
            [
                ("chunk-1", {"type": "DocumentChunk", "text": "chunk text"}),
                ("doc-1", {"type": "TextDocument", "name": "parent doc"}),
            ],
            [("chunk-1", "doc-1", "is_part_of", {})],
        ),
    }

    async def fake_graph(document_id: str) -> tuple[list[Any], list[Any]]:
        return graphs.get(str(document_id), ([], []))

    monkeypatch.setattr(client, "_document_graph", fake_graph)

    document = await client.get_document("summary-1", chunk_scope=True)

    assert document is not None
    assert document["body"] == "a summary"
    assert "doc-1" in document["dataset_node_ids"]


# ---- stored check vs the lifecycle drain (#286) -------------------------------


def _cognify_fakes(
    monkeypatch: Any,
    *,
    missing: list[str],
    violations: int,
    processed_by_dataset: dict[str, list[str]] | None = None,
) -> Any:
    """Client whose cognify receipt swept one doc and whose census reports it."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    processed = processed_by_dataset or {"dataset-id": missing}

    async def run_migrations() -> None:
        return None

    async def cognify(**_: Any) -> dict[str, Any]:
        return {
            dataset: SimpleNamespace(
                data_ingestion_info=[{"data_id": doc} for doc in document_ids]
            )
            for dataset, document_ids in processed.items()
        }

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(run_migrations=run_migrations, cognify=cognify),
    )
    client = CogneePublicClient()

    async def stored_chunk_budget_check(**_: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "violation_count": violations,
            "chunks_scanned": 2094,
            "budget": 256,
            "violations": (
                [
                    {
                        "reason": "chunk_over_budget",
                        "chunk_id": "chunk-1",
                        "document_id": "doc-oversized",
                        "measured_tokens": 999,
                    }
                ]
                if violations
                else []
            ),
            "missing_document_ids": list(missing),
        }

    monkeypatch.setattr(client, "stored_chunk_budget_check", stored_chunk_budget_check)
    return client


@pytest.mark.asyncio
async def test_cognify_missing_docs_with_active_projection_do_not_fail(
    monkeypatch: Any, caplog: Any
) -> None:
    """The live 18:02Z failure (#286): zero violations, one missing id whose
    lifecycle projection was still pending. Phase-2 incremental cognify sweeps
    in-flight docs into its receipt while the drain still owns their chunk
    writes — in-flight is not a gap, so the check must warn, not raise."""
    live_id = "4552e276-e329-5dbf-9d45-d029160d82f4"
    client = _cognify_fakes(monkeypatch, missing=[live_id], violations=0)
    looked_up: list[list[str]] = []

    def lookup(document_ids: list[str]) -> tuple[set[str], set[str]]:
        looked_up.append(list(document_ids))
        return {live_id}, set()

    client.lifecycle_projection_state_lookup = lookup

    with caplog.at_level("WARNING"):
        await client.cognify(datasets=["notes"])  # must not raise

    assert looked_up == [[live_id]]
    warning = next(
        record.getMessage()
        for record in caplog.records
        if "in flight" in record.getMessage()
    )
    assert live_id in warning  # the ids are named, not just counted


@pytest.mark.asyncio
async def test_cognify_missing_empty_document_does_not_fail_the_pass(
    monkeypatch: Any, caplog: Any
) -> None:
    """A 52-byte masumi-network row with 0 chunks (live id 4552e276-...) made
    cognify verify=True raise RuntimeError and stamp /readyz unhealthy forever.
    Zero budget violations plus a missing id is a per-document gap, not a
    global stored-chunk failure.
    """
    live_id = "4552e276-e329-5dbf-9d45-d029160d82f4"
    client = _cognify_fakes(monkeypatch, missing=[live_id], violations=0)
    client.lifecycle_projection_state_lookup = lambda document_ids: (set(), set())

    with caplog.at_level("WARNING"):
        await client.cognify(datasets=["notes"])  # must not raise

    assert any(
        live_id in record.getMessage() and "per-document" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_cognify_rechecks_missing_doc_after_projection_completes(
    monkeypatch: Any, caplog: Any
) -> None:
    """A completed job can make the first census stale before its state lookup."""
    live_id = "4552e276-e329-5dbf-9d45-d029160d82f4"
    client = _cognify_fakes(
        monkeypatch,
        missing=[live_id],
        violations=0,
        processed_by_dataset={"notes": [live_id], "other": ["other-id"]},
    )
    first_check = client.stored_chunk_budget_check
    checks: list[dict[str, Any]] = []

    async def completing_check(**kwargs: Any) -> dict[str, Any]:
        checks.append(kwargs)
        result = await first_check(**kwargs)
        if len(checks) == 2:
            return {
                **result,
                "ok": True,
                "chunks_scanned": 1,
                "missing_document_ids": [],
            }
        return result

    monkeypatch.setattr(client, "stored_chunk_budget_check", completing_check)
    client.lifecycle_projection_state_lookup = lambda document_ids: (
        set(),
        {live_id},
    )

    with caplog.at_level("WARNING"):
        await client.cognify(datasets=["notes", "other"])

    assert len(checks) == 2
    assert checks[1]["document_ids"] == [live_id]
    assert checks[1]["datasets"] == ["notes"]
    assert checks[1]["document_ids_by_dataset"] == {"notes": [live_id]}
    assert any(
        "cleared after lifecycle projection completion" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_cognify_violations_fail_even_when_missing_ids_are_in_flight(
    monkeypatch: Any,
) -> None:
    """A budget violation is fatal regardless of drain state; the message
    still notes the in-flight ids so the operator sees both facts."""
    client = _cognify_fakes(monkeypatch, missing=["doc-pending"], violations=1)
    client.lifecycle_projection_state_lookup = lambda document_ids: (
        {"doc-pending"},
        set(),
    )

    with pytest.raises(RuntimeError) as excinfo:
        await client.cognify(datasets=["notes"])

    message = str(excinfo.value)
    assert "doc-oversized" in message
    assert "still in lifecycle projection" in message
