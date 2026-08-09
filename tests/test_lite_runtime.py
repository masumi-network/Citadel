from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kb import lite_runtime


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
        "COGNEE_LOGS_DIR",
        "CITADEL_STATE_DIRECTORY",
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
