from __future__ import annotations

from typing import Any

from kb import __version__
from kb.build_identity import (
    build_identity_from_env,
    build_identity_from_runtime,
    write_build_id_marker,
)
from kb.cli import _cli_version
from kb.server import _build_id_from_env, app


def test_cli_and_server_use_the_package_source_version() -> None:
    assert __version__ == "0.5.2"
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
            "CITADEL_BUILD_ID": "b" * 40,
            "RAILWAY_DEPLOYMENT_ID": "deployment-1",
        }
    )

    assert identity.version == __version__
    assert identity.build_id == "a" * 40
    assert identity.deployment_id == "deployment-1"


def test_build_identity_uses_exact_ci_fallback_and_rejects_invalid_values() -> None:
    identity = build_identity_from_env(
        {
            "RAILWAY_GIT_COMMIT_SHA": "  ",
            "CITADEL_BUILD_ID": "b" * 40,
            "RAILWAY_SNAPSHOT_ID": "snapshot-1",
        }
    )

    assert identity.build_id == "b" * 40
    assert identity.deployment_id == "snapshot-1"
    assert build_identity_from_env({"RAILWAY_GIT_COMMIT_SHA": "not-a-sha"}).build_id is None
    assert build_identity_from_env({}).build_id is None


def test_runtime_build_identity_accepts_baked_git_revision(tmp_path: Any) -> None:
    marker = tmp_path / "build-id"
    marker.write_text("D" * 40 + "\n", encoding="ascii")

    identity = build_identity_from_runtime(
        {"CITADEL_BUILD_ID_PATH": str(marker)},
    )

    assert identity.build_id == "d" * 40


def test_runtime_build_identity_keeps_environment_precedence(tmp_path: Any) -> None:
    marker = tmp_path / "build-id"
    marker.write_text("b" * 40, encoding="ascii")

    identity = build_identity_from_runtime(
        {
            "RAILWAY_GIT_COMMIT_SHA": "c" * 40,
            "CITADEL_BUILD_ID_PATH": str(marker),
        },
    )

    assert identity.build_id == "c" * 40


def test_runtime_build_identity_skips_invalid_environment_values(tmp_path: Any) -> None:
    marker = tmp_path / "build-id"
    marker.write_text("d" * 40, encoding="ascii")

    identity = build_identity_from_runtime(
        {
            "RAILWAY_GIT_COMMIT_SHA": "not-a-sha",
            "CITADEL_BUILD_ID": "also-not-a-sha",
            "CITADEL_BUILD_ID_PATH": str(marker),
        },
    )

    assert identity.build_id == "d" * 40


def test_build_id_marker_writes_only_an_exact_source_revision(tmp_path: Any) -> None:
    marker = tmp_path / "build-id"

    assert write_build_id_marker(
        str(marker),
        {"RAILWAY_GIT_COMMIT_SHA": "E" * 40},
    ) == "e" * 40
    assert marker.read_text(encoding="ascii") == "e" * 40 + "\n"

    assert write_build_id_marker(
        str(marker),
        {"RAILWAY_GIT_COMMIT_SHA": "not-a-sha"},
    ) is None
    assert marker.read_text(encoding="ascii") == ""


def test_runtime_build_identity_rejects_missing_or_malformed_marker(tmp_path: Any) -> None:
    marker = tmp_path / "build-id"
    marker.write_text("not-a-sha", encoding="ascii")

    assert build_identity_from_runtime(
        {"CITADEL_BUILD_ID_PATH": str(marker)},
    ).build_id is None
    assert build_identity_from_runtime(
        {"CITADEL_BUILD_ID_PATH": str(tmp_path / "missing")},
    ).build_id is None
