from __future__ import annotations

from kb import __version__
from kb.cli import _cli_version
from kb.server import _runtime_build_id, app


def test_cli_and_server_use_the_package_source_version() -> None:
    assert __version__ == "0.5.0"
    assert _cli_version() == __version__
    assert app.version == __version__


def test_runtime_build_id_is_optional_and_never_falls_back_to_version(monkeypatch) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("CITADEL_BUILD_ID", raising=False)
    assert _runtime_build_id() is None

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    assert _runtime_build_id() == "a" * 40

    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setenv("CITADEL_BUILD_ID", "local-build")
    assert _runtime_build_id() == "local-build"
