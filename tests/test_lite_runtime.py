from __future__ import annotations

from collections.abc import Iterator
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kb import lite_runtime


LITE_WRITTEN_ENV_KEYS = (
    "SYSTEM_ROOT_DIRECTORY",
    "DATA_ROOT_DIRECTORY",
    "CACHE_ROOT_DIRECTORY",
    "FASTEMBED_CACHE_PATH",
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
    "CITADEL_LITE_DATA_ROOT",
    "CITADEL_LIFECYCLE_STORE_PATH",
    "CITADEL_LITE_PROJECTION_CUTOVER_ROOT",
    "CITADEL_LITE_PROJECTION_CUTOVER_BACKUP_ROOT",
    "CITADEL_LITE_PROJECTION_CUTOVER_MANIFEST_SHA256",
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
    # The image pins HF_HUB_OFFLINE=1; these tests use a fake EMBEDDING_MODEL
    # and would trip the offline drift guard when run in-image.
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", str(root))
    monkeypatch.setenv("CITADEL_GENERATION_ID", "test-generation")
    monkeypatch.setenv("VECTOR_DB_URL", "http://qdrant:6333")
    monkeypatch.setenv("VECTOR_DB_KEY", "qdrant-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("CITADEL_ADMIN_KEY", "a" * 32)
    monkeypatch.setenv("CITADEL_BUILD_ID", "c" * 40)
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


def test_build_id_uses_shared_source_revision_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "build-id"
    marker.write_text("d" * 40, encoding="ascii")
    monkeypatch.setenv("CITADEL_BUILD_ID_PATH", str(marker))
    monkeypatch.setenv("CITADEL_BUILD_ID", "b" * 40)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)

    assert lite_runtime._build_id() == "a" * 40

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "not-a-sha")
    monkeypatch.setenv("CITADEL_BUILD_ID", "also-not-a-sha")
    assert lite_runtime._build_id() == "d" * 40


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


def test_rebind_lite_paths_preserves_nested_persisted_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_root = Path("/data")
    new_root = tmp_path / "new-root"
    external = tmp_path / "external" / "access.json"
    configured = {
        "CITADEL_ACCESS_STORE_PATH": old_root / "citadel-state/auth/access.json",
        "CITADEL_REPAIR_JOURNAL_PATH": old_root / "citadel-state/repair/repair.jsonl",
        "CITADEL_EVALUATION_GATE_PATH": old_root / "citadel-state/gates/evaluation.json",
        "CITADEL_CONTACT_STORE_PATH": external,
    }
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", str(old_root))
    root_paths = "README.md,docs/"
    monkeypatch.setenv("CITADEL_REPO_CONTENT_SYNC_ROOT_PATHS", root_paths)

    for name, value in configured.items():
        monkeypatch.setenv(name, str(value))

    lite_runtime._rebind_lite_paths(new_root)

    state_root = new_root / "citadel-state"
    assert Path(os.environ["CITADEL_ACCESS_STORE_PATH"]) == state_root / "auth/access.json"
    assert Path(os.environ["CITADEL_REPAIR_JOURNAL_PATH"]) == state_root / "repair/repair.jsonl"
    assert Path(os.environ["CITADEL_EVALUATION_GATE_PATH"]) == (
        state_root / "gates/evaluation.json"
    )
    assert os.environ["CITADEL_CONTACT_STORE_PATH"] == str(external)
    assert os.environ["CITADEL_REPO_CONTENT_SYNC_ROOT_PATHS"] == root_paths


def test_rebind_lite_paths_preserves_external_fastembed_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fastembed_cache = "/opt/fastembed-cache"
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", "/data")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", fastembed_cache)

    lite_runtime._rebind_lite_paths(tmp_path / "new-root")

    assert os.environ["FASTEMBED_CACHE_PATH"] == fastembed_cache


def test_rebind_lite_paths_preserves_relative_persisted_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    relative = "citadel-state/contacts.json"
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", "/data")
    monkeypatch.setenv("CITADEL_CONTACT_STORE_PATH", relative)

    lite_runtime._rebind_lite_paths(tmp_path / "new-root")

    assert os.environ["CITADEL_CONTACT_STORE_PATH"] == relative


