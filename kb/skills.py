from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Short public paths → bundled agent skills (no auth required).
# These live in the top-level ``skills/`` directory so they are discoverable both
# by the hosted ``/skills`` endpoint and by skills.sh
# (``npx skills add masumi-network/citadel --skill …``).
# The primary skill is ``skills/citadel/SKILL.md`` (not a repo-root file —
# a root SKILL.md would shadow ``skills/*`` in the skills CLI).
SKILL_FILES: dict[str, Path] = {
    "connect": REPO_ROOT / "skills/citadel-mcp-connector/SKILL.md",
    "vault": REPO_ROOT / "skills/citadel-vault/SKILL.md",
    "boundary": REPO_ROOT / "skills/citadel-data-boundary/SKILL.md",
    "proactive-ingest": REPO_ROOT / "skills/citadel-proactive-ingest/SKILL.md",
}

SKILL_ALIASES: dict[str, str] = {
    "mcp": "connect",
    "mcp-connector": "connect",
    "citadel-mcp-connector": "connect",
    "citadel-vault": "vault",
    "policy": "boundary",
    "privacy": "boundary",
    "public-private": "boundary",
    "citadel-data-boundary": "boundary",
    "autosync": "proactive-ingest",
    "citadel-proactive-ingest": "proactive-ingest",
}


def resolve_skill_slug(slug: str) -> str | None:
    normalized = slug.strip().lower().removesuffix(".md")
    if normalized in SKILL_FILES:
        return normalized
    return SKILL_ALIASES.get(normalized)


def skill_path(slug: str) -> Path | None:
    resolved = resolve_skill_slug(slug)
    if resolved is None:
        return None
    path = SKILL_FILES[resolved]
    return path if path.is_file() else None


def skill_integrity(path: Path) -> dict[str, str | int]:
    content = path.read_bytes()
    digest = hashlib.sha256(content).digest()
    sha256 = digest.hex()
    return {
        "size_bytes": len(content),
        "sha256": sha256,
        "integrity": f"sha256-{base64.b64encode(digest).decode('ascii')}",
    }


def skill_catalog() -> list[dict[str, object]]:
    """Metadata for the public /skills index (canonical slugs only)."""
    aliases_by_slug: dict[str, list[str]] = {slug: [] for slug in SKILL_FILES}
    for alias, target in SKILL_ALIASES.items():
        if target in aliases_by_slug:
            aliases_by_slug[target].append(alias)
    rows: list[dict[str, object]] = []
    for slug in sorted(SKILL_FILES):
        path = SKILL_FILES[slug]
        rows.append(
            {
                "slug": slug,
                "description": _SKILL_DESCRIPTIONS.get(slug, ""),
                "aliases": sorted(aliases_by_slug[slug]),
                **skill_integrity(path),
            }
        )
    return rows


_SKILL_DESCRIPTIONS: dict[str, str] = {
    "connect": "Set up Citadel MCP in Claude Code, Codex, Cursor, or any MCP agent.",
    "vault": "Search, ingest, and use the Organization Vault after MCP is connected.",
    "boundary": "Public vs private data boundaries for Citadel code, vault, and tokens.",
    "proactive-ingest": "Autonomous Node sync — git push + Claude SessionEnd hooks to your personal seat.",
}


def skills_state_path(value: str | None = None) -> str:
    """Where the scheduled pipeline persists last-seen skill content hashes."""
    if value:
        return value
    configured = os.getenv("CITADEL_SKILLS_STATE_PATH")
    if configured:
        return configured
    root = (
        os.getenv("CITADEL_STATE_DIRECTORY")
        or os.getenv("SYSTEM_ROOT_DIRECTORY")
        or ("/data/.citadel" if Path("/data").exists() else ".citadel")
    )
    return str(Path(root) / "skills_catalog.json")


def refresh_skill_catalog(state_path: str | Path | None = None) -> dict[str, object]:
    """Re-hash the bundled skills and report what changed since the last run.

    Used by the scheduled pipeline so skill/plugin updates shipped with a
    deploy are picked up and visible in the run summary. The catalog itself is
    always computed fresh from disk; this only tracks change detection state.
    """
    path = Path(skills_state_path(str(state_path) if state_path else None))
    catalog = skill_catalog()
    current = {row["slug"]: row["sha256"] for row in catalog}

    previous: dict[str, str] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = {
                    str(slug): str(digest) for slug, digest in (loaded.get("skills") or {}).items()
                }
        except (OSError, json.JSONDecodeError, AttributeError):
            previous = {}

    changed = sorted(
        slug for slug, digest in current.items() if slug in previous and previous[slug] != digest
    )
    added = sorted(slug for slug in current if slug not in previous)
    removed = sorted(slug for slug in previous if slug not in current)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"skills": current}, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "skills": len(current),
        "changed": changed,
        "added": added,
        "removed": removed,
        "state_path": str(path),
        "catalog": catalog,
    }
