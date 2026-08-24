from __future__ import annotations

import os

from kb.model_routing import (
    DEFAULT_COGNEE_FREE_ROUTER_MODEL,
    activate_cognee_free_router_fallback,
    configure_cognee_model_routes,
    route_for,
)


def _clear_routing_env(monkeypatch) -> None:
    for name in (
        "CITADEL_MODEL_ROUTING_ENABLED",
        "CITADEL_LLM_MODEL",
        "CITADEL_LLM_MODEL_ROUTINE",
        "CITADEL_LLM_MODEL_REASONING",
        "CITADEL_LLM_MODEL_RESEARCH",
        "CITADEL_COGNEE_FREE_ROUTER_FALLBACK",
        "CITADEL_COGNEE_FREE_ROUTER_ACTIVE",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_EXTRACTION_MODEL",
        "LLM_SUMMARIZATION_MODEL",
        "LLM_QUERY_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_direct_route_uses_openrouter_free_router(monkeypatch) -> None:
    _clear_routing_env(monkeypatch)

    route = route_for("routine")

    assert route.model == "openrouter/free"
    assert route.plugins == ()


def test_cognee_route_uses_litellm_form_of_openrouter_free_router(monkeypatch) -> None:
    _clear_routing_env(monkeypatch)

    routes = configure_cognee_model_routes()

    assert DEFAULT_COGNEE_FREE_ROUTER_MODEL == "openrouter/openrouter/free"
    assert routes["LLM_MODEL"] == DEFAULT_COGNEE_FREE_ROUTER_MODEL
    assert routes["LLM_EXTRACTION_MODEL"] == DEFAULT_COGNEE_FREE_ROUTER_MODEL
    assert routes["LLM_SUMMARIZATION_MODEL"] == DEFAULT_COGNEE_FREE_ROUTER_MODEL
    assert routes["LLM_QUERY_MODEL"] == DEFAULT_COGNEE_FREE_ROUTER_MODEL
    assert os.environ["LLM_PROVIDER"] == "custom"


def test_cognee_fallback_uses_same_litellm_route(monkeypatch) -> None:
    _clear_routing_env(monkeypatch)
    error = RuntimeError("provider quota")

    assert activate_cognee_free_router_fallback(error) is True
    assert os.environ["LLM_MODEL"] == DEFAULT_COGNEE_FREE_ROUTER_MODEL
    assert os.environ["LLM_EXTRACTION_MODEL"] == DEFAULT_COGNEE_FREE_ROUTER_MODEL
