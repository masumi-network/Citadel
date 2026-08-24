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


_BUILD_MARKER_RE = re.compile(r"[0-9a-fA-F]{64}")
_DEFAULT_BUILD_ID_PATH = "/opt/citadel/build-id"


def build_identity_from_env(env: Mapping[str, str]) -> BuildIdentity:
    """Capture source and deployment identifiers from the running environment.

    Railway supplies ``RAILWAY_GIT_COMMIT_SHA`` for Git-triggered deploys. CI
    can provide the same exact commit through ``CITADEL_BUILD_ID`` when the
    platform does not inject Railway's variable. Neither release version nor
    deployment ID is used as a substitute for the source build ID.
    """

    return BuildIdentity(
        version=__version__,
        build_id=_first_nonempty(env, ("RAILWAY_GIT_COMMIT_SHA", "CITADEL_BUILD_ID")),
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
    if _BUILD_MARKER_RE.fullmatch(value) is None:
        return None
    return value.lower()


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
