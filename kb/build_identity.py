"""Build and deployment identity shared by every public service surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re

from kb import __version__


def _first_nonempty(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True)
class BuildIdentity:
    """Identity reported without guessing when deployment metadata is absent."""

    version: str
    build_id: str | None
    deployment_id: str | None


_GIT_REVISION_RE = re.compile(r"[0-9a-fA-F]{40}")
_DEFAULT_BUILD_ID_PATH = "/opt/citadel/build-id"


def _git_revision(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if _GIT_REVISION_RE.fullmatch(candidate) is None:
        return None
    return candidate.lower()


def _first_git_revision(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        revision = _git_revision(env.get(name))
        if revision is not None:
            return revision
    return None


def build_identity_from_env(env: Mapping[str, str]) -> BuildIdentity:
    """Capture source and deployment identifiers from the running environment.

    Railway supplies ``RAILWAY_GIT_COMMIT_SHA`` for Git-triggered deploys. CI
    can provide the same exact commit through ``CITADEL_BUILD_ID`` when the
    platform does not inject Railway's variable. Neither release version nor
    deployment ID is used as a substitute for the source build ID.
    """

    return BuildIdentity(
        version=__version__,
        build_id=_first_git_revision(env, ("RAILWAY_GIT_COMMIT_SHA", "CITADEL_BUILD_ID")),
        deployment_id=_first_nonempty(
            env,
            ("RAILWAY_DEPLOYMENT_ID", "RAILWAY_SNAPSHOT_ID", "CITADEL_DEPLOYMENT_ID"),
        ),
    )


def _build_id_from_marker(path: str) -> str | None:
    try:
        value = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return _git_revision(value)


def write_build_id_marker(path: str, env: Mapping[str, str]) -> str | None:
    """Write the exact source revision, or an empty marker when unavailable."""
    revision = build_identity_from_env(env).build_id
    Path(path).write_text(f"{revision}\n" if revision is not None else "", encoding="ascii")
    return revision


def build_identity_from_runtime(
    env: Mapping[str, str], *, build_id_path: str | None = None
) -> BuildIdentity:
    """Read environment identity, then the immutable image build marker.

    The marker is only a fallback. A Railway commit or explicit build ID remains
    authoritative when present. Invalid or unreadable marker contents stay absent.
    """
    identity = build_identity_from_env(env)
    if identity.build_id is not None:
        return identity
    marker_path = (
        build_id_path
        if build_id_path is not None
        else env.get("CITADEL_BUILD_ID_PATH", _DEFAULT_BUILD_ID_PATH)
    ).strip()
    if not marker_path:
        return identity
    return BuildIdentity(
        version=identity.version,
        build_id=_build_id_from_marker(marker_path),
        deployment_id=identity.deployment_id,
    )


SERVICE_BUILD_IDENTITY = build_identity_from_runtime(os.environ)
