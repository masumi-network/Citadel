"""Drill-down resolution tests for CogneePublicClient.get_document.

Measured defect (2026-08-02, prod node): a hit id that search retrieved from
the durable chunk store returned document_drilldown_available=false and
/api/documents 404'd, while a control id resolved fine — an answer can be
retrievable in search and unreachable through drill-down. Two code paths are
under test here:

1. GRAPH: a CHUNKS search hit id IS a DocumentChunk id. get_document used to
   return only that chunk's own text (the same fragment search already
   showed); it must resolve the ``is_part_of`` parent and return the whole
   document.
2. CHUNK-STORE FALLBACK: search reads the "DocumentChunk_text" vector
   collection; drill-down used to read ONLY the graph. When the graph cannot
   resolve an id that the chunk store holds, get_document must assemble the
   document from the chunk store instead of returning None (HTTP 404).

Fixture honesty ("a fake cannot specify a shape you don't own"): the graph
tests run against the REAL cognee LadybugAdapter (the kuzu provider prod
uses) over a throwaway on-disk database, written through cognee's real batch
writers (add_nodes/add_edges) — the same get_node/get_connections shapes,
undirected-match direction loss included, that production serves. The vector
store cannot be exercised for real without an embedding engine, so its seam
is a mirror of the REAL retrieve() contract verified against cognee 1.2.2
(PGVectorAdapter.retrieve / LanceDBAdapter.retrieve): ScoredResult rows with
``id``/``payload`` attributes, only requested ids returned, [] for an
unknown collection. What only a live node can prove: that the measured id
53915a09-610b-5ad2-9c61-d66f7f5986ad now resolves — post-deploy, GET
/api/documents/<that id> must return 200 with the full README body and
metadata.assembled_from == "chunk_store".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_OID, UUID, uuid5

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb.cognee_client import CogneePublicClient  # noqa: E402

pytestmark = pytest.mark.asyncio

DOC_ID = str(uuid5(NAMESPACE_OID, "drilldown-doc"))
CHUNK_IDS = [str(uuid5(NAMESPACE_OID, f"{DOC_ID}-{index}")) for index in range(3)]
ENTITY_ID = str(uuid5(NAMESPACE_OID, "drilldown-entity"))
# In the chunk store but never written to the graph (the measured 404 class).
ORPHAN_DOC_ID = str(uuid5(NAMESPACE_OID, "orphan-doc"))
ORPHAN_CHUNK_IDS = [
    str(uuid5(NAMESPACE_OID, f"{ORPHAN_DOC_ID}-{index}")) for index in range(4)
]
FULL_BODY = "part zero\n\npart one\n\npart two"


class _DataPoint:
    """Duck-typed stand-in accepted by LadybugAdapter.add_nodes (model_dump)."""

    def __init__(self, dump: dict[str, Any]) -> None:
        self._dump = dump

    def model_dump(self) -> dict[str, Any]:
        return dict(self._dump)


@pytest.fixture(scope="module")
def real_graph(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A REAL LadybugAdapter over a temp kuzu db, holding the cognify topology.

    Written through cognee's own batch writers so the on-disk shape (and the
    get_node/get_connections read shapes) are exactly what production serves:
    a textless TextDocument node, text-bearing DocumentChunk nodes, edges in
    the real chunk --is_part_of--> document direction plus a reversed
    document --is_part_of--> chunk edge (the adapter's undirected match makes
    direction unobservable, so the assembly must not care), and a textless
    Entity reachable from a chunk via ``contains``.
    """
    ladybug = pytest.importorskip(
        "cognee.infrastructure.databases.graph.ladybug.adapter"
    )
    import asyncio

    adapter = ladybug.LadybugAdapter(
        db_path=str(tmp_path_factory.mktemp("kuzu") / "graph.kuzu"),
        kuzu_buffer_pool_size=1 << 28,
        kuzu_max_db_size=1 << 30,
    )

    async def _seed() -> None:
        await adapter.add_nodes(
            [
                _DataPoint(
                    {
                        "id": DOC_ID,
                        "name": "README.md",
                        "type": "TextDocument",
                        "mime_type": "text/plain",
                    }
                ),
                _DataPoint(
                    {
                        "id": CHUNK_IDS[0],
                        "name": "DocumentChunk",
                        "type": "DocumentChunk",
                        "text": "part zero",
                        "chunk_index": 0,
                        "document_id": DOC_ID,
                    }
                ),
                _DataPoint(
                    {
                        "id": CHUNK_IDS[1],
                        "name": "DocumentChunk",
                        "type": "DocumentChunk",
                        "text": "part one",
                        "chunk_index": 1,
                        "document_id": DOC_ID,
                    }
                ),
                _DataPoint(
                    {
                        "id": CHUNK_IDS[2],
                        "name": "DocumentChunk",
                        "type": "DocumentChunk",
                        "text": "part two",
                        "chunk_index": 2,
                        "document_id": DOC_ID,
                    }
                ),
                _DataPoint(
                    {
                        "id": ENTITY_ID,
                        "name": "SomeEntity",
                        "type": "Entity",
                        "description": "textless entity",
                    }
                ),
            ]
        )
        await adapter.add_edges(
            [
                # Real cognify direction: chunk --is_part_of--> document.
                (CHUNK_IDS[0], DOC_ID, "is_part_of", {}),
                (CHUNK_IDS[1], DOC_ID, "is_part_of", {}),
                # Deliberately REVERSED endpoint order for one chunk: the
                # traversal must be direction-agnostic.
                (DOC_ID, CHUNK_IDS[2], "is_part_of", {}),
                (CHUNK_IDS[0], ENTITY_ID, "contains", {}),
            ]
        )

    asyncio.run(_seed())
    yield adapter
    asyncio.run(adapter.close())


