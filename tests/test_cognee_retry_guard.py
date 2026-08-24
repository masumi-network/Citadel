from __future__ import annotations

import os

from kb.cognee_retry_guard import _free_route_configured


def _clear_model_env(monkeypatch) -> None:
    for name in tuple(os.environ):
        if "MODEL" in name:
            monkeypatch.delenv(name, raising=False)


def test_free_route_detection_ignores_paid_openrouter_models(monkeypatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "openrouter/anthropic/claude-sonnet-4")

    assert _free_route_configured() is False


def test_free_route_detection_matches_free_openrouter_routes(monkeypatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "openrouter/nvidia/nemotron-nano-9b-v2:free")

    assert _free_route_configured() is True

    monkeypatch.setenv("LLM_MODEL", "openrouter/free")
    assert _free_route_configured() is True

    monkeypatch.setenv("LLM_MODEL", "openrouter/openrouter/free")
    assert _free_route_configured() is True
