from __future__ import annotations

import json
from typing import Any

from kb.knowledge_mesh import KnowledgeMesh, build_graph_payload, fallback_graph


class FakeGraphGateway:
    def __init__(
        self,
        nodes: list[Any] | None = None,
        edges: list[Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.nodes = nodes or []
        self.edges = edges or []
        self.error = error
        self.calls = 0

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        self.calls += 1
        if self.error:
            raise self.error
        return self.nodes, self.edges


NODES = [
    ("node-1", {"name": "Citadel", "type": "Entity"}),
    ("node-2", {"label": "Cognee", "node_type": "Tool"}),
    ("node-3", {}),
]
EDGES = [
    ("node-1", "node-2", "uses", {}),
    ("node-2", "node-3", "", {}),
    ("node-1", "node-missing", "dangles", {}),
]


async def test_graph_maps_cognee_tuples_to_nodes_and_edges() -> None:
    mesh = KnowledgeMesh(FakeGraphGateway(NODES, EDGES))

    graph = await mesh.graph()

    assert graph["ok"] is True
    assert graph["fallback"] is False
    assert graph["nodes"] == [
        {
            "id": "node-1",
            "label": "Citadel",
            "type": "Entity",
            "trust_tier": "unattested",
        },
        {
            "id": "node-2",
            "label": "Cognee",
            "type": "Tool",
            "trust_tier": "unattested",
        },
        {
            "id": "node-3",
            "label": "node-3",
            "type": "node",
            "trust_tier": "unattested",
        },
    ]
    assert {"source": "node-1", "target": "node-2", "relationship": "uses"} in graph["edges"]
    # Empty relationship names fall back to "related"; dangling edges are dropped.
    assert {"source": "node-2", "target": "node-3", "relationship": "related"} in graph["edges"]
    assert all(edge["target"] != "node-missing" for edge in graph["edges"])
    assert graph["truncated"] is False


async def test_graph_caps_nodes_and_drops_edges_outside_the_cap() -> None:
    mesh = KnowledgeMesh(FakeGraphGateway(NODES, EDGES))

    graph = await mesh.graph(limit=2)

    assert [node["id"] for node in graph["nodes"]] == ["node-1", "node-2"]
    assert graph["edges"] == [{"source": "node-1", "target": "node-2", "relationship": "uses"}]
    assert graph["truncated"] is True
    assert graph["total_nodes"] == 3


async def test_empty_cognee_graph_returns_fallback() -> None:
    mesh = KnowledgeMesh(FakeGraphGateway([], []))

    graph = await mesh.graph()

    assert graph["ok"] is True
    assert graph["fallback"] is True
    assert graph["fallback_reason"] == "graph_empty"
    assert graph["nodes"] == []
    assert graph["edges"] == []


async def test_graph_engine_errors_never_raise() -> None:
    mesh = KnowledgeMesh(FakeGraphGateway(error=RuntimeError("kuzu offline")))

    graph = await mesh.graph()

    assert graph["fallback"] is True
    assert graph["fallback_reason"] == "graph_engine_error:RuntimeError"


async def test_gateway_without_graph_access_returns_fallback() -> None:
    class NoGraphGateway:
        pass

    for gateway in (NoGraphGateway(), None):
        graph = await KnowledgeMesh(gateway).graph()
        assert graph["fallback"] is True
        assert graph["fallback_reason"] == "graph_access_unavailable"


def test_build_graph_payload_skips_malformed_rows() -> None:
    payload = build_graph_payload(
        [("ok-node", {"name": "Fine"}), None, 42, ("dupe", {}), ("dupe", {})],
        [None, ("ok-node", "dupe", "links", {})],
        limit=10,
    )

    assert [node["id"] for node in payload["nodes"]] == ["ok-node", "dupe"]
    assert payload["edges"] == [{"source": "ok-node", "target": "dupe", "relationship": "links"}]


def test_fallback_graph_shape() -> None:
    graph = fallback_graph("graph_empty")

    assert graph == {
        "ok": True,
        "nodes": [],
        "edges": [],
        "total_nodes": 0,
        "total_edges": 0,
        "truncated": False,
        "fallback": True,
        "fallback_reason": "graph_empty",
    }


# --- dataset attribution (seat:<slug> datasets + synthetic hubs) -------------


class FakeDatasetGateway(FakeGraphGateway):
    def __init__(
        self,
        nodes: list[Any] | None = None,
        edges: list[Any] | None = None,
        *,
        dataset_map: dict[str, list[str]] | None = None,
        map_error: Exception | None = None,
    ) -> None:
        super().__init__(nodes, edges)
        self.dataset_map = dataset_map or {}
        self.map_error = map_error

    async def node_dataset_map(self) -> dict[str, list[str]]:
        if self.map_error:
            raise self.map_error
        return self.dataset_map


DOC_NODES = [
    ("doc-1", {"name": "Design Doc", "type": "TextDocument"}),
    ("chunk-1", {"text": "chunk body", "type": "DocumentChunk"}),
]
DOC_EDGES = [("chunk-1", "doc-1", "is_part_of", {})]


def test_build_graph_payload_without_dataset_map_is_unchanged() -> None:
    baseline = build_graph_payload(NODES, EDGES, limit=10)
    payload = build_graph_payload(NODES, EDGES, limit=10, dataset_map=None)

    assert payload == baseline
    assert all("dataset" not in node for node in payload["nodes"])
    assert all(node["type"] != "dataset" for node in payload["nodes"])
    assert all(edge["relationship"] != "belongs_to" for edge in payload["edges"])


def test_dataset_map_tags_document_and_appends_hub() -> None:
    payload = build_graph_payload(
        DOC_NODES,
        [],
        limit=10,
        dataset_map={"doc-1": ["seat:alice"]},
    )

    doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
    assert doc["dataset"] == "seat:alice"
    assert doc["datasets"] == ["seat:alice"]
    hub = next(node for node in payload["nodes"] if node["id"] == "dataset:seat:alice")
    assert hub == {
        "id": "dataset:seat:alice",
        "label": "seat:alice",
        "type": "dataset",
        "dataset": "seat:alice",
    }
    assert {
        "source": "doc-1",
        "target": "dataset:seat:alice",
        "relationship": "belongs_to",
    } in payload["edges"]
    # Hubs are synthetic: raw-count semantics unchanged.
    assert payload["total_nodes"] == 2
    assert payload["truncated"] is False


def test_graph_trust_and_promotion_metadata_are_fail_closed() -> None:
    payload = build_graph_payload(
        [
            (
                "central-doc",
                {
                    "name": "Central",
                    "type": "TextDocument",
                    "external_metadata": json.dumps(
                        {
                            "citadel_attestation": {
                                "promoted_by": "admin-7",
                                "promoted_at": "2026-08-06T12:00:00+00:00",
                            }
                        }
                    ),
                },
            ),
            ("trace-doc", {"name": "Trace", "type": "TextDocument"}),
            (
                "tagged-doc",
                {
                    "name": "Tagged",
                    "type": "TextDocument",
                    "external_metadata": {
                        "citadel_tags": ["promoted_by=attacker"],
                    },
                },
            ),
            (
                "malformed-doc",
                {
                    "name": "Malformed",
                    "type": "TextDocument",
                    "external_metadata": {
                        "citadel_attestation": {
                            "promoted_by": "admin-8",
                            "promoted_at": "2026-08-06T12:00:00",
                        }
                    },
                },
            ),
        ],
        [],
        limit=10,
        dataset_map={
            "central-doc": ["masumi-network"],
            "trace-doc": ["session-traces"],
        },
    )

    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["central-doc"]["trust_tier"] == "unattested"
    assert nodes["central-doc"]["promoted_by"] == "admin-7"
    assert nodes["central-doc"]["promoted_at"] == "2026-08-06T12:00:00+00:00"
    assert nodes["trace-doc"]["trust_tier"] == "reference-only"
    assert "promoted_by" not in nodes["tagged-doc"]
    assert "promoted_at" not in nodes["tagged-doc"]
    assert "promoted_by" not in nodes["malformed-doc"]
    assert "promoted_at" not in nodes["malformed-doc"]


def test_dataset_map_propagates_to_chunks_via_is_part_of() -> None:
    payload = build_graph_payload(
        DOC_NODES,
        DOC_EDGES,
        limit=10,
        dataset_map={"doc-1": ["seat:alice"]},
    )

    chunk = next(node for node in payload["nodes"] if node["id"] == "chunk-1")
    assert chunk["dataset"] == "seat:alice"
    assert chunk["datasets"] == ["seat:alice"]
    assert {
        "source": "chunk-1",
        "target": "dataset:seat:alice",
        "relationship": "belongs_to",
    } in payload["edges"]


def test_dataset_hub_survives_node_cap_without_edges_to_dropped_nodes() -> None:
    payload = build_graph_payload(
        DOC_NODES,
        DOC_EDGES,
        limit=1,
        dataset_map={"doc-1": ["seat:alice"]},
    )

    assert [node["id"] for node in payload["nodes"]] == ["doc-1", "dataset:seat:alice"]
    assert payload["edges"] == [
        {
            "source": "doc-1",
            "target": "dataset:seat:alice",
            "relationship": "belongs_to",
        }
    ]
    assert all("chunk-1" not in (edge["source"], edge["target"]) for edge in payload["edges"])
    assert payload["truncated"] is True
    assert payload["total_nodes"] == 2


def test_two_datasets_on_one_document_yield_two_hubs_and_edges() -> None:
    payload = build_graph_payload(
        [("doc-1", {"name": "Mirrored", "type": "TextDocument"})],
        [],
        limit=10,
        dataset_map={"doc-1": ["seat:alice", "seat:bob"]},
    )

    doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
    assert doc["dataset"] == "seat:alice"
    assert doc["datasets"] == ["seat:alice", "seat:bob"]
    hub_ids = [node["id"] for node in payload["nodes"] if node["type"] == "dataset"]
    assert hub_ids == ["dataset:seat:alice", "dataset:seat:bob"]
    belongs = [edge for edge in payload["edges"] if edge["relationship"] == "belongs_to"]
    assert belongs == [
        {"source": "doc-1", "target": "dataset:seat:alice", "relationship": "belongs_to"},
        {"source": "doc-1", "target": "dataset:seat:bob", "relationship": "belongs_to"},
    ]


async def test_graph_attributes_datasets_through_gateway() -> None:
    mesh = KnowledgeMesh(
        FakeDatasetGateway(DOC_NODES, DOC_EDGES, dataset_map={"doc-1": ["seat:alice"]})
    )

    graph = await mesh.graph()

    assert graph["ok"] is True
    doc = next(node for node in graph["nodes"] if node["id"] == "doc-1")
    assert doc["dataset"] == "seat:alice"
    assert any(node["id"] == "dataset:seat:alice" for node in graph["nodes"])


async def test_graph_dataset_map_failure_degrades_to_plain_graph() -> None:
    mesh = KnowledgeMesh(
        FakeDatasetGateway(DOC_NODES, DOC_EDGES, map_error=RuntimeError("db offline"))
    )

    graph = await mesh.graph()

    assert graph["ok"] is True
    assert graph["fallback"] is False
    assert all("dataset" not in node for node in graph["nodes"])
    assert all(node["type"] != "dataset" for node in graph["nodes"])


async def test_graph_dataset_visible_filters_hidden_datasets_before_shaping() -> None:
    # Privacy: seat datasets are default-deny private memory. Names the caller
    # may not read are dropped before shaping — no node tag, no hub, no
    # belongs_to edge — so a plain reader cannot enumerate seat attribution.
    mesh = KnowledgeMesh(
        FakeDatasetGateway(
            DOC_NODES,
            DOC_EDGES,
            dataset_map={"doc-1": ["seat:alice", "masumi-network"]},
        )
    )

    graph = await mesh.graph(dataset_visible=lambda name: name == "masumi-network")

    doc = next(node for node in graph["nodes"] if node["id"] == "doc-1")
    assert doc["dataset"] == "masumi-network"
    assert doc["datasets"] == ["masumi-network"]
    hub_ids = [node["id"] for node in graph["nodes"] if node["type"] == "dataset"]
    assert hub_ids == ["dataset:masumi-network"]
    assert all("seat:alice" not in str(edge.values()) for edge in graph["edges"])
    assert all("seat:alice" not in str(node.values()) for node in graph["nodes"])


# --- human-readable labels for internally-named documents and chunks ---------


INTERNAL_DOC_NAME = "text_9a0364b9e99bb480dd25e1f0284c8555"


def test_internal_document_name_replaced_by_lowest_chunk_index_text() -> None:
    nodes = [
        ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
        ("chunk-b", {"text": "second chunk body", "chunk_index": 1, "type": "DocumentChunk"}),
        (
            "chunk-a",
            {
                "text": "# Release checklist\nrest of the body",
                "chunk_index": 0,
                "type": "DocumentChunk",
            },
        ),
    ]
    # Edge order and direction must not matter: chunk_index 0 always wins.
    for edges in (
        [("chunk-b", "doc-1", "is_part_of", {}), ("doc-1", "chunk-a", "is_part_of", {})],
        [("doc-1", "chunk-a", "is_part_of", {}), ("chunk-b", "doc-1", "is_part_of", {})],
    ):
        payload = build_graph_payload(nodes, edges, limit=10)

        doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
        assert doc["label"] == "# Release checklist"
        assert doc["internal_name"] == INTERNAL_DOC_NAME


def test_internal_document_name_kept_when_no_chunk_text_exists() -> None:
    payload = build_graph_payload(
        [("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"})],
        [],
        limit=10,
    )

    doc = payload["nodes"][0]
    assert doc["label"] == INTERNAL_DOC_NAME
    assert "internal_name" not in doc


def test_real_document_name_untouched_despite_is_part_of_neighbors() -> None:
    payload = build_graph_payload(
        [
            ("doc-1", {"name": "Design Doc", "type": "TextDocument"}),
            ("chunk-1", {"text": "chunk body", "chunk_index": 0, "type": "DocumentChunk"}),
        ],
        [("chunk-1", "doc-1", "is_part_of", {})],
        limit=10,
    )

    doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
    assert doc["label"] == "Design Doc"
    assert "internal_name" not in doc


def test_chunk_label_is_first_nonempty_line_capped_at_64() -> None:
    long_line = "c" * 70
    payload = build_graph_payload(
        [("chunk-1", {"text": f"\n   \n{long_line}\nsecond line", "type": "DocumentChunk"})],
        [],
        limit=10,
    )

    label = payload["nodes"][0]["label"]
    assert label == "c" * 63 + "…"
    assert len(label) == 64


def test_document_label_truncates_at_80_only_when_cut() -> None:
    for line, expected in (
        ("a" * 81, "a" * 79 + "…"),  # cut: 80 chars including the ellipsis
        ("b" * 80, "b" * 80),  # exactly 80: unchanged
    ):
        payload = build_graph_payload(
            [
                ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
                ("chunk-1", {"text": f"{line}\nmore text", "chunk_index": 0}),
            ],
            [("chunk-1", "doc-1", "is_part_of", {})],
            limit=10,
        )

        doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
        assert doc["label"] == expected
        assert len(doc["label"]) == 80


# --- ADR-0009 content isolation (layered visibility for scoped callers) ------


ISOLATION_NODES = [
    ("doc-hidden", {"name": "Alice private doc", "type": "TextDocument"}),
    ("chunk-hidden", {"text": "alice secret chunk", "type": "DocumentChunk"}),
    ("doc-visible", {"name": "Org doc", "type": "TextDocument"}),
    ("chunk-visible", {"text": "org chunk", "type": "DocumentChunk"}),
    ("ent-shared", {"name": "AcquireCo", "type": "Entity"}),
    ("ent-hidden", {"name": "SecretCo", "type": "Entity"}),
    ("type-shared", {"name": "company", "type": "EntityType"}),
    ("type-hidden", {"name": "secret-kind", "type": "EntityType"}),
    ("ring-three", {"name": "beyond the second ring", "type": "Entity"}),
]
ISOLATION_EDGES = [
    ("chunk-hidden", "doc-hidden", "is_part_of", {}),
    ("chunk-visible", "doc-visible", "is_part_of", {}),
    ("chunk-visible", "ent-shared", "contains", {}),
    ("chunk-hidden", "ent-shared", "contains", {}),
    ("chunk-hidden", "ent-hidden", "contains", {}),
    ("ent-shared", "type-shared", "is_a", {}),
    ("ent-hidden", "type-hidden", "is_a", {}),
    ("type-shared", "ring-three", "related_to", {}),
    # Pass-1 permanence probe: the hidden doc touches a visible entity.
    ("doc-hidden", "ent-shared", "mentions", {}),
]
ISOLATION_MAP = {"doc-hidden": ["seat:alice"], "doc-visible": ["masumi-network"]}


def _central_only(name: str) -> bool:
    return name == "masumi-network"


async def _isolated_graph(**kwargs: Any) -> dict[str, Any]:
    mesh = KnowledgeMesh(
        FakeDatasetGateway(ISOLATION_NODES, ISOLATION_EDGES, dataset_map=ISOLATION_MAP)
    )
    return await mesh.graph(dataset_visible=_central_only, **kwargs)


async def test_hidden_document_and_its_chunk_are_filtered_out() -> None:
    graph = await _isolated_graph()

    ids = {node["id"] for node in graph["nodes"]}
    assert "doc-hidden" not in ids
    assert "chunk-hidden" not in ids
    # Not just unlabeled — the content is gone from every part of the payload.
    payload_text = str(graph)
    assert "alice secret chunk" not in payload_text
    assert "seat:alice" not in payload_text
    for edge in graph["edges"]:
        assert "doc-hidden" not in (edge["source"], edge["target"])
        assert "chunk-hidden" not in (edge["source"], edge["target"])


async def test_shared_entity_next_to_visible_and_hidden_docs_stays_visible() -> None:
    graph = await _isolated_graph()

    ids = {node["id"] for node in graph["nodes"]}
    assert "ent-shared" in ids


async def test_hidden_doc_is_not_revived_through_a_shared_entity() -> None:
    # Pass-1 permanence: doc-hidden is adjacent to ent-shared (visible via the
    # visible chunk), but a mapped node with no visible dataset stays hidden.
    graph = await _isolated_graph()

    ids = {node["id"] for node in graph["nodes"]}
    assert "ent-shared" in ids
    assert "doc-hidden" not in ids
    assert "chunk-hidden" not in ids


async def test_entity_adjacent_only_to_hidden_content_is_hidden() -> None:
    graph = await _isolated_graph()

    ids = {node["id"] for node in graph["nodes"]}
    assert "ent-hidden" not in ids
    assert "type-hidden" not in ids


async def test_entity_type_second_ring_is_visible_and_expansion_stops_there() -> None:
    graph = await _isolated_graph()

    ids = {node["id"] for node in graph["nodes"]}
    assert "type-shared" in ids  # entity -> EntityType, second ring
    assert "ring-three" not in ids  # third ring: expansion stopped


async def test_generic_edge_to_ring1_never_leaks_hidden_only_entity_name() -> None:
    # ent-leak was extracted solely from the hidden seat's document, but shares
    # a generic edge with a visible ring-1 entity. Promoting it would leak its
    # NAME; pass 4 is type-lineage (is_a) only, so it must stay hidden while
    # the EntityType reached via is_a stays visible.
    nodes = ISOLATION_NODES + [("ent-leak", {"name": "HiddenOnlyCo", "type": "Entity"})]
    edges = ISOLATION_EDGES + [
        ("chunk-hidden", "ent-leak", "contains", {}),
        ("ent-leak", "ent-shared", "related_to", {}),
    ]
    mesh = KnowledgeMesh(FakeDatasetGateway(nodes, edges, dataset_map=ISOLATION_MAP))

    graph = await mesh.graph(dataset_visible=_central_only)

    ids = {node["id"] for node in graph["nodes"]}
    assert "ent-leak" not in ids
    assert "HiddenOnlyCo" not in str(graph)
    assert "type-shared" in ids  # is_a lineage from ring 1 still promotes


async def test_is_a_edge_to_plain_entity_never_leaks_hidden_only_entity_name() -> None:
    # Extraction can name an entity→entity edge literally "is_a"
    # ("SecretKuzuFork is a graph database"). The edge name alone must not
    # promote: pass 4 also requires the promoted node to BE an EntityType,
    # so a hidden-only plain Entity stays hidden even across an is_a edge
    # to a visible ring-1 entity (in either direction).
    for source, target in (("ent-isa-leak", "ent-shared"), ("ent-shared", "ent-isa-leak")):
        nodes = ISOLATION_NODES + [("ent-isa-leak", {"name": "SecretKuzuFork", "type": "Entity"})]
        edges = ISOLATION_EDGES + [
            ("chunk-hidden", "ent-isa-leak", "contains", {}),
            (source, target, "is_a", {}),
        ]
        mesh = KnowledgeMesh(FakeDatasetGateway(nodes, edges, dataset_map=ISOLATION_MAP))

        graph = await mesh.graph(dataset_visible=_central_only)

        ids = {node["id"] for node in graph["nodes"]}
        assert "ent-isa-leak" not in ids
        assert "SecretKuzuFork" not in str(graph)
        assert "type-shared" in ids  # genuine EntityType second ring intact


async def test_node_cap_applies_after_filtering_and_counts_stay_honest() -> None:
    graph = await _isolated_graph(limit=2)

    content = [node for node in graph["nodes"] if node["type"] != "dataset"]
    # The visible set is doc-visible, chunk-visible, ent-shared, type-shared;
    # the cap trims the VISIBLE set, never re-admits hidden nodes.
    assert [node["id"] for node in content] == ["doc-visible", "chunk-visible"]
    assert graph["truncated"] is True
    assert graph["total_nodes"] == len(ISOLATION_NODES)  # raw org-wide count
    assert graph["visible_nodes"] == 4  # caller-scoped count


async def test_visible_nodes_field_reports_caller_scope() -> None:
    graph = await _isolated_graph()

    assert graph["visible_nodes"] == 4
    assert graph["total_nodes"] == len(ISOLATION_NODES)
    assert graph["total_edges"] == len(ISOLATION_EDGES)


async def test_graph_without_dataset_visible_matches_unfiltered_payload() -> None:
    # None = bypass/admin callers: byte-identical to the unfiltered shaping,
    # no visible_nodes field, hidden content fully present.
    gateway = FakeDatasetGateway(ISOLATION_NODES, ISOLATION_EDGES, dataset_map=ISOLATION_MAP)

    graph = await KnowledgeMesh(gateway).graph()

    expected = build_graph_payload(ISOLATION_NODES, ISOLATION_EDGES, dataset_map=ISOLATION_MAP)
    assert graph == {"ok": True, "fallback": False, **expected}
    assert "visible_nodes" not in graph
    assert any(node["id"] == "doc-hidden" for node in graph["nodes"])


async def test_scoped_caller_sees_no_content_when_dataset_map_read_fails() -> None:
    # Fail-closed: without attribution nothing is provably visible, so a
    # scoped caller gets an empty content set instead of the whole org graph.
    mesh = KnowledgeMesh(
        FakeDatasetGateway(ISOLATION_NODES, ISOLATION_EDGES, map_error=RuntimeError("db offline"))
    )

    graph = await mesh.graph(dataset_visible=lambda name: True)

    assert graph["ok"] is True
    assert graph["fallback"] is False
    assert graph["nodes"] == []
    assert graph["edges"] == []
    assert graph["visible_nodes"] == 0
    assert graph["total_nodes"] == len(ISOLATION_NODES)


# --- universal seat presence hubs (ADR-0009) ----------------------------------


PRESENCE = [
    {"dataset": "masumi-network", "label": "masumi-network"},
    {"dataset": "seat:alice", "label": "seat:alice"},
    {"dataset": "seat:empty", "label": "seat:empty"},
]


async def test_presence_hubs_appear_even_when_caller_sees_none_of_the_content() -> None:
    graph = await _isolated_graph(presence=PRESENCE)

    hubs = {node["id"]: node for node in graph["nodes"] if node["type"] == "dataset"}
    assert set(hubs) == {
        "dataset:masumi-network",
        "dataset:seat:alice",
        "dataset:seat:empty",
    }
    # Presence counts come from the RAW map (contribution counts are
    # org-visible presence metadata), zero-content seats included.
    assert hubs["dataset:seat:alice"]["presence"] == {"documents": 1}
    assert hubs["dataset:seat:empty"]["presence"] == {"documents": 0}
    assert hubs["dataset:masumi-network"]["presence"] == {"documents": 1}
    # Every seat hub anchors to the Central hub so it never floats.
    presence_edges = [edge for edge in graph["edges"] if edge["relationship"] == "presence"]
    assert {
        "source": "dataset:seat:alice",
        "target": "dataset:masumi-network",
        "relationship": "presence",
    } in presence_edges
    assert {
        "source": "dataset:seat:empty",
        "target": "dataset:masumi-network",
        "relationship": "presence",
    } in presence_edges
    assert len(presence_edges) == 2


async def test_presence_hubs_dedupe_against_content_derived_hubs() -> None:
    # Bypass caller: seat:alice gets a content-derived hub AND a presence
    # entry — exactly one hub per dataset must survive.
    mesh = KnowledgeMesh(
        FakeDatasetGateway(ISOLATION_NODES, ISOLATION_EDGES, dataset_map=ISOLATION_MAP)
    )

    graph = await mesh.graph(presence=PRESENCE)

    hub_ids = [node["id"] for node in graph["nodes"] if node["type"] == "dataset"]
    assert sorted(hub_ids) == [
        "dataset:masumi-network",
        "dataset:seat:alice",
        "dataset:seat:empty",
    ]
    assert len(hub_ids) == len(set(hub_ids))
    # belongs_to edges from kept content to its hub survive alongside presence.
    assert {
        "source": "doc-hidden",
        "target": "dataset:seat:alice",
        "relationship": "belongs_to",
    } in graph["edges"]


async def test_fallback_payloads_still_carry_presence_hubs() -> None:
    # ADR-0009: every seat is ALWAYS visible — fallback payloads included.
    # No dataset map is available on these paths, so documents degrade to 0.
    for mesh, reason in (
        (KnowledgeMesh(None), "graph_access_unavailable"),
        (
            KnowledgeMesh(FakeGraphGateway(error=RuntimeError("kuzu offline"))),
            "graph_engine_error:RuntimeError",
        ),
        (KnowledgeMesh(FakeGraphGateway([], [])), "graph_empty"),
    ):
        graph = await mesh.graph(presence=PRESENCE)

        assert graph["ok"] is True
        assert graph["fallback"] is True
        assert graph["fallback_reason"] == reason
        hubs = {node["id"]: node for node in graph["nodes"] if node["type"] == "dataset"}
        assert set(hubs) == {
            "dataset:masumi-network",
            "dataset:seat:alice",
            "dataset:seat:empty",
        }
        assert all(hub["presence"] == {"documents": 0} for hub in hubs.values())
        # Seat hubs still anchor to the Central hub.
        assert {
            "source": "dataset:seat:alice",
            "target": "dataset:masumi-network",
            "relationship": "presence",
        } in graph["edges"]
        # Hubs are synthetic: raw counts stay 0.
        assert graph["total_nodes"] == 0
        assert graph["total_edges"] == 0
        assert graph["truncated"] is False


async def test_graph_empty_fallback_counts_presence_from_available_map() -> None:
    # graph_empty is the one fallback where the dataset map CAN be read:
    # presence counts come from the map instead of degrading to 0.
    mesh = KnowledgeMesh(FakeDatasetGateway([], [], dataset_map={"doc-1": ["seat:alice"]}))

    graph = await mesh.graph(presence=PRESENCE)

    assert graph["fallback"] is True
    assert graph["fallback_reason"] == "graph_empty"
    hubs = {node["id"]: node for node in graph["nodes"] if node["type"] == "dataset"}
    assert hubs["dataset:seat:alice"]["presence"] == {"documents": 1}
    assert hubs["dataset:seat:empty"]["presence"] == {"documents": 0}
    assert graph["total_nodes"] == 0


# --- frontmatter-aware first-line labels --------------------------------------


def test_frontmatter_block_is_skipped_when_deriving_document_labels() -> None:
    for closing in ("---", "..."):
        text = f"---\ntitle: raw yaml\ntags: [a, b]\n{closing}\n\n# Release checklist\nbody"
        payload = build_graph_payload(
            [
                ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
                ("chunk-1", {"text": text, "chunk_index": 0, "type": "DocumentChunk"}),
            ],
            [("chunk-1", "doc-1", "is_part_of", {})],
            limit=10,
        )

        doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
        assert doc["label"] == "# Release checklist"
        chunk = next(node for node in payload["nodes"] if node["id"] == "chunk-1")
        assert chunk["label"] == "# Release checklist"


def test_unclosed_frontmatter_fence_skips_only_the_fence_line() -> None:
    payload = build_graph_payload(
        [("chunk-1", {"text": "---\nfirst body line\nsecond", "type": "DocumentChunk"})],
        [],
        limit=10,
    )

    assert payload["nodes"][0]["label"] == "first body line"


def test_dashes_only_chunk_text_leaves_internal_document_label_unchanged() -> None:
    payload = build_graph_payload(
        [
            ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
            ("chunk-1", {"text": "---\n---", "chunk_index": 0, "type": "DocumentChunk"}),
        ],
        [("chunk-1", "doc-1", "is_part_of", {})],
        limit=10,
    )

    doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
    assert doc["label"] == INTERNAL_DOC_NAME
    assert "internal_name" not in doc


# --- fallback labels when no is_part_of chunk names an internal node ----------

SECOND_INTERNAL_DOC_NAME = "text_" + "b" * 32


def test_internal_document_labeled_by_summary_neighbor() -> None:
    nodes = [
        ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
        ("sum-1", {"text": "Weekly release notes\nrest", "type": "TextSummary"}),
    ]
    payload = build_graph_payload(nodes, [("sum-1", "doc-1", "made_from", {})], limit=10)

    doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
    assert doc["label"] == "Weekly release notes"
    assert doc["internal_name"] == INTERNAL_DOC_NAME


def test_internal_document_labeled_by_raw_data_location_basename() -> None:
    payload = build_graph_payload(
        [
            (
                "doc-1",
                {
                    "name": INTERNAL_DOC_NAME,
                    "type": "TextDocument",
                    "raw_data_location": "/data/store/README.md",
                },
            )
        ],
        [],
        limit=10,
    )

    doc = payload["nodes"][0]
    assert doc["label"] == "README.md"
    assert doc["internal_name"] == INTERNAL_DOC_NAME


def test_internal_raw_data_location_name_is_not_used_as_label() -> None:
    # A cognee-generated text_<md5>.txt path is no better than the internal name.
    payload = build_graph_payload(
        [
            (
                "doc-1",
                {
                    "name": INTERNAL_DOC_NAME,
                    "type": "TextDocument",
                    "raw_data_location": f"/data/store/{INTERNAL_DOC_NAME}.txt",
                },
            )
        ],
        [],
        limit=10,
    )

    doc = payload["nodes"][0]
    assert doc["label"] == INTERNAL_DOC_NAME
    assert "internal_name" not in doc


def test_internal_document_labeled_by_nodeset_membership() -> None:
    nodes = [
        ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
        ("set-1", {"name": "user_sessions_from_cache", "type": "NodeSet"}),
    ]
    payload = build_graph_payload(nodes, [("doc-1", "set-1", "belongs_to_set", {})], limit=10)

    doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
    assert doc["label"] == "user_sessions_from_cache"
    assert doc["internal_name"] == INTERNAL_DOC_NAME


def test_summary_fallback_wins_over_nodeset_fallback() -> None:
    nodes = [
        ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
        ("sum-1", {"text": "Concrete summary line", "type": "TextSummary"}),
        ("set-1", {"name": "user_sessions_from_cache", "type": "NodeSet"}),
    ]
    edges = [
        ("sum-1", "doc-1", "made_from", {}),
        ("doc-1", "set-1", "belongs_to_set", {}),
    ]
    payload = build_graph_payload(nodes, edges, limit=10)

    doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
    assert doc["label"] == "Concrete summary line"


# --- collapse orphan documents into their NodeSet hub (opt-in) ----------------


def test_collapse_orphans_folds_internal_documents_into_nodeset() -> None:
    nodes = [
        ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
        ("doc-2", {"name": SECOND_INTERNAL_DOC_NAME, "type": "TextDocument"}),
        ("set-1", {"name": "user_sessions_from_cache", "type": "NodeSet"}),
    ]
    edges = [
        ("doc-1", "set-1", "belongs_to_set", {}),
        ("doc-2", "set-1", "belongs_to_set", {}),
    ]
    payload = build_graph_payload(nodes, edges, limit=10, collapse_orphan_documents=True)

    ids = {node["id"] for node in payload["nodes"]}
    assert ids == {"set-1"}
    hub = payload["nodes"][0]
    assert hub["collapsed"] == 2
    # Collapsed documents take their edges with them.
    assert payload["edges"] == []


def test_orphans_are_not_collapsed_without_opt_in() -> None:
    nodes = [
        ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
        ("set-1", {"name": "user_sessions_from_cache", "type": "NodeSet"}),
    ]
    payload = build_graph_payload(nodes, [("doc-1", "set-1", "belongs_to_set", {})], limit=10)

    ids = {node["id"] for node in payload["nodes"]}
    assert ids == {"doc-1", "set-1"}
    set_node = next(node for node in payload["nodes"] if node["id"] == "set-1")
    assert "collapsed" not in set_node


def test_summary_boilerplate_lead_in_is_stripped_from_labels() -> None:
    nodes = [
        (
            "sum-1",
            {
                "text": "This chunk is about a repository of question-answer pairs",
                "type": "TextSummary",
            },
        ),
        ("sum-2", {"name": "This document describes the release checklist", "type": "TextSummary"}),
        ("sum-3", {"text": "This input is a list of 40 empty questions", "type": "TextSummary"}),
    ]
    payload = build_graph_payload(nodes, [], limit=10)
    labels = {node["id"]: node["label"] for node in payload["nodes"]}
    assert labels["sum-1"] == "Repository of question-answer pairs"
    assert labels["sum-2"] == "Release checklist"
    assert labels["sum-3"] == "List of 40 empty questions"


def test_ordinary_name_is_not_mistaken_for_summary_boilerplate() -> None:
    # "This is the auth service" has no summary noun after the pronoun, so it
    # must survive untouched.
    payload = build_graph_payload(
        [("n-1", {"name": "This is the auth service", "type": "Entity"})], [], limit=10
    )
    assert payload["nodes"][0]["label"] == "This is the auth service"


def test_internal_document_summary_fallback_strips_boilerplate() -> None:
    nodes = [
        ("doc-1", {"name": INTERNAL_DOC_NAME, "type": "TextDocument"}),
        ("sum-1", {"text": "This chunk is about the promotion pipeline", "type": "TextSummary"}),
    ]
    payload = build_graph_payload(nodes, [("sum-1", "doc-1", "made_from", {})], limit=10)
    doc = next(node for node in payload["nodes"] if node["id"] == "doc-1")
    assert doc["label"] == "Promotion pipeline"


def test_named_document_is_never_collapsed() -> None:
    nodes = [
        ("doc-1", {"name": "Design Doc", "type": "TextDocument"}),
        ("set-1", {"name": "user_sessions_from_cache", "type": "NodeSet"}),
    ]
    payload = build_graph_payload(
        nodes,
        [("doc-1", "set-1", "belongs_to_set", {})],
        limit=10,
        collapse_orphan_documents=True,
    )

    ids = {node["id"] for node in payload["nodes"]}
    assert ids == {"doc-1", "set-1"}
