"""Embedding profiles used by Cognee and its vector provider."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

PRIMARY_PROFILE = "nemotron"
LOCAL_PROFILE = "fastembed"
LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
LOCAL_DIMENSIONS = 384


@dataclass(frozen=True)
class EmbeddingProfile:
    name: str
    provider: str
    model: str
    dimensions: int

    @property
    def collection_suffix(self) -> str:
        if self.name == LOCAL_PROFILE:
            return ""
        return f"{self.name}-{self.dimensions}"


PRIMARY_EMBEDDING_PROFILE = EmbeddingProfile(
    name=PRIMARY_PROFILE,
    provider="openai_compatible",
    model="nvidia/nemotron-3-embed-1b:free",
    dimensions=2048,
)
LOCAL_EMBEDDING_PROFILE = EmbeddingProfile(
    name=LOCAL_PROFILE,
    provider="fastembed",
    model=LOCAL_MODEL,
    dimensions=LOCAL_DIMENSIONS,
)


def _profile_for_name(value: str) -> EmbeddingProfile:
    normalized = value.strip().lower()
    if normalized in {"", "primary", PRIMARY_PROFILE}:
        return PRIMARY_EMBEDDING_PROFILE
    if normalized in {LOCAL_PROFILE, "local", "fastembed-bge-small-en-v1.5"}:
        return LOCAL_EMBEDDING_PROFILE
    raise RuntimeError(
        "CITADEL_EMBEDDING_PROFILE must be one of primary, nemotron, or fastembed"
    )


def _state_path() -> Path | None:
    configured = os.getenv("CITADEL_EMBEDDING_PROFILE_STATE_PATH", "").strip()
    if configured:
        return Path(configured)
    state_directory = os.getenv("CITADEL_STATE_DIRECTORY", "").strip()
    if state_directory:
        return Path(state_directory) / "embedding-profile.json"
    return None


def _persisted_profile_name() -> str | None:
    path = _state_path()
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    value = payload.get("profile") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def active_embedding_profile() -> EmbeddingProfile:
    """Return the profile shared by Cognee and the Qdrant adapter."""
    configured = os.getenv("CITADEL_EMBEDDING_PROFILE", "").strip()
    if configured:
        return _profile_for_name(configured)
    persisted = _persisted_profile_name()
    if persisted:
        return _profile_for_name(persisted)
    provider = os.getenv("EMBEDDING_PROVIDER", "").strip().lower()
    model = os.getenv("EMBEDDING_MODEL", "").strip()
    dimensions = os.getenv("EMBEDDING_DIMENSIONS", "").strip()
    if provider == LOCAL_EMBEDDING_PROFILE.provider and model == LOCAL_MODEL:
        if not dimensions or dimensions == str(LOCAL_DIMENSIONS):
            return LOCAL_EMBEDDING_PROFILE
    return PRIMARY_EMBEDDING_PROFILE


def local_embedding_fallback_enabled() -> bool:
    value = os.getenv("CITADEL_LOCAL_EMBEDDING_FALLBACK", "true")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_embedding_provider_failure(error: BaseException) -> bool:
    """Identify provider failures that can be retried with local embeddings."""
    text = f"{type(error).__name__}: {error}".lower()
    if "embed" not in text and "vector" not in text:
        return False
    if any(marker in text for marker in ("dimension mismatch", "vector size", "wrong dimension")):
        return True
    return any(
        marker in text
        for marker in (
            "quota",
            "rate limit",
            "429",
            "timeout",
            "timed out",
            "connection",
            "unauthorized",
            "authentication",
            "api key",
            "provider",
        )
    )


def activate_local_embedding_fallback(error: BaseException) -> bool:
    """Switch Cognee to local embeddings and persist the selected profile."""
    if not local_embedding_fallback_enabled():
        return False
    if active_embedding_profile().name == LOCAL_PROFILE:
        return False

    os.environ["CITADEL_EMBEDDING_PROFILE"] = LOCAL_PROFILE
    path = _state_path()
    if path is not None:
        payload: dict[str, Any] = {
            "profile": LOCAL_PROFILE,
            "activated_at": datetime.now(UTC).isoformat(),
            "reason": type(error).__name__,
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            logger.warning("could not persist local embedding profile", exc_info=True)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return True
