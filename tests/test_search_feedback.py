"""Tests for implicit search telemetry / feedback loop."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from kb.config import CitadelConfig
from kb.mesh import MeshState
from kb.search_feedback import (
    SCHEMA_VERSION,
    build_search_telemetry,
    summarize_hit,
)
from kb.server import capture_search_feedback


CONFIG = CitadelConfig(tenant_id="test", default_dataset="notes")


def test_build_search_telemetry_payload_shape_is_stable() -> None:
    results = [
        {
            "id": "doc-1",
            "url": "https://example.com/a",
            "score": 0.9,
            "_citadel": {
                "doc_type": "spec",
                "trust_tier": "canonical",
                "dataset": "masumi-network",
                "result_id": "doc-1",
                "rank": 1,
            },
        },
        {
            "id": "doc-2",
            "score": 0.05,
            "_citadel": {
                "doc_type": "activity",
                "trust_tier": "ambient",
                "dataset": "notes",
                "result_id": "doc-2",
            },
        },
    ]
    payload = build_search_telemetry(
        query="MIP-003 payment endpoint schema",
        results=results,
        datasets=["masumi-network", "notes"],
        primary_dataset="masumi-network",
        top_k=10,
        latency_ms=123.45,
        timed_out=False,
        tool_name="citadel_search",
        client_hint="Cursor/MCP",
        seat_slug="sarthi",
        actor_id="actor-1",
        filters={"canonical_only": True, "repo": "masumi-node", "top_k": 10},
    )

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["kind"] == "search_telemetry"
    assert payload["search_id"].startswith("search:")
    assert payload["query"] == "MIP-003 payment endpoint schema"
    assert payload["result_count"] == 2
    assert payload["empty"] is False
    assert payload["low_score"] is False
    assert payload["latency_ms"] == 123.5
    assert payload["tool_name"] == "citadel_search"
    assert payload["seat_slug"] == "sarthi"
    assert payload["filters"]["canonical_only"] is True
    assert payload["filters"]["repo"] == "masumi-node"
    assert len(payload["top_results"]) == 2
    assert payload["top_results"][0] == {
        "rank": 1,
        "id": "doc-1",
        "url": "https://example.com/a",
        "doc_type": "spec",
        "trust_tier": "canonical",
        "dataset": "masumi-network",
        "score": 0.9,
    }
    # No body text leaked into telemetry.
    assert "text" not in payload["top_results"][0]
    assert "content" not in payload


def test_build_search_telemetry_redacts_filter_strings() -> None:
    payload = build_search_telemetry(
        query="q",
        results=[],
        filters={
            "types": ["spec", "skill"],
            "repo": "masumi-network/agent",
            "path": "docs/**/MIP-003/**",
            "canonical_only": True,
            "type": "ignored-when-types-present",  # types key wins via separate entries
        },
    )
    assert payload["filters"]["types"] == ["spec", "skill"]
    assert payload["filters"]["repo"] == "masumi-network/agent"
    assert payload["filters"]["path"] == "docs/**/MIP-003/**"
    assert payload["filters"]["canonical_only"] is True

    secretive = build_search_telemetry(
        query="q",
        results=[],
        filters={"repo": "https://x/?token=ctdl_abcdefghijklmnopqrstuvwxyz012345"},
    )
    assert "ctdl_" not in secretive["filters"]["repo"]


def test_build_search_telemetry_marks_empty_and_low_score() -> None:
    empty = build_search_telemetry(query="nothing", results=[])
    assert empty["empty"] is True
    assert empty["low_score"] is False

    weak = build_search_telemetry(
        query="weak",
        results=[{"id": "x", "score": 0.01, "_citadel": {"result_id": "x"}}],
    )
    assert weak["empty"] is False
    assert weak["low_score"] is True


def test_summarize_hit_redacts_secrets_in_url() -> None:
    summary = summarize_hit(
        {
            "id": "1",
            "url": "https://example.com/?token=ctdl_abcdefghijklmnopqrstuvwxyz012345",
            "_citadel": {"result_id": "1", "doc_type": "other"},
        },
        rank=1,
    )
    assert summary is not None
    assert "ctdl_" not in (summary.get("url") or "")


@pytest.mark.asyncio
async def test_mesh_record_search_telemetry_increments_feedback_index() -> None:
    mesh = MeshState()
    telemetry = build_search_telemetry(
        query="alpha",
        results=[{"id": "a", "score": 0.8}],
        primary_dataset="notes",
        tool_name="citadel_search",
    )
    feedback_id = await mesh.record_search_telemetry(
        CONFIG, telemetry=telemetry, dataset="notes"
    )
    snapshot = await mesh.snapshot(CONFIG)

    assert feedback_id.startswith("feedback:")
    assert snapshot["stats"]["feedback"] == 1
    feedback_nodes = [n for n in snapshot["nodes"] if n["type"] == "feedback"]
    assert len(feedback_nodes) == 1
    assert feedback_nodes[0]["metadata"]["kind"] == "search_telemetry"
    assert snapshot["events"][0]["type"] == "feedback"
    assert snapshot["events"][0]["details"]["kind"] == "search_telemetry"
    assert snapshot["events"][0]["details"]["telemetry"]["search_id"] == telemetry["search_id"]


@pytest.mark.asyncio
async def test_capture_search_feedback_swallows_write_failures() -> None:
    class BoomMesh(MeshState):
        async def record_search_telemetry(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("disk full")

    class FakeRequest:
        headers: dict[str, str] = {
            "user-agent": "pytest",
            "x-citadel-mcp-tool": "citadel_search",
        }

    class FakeActor:
        seat_slug = "sarthi"
        actor_id = "actor-1"
        default_dataset = "seat:sarthi"

    result = await capture_search_feedback(
        mesh_state=BoomMesh(),
        config=CONFIG,
        request=FakeRequest(),  # type: ignore[arg-type]
        actor=FakeActor(),  # type: ignore[arg-type]
        query="q",
        results=[],
        search_datasets=["notes"],
        primary_dataset="notes",
        top_k=5,
        latency_ms=10.0,
        timed_out=False,
    )
    assert result is None


@pytest.mark.asyncio
async def test_capture_search_feedback_attempts_write_on_every_call() -> None:
    mesh = MeshState()
    mesh.record_search_telemetry = AsyncMock(wraps=mesh.record_search_telemetry)  # type: ignore[method-assign]

    class FakeRequest:
        headers: dict[str, str] = {"x-citadel-mcp-tool": "citadel_search"}

    class FakeActor:
        seat_slug = None
        actor_id = "svc"
        default_dataset = "notes"

    telemetry = await capture_search_feedback(
        mesh_state=mesh,
        config=CONFIG,
        request=FakeRequest(),  # type: ignore[arg-type]
        actor=FakeActor(),  # type: ignore[arg-type]
        query="payment schema",
        results=[{"id": "hit-1", "score": 0.7, "_citadel": {"result_id": "hit-1"}}],
        search_datasets=["notes"],
        primary_dataset="notes",
        top_k=3,
        latency_ms=42.0,
        timed_out=False,
        filters={"top_k": 3},
    )
    assert telemetry is not None
    assert telemetry["tool_name"] == "citadel_search"
    mesh.record_search_telemetry.assert_awaited_once()


def test_search_endpoint_records_telemetry_and_survives_feedback_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kb.mesh import MeshState as LiveMesh
    from test_server import authed_client

    client = authed_client()
    search = client.post("/search", json={"query": "useful", "top_k": 3})
    assert search.status_code == 200
    body = search.json()
    assert body.get("feedback", {}).get("automatic") is True
    assert str(body.get("search_id", "")).startswith("search:")

    mesh = client.get("/api/mesh").json()
    assert mesh["stats"]["feedback"] >= 1

    async def failing(self: Any, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("telemetry store down")

    monkeypatch.setattr(LiveMesh, "record_search_telemetry", failing)
    again = client.post("/search", json={"query": "useful", "top_k": 1})
    assert again.status_code == 200
    assert "results" in again.json()
    assert again.json().get("feedback") is None


def test_search_endpoint_records_client_filters_in_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_server import authed_client

    client = authed_client()
    captured: dict[str, Any] = {}

    async def capture(**kwargs: Any) -> dict[str, Any]:
        captured["filters"] = kwargs.get("filters")
        return {
            "search_id": "search:test",
            "kind": "search_telemetry",
            "filters": kwargs.get("filters") or {},
        }

    import kb.server as server_mod

    monkeypatch.setattr(server_mod, "capture_search_feedback", capture)
    response = client.post(
        "/search",
        json={
            "query": "useful",
            "top_k": 5,
            "types": ["spec", "skill"],
            "repo": "masumi-network",
            "path": "docs/MIP",
            "canonical_only": True,
        },
    )

    assert response.status_code == 200
    assert captured["filters"]["types"] == ["spec", "skill"]
    assert captured["filters"]["repo"] == "masumi-network"
    assert captured["filters"]["path"] == "docs/MIP"
    assert captured["filters"]["canonical_only"] is True
    assert captured["filters"]["top_k"] == 5


def test_search_telemetry_never_reaches_another_seat(tmp_path: Any) -> None:
    """ADR-0009: a seat's query text must never be readable by another seat.

    Telemetry rows used to be tagged with ``primary_dataset``, and timeline
    scoping keys off ``details.dataset`` alone, so any seat that narrowed with
    an explicit ``dataset`` published its query text, ``seat_slug`` and
    ``actor_id`` to every other reader. Passing ``dataset`` is documented normal
    usage, so this was reachable without doing anything unusual.
    """
    import json as _json

    from fastapi.testclient import TestClient

    from kb.access import AccessStore
    from kb.server import app
    from test_server import authed_client

    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    alice = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"})
    bob = admin.post("/api/access/seats", json={"name": "Bob", "slug": "bob"})
    assert alice.status_code == 200 and bob.status_code == 200
    alice_token = alice.json()["token"]
    bob_token = bob.json()["token"]

    client = TestClient(app, base_url="https://testserver")
    secret = "alice private merger codename bluebird"
    search = client.post(
        "/search",
        json={"query": secret, "dataset": "masumi-network"},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert search.status_code == 200

    leaked = client.get(
        "/api/knowledge/events?limit=50",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert leaked.status_code == 200
    blob = _json.dumps(leaked.json())
    assert secret not in blob
    assert "seat:alice" not in blob
    assert alice.json()["principal"]["id"] not in blob

    # The signal is still preserved for the seat that owns it.
    own = client.get(
        "/api/knowledge/events?limit=50",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert own.status_code == 200
    assert secret in _json.dumps(own.json())


def test_feedback_accepts_result_id_and_correct_flag() -> None:
    from test_server import authed_client

    client = authed_client()
    response = client.post(
        "/feedback",
        json={"result_id": "hit-abc", "correct": True, "text": "relevant"},
    )
    assert response.status_code == 200
    assert response.json()["recorded"] is True
