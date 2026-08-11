"""Pure captured-evidence checks for the v0.5 Docker acceptance run.

The later runtime operator supplies already-authenticated bounded responses.
This module never opens sockets, invokes Docker, or accepts credentials.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from kb.access import CENTRAL_DATASET


class ReleaseAcceptanceError(ValueError):
    """Raised when a release receipt cannot prove a required invariant."""


REQUIRED_BACKENDS = ("relational", "vector", "graph")
REQUIRED_SURFACES = ("http", "cli", "mcp")
_CONFIG_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SUSPICIOUS_KEY = re.compile(r"(?:token|authorization|secret|password|api[_-]?key|credential)", re.I)
_TOKEN_LITERAL = re.compile(
    r"(?:bearer\s+\S+|x-api-key\s*[:=]\s*\S+|gh[pousr]_[A-Za-z0-9_]+|"
    r"github_pat_[A-Za-z0-9_]+|ctdl_[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]+|"
    r"eyJ[A-Za-z0-9_-]{10,})",
    re.I,
)
_HIT_TEXT_FIELDS = ("text", "evidence_text", "content")


def load_seed_manifest(path: str | Path) -> dict[str, Any]:
    """Load and structurally validate one deterministic seed fixture."""
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseAcceptanceError(f"seed manifest cannot be loaded: {exc}") from exc
    validate_seed_manifest(manifest)
    return manifest


def validate_seed_manifest(manifest: Mapping[str, Any]) -> None:
    """Reject fixture drift before any runtime proof starts."""
    if manifest.get("schema_version") != 1:
        raise ReleaseAcceptanceError("seed manifest schema_version must be 1")
    if manifest.get("fixture_id") != "citadel-v050-seed-v1":
        raise ReleaseAcceptanceError("seed manifest fixture_id is not citadel-v050-seed-v1")
    generation = _mapping(manifest.get("generation"), "generation")
    if generation.get("generation_id") != "citadel-v050-ring12-g1":
        raise ReleaseAcceptanceError("seed generation_id must be citadel-v050-ring12-g1")
    _nonempty_string(generation.get("projection_version"), "generation.projection_version")
    if "config_digest" in generation:
        raise ReleaseAcceptanceError("manifest must not fabricate a runtime config digest")
    if manifest.get("empty_dataset") != "release-empty-v1":
        raise ReleaseAcceptanceError("seed empty_dataset must be release-empty-v1")
    if manifest.get("central_dataset") != CENTRAL_DATASET:
        raise ReleaseAcceptanceError("seed manifest central_dataset must match the runtime Central dataset")
    if _mapping(manifest.get("expected_counts"), "expected_counts") != {
        "sources": 8,
        "jobs": 8,
        "receipts": 24,
        "chunks": 8,
    }:
        raise ReleaseAcceptanceError("seed manifest must declare 8 sources, 8 jobs, 24 receipts, 8 chunks")
    if manifest.get("search_surfaces") != list(REQUIRED_SURFACES):
        raise ReleaseAcceptanceError("seed manifest search_surfaces must be http, cli, mcp")
    sources = _sources(manifest)
    if len(sources) != 8:
        raise ReleaseAcceptanceError("seed manifest must contain exactly eight source revisions")
    source_ids: set[str] = set()
    sources_by_dataset: dict[str, int] = {}
    for index, source in enumerate(sources):
        source_id = _nonempty_string(source.get("source_id"), f"sources[{index}].source_id")
        dataset = _nonempty_string(source.get("dataset"), f"sources[{index}].dataset")
        content = _nonempty_string(source.get("content"), f"sources[{index}].content")
        runtime_source_key = _nonempty_string(
            source.get("runtime_source_key"), f"sources[{index}].runtime_source_key"
        )
        expected_runtime_source_key = f"manual:{dataset}:{sha256(content.encode('utf-8')).hexdigest()}"
        if runtime_source_key != expected_runtime_source_key:
            raise ReleaseAcceptanceError(f"sources[{index}] runtime_source_key does not match runtime ingest identity")
        operation = _mapping(source.get("operation"), f"sources[{index}].operation")
        _nonempty_string(operation.get("kind"), f"sources[{index}].operation.kind")
        if operation.get("surface") not in REQUIRED_SURFACES:
            raise ReleaseAcceptanceError(f"sources[{index}].operation.surface is invalid")
        if operation.get("expected_source_revisions") != 1:
            raise ReleaseAcceptanceError(f"sources[{index}] must expect exactly one source revision")
        binding = _mapping(operation.get("source_key_binding"), f"sources[{index}].binding")
        if binding != {"from": "GET /api/operations/{projection_job_id}", "count": 1}:
            raise ReleaseAcceptanceError(f"sources[{index}] has an invalid source-key binding")
        payload = operation.get("payload")
        if operation["kind"] == "mcp-share":
            expected_payload = {
                "data": "# Shared Session Trace\n\nCITADEL-V050-SEED-SHARED-TRACE\n",
                "cwd": "/data/release-seed/capture-root",
                "capture_roots": ["/data/release-seed/capture-root"],
                "has_tool_errors": False,
            }
            if payload != expected_payload:
                raise ReleaseAcceptanceError(f"sources[{index}] share payload drifted")
            expected_content = (
                "# Shared Session Trace\nAuthor-Seat: alice\n\n"
                "CITADEL-V050-SEED-SHARED-TRACE"
            )
            if content != expected_content:
                raise ReleaseAcceptanceError(f"sources[{index}] final share content drifted")
        if operation["kind"] == "capture":
            expected_capture = {
                "capture_config": {
                    "version": 1,
                    "node_url": "http://127.0.0.1:8000",
                    "roots": [
                        {
                            "path": "/data/release-seed/capture-root",
                            "tags": ["org-work"],
                        }
                    ],
                    "updated_at": None,
                },
                "registered_roots": {"roots": ["/data/release-seed/capture-root"]},
                "readme": {
                    "path": "README.md",
                    "content": "# Release capture README\n\nCITADEL-V050-SEED-CAPTURE\n"
                    "CITADEL-V050-SEED-PROMOTION\n",
                },
            }
            if payload != expected_capture:
                raise ReleaseAcceptanceError(f"sources[{index}] capture payload drifted")
            expected_summary = (
                "# Capture summary: capture-root\n\n"
                "- Path: `/data/release-seed/capture-root`\n"
                "- Capture Root Tags: org-work\n"
                "- Status: non-git folder\n\n"
                "## README\nCITADEL-V050-SEED-CAPTURE CITADEL-V050-SEED-PROMOTION"
            )
            if content != expected_summary:
                raise ReleaseAcceptanceError(f"sources[{index}] final capture summary drifted")
        readers = _string_list(source.get("readers"), f"sources[{index}].readers")
        if not readers or any(not reader.startswith("seat:") for reader in readers):
            raise ReleaseAcceptanceError(f"sources[{index}].readers must contain seat principals")
        if source_id in source_ids:
            raise ReleaseAcceptanceError("seed source ids must be unique")
        source_ids.add(source_id)
        if len(content.encode("utf-8")) > 2000:
            raise ReleaseAcceptanceError(f"sources[{index}].content exceeds the one-chunk budget")
        sources_by_dataset[dataset] = sources_by_dataset.get(dataset, 0) + 1
    if _mapping(manifest.get("expected_census_by_dataset"), "expected_census_by_dataset") != sources_by_dataset:
        raise ReleaseAcceptanceError("seed manifest dataset census does not match eight sources")
    queries = _queries(manifest)
    if len(queries) != 8:
        raise ReleaseAcceptanceError("seed manifest must contain exactly eight logical query markers")
    query_ids: set[str] = set()
    query_markers: set[str] = set()
    for index, query in enumerate(queries):
        query_id = _nonempty_string(query.get("query_id"), f"queries[{index}].query_id")
        marker = _nonempty_string(query.get("marker"), f"queries[{index}].marker")
        _nonempty_string(query.get("dataset"), f"queries[{index}].dataset")
        actors = _string_list(query.get("actors"), f"queries[{index}].actors")
        ids = _string_list(query.get("source_ids"), f"queries[{index}].source_ids")
        if not actors or any(not actor.startswith("seat:") for actor in actors):
            raise ReleaseAcceptanceError(f"queries[{index}].actors must contain seat principals")
        if not ids or any(source_id not in source_ids for source_id in ids):
            raise ReleaseAcceptanceError(f"queries[{index}].source_ids must name seed sources")
        if query_id in query_ids or marker in query_markers:
            raise ReleaseAcceptanceError("logical query ids and markers must be unique")
        query_ids.add(query_id)
        query_markers.add(marker)
    for query in queries:
        marker = str(query["marker"])
        query_source_ids = _string_list(query["source_ids"], "query source_ids")
        query_sources = [source for source in sources if source["source_id"] in query_source_ids]
        if not any(marker in str(source["content"]) for source in query_sources):
            raise ReleaseAcceptanceError("logical query marker is absent from declared source content")
        actor_datasets = query.get("actor_datasets")
        actor_source_ids = query.get("actor_source_ids")
        if actor_datasets is not None or actor_source_ids is not None:
            datasets = _mapping(actor_datasets, "query actor_datasets")
            actor_ids = _mapping(actor_source_ids, "query actor_source_ids")
            if set(datasets) != set(query["actors"]) or set(actor_ids) != set(query["actors"]):
                raise ReleaseAcceptanceError("actor-specific query rules must cover every actor")
            for actor in query["actors"]:
                allowed_datasets = _string_list(datasets[actor], "actor allowed datasets")
                allowed_sources = _string_list(actor_ids[actor], "actor allowed source ids")
                if not allowed_datasets or any(source_id not in query_source_ids for source_id in allowed_sources):
                    raise ReleaseAcceptanceError("actor-specific query rules are invalid")


def validate_release_evidence(manifest: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Run all acceptance checks and return a credential-free receipt."""
    validate_seed_manifest(manifest)
    validate_no_secret_literals(evidence)
    identity = _runtime_identity(manifest, evidence)
    bindings = validate_operation_bindings(
        manifest, _sequence(evidence.get("operation_bindings"), "evidence.operation_bindings"), identity
    )
    validate_search_surfaces(
        manifest, _mapping(evidence.get("search"), "evidence.search"), identity, bindings
    )
    validate_current_head_evidence(
        manifest, _mapping(evidence.get("current_head"), "evidence.current_head"), identity, bindings
    )
    validate_lifecycle_census(
        manifest, _mapping(evidence.get("lifecycle_census"), "evidence.lifecycle_census"), identity
    )
    validate_corpus_graph_presence(
        manifest, _mapping(evidence.get("corpus"), "evidence.corpus"), bindings
    )
    validate_visibility_matrix(manifest, _mapping(evidence.get("visibility"), "evidence.visibility"), bindings)
    return redacted_receipt(manifest, evidence, identity)