def test_rebind_lite_paths_canonicalizes_lifecycle_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_root = Path("/data")
    new_root = tmp_path / "new-root"
    old_state = old_root / ".citadel"
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", str(old_root))
    monkeypatch.setenv("CITADEL_STATE_DIRECTORY", str(old_state))
    monkeypatch.setenv(
        "CITADEL_LIFECYCLE_STORE_PATH",
        str(old_state / "legacy/lifecycle.sqlite3"),
    )
    monkeypatch.setenv(
        "CITADEL_ACCESS_STORE_PATH",
        str(old_state / "auth/access.json"),
    )

    lite_runtime._rebind_lite_paths(new_root)

    assert Path(os.environ["CITADEL_LIFECYCLE_STORE_PATH"]) == (
        new_root / "citadel-state/lifecycle.sqlite3"
    )
    assert Path(os.environ["CITADEL_ACCESS_STORE_PATH"]) == (
        new_root / "citadel-state/auth/access.json"
    )


def test_rebind_lite_paths_maps_all_storage_root_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_root = Path("/data")
    old_system_root = old_root / "cognee-system"
    old_data_root = old_root / "data-storage"
    old_state_root = old_root / "citadel-state"
    new_root = tmp_path / "new-root"
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", str(old_root))
    monkeypatch.setenv("SYSTEM_ROOT_DIRECTORY", str(old_system_root))
    monkeypatch.setenv("DATA_ROOT_DIRECTORY", str(old_data_root))
    monkeypatch.setenv("CITADEL_STATE_DIRECTORY", str(old_state_root))
    monkeypatch.setenv(
        "CITADEL_CONTACT_STORE_PATH",
        str(old_data_root / "nested/contact.json"),
    )
    monkeypatch.setenv(
        "CITADEL_REPAIR_JOURNAL_PATH",
        str(old_system_root / "nested/repair.jsonl"),
    )
    monkeypatch.setenv(
        "CITADEL_LIFECYCLE_STORE_PATH",
        str(old_system_root / "nested/lifecycle.sqlite3"),
    )
    monkeypatch.setenv(
        "CITADEL_EVALUATION_GATE_PATH",
        str(old_state_root / "nested/evaluation.json"),
    )
    outside_storage = str(old_root / "outside/persisted.json")
    monkeypatch.setenv("CITADEL_OBSIDIAN_SYNC_STATE_PATH", outside_storage)

    lite_runtime._rebind_lite_paths(new_root)

    assert Path(os.environ["CITADEL_CONTACT_STORE_PATH"]) == (
        new_root / "data-storage/nested/contact.json"
    )
    assert Path(os.environ["CITADEL_REPAIR_JOURNAL_PATH"]) == (
        new_root / "cognee-system/nested/repair.jsonl"
    )
    assert Path(os.environ["CITADEL_EVALUATION_GATE_PATH"]) == (
        new_root / "citadel-state/nested/evaluation.json"
    )
    assert os.environ["CITADEL_OBSIDIAN_SYNC_STATE_PATH"] == outside_storage
    assert Path(os.environ["CITADEL_LIFECYCLE_STORE_PATH"]) == (
        new_root / "citadel-state/lifecycle.sqlite3"
    )


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
    assert receipt["build_id"] == "c" * 40
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


def test_offline_rejects_a_model_that_is_not_baked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    with pytest.raises(lite_runtime.LiteConfigurationError, match="baked into this image"):
        lite_runtime.configure_lite_environment(tmp_path)


def test_offline_accepts_the_baked_model(tmp_path, monkeypatch) -> None:
    _configured_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("EMBEDDING_MODEL", lite_runtime.BAKED_EMBEDDING_MODEL)
    assert lite_runtime.configure_lite_environment() == tmp_path.resolve()


def test_quarantine_skips_missing_sqlite(tmp_path: Path) -> None:
    missing = tmp_path / "cognee.db"
    assert lite_runtime.quarantine_unreadable_sqlite(missing) is None
    assert not missing.exists()


def test_quarantine_leaves_a_readable_sqlite(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "cognee.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (id INTEGER)")
    assert lite_runtime.quarantine_unreadable_sqlite(path) is None
    assert path.is_file()


