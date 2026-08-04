"""GitHub organization sync: fetch raw activity, track seen state, and feed the
Repository Daily Update composer plus the Learning Process.

Update quality rules (meaningful-change filtering and digest formatting) live
in :mod:`kb.repository_update`. Ingest/improve orchestration lives in
:mod:`kb.learning`. This module owns the GitHub HTTP adapter, repo policy
filters, the security scan gate, and persistent sync state.
"""

from __future__ import annotations

import argparse
import asyncio
from fnmatch import fnmatchcase
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request

from kb.secure_http import open_secure

from kb.learning import LearningProcess
from kb.models import INDEX_STATE_UNKNOWN
from kb.repository_update import (
    SOURCE_URL_TEMPLATE,
    GitHubCommit,
    GitHubEvent,
    GitHubPullRequest,
    GitHubRepo,
    compose_repository_update,
    filter_changed_repos,
    format_digest,
)
from kb.retry import run_with_retries
from kb.security_scan import SecurityScanEntry, redact_secrets, scan_text_entries
from kb.service import Citadel
from kb.state_io import StateFileError, load_state_file

__all__ = [
    "GitHubAPIError",
    "GitHubCommit",
    "GitHubEvent",
    "GitHubOrgClient",
    "GitHubOrgSyncer",
    "GitHubPullRequest",
    "GitHubRepo",
    "SOURCE_URL_TEMPLATE",
    "format_digest",
]

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
STATE_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    candidates = {name, name.split("/")[-1]}
    return any(
        fnmatchcase(candidate, pattern)
        for pattern in patterns
        for candidate in candidates
    )


