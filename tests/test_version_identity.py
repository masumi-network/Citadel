from __future__ import annotations

from kb import __version__
from kb.build_identity import build_identity_from_env
from kb.cli import _cli_version
from kb.server import _build_id_from_env, app


def test_cli_and_server_use_the_package_source_version() -> None:
    assert __version__ == "0.5.1"
    assert _cli_version() == __version__
    assert app.version == __version__


def test_build_id_is_optional_and_never_falls_back_to_version() -> None:
    assert _build_id_from_env({"RAILWAY_GIT_COMMIT_SHA": "a" * 40}) == "a" * 40
    assert _build_id_from_env({"RAILWAY_GIT_COMMIT_SHA": "b" * 40}) == "b" * 40
    assert _build_id_from_env({"RAILWAY_GIT_COMMIT_SHA": "  "}) is None
    assert _build_id_from_env({}) is None


def test_build_identity_prefers_railway_sha_and_keeps_deployment_separate() -> None:
    identity = build_identity_from_env(
        {
            "RAILWAY_GIT_COMMIT_SHA": "a" * 40,
            "CITADEL_BUILD_ID": "ci-build",
            "RAILWAY_DEPLOYMENT_ID": "deployment-1",
        }
    )

    assert identity.version == __version__
    assert identity.build_id == "a" * 40
    assert identity.deployment_id == "deployment-1"


def test_build_identity_uses_ci_fallback_and_rejects_blank_values() -> None:
    identity = build_identity_from_env(
        {
            "RAILWAY_GIT_COMMIT_SHA": "  ",
            "CITADEL_BUILD_ID": "ci-build",
            "RAILWAY_SNAPSHOT_ID": "snapshot-1",
        }
    )

    assert identity.build_id == "ci-build"
    assert identity.deployment_id == "snapshot-1"
    assert build_identity_from_env({}).build_id is None
