from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from kb.qdrant_adapter import (
    CitadelQdrantAdapter,
    CitadelQdrantDatasetDatabaseHandler,
    IndexSchema,
    QdrantConfigurationError,
    QdrantProviderError,
    QdrantScopeError,
    physical_collection_name,
    qdrant_document_scope,
    qdrant_scope,
    register_qdrant_adapter,
)


GENERATION = "generation-test"
LOGICAL_COLLECTION = "DocumentChunk_text"
ALICE = "seat:alice"
BOB = "seat:bob"


class _EmbeddingEngine:
    async def embed_text(self, values: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in values]

    def get_vector_size(self) -> int:
        return 3

    def get_batch_size(self) -> int:
        return 16


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.collections: set[str] = set()
        self.collection_exists_result = True
        self.retrieve_records: list[object] = []
        self.scroll_records: list[object] = []
        self.next_offset: str | int | UUID | None = None
        self.query_records: list[object] = []
        self.batch_records: list[list[object]] = []
        self.count_value = 0
        self.fail_on: set[str] = set()

    def _record(self, method: str, **kwargs: object) -> None:
        self.calls.append((method, kwargs))
        if method in self.fail_on:
            raise RuntimeError(f"provider {method} failed")

    def kwargs_for(self, method: str) -> list[dict[str, object]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    async def collection_exists(self, collection_name: str) -> bool:
        self._record("collection_exists", collection_name=collection_name)
        return self.collection_exists_result or collection_name in self.collections

    async def create_collection(self, **kwargs: object) -> None:
        self._record("create_collection", **kwargs)
        self.collections.add(str(kwargs["collection_name"]))

    async def create_payload_index(self, **kwargs: object) -> None:
        self._record("create_payload_index", **kwargs)

    async def retrieve(self, **kwargs: object) -> list[object]:
        self._record("retrieve", **kwargs)
        return self.retrieve_records

    async def upsert(self, **kwargs: object) -> None:
        self._record("upsert", **kwargs)

    async def query_points(self, **kwargs: object) -> object:
        self._record("query_points", **kwargs)
        return SimpleNamespace(points=self.query_records)

    async def query_batch_points(self, **kwargs: object) -> list[object]:
        self._record("query_batch_points", **kwargs)
        return [SimpleNamespace(points=records) for records in self.batch_records]

    async def delete(self, **kwargs: object) -> object:
        self._record("delete", **kwargs)
        return SimpleNamespace(status="completed")

    async def count(self, **kwargs: object) -> object:
        self._record("count", **kwargs)
        return SimpleNamespace(count=self.count_value)

    async def scroll(self, **kwargs: object) -> tuple[list[object], object]:
        self._record("scroll", **kwargs)
        return self.scroll_records, self.next_offset

    async def get_collections(self) -> object:
        self._record("get_collections")
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in sorted(self.collections)]
        )

    async def delete_collection(self, **kwargs: object) -> None:
        self._record("delete_collection", **kwargs)

    async def set_payload(self, **kwargs: object) -> None:
        self._record("set_payload", **kwargs)

    async def close(self) -> None:
        self._record("close")


def _adapter(client: _FakeClient) -> CitadelQdrantAdapter:
    return CitadelQdrantAdapter(
        url="http://127.0.0.1:6333",
        api_key="test-key",
        embedding_engine=_EmbeddingEngine(),
        database_name=GENERATION,
        client_factory=lambda: client,
    )


def _bound_adapter(
    client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dataset: str = ALICE,
) -> CitadelQdrantAdapter:
    monkeypatch.setenv("CITADEL_GENERATION_ID", GENERATION)
    return CitadelQdrantAdapter(
        url="http://127.0.0.1:6333",
        api_key="test-key",
        embedding_engine=_EmbeddingEngine(),
        database_name=dataset,
        client_factory=lambda: client,
    )


def _scope_values(filter_value: object) -> dict[str, object]:
    values: dict[str, object] = {}
    for condition in getattr(filter_value, "must", None) or []:
        key = getattr(condition, "key", None)
        match = getattr(condition, "match", None)
        if key is not None and match is not None and hasattr(match, "value"):
            values[str(key)] = match.value
    return values


def _filtered_ids(filter_value: object) -> list[object]:
    for condition in getattr(filter_value, "must", None) or []:
        if hasattr(condition, "has_id"):
            return list(condition.has_id)
    return []


def _matched_any(filter_value: object, key: str) -> list[object]:
    for condition in getattr(filter_value, "must", None) or []:
        if getattr(condition, "key", None) != key:
            continue
        match = getattr(condition, "match", None)
        if match is not None and hasattr(match, "any"):
            return list(match.any)
    return []