class GitHubAPIError(RuntimeError):
    """A GitHub API call failed. ``status`` is the HTTP code, or None if it never got one.

    Callers need the code, not the message: repo_content_sync probes optional
    paths and a 404 is its expected "not present" answer. It used to test
    ``"404" in str(exc)``, which also matches a 404 appearing anywhere in a repo
    name or an error body.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubOrgClient:
    def __init__(self, *, token: str | None = None, timeout: float = 20.0) -> None:
        self.token = token
        self.timeout = timeout

    def fetch_repos(
        self,
        org: str,
        *,
        max_repos: int,
        include_private: bool = True,
    ) -> list[GitHubRepo]:
        repos: list[GitHubRepo] = []
        per_page = min(max(max_repos, 1), 100)
        page = 1
        while len(repos) < max_repos:
            data = self._get_json(
                f"/orgs/{org}/repos",
                {
                    "type": "all" if include_private else "public",
                    "sort": "pushed",
                    "direction": "desc",
                    "per_page": per_page,
                    "page": page,
                },
            )
            if not isinstance(data, list):
                raise GitHubAPIError("GitHub returned an unexpected repositories payload.")
            repos.extend(GitHubRepo.from_api(item) for item in data)
            if len(data) < per_page:
                break
            page += 1
        return repos[:max_repos]

    def fetch_events(self, org: str, *, max_events: int) -> list[GitHubEvent]:
        events: list[GitHubEvent] = []
        per_page = min(max(max_events, 1), 100)
        page = 1
        while len(events) < max_events:
            data = self._get_json(
                f"/orgs/{org}/events",
                {
                    "per_page": per_page,
                    "page": page,
                },
            )
            if not isinstance(data, list):
                raise GitHubAPIError("GitHub returned an unexpected events payload.")
            events.extend(GitHubEvent.from_api(item) for item in data)
            if len(data) < per_page:
                break
            page += 1
        return events[:max_events]

    def fetch_commits(self, repo: GitHubRepo, *, max_commits: int) -> list[GitHubCommit]:
        if max_commits <= 0:
            return []
        params: dict[str, Any] = {
            "per_page": min(max(max_commits, 1), 100),
        }
        if repo.default_branch:
            params["sha"] = repo.default_branch
        data = self._get_json(f"/repos/{quote(repo.full_name, safe='/')}/commits", params)
        if not isinstance(data, list):
            raise GitHubAPIError("GitHub returned an unexpected commits payload.")
        return [GitHubCommit.from_api(item, repo=repo.full_name) for item in data[:max_commits]]

    def fetch_pull_requests(
        self,
        repo: GitHubRepo,
        *,
        max_pull_requests: int,
    ) -> list[GitHubPullRequest]:
        if max_pull_requests <= 0:
            return []
        data = self._get_json(
            f"/repos/{quote(repo.full_name, safe='/')}/pulls",
            {
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": min(max(max_pull_requests, 1), 100),
            },
        )
        if not isinstance(data, list):
            raise GitHubAPIError("GitHub returned an unexpected pull requests payload.")
        return [
            GitHubPullRequest.from_api(item, repo=repo.full_name)
            for item in data[:max_pull_requests]
        ]

    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        query = urlencode(params)
        request = Request(
            f"{GITHUB_API}{path}?{query}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "citadel-archive-github-sync",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )

        def fetch() -> Any:
            with open_secure(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            return run_with_retries(fetch, operation=f"github_sync.get {path}")
        except HTTPError as exc:
            detail = redact_secrets(exc.read().decode("utf-8", errors="replace")[:300], self.token)
            # A 404 is the expected answer when probing for an optional path:
            # repo_content_sync asks every repo for AGENTS.md, SKILL.md,
            # CONTEXT.md, skills/, docs/, content/docs/ and plugins/, and most
            # repos have none of them. Logging those at ERROR produced several
            # hundred lines an hour and buried every real failure.
            log = logger.debug if exc.code == 404 else logger.error
            log("GitHub API request %s failed: HTTPError %s", path, exc.code)
            raise GitHubAPIError(
                f"GitHub API returned {exc.code}: {detail}", status=exc.code
            ) from exc
        except URLError as exc:
            logger.error(
                "GitHub API request %s failed: %s: %s",
                path,
                exc.__class__.__name__,
                redact_secrets(str(exc.reason), self.token),
            )
            raise GitHubAPIError(f"Could not reach GitHub API: {exc.reason}") from exc


class GitHubOrgSyncer:
    def __init__(
        self,
        citadel: Citadel,
        *,
        org: str | None = None,
        client: GitHubOrgClient | None = None,
        state_path: str | Path | None = None,
        max_repos: int | None = None,
        max_events: int | None = None,
        max_commits_per_repo: int | None = None,
        max_pull_requests_per_repo: int | None = None,
        include_commits: bool | None = None,
        ingest_unchanged: bool | None = None,
        run_improve: bool | None = None,
        learning: LearningProcess | None = None,
    ) -> None:
        self.citadel = citadel
        self.config = citadel.config
        self.org = org or self.config.github_org
        self.client = client or GitHubOrgClient(token=self.config.github_token)
        self.learning = learning or LearningProcess(citadel)
        self.state_path = Path(state_path or self.config.github_sync_state_path)
        self.max_repos = max_repos or self.config.github_sync_max_repos
        self.max_events = max_events or self.config.github_sync_max_events
        self.max_commits_per_repo = (
            self.config.github_sync_max_commits_per_repo
            if max_commits_per_repo is None
            else max_commits_per_repo
        )
        self.max_pull_requests_per_repo = (
            self.config.github_sync_max_pull_requests_per_repo
            if max_pull_requests_per_repo is None
            else max_pull_requests_per_repo
        )
        self.include_commits = (
            self.config.github_sync_include_commits if include_commits is None else include_commits
        )
        self.ingest_unchanged = (
            self.config.github_sync_ingest_unchanged
            if ingest_unchanged is None
            else ingest_unchanged
        )
        self.run_improve = self.config.github_sync_run_improve if run_improve is None else run_improve
        self.include_private = self.config.github_sync_include_private
        self.repo_allowlist = self.config.github_sync_repo_allowlist
        self.repo_denylist = self.config.github_sync_repo_denylist
        self.security_scan_enabled = self.config.github_sync_security_scan_enabled
        self.security_block_severity = self.config.github_sync_security_block_severity

    @classmethod
    def from_env(cls) -> "GitHubOrgSyncer":
        return cls(Citadel.from_env())

    async def status(self) -> dict[str, Any]:
        # A corrupt state file must show as a red source, not a 500 (#148).
        state_error: str | None = None
        try:
            state = self._load_state()
        except StateFileError as exc:
            state = {}
            state_error = str(exc)
        return {
            "ok": state_error is None,
            "state_error": state_error,
            "org": self.org,
            "source_url": SOURCE_URL_TEMPLATE.format(org=self.org),
            "dataset": self.config.github_sync_dataset,
            "session_id": self.config.github_sync_session,
            "state_path": str(self.state_path),
            "last_checked_at": state.get("last_checked_at"),
            "last_digest_at": state.get("last_digest_at"),
            "tracked_repositories": len(state.get("repos") or {}),
            "seen_events": len(state.get("seen_event_ids") or []),
            "tracked_commit_repositories": len(state.get("commits") or {}),
            "authenticated": bool(getattr(self.client, "token", None)),
            "include_commits": self.include_commits,
            "include_private": self.include_private,
            "repo_allowlist": list(self.repo_allowlist),
            "repo_denylist": list(self.repo_denylist),
            "max_commits_per_repo": self.max_commits_per_repo,
            "max_pull_requests_per_repo": self.max_pull_requests_per_repo,
            "run_improve": self.run_improve,
            "ingest_unchanged": self.ingest_unchanged,
            "security_scan_enabled": self.security_scan_enabled,
            "security_block_severity": self.security_block_severity,
            "last_security_scan": state.get("last_security_scan"),
        }

    async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        checked_at = utc_now()
        authenticated = bool(getattr(self.client, "token", None))
        if not authenticated:
            logger.warning(
                "GitHub sync for org %s is running UNAUTHENTICATED (no GITHUB_TOKEN); GitHub "
                "throttles anonymous requests to 60/hr and will 403 across an org-wide sync.",
                self.org,
            )
        logger.info(
            "GitHub sync starting for org %s (force=%s, dry_run=%s)", self.org, force, dry_run
        )
        state = self._load_state()
        repos, events = await asyncio.to_thread(self._fetch_activity)
        previous_repos = state.get("repos") or {}
        previous_event_ids = set(state.get("seen_event_ids") or [])
        changed_repos = filter_changed_repos(repos, previous_repos, force=force)
        commit_candidates = repos if force else changed_repos
        commits_by_repo = await asyncio.to_thread(self._fetch_commits, commit_candidates)
        pull_requests_by_repo = await asyncio.to_thread(self._fetch_pull_requests, repos)
        previous_commits = state.get("commits") or {}
        if not isinstance(previous_commits, dict):
            previous_commits = {}
        seen_commits_by_repo = {
            repo_name: set(shas or [])
            for repo_name, shas in previous_commits.items()
            if isinstance(shas, list)
        }

        update = compose_repository_update(
            org=self.org,
            checked_at=checked_at,
            repos=repos,
            events=events,
            commits_by_repo=commits_by_repo,
            pull_requests_by_repo=pull_requests_by_repo,
            previous_repos=previous_repos,
            previous_event_ids=previous_event_ids,
            seen_commits_by_repo=seen_commits_by_repo,
            window_hours=self.config.organization_digest_window_hours,
            force=force,
            max_commits_per_repo=self.max_commits_per_repo if self.include_commits else None,
        )

        should_ingest = force or self.ingest_unchanged or update.meaningful
        security_scan = self._scan_activity(
            repos=repos,
            events=update.new_events,
            commits=update.new_commits,
            pull_requests=update.recent_pull_requests,
        )
        security_blocked = bool(security_scan.get("blocked"))
        if security_blocked:
            logger.warning(
                "GitHub sync ingest blocked by security scan: %s finding(s), highest severity %s",
                security_scan.get("finding_count"),
                security_scan.get("highest_severity"),
            )
        should_ingest = should_ingest and not security_blocked

        ingest_result = None
        improve_result = None
        if should_ingest and not dry_run:
            outcome = await self.learning.learn(
                update.digest,
                dataset=self.config.github_sync_dataset,
                session_id=self.config.github_sync_session,
                tags=["github", self.org, "daily-sync", "repository-activity"],
                run_improve=self.run_improve,
                detect_conflicts=False,
            )
            ingest_result = outcome.ingest
            improve_result = outcome.improve

        if not dry_run:
            tracked_commits = dict(previous_commits)
            for repo_name, repo_commits in commits_by_repo.items():
                tracked_commits[repo_name] = [commit.sha for commit in repo_commits if commit.sha][:500]
            state.update(
                {
                    "version": STATE_VERSION,
                    "org": self.org,
                    "last_checked_at": checked_at,
                    "repos": {repo.full_name: repo.state() for repo in repos},
                    "commits": tracked_commits,
                    "seen_event_ids": [
                        event.id for event in events if event.id
                    ][:500],
                }
            )
            if should_ingest:
                state["last_digest_at"] = checked_at
                state["last_digest"] = update.digest
                # What was OBSERVED about the graph write, plus the ids cognee
                # assigned. ``last_digest_at`` only ever recorded that a write
                # was requested; with the digest reported as ingested the
                # moment cognee.add() returned, a pass whose cognify silently
                # never ran left a state file identical to a pass that indexed
                # everything. Persisting the ids is what lets a later pass (or
                # an operator) check this claim against the index instead of
                # taking the connector's word for it — the same repair
                # repo_content_sync already made for its skip decision.
                state["last_digest_index"] = {
                    "checked_at": checked_at,
                    "index_state": getattr(ingest_result, "index_state", INDEX_STATE_UNKNOWN),
                    "cognee_data_ids": list(getattr(ingest_result, "cognee_data_ids", ()) or ()),
                }
            state["last_security_scan"] = {
                "checked_at": checked_at,
                "ok": security_scan.get("ok"),
                "blocked": security_scan.get("blocked"),
                "highest_severity": security_scan.get("highest_severity"),
                "finding_count": security_scan.get("finding_count"),
            }
            self._save_state(state)

        # `ingested` reports that the store ACCEPTED the digest (the add). The
        # graph write that makes it searchable is asynchronous, so this run can
        # only observe the state that write reached — never assume it finished.
        indexing_state = (
            ingest_result.index_state if ingest_result is not None else INDEX_STATE_UNKNOWN
        )
        logger.info(
            "GitHub sync finished for org %s: %d repos scanned, %d changed, %d events, "
            "%d commits, ingested=%s index_state=%s",
            self.org,
            len(repos),
            len(update.changed_repos),
            len(update.new_events),
            len(update.new_commits),
            bool(ingest_result and ingest_result.accepted),
            indexing_state,
        )
        return {
            "ok": True,
            "authenticated": authenticated,
            "org": self.org,
            "source_url": SOURCE_URL_TEMPLATE.format(org=self.org),
            "checked_at": checked_at,
            "state_path": str(self.state_path),
            "repos_scanned": len(repos),
            "private_repo_count": len([repo for repo in repos if repo.visibility == "private"]),
            "contains_private_repositories": any(repo.visibility == "private" for repo in repos),
            "window_started_at": update.window_started_at_iso,
            "changed_count": len(update.changed_repos),
            "event_count": len(update.new_events),
            "commit_count": len(update.new_commits),
            "open_pull_request_count": len(update.open_pull_requests),
            "merged_pull_request_count": len(update.merged_pull_requests),
            "changed_repositories": [repo.summary() for repo in update.changed_repos[:20]],
            "recent_commits": [commit.summary_dict() for commit in update.new_commits[:40]],
            "open_pull_requests": [
                pull_request.summary_dict() for pull_request in update.open_pull_requests[:40]
            ],
            "merged_pull_requests": [
                pull_request.summary_dict() for pull_request in update.merged_pull_requests[:40]
            ],
            "active_repositories": update.active_repositories[:20],
            "recent_events": [event.summary_dict() for event in update.new_events[:20]],
            # `ingested` describes the REQUEST: it is True the instant
            # cognee.add() returned. `indexed` describes the OUTCOME, and is
            # deliberately tri-state — null while the graph write is still owed,
            # false when nothing will ever perform it. Reading `ingested` as
            # "retrievable" is the mistake this pair exists to prevent, so the
            # two are reported side by side rather than one replacing the other.
            "ingested": bool(ingest_result and ingest_result.accepted),
            "index_state": indexing_state,
            "indexed": (ingest_result.indexed if ingest_result is not None else None),
            "cognee_data_ids": list(
                getattr(ingest_result, "cognee_data_ids", ()) or ()
            ),
            "ingest_reason": (
                "blocked_by_security_scan"
                if security_blocked
                else getattr(ingest_result, "reason", None)
            ),
            "improved": bool(improve_result)
            and not (isinstance(improve_result, dict) and improve_result.get("ok") is False),
            "improve_error": improve_result.get("error")
            if isinstance(improve_result, dict) and improve_result.get("ok") is False
            else None,
            "dry_run": dry_run,
            "digest": update.digest if dry_run else None,
            "security_scan": security_scan,
        }

    def _fetch_activity(self) -> tuple[list[GitHubRepo], list[GitHubEvent]]:
        repos = self.client.fetch_repos(
            self.org,
            max_repos=self.max_repos,
            include_private=self.include_private,
        )
        repos = [repo for repo in repos if self._repo_allowed(repo)]
        events = self.client.fetch_events(self.org, max_events=self.max_events)
        return repos, events

    def _repo_allowed(self, repo: GitHubRepo) -> bool:
        name = repo.full_name or repo.name
        if not self.include_private and repo.visibility == "private":
            return False
        if self.repo_allowlist and not _matches_any(name, self.repo_allowlist):
            return False
        if self.repo_denylist and _matches_any(name, self.repo_denylist):
            return False
        return True

    def _fetch_commits(self, repos: list[GitHubRepo]) -> dict[str, list[GitHubCommit]]:
        if not self.include_commits or self.max_commits_per_repo <= 0:
            return {}
        commits: dict[str, list[GitHubCommit]] = {}
        for repo in repos:
            if not repo.full_name or repo.archived:
                continue
            commits[repo.full_name] = self.client.fetch_commits(
                repo,
                max_commits=self.max_commits_per_repo,
            )
        return commits

    def _fetch_pull_requests(self, repos: list[GitHubRepo]) -> dict[str, list[GitHubPullRequest]]:
        if self.max_pull_requests_per_repo <= 0:
            return {}
        pull_requests: dict[str, list[GitHubPullRequest]] = {}
        for repo in repos:
            if not repo.full_name or repo.archived:
                continue
            pull_requests[repo.full_name] = self.client.fetch_pull_requests(
                repo,
                max_pull_requests=self.max_pull_requests_per_repo,
            )
        return pull_requests

    def _scan_activity(
        self,
        *,
        repos: list[GitHubRepo],
        events: list[GitHubEvent],
        commits: list[GitHubCommit],
        pull_requests: list[GitHubPullRequest],
    ) -> dict[str, object]:
        if not self.security_scan_enabled:
            return {
                "ok": True,
                "blocked": False,
                "block_severity": self.security_block_severity,
                "highest_severity": None,
                "finding_count": 0,
                "findings": [],
                "enabled": False,
            }
        entries: list[SecurityScanEntry] = []
        for repo in repos:
            entries.append(
                SecurityScanEntry(
                    source="repository",
                    location=repo.full_name,
                    text=" ".join(
                        part
                        for part in (
                            repo.name,
                            repo.full_name,
                            repo.description or "",
                            " ".join(repo.topics),
                            repo.html_url,
                        )
                        if part
                    ),
                )
            )
        for event in events:
            entries.append(
                SecurityScanEntry(
                    source="event",
                    location=f"{event.repo}:{event.id}",
                    text=f"{event.type} {event.actor} {event.summary}",
                )
            )
        for commit in commits:
            entries.append(
                SecurityScanEntry(
                    source="commit",
                    location=f"{commit.repo}@{commit.sha[:12]}",
                    text=" ".join(
                        part
                        for part in (
                            commit.message,
                            commit.html_url,
                            commit.author_login or "",
                            commit.author_name or "",
                        )
                        if part
                    ),
                )
            )
        for pull_request in pull_requests:
            entries.append(
                SecurityScanEntry(
                    source="pull_request",
                    location=f"{pull_request.repo}#{pull_request.number}",
                    text=" ".join(
                        part
                        for part in (
                            pull_request.title,
                            pull_request.html_url,
                            pull_request.user_login or "",
                        )
                        if part
                    ),
                )
            )
        result = scan_text_entries(entries, block_severity=self.security_block_severity)
        result["enabled"] = True
        return result

    def _load_state(self) -> dict[str, Any]:
        # Absent file = genuine first run. A file that exists but cannot be
        # parsed raises StateFileError instead of flattening to this default:
        # an empty state would mark every repo changed and every event new,
        # then ingest and post a false "everything changed" digest (#148).
        data = load_state_file(self.state_path)
        if data is None:
            return {
                "version": STATE_VERSION,
                "org": self.org,
                "repos": {},
                "commits": {},
                "seen_event_ids": [],
            }
        data.setdefault("repos", {})
        data.setdefault("commits", {})
        data.setdefault("seen_event_ids", [])
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True)
        temp_path.replace(self.state_path)


async def _sync_github(args: argparse.Namespace) -> None:
    syncer = GitHubOrgSyncer(
        Citadel.from_env(),
        org=args.org,
        state_path=args.state_path,
        max_repos=args.max_repos,
        max_events=args.max_events,
        max_commits_per_repo=args.max_commits_per_repo,
        include_commits=not args.skip_commits,
        ingest_unchanged=not args.skip_unchanged,
        run_improve=not args.skip_improve,
    )
    result = await syncer.run(force=args.force, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kb.github_sync")
    parser.add_argument("--org", default=None, help="GitHub organization login")
    parser.add_argument("--state-path", default=None, help="Persistent sync state JSON path")
    parser.add_argument("--max-repos", type=int, default=None, help="Maximum repositories to scan")
    parser.add_argument("--max-events", type=int, default=None, help="Maximum org events to scan")
    parser.add_argument(
        "--max-commits-per-repo",
        type=int,
        default=None,
        help="Maximum recent commits to summarize per changed repository",
    )
    parser.add_argument("--force", action="store_true", help="Treat all fetched activity as new")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print without ingesting")
    parser.add_argument("--skip-improve", action="store_true", help="Do not run Citadel improve")
    parser.add_argument("--skip-commits", action="store_true", help="Do not fetch commit summaries")
    parser.add_argument(
        "--skip-unchanged",
        action="store_true",
        help="Skip ingest when GitHub reports no new activity",
    )
    return parser


def main() -> None:
    from kb.logging_utils import configure_logging

    configure_logging()
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_sync_github(args))


if __name__ == "__main__":
    main()
