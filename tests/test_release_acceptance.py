from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb.capture import summarize_root
from kb.capture_config import CaptureRoot
from kb.release_acceptance import ReleaseAcceptanceError, validate_release_evidence


FIXTURE = Path(__file__).parent / "fixtures" / "citadel_v050_seed_v1.json"
CONFIG_DIGEST = "sha256:" + "a" * 64


def _manifest() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _response(surface: str, results: list[object]) -> dict[str, object]:
    body = {"ok": True, "results": results}
    if surface == "http":
        return {"status": 200, "body": body}
    if surface == "cli":
        return {"exit_code": 0, "json": body}
    return {"isError": False, "result": body}


def _identity(manifest: dict[str, object]) -> dict[str, str]:
    generation = manifest["generation"]
    assert isinstance(generation, dict)
    return {
        "generation_id": str(generation["generation_id"]),
        "projection_version": str(generation["projection_version"]),
        "config_digest": CONFIG_DIGEST,
    }


def _evidence(manifest: dict[str, object]) -> dict[str, object]:
    identity = _identity(manifest)
    sources = manifest["sources"]
    queries = manifest["queries"]
    surfaces = manifest["search_surfaces"]
    assert isinstance(sources, list) and isinstance(queries, list) and isinstance(surfaces, list)
    bindings: dict[str, dict[str, str]] = {}
    operation_bindings = []
    current_head: dict[str, object] = {}
    corpus_rows = []
    visibility: dict[str, dict[str, bool]] = {"seat:alice": {}, "seat:bob": {}}
    by_dataset: dict[str, list[dict[str, object]]] = {}
    for index, source in enumerate(sources):
        assert isinstance(source, dict)
        source_id = str(source["source_id"])
        dataset = str(source["dataset"])
        binding = {
            "source_key": str(source["runtime_source_key"]),
            "source_revision_id": f"revision-{index}",
            "projection_job_id": f"job-{index}",
            "vector_receipt_id": f"receipt-{index}-vector",
            "dataset": dataset,
        }
        bindings[source_id] = binding
        operation_bindings.append(
            {
                "source_id": source_id,
                "operation": {
                    "projection_job_id": binding["projection_job_id"],
                    "source_revision": {
                        "source_key": binding["source_key"],
                        "source_revision_id": binding["source_revision_id"],
                        "dataset": dataset,
                    },
                    "job": {
                        "projection_job_id": binding["projection_job_id"],
                        "source_revision_id": binding["source_revision_id"],
                        "dataset": dataset,
                        "generation_id": identity["generation_id"],
                        "projection_version": identity["projection_version"],
                    },
                    "receipts": [
                        {
                            "backend": backend,
                            "state": "searchable",
                            "projection_receipt_id": f"receipt-{index}-{backend}",
                            "projection_job_id": binding["projection_job_id"],
                            "source_revision_id": binding["source_revision_id"],
                            "generation_id": identity["generation_id"],
                            "projection_version": identity["projection_version"],
                            "provider": {"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"}[backend],
                        }
                        for backend in ("relational", "vector", "graph")
                    ],
                },
            }
        )
        corpus_rows.append(
            {
                "source_id": source_id,
                "source_key": binding["source_key"],
                "document_id": f"document-{index}",
                "dataset": dataset,
                "chunk_count": 1,
                "in_graph": True,
            }
        )
        by_dataset.setdefault(dataset, []).append(source)
        readers = source["readers"]
        assert isinstance(readers, list)
        for actor in visibility:
            visibility[actor][binding["source_key"]] = actor in readers
    for dataset, dataset_sources in by_dataset.items():
        rows = []
        for source in dataset_sources:
            source_id = str(source["source_id"])
            binding = bindings[source_id]
            index = int(binding["projection_job_id"].split("-")[-1])
            rows.append(
                {
                    "source_key": binding["source_key"],
                    "dataset": dataset,
                    "source_revision_id": binding["source_revision_id"],
                    "projection_job_id": binding["projection_job_id"],
                    "state": "searchable",
                    **identity,
                    "receipts": [
                        {
                            "backend": backend,
                            "state": "searchable",
                            "projection_receipt_id": f"receipt-{index}-{backend}",
                            "provider": {"relational": "sqlite", "vector": "qdrant", "graph": "ladybug"}[backend],
                        }
                        for backend in ("relational", "vector", "graph")
                    ],
                }
            )
        current_head[dataset] = {"ok": True, "errors": [], **identity, "evidence": rows}
    populated = []
    for query in queries:
        assert isinstance(query, dict)
        for actor in query["actors"]:
            assert isinstance(actor, str)
            source_id = _query_source_id(query, actor)
            binding = bindings[source_id]
            for surface in surfaces:
                assert isinstance(surface, str)
                hit = {
                    "text": f"exact runtime evidence: {query['marker']}",
                    "_citadel": {
                        "dataset": binding["dataset"],
                        "result_id": f"result-{query['query_id']}-{actor}",
                        "source_revision_id": binding["source_revision_id"],
                        "projection_receipt_id": binding["vector_receipt_id"],
                        "projection": {
                            "generation_id": identity["generation_id"],
                            "projection_version": identity["projection_version"],
                        },
                    },
                }
                populated.append(
                    {
                        "actor": actor,
                        "query_id": query["query_id"],
                        "surface": surface,
                        "response": _response(surface, [{"text": "approximate"}, hit]),
                    }
                )
    empty = [
        {
            "actor": actor,
            "dataset": manifest["empty_dataset"],
            "surface": surface,
            "response": _response(surface, []),
        }
        for actor in visibility
        for surface in surfaces
    ]
    return {
        "projection_identity": identity,
        "operation_bindings": operation_bindings,
        "search": {"populated": populated, "empty": empty},
        "current_head": current_head,
        "lifecycle_census": {
            "source_revisions": 8,
            "current_sources": 8,
            "projection_jobs": 8,
            "projection_receipts": 24,
            "current_generation": {
                **identity,
                "current_projection_jobs": 8,
                "current_projection_receipts": 24,
                "current_receipts_by_backend": {"relational": 8, "vector": 8, "graph": 8},
                "current_searchable_by_backend": {"relational": 8, "vector": 8, "graph": 8},
            },
        },
        "corpus": {"source_rows": corpus_rows},
        "visibility": visibility,
    }


