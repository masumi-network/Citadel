"""Free-first model routing for direct LLM calls and Cognee stages."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Any

from kb.embedding_profile import LOCAL_PROFILE, active_embedding_profile

logger = logging.getLogger(__name__)

DEFAULT_ROUTINE_MODEL = "openrouter/free"
DEFAULT_REASONING_MODEL = "openrouter/free"
DEFAULT_RESEARCH_MODEL = "openrouter/free"

# Cognee passes these through LiteLLM. The first ``openrouter/`` selects the
# LiteLLM provider; the second is part of OpenRouter's native model id.
DEFAULT_COGNEE_FREE_ROUTER_MODEL = "openrouter/openrouter/free"
DEFAULT_COGNEE_EXTRACTION_MODEL = DEFAULT_COGNEE_FREE_ROUTER_MODEL
DEFAULT_COGNEE_SUMMARIZATION_MODEL = DEFAULT_COGNEE_FREE_ROUTER_MODEL
DEFAULT_COGNEE_QUERY_MODEL = DEFAULT_COGNEE_FREE_ROUTER_MODEL
DEFAULT_EMBEDDING_PROVIDER = "openai_compatible"
DEFAULT_EMBEDDING_MODEL = "nvidia/nemotron-3-embed-1b:free"
DEFAULT_EMBEDDING_DIMENSIONS = "2048"
DEFAULT_EMBEDDING_ENDPOINT = "https://openrouter.ai/api/v1"

_AUTO_MODELS = frozenset({"openrouter/auto", "openrouter/auto-beta"})
_COST_TIERS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True)
class ModelRoute:
    """The requested model and any OpenRouter router options for one task."""

    task: str
    model: str
    plugins: tuple[dict[str, Any], ...] = ()


def _bool_env(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def cognee_free_router_fallback_enabled() -> bool:
    """Return whether Cognee may make one bounded Auto Router fallback."""

    return _bool_env("CITADEL_COGNEE_FREE_ROUTER_FALLBACK", default=True)


def is_free_model_daily_quota_error(error: BaseException) -> bool:
    """Return whether an error means the account-wide free quota is exhausted."""

    text = f"{type(error).__name__}: {error}".lower()
    return any(
        marker in text
        for marker in (
            "free-model daily quota",
            "free model daily quota",
            "free-models-per-day",
            "openrouter_free_tier_daily",
        )
    )


def is_cognee_llm_provider_failure(error: BaseException) -> bool:
    """Identify LLM provider failures that can try the free router once."""

    text = f"{type(error).__name__}: {error}".lower()
    if "embed" in text or "vector" in text:
        return False
    return any(
        marker in text
        for marker in (
            "llm",
            "language model",
            "litellm",
            "structured output",
            "openrouter",
            "provider quota",
            "rate limit",
            "429",
            "422",
            "503",
            "timeout",
            "timed out",
            "connection",
        )
    )


def activate_cognee_free_router_fallback(error: BaseException) -> bool:
    """Switch all Cognee LLM stages to ``openrouter/free`` once per process."""

    if not cognee_free_router_fallback_enabled():
        return False
    if not routing_enabled() or is_free_model_daily_quota_error(error):
        return False
    if os.getenv("CITADEL_COGNEE_FREE_ROUTER_ACTIVE", "").strip().lower() == "true":
        return False

    for name in (
        "LLM_MODEL",
        "LLM_EXTRACTION_MODEL",
        "LLM_SUMMARIZATION_MODEL",
        "LLM_QUERY_MODEL",
    ):
        os.environ[name] = DEFAULT_COGNEE_FREE_ROUTER_MODEL
    os.environ["LLM_PROVIDER"] = "custom"
    os.environ["CITADEL_COGNEE_FREE_ROUTER_ACTIVE"] = "true"
    return True


def routing_enabled() -> bool:
    """Return whether the free-first routing policy is active."""

    return _bool_env("CITADEL_MODEL_ROUTING_ENABLED", default=True)


def normalize_free_openrouter_model(
    value: str | None,
    *,
    litellm: bool = False,
) -> str | None:
    """Return one free OpenRouter model id in direct or LiteLLM form."""

    if not isinstance(value, str) or not value.strip():
        return None
    direct = value.strip()
    lowered = direct.lower()
    if lowered.startswith("openrouter/") and lowered != "openrouter/free":
        remainder = direct[len("openrouter/") :]
        if "/" in remainder:
            direct = remainder
            lowered = direct.lower()
    if lowered != "openrouter/free" and (
        not lowered.endswith(":free") or "/" not in direct
    ):
        return None
    return f"openrouter/{direct}" if litellm else direct


def enforce_free_openrouter_model(
    value: str | None,
    *,
    fallback: str,
    litellm: bool = False,
) -> str:
    """Use the configured model only when OpenRouter marks it as free."""

    normalized = normalize_free_openrouter_model(value, litellm=litellm)
    if normalized is not None:
        return normalized
    if isinstance(value, str) and value.strip():
        logger.warning("Ignoring non-free OpenRouter model override: %s", value.strip())
    return fallback


def model_for_task(task: str) -> str:
    """Resolve a bare OpenRouter model id for a direct application task."""

    normalized = task.strip().lower()
    fallback = DEFAULT_ROUTINE_MODEL
    configured: str | None
    if not routing_enabled():
        configured = _env("CITADEL_LLM_MODEL")
        fallback = DEFAULT_REASONING_MODEL
        return enforce_free_openrouter_model(configured, fallback=fallback)
    if normalized in {"research", "digest"}:
        configured = _env("CITADEL_LLM_MODEL_RESEARCH")
        fallback = DEFAULT_RESEARCH_MODEL
    elif normalized in {"reasoning", "self_improve"}:
        configured = _env("CITADEL_LLM_MODEL_REASONING")
        fallback = DEFAULT_REASONING_MODEL
    else:
        configured = _env("CITADEL_LLM_MODEL_ROUTINE")
    return enforce_free_openrouter_model(configured, fallback=fallback)


def _auto_cost_tier(task: str) -> str:
    configured = _env("CITADEL_LLM_AUTO_COST_TIER")
    if configured in _COST_TIERS:
        return configured
    if task.strip().lower() in {"research", "digest", "reasoning", "self_improve"}:
        return "medium"
    return "low"


def plugins_for_model(task: str, model: str) -> tuple[dict[str, Any], ...]:
    """Return OpenRouter plugin settings for an Auto Router model."""

    normalized = model.strip().lower()
    if normalized not in _AUTO_MODELS:
        return ()
    plugin_id = "auto-beta-router" if normalized == "openrouter/auto-beta" else "auto-router"
    return ({"id": plugin_id, "cost_tier": _auto_cost_tier(task)},)


def route_for(task: str) -> ModelRoute:
    """Resolve a direct-call route, including Auto Router settings."""

    model = model_for_task(task)
    return ModelRoute(task=task, model=model, plugins=plugins_for_model(task, model))


def _clear_cognee_embedding_caches() -> None:
    """Drop Cognee's cached embedding config after changing its environment."""

    try:
        from cognee.infrastructure.databases.vector.embeddings.config import (
            get_embedding_config,
        )
        from cognee.infrastructure.databases.vector.embeddings.get_embedding_engine import (
            get_embedding_engine,
        )
    except ImportError:
        return

    factories = [get_embedding_config, get_embedding_engine]
    try:
        from kb.chunk_window import _create_vector_engine
    except ImportError:
        _create_vector_engine = None
    if _create_vector_engine is not None:
        factories.append(_create_vector_engine)

    for factory in factories:
        clear_cache = getattr(factory, "cache_clear", None)
        if clear_cache is not None:
            clear_cache()


