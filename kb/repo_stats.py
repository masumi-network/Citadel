"""Live repository figures for the public pages.

Every number on `/` and `/info` used to be a string typed into the HTML, which
was wrong the day after it was written. This module replaces the ones that can
be sourced honestly.

**Why GitHub rather than the local checkout.** A deployed node cannot count its
own commits. Railway builds the repo into a container image with Railpack: the
runtime layer is a separate minimal base, and the only toolchain installed is
python and uv. There is no git binary to run, and the copied source is a
point-in-time snapshot that would go stale between deploys even if there were.
So commit history comes from the GitHub API, which is also the only source that
stays true without a redeploy.

**What is derived locally instead.** Counts that are a property of the source
tree, the architecture decision records and the MCP tool policies, are read off
disk. They are exact, free, and cannot rate-limit.

**What is deliberately still stamped.** The test count. Counting `def test_`
across `tests/` gives 752 against pytest's 906, because of parametrised cases
and test methods on classes, so a locally derived figure would be confidently
wrong by about a sixth. A number that is precise and false is worse than one
that is stale and labelled, and getting it right needs a collection run, which
is a CI artifact rather than something a web process should do.

Everything here fails soft. No token, a 403, a rate limit, a timeout, or a
GitHub outage leaves the last good value in place and never raises into
`/api/state`, the scheduler, or a page render.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from kb.security_scan import redact_secrets

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
CACHE_VERSION = 1

# 52 weeks come back from GitHub; the chart on /info shows a quarter or so.
CHART_WEEKS = 12

# Two missed daily refreshes before the page stops calling the number current.
STALE_AFTER_SECONDS = 48 * 3600

__all__ = [
    "RepoStats",
    "count_adr_records",
    "count_mcp_tools",
    "fetch_commit_activity",
    "load_cache",
    "public_payload",
    "refresh",
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _repo_root() -> Path:
    """The source tree root. `kb/` sits directly under it, deployed included."""
    return Path(__file__).resolve().parent.parent


class RepoStatsUnavailable(RuntimeError):
    """GitHub could not answer right now. Callers keep the cached value."""


@dataclass(frozen=True)
class RepoStats:
    """One GitHub answer: weekly commit counts, oldest week first."""

    weeks: tuple[tuple[str, int], ...]
    commits_total: int
    fetched_at: str

    def as_cache(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "fetched_at": self.fetched_at,
            "weeks": [[start, count] for start, count in self.weeks],
            "commits_total": self.commits_total,
        }

    @classmethod
    def from_cache(cls, raw: Any) -> "RepoStats | None":
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return None
        fetched_at = raw.get("fetched_at")
        if not isinstance(fetched_at, str):
            return None
        weeks: list[tuple[str, int]] = []
        for entry in raw.get("weeks") or []:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                start, count = entry
                if isinstance(start, str) and isinstance(count, int):
                    weeks.append((start, count))
        return cls(
            weeks=tuple(weeks),
            commits_total=int(raw.get("commits_total") or 0),
            fetched_at=fetched_at,
        )


# --------------------------------------------------------------------------
# locally derived counts
# --------------------------------------------------------------------------


def count_adr_records() -> int | None:
    """Numbered architecture decision records on disk, or None if unreadable.

    Counts `NNNN-*.md` rather than every markdown file, so a README or an index
    dropped into the directory never inflates the figure.
    """
    try:
        adr_dir = _repo_root() / "docs" / "adr"
        if not adr_dir.is_dir():
            return None
        return sum(
            1
            for path in adr_dir.glob("*.md")
            if path.name[:4].isdigit()
        )
    except OSError:
        logger.debug("ADR count unavailable", exc_info=True)
        return None


def count_mcp_tools() -> int | None:
    """Tools the MCP server exposes, read from the policy table.

    Imported lazily: `kb.mcp_server` is a server-side module and this one is
    reachable from the CLI import graph, which `test_client_boundary` pins.
    """
    try:
        from kb.mcp_server import TOOL_POLICIES

        return len(TOOL_POLICIES)
    except Exception:
        logger.debug("MCP tool count unavailable", exc_info=True)
        return None


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def fetch_commit_activity(
    repo: str,
    *,
    token: str | None = None,
    timeout: float = 20.0,
) -> RepoStats:
    """52 weeks of commit counts for ``owner/name``.

    One attempt, no retry loop. This runs daily against a 60-per-hour
    unauthenticated budget, so a failure waits for the next cycle rather than
    spending the budget immediately.

    Raises :class:`RepoStatsUnavailable` for every failure mode, including the
    202 that GitHub answers while it computes the statistics for a repository
    it has not cached yet. A 202 carries an empty body, so treating it as
    success would parse as a JSON error and read as a crash rather than as the
    "ask again later" it actually is.
    """
    request = Request(
        f"{GITHUB_API}/repos/{quote(repo, safe='/')}/stats/commit_activity",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "citadel-archive-repo-stats",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status == 202:
                raise RepoStatsUnavailable(
                    "GitHub is still computing commit statistics for this repository."
                )
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # 403 covers both a rate limit and a token that cannot see the repo.
        raise RepoStatsUnavailable(f"GitHub API returned {exc.code}") from exc
    except URLError as exc:
        raise RepoStatsUnavailable(
            f"Could not reach GitHub: {redact_secrets(str(exc.reason), token)}"
        ) from exc
    except (ValueError, TimeoutError, OSError) as exc:
        raise RepoStatsUnavailable(f"Unreadable GitHub response: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise RepoStatsUnavailable("GitHub returned no commit activity.")

    weeks: list[tuple[str, int]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        week = entry.get("week")
        total = entry.get("total")
        if not isinstance(week, int) or not isinstance(total, int):
            continue
        start = datetime.fromtimestamp(week, UTC).date().isoformat()
        weeks.append((start, total))

    if not weeks:
        raise RepoStatsUnavailable("GitHub commit activity had no usable weeks.")

    return RepoStats(
        weeks=tuple(weeks),
        commits_total=sum(count for _, count in weeks),
        fetched_at=_iso(_utc_now()),
    )


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


def load_cache(path: str | Path) -> RepoStats | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return RepoStats.from_cache(raw)


def _save_cache(path: str | Path, stats: RepoStats) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write beside then replace, so a crash mid-write cannot leave the
        # public page reading a half-file.
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(stats.as_cache(), indent=2), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        logger.warning("Could not write repo stats cache to %s", target, exc_info=True)


def refresh(
    config: Any,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> RepoStats | None:
    """Refresh at most once per configured interval. Never raises.

    Returns the stats now in effect, cached or fresh, or None when there has
    never been a successful fetch.
    """
    path = config.repo_stats_state_path
    cached = load_cache(path)
    moment = now or _utc_now()

    if not getattr(config, "repo_stats_enabled", True):
        return cached

    if cached and not force:
        fetched = _parse_iso(cached.fetched_at)
        interval = max(3600, int(getattr(config, "repo_stats_interval_seconds", 86400)))
        if fetched and moment - fetched < timedelta(seconds=interval):
            return cached

    try:
        fresh = fetch_commit_activity(
            config.repo_stats_repo,
            token=getattr(config, "github_token", None),
        )
    except RepoStatsUnavailable as exc:
        # Expected and survivable. The page keeps the last good numbers and
        # labels them, which beats showing zeroes.
        logger.info("Repo stats refresh skipped: %s", exc)
        return cached
    except Exception:
        logger.warning("Repo stats refresh failed unexpectedly", exc_info=True)
        return cached

    _save_cache(path, fresh)
    logger.info(
        "Repo stats refreshed: %s weeks, %s commits",
        len(fresh.weeks),
        fresh.commits_total,
    )
    return fresh


# --------------------------------------------------------------------------
# public payload
# --------------------------------------------------------------------------


def public_payload(config: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """The `repo` block of /api/state. Reads the cache, never the network.

    /api/state is public, unauthenticated and polled by every page load, so it
    must not make an outbound request. The daily task fills the cache; this
    only reads it.

    Carries counts and dates only. No token, no repository name, no path, and
    nothing that is not already public on the pages themselves.
    """
    moment = now or _utc_now()
    stats: RepoStats | None = None
    try:
        stats = load_cache(config.repo_stats_state_path)
    except Exception:  # a cache problem must never break the public snapshot
        logger.debug("Repo stats cache unreadable", exc_info=True)

    payload: dict[str, Any] = {
        "adrs": count_adr_records(),
        "mcp_tools": count_mcp_tools(),
        "weeks": [],
        "commits_total": None,
        "commits_window_weeks": 0,
        "refreshed_at": None,
        "stale": True,
        "source": "unavailable",
    }

    if stats is None:
        return payload

    recent = stats.weeks[-CHART_WEEKS:]
    fetched = _parse_iso(stats.fetched_at)
    age = (moment - fetched).total_seconds() if fetched else None

    payload.update(
        {
            "weeks": [{"start": start, "commits": count} for start, count in recent],
            "commits_total": stats.commits_total,
            "commits_window_weeks": len(stats.weeks),
            "refreshed_at": stats.fetched_at,
            "stale": age is None or age > STALE_AFTER_SECONDS,
            "source": "github",
        }
    )
    return payload
