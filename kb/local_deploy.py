from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


class LocalDeployError(RuntimeError):
    pass


DEFAULT_CONFIG_DIR = Path.home() / ".citadel" / "deploy" / "local"
_PINNED_IMAGE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_REQUIRED_EXISTING_KEYS = {
    "CITADEL_ADMIN_KEY",
    "CITADEL_GENERATION_ID",
    "CITADEL_IMAGE",
    "CITADEL_PORT",
    "LLM_API_KEY",
    "QDRANT_API_KEY",
}


def _run_checked(
    command: list[str],
    *,
    stage: str,
    timeout_seconds: float = 120.0,
) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise LocalDeployError(f"{stage} could not run") from error
    if result.returncode != 0:
        raise LocalDeployError(f"{stage} failed with exit code {result.returncode}")
    return result.stdout.strip()


def _docker_preflight() -> dict[str, str]:
    docker = shutil.which("docker")
    if docker is None:
        raise LocalDeployError("Docker Engine is required and was not found")
    engine = _run_checked(
        [docker, "version", "--format", "{{.Server.Version}}"],
        stage="Docker Engine preflight",
    )
    compose = _run_checked(
        [docker, "compose", "version", "--short"],
        stage="Docker Compose v2 preflight",
    )
    return {"docker": docker, "engine_version": engine, "compose_version": compose}