def _assert_tenant_filter(filter_value: object, dataset: str = ALICE) -> None:
    expected = {
        "citadel_generation_id": GENERATION,
        "citadel_dataset_scope": dataset,
    }
    assert expected.items() <= _scope_values(filter_value).items()


def _point(raw_id: UUID, *, text: str = "private text") -> IndexSchema:
    return IndexSchema(
        id=raw_id,
        text=text,
        document_id="document-17",
        document_name="design.md",
        chunk_index=4,
        source_chunk_id="source-chunk-4",
        importance_weight=0.75,
        metadata={
            "index_fields": ["text"],
            "source_uri": "github://org/repo/design.md",
            "revision": "commit-abc",
        },
        belongs_to_set=["github-central"],
        source_pipeline="github-sync",
        source_task="chunk",
        source_node_set="github-central",
        source_user=ALICE,
        source_content_hash="sha256:abc",
    )


async def _stored_id_for(
    adapter: CitadelQdrantAdapter,
    client: _FakeClient,
    raw_id: UUID,
    *,
    dataset: str = ALICE,
) -> object:
    with qdrant_scope(mode="write", generation_id=GENERATION, dataset=dataset):
        await adapter.create_data_points(LOGICAL_COLLECTION, [_point(raw_id)])
    points = [
        point
        for kwargs in client.kwargs_for("upsert")
        for point in kwargs["points"]
        if point.payload["citadel_dataset_scope"] == dataset
    ]
    assert points
    return points[-1].id


def test_physical_collection_is_shared_by_datasets_within_generation() -> None:
    alice = physical_collection_name(GENERATION, ALICE, LOGICAL_COLLECTION)
    bob = physical_collection_name(GENERATION, BOB, LOGICAL_COLLECTION)

    assert alice == bob
    assert alice != physical_collection_name(
        "generation-next", ALICE, LOGICAL_COLLECTION
    )
    assert alice != physical_collection_name(GENERATION, ALICE, "Entity_name")


@pytest.mark.asyncio
async def test_new_collection_uses_tenant_hnsw_and_scope_indexes() -> None:
    client = _FakeClient()
    client.collection_exists_result = False
    adapter = _adapter(client)

    with qdrant_scope(mode="write", generation_id=GENERATION, dataset=ALICE):
        await adapter.create_collection(LOGICAL_COLLECTION)

    creation = client.kwargs_for("create_collection")
    assert len(creation) == 1
    hnsw_config = creation[0]["hnsw_config"]
    assert hnsw_config.m == 0
    assert hnsw_config.payload_m == 16

    indexes = client.kwargs_for("create_payload_index")
    assert [index["field_name"] for index in indexes] == [
        "citadel_generation_id",
        "citadel_dataset_scope",
        "document_id",
    ]
    assert indexes[1]["field_schema"].is_tenant is True


def test_read_without_scope_fails_before_provider_request() -> None:
    client = _FakeClient()
    adapter = _adapter(client)

    with pytest.raises(QdrantScopeError, match="scope"):
        adapter.scoped_collection(LOGICAL_COLLECTION, required_mode="read")

    assert client.calls == []


def test_conflicting_nested_scope_fails_closed() -> None:
    with qdrant_scope(mode="read", generation_id=GENERATION, dataset=ALICE):
        with pytest.raises(QdrantScopeError, match="conflict"):
            with qdrant_scope(mode="read", generation_id=GENERATION, dataset=BOB):
                pass


def test_write_scope_is_exactly_one_dataset() -> None:
    adapter = _adapter(_FakeClient())

    with qdrant_scope(mode="write", generation_id=GENERATION, dataset=ALICE):
        assert adapter.current_dataset(required_mode="write") == ALICE


def test_ebac_bound_adapter_uses_dataset_without_task_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _bound_adapter(_FakeClient(), monkeypatch)

    assert adapter.generation_id == GENERATION
    assert adapter.current_dataset(required_mode="read") == ALICE
    assert adapter.scoped_collection(LOGICAL_COLLECTION, required_mode="read") == (
        physical_collection_name(GENERATION, ALICE, LOGICAL_COLLECTION)
    )


