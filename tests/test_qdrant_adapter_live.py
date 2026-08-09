from __future__ import annotations

import os
from uuid import uuid4

import pytest

from kb.qdrant_adapter import CitadelQdrantAdapter, IndexSchema, qdrant_scope


class _EmbeddingEngine:
    async def embed_text(self, values: list[str]) -> list[list[float]]:
        return [self._vector(value) for value in values]

    @staticmethod
    def _vector(value: str) -> list[float]:
        return [1.0, 0.0, 0.0] if "alice" in value.lower() else [0.0, 1.0, 0.0]

    def get_vector_size(self) -> int:
        return 3

    def get_batch_size(self) -> int:
        return 16


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_qdrant_keeps_same_raw_id_isolated_by_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("CITADEL_QDRANT_LIVE_URL")
    if not url:
        pytest.skip("set CITADEL_QDRANT_LIVE_URL to run the real Qdrant contract")
    monkeypatch.delenv("CITADEL_GENERATION_ID", raising=False)
    generation = f"live-{uuid4()}"
    alice = "seat:alice"
    bob = "seat:bob"
    raw_id = uuid4()
    logical_collection = "DocumentChunk_text"
    adapter = CitadelQdrantAdapter(
        url=url,
        api_key=os.getenv("CITADEL_QDRANT_LIVE_KEY", "disposable-live-test-key"),
        embedding_engine=_EmbeddingEngine(),
        database_name=generation,
    )

    with qdrant_scope(mode="write", generation_id=generation, dataset=alice):
        await adapter.create_data_points(
            logical_collection,
            [IndexSchema(id=raw_id, text="alice private marker")],
        )
    with qdrant_scope(mode="write", generation_id=generation, dataset=bob):
        await adapter.create_data_points(
            logical_collection,
            [IndexSchema(id=raw_id, text="bob private marker")],
        )

    with qdrant_scope(mode="read", generation_id=generation, dataset=alice):
        alice_count = await adapter.count_data_points(logical_collection)
        alice_rows = await adapter.retrieve(logical_collection, [str(raw_id)])
        alice_hits = await adapter.search(
            logical_collection,
            query_vector=[1.0, 0.0, 0.0],
            limit=5,
            include_payload=True,
        )
    with qdrant_scope(mode="read", generation_id=generation, dataset=bob):
        bob_count = await adapter.count_data_points(logical_collection)
        bob_rows = await adapter.retrieve(logical_collection, [str(raw_id)])
        bob_hits = await adapter.search(
            logical_collection,
            query_vector=[0.0, 1.0, 0.0],
            limit=5,
            include_payload=True,
        )

    assert alice_count == bob_count == 1
    assert alice_rows[0].payload["text"] == "alice private marker"
    assert bob_rows[0].payload["text"] == "bob private marker"
    assert [hit.payload["text"] for hit in alice_hits] == ["alice private marker"]
    assert [hit.payload["text"] for hit in bob_hits] == ["bob private marker"]

    with qdrant_scope(mode="write", generation_id=generation, dataset=alice):
        await adapter.delete_data_points(logical_collection, [raw_id])
    with qdrant_scope(mode="read", generation_id=generation, dataset=alice):
        assert await adapter.count_data_points(logical_collection) == 0
    with qdrant_scope(mode="read", generation_id=generation, dataset=bob):
        assert await adapter.count_data_points(logical_collection) == 1
    with qdrant_scope(mode="admin", generation_id=generation, dataset=bob):
        await adapter.prune()