def _client_over(graph_engine: Any) -> CogneePublicClient:
    client = CogneePublicClient()

    async def _engine() -> Any:
        return graph_engine

    client._graph_engine = _engine  # type: ignore[method-assign]
    return client


class _ScoredResult:
    """Mirror of cognee's vector ScoredResult rows (id + payload + score)."""

    def __init__(self, row_id: UUID, payload: dict[str, Any]) -> None:
        self.id = row_id
        self.payload = payload
        self.score = 0


class _ChunkStore:
    """Mirror of the REAL vector retrieve() contract (cognee 1.2.2).

    PGVectorAdapter.retrieve / LanceDBAdapter.retrieve: return only rows whose
    id was requested, [] when the collection is unknown, rows shaped as
    ScoredResult. This fake refuses to invent rows: content must be seeded by
    id, exactly like the store it mirrors.
    """

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = {str(k): dict(v) for k, v in rows.items()}
        self.calls: list[tuple[str, list[str]]] = []

    async def retrieve(self, collection_name: str, data_point_ids: list[Any]) -> list[Any]:
        self.calls.append((collection_name, [str(i) for i in data_point_ids]))
        if collection_name != "DocumentChunk_text":
            return []
        out = []
        for row_id in data_point_ids:
            payload = self._rows.get(str(row_id))
            if payload is not None:
                out.append(_ScoredResult(UUID(str(row_id)), payload))
        return out


def _install_chunk_store(
    client: CogneePublicClient, store: _ChunkStore
) -> None:
    async def _engine() -> _ChunkStore:
        return store

    client._vector_engine = _engine  # type: ignore[method-assign]


def _orphan_store() -> _ChunkStore:
    texts = ["orphan zero", "orphan one", "orphan two", "orphan three"]
    return _ChunkStore(
        {
            chunk_id: {
                "id": chunk_id,
                "text": texts[index],
                "chunk_index": index,
                "document_id": ORPHAN_DOC_ID,
                "document_name": "sokosumi-cli-readme.md",
            }
            for index, chunk_id in enumerate(ORPHAN_CHUNK_IDS)
        }
    )


def _empty_store() -> _ChunkStore:
    return _ChunkStore({})


# --------------------------------------------------------------------------
# Graph path, REAL adapter: chunk-id hits must resolve the whole document.
# --------------------------------------------------------------------------


async def test_chunk_id_resolves_full_parent_document(real_graph: Any) -> None:
    """A CHUNKS hit id (a DocumentChunk id) must return the WHOLE document.

    This is the fragment defect: the pre-fix code returned only the chunk's
    own text ("part one"), i.e. drill-down re-served the search snippet and
    the remaining lines of the document stayed unreachable.
    """
    client = _client_over(real_graph)
    _install_chunk_store(client, _empty_store())

    document = await client.get_document(CHUNK_IDS[1])

    assert document is not None
    assert document["body"] == FULL_BODY  # not just "part one"
    assert document["chunk_count"] == 3
    # Scope attribution must carry the parent document id (the map key) and
    # keep the requested chunk id.
    assert DOC_ID in document["dataset_node_ids"]
    assert CHUNK_IDS[1] in document["dataset_node_ids"]