def test_quarantine_renames_header_valid_but_unreadable_sqlite(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    path = tmp_path / "cognee.db"
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 32)
    stamp = datetime(2026, 8, 17, 7, 31, tzinfo=UTC)

    dest = lite_runtime.quarantine_unreadable_sqlite(path, now=stamp)

    assert dest == tmp_path / "cognee.db.corrupt-20260817T073100Z"
    assert not path.exists()
    assert dest is not None and dest.is_file()


def test_quarantine_renames_unreadable_sqlite_and_sidecars(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    path = tmp_path / "cognee.db"
    path.write_bytes(b"this is not a database")
    wal = tmp_path / "cognee.db-wal"
    shm = tmp_path / "cognee.db-shm"
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")
    stamp = datetime(2026, 8, 17, 7, 30, tzinfo=UTC)

    dest = lite_runtime.quarantine_unreadable_sqlite(path, now=stamp)

    assert dest == tmp_path / "cognee.db.corrupt-20260817T073000Z"
    assert dest is not None
    assert dest.read_bytes() == b"this is not a database"
    assert not path.exists()
    assert (tmp_path / "cognee.db.corrupt-20260817T073000Z-wal").read_bytes() == b"wal"
    assert (tmp_path / "cognee.db.corrupt-20260817T073000Z-shm").read_bytes() == b"shm"
    assert not wal.exists()
    assert not shm.exists()


def test_main_runs_prelock_backup_before_startup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[object] = []

    destination = "/data/backups/v058-canonical-20260829"

    monkeypatch.setenv("CITADEL_LITE_BACKUP_DESTINATION", destination)
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-058")
    monkeypatch.setenv("VECTOR_DB_URL", "http://qdrant:6333")
    monkeypatch.setenv("VECTOR_DB_KEY", "qdrant-key")

    def configure(data_root: Path | None = None) -> Path:
        events.append(("configure", data_root))
        return Path("/data")

    class FakeSnapshotStore:
        def __init__(self, *, url: str, api_key: str | None) -> None:
            events.append(("qdrant", url, api_key))

        def __enter__(self) -> FakeSnapshotStore:
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("qdrant-close")

    def create_backup(
        *,
        generation_id: str,
        data_root: Path,
        destination: Path,
        snapshot_store: object,
    ) -> dict[str, object]:
        events.append(
            ("backup", generation_id, data_root, destination, snapshot_store.__class__.__name__)
        )
        return {"generation_id": generation_id, "destination": str(destination)}

    monkeypatch.setattr(lite_runtime, "configure_lite_environment", configure)

    def drop_privileges(_root: Path) -> None:
        raise AssertionError("pre-lock backup must run before dropping privileges")

    monkeypatch.setattr(lite_runtime, "drop_root_privileges", drop_privileges)

    def lock(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pre-lock backup must not acquire the Lite writer lock")
    monkeypatch.setattr(lite_runtime, "acquire_single_instance_lock", lock)

    async def migrations() -> None:
        raise AssertionError("pre-lock backup must not run migrations")
    monkeypatch.setattr(lite_runtime, "run_migrations", migrations)

    fake_generation_backup = SimpleNamespace(
        QdrantSnapshotStore=FakeSnapshotStore,
        create_generation_backup=create_backup,
    )
    monkeypatch.setitem(sys.modules, "kb.generation_backup", fake_generation_backup)

    lite_runtime.main()

    assert events == [
        ("configure", None),
        ("qdrant", "http://qdrant:6333", "qdrant-key"),
        ("backup", "generation-058", Path("/data"), Path(destination), "FakeSnapshotStore"),
        "qdrant-close",
    ]
    assert json.dumps(
        {"generation_id": "generation-058", "destination": destination}, sort_keys=True
    ) in capsys.readouterr().out


def test_main_reports_prelock_backup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CITADEL_LITE_BACKUP_DESTINATION", "/data/backups/failure")
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-058")
    monkeypatch.setenv("VECTOR_DB_URL", "http://qdrant:6333")
    monkeypatch.setenv("VECTOR_DB_KEY", "qdrant-key")

    def configure(data_root: Path | None = None) -> Path:
        return Path("/data")

    class FailingSnapshotStore:
        def __init__(self, *, url: str, api_key: str | None) -> None:
            pass

        def __enter__(self) -> FailingSnapshotStore:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    def create_backup(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("backup failed")

    fake_generation_backup = SimpleNamespace(
        QdrantSnapshotStore=FailingSnapshotStore,
        create_generation_backup=create_backup,
    )
    monkeypatch.setitem(sys.modules, "kb.generation_backup", fake_generation_backup)

    monkeypatch.setattr(lite_runtime, "configure_lite_environment", configure)
    monkeypatch.setattr(
        lite_runtime,
        "acquire_single_instance_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pre-lock backup must run before the Lite writer lock")
        ),
    )

    with pytest.raises(SystemExit, match="backup failed") as error:
        lite_runtime.main()

    assert error.value.code != 0


def test_main_runs_projection_cutover_before_lock_and_starts_web(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    target = tmp_path / "clean-root"
    backup = tmp_path / "backup"
    manifest_hash = "a" * 64
    monkeypatch.setenv("CITADEL_LITE_PROJECTION_CUTOVER_ROOT", str(target))
    monkeypatch.setenv("CITADEL_LITE_PROJECTION_CUTOVER_BACKUP_ROOT", str(backup))
    monkeypatch.setenv("CITADEL_LITE_PROJECTION_CUTOVER_MANIFEST_SHA256", manifest_hash)
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-059")

    def cutover() -> dict[str, object]:
        events.append(
            ("cutover", {"target_root": Path(os.environ["CITADEL_LITE_PROJECTION_CUTOVER_ROOT"])})
        )
        target.mkdir()
        return {"ok": True, "generation_id": "generation-059"}

    def configure(data_root: Path | None = None) -> Path:
        events.append(("configure", data_root))
        return Path(data_root or target)
    monkeypatch.setattr(lite_runtime, "configure_lite_environment", configure)
    monkeypatch.setattr(lite_runtime, "run_prelock_projection_cutover", cutover)
    monkeypatch.setattr(lite_runtime, "drop_root_privileges", lambda root: events.append("drop"))
    monkeypatch.setattr(
        lite_runtime,
        "acquire_single_instance_lock",
        lambda *_args, **_kwargs: events.append("lock"),
    )
    monkeypatch.setattr(lite_runtime, "wait_for_qdrant", lambda: events.append("qdrant"))

    async def migrations() -> None:
        events.append("migrations")

    monkeypatch.setattr(lite_runtime, "run_migrations", migrations)
    monkeypatch.setattr(
        lite_runtime,
        "write_bootstrap_receipt",
        lambda root: events.append(("receipt", root)) or target / "bootstrap.json",
    )
    monkeypatch.setattr(lite_runtime.os, "execv", lambda executable, args: events.append(("exec", args)))
    lite_runtime.main()

    assert [item[0] if isinstance(item, tuple) else item for item in events] == [
        "cutover",
        "configure",
        "drop",
        "lock",
        "qdrant",
        "migrations",
        "receipt",
        "exec",
    ]
    assert events[0][1]["target_root"] == target
    assert events[1][1] == target


def test_projection_cutover_rebinds_persisted_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kb import projection_cutover

    target = tmp_path / "clean-root"
    backup = tmp_path / "backup"
    manifest_hash = "b" * 64
    monkeypatch.setenv("CITADEL_LITE_DATA_ROOT", "/data")
    monkeypatch.setenv("CITADEL_ACCESS_STORE_PATH", "/data/citadel-state/auth/access.json")
    monkeypatch.setenv("CITADEL_GENERATION_ID", "generation-060")
    monkeypatch.setenv("CITADEL_LITE_PROJECTION_CUTOVER_ROOT", str(target))
    monkeypatch.setenv("CITADEL_LITE_PROJECTION_CUTOVER_BACKUP_ROOT", str(backup))
    monkeypatch.setenv("CITADEL_LITE_PROJECTION_CUTOVER_MANIFEST_SHA256", manifest_hash)
    calls: list[dict[str, object]] = []

    def prepare(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"ok": True, "generation_id": "generation-060"}

    monkeypatch.setattr(projection_cutover, "prepare_projection_cutover", prepare)

    assert lite_runtime.run_prelock_projection_cutover() == {
        "ok": True,
        "generation_id": "generation-060",
    }

    assert calls == [
        {
            "backup_root": backup,
            "target_root": target,
            "generation_id": "generation-060",
            "manifest_sha256": manifest_hash,
        }
    ]
    assert Path(os.environ["CITADEL_ACCESS_STORE_PATH"]) == (
        target / "citadel-state/auth/access.json"
    )
