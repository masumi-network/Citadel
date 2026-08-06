"""Build and deployment identity shared by every public service surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os

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


SERVICE_BUILD_IDENTITY = build_identity_from_env(os.environ)
