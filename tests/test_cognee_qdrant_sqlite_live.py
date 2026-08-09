from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPORT_PREFIX = "CITADEL_LITE_REPORT="


def _worker_environment(root: Path, *, generation: str, mode: str) -> dict[str, str]:
    system = root / "system"
    data = root / "data"
    state = root / "state"
    logs = root / "logs"
    for directory in (system / "databases", data, state, logs):
        directory.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "CITADEL_LITE_WORKER": "1",
            "CITADEL_LITE_WORKER_MODE": mode,
            "CITADEL_LITE_SMOKE_ROOT": str(root),
            "SYSTEM_ROOT_DIRECTORY": str(system),
            "DATA_ROOT_DIRECTORY": str(data),
            "COGNEE_LOGS_DIR": str(logs),
            "CITADEL_STATE_DIRECTORY": str(state),
            "CITADEL_COGNIFY_QUEUE_PATH": str(state / "cognify-queue.json"),
            "DB_PROVIDER": "sqlite",
            "DB_PATH": str(system / "databases"),
            "DB_NAME": "cognee.db",
            "GRAPH_DATABASE_PROVIDER": "ladybug",
            "VECTOR_DB_PROVIDER": "qdrant",
            "VECTOR_DB_URL": os.environ["CITADEL_QDRANT_LIVE_URL"],
            "VECTOR_DB_KEY": os.environ.get(
                "CITADEL_QDRANT_LIVE_KEY", "disposable-live-test-key"
            ),
            "VECTOR_DATASET_DATABASE_HANDLER": "qdrant",
            "ENABLE_BACKEND_ACCESS_CONTROL": "true",
            "REQUIRE_AUTHENTICATION": "true",
            "CITADEL_GENERATION_ID": generation,
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "openai/gpt-4o-mini",
            "LLM_API_KEY": "disposable-live-test-key",
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": "openai/text-embedding-3-small",
            "EMBEDDING_DIMENSIONS": "3",
            "EMBEDDING_API_KEY": "disposable-live-test-key",
            "MOCK_EMBEDDING": "true",
            "COGNEE_SKIP_CONNECTION_TEST": "true",
            "TELEMETRY_DISABLED": "true",
            "AUTO_FEEDBACK": "false",
        }
    )
    return environment


