from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kb import local_deploy
from kb.cli import _deploy_local, build_parser


def _source_tree(root: Path) -> Path:
    root.mkdir()
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    return root


def _preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        local_deploy,
        "_docker_preflight",
        lambda: {"docker": "/usr/bin/docker", "engine_version": "29", "compose_version": "5"},
    )


def test_parser_adds_deploy_local_without_changing_setup_or_onboard() -> None:
    parser = build_parser()

    deploy = parser.parse_args(["deploy", "local", "--dry-run", "--source-dir", "."])
    setup = parser.parse_args(["setup", "--show"])
    onboard = parser.parse_args(["onboard", "--non-interactive", "--token", "test"])

    assert deploy.handler is _deploy_local
    assert setup.command == "setup"
    assert onboard.command == "onboard"


def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _preflight(monkeypatch)
    source = _source_tree(tmp_path / "source")
    target = tmp_path / "deploy"

    result = local_deploy.deploy_local(
        config_dir=target,
        source_dir=source,
        image=None,
        port=8000,
        dry_run=True,
        timeout_seconds=30,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert not target.exists()


def test_new_deploy_writes_private_config_and_reuses_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _preflight(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "local-test-llm-key")
    source = _source_tree(tmp_path / "source")
    target = tmp_path / "deploy"
    compose_calls: list[bool] = []
    monkeypatch.setattr(
        local_deploy,
        "_run_compose",
        lambda _target, _docker, *, source_build: compose_calls.append(source_build),
    )
    monkeypatch.setattr(
        local_deploy,
        "_wait_for_ready",
        lambda **_: {"ok": True, "service": "citadel", "generation_id": None},
    )

    first = local_deploy.deploy_local(
        config_dir=target,
        source_dir=source,
        image=None,
        port=8123,
        dry_run=False,
        timeout_seconds=30,
    )
    before = (target / ".env").read_bytes()
    second = local_deploy.deploy_local(
        config_dir=target,
        source_dir=source,
        image=None,
        port=8123,
        dry_run=False,
        timeout_seconds=30,
    )

    assert first["created"] is True
    assert second["created"] is False
    assert (target / ".env").read_bytes() == before
    assert stat_mode(target) == 0o700
    assert stat_mode(target / ".env") == 0o600
    assert "local-test-llm-key" not in str(first)
    assert compose_calls == [True, True]


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_published_image_requires_digest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _preflight(monkeypatch)

    with pytest.raises(local_deploy.LocalDeployError, match="exact sha256 digest"):
        local_deploy.deploy_local(
            config_dir=tmp_path / "deploy",
            source_dir=None,
            image="ghcr.io/example/citadel:0.5.0",
            port=8000,
            dry_run=True,
            timeout_seconds=30,
        )


def test_explicit_image_disables_source_auto_detection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _preflight(monkeypatch)
    source = _source_tree(tmp_path / "source")
    monkeypatch.chdir(source)
    image = f"ghcr.io/example/citadel:0.5.0@sha256:{'a' * 64}"

    result = local_deploy.deploy_local(
        config_dir=tmp_path / "deploy",
        source_dir=None,
        image=image,
        port=8000,
        dry_run=True,
        timeout_seconds=30,
    )

    assert result["source_build"] is False


def test_existing_secret_file_with_open_permissions_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _preflight(monkeypatch)
    target = tmp_path / "deploy"
    target.mkdir()
    (target / ".env").write_text("CITADEL_ADMIN_KEY=\"secret\"\n", encoding="utf-8")
    (target / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    os.chmod(target / ".env", 0o644)

    with pytest.raises(local_deploy.LocalDeployError, match="permissions"):
        local_deploy.deploy_local(
            config_dir=target,
            source_dir=None,
            image=None,
            port=8000,
            dry_run=False,
            timeout_seconds=30,
        )


def test_source_compose_build_has_a_cold_build_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    timeouts: list[float] = []

    def fake_run(*_args, timeout: float, **_kwargs):
        timeouts.append(timeout)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_deploy.subprocess, "run", fake_run)
    config_dir = tmp_path / "deploy"
    config_dir.mkdir()
    (config_dir / ".env").write_text("", encoding="utf-8")
    (config_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    local_deploy._run_compose(
        config_dir,
        "/usr/bin/docker",
        source_build=True,
    )

    assert timeouts == [120.0, 1800.0]