def _quote_env(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise LocalDeployError("deployment values must be single-line text")
    return json.dumps(value)


def _write_exclusive(path: Path, content: str, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _read_generated_env(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise LocalDeployError("existing deployment .env must be a regular file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise LocalDeployError("existing deployment .env permissions must be 0600 or stricter")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, raw = line.partition("=")
        if not separator:
            raise LocalDeployError("existing deployment .env is malformed")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LocalDeployError("existing deployment .env is malformed") from error
        if not isinstance(value, str):
            raise LocalDeployError("existing deployment .env is malformed")
        values[name] = value
    missing = sorted(_REQUIRED_EXISTING_KEYS - set(values))
    if missing:
        raise LocalDeployError(
            "existing deployment .env is missing required configuration"
        )
    return values


def _source_tree(source_dir: Path | None) -> Path | None:
    candidate = source_dir
    if candidate is None and (Path.cwd() / "Dockerfile").is_file():
        candidate = Path.cwd()
    if candidate is None:
        return None
    resolved = candidate.resolve()
    if not (resolved / "Dockerfile").is_file() or not (resolved / "pyproject.toml").is_file():
        raise LocalDeployError("source directory must contain Dockerfile and pyproject.toml")
    return resolved


def _validate_port(port: int) -> int:
    if port < 1 or port > 65535:
        raise LocalDeployError("port must be between 1 and 65535")
    return port


def _new_environment(
    *,
    source_dir: Path | None,
    image: str | None,
    port: int,
) -> tuple[dict[str, str], bool]:
    if source_dir is not None and image is not None:
        raise LocalDeployError("choose either --source-dir or --image")
    source = None if image is not None else _source_tree(source_dir)
    source_build = source is not None
    selected_image = image or "citadel-archive:local"
    if not source_build and not _PINNED_IMAGE.fullmatch(selected_image):
        raise LocalDeployError("published image must include an exact sha256 digest")
    llm_api_key = os.getenv("LLM_API_KEY", "").strip()
    if not llm_api_key:
        raise LocalDeployError("set LLM_API_KEY in the environment before first deployment")
    return (
        {
            "CITADEL_PORT": str(_validate_port(port)),
            "CITADEL_GENERATION_ID": f"citadel-local-{uuid4().hex}",
            "CITADEL_ADMIN_KEY": f"ctdl_{secrets.token_urlsafe(48)}",
            "QDRANT_API_KEY": secrets.token_urlsafe(48),
            "LLM_API_KEY": llm_api_key,
            "CITADEL_SOURCE_DIR": str(source) if source is not None else "",
            "CITADEL_IMAGE": selected_image,
        },
        source_build,
    )


def _create_config(
    config_dir: Path,
    *,
    source_dir: Path | None,
    image: str | None,
    port: int,
) -> tuple[dict[str, str], bool]:
    environment, source_build = _new_environment(
        source_dir=source_dir,
        image=image,
        port=port,
    )
    config_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(config_dir, 0o700)
    env_content = "".join(
        f"{name}={_quote_env(value)}\n"
        for name, value in sorted(environment.items())
    )
    compose_content = (
        files("kb.deploy_assets").joinpath("docker-compose.yml").read_text(encoding="utf-8")
    )
    try:
        _write_exclusive(config_dir / ".env", env_content, 0o600)
        _write_exclusive(config_dir / "docker-compose.yml", compose_content, 0o644)
    except FileExistsError as error:
        raise LocalDeployError("deployment config appeared concurrently; retry") from error
    return environment, source_build


def _load_or_create_config(
    config_dir: Path,
    *,
    source_dir: Path | None,
    image: str | None,
    port: int,
) -> tuple[dict[str, str], bool, bool]:
    if config_dir.is_symlink():
        raise LocalDeployError("deployment config directory must not be a symlink")
    env_path = config_dir / ".env"
    compose_path = config_dir / "docker-compose.yml"
    if env_path.exists() != compose_path.exists():
        raise LocalDeployError("deployment config is incomplete; refusing to overwrite it")
    if env_path.exists():
        if compose_path.is_symlink() or not compose_path.is_file():
            raise LocalDeployError("existing deployment Compose file must be regular")
        environment = _read_generated_env(env_path)
        if source_dir is not None or image is not None or port != int(environment["CITADEL_PORT"]):
            requested_source = str(source_dir.resolve()) if source_dir is not None else ""
            if (
                requested_source != environment.get("CITADEL_SOURCE_DIR", "")
                or (image is not None and image != environment["CITADEL_IMAGE"])
                or port != int(environment["CITADEL_PORT"])
            ):
                raise LocalDeployError(
                    "existing deployment identity differs; use its recorded settings"
                )
        source_build = bool(environment.get("CITADEL_SOURCE_DIR"))
        if not source_build and not _PINNED_IMAGE.fullmatch(environment["CITADEL_IMAGE"]):
            raise LocalDeployError("existing deployment image is not digest-pinned")
        return environment, source_build, False
    environment, source_build = _create_config(
        config_dir,
        source_dir=source_dir,
        image=image,
        port=port,
    )
    return environment, source_build, True


def _run_compose(config_dir: Path, docker: str, *, source_build: bool) -> None:
    base = [
        docker,
        "compose",
        "--project-directory",
        str(config_dir),
        "--env-file",
        str(config_dir / ".env"),
        "-f",
        str(config_dir / "docker-compose.yml"),
    ]
    _run_checked([*base, "config", "--quiet"], stage="Docker Compose config validation")
    mode = "--build" if source_build else "--no-build"
    _run_checked(
        [*base, "up", "-d", mode],
        stage="Docker Compose startup",
        timeout_seconds=1800.0 if source_build else 600.0,
    )


def _wait_for_ready(*, port: int, token: str, timeout_seconds: float) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise LocalDeployError("readiness timeout must be positive")
    deadline = time.monotonic() + timeout_seconds
    last_status = "no response"
    while time.monotonic() < deadline:
        request = Request(
            f"http://127.0.0.1:{port}/readyz",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
                    return {
                        "ok": True,
                        "service": payload.get("service"),
                        "generation_id": payload.get("generation_id"),
                    }
                last_status = f"HTTP {response.status} not ready"
        except HTTPError as error:
            last_status = f"HTTP {error.code}"
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            last_status = "no valid readiness response"
        time.sleep(1)
    raise LocalDeployError(
        f"authenticated readiness failed within {timeout_seconds:g}s ({last_status})"
    )


def deploy_local(
    *,
    config_dir: Path | None,
    source_dir: Path | None,
    image: str | None,
    port: int,
    dry_run: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    target = (config_dir or DEFAULT_CONFIG_DIR).expanduser().absolute()
    if target.exists() and (target.is_symlink() or not target.is_dir()):
        raise LocalDeployError("deployment config path must be a directory")
    preflight = _docker_preflight()
    if dry_run:
        source = None if image is not None else _source_tree(source_dir)
        if source is None and image is None:
            raise LocalDeployError("pass --source-dir or a digest-pinned --image")
        if source is None and not _PINNED_IMAGE.fullmatch(str(image)):
            raise LocalDeployError("published image must include an exact sha256 digest")
        _validate_port(port)
        return {
            "ok": True,
            "dry_run": True,
            "config_dir": str(target),
            "source_build": source is not None,
            "docker_engine": preflight["engine_version"],
            "docker_compose": preflight["compose_version"],
        }
    environment, source_build, created = _load_or_create_config(
        target,
        source_dir=source_dir,
        image=image,
        port=port,
    )
    _run_compose(target, preflight["docker"], source_build=source_build)
    ready = _wait_for_ready(
        port=int(environment["CITADEL_PORT"]),
        token=environment["CITADEL_ADMIN_KEY"],
        timeout_seconds=timeout_seconds,
    )
    return {
        "ok": True,
        "dry_run": False,
        "created": created,
        "config_dir": str(target),
        "url": f"http://127.0.0.1:{environment['CITADEL_PORT']}",
        "source_build": source_build,
        "generation_id": environment["CITADEL_GENERATION_ID"],
        "readiness": ready,
        "docker_engine": preflight["engine_version"],
        "docker_compose": preflight["compose_version"],
    }