def test_embedding_probe_allows_unbound_adapter_but_data_operations_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CITADEL_GENERATION_ID", GENERATION)
    adapter = CitadelQdrantAdapter(
        url="http://127.0.0.1:6333",
        api_key="test-key",
        embedding_engine=_EmbeddingEngine(),
        database_name="",
        client_factory=lambda: _FakeClient(),
    )

    assert adapter.generation_id == GENERATION
    assert adapter.bound_dataset is None
    with pytest.raises(QdrantScopeError, match="explicit Citadel scope"):
        adapter.current_dataset(required_mode="read")


def test_ebac_bound_adapter_rejects_conflicting_task_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _bound_adapter(_FakeClient(), monkeypatch)

    with qdrant_scope(mode="read", generation_id=GENERATION, dataset=BOB):
        with pytest.raises(QdrantScopeError, match="dataset"):
            adapter.current_dataset(required_mode="read")


def test_registration_requires_ebac_qdrant_handler_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    monkeypatch.setenv("VECTOR_DATASET_DATABASE_HANDLER", "qdrant")
    monkeypatch.delenv("CITADEL_GENERATION_ID", raising=False)

    with pytest.raises(QdrantScopeError, match="generation"):
        register_qdrant_adapter()


def test_registration_installs_citadel_adapter_and_dataset_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cognee.infrastructure.databases.dataset_database_handler as handler_module
    import cognee.infrastructure.databases.vector as vector_module
    import kb.qdrant_adapter as adapter_module

    registered: list[tuple[str, object]] = []
    registered_handlers: list[tuple[str, object, str]] = []
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    monkeypatch.setenv("VECTOR_DATASET_DATABASE_HANDLER", "qdrant")
    monkeypatch.setenv("CITADEL_GENERATION_ID", GENERATION)
    monkeypatch.setattr(adapter_module, "version", lambda _: "1.19.0")
    monkeypatch.setattr(
        vector_module,
        "use_vector_adapter",
        lambda name, adapter: registered.append((name, adapter)),
    )
    monkeypatch.setattr(
        handler_module,
        "use_dataset_database_handler",
        lambda name, handler, provider: registered_handlers.append(
            (name, handler, provider)
        ),
    )

    register_qdrant_adapter()

    assert registered == [("qdrant", CitadelQdrantAdapter)]
    assert registered_handlers == [
        ("qdrant", CitadelQdrantDatasetDatabaseHandler, "qdrant")
    ]


@pytest.mark.asyncio
async def test_dataset_handler_binds_qdrant_connection_to_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kb.qdrant_adapter as adapter_module

    dataset_id = uuid4()
    monkeypatch.setattr(
        adapter_module,
        "get_vectordb_config",
        lambda: SimpleNamespace(
            vector_db_provider="qdrant",
            vector_db_url="http://qdrant:6333",
            vector_db_key="secret",
        ),
    )

    created = await CitadelQdrantDatasetDatabaseHandler.create_dataset(dataset_id, None)

    assert created == {
        "vector_database_provider": "qdrant",
        "vector_database_url": "http://qdrant:6333",
        "vector_database_key": "secret",
        "vector_database_name": str(dataset_id),
        "vector_dataset_database_handler": "qdrant",
    }


@pytest.mark.asyncio
async def test_dataset_handler_rejects_missing_id_and_wrong_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kb.qdrant_adapter as adapter_module

    with pytest.raises(QdrantConfigurationError, match="dataset ID"):
        await CitadelQdrantDatasetDatabaseHandler.create_dataset(None, None)

    monkeypatch.setattr(
        adapter_module,
        "get_vectordb_config",
        lambda: SimpleNamespace(vector_db_provider="pgvector"),
    )
    with pytest.raises(QdrantConfigurationError, match="VECTOR_DB_PROVIDER"):
        await CitadelQdrantDatasetDatabaseHandler.create_dataset(uuid4(), None)


@pytest.mark.asyncio
async def test_dataset_handler_prunes_only_the_bound_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kb.qdrant_adapter as adapter_module

    created_with: dict[str, object] = {}
    pruned: list[bool] = []

    async def prune() -> None:
        pruned.append(True)

    def create_vector_engine(**kwargs: object) -> object:
        created_with.update(kwargs)
        return SimpleNamespace(prune=prune)

    monkeypatch.setattr(adapter_module, "create_vector_engine", create_vector_engine)
    dataset_database = SimpleNamespace(
        vector_database_provider="qdrant",
        vector_database_url="http://qdrant:6333",
        vector_database_key="secret",
        vector_database_name=str(uuid4()),
    )

    await CitadelQdrantDatasetDatabaseHandler.delete_dataset(dataset_database)

    assert created_with == {
        "vector_db_provider": "qdrant",
        "vector_db_url": "http://qdrant:6333",
        "vector_db_key": "secret",
        "vector_db_name": dataset_database.vector_database_name,
    }
    assert pruned == [True]


