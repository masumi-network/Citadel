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
# Outer backstop for the whole worker subprocess. It must stay well above the
# inner projection waits below: the ingest pass runs three cognify calls plus
# two sequential lifecycle projections, so a 240s cap equal to a single inner
# wait leaves no headroom and a slow CI runner gets SIGKILLed mid-ingest (-9)
# instead of surfacing the diagnosable TimeoutError from
# wait_for_lifecycle_operation.
_WORKER_TIMEOUT_SECONDS = int(os.getenv("COGNEE_WORKER_TIMEOUT_SECONDS", "900"))
_WORKER_PROJECTION_TIMEOUT_SECONDS = float(
    os.getenv("COGNEE_WORKER_PROJECTION_TIMEOUT_SECONDS", "240")
)


def _worker_environment(root: Path, *, generation: str, mode: str) -> dict[str, str]:
    system = root / "cognee-system"
    data = root / "data-storage"
    state = root / "citadel-state"
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
        timeout=_WORKER_TIMEOUT_SECONDS,
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
    assert first["central_cross_texts"] == []
    assert first["lifecycle_texts"] == [
        "lifecycle-qdrant-live-marker source record"
    ]
    assert first["central_lifecycle_texts"] == [
        "central-lifecycle-qdrant-live-marker source record"
    ]
    assert second["central_texts"] == first["central_texts"]
    assert second["alice_texts"] == first["alice_texts"]
    assert second["central_cross_texts"] == first["central_cross_texts"]
    assert second["lifecycle_texts"] == first["lifecycle_texts"]
    assert second["central_lifecycle_texts"] == first["central_lifecycle_texts"]
    assert (tmp_path / "cognee-system" / "databases" / "cognee.db").is_file()


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


def _matching_texts(results: list[Any], marker: str) -> list[str]:
    return sorted(text for text in _result_texts(results) if marker in text)


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
    central_lifecycle_marker = "central-lifecycle-qdrant-live-marker"
    session_trace_marker = "session-traces-citadel-lite-marker"
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
    document_ids_path = Path(os.environ["CITADEL_STATE_DIRECTORY"]) / "live-document-ids.json"
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
        session_trace_result = await client.remember(
            f"{session_trace_marker} bootstrap source record",
            dataset_name="session-traces",
            defer_cognify=True,
        )
        await client.cognify(datasets=["central"])
        await client.cognify(datasets=["seat:alice"])
        await client.cognify(datasets=["session-traces"])
        central_ids = _cognify_data_ids(central_result.get("added"))
        alice_ids = _cognify_data_ids(alice_result.get("added"))
        session_trace_ids = _cognify_data_ids(session_trace_result.get("added"))
        assert len(central_ids) == 1
        assert len(alice_ids) == 1
        assert len(session_trace_ids) == 1
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
        await lifecycle.wait_for_lifecycle_idle()
        lifecycle.resume_lifecycle_queue(include_deferred=True)
        lifecycle_operation = await lifecycle.wait_for_lifecycle_operation(
            str(lifecycle_result.projection_job_id),
            timeout_seconds=_WORKER_PROJECTION_TIMEOUT_SECONDS,
        )
        assert lifecycle_operation["state"] == "searchable"
        central_lifecycle_result = await lifecycle.ingest(
            f"{central_lifecycle_marker} source record",
            dataset="central",
            source_key="live:central-lifecycle-marker",
        )
        await lifecycle.wait_for_lifecycle_idle()
        lifecycle.resume_lifecycle_queue(include_deferred=True)
        central_lifecycle_operation = await lifecycle.wait_for_lifecycle_operation(
            str(central_lifecycle_result.projection_job_id),
            timeout_seconds=_WORKER_PROJECTION_TIMEOUT_SECONDS,
        )
        assert central_lifecycle_operation["state"] == "searchable"
        document_ids = {
            "central": [
                *central_ids,
                str(central_lifecycle_result.source_revision_id),
            ],
            "seat:alice": alice_ids,
            "session-traces": session_trace_ids,
            "lifecycle-live": [str(lifecycle_result.source_revision_id)],
        }
        document_ids_path.write_text(
            json.dumps(document_ids, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        document_ids = json.loads(document_ids_path.read_text(encoding="utf-8"))

    central_hits = await client.recall(central_marker, dataset="central", top_k=10)
    central_cross_hits = await client.recall(alice_marker, dataset="central", top_k=10)
    alice_hits = await client.recall(alice_marker, dataset="seat:alice", top_k=10)
    lifecycle_hits = await lifecycle.search(
        lifecycle_marker,
        dataset="lifecycle-live",
        top_k=10,
    )
    central_lifecycle_hits = await lifecycle.search(
        central_lifecycle_marker,
        dataset="central",
        top_k=10,
    )
    graph_presence: dict[str, list[str]] = {}
    for dataset, expected_ids in document_ids.items():
        present = await client.corpus_graph_presence(
            expected_ids,
            datasets=[dataset],
        )
        assert present == set(expected_ids)
        graph_presence[dataset] = sorted(present)
    await lifecycle.stop_lifecycle_queue()
    report = {
        "central_texts": _matching_texts(central_hits, central_marker),
        "central_cross_texts": _matching_texts(central_cross_hits, alice_marker),
        "alice_texts": _matching_texts(alice_hits, alice_marker),
        "lifecycle_texts": _matching_texts(lifecycle_hits, lifecycle_marker),
        "central_lifecycle_texts": _matching_texts(
            central_lifecycle_hits,
            central_lifecycle_marker,
        ),
        "graph_presence": graph_presence,
    }
    print(_REPORT_PREFIX + json.dumps(report, sort_keys=True))


if __name__ == "__main__" and os.getenv("CITADEL_LITE_WORKER") == "1":
    asyncio.run(_worker())
