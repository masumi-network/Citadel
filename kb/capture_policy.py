from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_ORG_CAPTURE_DENY_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "credentials.json",
    "secrets/**",
    "**/secrets/**",
    "*.p12",
    "*.pfx",
)


@dataclass(frozen=True)
class SeatCapturePolicy:
    deny_globs: tuple[str, ...] = ()
    updated_at: str | None = None
    updated_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_deny_globs(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        stripped = value.strip()
        if not stripped:
            continue
        seen.setdefault(stripped, None)
    return tuple(seen)


def merged_deny_globs(
    *,
    env_exclude_patterns: tuple[str, ...],
    seat_deny_globs: tuple[str, ...] = (),
    include_default_org_denies: bool = True,
) -> tuple[str, ...]:
    """Merge env excludes with optional org defaults and per-seat admin baseline."""
    parts: list[str] = list(env_exclude_patterns)
    if include_default_org_denies:
        parts.extend(DEFAULT_ORG_CAPTURE_DENY_GLOBS)
    parts.extend(seat_deny_globs)
    return normalize_deny_globs(parts)


def capture_policy_payload(
    *,
    seat_slug: str | None,
    baseline: SeatCapturePolicy,
    env_exclude_patterns: tuple[str, ...],
) -> dict[str, Any]:
    effective = merged_deny_globs(
        env_exclude_patterns=env_exclude_patterns,
        seat_deny_globs=baseline.deny_globs,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "env_exclude_patterns": list(env_exclude_patterns),
        "default_org_deny_globs": list(DEFAULT_ORG_CAPTURE_DENY_GLOBS),
        "effective_deny_globs": list(effective),
        "baseline": {
            "deny_globs": list(baseline.deny_globs),
            "updated_at": baseline.updated_at,
            "updated_by": baseline.updated_by,
        },
    }
    if seat_slug is not None:
        payload["seat_slug"] = seat_slug
    return payload
