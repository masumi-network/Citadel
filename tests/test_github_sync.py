from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kb.config import CitadelConfig
from kb.github_sync import (
    GitHubCommit,
    GitHubEvent,
    GitHubOrgClient,
    GitHubOrgSyncer,
    GitHubPullRequest,
    GitHubRepo,
)
from kb.models import IngestResult


class FakeCitadel:
    def __init__(self, config: CitadelConfig) -> None:
        self.config = config
        self.ingest_calls: list[dict[str, Any]] = []
        self.improve_calls: list[dict[str, Any]] = []

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        return IngestResult(True, "accepted", kwargs["dataset"], tuple(kwargs["tags"]))

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        self.improve_calls.append(kwargs)
        return {"ok": True}


class FailingImproveCitadel(FakeCitadel):
    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        self.improve_calls.append(kwargs)
        raise RuntimeError("llm unavailable")


class FakeGitHubClient:
    def fetch_repos(
        self,
        org: str,
        *,
        max_repos: int,
        include_private: bool = True,
    ) -> list[GitHubRepo]:
        return [
            GitHubRepo(
                name="agent",
                full_name=f"{org}/agent",
                html_url=f"https://github.com/{org}/agent",
                description="Agent runtime",
                language="TypeScript",
                pushed_at="2026-05-21T00:00:00Z",
                updated_at="2026-05-21T00:00:00Z",
                default_branch="main",
                visibility="public",
                archived=False,
                stargazers_count=7,
                forks_count=2,
                open_issues_count=1,
                topics=("masumi", "agent"),
                license_name="Apache License 2.0",
            )
        ][:max_repos]

    def fetch_events(self, org: str, *, max_events: int) -> list[GitHubEvent]:
        return [
            GitHubEvent(
                id="evt-1",
                type="PushEvent",
                repo=f"{org}/agent",
                actor="sarthib7",
                created_at="2026-05-21T00:00:00Z",
                summary="Pushed 1 commit to main: update docs",
            )
        ][:max_events]

    def fetch_commits(self, repo: GitHubRepo, *, max_commits: int) -> list[GitHubCommit]:
        return [
            GitHubCommit(
                repo=repo.full_name,
                sha="abc123def456",
                html_url=f"{repo.html_url}/commit/abc123def456",
                message="teach the archive about commits",
                authored_at="2026-05-21T00:00:00Z",
                author_name="Sarthi Borkar",
                author_login="sarthib7",
            )
        ][:max_commits]

    def fetch_pull_requests(
        self,
        repo: GitHubRepo,
        *,
        max_pull_requests: int,
    ) -> list[GitHubPullRequest]:
        return []