async def test_document_id_assembles_chunks_both_edge_directions(
    real_graph: Any,
) -> None:
    """Textless document node assembles ALL chunks, one edge written reversed."""
    client = _client_over(real_graph)
    _install_chunk_store(client, _empty_store())

    document = await client.get_document(DOC_ID)

    assert document is not None
    assert document["body"] == FULL_BODY  # includes the reversed-edge chunk
    assert document["chunk_count"] == 3
    assert document["dataset_node_ids"] == [DOC_ID]


async def test_textless_entity_still_resolves_none(real_graph: Any) -> None:
    """Entities adjacent to text chunks via ``contains`` stay a 404.

    Guards against the parent-follow/fallback fabricating a document for a
    textless entity. The chunk store legitimately has no row for it either.
    """
    client = _client_over(real_graph)
    _install_chunk_store(client, _empty_store())

    assert await client.get_document(ENTITY_ID) is None


# --------------------------------------------------------------------------
# Chunk-store fallback: graph-missing ids must resolve from the durable
# store search reads (the measured 404).
# --------------------------------------------------------------------------


async def test_graph_missing_chunk_id_resolves_from_chunk_store(
    real_graph: Any,
) -> None:
    """The measured defect: searchable chunk, no graph node, drill-down 404'd.

    The requested id exists ONLY in the chunk store. get_document must
    assemble the full document from the store (all four sibling chunks,
    ordered), not return None.
    """
    client = _client_over(real_graph)
    store = _orphan_store()
    _install_chunk_store(client, store)

    document = await client.get_document(ORPHAN_CHUNK_IDS[2])

    assert document is not None
    assert document["body"] == "orphan zero\n\norphan one\n\norphan two\n\norphan three"
    assert document["chunk_count"] == 4
    assert document["metadata"]["assembled_from"] == "chunk_store"
    # ADR-0009 scope attribution: the parent document id is the relational
    # Data.id the node_dataset_map keys on; it must lead dataset_node_ids.
    assert document["dataset_node_ids"][0] == ORPHAN_DOC_ID
    assert ORPHAN_CHUNK_IDS[2] in document["dataset_node_ids"]
    # The fallback read the SAME collection search reads.
    assert store.calls and all(
        collection == "DocumentChunk_text" for collection, _ in store.calls
    )


async def test_graph_missing_document_id_resolves_from_chunk_store(
    real_graph: Any,
) -> None:
    """A document id with no graph node resolves via deterministic sibling ids."""
    client = _client_over(real_graph)
    _install_chunk_store(client, _orphan_store())

    document = await client.get_document(ORPHAN_DOC_ID)

    assert document is not None
    assert document["id"] == ORPHAN_DOC_ID
    assert document["chunk_count"] == 4
    assert document["body"].startswith("orphan zero")
    assert document["dataset_node_ids"][0] == ORPHAN_DOC_ID


async def test_id_absent_everywhere_resolves_none(real_graph: Any) -> None:
    """No graph node, no chunk-store row: the 404 stays honest."""
    client = _client_over(real_graph)
    _install_chunk_store(client, _empty_store())

    ghost = str(uuid5(NAMESPACE_OID, "ghost-node"))
    assert await client.get_document(ghost) is None


async def test_chunk_store_failure_degrades_to_none(real_graph: Any) -> None:
    """A broken vector engine must degrade to the pre-fix 404, never raise."""
    client = _client_over(real_graph)

    class _Exploding:
        async def retrieve(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            raise RuntimeError("vector store down")

    async def _engine() -> Any:
        return _Exploding()

    client._vector_engine = _engine  # type: ignore[method-assign]

    ghost = str(uuid5(NAMESPACE_OID, "ghost-node"))
    assert await client.get_document(ghost) is None


async def test_non_uuid_id_skips_chunk_store(real_graph: Any) -> None:
    """Synthetic ids (chunk:<sha>, ghsync:*) never touch the vector engine."""
    client = _client_over(real_graph)
    store = _empty_store()
    _install_chunk_store(client, store)

    assert await client.get_document("chunk:deadbeef") is None
    assert store.calls == []


async def test_healthy_graph_never_consults_chunk_store(real_graph: Any) -> None:
    """Ids the graph resolves must not pay for vector reads (drill-down hint
    resolution calls get_document once per unique hit id per search)."""
    client = _client_over(real_graph)
    store = _orphan_store()
    _install_chunk_store(client, store)

    document = await client.get_document(DOC_ID)

    assert document is not None
    assert store.calls == []
