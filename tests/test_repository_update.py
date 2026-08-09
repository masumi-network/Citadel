from __future__ import annotations

from kb.repository_update import (
    GitHubCommit,
    GitHubEvent,
    GitHubPullRequest,
    GitHubRepo,
    compose_repository_update,
    filter_changed_repos,
)


def repo(name: str = "agent", *, pushed_at: str = "2026-06-09T00:00:00Z") -> GitHubRepo:
    return GitHubRepo(
        name=name,
        full_name=f"masumi-network/{name}",
        html_url=f"https://github.com/masumi-network/{name}",
        description="Agent runtime",
        language="Python",
        pushed_at=pushed_at,
        updated_at=pushed_at,
        default_branch="main",
        visibility="public",
        archived=False,
        stargazers_count=1,
        forks_count=0,
        open_issues_count=2,
        topics=("masumi",),
        license_name=None,
    )


def commit(sha: str, *, repo_name: str = "masumi-network/agent") -> GitHubCommit:
    return GitHubCommit(
        repo=repo_name,
        sha=sha,
        html_url=f"https://github.com/{repo_name}/commit/{sha}",
        message="teach the archive about commits",
        authored_at="2026-06-09T08:00:00Z",
        author_name="Sarthi",
        author_login="sarthib7",
    )


def event(event_id: str) -> GitHubEvent:
    return GitHubEvent(
        id=event_id,
        type="PushEvent",
        repo="masumi-network/agent",
        actor="sarthib7",
        created_at="2026-06-09T08:00:00Z",
        summary="Pushed 1 commit to main",
    )


def pull_request(
    number: int,
    *,
    state: str = "open",
    updated_at: str = "2026-06-09T08:00:00Z",
    merged_at: str | None = None,
) -> GitHubPullRequest:
    return GitHubPullRequest(
        repo="masumi-network/agent",
        number=number,
        title="Ship the composer",
        html_url=f"https://github.com/masumi-network/agent/pull/{number}",
        state=state,
        draft=False,
        user_login="sarthib7",
        created_at="2026-06-08T00:00:00Z",
        updated_at=updated_at,
        merged_at=merged_at,
    )


CHECKED_AT = "2026-06-09T09:00:00Z"


def compose(**overrides: object) -> object:
    kwargs: dict = {
        "org": "masumi-network",
        "checked_at": CHECKED_AT,
        "repos": [repo()],
        "events": [event("evt-1")],
        "commits_by_repo": {"masumi-network/agent": [commit("abc123")]},
        "pull_requests_by_repo": {"masumi-network/agent": [pull_request(42)]},
        "previous_repos": {},
        "previous_event_ids": set(),
        "seen_commits_by_repo": {},
        "window_hours": 24,
        "force": False,
        "max_commits_per_repo": 5,
    }
    kwargs.update(overrides)
    return compose_repository_update(**kwargs)


def test_unchanged_fingerprints_and_seen_activity_are_filtered_out() -> None:
    tracked_repo = repo()
    update = compose(
        previous_repos={tracked_repo.full_name: tracked_repo.state()},
        previous_event_ids={"evt-1"},
        seen_commits_by_repo={"masumi-network/agent": {"abc123"}},
        pull_requests_by_repo={},
    )

    assert update.changed_repos == []
    assert update.new_events == []
    assert update.new_commits == []
    assert update.meaningful is False


def test_new_activity_is_meaningful_and_formatted_into_the_digest() -> None:
    update = compose()

    assert update.meaningful is True
    assert [r.full_name for r in update.changed_repos] == ["masumi-network/agent"]
    assert [e.id for e in update.new_events] == ["evt-1"]
    assert [c.sha for c in update.new_commits] == ["abc123"]
    assert update.open_pull_requests[0].number == 42
    assert "# masumi-network GitHub daily update" in update.digest
    assert "teach the archive about commits" in update.digest
    assert "masumi-network/agent#42" in update.digest


def test_digest_is_stable_when_the_window_content_is_unchanged() -> None:
    """Two syncs over identical activity must produce byte-identical digests.

    ``Checked at: {utc_now}`` (and the wall-clock-derived ``Window started
    at:``) used to make every sync's digest textually distinct even when the
    reported activity had not changed, which defeated content-hash dedup at
    ingest and text-keyed dedup at query time — the exact defect ADR-0016
    fixed for repo content, in this second render path. Measured before the
    fix: one result page held five byte-identical digests apart from their
    ``Checked at:`` line.
    """
    first = compose()
    second = compose(checked_at="2026-06-09T11:00:00Z")

    assert first.digest == second.digest
    assert "Checked at:" not in first.digest, "a per-sync timestamp reintroduces duplicates"
    assert "Window started at:" not in first.digest, (
        "a wall-clock-derived window bound reintroduces duplicates"
    )


def test_force_treats_all_activity_as_new() -> None:
    tracked_repo = repo()
    update = compose(
        force=True,
        previous_repos={tracked_repo.full_name: tracked_repo.state()},
        previous_event_ids={"evt-1"},
        seen_commits_by_repo={"masumi-network/agent": {"abc123"}},
    )

    assert len(update.changed_repos) == 1
    assert len(update.new_events) == 1
    assert len(update.new_commits) == 1


def test_pull_requests_outside_the_window_are_not_meaningful() -> None:
    stale = pull_request(7, updated_at="2026-06-01T00:00:00Z")
    update = compose(
        repos=[],
        events=[],
        commits_by_repo={},
        pull_requests_by_repo={"masumi-network/agent": [stale]},
    )

    assert update.recent_pull_requests == []
    assert update.meaningful is False
    assert "No open pull requests were active in the window." in update.digest


def test_merged_pull_requests_use_the_merge_timestamp_for_the_window() -> None:
    merged = pull_request(
        8,
        state="closed",
        updated_at="2026-06-01T00:00:00Z",
        merged_at="2026-06-09T08:30:00Z",
    )
    update = compose(pull_requests_by_repo={"masumi-network/agent": [merged]})

    assert [p.number for p in update.merged_pull_requests] == [8]
    assert update.open_pull_requests == []


def test_active_repositories_rank_pull_requests_above_commits_and_events() -> None:
    other_repo = repo("scout")
    update = compose(
        repos=[repo(), other_repo],
        events=[event("evt-1")],
        commits_by_repo={"masumi-network/scout": [commit("ddd", repo_name="masumi-network/scout")]},
        pull_requests_by_repo={"masumi-network/agent": [pull_request(42)]},
    )

    momentum = update.active_repositories
    assert momentum[0]["repo"] == "masumi-network/agent"
    assert momentum[0]["pull_requests"] == 1
    assert momentum[0]["score"] > momentum[1]["score"]
    assert "## Repository momentum" in update.digest


def test_filter_changed_repos_compares_fingerprints() -> None:
    current = repo(pushed_at="2026-06-09T00:00:00Z")
    moved = repo(pushed_at="2026-06-09T05:00:00Z")

    unchanged = filter_changed_repos([current], {current.full_name: current.state()})
    changed = filter_changed_repos([moved], {current.full_name: current.state()})

    assert unchanged == []
    assert changed == [moved]