@pytest.mark.asyncio
async def test_same_raw_id_is_deterministically_isolated_by_dataset() -> None:
    raw_id = uuid4()
    client = _FakeClient()
    adapter = _adapter(client)

    alice_first = await _stored_id_for(adapter, client, raw_id, dataset=ALICE)
    alice_second = await _stored_id_for(adapter, client, raw_id, dataset=ALICE)
    bob = await _stored_id_for(adapter, client, raw_id, dataset=BOB)

    assert alice_first == alice_second
    assert alice_first != bob
    upserts = client.kwargs_for("upsert")
    assert {str(kwargs["collection_name"]) for kwargs in upserts} == {
        physical_collection_name(GENERATION, ALICE, LOGICAL_COLLECTION)
    }
    for kwargs in upserts:
        for point in kwargs["points"]:
            assert point.payload["citadel_original_id"] == str(raw_id)


@pytest.mark.asyncio
async def test_index_payload_preserves_cognee_chunk_provenance() -> None:
    raw_id = uuid4()
    client = _FakeClient()
    adapter = _adapter(client)

    with qdrant_scope(mode="write", generation_id=GENERATION, dataset=ALICE):
        await adapter.index_data_points("DocumentChunk", "text", [_point(raw_id)])

    points = [
        point
        for kwargs in client.kwargs_for("upsert")
        for point in kwargs["points"]
        if point.payload["citadel_dataset_scope"] == ALICE
    ]
    assert points
    payload = points[-1].payload
    expected = {
        "citadel_original_id": str(raw_id),
        "document_id": "document-17",
        "document_name": "design.md",
        "chunk_index": 4,
        "source_chunk_id": "source-chunk-4",
        "importance_weight": 0.75,
        "source_pipeline": "github-sync",
        "source_task": "chunk",
        "source_node_set": "github-central",
        "source_user": ALICE,
        "source_content_hash": "sha256:abc",
    }
    assert {key: payload.get(key) for key in expected} == expected
    assert payload["metadata"] == {
        "index_fields": ["text"],
        "source_uri": "github://org/repo/design.md",
        "revision": "commit-abc",
    }


@pytest.mark.asyncio
async def test_search_uses_shared_collection_and_mandatory_tenant_filter() -> None:
    client = _FakeClient()
    adapter = _adapter(client)

    with qdrant_scope(mode="read", generation_id=GENERATION, dataset=ALICE):
        assert await adapter.search(LOGICAL_COLLECTION, query_vector=[1.0, 0.0, 0.0]) == []

    request = client.kwargs_for("query_points")[-1]
    assert request["collection_name"] == physical_collection_name(
        GENERATION, BOB, LOGICAL_COLLECTION
    )
    _assert_tenant_filter(request["query_filter"])
    assert client.kwargs_for("close")


@pytest.mark.asyncio
async def test_search_filters_lifecycle_documents_before_qdrant_ranking() -> None:
    client = _FakeClient()
    adapter = _adapter(client)

    with qdrant_scope(mode="read", generation_id=GENERATION, dataset=ALICE):
        with qdrant_document_scope(["current-a", "current-b"]):
            await adapter.search(LOGICAL_COLLECTION, query_vector=[1.0, 0.0, 0.0])

    request = client.kwargs_for("query_points")[-1]
    assert _matched_any(request["query_filter"], "document_id") == [
        "current-a",
        "current-b",
    ]

    calls_before_empty_scope = len(client.kwargs_for("query_points"))
    with qdrant_scope(mode="read", generation_id=GENERATION, dataset=ALICE):
        with qdrant_document_scope([]):
            assert (
                await adapter.search(
                    LOGICAL_COLLECTION,
                    query_vector=[1.0, 0.0, 0.0],
                )
                == []
            )
    assert len(client.kwargs_for("query_points")) == calls_before_empty_scope