def validate_operation_bindings(
    manifest: Mapping[str, Any], rows: Sequence[Any], identity: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """Bind stable fixture aliases to keys returned by lifecycle operations."""
    sources = {source["source_id"]: source for source in _sources(manifest)}
    if len(rows) != len(sources):
        raise ReleaseAcceptanceError("operation bindings must contain exactly eight rows")
    bindings: dict[str, dict[str, Any]] = {}
    runtime_keys: set[str] = set()
    for row in rows:
        item = _mapping(row, "operation binding")
        source_id = _nonempty_string(item.get("source_id"), "operation binding source_id")
        if source_id not in sources or source_id in bindings:
            raise ReleaseAcceptanceError("operation binding source_id is unknown or duplicate")
        operation = _mapping(item.get("operation"), "operation binding operation")
        revision = _mapping(operation.get("source_revision"), "operation source_revision")
        job = _mapping(operation.get("job"), "operation job")
        source_key = _nonempty_string(revision.get("source_key"), "operation source_key")
        source_revision_id = _nonempty_string(
            revision.get("source_revision_id"), "operation source_revision_id"
        )
        projection_job_id = _nonempty_string(
            operation.get("projection_job_id"), "operation projection_job_id"
        )
        if job.get("projection_job_id") != projection_job_id:
            raise ReleaseAcceptanceError("operation job id does not match operation id")
        if job.get("source_revision_id") != source_revision_id:
            raise ReleaseAcceptanceError("operation job source revision does not match")
        if revision.get("dataset") != sources[source_id]["dataset"]:
            raise ReleaseAcceptanceError("operation dataset does not match source alias")
        if source_key != sources[source_id]["runtime_source_key"]:
            raise ReleaseAcceptanceError("operation source key does not match runtime ingest identity")
        if job.get("dataset") != sources[source_id]["dataset"]:
            raise ReleaseAcceptanceError("operation job dataset does not match source alias")
        if job.get("generation_id") != identity["generation_id"]:
            raise ReleaseAcceptanceError("operation generation does not match runtime identity")
        if job.get("projection_version") != identity["projection_version"]:
            raise ReleaseAcceptanceError("operation version does not match runtime identity")
        receipts = _sequence(operation.get("receipts"), "operation receipts")
        if [receipt.get("backend") for receipt in receipts] != list(REQUIRED_BACKENDS):
            raise ReleaseAcceptanceError("operation receipts must be relational, vector, graph")
        receipt_ids: dict[str, str] = {}
        providers: dict[str, str] = {}
        for receipt in receipts:
            receipt_item = _mapping(receipt, "operation receipt")
            if receipt_item.get("projection_job_id") != projection_job_id:
                raise ReleaseAcceptanceError("operation receipt job does not match")
            if receipt_item.get("source_revision_id") != source_revision_id:
                raise ReleaseAcceptanceError("operation receipt source revision does not match")
            if receipt_item.get("generation_id") != identity["generation_id"]:
                raise ReleaseAcceptanceError("operation receipt generation does not match")
            if receipt_item.get("projection_version") != identity["projection_version"]:
                raise ReleaseAcceptanceError("operation receipt version does not match")
            if receipt_item.get("state") != "searchable":
                raise ReleaseAcceptanceError("operation receipt is not searchable")
            receipt_id = _nonempty_string(
                receipt_item.get("projection_receipt_id"), "operation projection_receipt_id"
            )
            _nonempty_string(receipt_item.get("provider"), "operation receipt provider")
            backend = str(receipt_item["backend"])
            receipt_ids[backend] = receipt_id
            providers[backend] = str(receipt_item["provider"])
        if not receipt_ids.get("vector") or source_key in runtime_keys:
            raise ReleaseAcceptanceError("operation runtime source keys must be distinct and non-empty")
        runtime_keys.add(source_key)
        bindings[source_id] = {
            "source_key": source_key,
            "source_revision_id": source_revision_id,
            "projection_job_id": projection_job_id,
            "receipt_ids": receipt_ids,
            "providers": providers,
            "dataset": str(sources[source_id]["dataset"]),
        }
    if set(bindings) != set(sources):
        raise ReleaseAcceptanceError("operation bindings are incomplete")
    return bindings


def validate_search_surfaces(
    manifest: Mapping[str, Any],
    evidence: Mapping[str, Any],
    identity: Mapping[str, str],
    bindings: Mapping[str, Mapping[str, Any]],
) -> None:
    """Require one exact text-marker hit per actor/query/surface with stable IDs."""
    expected = {
        (actor, query["query_id"], surface)
        for query in _queries(manifest)
        for actor in query["actors"]
        for surface in REQUIRED_SURFACES
    }
    queries = {query["query_id"]: query for query in _queries(manifest)}
    rows = _sequence(evidence.get("populated"), "search.populated")
    seen: set[tuple[str, str, str]] = set()
    per_actor_query: dict[tuple[str, str], dict[str, tuple[str, str, str, str, str]]] = {}
    for index, row in enumerate(rows):
        item = _mapping(row, f"search.populated[{index}]")
        actor = _nonempty_string(item.get("actor"), f"search.populated[{index}].actor")
        query_id = _nonempty_string(item.get("query_id"), f"search.populated[{index}].query_id")
        surface = _nonempty_string(item.get("surface"), f"search.populated[{index}].surface")
        key = (actor, query_id, surface)
        if key not in expected or key in seen:
            raise ReleaseAcceptanceError(f"unexpected or duplicate populated search row: {key!r}")
        seen.add(key)
        query = queries[query_id]
        hit = _one_exact_marker_hit(surface, _mapping(item.get("response"), "search response"), query)
        observed = _hit_identity(hit, query, actor, identity, bindings)
        per_actor_query.setdefault((actor, query_id), {})[surface] = observed
    if seen != expected:
        raise ReleaseAcceptanceError("populated search evidence does not cover every actor/query/surface")
    for key, by_surface in per_actor_query.items():
        if set(by_surface) != set(REQUIRED_SURFACES):
            raise ReleaseAcceptanceError(f"surface identity missing for actor/query {key!r}")
        if len(set(by_surface.values())) != 1:
            raise ReleaseAcceptanceError(f"HTTP, CLI, MCP identities diverged for actor/query {key!r}")
    empty_expected = {
        (actor, surface)
        for actor in sorted({actor for query in _queries(manifest) for actor in query["actors"]})
        for surface in REQUIRED_SURFACES
    }
    empty_rows = _sequence(evidence.get("empty"), "search.empty")
    empty_seen: set[tuple[str, str]] = set()
    for index, row in enumerate(empty_rows):
        item = _mapping(row, f"search.empty[{index}]")
        key = (
            _nonempty_string(item.get("actor"), "empty actor"),
            _nonempty_string(item.get("surface"), "empty surface"),
        )
        if key not in empty_expected or key in empty_seen:
            raise ReleaseAcceptanceError(f"unexpected or duplicate empty search row: {key!r}")
        empty_seen.add(key)
        if item.get("dataset") != manifest["empty_dataset"]:
            raise ReleaseAcceptanceError("empty search did not use release-empty-v1")
        if _successful_hits(key[1], _mapping(item.get("response"), "empty response")):
            raise ReleaseAcceptanceError(f"{key[1]} empty search returned hits")
    if empty_seen != empty_expected:
        raise ReleaseAcceptanceError("genuine-empty evidence does not cover every actor and surface")


def validate_current_head_evidence(
    manifest: Mapping[str, Any],
    by_dataset: Mapping[str, Any],
    identity: Mapping[str, str],
    bindings: Mapping[str, Mapping[str, str]],
) -> None:
    """Require eight active revisions, jobs, and three searchable receipts each."""
    expected_by_dataset = _sources_by_dataset(manifest)
    if set(by_dataset) != set(expected_by_dataset):
        raise ReleaseAcceptanceError("current-head evidence datasets do not match seed manifest")
    for dataset, sources in expected_by_dataset.items():
        result = _mapping(by_dataset[dataset], f"current_head.{dataset}")
        if result.get("ok") is not True or result.get("errors") not in ([], ()):
            raise ReleaseAcceptanceError(f"current-head evidence failed for {dataset}")
        _assert_runtime_identity(result, identity, f"current_head.{dataset}")
        rows = _sequence(result.get("evidence"), f"current_head.{dataset}.evidence")
        expected_keys = [bindings[source["source_id"]]["source_key"] for source in sources]
        if [row.get("source_key") for row in rows] != expected_keys:
            raise ReleaseAcceptanceError(f"current-head source order or membership drifted for {dataset}")
        for source, row in zip(sources, rows, strict=True):
            item = _mapping(row, "current-head evidence row")
            if item.get("dataset") != dataset or item.get("state") != "searchable":
                raise ReleaseAcceptanceError(f"current-head row is not searchable in {dataset}")
            binding = bindings[source["source_id"]]
            if item.get("source_revision_id") != binding["source_revision_id"]:
                raise ReleaseAcceptanceError("current-head source revision does not match operation binding")
            if item.get("projection_job_id") != binding["projection_job_id"]:
                raise ReleaseAcceptanceError("current-head job does not match operation binding")
            _assert_runtime_identity(item, identity, "current-head evidence row")
            receipts = _sequence(item.get("receipts"), "current-head receipts")
            if [receipt.get("backend") for receipt in receipts] != list(REQUIRED_BACKENDS):
                raise ReleaseAcceptanceError("current-head receipts must be relational, vector, graph")
            for receipt in receipts:
                if receipt.get("state") != "searchable":
                    raise ReleaseAcceptanceError("current-head receipt is not searchable")
                _nonempty_string(receipt.get("projection_receipt_id"), "projection_receipt_id")
                _nonempty_string(receipt.get("provider"), "projection receipt provider")
                backend = str(receipt["backend"])
                if receipt.get("projection_receipt_id") != binding["receipt_ids"][backend]:
                    raise ReleaseAcceptanceError("current-head receipt does not match operation binding")
                if receipt.get("provider") != binding["providers"][backend]:
                    raise ReleaseAcceptanceError("current-head receipt provider does not match operation binding")


def validate_lifecycle_census(
    manifest: Mapping[str, Any], census: Mapping[str, Any], identity: Mapping[str, str]
) -> None:
    """Require exact source/job/receipt arithmetic, never a similarity estimate."""
    expected = _mapping(manifest["expected_counts"], "expected_counts")
    for field, value in {
        "source_revisions": expected["sources"],
        "current_sources": expected["sources"],
        "projection_jobs": expected["jobs"],
        "projection_receipts": expected["receipts"],
    }.items():
        if census.get(field) != value:
            raise ReleaseAcceptanceError(f"lifecycle census {field} must equal {value}")
    generation = _mapping(census.get("current_generation"), "lifecycle current_generation")
    _assert_runtime_identity(generation, identity, "lifecycle current_generation")
    if generation.get("current_projection_jobs") != expected["jobs"]:
        raise ReleaseAcceptanceError("current generation job census mismatch")
    if generation.get("current_projection_receipts") != expected["receipts"]:
        raise ReleaseAcceptanceError("current generation receipt census mismatch")
    expected_backend_counts = {backend: expected["sources"] for backend in REQUIRED_BACKENDS}
    for field in ("current_receipts_by_backend", "current_searchable_by_backend"):
        if _mapping(generation.get(field), f"lifecycle {field}") != expected_backend_counts:
            raise ReleaseAcceptanceError(f"lifecycle {field} must show eight rows per backend")


def validate_corpus_graph_presence(
    manifest: Mapping[str, Any], evidence: Mapping[str, Any], bindings: Mapping[str, Mapping[str, Any]]
) -> None:
    """Require exactly eight operator-mapped source rows, one chunk, and graph presence."""
    rows = _sequence(evidence.get("source_rows"), "corpus.source_rows")
    expected = {source["source_id"]: source for source in _sources(manifest)}
    if len(rows) != len(expected):
        raise ReleaseAcceptanceError("corpus evidence must contain exactly eight source rows")
    seen_ids: set[str] = set()
    document_ids: set[str] = set()
    for row in rows:
        item = _mapping(row, "corpus source row")
        source_id = _nonempty_string(item.get("source_id"), "corpus source_id")
        source_key = _nonempty_string(item.get("source_key"), "corpus source_key")
        document_id = _nonempty_string(item.get("document_id"), "corpus document_id")
        source = expected.get(source_id)
        if source is None or source_id in seen_ids or document_id in document_ids:
            raise ReleaseAcceptanceError("corpus source/document mapping is unknown or duplicate")
        if source_key != bindings[source_id]["source_key"]:
            raise ReleaseAcceptanceError("corpus source key does not match operation binding")
        if item.get("dataset") != source["dataset"]:
            raise ReleaseAcceptanceError(f"corpus dataset mismatch for {source_id}")
        if item.get("chunk_count") != 1 or item.get("in_graph") is not True:
            raise ReleaseAcceptanceError(f"corpus one-chunk or graph proof failed for {source_id}")
        seen_ids.add(source_id)
        document_ids.add(document_id)
    if seen_ids != set(expected):
        raise ReleaseAcceptanceError("corpus source evidence is incomplete")


def validate_visibility_matrix(
    manifest: Mapping[str, Any], matrix: Mapping[str, Any], bindings: Mapping[str, Mapping[str, Any]]
) -> None:
    """Require every reader/source decision, including forbidden cross-seat reads."""
    readers = sorted({reader for source in _sources(manifest) for reader in source["readers"]})
    if set(matrix) != set(readers):
        raise ReleaseAcceptanceError("visibility matrix principals do not match manifest readers")
    expected_keys = {bindings[source["source_id"]]["source_key"] for source in _sources(manifest)}
    for reader in readers:
        decisions = _mapping(matrix[reader], f"visibility.{reader}")
        if set(decisions) != expected_keys:
            raise ReleaseAcceptanceError(f"visibility matrix is incomplete for {reader}")
        for source in _sources(manifest):
            source_key = bindings[source["source_id"]]["source_key"]
            if decisions[source_key] is not (reader in source["readers"]):
                raise ReleaseAcceptanceError(
                    f"visibility mismatch for {reader} and {source['source_id']}"
                )


def validate_no_secret_literals(value: Any, path: str = "evidence") -> None:
    """Fail closed when captured evidence contains credential-shaped data."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SUSPICIOUS_KEY.search(str(key)):
                raise ReleaseAcceptanceError(f"suspicious credential key at {path}.{key}")
            validate_no_secret_literals(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_no_secret_literals(item, f"{path}[{index}]")
    elif isinstance(value, str) and _TOKEN_LITERAL.search(value):
        raise ReleaseAcceptanceError(f"credential-shaped literal at {path}")


def redacted_receipt(
    manifest: Mapping[str, Any], evidence: Mapping[str, Any], identity: Mapping[str, str]
) -> dict[str, Any]:
    """Return reproducibility identifiers, not raw markers, hits, or credentials."""
    return {
        "fixture_id": manifest["fixture_id"],
        "projection_identity": dict(identity),
        "source_count": len(_sources(manifest)),
        "logical_query_count": len(_queries(manifest)),
        "surface_count": len(REQUIRED_SURFACES),
        "marker_sha256": {
            query["query_id"]: sha256(query["marker"].encode("utf-8")).hexdigest()
            for query in _queries(manifest)
        },
        "evidence_sha256": sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "blind_spot": "document_id_to_source_key mapping is operator evidence",
    }


def _runtime_identity(manifest: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, str]:
    identity = _mapping(evidence.get("projection_identity"), "evidence.projection_identity")
    generation = _mapping(manifest["generation"], "generation")
    if identity.get("generation_id") != generation["generation_id"]:
        raise ReleaseAcceptanceError("runtime generation_id does not match manifest")
    if identity.get("projection_version") != generation["projection_version"]:
        raise ReleaseAcceptanceError("runtime projection_version does not match manifest")
    digest = _nonempty_string(identity.get("config_digest"), "runtime config_digest")
    if not _CONFIG_DIGEST.fullmatch(digest):
        raise ReleaseAcceptanceError("runtime config_digest must be a nonempty sha256 digest")
    return {
        "generation_id": str(identity["generation_id"]),
        "projection_version": str(identity["projection_version"]),
        "config_digest": digest,
    }


def _successful_hits(surface: str, response: Mapping[str, Any]) -> list[Any]:
    if surface == "http":
        if response.get("status") != 200:
            raise ReleaseAcceptanceError("HTTP search did not return 200")
        body = _mapping(response.get("body"), "HTTP response body")
    elif surface == "cli":
        if response.get("exit_code") != 0:
            raise ReleaseAcceptanceError("CLI search did not exit zero")
        body = _mapping(response.get("json"), "CLI JSON output")
        if body.get("ok") is False:
            raise ReleaseAcceptanceError("CLI search reported ok=false")
    elif surface == "mcp":
        if response.get("isError") is True:
            raise ReleaseAcceptanceError("MCP search returned an error")
        body = _mapping(response.get("result"), "MCP result")
    else:
        raise ReleaseAcceptanceError(f"unknown search surface: {surface}")
    return list(_sequence(body.get("results"), f"{surface} results"))


def _one_exact_marker_hit(
    surface: str, response: Mapping[str, Any], query: Mapping[str, Any]
) -> Mapping[str, Any]:
    hits = _successful_hits(surface, response)
    marker = str(query["marker"])
    matching = [
        hit
        for hit in hits
        if isinstance(hit, Mapping) and _marker_in_hit_text(hit, marker)
    ]
    if len(matching) != 1:
        raise ReleaseAcceptanceError(f"{surface} must return one exact marker hit")
    return matching[0]


def _hit_text(hit: Mapping[str, Any]) -> str:
    texts = [hit[field] for field in _HIT_TEXT_FIELDS if isinstance(hit.get(field), str)]
    if not texts:
        raise ReleaseAcceptanceError("search hit has no actual evidence text field")
    return "\n".join(texts)


def _marker_in_hit_text(hit: Mapping[str, Any], marker: str) -> bool:
    texts = [hit[field] for field in _HIT_TEXT_FIELDS if isinstance(hit.get(field), str)]
    return any(marker in text for text in texts)


def _hit_identity(
    hit: Mapping[str, Any],
    query: Mapping[str, Any],
    actor: str,
    identity: Mapping[str, str],
    bindings: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str, str, str, str]:
    citadel = _mapping(hit.get("_citadel"), "search hit _citadel")
    allowed_datasets = _actor_rule(query, actor, "actor_datasets", [str(query["dataset"])])
    if citadel.get("dataset") not in allowed_datasets:
        raise ReleaseAcceptanceError("search hit dataset does not match actor query rule")
    projection = _mapping(citadel.get("projection"), "search hit projection")
    if projection.get("generation_id") != identity["generation_id"]:
        raise ReleaseAcceptanceError("search hit generation does not match runtime identity")
    if projection.get("projection_version") != identity["projection_version"]:
        raise ReleaseAcceptanceError("search hit projection version does not match runtime identity")
    observed = (
        _nonempty_string(citadel.get("dataset"), "search hit dataset"),
        _nonempty_string(citadel.get("result_id"), "search hit result_id"),
        _nonempty_string(citadel.get("source_revision_id"), "search hit source_revision_id"),
        _nonempty_string(citadel.get("projection_receipt_id"), "search hit projection_receipt_id"),
        _nonempty_string(projection.get("generation_id"), "search hit projection.generation_id"),
    )
    default_source_ids = (
        [str(query["expected_source_id"])] if query.get("actor_source_ids") is None else []
    )
    allowed_source_ids = _actor_rule(query, actor, "actor_source_ids", default_source_ids)
    allowed_bindings = [bindings[source_id] for source_id in allowed_source_ids]
    if not any(
        observed[0] == binding["dataset"]
        and observed[2] == binding["source_revision_id"]
        and observed[3] == binding["receipt_ids"]["vector"]
        for binding in allowed_bindings
    ):
        raise ReleaseAcceptanceError("search hit does not match an operation-bound source")
    return observed


def _actor_rule(
    query: Mapping[str, Any], actor: str, field: str, default: list[str]
) -> list[str]:
    rules = query.get(field)
    if rules is None:
        return default
    return _string_list(_mapping(rules, field).get(actor), f"{field}.{actor}")


def _assert_runtime_identity(value: Mapping[str, Any], identity: Mapping[str, str], context: str) -> None:
    for field, expected in identity.items():
        if value.get(field) != expected:
            raise ReleaseAcceptanceError(f"{context} {field} does not match runtime identity")


def _sources(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(_mapping(source, "source")) for source in _sequence(manifest["sources"], "sources")]


def _queries(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(_mapping(query, "query")) for query in _sequence(manifest["queries"], "queries")]


def _sources_by_dataset(manifest: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for source in _sources(manifest):
        grouped.setdefault(str(source["dataset"]), []).append(source)
    return grouped


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseAcceptanceError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise ReleaseAcceptanceError(f"{name} must be an array")
    return value


def _string_list(value: Any, name: str) -> list[str]:
    values = list(_sequence(value, name))
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ReleaseAcceptanceError(f"{name} must contain non-empty strings")
    return values


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseAcceptanceError(f"{name} must be a non-empty string")
    return value
