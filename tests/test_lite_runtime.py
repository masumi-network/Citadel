from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kb import lite_runtime


LITE_WRITTEN_ENV_KEYS = (
    "SYSTEM_ROOT_DIRECTORY",
    "DATA_ROOT_DIRECTORY",
    "CACHE_ROOT_DIRECTORY",
    "COGNEE_LOGS_DIR",
    "CITADEL_STATE_DIRECTORY",
    "LADYBUG_HOME_DIRECTORY",
    "DB_PROVIDER",
    "DB_PATH",
    "DB_NAME",
    "GRAPH_DATABASE_PROVIDER",
    "VECTOR_DB_PROVIDER",
    "VECTOR_DATASET_DATABASE_HANDLER",
    "ENABLE_BACKEND_ACCESS_CONTROL",
    "REQUIRE_AUTHENTICATION",
    "TELEMETRY_DISABLED",
    "AUTO_FEEDBACK",
    "CITADEL_COGNIFY_QUEUE_PATH",
)


@pytest.fixture(autouse=True)
def restore_lite_written_environment() -> Iterator[None]:
    missing = object()
    original = {key: os.environ.get(key, missing) for key in LITE_WRITTEN_ENV_KEYS}
    yield
    for key, value in original.items():
        if value is missing:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _configured_environment(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", str(root))
    monkeypatch.setenv("CITADEL_GENERATION_ID", "test-generation")
    monkeypatch.setenv("VECTOR_DB_URL", "http://qdrant:6333")
    monkeypatch.setenv("VECTOR_DB_KEY", "qdrant-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("CITADEL_ADMIN_KEY", "a" * 32)
    monkeypatch.setenv("CITADEL_BUILD_ID", "candidate-wheel-sha256")
    monkeypatch.setenv(
        "CITADEL_QDRANT_SERVER_IMAGE",
        "qdrant/qdrant:v1.19.0@sha256:test",
    )
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_MODEL", "test-llm")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "384")
    for name in (
        "SYSTEM_ROOT_DIRECTORY",
        "DATA_ROOT_DIRECTORY",
        "CACHE_ROOT_DIRECTORY",
        "COGNEE_LOGS_DIR",
        "CITADEL_STATE_DIRECTORY",
        "LADYBUG_HOME_DIRECTORY",
        "CITADEL_COGNIFY_QUEUE_PATH",
        "DB_PROVIDER",
        "DB_PATH",
        "DB_NAME",
        "GRAPH_DATABASE_PROVIDER",
        "VECTOR_DB_PROVIDER",
        "VECTOR_DATASET_DATABASE_HANDLER",
        "ENABLE_BACKEND_ACCESS_CONTROL",
        "REQUIRE_AUTHENTICATION",
    ):
        monkeypatch.delenv(name, raising=False)


def test_configure_lite_environment_creates_sqlite_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configured_environment(monkeypatch, tmp_path)

    assert lite_runtime.configure_lite_environment() == tmp_path.resolve()

    assert os.environ["DB_PROVIDER"] == "sqlite"
    assert os.environ["VECTOR_DB_PROVIDER"] == "qdrant"
    assert os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] == "true"
    assert Path(os.environ["DB_PATH"]).is_dir()
    cache_root = Path(os.environ["CACHE_ROOT_DIRECTORY"])
    assert cache_root == tmp_path.resolve() / "cache"
    assert cache_root.is_dir()
    ladybug_home = Path(os.environ["LADYBUG_HOME_DIRECTORY"])
    assert ladybug_home == tmp_path.resolve() / "ladybug-home"
    assert ladybug_home.is_dir()


def test_configure_lite_environment_preserves_contained_ladybug_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configured_environment(monkeypatch, tmp_path)
    override = tmp_path / "provider-cache" / "ladybug"
    monkeypatch.setenv("LADYBUG_HOME_DIRECTORY", str(override))
    home = os.environ.get("HOME")

    lite_runtime.configure_lite_environment()

    assert Path(os.environ["LADYBUG_HOME_DIRECTORY"]) == override.resolve()
    assert override.is_dir()
    assert os.environ.get("HOME") == home


def test_configure_lite_environment_rejects_ladybug_home_outside_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    _configured_environment(monkeypatch, root)
    outside = tmp_path / "outside"
    monkeypatch.setenv("LADYBUG_HOME_DIRECTORY", str(outside))

    with pytest.raises(lite_runtime.LiteConfigurationError, match="must resolve inside"):
        lite_runtime.configure_lite_environment()

    assert not outside.exists()


def test_cognee_cache_resolves_below_writable_lite_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configured_environment(monkeypatch, tmp_path)
    root = lite_runtime.configure_lite_environment()

    from cognee.base_config import get_base_config
    from cognee.shared.cache import StorageAwareCache

    get_base_config.cache_clear()
    try:
        cache = StorageAwareCache()
        resolved = Path(cache.storage_manager.storage.storage_path).resolve()
        package_root = Path(__import__("cognee").__file__).resolve().parent

        assert resolved == root / "cache"
        assert resolved.is_relative_to(root)
        assert resolved.is_dir()
        assert package_root not in resolved.parents
    finally:
        get_base_config.cache_clear()


def test_configure_lite_environment_preserves_cache_root_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configured_environment(monkeypatch, tmp_path)
    cache_override = tmp_path / "mounted-cache"
    monkeypatch.setenv("CACHE_ROOT_DIRECTORY", str(cache_override))

    lite_runtime.configure_lite_environment()

    assert Path(os.environ["CACHE_ROOT_DIRECTORY"]) == cache_override
    assert cache_override.is_dir()


def test_configure_lite_environment_rejects_postgres(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configured_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DB_PROVIDER", "postgres")

    with pytest.raises(lite_runtime.LiteConfigurationError, match="DB_PROVIDER=sqlite"):
        lite_runtime.configure_lite_environment()


def test_single_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    (tmp_path / "citadel-state").mkdir()
    first = lite_runtime.acquire_single_instance_lock(tmp_path)
    try:
        with pytest.raises(lite_runtime.LiteConfigurationError, match="exactly one"):
            lite_runtime.acquire_single_instance_lock(tmp_path)
    finally:
        first.close()
        lite_runtime._LOCK_HANDLE = None


def test_drop_root_privileges_never_follows_volume_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        lite_runtime.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=10001, pw_gid=10001),
    )
    monkeypatch.setattr(
        lite_runtime.os,
        "walk",
        lambda _: [(str(tmp_path), ["linked-directory"], ["linked-file"])],
    )

    def record_chown(
        path: str | Path,
        _uid: int,
        _gid: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        calls.append((Path(path), follow_symlinks))

    monkeypatch.setattr(lite_runtime.os, "chown", record_chown)
    monkeypatch.setattr(lite_runtime.os, "setgroups", lambda _: None)
    monkeypatch.setattr(lite_runtime.os, "setgid", lambda _: None)
    monkeypatch.setattr(lite_runtime.os, "setuid", lambda _: None)

    lite_runtime.drop_root_privileges(tmp_path)

    assert len(calls) == 3
    assert all(follow_symlinks is False for _, follow_symlinks in calls)


def test_bootstrap_receipt_excludes_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configured_environment(monkeypatch, tmp_path)
    root = lite_runtime.configure_lite_environment()

    receipt_path = lite_runtime.write_bootstrap_receipt(root)
    content = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(content)

    assert receipt["generation_id"] == "test-generation"
    assert receipt["build_id"] == "candidate-wheel-sha256"
    assert receipt["relational_provider"] == "sqlite"
    assert receipt["vector_provider"] == "qdrant"
    assert receipt["qdrant_server_image"].startswith("qdrant/qdrant:v1.19.0@sha256:")
    assert receipt["qdrant_adapter_baseline"] == lite_runtime._QDRANT_ADAPTER_BASELINE
    assert receipt["qdrant_chunk_collection"].startswith("citadel_g_")
    assert receipt["llm_model"] == "test-llm"
    assert receipt["embedding_model"] == "test-embedding"
    assert receipt["embedding_dimensions"] == 384
    assert receipt["readiness_authentication_required"] is True
    assert "qdrant-secret" not in content
    assert "llm-secret" not in content
    assert "a" * 32 not in content


def test_container_healthcheck_uses_authenticated_readiness() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
        encoding="utf-8"
    )

    healthcheck = dockerfile.split("HEALTHCHECK", maxsplit=1)[1]
    assert "/readyz" in healthcheck
    assert "CITADEL_ADMIN_KEY" in healthcheck
    assert "--timeout=15s" in healthcheck
    assert "timeout=12" in healthcheck
    assert "/healthz" not in healthcheck


def test_container_runtime_home_belongs_to_the_dropped_user() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "HOME=/home/citadel" in dockerfile
