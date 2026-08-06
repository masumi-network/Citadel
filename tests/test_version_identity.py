from __future__ import annotations

from kb import __version__
from kb.cli import _cli_version
from kb.server import _build_id_from_env, app


def test_cli_and_server_use_the_package_source_version() -> None:
    assert __version__ == "0.4.1"
    assert _cli_version() == __version__
    assert app.version == __version__


def test_build_id_is_optional_and_never_falls_back_to_version() -> None:
    assert _build_id_from_env({"RAILWAY_GIT_COMMIT_SHA": "a" * 40}) == "a" * 40
    assert _build_id_from_env({"RAILWAY_GIT_COMMIT_SHA": "b" * 40}) == "b" * 40
    assert _build_id_from_env({"RAILWAY_GIT_COMMIT_SHA": "  "}) is None
    assert _build_id_from_env({}) is None