def _query_source_id(query: dict[str, object], actor: str) -> str:
    rules = query.get("actor_source_ids")
    if isinstance(rules, dict):
        candidates = rules[actor]
        assert isinstance(candidates, list)
        return str(candidates[0])
    return str(query["expected_source_id"])


def test_release_evidence_requires_all_acceptance_receipts() -> None:
    manifest = _manifest()
    receipt = validate_release_evidence(manifest, _evidence(manifest))
    assert receipt["source_count"] == 8
    assert receipt["logical_query_count"] == 8
    assert receipt["blind_spot"] == "document_id_to_source_key mapping is operator evidence"


def test_release_evidence_rejects_provider_failure_as_empty() -> None:
    manifest = _manifest()
    evidence = _evidence(manifest)
    empty = evidence["search"]["empty"]  # type: ignore[index]
    assert isinstance(empty, list) and isinstance(empty[0], dict)
    empty[0]["response"] = {"status": 503, "body": {"results": []}}
    with pytest.raises(ReleaseAcceptanceError, match="HTTP search did not return 200"):
        validate_release_evidence(manifest, evidence)


def test_release_evidence_rejects_marker_in_metadata_only() -> None:
    manifest = _manifest()
    evidence = _evidence(manifest)
    populated = evidence["search"]["populated"]  # type: ignore[index]
    assert isinstance(populated, list) and isinstance(populated[0], dict)
    results = populated[0]["response"]["body"]["results"]  # type: ignore[index]
    assert isinstance(results, list) and isinstance(results[1], dict)
    results[1]["text"] = "unrelated approximate material"
    results[1]["marker"] = "CITADEL-V050-SEED-CENTRAL"
    with pytest.raises(ReleaseAcceptanceError, match="one exact marker hit"):
        validate_release_evidence(manifest, evidence)


def test_release_evidence_rejects_fabricated_runtime_source_key() -> None:
    manifest = _manifest()
    evidence = _evidence(manifest)
    bindings = evidence["operation_bindings"]  # type: ignore[index]
    assert isinstance(bindings, list) and isinstance(bindings[0], dict)
    operation = bindings[0]["operation"]
    assert isinstance(operation, dict)
    revision = operation["source_revision"]
    assert isinstance(revision, dict)
    revision["source_key"] = "manual:masumi-network:runtime-fixture"

    with pytest.raises(ReleaseAcceptanceError, match="source key does not match"):
        validate_release_evidence(manifest, evidence)


def test_release_evidence_rejects_credential_literal() -> None:
    manifest = _manifest()
    evidence = _evidence(manifest)
    evidence["credential_value"] = "Bearer runtime-value"
    with pytest.raises(ReleaseAcceptanceError, match="suspicious credential key"):
        validate_release_evidence(manifest, evidence)


def test_release_evidence_rejects_raw_ctdl_token_under_innocent_key() -> None:
    manifest = _manifest()
    evidence = _evidence(manifest)
    evidence["note"] = "ctdl_runtime_token_value"
    with pytest.raises(ReleaseAcceptanceError, match="credential-shaped literal"):
        validate_release_evidence(manifest, evidence)


def test_capture_summary_matches_manifest_final_content(tmp_path: Path) -> None:
    manifest = _manifest()
    root = tmp_path / "capture-root"
    root.mkdir()
    (root / "README.md").write_text(
        "# Release capture README\n\nCITADEL-V050-SEED-CAPTURE\n"
        "CITADEL-V050-SEED-PROMOTION\n",
        encoding="utf-8",
    )
    summary = summarize_root(CaptureRoot(path=str(root), tags=("org-work",)))
    source = next(item for item in manifest["sources"] if item["source_id"] == "capture-promotion")
    assert isinstance(source, dict)
    assert summary == source["content"].replace("/data/release-seed/capture-root", str(root))