@pytest.mark.asyncio
async def test_retrieve_derives_stored_ids_and_returns_raw_ids() -> None:
    raw_id = uuid4()
    client = _FakeClient()
    adapter = _adapter(client)
    stored_id = await _stored_id_for(adapter, client, raw_id)
    client.calls.clear()
    client.scroll_records = [
        SimpleNamespace(
            id=stored_id,
            payload={
                "citadel_original_id": str(raw_id),
                "citadel_generation_id": GENERATION,
                "citadel_dataset_scope": ALICE,
            },
        )
    ]

    with qdrant_scope(mode="read", generation_id=GENERATION, dataset=ALICE):
        results = await adapter.retrieve(LOGICAL_COLLECTION, [str(raw_id)])

    request = client.kwargs_for("scroll")[-1]
    assert request["collection_name"] == physical_collection_name(
        GENERATION, BOB, LOGICAL_COLLECTION
    )
    _assert_tenant_filter(request["scroll_filter"])
    assert [str(point_id) for point_id in _filtered_ids(request["scroll_filter"])] == [
        str(stored_id)
    ]
    assert results[0].id == raw_id
    assert client.kwargs_for("close")


@pytest.mark.asyncio
async def test_delete_derives_stored_ids_and_keeps_tenant_filter() -> None:
    raw_id = uuid4()
    client = _FakeClient()
    adapter = _adapter(client)
    stored_id = await _stored_id_for(adapter, client, raw_id)
    client.calls.clear()

    with qdrant_scope(mode="write", generation_id=GENERATION, dataset=ALICE):
        await adapter.delete_data_points(LOGICAL_COLLECTION, [raw_id])

    request = client.kwargs_for("delete")[-1]
    assert request["collection_name"] == physical_collection_name(
        GENERATION, BOB, LOGICAL_COLLECTION
    )
    delete_filter = request["points_selector"].filter
    _assert_tenant_filter(delete_filter)
    assert [str(point_id) for point_id in _filtered_ids(delete_filter)] == [
        str(stored_id)
    ]
    assert client.kwargs_for("close")


@pytest.mark.asyncio
async def test_count_and_scroll_are_tenant_filtered() -> None:
    raw_id = uuid4()
    client = _FakeClient()
    adapter = _adapter(client)
    stored_id = await _stored_id_for(adapter, client, raw_id)
    client.calls.clear()
    client.count_value = 1
    client.scroll_records = [
        SimpleNamespace(
            id=stored_id,
            payload={
                "citadel_original_id": str(raw_id),
                "citadel_generation_id": GENERATION,
                "citadel_dataset_scope": ALICE,
            },
        )
    ]

    with qdrant_scope(mode="read", generation_id=GENERATION, dataset=ALICE):
        assert await adapter.count_data_points(LOGICAL_COLLECTION) == 1
        results, next_offset = await adapter.scroll_data_points(LOGICAL_COLLECTION)

    count_request = client.kwargs_for("count")[-1]
    scroll_request = client.kwargs_for("scroll")[-1]
    assert count_request["collection_name"] == scroll_request["collection_name"]
    assert count_request["collection_name"] == physical_collection_name(
        GENERATION, BOB, LOGICAL_COLLECTION
    )
    _assert_tenant_filter(count_request["count_filter"])
    _assert_tenant_filter(scroll_request["scroll_filter"])
    assert results[0].id == raw_id
    assert next_offset is None
    assert len(client.kwargs_for("close")) >= 4


@pytest.mark.asyncio
async def test_prune_deletes_only_scoped_points_from_shared_collection() -> None:
    client = _FakeClient()
    shared = physical_collection_name(GENERATION, ALICE, LOGICAL_COLLECTION)
    client.collections = {shared}
    adapter = _adapter(client)

    with qdrant_scope(mode="write", generation_id=GENERATION, dataset=ALICE):
        await adapter.prune()

    deletes = client.kwargs_for("delete")
    assert deletes
    assert {request["collection_name"] for request in deletes} == {shared}
    for request in deletes:
        _assert_tenant_filter(request["points_selector"].filter)
    assert client.kwargs_for("delete_collection") == []
    assert client.kwargs_for("close")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_message"),
    [
        ("search", "Qdrant search failed"),
        ("batch_search", "Qdrant batch search failed"),
    ],
)
async def test_search_failures_are_typed_and_close_client(
    operation: str, expected_message: str
) -> None:
    client = _FakeClient()
    client.fail_on.add(
        "query_points" if operation == "search" else "query_batch_points"
    )
    if operation == "batch_search":
        client.batch_records = [[]]
        client.count_value = 1
    adapter = _adapter(client)

    with qdrant_scope(mode="read", generation_id=GENERATION, dataset=ALICE):
        with pytest.raises(QdrantProviderError, match=expected_message):
            if operation == "search":
                await adapter.search(
                    LOGICAL_COLLECTION, query_vector=[1.0, 0.0, 0.0]
                )
            else:
                await adapter.batch_search(LOGICAL_COLLECTION, ["private query"])

    assert client.kwargs_for("close")
