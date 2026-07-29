"""Live repo figures for the public pages.

The whole module exists because a deployed node cannot count its own commits:
Railway builds the repo into an image whose runtime layer installs python and
uv and nothing else, so there is no git binary and no repository to read. The
numbers come from the GitHub API instead, and everything here is about what
happens when that call does not succeed, which on a public page matters more
than the happy path.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from kb import repo_stats
from kb.config import CitadelConfig
from kb.server import app


def _config(tmp_path: Path, **overrides: Any) -> CitadelConfig:
    base = CitadelConfig(
        tenant_id="test",
        default_dataset="notes",
        admin_key="test-admin",
        repo_stats_state_path=str(tmp_path / "repo_stats.json"),
        repo_stats_repo="masumi-network/Citadel",
    )
    return replace(base, **overrides) if overrides else base


def _stats(fetched_at: str, *, commits: int = 340) -> repo_stats.RepoStats:
    return repo_stats.RepoStats(
        weeks=(("2026-07-12", 26), ("2026-07-19", 75)),
        commits_total=commits,
        fetched_at=fetched_at,
    )


def _write_cache(config: CitadelConfig, stats: repo_stats.RepoStats) -> None:
    Path(config.repo_stats_state_path).write_text(
        json.dumps(stats.as_cache()), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# the cache serves stale data when the fetch fails
# --------------------------------------------------------------------------


def test_cache_is_served_when_github_is_unreachable(tmp_path: Path, monkeypatch: Any) -> None:
    """A failed fetch keeps the last good numbers rather than blanking them.

    Stale and labelled beats zeroes. This is the whole reason the cache exists,
    so it is asserted against every failure shape the fetch can raise, not just
    one.
    """
    config = _config(tmp_path)
    _write_cache(config, _stats("2026-07-28T00:00:00Z"))

    for failure in (
        repo_stats.RepoStatsUnavailable("GitHub API returned 403"),
        repo_stats.RepoStatsUnavailable("rate limited"),
        repo_stats.RepoStatsUnavailable("still computing"),
        RuntimeError("something entirely unexpected"),
    ):
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise failure

        monkeypatch.setattr(repo_stats, "fetch_commit_activity", _boom)

        result = repo_stats.refresh(config, force=True)

        assert result is not None, f"{failure!r} lost the cached value"
        assert result.commits_total == 340
        assert result.fetched_at == "2026-07-28T00:00:00Z"
    # ...and the cache file itself was never damaged by the failures.
    assert repo_stats.load_cache(config.repo_stats_state_path) is not None


def test_refresh_without_a_cache_returns_none_and_does_not_raise(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """First boot with no token and no cache: no numbers, but no crash."""
    config = _config(tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise repo_stats.RepoStatsUnavailable("no token")

    monkeypatch.setattr(repo_stats, "fetch_commit_activity", _boom)

    assert repo_stats.refresh(config, force=True) is None


def test_refresh_honours_the_daily_floor(tmp_path: Path, monkeypatch: Any) -> None:
    """At most one GitHub request a day.

    Unauthenticated GitHub allows 60 requests an hour. The loop wakes hourly so
    a missed window is picked up promptly, which only works because refresh()
    itself refuses to go out again inside the interval.
    """
    config = _config(tmp_path, repo_stats_interval_seconds=86400)
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    _write_cache(config, _stats((now - timedelta(hours=2)).isoformat()))

    calls: list[str] = []

    def _count(repo: str, **kwargs: Any) -> repo_stats.RepoStats:
        calls.append(repo)
        return _stats(now.isoformat(), commits=999)

    monkeypatch.setattr(repo_stats, "fetch_commit_activity", _count)

    fresh_enough = repo_stats.refresh(config, now=now)
    assert calls == [], "refreshed inside the interval"
    assert fresh_enough is not None and fresh_enough.commits_total == 340

    # A day later the floor has passed.
    repo_stats.refresh(config, now=now + timedelta(hours=25))
    assert calls == ["masumi-network/Citadel"]


def test_a_202_is_treated_as_try_again_not_as_data(monkeypatch: Any) -> None:
    """GitHub answers 202 with an empty body while it computes the statistics.

    Observed on the real repository: the very first request returned 202. Read
    as success it parses as a JSON error and looks like a crash, when it means
    "ask again later" and the cached value should stand.
    """

    class _Response:
        status = 202

        def read(self) -> bytes:
            return b""

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(repo_stats, "open_secure", lambda *a, **k: _Response())

    try:
        repo_stats.fetch_commit_activity("masumi-network/Citadel")
    except repo_stats.RepoStatsUnavailable as exc:
        assert "computing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a 202 must not be read as commit data")


def test_a_corrupt_cache_file_does_not_break_anything(tmp_path: Path) -> None:
    """A half-written or hand-edited cache degrades to no data, never a raise."""
    config = _config(tmp_path)
    path = Path(config.repo_stats_state_path)

    for junk in ("", "{", "null", "[]", '{"version": 999}', '{"version": 1}'):
        path.write_text(junk, encoding="utf-8")
        assert repo_stats.load_cache(path) is None
        payload = repo_stats.public_payload(config)
        assert payload["source"] == "unavailable"
        assert payload["weeks"] == []


# --------------------------------------------------------------------------
# locally derived counts
# --------------------------------------------------------------------------


def test_local_counts_are_real_and_not_stamped() -> None:
    """ADRs and MCP tools are read off the source tree, so they cannot drift."""
    adrs = repo_stats.count_adr_records()
    tools = repo_stats.count_mcp_tools()

    assert adrs is not None and adrs > 0
    assert tools is not None and tools > 0

    from kb.mcp_server import TOOL_POLICIES

    assert tools == len(TOOL_POLICIES)
    # Numbered records only: a README or an index in docs/adr must not count.
    adr_dir = Path(repo_stats.__file__).resolve().parent.parent / "docs" / "adr"
    assert adrs == len([p for p in adr_dir.glob("*.md") if p.name[:4].isdigit()])


# --------------------------------------------------------------------------
# /api/state
# --------------------------------------------------------------------------


def test_api_state_is_valid_with_no_token_and_no_cache(monkeypatch: Any, tmp_path: Path) -> None:
    """The public snapshot survives a node that has never reached GitHub.

    No token, no cache file. /api/state must still answer 200 with its existing
    shape intact, and say the commit figures are unavailable rather than
    reporting zero commits, which would be a claim rather than an absence.
    """
    monkeypatch.delenv("CITADEL_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client = TestClient(app, base_url="https://testserver")

    payload = client.get("/api/state").json()

    assert payload["ok"] is True
    # The shape /info already reads must not move.
    for key in ("service", "version", "healthy", "sources", "totals", "updated_at"):
        assert key in payload, f"/api/state lost {key}"
    assert "documents" in payload["totals"]

    repo = payload["repo"]
    assert repo["source"] in {"github", "unavailable"}
    if repo["source"] == "unavailable":
        assert repo["commits_total"] is None, "absent data must not read as zero commits"
        assert repo["weeks"] == []
        assert repo["stale"] is True
    # Locally derived counts need no token and are always present.
    assert repo["adrs"] > 0
    assert repo["mcp_tools"] > 0


def test_api_state_serves_cached_commits_and_flags_staleness(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Cached numbers are published, and old ones are labelled old."""
    config = _config(tmp_path)
    monkeypatch.setattr(
        "kb.server.get_citadel", lambda: type("_C", (), {"config": config})()
    )

    fresh = datetime.now(UTC) - timedelta(hours=3)
    _write_cache(config, _stats(fresh.isoformat()))
    payload = repo_stats.public_payload(config)
    assert payload["source"] == "github"
    assert payload["commits_total"] == 340
    assert payload["stale"] is False
    assert payload["weeks"][0] == {"start": "2026-07-12", "commits": 26}

    old = datetime.now(UTC) - timedelta(days=4)
    _write_cache(config, _stats(old.isoformat()))
    assert repo_stats.public_payload(config)["stale"] is True


