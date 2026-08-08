from __future__ import annotations

import json
from typing import Any

import pytest

from kb import self_improve as self_improve_module
from kb.config import CitadelConfig
from kb.vault_activity import MeshState
from kb.models import IngestResult
from kb.self_improve import SelfImprovement, propose_optimizations


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CITADEL_SELF_IMPROVE_ENABLED",
        "CITADEL_SELF_IMPROVE_MAX_ITEMS",
        "CITADEL_LLM_ENRICHMENT_ENABLED",
        "OPENROUTER_API_KEY",
        "LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeCitadel:
    config = CitadelConfig(default_dataset="notes")

    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []
        self.improve_calls: list[dict[str, Any]] = []

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        return IngestResult(True, "accepted", "notes", tuple(kwargs.get("tags") or ()))

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        self.improve_calls.append(kwargs)
        return {"ok": True}


class FailingImproveCitadel(FakeCitadel):
    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("cognee offline")


class FakeAccessStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


async def seeded_mesh(citadel: FakeCitadel, count: int) -> MeshState:
    mesh = MeshState()
    for index in range(count):
        result = IngestResult(True, "accepted", "notes", ("seed",))
        await mesh.record_ingest(
            citadel.config,
            result,
            data=f"Document {index} body",
            dataset="notes",
            tags=["seed"],
        )
    return mesh


async def test_pass_is_bounded_by_max_items(monkeypatch: pytest.MonkeyPatch) -> None:
    citadel = FakeCitadel()
    mesh = await seeded_mesh(citadel, 15)
    monkeypatch.setattr(
        self_improve_module,
        "propose_optimizations",
        lambda items: [
            {"label": item["label"], "summary": "better", "tags": ["better-tag"]}
            for item in items
        ],
    )
    optimizer = SelfImprovement(citadel, mesh=mesh)

    result = await optimizer.run(max_items=3)

    assert result["reviewed"] <= 3
    assert result["optimized"] <= 3
    assert len(citadel.ingest_calls) <= 3
    # Every applied optimization is an additive re-ingest, never a delete.
    assert all("Knowledge optimization note" in call["data"] for call in citadel.ingest_calls)
    assert all("self-improvement" in call["tags"] for call in citadel.ingest_calls)


async def test_env_default_bound_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITADEL_SELF_IMPROVE_MAX_ITEMS", "2")
    citadel = FakeCitadel()
    mesh = await seeded_mesh(citadel, 6)
    monkeypatch.setattr(
        self_improve_module,
        "propose_optimizations",
        lambda items: [{"label": item["label"], "summary": "s", "tags": ["t"]} for item in items],
    )

    result = await SelfImprovement(citadel, mesh=mesh).run()

    assert result["max_items"] == 2
    assert result["optimized"] <= 2


async def test_no_llm_is_a_deterministic_noop_that_still_improves() -> None:
    citadel = FakeCitadel()
    mesh = await seeded_mesh(citadel, 4)
    optimizer = SelfImprovement(citadel, mesh=mesh)

    result = await optimizer.run()

    assert result["ok"] is True
    assert result["llm_used"] is False
    assert result["proposals"] == []
    assert result["optimized"] == 0
    assert citadel.ingest_calls == []  # nothing re-ingested without proposals
    assert citadel.improve_calls  # the existing improve step still ran
    snapshot = await mesh.snapshot(citadel.config)
    optimization_events = [
        event for event in snapshot["events"] if event["type"] == "optimization"
    ]
    assert optimization_events[0]["details"]["used_llm"] is False


async def test_dry_run_proposes_but_never_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    citadel = FakeCitadel()
    mesh = await seeded_mesh(citadel, 2)
    monkeypatch.setattr(
        self_improve_module,
        "propose_optimizations",
        lambda items: [{"label": item["label"], "summary": "s", "tags": ["t"]} for item in items],
    )

    result = await SelfImprovement(citadel, mesh=mesh).run(dry_run=True)

    assert result["proposals"]
    assert result["optimized"] == 0
    assert citadel.ingest_calls == []


async def test_audit_event_summarizes_the_pass() -> None:
    citadel = FakeCitadel()
    mesh = await seeded_mesh(citadel, 1)
    store = FakeAccessStore()

    await SelfImprovement(citadel, mesh=mesh, access_store=store).run()

    assert len(store.events) == 1
    event = store.events[0]
    assert event["action"] == "learning_agent.optimize"
    assert event["success"] is True
    assert event["detail"]["reviewed"] == 1
    assert event["detail"]["optimized"] == 0


async def test_improve_failure_does_not_break_the_pass() -> None:
    citadel = FailingImproveCitadel()
    mesh = await seeded_mesh(citadel, 1)

    result = await SelfImprovement(citadel, mesh=mesh).run()

    assert result["ok"] is True
    assert result["improve"]["ok"] is False


async def test_previous_optimization_notes_are_not_reoptimized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    citadel = FakeCitadel()
    mesh = MeshState()
    await mesh.record_ingest(
        citadel.config,
        IngestResult(True, "accepted", "notes", ("self-improvement",)),
        data="Knowledge optimization note: old",
        dataset="notes",
        tags=["self-improvement"],
    )

    result = await SelfImprovement(citadel, mesh=mesh).run()

    assert result["reviewed"] == 0


def test_propose_optimizations_parses_defensively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    items = [{"label": "Doc A", "tags": ["old"]}, {"label": "Doc B", "tags": []}]
    payload = json.dumps(
        {
            "items": [
                {"label": "Doc A", "summary": "Better summary", "tags": ["fresh", "Tags "]},
                {"label": "Unknown doc", "tags": ["x"]},
                "garbage",
                {"label": "Doc B"},
            ]
        }
    )
    monkeypatch.setattr(self_improve_module, "openrouter_chat", lambda *a, **k: payload)

    proposals = propose_optimizations(items)

    assert len(proposals) == 1
    assert proposals[0]["label"] == "Doc A"
    assert proposals[0]["tags"] == ["fresh", "tags"]


def test_propose_optimizations_is_noop_on_malformed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(self_improve_module, "openrouter_chat", lambda *a, **k: "not json")

    assert propose_optimizations([{"label": "Doc A", "tags": []}]) == []


def test_propose_optimizations_requires_credentials() -> None:
    assert propose_optimizations([{"label": "Doc A", "tags": []}]) == []
