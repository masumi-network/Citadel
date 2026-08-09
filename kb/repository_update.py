"""Repository Daily Update composition.

Per CONTEXT.md, a Repository Daily Update is a source-linked summary of
meaningful changes in one repository over a day, composed from Meaningful
Source Changes (pull requests, merged work, commits, repository momentum).

This module owns the domain rules: which fetched GitHub activity counts as
meaningful (new versus already-seen, inside the reporting window) and how the
update digest is formatted. ``kb.github_sync`` stays a fetch/state adapter that
feeds raw activity into :func:`compose_repository_update`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

SOURCE_URL_TEMPLATE = "https://github.com/orgs/{org}/repositories"


def _short(value: str | None, *, length: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= length:
        return text
    return f"{text[: length - 1]}."


def _parse_github_time(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(UTC)


@dataclass(frozen=True)
class GitHubRepo:
    name: str
    full_name: str
    html_url: str
    description: str | None
    language: str | None
    pushed_at: str | None
    updated_at: str | None
    default_branch: str | None
    visibility: str | None
    archived: bool
    stargazers_count: int
    forks_count: int
    open_issues_count: int
    topics: tuple[str, ...]
    license_name: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "GitHubRepo":
        license_data = data.get("license") or {}
        return cls(
            name=str(data.get("name") or ""),
            full_name=str(data.get("full_name") or ""),
            html_url=str(data.get("html_url") or ""),
            description=data.get("description"),
            language=data.get("language"),
            pushed_at=data.get("pushed_at"),
            updated_at=data.get("updated_at"),
            default_branch=data.get("default_branch"),
            visibility=data.get("visibility"),
            archived=bool(data.get("archived")),
            stargazers_count=int(data.get("stargazers_count") or 0),
            forks_count=int(data.get("forks_count") or 0),
            open_issues_count=int(data.get("open_issues_count") or 0),
            topics=tuple(data.get("topics") or ()),
            license_name=license_data.get("name"),
        )

    @property
    def fingerprint(self) -> str:
        parts = [
            self.pushed_at or "",
            self.updated_at or "",
            str(self.open_issues_count),
            self.default_branch or "",
            str(self.archived),
        ]
        return "|".join(parts)

    def state(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "pushed_at": self.pushed_at,
            "updated_at": self.updated_at,
            "open_issues_count": self.open_issues_count,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "full_name": self.full_name,
            "url": self.html_url,
            "description": self.description,
            "language": self.language,
            "pushed_at": self.pushed_at,
            "updated_at": self.updated_at,
            "open_issues_count": self.open_issues_count,
            "stars": self.stargazers_count,
            "forks": self.forks_count,
            "topics": list(self.topics),
            "archived": self.archived,
        }


@dataclass(frozen=True)
class GitHubEvent:
    id: str
    type: str
    repo: str
    actor: str
    created_at: str
    summary: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "GitHubEvent":
        event_type = str(data.get("type") or "Event")
        payload = data.get("payload") or {}
        repo = (data.get("repo") or {}).get("name") or "unknown/repository"
        actor = (data.get("actor") or {}).get("login") or "unknown"
        return cls(
            id=str(data.get("id") or ""),
            type=event_type,
            repo=repo,
            actor=actor,
            created_at=str(data.get("created_at") or ""),
            summary=_event_summary(event_type, payload),
        )

    def summary_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "type": self.type,
            "repo": self.repo,
            "actor": self.actor,
            "created_at": self.created_at,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class GitHubCommit:
    repo: str
    sha: str
    html_url: str
    message: str
    authored_at: str | None
    author_name: str | None
    author_login: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any], *, repo: str) -> "GitHubCommit":
        commit = data.get("commit") or {}
        author = commit.get("author") or {}
        github_author = data.get("author") or {}
        message = str(commit.get("message") or "").splitlines()[0]
        return cls(
            repo=repo,
            sha=str(data.get("sha") or ""),
            html_url=str(data.get("html_url") or ""),
            message=_short(message, length=140),
            authored_at=author.get("date"),
            author_name=author.get("name"),
            author_login=github_author.get("login"),
        )

    def summary_dict(self) -> dict[str, str | None]:
        return {
            "repo": self.repo,
            "sha": self.sha[:12],
            "url": self.html_url,
            "message": self.message,
            "authored_at": self.authored_at,
            "author": self.author_login or self.author_name,
        }


@dataclass(frozen=True)
class GitHubPullRequest:
    repo: str
    number: int
    title: str
    html_url: str
    state: str
    draft: bool
    user_login: str | None
    created_at: str | None
    updated_at: str | None
    merged_at: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any], *, repo: str) -> "GitHubPullRequest":
        user = data.get("user") or {}
        return cls(
            repo=repo,
            number=int(data.get("number") or 0),
            title=_short(data.get("title"), length=140) or "(no title)",
            html_url=str(data.get("html_url") or ""),
            state=str(data.get("state") or ""),
            draft=bool(data.get("draft")),
            user_login=user.get("login"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            merged_at=data.get("merged_at"),
        )

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_merged(self) -> bool:
        return bool(self.merged_at)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "url": self.html_url,
            "state": self.state,
            "draft": self.draft,
            "author": self.user_login,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "merged_at": self.merged_at,
        }


@dataclass(frozen=True)
class RepositoryDailyUpdate:
    """One composed Repository Daily Update: meaningful changes plus digest."""

    org: str
    checked_at: str
    window_started_at: datetime
    changed_repos: list[GitHubRepo] = field(default_factory=list)
    new_events: list[GitHubEvent] = field(default_factory=list)
    new_commits: list[GitHubCommit] = field(default_factory=list)
    recent_pull_requests: list[GitHubPullRequest] = field(default_factory=list)
    open_pull_requests: list[GitHubPullRequest] = field(default_factory=list)
    merged_pull_requests: list[GitHubPullRequest] = field(default_factory=list)
    active_repositories: list[dict[str, Any]] = field(default_factory=list)
    digest: str = ""

    @property
    def meaningful(self) -> bool:
        """Whether the window contains any Meaningful Source Changes."""
        return bool(
            self.changed_repos or self.new_events or self.new_commits or self.recent_pull_requests
        )

    @property
    def window_started_at_iso(self) -> str:
        return self.window_started_at.isoformat(timespec="seconds").replace("+00:00", "Z")


def filter_changed_repos(
    repos: list[GitHubRepo],
    previous_repos: dict[str, dict[str, Any]],
    *,
    force: bool = False,
) -> list[GitHubRepo]:
    """Repositories whose tracked fingerprint changed since the last check."""
    return [
        repo
        for repo in repos
        if force or previous_repos.get(repo.full_name, {}).get("fingerprint") != repo.fingerprint
    ]


def pull_request_in_window(
    pull_request: GitHubPullRequest,
    window_started_at: datetime,
) -> bool:
    timestamps = [pull_request.updated_at, pull_request.created_at]
    if pull_request.is_merged:
        timestamps.insert(0, pull_request.merged_at)
    return any(
        timestamp is not None and _parse_github_time(timestamp) >= window_started_at
        for timestamp in timestamps
    )


def compose_repository_update(
    *,
    org: str,
    checked_at: str,
    repos: list[GitHubRepo],
    events: list[GitHubEvent],
    commits_by_repo: dict[str, list[GitHubCommit]],
    pull_requests_by_repo: dict[str, list[GitHubPullRequest]],
    previous_repos: dict[str, dict[str, Any]],
    previous_event_ids: set[str],
    seen_commits_by_repo: dict[str, set[str]],
    window_hours: int,
    force: bool = False,
    max_commits_per_repo: int | None = None,
) -> RepositoryDailyUpdate:
    """Apply meaningful-change filtering and format the daily update digest.

    Pure with respect to GitHub and Cognee: callers supply already-fetched
    activity plus the previously-seen state, and receive the filtered update.
    """
    window_started_at = _parse_github_time(checked_at) - timedelta(hours=max(1, window_hours))
    changed_repos = filter_changed_repos(repos, previous_repos, force=force)
    new_events = [event for event in events if force or event.id not in previous_event_ids]
    new_commits = [
        commit
        for repo_commits in commits_by_repo.values()
        for commit in repo_commits
        if commit.sha and (force or commit.sha not in seen_commits_by_repo.get(commit.repo, set()))
    ]
    recent_pull_requests = [
        pull_request
        for repo_pull_requests in pull_requests_by_repo.values()
        for pull_request in repo_pull_requests
        if pull_request_in_window(pull_request, window_started_at)
    ]
    open_pull_requests = [
        pull_request for pull_request in recent_pull_requests if pull_request.is_open
    ]
    merged_pull_requests = [
        pull_request for pull_request in recent_pull_requests if pull_request.is_merged
    ]
    active_repositories = _active_repositories(
        changed_repos=changed_repos,
        events=new_events,
        commits=new_commits,
        pull_requests=recent_pull_requests,
    )
    digest = format_digest(
        org=org,
        repos=repos,
        changed_repos=changed_repos,
        events=new_events,
        commits=new_commits,
        open_pull_requests=open_pull_requests,
        merged_pull_requests=merged_pull_requests,
        active_repositories=active_repositories,
        max_commits_per_repo=max_commits_per_repo,
    )
    return RepositoryDailyUpdate(
        org=org,
        checked_at=checked_at,
        window_started_at=window_started_at,
        changed_repos=changed_repos,
        new_events=new_events,
        new_commits=new_commits,
        recent_pull_requests=recent_pull_requests,
        open_pull_requests=open_pull_requests,
        merged_pull_requests=merged_pull_requests,
        active_repositories=active_repositories,
        digest=digest,
    )


def format_digest(
    *,
    org: str,
    repos: list[GitHubRepo],
    changed_repos: list[GitHubRepo],
    events: list[GitHubEvent],
    commits: list[GitHubCommit],
    open_pull_requests: list[GitHubPullRequest] | None = None,
    merged_pull_requests: list[GitHubPullRequest] | None = None,
    active_repositories: list[dict[str, Any]] | None = None,
    max_commits_per_repo: int | None = None,
) -> str:
    """Render one Repository Daily Update as a vault document body.

    Deliberately carries NO wall-clock fields (ADR-0016: a document rendered
    from unchanged source content must be byte-identical to the last render).
    It used to open with ``Checked at: {utc_now}`` and the derived ``Window
    started at:`` line, so a digest whose reported activity had not changed was
    still textually distinct on every sync. That defeated content-hash dedup at
    ingest and the text-keyed dedup at query time, and each pass stored another
    copy: one measured result page held five byte-identical digests apart from
    their ``Checked at:`` line. The activity itself already carries its own
    timestamps (event, commit, and PR lines), which change exactly when the
    digest's content changes; the run's wall-clock time lives in the sync state
    (``last_checked_at`` / ``last_digest_at``), not in the indexed body.
    """
    open_pull_requests = open_pull_requests or []
    merged_pull_requests = merged_pull_requests or []
    active_repositories = active_repositories or []
    source_url = SOURCE_URL_TEMPLATE.format(org=org)
    lines = [
        f"# {org} GitHub daily update",
        "",
        f"Source: {source_url}",
        f"Repositories scanned: {len(repos)}",
        f"Changed repositories since last check: {len(changed_repos)}",
        f"New public organization events: {len(events)}",
        f"New commits observed: {len(commits)}",
        f"Open pull requests active in window: {len(open_pull_requests)}",
        f"Merged pull requests in window: {len(merged_pull_requests)}",
        "",
        "## Changed repositories",
    ]

    if changed_repos:
        for repo in changed_repos[:20]:
            topics = f" Topics: {', '.join(repo.topics[:8])}." if repo.topics else ""
            description = _short(repo.description) or "No description."
            lines.append(
                "- "
                f"{repo.full_name} ({repo.language or 'unknown language'}): "
                f"pushed {repo.pushed_at or 'unknown'}, updated {repo.updated_at or 'unknown'}, "
                f"open issues {repo.open_issues_count}, stars {repo.stargazers_count}, "
                f"forks {repo.forks_count}. {description}{topics} {repo.html_url}"
            )
    else:
        lines.append("- No repository metadata changed since the last check.")

    lines.extend(["", "## Open pull requests worth attention"])
    if open_pull_requests:
        for pull_request in open_pull_requests[:20]:
            author = f" by {pull_request.user_login}" if pull_request.user_login else ""
            draft = " draft" if pull_request.draft else ""
            lines.append(
                "- "
                f"{pull_request.repo}#{pull_request.number}{author}{draft}: "
                f"{pull_request.title}. Updated {pull_request.updated_at or 'unknown'}. "
                f"{pull_request.html_url}"
            )
    else:
        lines.append("- No open pull requests were active in the window.")

    lines.extend(["", "## Merged pull requests"])
    if merged_pull_requests:
        for pull_request in merged_pull_requests[:20]:
            author = f" by {pull_request.user_login}" if pull_request.user_login else ""
            lines.append(
                "- "
                f"{pull_request.repo}#{pull_request.number}{author}: "
                f"{pull_request.title}. Merged {pull_request.merged_at or 'unknown'}. "
                f"{pull_request.html_url}"
            )
    else:
        lines.append("- No pull requests were merged in the window.")

    lines.extend(["", "## Recent public activity"])
    if events:
        for event in events[:25]:
            lines.append(
                "- "
                f"{event.created_at}: {event.actor} on {event.repo}: "
                f"{event.type} - {event.summary}"
            )
    else:
        lines.append("- No new public org events were returned by GitHub.")

    lines.extend(["", "## Recent commits"])
    if max_commits_per_repo:
        lines.append(
            f"Showing up to {max_commits_per_repo} most recent commit(s) per changed "
            "repository; repositories with more commits than this are truncated here."
        )
    if commits:
        for commit in commits[:40]:
            author = commit.author_login or commit.author_name or "unknown author"
            lines.append(
                "- "
                f"{commit.authored_at or 'unknown time'}: {author} committed "
                f"{commit.sha[:12]} to {commit.repo}: {commit.message}. {commit.html_url}"
            )
    else:
        lines.append("- No new commits were observed in changed repositories.")

    lines.extend(["", "## Most recently pushed repositories"])
    for repo in repos[:10]:
        lines.append(
            "- "
            f"{repo.full_name}: pushed {repo.pushed_at or 'unknown'}; "
            f"language {repo.language or 'unknown'}; issues {repo.open_issues_count}; "
            f"{repo.html_url}"
        )

    lines.extend(["", "## Repository momentum"])
    if active_repositories:
        for repository in active_repositories[:10]:
            lines.append(
                "- "
                f"{repository['repo']}: activity score {repository['score']} "
                f"(repos {repository['changed_repos']}, PRs {repository['pull_requests']}, "
                f"commits {repository['commits']}, events {repository['events']})"
            )
    else:
        lines.append("- No active repositories were identified from the source packet.")

    return "\n".join(lines).strip()


def _active_repositories(
    *,
    changed_repos: list[GitHubRepo],
    events: list[GitHubEvent],
    commits: list[GitHubCommit],
    pull_requests: list[GitHubPullRequest],
) -> list[dict[str, Any]]:
    activity: dict[str, dict[str, Any]] = {}

    def entry(repo: str) -> dict[str, Any]:
        return activity.setdefault(
            repo,
            {
                "repo": repo,
                "score": 0,
                "changed_repos": 0,
                "pull_requests": 0,
                "commits": 0,
                "events": 0,
            },
        )

    for repo in changed_repos:
        row = entry(repo.full_name)
        row["changed_repos"] += 1
        row["score"] += 1
    for event in events:
        row = entry(event.repo)
        row["events"] += 1
        row["score"] += 1
    for commit in commits:
        row = entry(commit.repo)
        row["commits"] += 1
        row["score"] += 2
    for pull_request in pull_requests:
        row = entry(pull_request.repo)
        row["pull_requests"] += 1
        row["score"] += 3

    return sorted(
        activity.values(),
        key=lambda row: (row["score"], row["pull_requests"], row["commits"], row["events"]),
        reverse=True,
    )


def _title_suffix(pull_request: dict[str, Any]) -> str:
    """`: <title>` when the payload actually carries one, else nothing.

    The org events feed's `pull_request` object holds only base/head/id/number/
    url, so a title is usually absent. Emitting the literal string "(no title)"
    put that phrase on every PR line in the daily digest, and the digest is
    cognified into the vault as durable knowledge. Saying less is better than
    saying something false.
    """
    title = _short(pull_request.get("title"), length=100)
    return f": {title}" if title else ""


def _event_summary(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "PushEvent":
        commits = payload.get("commits") or []
        # `/orgs/{org}/events` returns an ABBREVIATED payload: its PushEvent
        # carries only before/head/push_id/ref/repository_id, with no `size`,
        # `distinct_size` or `commits`. An earlier fix here preferred `size`
        # over `len(commits)` to avoid claiming "Pushed 0 commits", but since
        # none of those keys exist on this endpoint it always fell through to
        # len([]) == 0 and every push in the digest read "Pushed 0 commits".
        # When the count is genuinely unknown, do not invent one.
        count = payload.get("size")
        if count is None:
            count = payload.get("distinct_size")
        if count is None and commits:
            count = len(commits)
        messages = [_short(commit.get("message"), length=80) for commit in commits[:2]]
        ref = str(payload.get("ref") or "").removeprefix("refs/heads/")
        detail = "; ".join(message for message in messages if message)
        target = ref or "a branch"
        if count is None:
            head = str(payload.get("head") or "")
            summary = f"Pushed to {target}"
            if head:
                summary += f" (head {head[:12]})"
        else:
            plural = "" if count == 1 else "s"
            summary = f"Pushed {count} commit{plural} to {target}"
        return summary + (f": {detail}" if detail else "")
    if event_type == "PullRequestEvent":
        pull_request = payload.get("pull_request") or {}
        action = payload.get("action", "updated")
        if action == "closed" and pull_request.get("merged"):
            action = "merged"
        number = pull_request.get("number") or payload.get("number")
        return f"{action} pull request #{number}" + _title_suffix(pull_request)
    if event_type == "PullRequestReviewEvent":
        pull_request = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        return (
            f"{payload.get('action', 'reviewed')} review "
            f"{review.get('state', 'submitted')} on pull request "
            f"#{pull_request.get('number')}" + _title_suffix(pull_request)
        )
    if event_type == "PullRequestReviewCommentEvent":
        pull_request = payload.get("pull_request") or {}
        comment = payload.get("comment") or {}
        body = _short(comment.get("body"), length=100) or "(no comment body)"
        return (
            f"{payload.get('action', 'commented')} review comment on pull request "
            f"#{pull_request.get('number')}: {body}"
        )
    if event_type == "IssuesEvent":
        issue = payload.get("issue") or {}
        title = _short(issue.get("title"), length=100) or "(no title)"
        return f"{payload.get('action', 'updated')} issue #{issue.get('number')}: {title}"
    if event_type == "CreateEvent":
        return f"Created {payload.get('ref_type', 'ref')} {payload.get('ref') or ''}".strip()
    if event_type == "ReleaseEvent":
        release = payload.get("release") or {}
        name = _short(release.get("name"), length=100) or release.get("tag_name") or "(unnamed)"
        return f"{payload.get('action', 'updated')} release {name}"
    if event_type == "ForkEvent":
        forkee = payload.get("forkee") or {}
        return f"Forked to {forkee.get('full_name', 'a new repository')}"
    if event_type == "WatchEvent":
        return f"{payload.get('action', 'starred')} repository"
    return _short(event_type.replace("Event", " event"))
