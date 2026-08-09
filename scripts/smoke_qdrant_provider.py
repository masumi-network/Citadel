#!/usr/bin/env python3
"""Exercise Citadel's live Qdrant provider without an external model call."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from qdrant_client import QdrantClient, models

from kb.qdrant_adapter import (
    CitadelQdrantAdapter,
    IndexSchema,
    physical_collection_name,
    qdrant_scope,
)


COLLECTION = "DocumentChunk"
DATASETS = ("seat:alice", "seat:bob")
DEFAULT_RECEIPT = Path("/data/citadel-state/provider-smoke.json")
SQLITE_PATH = Path("/data/cognee-system/databases/cognee.db")


class _LocalEmbeddingEngine:
    """Small deterministic engine used only for provider wiring checks."""

    dimensions = 3
    vector_size = 3

    async def embed_text(
        self, text: str | Sequence[str]
    ) -> list[float] | list[list[float]]:
        if isinstance(text, str):
            return [1.0, 0.0, 0.0]
        return [[1.0, 0.0, 0.0] for _ in text]

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def get_embeddings(self, texts: Sequence[str]) -> list[list[float]]:
        return await self.embed_texts(texts)

    def get_vector_size(self) -> int:
        return self.vector_size


def _adapter(generation: str) -> CitadelQdrantAdapter:
    previous_generation = os.environ.pop("CITADEL_GENERATION_ID", None)
    try:
        return CitadelQdrantAdapter(
            url=os.environ["VECTOR_DB_URL"],
            api_key=os.environ.get("VECTOR_DB_KEY"),
            embedding_engine=_LocalEmbeddingEngine(),
            database_name=generation,
        )
    finally:
        if previous_generation is not None:
            os.environ["CITADEL_GENERATION_ID"] = previous_generation


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    payload = getattr(value, "payload", None)
    if isinstance(payload, Mapping):
        return payload
    return None


def _find_text(value: Any) -> str | None:
    direct = getattr(value, "text", None)
    if isinstance(direct, str):
        return direct
    mapped = _mapping(value)
    if mapped is None:
        return None
    text = mapped.get("text")
    if isinstance(text, str):
        return text
    for nested in mapped.values():
        nested_text = _find_text(nested)
        if nested_text is not None:
            return nested_text
    return None


async def _verify_dataset(
    adapter: CitadelQdrantAdapter,
    *,
    generation: str,
    dataset: str,
    raw_id: UUID,
    expected_text: str,
) -> dict[str, Any]:
    with qdrant_scope(mode="read", generation_id=generation, dataset=dataset):
        count = await adapter.count_data_points(COLLECTION)
        retrieved = await adapter.retrieve(COLLECTION, [str(raw_id)])
        searched = await adapter.search(
            COLLECTION,
            query_vector=[1.0, 0.0, 0.0],
            include_payload=True,
        )
        scrolled, next_offset = await adapter.scroll_data_points(COLLECTION)

    surfaces = {
        "retrieve": [_find_text(item) for item in retrieved],
        "search": [_find_text(item) for item in searched],
        "scroll": [_find_text(item) for item in scrolled],
    }
    if count != 1:
        raise RuntimeError(f"{dataset} count was {count}, expected 1")
    for surface, texts in surfaces.items():
        if texts != [expected_text]:
            raise RuntimeError(
                f"{dataset} {surface} returned {texts!r}, expected {[expected_text]!r}"
            )
    if next_offset is not None:
        raise RuntimeError(f"{dataset} scroll was not exhaustive")
    return {"count": count, **surfaces}


async def _run(mode: str, receipt_path: Path) -> dict[str, Any]:
    if mode == "write":
        generation = f"provider-smoke-{uuid4().hex}"
        raw_id = uuid4()
        expected = {
            "seat:alice": f"alice-private-{uuid4().hex}",
            "seat:bob": f"bob-private-{uuid4().hex}",
        }
    else:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        generation = str(receipt["generation"])
        raw_id = UUID(str(receipt["raw_id"]))
        expected = {str(key): str(value) for key, value in receipt["expected"].items()}

    adapter = _adapter(generation)
    try:
        if mode == "write":
            for dataset in DATASETS:
                point = IndexSchema(
                    id=raw_id,
                    text=expected[dataset],
                    document_id="shared-document-id",
                    document_name="provider-smoke",
                )
                with qdrant_scope(
                    mode="write", generation_id=generation, dataset=dataset
                ):
                    await adapter.create_data_points(COLLECTION, [point])

            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(
                    {
                        "generation": generation,
                        "raw_id": str(raw_id),
                        "expected": expected,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

        datasets = {
            dataset: await _verify_dataset(
                adapter,
                generation=generation,
                dataset=dataset,
                raw_id=raw_id,
                expected_text=expected[dataset],
            )
            for dataset in DATASETS
        }
    finally:
        await adapter.close()

    return {
        "ok": True,
        "mode": mode,
        "generation": generation,
        "raw_id": str(raw_id),
        "isolated": True,
        "datasets": datasets,
        "receipt": str(receipt_path),
    }


def _sqlite_fingerprint(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        row_counts = {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            )
            for table in tables
        }
        logical_dump = "\n".join(connection.iterdump()).encode()
    finally:
        connection.close()
    return {
        "integrity": integrity,
        "row_counts": row_counts,
        "logical_sha256": hashlib.sha256(logical_dump).hexdigest(),
    }


def _backup_sqlite(backup_root: Path) -> dict[str, Any]:
    backup_path = backup_root / "cognee.sqlite"
    restored_path = backup_root / "cognee-restored.sqlite"
    source = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()

    backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    restored = sqlite3.connect(restored_path)
    try:
        backup.backup(restored)
    finally:
        restored.close()
        backup.close()

    backup_fingerprint = _sqlite_fingerprint(backup_path)
    restored_fingerprint = _sqlite_fingerprint(restored_path)
    if backup_fingerprint != restored_fingerprint:
        raise RuntimeError("restored SQLite database differs from online backup")
    if backup_fingerprint["integrity"] != "ok":
        raise RuntimeError("restored SQLite integrity check failed")
    return {
        "source": str(SQLITE_PATH),
        "backup": str(backup_path),
        "restored": str(restored_path),
        **backup_fingerprint,
    }


def _qdrant_rows(client: QdrantClient, collection: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset: Any = None
    while True:
        records, offset = client.scroll(
            collection_name=collection,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        rows.extend(record.model_dump(mode="json") for record in records)
        if offset is None:
            break
    return sorted(rows, key=lambda row: str(row["id"]))


def _download_snapshot(
    *,
    url: str,
    api_key: str | None,
    collection: str,
    snapshot_name: str,
    destination: Path,
) -> str:
    headers = {"api-key": api_key} if api_key else {}
    request = Request(
        f"{url.rstrip('/')}/collections/{quote(collection, safe='')}"
        f"/snapshots/{quote(snapshot_name, safe='')}",
        headers=headers,
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310
        data = response.read()
    destination.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _backup_restore(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    generation = str(receipt["generation"])
    source_collection = physical_collection_name(
        generation, DATASETS[0], COLLECTION
    )
    backup_id = uuid4().hex
    backup_root = Path("/data/citadel-state/backups") / backup_id
    backup_root.mkdir(parents=True, exist_ok=False)

    sqlite_result = _backup_sqlite(backup_root)
    qdrant_url = os.environ["VECTOR_DB_URL"]
    qdrant_key = os.environ.get("VECTOR_DB_KEY")
    client = QdrantClient(url=qdrant_url, api_key=qdrant_key)
    try:
        source_rows = _qdrant_rows(client, source_collection)
        snapshot = client.create_snapshot(source_collection, wait=True)
        if snapshot is None:
            raise RuntimeError("Qdrant did not return a snapshot receipt")
        snapshot_path = backup_root / snapshot.name
        snapshot_sha256 = _download_snapshot(
            url=qdrant_url,
            api_key=qdrant_key,
            collection=source_collection,
            snapshot_name=snapshot.name,
            destination=snapshot_path,
        )
        restored_collection = f"{source_collection}-restore-{backup_id[:8]}"
        recovered = client.recover_snapshot(
            collection_name=restored_collection,
            location=(
                f"file:///qdrant/snapshots/{source_collection}/{snapshot.name}"
            ),
            priority=models.SnapshotPriority.SNAPSHOT,
            wait=True,
        )
        if not recovered:
            raise RuntimeError("Qdrant snapshot recovery returned false")
        restored_rows = _qdrant_rows(client, restored_collection)
    finally:
        client.close()

    if source_rows != restored_rows:
        raise RuntimeError("restored Qdrant rows differ from source snapshot")
    return {
        "ok": True,
        "mode": "backup-restore",
        "backup_id": backup_id,
        "backup_root": str(backup_root),
        "sqlite": sqlite_result,
        "qdrant": {
            "source_collection": source_collection,
            "restored_collection": restored_collection,
            "rows": len(restored_rows),
            "snapshot": str(snapshot_path),
            "snapshot_sha256": snapshot_sha256,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("write", "read", "backup-restore"))
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    args = parser.parse_args()
    result = (
        _backup_restore(args.receipt)
        if args.mode == "backup-restore"
        else asyncio.run(_run(args.mode, args.receipt))
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
