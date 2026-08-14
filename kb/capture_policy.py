from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
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


def path_is_denied(path: str, globs: tuple[str, ...] | None = None) -> bool:
    """True when ``path`` matches a deny glob on the posix path or its basename.

    ``fnmatch("/abs/.env", ".env")`` is false. GitHub blob URLs and
    ``github:org/repo:path:…`` locators are reduced to the file path first.
    """
    from fnmatch import fnmatchcase
    from urllib.parse import urlsplit

    raw = path.strip()
    if not raw:
        return False
    patterns = DEFAULT_ORG_CAPTURE_DENY_GLOBS if globs is None else globs
    posix = raw.replace("\\", "/")
    if posix.startswith("file://"):
        posix = posix[7:]
    candidates: set[str] = {posix, Path(posix).name}
    if ":path:" in posix:
        rest = posix.rsplit(":path:", 1)[-1]
        candidates.add(rest)
        candidates.add(Path(rest).name)
    if "://" in posix:
        url_path = urlsplit(posix).path.lstrip("/")
        if url_path:
            candidates.add(url_path)
            candidates.add(Path(url_path).name)
    return any(
        fnmatchcase(candidate, pattern)
        for pattern in patterns
        for candidate in candidates
        if candidate
    )


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