def _run_worker(root: Path, *, generation: str, mode: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=Path(__file__).resolve().parents[1],
        env=_worker_environment(root, generation=generation, mode=mode),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    reports = [
        line.removeprefix(_REPORT_PREFIX)
        for line in result.stdout.splitlines()
        if line.startswith(_REPORT_PREFIX)
    ]
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(reports) == 1, result.stdout + result.stderr
    return json.loads(reports[0])


@pytest.mark.live
def test_cognee_sqlite_qdrant_survives_restart_and_keeps_dataset_scope(
    tmp_path: Path,
) -> None:
    if not os.getenv("CITADEL_QDRANT_LIVE_URL"):
        pytest.skip("set CITADEL_QDRANT_LIVE_URL to run the real Lite contract")
    generation = f"sqlite-live-{tmp_path.name}"

    first = _run_worker(tmp_path, generation=generation, mode="ingest")
    second = _run_worker(tmp_path, generation=generation, mode="read")

    assert first["central_texts"] == ["central-only-citadel-lite-marker source record"]
    assert first["alice_texts"] == [
        "alice-only-citadel-lite-marker private source record"
    ]
    assert first["central_cross_texts"] == first["central_texts"]
    assert first["lifecycle_texts"] == [
        "lifecycle-qdrant-live-marker source record"
    ]
    assert second["central_texts"] == first["central_texts"]
    assert second["alice_texts"] == first["alice_texts"]
    assert second["central_cross_texts"] == first["central_cross_texts"]
    assert second["lifecycle_texts"] == first["lifecycle_texts"]
    assert (tmp_path / "system" / "databases" / "cognee.db").is_file()


def _result_texts(results: list[Any]) -> list[str]:
    texts: list[str] = []
    for result in results:
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        if isinstance(payload, dict) and isinstance(payload.get("search_result"), list):
            for item in payload["search_result"]:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    texts.append(item["text"])
        elif isinstance(payload, dict) and isinstance(payload.get("text"), str):
            texts.append(payload["text"])
    return texts


async def _worker() -> None:
    from cognee.infrastructure.llm import LLMGateway
    from cognee.shared.data_models import KnowledgeGraph, Node, SummarizedContent
    from kb.cognee_client import CogneePublicClient, _cognify_data_ids
    from kb.config import CitadelConfig
    from kb.service import Citadel

    async def mock_llm(text_input: str, system_prompt: str, response_model, **kwargs):
        del system_prompt, kwargs
        if response_model is SummarizedContent:
            return SummarizedContent(
                summary=text_input[:160],
                description=text_input[:160],
            )
        if response_model is KnowledgeGraph:
            lowered = text_input.lower()
            marker = (
                "alice"
                if "alice" in lowered
                else "lifecycle"
                if "lifecycle" in lowered
                else "central"
            )
            return KnowledgeGraph(
                nodes=[
                    Node(
                        id=f"{marker}-entity",
                        name=f"{marker}-entity",
                        type="Marker",
                        description=f"{marker} marker",
                    )
                ],
                edges=[],
            )
        if text_input == "test":
            return "test"
        raise AssertionError(f"unexpected response model: {response_model}")

    LLMGateway.acreate_structured_output = staticmethod(mock_llm)
    client = CogneePublicClient()
    central_marker = "central-only-citadel-lite-marker"
    alice_marker = "alice-only-citadel-lite-marker"
    lifecycle_marker = "lifecycle-qdrant-live-marker"
    lifecycle = Citadel(
        CitadelConfig(
            default_dataset="lifecycle-live",
            lifecycle_enabled=True,
            lifecycle_store_path=str(
                Path(os.environ["CITADEL_STATE_DIRECTORY"]) / "lifecycle.sqlite3"
            ),
        ),
        cognee=client,
    )
    if os.environ["CITADEL_LITE_WORKER_MODE"] == "ingest":
        central_result = await client.remember(
            f"{central_marker} source record",
            dataset_name="central",
            defer_cognify=True,
        )
        alice_result = await client.remember(
            f"{alice_marker} private source record",
            dataset_name="seat:alice",
            defer_cognify=True,
        )
        await client.cognify(datasets=["central"])
        await client.cognify(datasets=["seat:alice"])
        central_ids = _cognify_data_ids(central_result.get("added"))
        alice_ids = _cognify_data_ids(alice_result.get("added"))
        assert len(central_ids) == 1
        assert len(alice_ids) == 1
        central_census = await client.stored_chunk_budget_check(
            document_ids=central_ids,
            datasets=["central"],
        )
        alice_census = await client.stored_chunk_budget_check(
            document_ids=alice_ids,
            datasets=["seat:alice"],
        )
        assert central_census is not None
        assert alice_census is not None
        assert central_census["ok"] is True
        assert alice_census["ok"] is True
        assert central_census["provider"] == "qdrant"
        assert alice_census["provider"] == "qdrant"
        assert central_census["chunks_scanned"] > 0
        assert alice_census["chunks_scanned"] > 0
        assert central_census["missing_document_ids"] == []
        assert alice_census["missing_document_ids"] == []
        central_counts = await client.corpus_chunk_counts(central_ids)
        alice_counts = await client.corpus_chunk_counts(alice_ids)
        assert central_counts is not None
        assert alice_counts is not None
        assert central_counts[central_ids[0]] > 0
        assert alice_counts[alice_ids[0]] > 0
        lifecycle_result = await lifecycle.ingest(
            f"{lifecycle_marker} source record",
            source_key="live:lifecycle-marker",
        )
        lifecycle_operation = await lifecycle.wait_for_lifecycle_operation(
            str(lifecycle_result.projection_job_id)
        )
        assert lifecycle_operation["state"] == "searchable"

    central_hits = await client.recall(central_marker, dataset="central", top_k=10)
    central_cross_hits = await client.recall(alice_marker, dataset="central", top_k=10)
    alice_hits = await client.recall(alice_marker, dataset="seat:alice", top_k=10)
    lifecycle_hits = await lifecycle.search(
        lifecycle_marker,
        dataset="lifecycle-live",
        top_k=10,
    )
    await lifecycle.stop_lifecycle_queue()
    report = {
        "central_texts": _result_texts(central_hits),
        "central_cross_texts": _result_texts(central_cross_hits),
        "alice_texts": _result_texts(alice_hits),
        "lifecycle_texts": _result_texts(lifecycle_hits),
    }
    print(_REPORT_PREFIX + json.dumps(report, sort_keys=True))


if __name__ == "__main__" and os.getenv("CITADEL_LITE_WORKER") == "1":
    asyncio.run(_worker())