def configure_cognee_model_routes() -> dict[str, str]:
    """Set free-only Cognee routes in LiteLLM form."""

    routes: dict[str, str] = {}
    model_defaults = {
        "LLM_MODEL": DEFAULT_COGNEE_EXTRACTION_MODEL,
        "LLM_EXTRACTION_MODEL": DEFAULT_COGNEE_EXTRACTION_MODEL,
        "LLM_SUMMARIZATION_MODEL": DEFAULT_COGNEE_SUMMARIZATION_MODEL,
        "LLM_QUERY_MODEL": DEFAULT_COGNEE_QUERY_MODEL,
    }
    os.environ["LLM_PROVIDER"] = "custom"
    routes["LLM_PROVIDER"] = "custom"
    for name, fallback in model_defaults.items():
        os.environ[name] = enforce_free_openrouter_model(
            os.environ.get(name),
            fallback=fallback,
            litellm=True,
        )
        routes[name] = os.environ[name]

    profile = active_embedding_profile()
    if profile.name == LOCAL_PROFILE:
        embedding_routes = {
            "EMBEDDING_PROVIDER": profile.provider,
            "EMBEDDING_MODEL": profile.model,
            "EMBEDDING_DIMENSIONS": str(profile.dimensions),
        }
        for name, value in embedding_routes.items():
            os.environ[name] = value
        os.environ.pop("EMBEDDING_ENDPOINT", None)
        os.environ.pop("EMBEDDING_API_KEY", None)
        _clear_cognee_embedding_caches()
        routes.update(embedding_routes)
    elif _bool_env("CITADEL_NEMOTRON_EMBEDDINGS_ENABLED", default=False):
        embedding_routes = {
            "EMBEDDING_PROVIDER": DEFAULT_EMBEDDING_PROVIDER,
            "EMBEDDING_MODEL": DEFAULT_EMBEDDING_MODEL,
            "EMBEDDING_DIMENSIONS": DEFAULT_EMBEDDING_DIMENSIONS,
            "EMBEDDING_ENDPOINT": DEFAULT_EMBEDDING_ENDPOINT,
        }
        for name, value in embedding_routes.items():
            os.environ[name] = value
        embedding_api_key = _env("EMBEDDING_API_KEY") or _env("OPENROUTER_API_KEY")
        if embedding_api_key is None:
            llm_endpoint = (_env("LLM_ENDPOINT") or "").rstrip("/")
            if llm_endpoint == DEFAULT_EMBEDDING_ENDPOINT.rstrip("/"):
                embedding_api_key = _env("LLM_API_KEY")
        if embedding_api_key is not None:
            os.environ["EMBEDDING_API_KEY"] = embedding_api_key
        _clear_cognee_embedding_caches()
        routes.update(embedding_routes)

    return routes
