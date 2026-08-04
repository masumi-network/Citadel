"""Ingest surfaces report what they OBSERVED, not what they REQUESTED.

`remember()` returns before the graph write in every branch (suppressed /
deferred / scheduled), so for years `accepted` doubled as "indexed" on every
connector surface while 892 of 2867 documents sat at chunk_count 0 — accepted
and never cognified — and a working node and a silently broken one emitted
byte-identical output. These tests pin the ledger half of the fix: the detached background cognify
stamps its OBSERVED outcome into a module-level ledger
(`background_cognify_stats`) surfaced on /readyz, so a node whose scheduled
graph writes never complete shows runs_scheduled climbing while runs_completed
stays flat, instead of leaving no trace anywhere.

The per-write state that a caller branches on lives on `IngestResult`
(`index_state` / `indexed`) and is covered by tests/test_ingest_index_state.py;
this file covers only what the process itself watched happen.

The modules are imported as modules (not names) on purpose: reverting the
source must FAIL these tests, not error collection.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

import kb.cognee_client as cc


def _fresh_stats() -> dict[str, Any]:
    return {
        "runs_scheduled": 0,
        "runs_completed": 0,
        "runs_failed": 0,
        "runs_not_scheduled": 0,
        "last_scheduled_at": None,
        "last_completed_at": None,
        "last_failed_at": None,
        "last_error": None,
    }


def _install_fake_cognee(monkeypatch: Any, *, cognify: Any = None) -> None:
    async def run_startup_migrations() -> None:
        return None

    async def add(data: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def default_cognify(*, datasets: Any, incremental_loading: bool) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setitem(
        sys.modules,
        "cognee",
        SimpleNamespace(
            run_startup_migrations=run_startup_migrations,
            add=add,
            cognify=cognify or default_cognify,
        ),
    )


@pytest.mark.asyncio
async def test_remember_deferred_states_indexing_outcome_explicitly(
    monkeypatch: Any,
) -> None:
    # defer_cognify=True is add-only; the caller cognifies later. The return
    # must say the graph write has NOT happened rather than leaving the caller
    # to read the add as indexed.
    monkeypatch.delenv("CITADEL_SUPPRESS_INLINE_COGNIFY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "k")
    _install_fake_cognee(monkeypatch)
    client = cc.CogneePublicClient()

    result = await client.remember(
        "note", dataset_name="seat:sarthi", tags=(), defer_cognify=True
    )

    assert result == {
        "added": {"ok": True},
        "cognify": "deferred",
        "data_ids": (),
        "index_state": "pending_cognify",
    }


@pytest.mark.asyncio
async def test_background_cognify_completion_is_observed(monkeypatch: Any) -> None:
    # A scheduled cognify stamps `scheduled`; only the task body itself can
    # stamp `completed`. On a working node the two counters track each other.
    import asyncio

    monkeypatch.delenv("CITADEL_SUPPRESS_INLINE_COGNIFY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "k")
    _install_fake_cognee(monkeypatch)
    monkeypatch.setattr(cc, "_BACKGROUND_COGNIFY_STATS", _fresh_stats())
    client = cc.CogneePublicClient()

    result = await client.remember("note", dataset_name="seat:sarthi", tags=())
    # A graph write is owed, so the state is pending — never "indexed".
    assert result["index_state"] == "pending_cognify"
    assert result["background_cognify"] is True

    stats = cc.background_cognify_stats()
    assert stats["runs_scheduled"] == 1
    # Nothing has completed yet: the request and the outcome are not the same
    # number, which is the entire point of the ledger.
    assert stats["runs_completed"] == 0

    await asyncio.gather(*list(cc._BACKGROUND_COGNIFY_TASKS), return_exceptions=True)
    stats = cc.background_cognify_stats()
    assert stats["runs_completed"] == 1
    assert stats["runs_failed"] == 0
    assert stats["last_completed_at"] is not None


@pytest.mark.asyncio
async def test_background_cognify_failure_is_observed(monkeypatch: Any) -> None:
    # A cognify that dies must leave a failed stamp, not silence: previously a
    # dead task and a completed one looked identical to every reader.
    import asyncio

    monkeypatch.delenv("CITADEL_SUPPRESS_INLINE_COGNIFY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "k")

    async def broken_cognify(*, datasets: Any, incremental_loading: bool) -> dict[str, Any]:
        raise RuntimeError("kuzu lock held")

    _install_fake_cognee(monkeypatch, cognify=broken_cognify)
    monkeypatch.setattr(cc, "_BACKGROUND_COGNIFY_STATS", _fresh_stats())
    client = cc.CogneePublicClient()

    await client.remember("note", dataset_name="seat:sarthi", tags=())
    await asyncio.gather(*list(cc._BACKGROUND_COGNIFY_TASKS), return_exceptions=True)

    stats = cc.background_cognify_stats()
    assert stats["runs_scheduled"] == 1
    assert stats["runs_completed"] == 0
    assert stats["runs_failed"] == 1
    assert stats["last_error"] == "RuntimeError"
    assert stats["last_failed_at"] is not None


def test_schedule_cognify_without_loop_is_observed(monkeypatch: Any) -> None:
    # The no-running-loop branch stores data that will never be searchable in
    # this process. That used to be one error log; now it is a counted state.
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setattr(cc, "_BACKGROUND_COGNIFY_STATS", _fresh_stats())
    client = cc.CogneePublicClient()

    client.schedule_cognify(["seat:sarthi"])  # sync context: no running loop

    stats = cc.background_cognify_stats()
    assert stats["runs_scheduled"] == 0
    assert stats["runs_not_scheduled"] == 1
    assert stats["last_error"] == "no_running_event_loop"