def test_no_secret_reaches_the_public_payload(tmp_path: Path) -> None:
    """/api/state is unauthenticated, so the repo block carries counts only.

    Asserted against the serialised payload rather than the keys, because the
    risk is a token or a path arriving inside a value or an error string, not
    someone adding a field called "token".
    """
    secret = "ghp_examplevalueneverlogged0000000000000"
    config = _config(
        tmp_path,
        github_token=secret,
        repo_stats_repo="masumi-network/a-private-repo",
    )
    _write_cache(config, _stats("2026-07-29T00:00:00Z"))

    serialised = json.dumps(repo_stats.public_payload(config))

    assert secret not in serialised
    assert "ghp_" not in serialised
    # Never the repository name, never a filesystem path, never the token.
    assert "a-private-repo" not in serialised
    assert str(tmp_path) not in serialised
    assert "repo_stats.json" not in serialised

    allowed = {
        "adrs",
        "mcp_tools",
        "weeks",
        "commits_total",
        "commits_window_weeks",
        "refreshed_at",
        "stale",
        "source",
    }
    assert set(repo_stats.public_payload(config)) == allowed


def test_api_state_survives_a_broken_repo_stats_module(monkeypatch: Any) -> None:
    """A fault in the stats path degrades the block, never the endpoint.

    /api/state is polled by every public page load, so it fails soft on this
    the way it already does on a syncer hiccup.
    """
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("stats exploded")

    monkeypatch.setattr(repo_stats, "public_payload", _boom)
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["repo"]["source"] == "unavailable"