class RecordingGitHubOrgClient(GitHubOrgClient):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _get_json(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.requests.append((path, params))
        return []


def test_github_org_client_requests_all_org_repositories() -> None:
    client = RecordingGitHubOrgClient()

    client.fetch_repos("masumi-network", max_repos=100)

    assert client.requests[0] == (
        "/orgs/masumi-network/repos",
        {
            "type": "all",
            "sort": "pushed",
            "direction": "desc",
            "per_page": 100,
            "page": 1,
        },
    )


def test_github_org_client_can_request_public_repositories_only() -> None:
    client = RecordingGitHubOrgClient()

    client.fetch_repos("masumi-network", max_repos=100, include_private=False)

    assert client.requests[0][1]["type"] == "public"


def test_github_org_client_requests_pull_requests_by_recent_updates() -> None:
    client = RecordingGitHubOrgClient()
    repo = GitHubRepo(
        name="agent",
        full_name="masumi-network/agent",
        html_url="https://github.com/masumi-network/agent",
        description=None,
        language=None,
        pushed_at=None,
        updated_at=None,
        default_branch="main",
        visibility="public",
        archived=False,
        stargazers_count=0,
        forks_count=0,
        open_issues_count=0,
        topics=(),
        license_name=None,
    )

    client.fetch_pull_requests(repo, max_pull_requests=5)

    assert client.requests[0] == (
        "/repos/masumi-network/agent/pulls",
        {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": 5,
        },
    )


@pytest.mark.asyncio
async def test_github_sync_ingests_daily_digest_and_persists_state(tmp_path: Any) -> None:
    config = CitadelConfig(
        github_sync_dataset="masumi-network",
        github_sync_session="masumi-github-daily",
        github_sync_state_path=str(tmp_path / "github_state.json"),
        github_sync_run_improve=True,
    )
    citadel = FakeCitadel(config)
    syncer = GitHubOrgSyncer(citadel, client=FakeGitHubClient(), org="masumi-network")

    result = await syncer.run()
    status = await syncer.status()

    assert result["repos_scanned"] == 1
    assert result["changed_count"] == 1
    assert result["event_count"] == 1
    assert result["commit_count"] == 1
    assert result["ingested"] is True
    assert result["improved"] is True
    assert "masumi-network/agent" in citadel.ingest_calls[0]["data"]
    assert "teach the archive about commits" in citadel.ingest_calls[0]["data"]
    assert citadel.ingest_calls[0]["dataset"] == "masumi-network"
    assert citadel.improve_calls[0]["session_ids"] == ["masumi-github-daily"]
    state = json.loads(Path(config.github_sync_state_path).read_text(encoding="utf-8"))
    assert "teach the archive about commits" in state["last_digest"]
    assert status["tracked_repositories"] == 1
    assert status["seen_events"] == 1
    assert status["tracked_commit_repositories"] == 1


@pytest.mark.asyncio
async def test_github_sync_persists_digest_when_improve_fails(tmp_path: Any) -> None:
    config = CitadelConfig(
        github_sync_dataset="masumi-network",
        github_sync_session="masumi-github-daily",
        github_sync_state_path=str(tmp_path / "github_state.json"),
        github_sync_run_improve=True,
    )
    citadel = FailingImproveCitadel(config)
    syncer = GitHubOrgSyncer(citadel, client=FakeGitHubClient(), org="masumi-network")

    result = await syncer.run()
    status = await syncer.status()

    assert result["ingested"] is True
    assert result["improved"] is False
    assert result["improve_error"] == "llm unavailable"
    assert status["tracked_repositories"] == 1
    assert status["seen_events"] == 1
    assert citadel.improve_calls[0]["session_ids"] == ["masumi-github-daily"]


@pytest.mark.asyncio
async def test_github_sync_can_skip_unchanged_ingest(tmp_path: Any) -> None:
    config = CitadelConfig(
        github_sync_state_path=str(tmp_path / "github_state.json"),
        github_sync_ingest_unchanged=False,
    )
    citadel = FakeCitadel(config)
    syncer = GitHubOrgSyncer(
        citadel,
        client=FakeGitHubClient(),
        org="masumi-network",
        ingest_unchanged=False,
    )

    await syncer.run()
    second = await syncer.run()

    assert second["changed_count"] == 0
    assert second["event_count"] == 0
    assert second["commit_count"] == 0
    assert len(citadel.ingest_calls) == 1


@pytest.mark.asyncio
async def test_github_sync_returns_open_and_merged_pull_requests(tmp_path: Any) -> None:
    def hours_ago(hours: int) -> str:
        # Relative to now so the PRs always fall inside the reporting window
        # (hardcoded absolute dates silently age out and break this test).
        return (
            (datetime.now(UTC) - timedelta(hours=hours))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    class PullRequestGitHubClient(FakeGitHubClient):
        def fetch_pull_requests(
            self,
            repo: GitHubRepo,
            *,
            max_pull_requests: int,
        ) -> list[GitHubPullRequest]:
            return [
                GitHubPullRequest(
                    repo=repo.full_name,
                    number=42,
                    title="Ship organization digest",
                    html_url=f"{repo.html_url}/pull/42",
                    state="open",
                    draft=False,
                    user_login="sarthib7",
                    created_at=hours_ago(26),
                    updated_at=hours_ago(1),
                    merged_at=None,
                ),
                GitHubPullRequest(
                    repo=repo.full_name,
                    number=41,
                    title="Add source packet",
                    html_url=f"{repo.html_url}/pull/41",
                    state="closed",
                    draft=False,
                    user_login="sarthib7",
                    created_at=hours_ago(48),
                    updated_at=hours_ago(2),
                    merged_at=hours_ago(2),
                ),
            ][:max_pull_requests]

    config = CitadelConfig(
        github_sync_state_path=str(tmp_path / "github_state.json"),
        organization_digest_window_hours=24 * 14,
    )
    citadel = FakeCitadel(config)
    syncer = GitHubOrgSyncer(
        citadel,
        client=PullRequestGitHubClient(),
        org="masumi-network",
    )

    result = await syncer.run()

    assert result["open_pull_request_count"] == 1
    assert result["merged_pull_request_count"] == 1
    assert result["open_pull_requests"][0]["number"] == 42
    assert result["merged_pull_requests"][0]["number"] == 41
    assert result["active_repositories"][0]["repo"] == "masumi-network/agent"


@pytest.mark.asyncio
async def test_github_sync_filters_repositories_by_policy(tmp_path: Any) -> None:
    class MultiRepoGitHubClient(FakeGitHubClient):
        def fetch_repos(
            self,
            org: str,
            *,
            max_repos: int,
            include_private: bool = True,
        ) -> list[GitHubRepo]:
            return [
                GitHubRepo(
                    name="agent",
                    full_name=f"{org}/agent",
                    html_url=f"https://github.com/{org}/agent",
                    description=None,
                    language="Python",
                    pushed_at="2026-05-21T00:00:00Z",
                    updated_at="2026-05-21T00:00:00Z",
                    default_branch="main",
                    visibility="private",
                    archived=False,
                    stargazers_count=0,
                    forks_count=0,
                    open_issues_count=0,
                    topics=(),
                    license_name=None,
                ),
                GitHubRepo(
                    name="sandbox",
                    full_name=f"{org}/sandbox",
                    html_url=f"https://github.com/{org}/sandbox",
                    description=None,
                    language="Python",
                    pushed_at="2026-05-21T00:00:00Z",
                    updated_at="2026-05-21T00:00:00Z",
                    default_branch="main",
                    visibility="private",
                    archived=False,
                    stargazers_count=0,
                    forks_count=0,
                    open_issues_count=0,
                    topics=(),
                    license_name=None,
                ),
            ]

    config = CitadelConfig(
        github_sync_state_path=str(tmp_path / "github_state.json"),
        github_sync_repo_allowlist=("masumi-network/agent",),
        github_sync_repo_denylist=("sandbox",),
    )
    citadel = FakeCitadel(config)
    syncer = GitHubOrgSyncer(
        citadel,
        client=MultiRepoGitHubClient(),
        org="masumi-network",
    )

    result = await syncer.run()

    assert result["repos_scanned"] == 1
    assert result["private_repo_count"] == 1
    assert result["changed_repositories"][0]["full_name"] == "masumi-network/agent"


@pytest.mark.asyncio
async def test_github_sync_security_scan_blocks_secret_metadata(tmp_path: Any) -> None:
    class SecretCommitGitHubClient(FakeGitHubClient):
        def fetch_commits(self, repo: GitHubRepo, *, max_commits: int) -> list[GitHubCommit]:
            return [
                GitHubCommit(
                    repo=repo.full_name,
                    sha="abc123def456",
                    html_url=f"{repo.html_url}/commit/abc123def456",
                    message="rotate password=not-a-real-secret-value",
                    authored_at="2026-05-21T00:00:00Z",
                    author_name="Sarthi Borkar",
                    author_login="sarthib7",
                )
            ]

    config = CitadelConfig(github_sync_state_path=str(tmp_path / "github_state.json"))
    citadel = FakeCitadel(config)
    syncer = GitHubOrgSyncer(
        citadel,
        client=SecretCommitGitHubClient(),
        org="masumi-network",
    )

    result = await syncer.run()
    serialized_findings = json.dumps(result["security_scan"]["findings"])

    assert result["ingested"] is False
    assert result["ingest_reason"] == "blocked_by_security_scan"
    assert result["security_scan"]["blocked"] is True
    assert result["security_scan"]["highest_severity"] == "high"
    assert citadel.ingest_calls == []
    assert "not-a-real-secret-value" not in serialized_findings


@pytest.mark.asyncio
async def test_github_sync_reports_authenticated_flag(tmp_path: Any) -> None:
    config = CitadelConfig(
        github_sync_dataset="masumi-network",
        github_sync_session="masumi-github-daily",
        github_sync_state_path=str(tmp_path / "github_state.json"),
    )
    client = FakeGitHubClient()
    syncer = GitHubOrgSyncer(FakeCitadel(config), client=client, org="masumi-network")

    result = await syncer.run()
    assert result["authenticated"] is False
    assert (await syncer.status())["authenticated"] is False

    client.token = "ghp_example"  # type: ignore[attr-defined]
    assert (await syncer.status())["authenticated"] is True


def test_a_404_probe_is_not_logged_as_an_error(monkeypatch, caplog) -> None:
    """repo_content_sync asks every repo for optional paths; most 404.

    Those probes produced several hundred ERROR lines an hour in production and
    buried every real failure. A 404 is the expected answer here, so it belongs
    at DEBUG; anything else stays ERROR.
    """
    import logging
    from urllib.error import HTTPError

    import kb.github_sync as gh

    def boom(*_args, **_kwargs):
        raise HTTPError("https://api.github.com/x", 404, "Not Found", {}, io.BytesIO(b"{}"))

    client = gh.GitHubOrgClient(token=None)
    monkeypatch.setattr(gh, "run_with_retries", lambda fetch, operation="": fetch())
    monkeypatch.setattr(gh, "open_secure", boom, raising=False)
    monkeypatch.setattr(gh, "urlopen", boom, raising=False)

    with caplog.at_level(logging.DEBUG, logger=gh.logger.name):
        with pytest.raises(gh.GitHubAPIError) as caught:
            client._get_json("/repos/o/r/contents/SKILL.md", {})

    assert caught.value.status == 404
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"a 404 probe must not log at ERROR, got: {[r.message for r in errors]}"
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_a_real_failure_is_still_logged_as_an_error(monkeypatch, caplog) -> None:
    import logging
    from urllib.error import HTTPError

    import kb.github_sync as gh

    def boom(*_args, **_kwargs):
        raise HTTPError("https://api.github.com/x", 500, "Server Error", {}, io.BytesIO(b"{}"))

    client = gh.GitHubOrgClient(token=None)
    monkeypatch.setattr(gh, "run_with_retries", lambda fetch, operation="": fetch())
    monkeypatch.setattr(gh, "open_secure", boom, raising=False)
    monkeypatch.setattr(gh, "urlopen", boom, raising=False)

    with caplog.at_level(logging.DEBUG, logger=gh.logger.name):
        with pytest.raises(gh.GitHubAPIError) as caught:
            client._get_json("/repos/o/r/contents/SKILL.md", {})

    assert caught.value.status == 500
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- #148: corrupt state must fail loudly, not flatten to "first run" --------


@pytest.mark.asyncio
async def test_corrupt_state_raises_instead_of_everything_changed(tmp_path: Any) -> None:
    """#148: a restart mid-write truncates the state file; the next run must
    refuse it. Flattening to empty made every repo "changed" and every event
    "new", ingested a false digest, and then saved over the evidence."""
    from kb.state_io import StateFileError

    config = CitadelConfig(github_sync_state_path=str(tmp_path / "github_state.json"))
    citadel = FakeCitadel(config)
    syncer = GitHubOrgSyncer(citadel, client=FakeGitHubClient(), org="masumi-network")
    await syncer.run()  # seed a real state file
    corrupt = '{"version": 1, "org": "masumi-network", "repos"'
    Path(config.github_sync_state_path).write_text(corrupt, encoding="utf-8")

    with pytest.raises(StateFileError):
        await syncer.run()

    # No digest was fabricated from the corrupt state, and the file was left
    # untouched as evidence.
    assert len(citadel.ingest_calls) == 1
    assert Path(config.github_sync_state_path).read_text(encoding="utf-8") == corrupt


@pytest.mark.asyncio
async def test_status_reports_corrupt_state_instead_of_raising(tmp_path: Any) -> None:
    """status() must catch the corruption so /api/sources shows red, not 500."""
    config = CitadelConfig(github_sync_state_path=str(tmp_path / "github_state.json"))
    syncer = GitHubOrgSyncer(FakeCitadel(config), client=FakeGitHubClient(), org="masumi-network")
    Path(config.github_sync_state_path).write_text("not json", encoding="utf-8")

    status = await syncer.status()

    assert status["ok"] is False
    assert "github_state.json" in status["state_error"]
    assert status["tracked_repositories"] == 0
