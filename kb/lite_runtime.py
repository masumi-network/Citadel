from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import pwd
import socket
import sqlite3
import sys
import time
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class LiteConfigurationError(RuntimeError):
    pass


_LOCK_HANDLE: IO[str] | None = None
_QDRANT_ADAPTER_BASELINE = "7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise LiteConfigurationError(f"{name} must not be empty")
    return value


def _build_id() -> str | None:
    from kb.build_identity import build_identity_from_runtime

    return build_identity_from_runtime(os.environ).build_id


# Must match the model the Dockerfile bakes into /opt/fastembed-cache and
# /opt/hf-cache when the FastEmbed provider runs under HF_HUB_OFFLINE=1.
BAKED_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def configure_lite_environment(data_root: Path | None = None) -> Path:
    root = (data_root or Path(os.getenv("CITADEL_LITE_DATA_ROOT", "/data"))).resolve()
    # fastembed treats any of {1, true, yes, on} as offline. Remote providers
    # do not use the image's baked FastEmbed model.
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "fastembed").strip().lower()
    embedding_endpoint = os.getenv("EMBEDDING_ENDPOINT", "").strip()
    nemotron_enabled = (os.getenv("CITADEL_NEMOTRON_EMBEDDINGS_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if (
        embedding_provider == "fastembed"
        and not embedding_endpoint
        and not nemotron_enabled
        and (os.getenv("HF_HUB_OFFLINE") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    ):
        model = os.getenv("EMBEDDING_MODEL", BAKED_EMBEDDING_MODEL)
        if model != BAKED_EMBEDDING_MODEL:
            # Without this, the drift surfaces later as fastembed's buried
            # "Could not load model ... from any source" loop (2026-08-12).
            raise LiteConfigurationError(
                f"EMBEDDING_MODEL={model!r} cannot load under HF_HUB_OFFLINE=1: "
                f"only {BAKED_EMBEDDING_MODEL!r} is baked into this image. "
                "Rebake the image with the new model or unset HF_HUB_OFFLINE."
            )
    defaults = {
        "SYSTEM_ROOT_DIRECTORY": str(root / "cognee-system"),
        "DATA_ROOT_DIRECTORY": str(root / "data-storage"),
        "CACHE_ROOT_DIRECTORY": str(root / "cache"),
        # fastembed otherwise defaults to world-shared /tmp/fastembed_cache;
        # inside the image the Dockerfile ENV (baked, read-only) wins over this.
        "FASTEMBED_CACHE_PATH": str(root / "cache" / "fastembed"),
        "COGNEE_LOGS_DIR": str(root / "logs"),
        "CITADEL_STATE_DIRECTORY": str(root / "citadel-state"),
        "LADYBUG_HOME_DIRECTORY": str(root / "ladybug-home"),
        "DB_PROVIDER": "sqlite",
        "DB_PATH": str(root / "cognee-system" / "databases"),
        "DB_NAME": "cognee.db",
        "GRAPH_DATABASE_PROVIDER": "ladybug",
        "VECTOR_DB_PROVIDER": "qdrant",
        "VECTOR_DATASET_DATABASE_HANDLER": "qdrant",
        "ENABLE_BACKEND_ACCESS_CONTROL": "true",
        "REQUIRE_AUTHENTICATION": "true",
        "TELEMETRY_DISABLED": "true",
        "AUTO_FEEDBACK": "false",
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)
    state = Path(os.environ["CITADEL_STATE_DIRECTORY"])
    os.environ.setdefault("CITADEL_COGNIFY_QUEUE_PATH", str(state / "cognify_queue.json"))
    ladybug_home = Path(os.environ["LADYBUG_HOME_DIRECTORY"]).resolve()
    if not ladybug_home.is_relative_to(root):
        raise LiteConfigurationError(
            "LADYBUG_HOME_DIRECTORY must resolve inside CITADEL_LITE_DATA_ROOT"
        )
    os.environ["LADYBUG_HOME_DIRECTORY"] = str(ladybug_home)

    expected = {
        "DB_PROVIDER": "sqlite",
        "VECTOR_DB_PROVIDER": "qdrant",
        "VECTOR_DATASET_DATABASE_HANDLER": "qdrant",
        "ENABLE_BACKEND_ACCESS_CONTROL": "true",
        "REQUIRE_AUTHENTICATION": "true",
    }
    for name, wanted in expected.items():
        actual = os.environ[name].strip().lower()
        if actual != wanted:
            raise LiteConfigurationError(
                f"Lite profile requires {name}={wanted}; got {os.environ[name]!r}"
            )
    graph_provider = os.environ["GRAPH_DATABASE_PROVIDER"].strip().lower()
    if graph_provider not in ("ladybug", "postgres"):
        raise LiteConfigurationError(
            f"Lite profile requires GRAPH_DATABASE_PROVIDER in (ladybug, postgres); "
            f"got {os.environ['GRAPH_DATABASE_PROVIDER']!r}"
        )
    for name in (
        "CITADEL_GENERATION_ID",
        "VECTOR_DB_URL",
        "VECTOR_DB_KEY",
        "CITADEL_QDRANT_SERVER_IMAGE",
        "LLM_API_KEY",
        "CITADEL_ADMIN_KEY",
    ):
        _required(name)
    if len(os.environ["CITADEL_ADMIN_KEY"]) < 32:
        raise LiteConfigurationError("CITADEL_ADMIN_KEY must contain at least 32 characters")
    try:
        embedding_dimensions = int(_required("EMBEDDING_DIMENSIONS"))
    except ValueError as error:
        raise LiteConfigurationError("EMBEDDING_DIMENSIONS must be a positive integer") from error
    if embedding_dimensions <= 0:
        raise LiteConfigurationError("EMBEDDING_DIMENSIONS must be a positive integer")
    qdrant_url = urlparse(os.environ["VECTOR_DB_URL"])
    if qdrant_url.scheme not in {"http", "https"} or not qdrant_url.hostname:
        raise LiteConfigurationError("VECTOR_DB_URL must be an HTTP(S) Qdrant origin")
    if qdrant_url.username or qdrant_url.password or qdrant_url.query or qdrant_url.fragment:
        raise LiteConfigurationError(
            "VECTOR_DB_URL must not contain credentials, query parameters, or a fragment"
        )
    for directory in (
        Path(os.environ["SYSTEM_ROOT_DIRECTORY"]),
        Path(os.environ["DATA_ROOT_DIRECTORY"]),
        Path(os.environ["CACHE_ROOT_DIRECTORY"]),
        Path(os.environ["COGNEE_LOGS_DIR"]),
        state,
        Path(os.environ["DB_PATH"]),
        ladybug_home,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return root


def sqlite_database_path() -> Path:
    return Path(os.environ["DB_PATH"]) / os.environ["DB_NAME"]


_SQLITE_HEADER = b"SQLite format 3\x00"


def _sqlite_file_is_readable(path: Path) -> bool:
    try:
        header = path.read_bytes()[:16]
    except OSError:
        return False
    if header != _SQLITE_HEADER:
        return False
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return True


def quarantine_unreadable_sqlite(
    db_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> Path | None:
    """Rename an unreadable cognee.db so boot can recreate it.

    The live Railway start wrapper did ``p.rename(...corrupt-...)`` then
    ``os.execv(... kb.lite_runtime)``. Keep that repair in-repo so a git
    deploy of ``python -m kb.lite_runtime`` does not drop it. Sidecar
    ``-wal`` / ``-shm`` files move with the db so a new file is not
    poisoned. The volume is not wiped.
    """
    path = (db_path or sqlite_database_path()).resolve()
    if not path.is_file() or _sqlite_file_is_readable(path):
        return None
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.corrupt-{stamp}")
    if dest.exists():
        dest = path.with_name(f"{path.name}.corrupt-{stamp}-{os.getpid()}")
    path.rename(dest)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_file():
            sidecar.rename(Path(str(dest) + suffix))
    return dest


def drop_root_privileges(root: Path, *, user_name: str = "citadel") -> None:
    if os.geteuid() != 0:
        return
    try:
        account = pwd.getpwnam(user_name)
    except KeyError as error:
        raise LiteConfigurationError(f"container user {user_name!r} does not exist") from error
    for current_root, directories, files in os.walk(root):
        os.chown(current_root, account.pw_uid, account.pw_gid, follow_symlinks=False)
        for name in directories:
            os.chown(
                Path(current_root) / name,
                account.pw_uid,
                account.pw_gid,
                follow_symlinks=False,
            )
        for name in files:
            os.chown(
                Path(current_root) / name,
                account.pw_uid,
                account.pw_gid,
                follow_symlinks=False,
            )
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)


def acquire_single_instance_lock(
    root: Path,
    *,
    state_root: Path | None = None,
) -> IO[str]:
    global _LOCK_HANDLE
    lock_path = (state_root or root / "citadel-state") / "lite-runtime.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise LiteConfigurationError(
            "SQLite Lite allows exactly one Citadel process for this data volume"
        ) from error
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} host={socket.gethostname()}\n")
    handle.flush()
    os.fsync(handle.fileno())
    os.set_inheritable(handle.fileno(), True)
    _LOCK_HANDLE = handle
    return handle


def wait_for_qdrant(*, timeout_seconds: float = 90.0) -> None:
    origin = _required("VECTOR_DB_URL").rstrip("/")
    api_key = _required("VECTOR_DB_KEY")
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        request = Request(
            f"{origin}/readyz",
            headers={"api-key": api_key, "Accept": "text/plain"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
                last_error = RuntimeError(f"Qdrant readiness returned HTTP {response.status}")
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            last_error = error
        time.sleep(1)
    raise LiteConfigurationError(
        f"Qdrant did not become ready within {timeout_seconds:.0f} seconds: {last_error}"
    )


async def run_migrations() -> None:
    from kb.cognee_client import CogneePublicClient

    client = CogneePublicClient()
    client._prepare_cognee_environment()
    import cognee

    await client._ensure_cognee_ready(cognee)


def write_bootstrap_receipt(root: Path) -> Path:
    from importlib.metadata import version

    from kb.qdrant_adapter import physical_collection_name

    state = root / "citadel-state"
    receipt_path = state / "bootstrap.json"
    temporary = state / f".bootstrap.{os.getpid()}.tmp"
    qdrant_url = urlparse(os.environ["VECTOR_DB_URL"])
    generation_id = os.environ["CITADEL_GENERATION_ID"]
    logical_collection = "DocumentChunk_text"
    receipt: dict[str, Any] = {
        "bootstrapped_at": datetime.now(UTC).isoformat(),
        "build_id": _build_id(),
        "citadel_version": version("citadel-archive"),
        "cognee_version": version("cognee"),
        "qdrant_client_version": version("qdrant-client"),
        "qdrant_server_image": os.environ["CITADEL_QDRANT_SERVER_IMAGE"],
        "qdrant_adapter_baseline": _QDRANT_ADAPTER_BASELINE,
        "generation_id": generation_id,
        "relational_provider": "sqlite",
        "vector_provider": "qdrant",
        "qdrant_origin": f"{qdrant_url.scheme}://{qdrant_url.hostname}:{qdrant_url.port or 6333}",
        "qdrant_chunk_collection": physical_collection_name(
            generation_id,
            None,
            logical_collection,
        ),
        "llm_provider": os.environ["LLM_PROVIDER"],
        "llm_model": os.environ["LLM_MODEL"],
        "embedding_provider": os.environ["EMBEDDING_PROVIDER"],
        "embedding_model": os.environ["EMBEDDING_MODEL"],
        "embedding_dimensions": int(os.environ["EMBEDDING_DIMENSIONS"]),
        "backend_access_control": True,
        "readiness_path": "/readyz",
        "readiness_authentication_required": True,
        "bootstrap_stage": "migrations_complete",
    }
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, receipt_path)
    return receipt_path


def web_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "kb.server:app",
        "--host",
        "0.0.0.0",
        "--port",
        os.getenv("PORT", "8000"),
    ]


def main() -> None:
    try:
        root = configure_lite_environment()
        drop_root_privileges(root)
        acquire_single_instance_lock(root)
        quarantined = quarantine_unreadable_sqlite()
        if quarantined is not None:
            print(f"Citadel Lite quarantined unreadable SQLite: {quarantined}", flush=True)
        wait_for_qdrant()
        asyncio.run(run_migrations())
        receipt = write_bootstrap_receipt(root)
    except LiteConfigurationError as error:
        raise SystemExit(f"Citadel Lite startup refused: {error}") from error
    print(f"Citadel Lite bootstrap complete: {receipt}", flush=True)
    os.execv(sys.executable, web_command())


if __name__ == "__main__":
    main()
