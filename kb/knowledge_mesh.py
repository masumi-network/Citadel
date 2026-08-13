"""Knowledge Mesh: the real Cognee-backed relationship graph.

This is intentionally separate from :class:`kb.mesh.MeshState`, which is a
wrapper-level dashboard projection of runtime activity. The Knowledge Mesh
exposes what the Organization Vault actually knows: nodes and relationships
read from Cognee's graph engine (Kuzu in the v1 deployment).

The endpoint contract never fails hard: when Cognee has no data, the graph
engine is unavailable, or the gateway does not expose graph access, callers
receive an empty graph with ``fallback: true`` instead of an error.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MAX_NODES = 200

_LABEL_KEYS = ("name", "label", "title", "text", "id")
_TYPE_KEYS = ("type", "node_type", "entity_type", "category")

# Cognee names document nodes ``text_<md5>``/``data_<md5>`` — useless to
# humans. Labels matching this (or a bare node id) are candidates for
# derivation from their DocumentChunk neighbors' text.
_INTERNAL_NAME_RE = re.compile(r"^(?:text|data)_[0-9a-f]{16,}$", re.IGNORECASE)

_DOC_LABEL_MAX = 80
_CHUNK_LABEL_MAX = 64

# Seat datasets are namespaced "seat:<slug>" (kb.access.SEAT_DATASET_PREFIX);
# inlined so this module keeps zero kb imports.
_SEAT_PREFIX = "seat:"
_SESSION_TRACES_DATASET = "session-traces"
_ATTESTATION_METADATA_KEY = "citadel_attestation"

# YAML frontmatter closing fences are searched within this many leading lines.
_FRONTMATTER_SCAN_LINES = 30

# A line of only dashes/punctuation (a stray "---" fence, "***" rule, …) never
# makes a useful label.
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$")

# Cognee's summariser opens nearly every TextSummary with boilerplate —
# "This chunk is about …", "This document describes …", "This input is a …" —
# so every derived label reads identically. Strip that lead-in so the real
# subject shows. The subject noun is restricted to a known set so ordinary
# names ("This is the auth service") are never touched.
_SUMMARY_LEAD_RE = re.compile(
    r"^(?:the|this|these|it|here)\s+"
    r"(?:chunk|input|document|text|section|note|page|passage|content|summary|"
    r"entry|file|record|snippet|paragraph|excerpt|data|list|log)s?\s+"
    r"(?:is|are|describes?|discusses|contains?|covers?|provides?|details?|"
    r"explains?|outlines?|summari[sz]es?|represents?|lists?|shows?|appears?|"
    r"captures?|documents?|reflects?)\s+"
    r"(?:to\s+be\s+|that\s+|how\s+|about\s+|a\s+|an\s+|the\s+|of\s+)*",
    re.IGNORECASE,
)


def _strip_summary_lead(line: str) -> str:
    """Drop a cognee summariser lead-in; recapitalise the surviving subject.

    Returns ``""`` when only punctuation survives (e.g. "This chunk is about
    ."), so callers fall through to the next label source.
    """
    cleaned = _SUMMARY_LEAD_RE.sub("", line, count=1).strip()
    if not cleaned or _PUNCT_ONLY_RE.match(cleaned):
        return ""
    if cleaned == line:
        return line
    return cleaned[0].upper() + cleaned[1:]


def _first_line_label(text: str, max_len: int, *, clean=None) -> str:
    """First non-empty line, whitespace-collapsed, ellipsis-truncated when cut.

    Text opening with a ``---`` fence skips the whole YAML frontmatter block —
    up to and including the closing ``---``/``...`` fence within the first
    ``_FRONTMATTER_SCAN_LINES`` lines; when no closing fence is found only the
    opening fence line is skipped. Lines that are only dashes/punctuation are
    never returned as labels. ``clean`` optionally rewrites the surviving line
    (applied BEFORE truncation so the subject is not cut off by a lead-in).
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        start = 1
        for index in range(1, min(len(lines), _FRONTMATTER_SCAN_LINES)):
            if lines[index].strip() in ("---", "..."):
                start = index + 1
                break
    for line in lines[start:]:
        collapsed = " ".join(line.split())
        if not collapsed or _PUNCT_ONLY_RE.match(collapsed):
            continue
        if clean is not None:
            collapsed = clean(collapsed)
            if not collapsed:
                continue
        if len(collapsed) <= max_len:
            return collapsed
        return collapsed[: max_len - 1] + "…"
    return ""


def _is_internal_label(node_id: str, label: str) -> bool:
    return label == node_id or bool(_INTERNAL_NAME_RE.match(label))


def _node_label(node_id: str, properties: dict[str, Any]) -> str:
    for key in _LABEL_KEYS:
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            if key == "text":
                # Chunk/summary nodes: raw bodies are paragraphs, not names —
                # keep the first non-empty line, tightly capped, with the
                # summariser lead-in stripped. Bodies with no labelable line
                # (e.g. only dashes) fall through to the next key.
                label = _first_line_label(value, _CHUNK_LABEL_MAX, clean=_strip_summary_lead)
                if label:
                    return label
                continue
            # name/label/title may still carry a summariser sentence — strip it
            # too, but keep the original when nothing boilerplate matched.
            collapsed = " ".join(value.split())
            return (_strip_summary_lead(collapsed) or collapsed)[:120]
    return str(node_id)[:120]


def _node_type(properties: dict[str, Any]) -> str:
    for key in _TYPE_KEYS:
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return "node"


def _trust_tier(datasets: list[str] | tuple[str, ...] | None) -> str:
    """Derive graph trust from dataset attribution, never from content."""
    if datasets and _SESSION_TRACES_DATASET in datasets:
        return "reference-only"
    return "unattested"


def _external_metadata(value: Any) -> Mapping[str, Any] | None:
    """Decode Cognee external metadata; malformed values are untrusted."""
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _promotion_metadata(properties: dict[str, Any]) -> dict[str, str]:
    """Return only a complete, reserved, timezone-aware promotion attestation."""
    metadata = _external_metadata(properties.get("external_metadata"))
    if metadata is None:
        return {}
    attestation = metadata.get(_ATTESTATION_METADATA_KEY)
    if not isinstance(attestation, Mapping):
        return {}
    promoted_by = attestation.get("promoted_by")
    promoted_at = attestation.get("promoted_at")
    if (
        not isinstance(promoted_by, str)
        or not promoted_by.strip()
        or not isinstance(promoted_at, str)
        or not promoted_at.strip()
    ):
        return {}
    try:
        timestamp = datetime.fromisoformat(promoted_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return {}
    if timestamp.tzinfo is None:
        return {}
    return {
        "promoted_by": promoted_by.strip(),
        "promoted_at": promoted_at.strip(),
    }


def _raw_props_by_id(raw_nodes: list[Any]) -> dict[str, dict[str, Any]]:
    """Index ``{node_id: properties}`` for neighbour/own-property label lookups."""
    index: dict[str, dict[str, Any]] = {}
    for raw in raw_nodes:
        try:
            node_id, properties = str(raw[0]), raw[1]
        except (TypeError, IndexError, KeyError):
            continue
        if isinstance(properties, dict):
            index[node_id] = properties
    return index


def _basename_label(location: str) -> str:
    """Source-file basename from a ``raw_data_location`` path (``/`` or ``\\``)."""
    return location.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()


def _derive_fallback_label(
    node_id: str,
    raw_props: dict[str, dict[str, Any]],
    nodeset_of: dict[str, str],
    summary_of: dict[str, str],
) -> str:
    """Best-available label for an internal node the chunk pass could not name.

    Tried in descending descriptiveness: (1) the first line of a neighbouring
    TextSummary, (2) the source-file basename from ``raw_data_location`` (skipped
    when it is itself a cognee-internal ``text_<md5>.txt`` name), (3) the name of
    the NodeSet the node belongs to (coarse but real). Returns ``""`` when no
    source yields a usable label, leaving the internal name in place.
    """
    summary_id = summary_of.get(node_id)
    if summary_id:
        properties = raw_props.get(summary_id, {})
        for key in ("text", "name", "label"):
            value = properties.get(key)
            if isinstance(value, str) and value.strip():
                label = _first_line_label(value, _DOC_LABEL_MAX, clean=_strip_summary_lead)
                if label:
                    return label

    location = raw_props.get(node_id, {}).get("raw_data_location")
    if isinstance(location, str) and location.strip():
        base = _basename_label(location)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        if base and not _is_internal_label(node_id, stem):
            return base[:_DOC_LABEL_MAX]

    nodeset_id = nodeset_of.get(node_id)
    if nodeset_id:
        label = _node_label(nodeset_id, raw_props.get(nodeset_id, {}))
        if label and not _is_internal_label(nodeset_id, label):
            return label[:_DOC_LABEL_MAX]

    return ""


def _raw_id(raw: Any) -> str | None:
    try:
        return str(raw[0])
    except (TypeError, IndexError, KeyError):
        return None


def _raw_endpoints(raw: Any) -> tuple[str, str] | None:
    try:
        return str(raw[0]), str(raw[1])
    except (TypeError, IndexError, KeyError):
        return None


def _content_visible_ids(
    raw_nodes: list[Any],
    raw_edges: list[Any],
    dataset_map: dict[str, list[str]],
    dataset_visible: Callable[[str], bool],
) -> set[str]:
    """Layered content visibility for scoped callers (ADR-0009).

    Deliberately NOT unbounded reachability: cognee dedupes entities across
    datasets, so a BFS from visible documents through a shared entity would
    resurface a hidden seat's documents and chunks. Layered passes instead:

    - Pass 1: a node in ``dataset_map`` is visible iff ANY of its datasets is
      visible; a mapped node with NO visible dataset is hidden permanently —
      later passes never revive it.
    - Pass 2: untagged nodes linked to a mapped document by an ``is_part_of``
      edge (chunks) take that document's visibility: visible via ANY visible
      linked document, permanently hidden when every linked mapped document
      is hidden.
    - Pass 3: remaining untagged nodes (entities) adjacent to any visible
      document/chunk become visible.
    - Pass 4: remaining untagged nodes that are themselves type nodes
      (``EntityType``) linked to a pass-3 node by a type-lineage edge
      (``is_a``, case-insensitive) become visible (EntityType second ring).
      Both conditions are required: generic relationships never promote,
      and an ``is_a`` edge alone does not either — extraction can name an
      entity→entity edge "is_a", which would otherwise leak a hidden-only
      entity's NAME. Nothing in the denied set is ever promoted. Expansion
      stops there.

    Everything still unresolved stays hidden — fail-closed, so an empty
    ``dataset_map`` (failed attribution read included) hides all content.
    """
    node_ids: set[str] = set()
    type_node_ids: set[str] = set()  # nodes that ARE types (EntityType), pass-4 pool
    for raw in raw_nodes:
        try:
            node_id = str(raw[0])
            properties = raw[1] if isinstance(raw[1], dict) else {}
        except (TypeError, IndexError, KeyError):
            continue
        node_ids.add(node_id)
        if "entitytype" in _node_type(properties).lower():
            type_node_ids.add(node_id)

    visible: set[str] = set()  # pass 1 + pass 2: documents and their chunks
    denied: set[str] = set()  # permanently hidden, never revived
    for node_id in node_ids:
        names = dataset_map.get(node_id)
        if names is None:
            continue
        if any(dataset_visible(name) for name in names):
            visible.add(node_id)
        else:
            denied.add(node_id)

    # One edge sweep: pass-2 inputs (chunk -> mapped documents), the full
    # adjacency used by pass 3, and the type-lineage adjacency used by pass 4.
    chunk_docs: dict[str, set[str]] = {}
    adjacency: dict[str, set[str]] = {}
    type_adjacency: dict[str, set[str]] = {}
    for raw in raw_edges:
        try:
            source, target, relationship = str(raw[0]), str(raw[1]), str(raw[2])
        except (TypeError, IndexError, KeyError):
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
        if relationship.lower() == "is_a":
            type_adjacency.setdefault(source, set()).add(target)
            type_adjacency.setdefault(target, set()).add(source)
        if relationship != "is_part_of":
            continue
        for doc_id, other_id in ((source, target), (target, source)):
            if doc_id in dataset_map and other_id not in dataset_map:
                chunk_docs.setdefault(other_id, set()).add(doc_id)

    for chunk_id, doc_ids in chunk_docs.items():
        if chunk_id not in node_ids:
            continue
        if doc_ids & visible:
            visible.add(chunk_id)
        else:
            # Pass 1 resolved every mapped node, so all linked documents are
            # hidden here — the chunk IS that hidden content.
            denied.add(chunk_id)

    def _touches(node_id: str, pool: set[str]) -> bool:
        neighbors = adjacency.get(node_id)
        return bool(neighbors) and not neighbors.isdisjoint(pool)

    def _type_touches(node_id: str, pool: set[str]) -> bool:
        neighbors = type_adjacency.get(node_id)
        return bool(neighbors) and not neighbors.isdisjoint(pool)

    unresolved = node_ids - visible - denied
    ring1 = {node_id for node_id in unresolved if _touches(node_id, visible)}
    unresolved -= ring1
    # Type-lineage ONLY, and the promoted node must itself BE a type node:
    # extraction can emit an entity→entity edge literally named "is_a"
    # ("SecretFork is_a graph database"), so trusting the edge name alone
    # would still surface a hidden-only entity's name — that name is the
    # leak (unresolved already excludes the denied set).
    ring2 = {
        node_id
        for node_id in unresolved
        if node_id in type_node_ids and _type_touches(node_id, ring1)
    }
    return visible | ring1 | ring2


def build_graph_payload(
    raw_nodes: list[Any],
    raw_edges: list[Any],
    *,
    limit: int = DEFAULT_MAX_NODES,
    dataset_map: dict[str, list[str]] | None = None,
    presence: list[dict[str, Any]] | None = None,
    collapse_orphan_documents: bool = False,
) -> dict[str, Any]:
    """Shape raw Cognee graph tuples into the ``{nodes, edges}`` contract.

    Nodes are ``(node_id, properties)`` tuples; edges are
    ``(source_id, target_id, relationship_name, properties)`` tuples. The node
    list is capped at ``limit`` and edges are kept only when both endpoints
    survive the cap.

    ``dataset_map`` (``{data_id: [dataset_name, ...]}``) optionally attributes
    document nodes to their cognee datasets (per-seat ``seat:<slug>`` datasets):
    mapped nodes gain ``dataset``/``datasets`` keys, chunks inherit the dataset
    of their document via ``is_part_of`` edges, and one synthetic hub node per
    dataset (``dataset:<name>``) is appended with ``belongs_to`` edges. Raw
    nodes always expose ``trust_tier`` derived from their dataset attribution;
    ``session-traces`` is ``reference-only`` and every other case is
    ``unattested``. Hubs are synthetic: they do not count against ``limit`` or
    into ``total_nodes``.

    Raw nodes expose ``promoted_by``/``promoted_at`` only when Cognee carries a
    complete, timezone-aware attestation in the reserved
    ``external_metadata.citadel_attestation`` field. User tags and node text
    are never inspected for these fields. Malformed metadata is ignored.

    ``presence`` (``[{"dataset": name, "label": label, "documents": count?},
    ...]``) appends a universal hub per entry regardless of surviving content
    (ADR-0009 Seat Presence: content-derived hubs vanish exactly when isolation
    hides the content). Presence entries dedupe against content-derived hubs —
    one hub per dataset total. When ``presence`` is given, every hub carries
    ``presence: {"documents": N}`` (the entry's ``documents`` when provided,
    else counted from ``dataset_map``; 0 for empty seats) and every seat hub
    (``seat:``-prefixed dataset) gains one ``presence`` edge to the Central hub
    — the first non-seat presence entry — so hubs never float disconnected.
    ``None`` keeps the exact pre-presence behavior.

    Kept nodes labeled with cognee-internal names (``text_<md5>``/``data_<md5>``
    or the bare node id) are relabeled with the first non-empty line of their
    lowest-``chunk_index`` DocumentChunk neighbor's text (via ``is_part_of``
    edges, either direction); the internal name is preserved under
    ``internal_name``. This applies regardless of ``dataset_map``.
    """
    limit = max(1, limit)
    dataset_map = dataset_map or {}
    nodes: list[dict[str, Any]] = []
    kept_ids: set[str] = set()
    node_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_nodes:
        try:
            node_id, properties = raw[0], raw[1]
        except (TypeError, IndexError, KeyError):
            continue
        if not isinstance(properties, dict):
            properties = {}
        node_key = str(node_id)
        if node_key in kept_ids:
            continue
        kept_ids.add(node_key)
        node: dict[str, Any] = {
            "id": node_key,
            "label": _node_label(node_key, properties),
            "type": _node_type(properties),
            "trust_tier": _trust_tier(dataset_map.get(node_key)),
        }
        names = dataset_map.get(node_key)
        if names:
            node["dataset"] = names[0]
            node["datasets"] = list(names)
            node["trust_tier"] = _trust_tier(names)
        node.update(_promotion_metadata(properties))
        nodes.append(node)
        node_by_id[node_key] = node
        if len(nodes) >= limit:
            break

    # Captured before collapse can shrink ``nodes`` so ``truncated`` keeps its
    # meaning ("more raw nodes exist than were shaped"), not "collapse removed
    # some".
    kept_count = len(nodes)

    # Kept nodes still wearing a cognee-internal label (``text_<md5>`` or the
    # bare node id) get a human label derived from their chunk text below.
    internal_nodes: dict[str, dict[str, Any]] = {
        node["id"]: node
        for node in nodes
        if _is_internal_label(node["id"], node["label"])
    }
    doc_chunk_ids: dict[str, list[str]] = {}

    if dataset_map or internal_nodes:
        # Single shared pass over is_part_of edges (either direction):
        # 1) chunk-level attribution — a DocumentChunk links to its document
        #    via is_part_of; tag kept, untagged endpoints with the mapped
        #    document's dataset(s); 2) label derivation — collect the chunk
        #    neighbors of internally-named nodes. Single pass, no recursion.
        for raw in raw_edges:
            try:
                source, target, relationship = str(raw[0]), str(raw[1]), str(raw[2])
            except (TypeError, IndexError, KeyError):
                continue
            if relationship != "is_part_of":
                continue
            for doc_id, other_id in ((source, target), (target, source)):
                names = dataset_map.get(doc_id)
                other = node_by_id.get(other_id)
                if names and other is not None and "dataset" not in other:
                    other["dataset"] = names[0]
                    other["datasets"] = list(names)
                    other["trust_tier"] = _trust_tier(names)
                if doc_id in internal_nodes:
                    doc_chunk_ids.setdefault(doc_id, []).append(other_id)

    if doc_chunk_ids:
        # Derive readable document labels: pick the chunk with the smallest
        # numeric chunk_index (fallback: first encountered) and use the first
        # non-empty line of its text. Chunks beyond the node cap still count —
        # properties come from raw_nodes, not the kept set.
        wanted = {cid for ids in doc_chunk_ids.values() for cid in ids}
        chunk_props: dict[str, dict[str, Any]] = {}
        for raw in raw_nodes:
            try:
                chunk_id, properties = str(raw[0]), raw[1]
            except (TypeError, IndexError, KeyError):
                continue
            if (
                chunk_id in wanted
                and chunk_id not in chunk_props
                and isinstance(properties, dict)
            ):
                chunk_props[chunk_id] = properties
        for doc_id, chunk_ids in doc_chunk_ids.items():
            best: tuple[tuple[int, float, int], str] | None = None
            for position, chunk_id in enumerate(chunk_ids):
                properties = chunk_props.get(chunk_id, {})
                text = properties.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                index = properties.get("chunk_index")
                if isinstance(index, (int, float)) and not isinstance(index, bool):
                    key = (0, float(index), position)
                else:
                    key = (1, 0.0, position)
                if best is None or key < best[0]:
                    best = (key, text)
            if best is None:
                continue
            label = _first_line_label(best[1], _DOC_LABEL_MAX, clean=_strip_summary_lead)
            if not label:
                continue
            node = internal_nodes[doc_id]
            node["internal_name"] = node["label"]
            node["label"] = label

    # Internal-labeled nodes the chunk pass still could not name.
    still_internal: dict[str, dict[str, Any]] = {
        node_id: node
        for node_id, node in internal_nodes.items()
        if _is_internal_label(node_id, node["label"])
    }
    raw_props: dict[str, dict[str, Any]] | None = None

    if collapse_orphan_documents and still_internal:
        # Fold unnamed "orphan" documents — internal-labeled TextDocuments the
        # chunk pass could not name whose only real membership is a NodeSet
        # (legacy session-cache imports) — into that NodeSet node, which then
        # carries a ``collapsed`` count. Keeps the canvas readable: one hub per
        # set instead of a cloud of ``text_<md5>`` dots. Removed ids leave
        # ``kept_ids`` so their edges drop in the edge pass below. Opt-in;
        # default off keeps the exact prior node set. Runs AFTER caller-scoped
        # visibility filtering (KnowledgeMesh.graph), so it is display-only and
        # never widens what a scoped caller can see.
        nodeset_ids = {
            node["id"]
            for node in nodes
            if "nodeset" in str(node.get("type", "")).lower()
        }
        collapse_into: dict[str, str] = {}
        if nodeset_ids:
            for raw in raw_edges:
                try:
                    source, target, relationship = str(raw[0]), str(raw[1]), str(raw[2])
                except (TypeError, IndexError, KeyError):
                    continue
                if relationship != "belongs_to_set":
                    continue
                for doc_id, set_id in ((source, target), (target, source)):
                    if (
                        doc_id in still_internal
                        and set_id in nodeset_ids
                        and "document" in str(node_by_id[doc_id].get("type", "")).lower()
                    ):
                        collapse_into.setdefault(doc_id, set_id)
        for doc_id, set_id in collapse_into.items():
            hub = node_by_id.get(set_id)
            if hub is None:
                continue
            hub["collapsed"] = int(hub.get("collapsed", 0)) + 1
            node_by_id.pop(doc_id, None)
            kept_ids.discard(doc_id)
            still_internal.pop(doc_id, None)
        if collapse_into:
            nodes = [node for node in nodes if node["id"] in kept_ids]

    if still_internal:
        # Fallback labels for the survivors: neighbouring TextSummary, source
        # basename, or NodeSet name (see ``_derive_fallback_label``). One edge
        # sweep resolves each survivor's TextSummary and NodeSet neighbours.
        if raw_props is None:
            raw_props = _raw_props_by_id(raw_nodes)
        nodeset_of: dict[str, str] = {}
        summary_of: dict[str, str] = {}
        for raw in raw_edges:
            try:
                source, target, relationship = str(raw[0]), str(raw[1]), str(raw[2])
            except (TypeError, IndexError, KeyError):
                continue
            for node_id, other_id in ((source, target), (target, source)):
                if node_id not in still_internal:
                    continue
                other_type = _node_type(raw_props.get(other_id, {})).lower()
                if relationship == "belongs_to_set" and "nodeset" in other_type:
                    nodeset_of.setdefault(node_id, other_id)
                elif "summary" in other_type:
                    summary_of.setdefault(node_id, other_id)
        for node_id, node in still_internal.items():
            label = _derive_fallback_label(node_id, raw_props, nodeset_of, summary_of)
            if label:
                node["internal_name"] = node["label"]
                node["label"] = label

    edges: list[dict[str, Any]] = []
    for raw in raw_edges:
        try:
            source, target, relationship = str(raw[0]), str(raw[1]), str(raw[2])
        except (TypeError, IndexError, KeyError):
            continue
        if source not in kept_ids or target not in kept_ids:
            continue
        edges.append(
            {
                "source": source,
                "target": target,
                "relationship": relationship or "related",
            }
        )

    # Raw-count semantics captured before synthetic hubs are appended;
    # ``kept_count`` is the pre-collapse shaped count.
    truncated = len(raw_nodes) > kept_count

    if dataset_map or presence:
        # Synthesize one hub node per dataset attached to at least one KEPT
        # node, plus belongs_to edges. The "dataset:" prefix cannot collide
        # with kuzu UUID ids, so only intra-hub dedupe is needed.
        hub_names: list[str] = []
        seen_hubs: set[str] = set()
        for node in nodes:
            for name in node.get("datasets", []):
                edges.append(
                    {
                        "source": node["id"],
                        "target": f"dataset:{name}",
                        "relationship": "belongs_to",
                    }
                )
                if name not in seen_hubs:
                    seen_hubs.add(name)
                    hub_names.append(name)
        # Universal presence hubs (ADR-0009): every listed seat plus Central
        # appears for every caller, independent of surviving content. Deduped
        # against the content-derived hubs above — one hub per dataset total.
        presence_labels: dict[str, str] = {}
        presence_documents: dict[str, int] = {}
        central_name: str | None = None
        for entry in presence or []:
            name = str(entry.get("dataset") or "").strip()
            if not name:
                continue
            presence_labels.setdefault(name, str(entry.get("label") or name))
            documents = entry.get("documents")
            if isinstance(documents, int) and not isinstance(documents, bool):
                presence_documents.setdefault(name, documents)
            if central_name is None and not name.startswith(_SEAT_PREFIX):
                central_name = name
            if name not in seen_hubs:
                seen_hubs.add(name)
                hub_names.append(name)
        map_counts: dict[str, int] = {}
        if presence is not None:
            for names in dataset_map.values():
                for name in names:
                    map_counts[name] = map_counts.get(name, 0) + 1
        for name in hub_names:
            hub_id = f"dataset:{name}"
            kept_ids.add(hub_id)
            hub: dict[str, Any] = {
                "id": hub_id,
                "label": presence_labels.get(name, name),
                "type": "dataset",
                "dataset": name,
            }
            if presence is not None:
                hub["presence"] = {
                    "documents": presence_documents.get(name, map_counts.get(name, 0))
                }
            nodes.append(hub)
        if presence is not None and central_name is not None:
            # Seat hubs anchor to the Central hub so zero-content seats never
            # float disconnected on the canvas.
            central_hub_id = f"dataset:{central_name}"
            for name in hub_names:
                if not name.startswith(_SEAT_PREFIX):
                    continue
                edges.append(
                    {
                        "source": f"dataset:{name}",
                        "target": central_hub_id,
                        "relationship": "presence",
                    }
                )

    return {
        "nodes": nodes,
        "edges": edges,
        "total_nodes": len(raw_nodes),
        "total_edges": len(raw_edges),
        "truncated": truncated,
    }


def fallback_graph(
    reason: str, presence: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Empty-graph payload; ``presence`` still appends Seat Presence hubs.

    ADR-0009: every seat is ALWAYS visible, so fallback payloads carry the
    universal hubs (and their ``presence`` edges to the Central hub) too.
    Hubs are synthetic — the raw ``total_nodes``/``total_edges`` stay 0.
    ``None`` keeps the exact pre-presence fallback shape.
    """
    return {
        "ok": True,
        **build_graph_payload([], [], presence=presence),
        "fallback": True,
        "fallback_reason": reason,
    }


class KnowledgeMesh:
    """Reads the real knowledge graph through a Cognee gateway."""

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    async def graph(
        self,
        *,
        limit: int = DEFAULT_MAX_NODES,
        dataset_visible: Callable[[str], bool] | None = None,
        dataset: str | None = None,
        seat_first: str | None = None,
        presence: list[dict[str, Any]] | None = None,
        collapse_orphans: bool = False,
    ) -> dict[str, Any]:
        """Shaped graph payload; ``dataset_visible`` scopes CONTENT per caller.

        ``dataset_visible`` (dataset name -> bool) applies ADR-0009 content
        isolation before shaping: content nodes survive only per the layered
        ``_content_visible_ids`` passes (fail-closed — a failed dataset-map
        read hides all content), edges survive only between surviving nodes,
        the node cap applies AFTER filtering, and attribution names the caller
        may not read are stripped — no node tag, no hub, no belongs_to edge.
        ``total_nodes``/``total_edges`` keep the raw org-wide counts while
        ``visible_nodes`` reports the caller-scoped count so the UI can be
        honest. ``None`` applies no filtering (bypass/admin callers) and keeps
        the exact unfiltered behavior.

        ``dataset`` narrows the view to one dataset's subgraph using the SAME
        layered inclusion the visibility pass runs (documents, their chunks,
        adjacent entities), applied AFTER caller scoping — it can only narrow
        what ADR-0009 already allows, never widen. Presence hubs still render
        for every caller, so a non-member naming a foreign dataset sees
        presence and zero content, exactly the unfiltered isolation contract.
        ``None`` (absent query param) keeps today's behavior unchanged.

        ``seat_first`` (a ``seat:<slug>`` dataset name) orders that dataset's
        layered subgraph ahead of the node cap cut, so the caller's own
        content always makes the default sample (16k raw nodes against a
        1000-node cap starved every seat's content before). Ordering stays
        deterministic: the seat cluster, then the rest, both in enumeration
        order.

        ``presence`` appends universal Seat Presence hubs for every caller;
        entry ``documents`` counts are filled from the RAW dataset map —
        presence metadata (slug + contribution counts) is org-visible by
        design (ADR-0009), unlike content.
        """
        graph_data = getattr(self.gateway, "graph_data", None)
        if not callable(graph_data):
            return fallback_graph("graph_access_unavailable", presence)
        try:
            raw_nodes, raw_edges = await graph_data()
        except Exception as exc:
            logger.warning(
                "Knowledge mesh graph read failed with %s; returning fallback graph",
                exc.__class__.__name__,
            )
            return fallback_graph(
                f"graph_engine_error:{exc.__class__.__name__}", presence
            )
        raw_nodes = list(raw_nodes)
        raw_edges = list(raw_edges)
        dataset_map: dict[str, list[str]] = {}
        node_dataset_map = getattr(self.gateway, "node_dataset_map", None)
        if callable(node_dataset_map):
            try:
                dataset_map = await node_dataset_map() or {}
            except Exception as exc:
                logger.warning(
                    "Knowledge mesh dataset map read failed with %s; "
                    "omitting dataset attribution",
                    exc.__class__.__name__,
                )
                dataset_map = {}
        if presence:
            # Fill presence document counts from the RAW map, before caller
            # scoping strips hidden names: contribution counts are presence
            # metadata every Vault Member may see (ADR-0009).
            counts: dict[str, int] = {}
            for names in dataset_map.values():
                for name in names:
                    counts[name] = counts.get(name, 0) + 1
            presence = [
                {**entry, "documents": counts.get(str(entry.get("dataset")), 0)}
                for entry in presence
            ]
        if not raw_nodes:
            # ADR-0009: seats are ALWAYS visible — the empty-graph fallback
            # still renders presence hubs, with document counts from the
            # dataset map read above (0 when nothing is mapped).
            return fallback_graph("graph_empty", presence)
        total_nodes = len(raw_nodes)
        total_edges = len(raw_edges)
        visible_nodes: int | None = None
        if dataset_visible is not None:
            # ADR-0009 scoping is pure Python over already-materialized tuples/
            # dicts and closures (dataset_visible touches no cognee/Kuzu/loop
            # objects), so run the ~100ms visibility scan off the single event
            # loop to avoid starving concurrent requests (#50).
            visible_ids = await asyncio.to_thread(
                _content_visible_ids, raw_nodes, raw_edges, dataset_map, dataset_visible
            )
            visible_nodes = len(visible_ids)
            raw_nodes = [raw for raw in raw_nodes if _raw_id(raw) in visible_ids]
            raw_edges = [
                raw
                for raw in raw_edges
                if (endpoints := _raw_endpoints(raw)) is not None
                and endpoints[0] in visible_ids
                and endpoints[1] in visible_ids
            ]
            filtered: dict[str, list[str]] = {}
            for node_id, names in dataset_map.items():
                kept = [name for name in names if dataset_visible(name)]
                if kept:
                    filtered[node_id] = kept
            dataset_map = filtered
        if dataset is not None:
            # Single-dataset narrowing: reuse the layered visibility pass with
            # the predicate "is this the named dataset" so inclusion semantics
            # (docs -> chunks -> adjacent entities) cannot drift from the
            # ADR-0009 pass above. Runs on the ALREADY caller-scoped rows, so
            # it only narrows. Attribution names on kept nodes are left alone —
            # trust_tier and multi-dataset tags keep today's shaping.
            narrowed_ids = await asyncio.to_thread(
                _content_visible_ids,
                raw_nodes,
                raw_edges,
                dataset_map,
                lambda name: name == dataset,
            )
            raw_nodes = [raw for raw in raw_nodes if _raw_id(raw) in narrowed_ids]
            raw_edges = [
                raw
                for raw in raw_edges
                if (endpoints := _raw_endpoints(raw)) is not None
                and endpoints[0] in narrowed_ids
                and endpoints[1] in narrowed_ids
            ]
            if visible_nodes is not None:
                visible_nodes = len(raw_nodes)
        if seat_first:
            # Seat-first ordering pre-cap: the caller's own cluster (same
            # layered inclusion) moves ahead of the enumeration tail. Pure
            # reorder — nothing is added or dropped, so counts are untouched.
            priority_ids = await asyncio.to_thread(
                _content_visible_ids,
                raw_nodes,
                raw_edges,
                dataset_map,
                lambda name: name == seat_first,
            )
            if priority_ids:
                raw_nodes = [
                    raw for raw in raw_nodes if _raw_id(raw) in priority_ids
                ] + [raw for raw in raw_nodes if _raw_id(raw) not in priority_ids]
        # build_graph_payload is likewise pure Python over plain tuples/dicts —
        # shape it off the loop too (#50).
        payload = await asyncio.to_thread(
            functools.partial(
                build_graph_payload,
                raw_nodes,
                raw_edges,
                limit=limit,
                dataset_map=dataset_map,
                presence=presence,
                collapse_orphan_documents=collapse_orphans,
            )
        )
        if dataset_visible is not None:
            # Raw org-wide totals stay honest about what exists; the caller's
            # scope is reported separately.
            payload["total_nodes"] = total_nodes
            payload["total_edges"] = total_edges
            payload["visible_nodes"] = visible_nodes
        return {"ok": True, "fallback": False, **payload}
