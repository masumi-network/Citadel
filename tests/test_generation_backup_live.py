from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models

from kb.generation_backup import (
    GenerationBackupError,
    QdrantSnapshotStore,
    create_generation_backup,
    restore_generation_backup,
)
from tests.test_cognee_qdrant_sqlite_live import _run_worker
from tests.test_generation_backup import _lite_root


@pytest.mark.live
def test_generation_backup_restores_fresh_qdrant_and_lite_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_url = os.getenv("CITADEL_QDRANT_LIVE_URL")
    restore_url = os.getenv("CITADEL_QDRANT_RESTORE_URL")
    if not source_url or not restore_url:
        pytest.skip(
            "set CITADEL_QDRANT_LIVE_URL and CITADEL_QDRANT_RESTORE_URL "
            "to run the whole-generation restore contract"
        )
    source_key = os.getenv("CITADEL_QDRANT_LIVE_KEY", "disposable-live-test-key")
    restore_key = os.getenv(
        "CITADEL_QDRANT_RESTORE_KEY",
        "disposable-restore-test-key",
    )
    generation = f"generation-backup-live-{tmp_path.name}"
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    restore_root = tmp_path / "restore"

    first = _run_worker(source_root, generation=generation, mode="ingest")
    with QdrantSnapshotStore(url=source_url, api_key=source_key) as source_store:
        manifest = create_generation_backup(
            generation_id=generation,
            data_root=source_root,
            destination=backup_root,
            snapshot_store=source_store,
        )
    assert manifest["qdrant_collections"]
    assert any(
        item["path"].endswith(".lbug") for item in manifest["local_files"]
    )

    source_client = QdrantClient(url=source_url, api_key=source_key)
    try:
        for item in manifest["qdrant_collections"]:
            assert source_client.delete_collection(
                item["collection"],
                timeout=60,
            )
        remaining = {
            item.name for item in source_client.get_collections().collections
        }
        assert not {
            item["collection"] for item in manifest["qdrant_collections"]
        } & remaining
    finally:
        source_client.close()

    with QdrantSnapshotStore(url=restore_url, api_key=restore_key) as restore_store:
        restored = restore_generation_backup(
            generation_id=generation,
            backup_root=backup_root,
            target_data_root=restore_root,
            snapshot_store=restore_store,
        )
    assert len(restored["qdrant_collections"]) == len(
        manifest["qdrant_collections"]
    )

    monkeypatch.setenv("CITADEL_QDRANT_LIVE_URL", restore_url)
    monkeypatch.setenv("CITADEL_QDRANT_LIVE_KEY", restore_key)
    second = _run_worker(restore_root, generation=generation, mode="read")

    assert second["central_texts"] == first["central_texts"]
    assert second["alice_texts"] == first["alice_texts"]
    assert second["central_cross_texts"] == first["central_cross_texts"]
    assert second["lifecycle_texts"] == first["lifecycle_texts"]
    assert second["central_lifecycle_texts"] == first["central_lifecycle_texts"]
    assert second["graph_presence"] == first["graph_presence"]


@pytest.mark.live
def test_generation_restore_count_mismatch_rolls_back_real_qdrant_and_lite_root(
    tmp_path: Path,
) -> None:
    source_url = os.getenv("CITADEL_QDRANT_LIVE_URL")
    restore_url = os.getenv("CITADEL_QDRANT_RESTORE_URL")
    if not source_url or not restore_url:
        pytest.skip(
            "set CITADEL_QDRANT_LIVE_URL and CITADEL_QDRANT_RESTORE_URL "
            "to run the whole-generation restore contract"
        )
    source_key = os.getenv("CITADEL_QDRANT_LIVE_KEY", "disposable-live-test-key")
    restore_key = os.getenv(
        "CITADEL_QDRANT_RESTORE_KEY",
        "disposable-restore-test-key",
    )
    generation = f"generation-backup-count-mismatch-{tmp_path.name}"
    generation_hash = hashlib.sha256(generation.encode("utf-8")).hexdigest()[:12]
    collection = f"citadel_g_{generation_hash}_DocumentChunk_text"
    source_root = tmp_path / "source"
    backup_root = tmp_path / "backup"
    restore_root = tmp_path / "restore"
    _lite_root(source_root)

    source_client = QdrantClient(url=source_url, api_key=source_key)
    try:
        source_client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
        )
        source_client.upsert(
            collection_name=collection,
            points=[models.PointStruct(id=1, vector=[1.0, 0.0, 0.0])],
            wait=True,
        )
        with QdrantSnapshotStore(url=source_url, api_key=source_key) as source_store:
            create_generation_backup(
                generation_id=generation,
                data_root=source_root,
                destination=backup_root,
                snapshot_store=source_store,
            )
    finally:
        source_client.delete_collection(collection_name=collection, timeout=60)
        source_client.close()

    class CountMismatchAfterRecoveryStore(QdrantSnapshotStore):
        def restore_collection(
            self,
            collection: str,
            snapshot_path: Path,
            *,
            expected_sha256: str,
        ) -> int:
            actual = super().restore_collection(
                collection,
                snapshot_path,
                expected_sha256=expected_sha256,
            )
            return actual + 1

    restore_client = QdrantClient(url=restore_url, api_key=restore_key)
    try:
        assert restore_client.get_collections().collections == []
    finally:
        restore_client.close()

    with pytest.raises(GenerationBackupError, match="count mismatch"):
        with CountMismatchAfterRecoveryStore(
            url=restore_url,
            api_key=restore_key,
        ) as restore_store:
            restore_generation_backup(
                generation_id=generation,
                backup_root=backup_root,
                target_data_root=restore_root,
                snapshot_store=restore_store,
            )

    restore_client = QdrantClient(url=restore_url, api_key=restore_key)
    try:
        assert restore_client.get_collections().collections == []
    finally:
        restore_client.close()
    assert not restore_root.exists()
