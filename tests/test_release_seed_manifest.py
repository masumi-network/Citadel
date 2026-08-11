from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from kb.release_acceptance import ReleaseAcceptanceError, load_seed_manifest, validate_seed_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "citadel_v050_seed_v1.json"


def test_seed_manifest_is_exact_ring1_arithmetic() -> None:
    manifest = load_seed_manifest(FIXTURE)

    assert manifest["expected_counts"] == {
        "sources": 8,
        "jobs": 8,
        "receipts": 24,
        "chunks": 8,
    }
    assert len(manifest["sources"]) == 8
    assert len(manifest["queries"]) == 8
    assert manifest["generation"] == {
        "generation_id": "citadel-v050-ring12-g1",
        "projection_version": "lifecycle-v1:cognee-1.4.1",
    }
    assert manifest["empty_dataset"] == "release-empty-v1"
    assert manifest["search_surfaces"] == ["http", "cli", "mcp"]
    capture = next(source for source in manifest["sources"] if source["source_id"] == "capture-promotion")
    payload = capture["operation"]["payload"]
    assert payload["capture_config"] == {
        "version": 1,
        "node_url": "http://127.0.0.1:8000",
        "roots": [{"path": "/data/release-seed/capture-root", "tags": ["org-work"]}],
        "updated_at": None,
    }
    assert payload["registered_roots"] == {"roots": ["/data/release-seed/capture-root"]}
    for source in manifest["sources"]:
        assert source["runtime_source_key"] == (
            f"manual:{source['dataset']}:{sha256(source['content'].encode('utf-8')).hexdigest()}"
        )


def test_seed_manifest_rejects_duplicate_logical_marker() -> None:
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    manifest["queries"][1]["marker"] = manifest["queries"][0]["marker"]

    with pytest.raises(ReleaseAcceptanceError, match="unique"):
        validate_seed_manifest(manifest)


def test_seed_manifest_rejects_non_runtime_central_dataset() -> None:
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    manifest["central_dataset"] = "central"

    with pytest.raises(ReleaseAcceptanceError, match="Central dataset"):
        validate_seed_manifest(manifest)
