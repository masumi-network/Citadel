"""Deep repository content sync: fetch READMEs, skills, and docs from allowlisted
GitHub repositories and feed each file through the Learning Process for Cognee
cognification (entity extraction, indexing, and graph linking).

Unlike the GitHub activity digest (``kb.github_sync``), this connector ingests
product knowledge — source files, not commit summaries.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from kb.github_sync import GitHubAPIError, GitHubOrgClient, utc_now
from kb.learning import LearningProcess
from kb.security_scan import SecurityScanEntry, scan_text_entries
from kb.service import Citadel

__all__ = [
    "DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS",
    "DEFAULT_REPO_CONTENT_REPOS",
    "DEFAULT_REPO_CONTENT_ROOT_PATHS",
    "DEFAULT_REPO_CONTENT_TREE_EXTENSIONS",
    "DEFAULT_REPO_CONTENT_TREE_PREFIXES",
    "RepoContentFile",
    "RepoContentGitHubClient",
    "RepoContentSyncer",
    "format_repo_content_document",
    "resolve_repo_full_name",
]

logger = logging.getLogger(__name__)

STATE_VERSION = 1

DEFAULT_REPO_CONTENT_REPOS = (
    "sokosumi",
    "Sokosumi-MCP",
    "sokosumi-cli",
    "sokosumi-docs",
)
DEFAULT_REPO_CONTENT_ROOT_PATHS = ("README.md", "AGENTS.md", "SKILL.md", "CONTEXT.md")
DEFAULT_REPO_CONTENT_TREE_PREFIXES = (
    "skills/",
    "content/docs/",
    "docs/",
    "plugins/",
)
DEFAULT_REPO_CONTENT_TREE_EXTENSIONS = (".md", ".mdx", ".txt")
DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS = ("AGENTS.md", "CONTEXT.md", "SKILL.md")


@dataclass(frozen=True)
class RepoContentFile:
    repo: str
    path: str
    sha: str
    ref: str
    content: str
    html_url: str

    @property
    def content_hash(self) -> str:
        return sha256(self.content.encode("utf-8")).hexdigest()


def resolve_repo_full_name(name: str, org: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        return trimmed
    if "/" in trimmed:
        return trimmed
    return f"{org}/{trimmed}"


def _matches_extension(path: str, extensions: tuple[str, ...]) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(ext.lower()) for ext in extensions)


def format_repo_content_document(file: RepoContentFile, *, checked_at: str) -> str:
    return "\n".join(
        [
            f"# {file.repo}/{file.path}",
            "",
            f"Repository: {file.repo}",
            f"Source: {file.html_url}",
            f"Commit: {file.ref}",
            f"Blob: {file.sha}",
            f"Retrieved: {checked_at}",
            "",
            "---",
            "",
            file.content.strip(),
        ]
    )


class RepoContentGitHubClient(GitHubOrgClient):
    def fetch_default_branch(self, full_name: str) -> str:
        data = self._get_json(f"/repos/{quote(full_name, safe='/')}", {})
        if not isinstance(data, dict):
            raise GitHubAPIError("GitHub returned an unexpected repository payload.")
        branch = data.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise GitHubAPIError(f"Repository {full_name} has no default branch.")
        return branch

    def fetch_commit_sha(self, full_name: str, *, ref: str) -> str:
        data = self._get_json(
            f"/repos/{quote(full_name, safe='/')}/commits/{quote(ref, safe='')}",
            {},
        )
        if not isinstance(data, dict):
            raise GitHubAPIError("GitHub returned an unexpected commit payload.")
        commit_sha = data.get("sha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise GitHubAPIError(f"Could not resolve commit SHA for {full_name}@{ref}.")
        return commit_sha

    def fetch_tree(self, full_name: str, *, ref: str) -> tuple[list[str], bool] | None:
        """Every file path in the repo, in ONE request. (paths, truncated) or None.

        Replaces probing. The old discovery asked GitHub one question per root
        path and one per directory in a depth-4 walk, so a repo with none of the
        wanted files still cost ~7 requests that all 404'd — several hundred
        failed calls an hour across the org, and the bulk of the error log.

        Returns None when the tree cannot be read, so the caller can fall back to
        probing rather than silently syncing nothing.
        """
        try:
            data = self._get_json(
                f"/repos/{quote(full_name, safe='/')}/git/trees/{quote(ref, safe='')}",
                {"recursive": "1"},
            )
        except GitHubAPIError as exc:
            logger.warning(
                "Tree read failed for %s@%s (%s); falling back to path probing",
                full_name,
                ref[:12],
                exc.status or exc.__class__.__name__,
            )
            return None
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            logger.warning("Unexpected tree payload for %s; falling back to probing", full_name)
            return None
        paths = [
            str(entry["path"])
            for entry in data["tree"]
            if isinstance(entry, dict) and entry.get("type") == "blob" and entry.get("path")
        ]
        # GitHub truncates very large trees. A truncated tree is missing files,
        # so say so and let the caller decide rather than treating it as whole.
        return paths, bool(data.get("truncated"))

    def file_exists(self, full_name: str, path: str, *, ref: str) -> bool:
        try:
            data = self._get_json(
                f"/repos/{quote(full_name, safe='/')}/contents/{quote(path, safe='/')}",
                {"ref": ref},
            )
        except GitHubAPIError as exc:
            if exc.status == 404:
                return False
            raise
        return isinstance(data, dict) and data.get("type") == "file"

    def fetch_file_text(self, full_name: str, path: str, *, ref: str) -> RepoContentFile | None:
        data = self._get_json(
            f"/repos/{quote(full_name, safe='/')}/contents/{quote(path, safe='/')}",
            {"ref": ref},
        )
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        encoding = data.get("encoding")
        raw_content = data.get("content")
        if encoding != "base64" or not isinstance(raw_content, str):
            logger.warning("Skipping %s/%s: unsupported GitHub content encoding", full_name, path)
            return None
        try:
            decoded = base64.b64decode(raw_content, validate=False).decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Skipping %s/%s: not valid UTF-8 text", full_name, path)
            return None
        sha = str(data.get("sha") or "")
        html_url = str(data.get("html_url") or f"https://github.com/{full_name}/blob/{ref}/{path}")
        return RepoContentFile(
            repo=full_name,
            path=path,
            sha=sha,
            ref=ref,
            content=decoded,
            html_url=html_url,
        )

    def list_directory(self, full_name: str, path: str, *, ref: str) -> list[dict[str, Any]]:
        data = self._get_json(
            f"/repos/{quote(full_name, safe='/')}/contents/{quote(path, safe='/')}",
            {"ref": ref},
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []


def _in_probe_order(paths: list[str], root: str) -> list[str]:
    """The paths under ``root``, ordered the way the directory walk reached them.

    GitHub returns a recursive tree depth-first, so a file three levels down
    arrives before a sibling of its top directory. The walk this replaces was
    breadth-first: every file directly under the prefix, then the next level.
    Both orders hold the same files, so the difference stays invisible until
    ``max_files`` binds, at which point each path keeps a different subset and
    the vault quietly starts holding different content.

    Sorts on components rather than the raw string because '-' sorts before
    '/': a directory ``a`` and a file ``a-b.md`` come out in the opposite order
    otherwise, which is not what listing the directory returned.
    """
    marker = f"{root}/"
    under = [path for path in paths if path.startswith(marker)]
    return sorted(under, key=lambda path: (path[len(marker) :].count("/"), path.split("/")))


def _select_from_tree(
    paths: list[str],
    *,
    root_paths: tuple[str, ...],
    tree_prefixes: tuple[str, ...],
    tree_extensions: tuple[str, ...],
    max_files: int,
    max_depth: int,
) -> list[str]:
    """Pick the wanted files out of a full repo tree, with zero further requests.

    Selection order matches the probing walk this replaces: every root path that
    exists, in configured order, then each tree prefix in order, and within a
    prefix breadth-first by depth. That last part has to be restored explicitly
    (see ``_in_probe_order``) because the tree arrives depth-first.
    """
    present = set(paths)
    selected: list[str] = []
    seen: set[str] = set()

    for path in root_paths:
        normalized = path.strip().lstrip("/")
        if not normalized or normalized in seen:
            continue
        if normalized in present:
            selected.append(normalized)
            seen.add(normalized)
        if len(selected) >= max_files:
            return selected

    for prefix in tree_prefixes:
        if len(selected) >= max_files:
            break
        root = prefix.strip().strip("/")
        if not root:
            continue
        for entry_path in _in_probe_order(paths, root):
            if len(selected) >= max_files:
                return selected
            if entry_path in seen:
                continue
            # The probing walk stopped at max_depth levels below the prefix.
            if entry_path[len(root) + 1 :].count("/") + 1 > max_depth:
                continue
            if _matches_extension(entry_path, tree_extensions):
                selected.append(entry_path)
                seen.add(entry_path)
    return selected


def discover_repo_paths(
    client: RepoContentGitHubClient,
    full_name: str,
    *,
    ref: str,
    root_paths: tuple[str, ...],
    tree_prefixes: tuple[str, ...],
    tree_extensions: tuple[str, ...],
    max_files: int,
    max_depth: int = 4,
) -> list[str]:
    tree = client.fetch_tree(full_name, ref=ref)
    if tree is not None:
        paths, truncated = tree
        if truncated:
            # Missing entries would look like missing files, which is exactly the
            # silent-empty shape. Probe instead of quietly syncing a partial repo.
            logger.warning(
                "Tree for %s is truncated by GitHub; falling back to path probing",
                full_name,
            )
        else:
            return _select_from_tree(
                paths,
                root_paths=root_paths,
                tree_prefixes=tree_prefixes,
                tree_extensions=tree_extensions,
                max_files=max_files,
                max_depth=max_depth,
            )

    selected: list[str] = []
    seen: set[str] = set()

    for path in root_paths:
        normalized = path.strip().lstrip("/")
        if not normalized or normalized in seen:
            continue
        if client.file_exists(full_name, normalized, ref=ref):
            selected.append(normalized)
            seen.add(normalized)
        if len(selected) >= max_files:
            return selected

    for prefix in tree_prefixes:
        if len(selected) >= max_files:
            break
        root = prefix.strip().strip("/")
        if not root:
            continue
        queue: list[tuple[str, int]] = [(root, 0)]
        while queue and len(selected) < max_files:
            current, depth = queue.pop(0)
            try:
                entries = client.list_directory(full_name, current, ref=ref)
            except GitHubAPIError as exc:
                if exc.status == 404:
                    break
                raise
            for entry in entries:
                entry_path = str(entry.get("path") or "")
                entry_type = entry.get("type")
                if not entry_path:
                    continue
                if entry_type == "file" and _matches_extension(entry_path, tree_extensions):
                    if entry_path not in seen:
                        selected.append(entry_path)
                        seen.add(entry_path)
                        if len(selected) >= max_files:
                            return selected
                elif entry_type == "dir" and depth + 1 < max_depth:
                    queue.append((entry_path, depth + 1))
    return selected


def discover_org_repos(
    client: RepoContentGitHubClient,
    org: str,
    *,
    markers: tuple[str, ...],
    max_repos: int,
) -> list[str]:
    """Auto-join org repos that carry a marker file (e.g. AGENTS.md/CONTEXT.md/
    SKILL.md) at their root, so the static allowlist never silently lags reality.

    Returns the full names of non-archived repos in ``org`` that have at least one
    ``markers`` file at their default-branch root. Failures degrade to an empty
    result rather than aborting the sync.
    """
    if not markers or max_repos <= 0:
        return []
    try:
        repos = client.fetch_repos(org, max_repos=max_repos)
    except GitHubAPIError as exc:
        logger.warning("Auto-join skipped: could not list repos for org %s: %s", org, exc)
        return []
    discovered: list[str] = []
    for repo in repos:
        full_name = repo.full_name
        ref = repo.default_branch
        if not full_name or repo.archived or not ref:
            continue
        for marker in markers:
            normalized = marker.strip().lstrip("/")
            if not normalized:
                continue
            try:
                if client.file_exists(full_name, normalized, ref=ref):
                    discovered.append(full_name)
                    break
            except GitHubAPIError as exc:
                logger.warning(
                    "Auto-join: marker probe failed for %s/%s: %s", full_name, normalized, exc
                )
                continue
    return discovered


class RepoContentSyncer:
    """Fetch allowlisted repository files and cognify them into the vault."""

    def __init__(
        self,
        citadel: Citadel,
        *,
        org: str | None = None,
        client: RepoContentGitHubClient | None = None,
        state_path: str | Path | None = None,
        learning: LearningProcess | None = None,
    ) -> None:
        self.citadel = citadel
        self.config = citadel.config
        self.org = org or self.config.github_org
        self.client = client or RepoContentGitHubClient(token=self.config.github_token)
        self.learning = learning or LearningProcess(citadel)
        self.state_path = Path(state_path or self.config.repo_content_sync_state_path)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": STATE_VERSION, "files": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": STATE_VERSION, "files": {}}
        if not isinstance(data, dict):
            return {"version": STATE_VERSION, "files": {}}
        files = data.get("files")
        if not isinstance(files, dict):
            files = {}
        return {"version": STATE_VERSION, "files": files, **{k: v for k, v in data.items() if k != "files"}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _resolved_repos(self) -> list[str]:
        repos = self.config.repo_content_sync_repos or DEFAULT_REPO_CONTENT_REPOS
        resolved = [resolve_repo_full_name(name, self.org) for name in repos if name.strip()]
        if not self.config.repo_content_sync_autojoin_enabled:
            return resolved
        markers = (
            self.config.repo_content_sync_autojoin_markers
            or DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS
        )
        discovered = discover_org_repos(
            self.client,
            self.org,
            markers=markers,
            max_repos=self.config.repo_content_sync_autojoin_max_repos,
        )
        seen = set(resolved)
        for full_name in discovered:
            if full_name not in seen:
                resolved.append(full_name)
                seen.add(full_name)
        return resolved

    async def status(self) -> dict[str, Any]:
        state = self._load_state()
        files = state.get("files") if isinstance(state.get("files"), dict) else {}
        return {
            "ok": True,
            "authenticated": bool(getattr(self.client, "token", None)),
            "source_type": "github_repo_content",
            "org": self.org,
            "enabled": self.config.repo_content_sync_enabled,
            "dataset": self.config.repo_content_sync_dataset,
            "session": self.config.repo_content_sync_session,
            "autojoin_enabled": self.config.repo_content_sync_autojoin_enabled,
            "autojoin_markers": list(
                self.config.repo_content_sync_autojoin_markers
                or DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS
            ),
            "repos": self._resolved_repos(),
            "root_paths": list(
                self.config.repo_content_sync_root_paths or DEFAULT_REPO_CONTENT_ROOT_PATHS
            ),
            "tree_prefixes": list(
                self.config.repo_content_sync_tree_prefixes or DEFAULT_REPO_CONTENT_TREE_PREFIXES
            ),
            "tree_extensions": list(
                self.config.repo_content_sync_tree_extensions or DEFAULT_REPO_CONTENT_TREE_EXTENSIONS
            ),
            "max_files_per_repo": self.config.repo_content_sync_max_files_per_repo,
            "max_bytes_per_file": self.config.repo_content_sync_max_bytes_per_file,
            "run_improve": self.config.repo_content_sync_run_improve,
            "last_checked_at": state.get("last_checked_at"),
            "tracked_files": len(files),
            "state_path": str(self.state_path),
        }

    async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        if not self.config.repo_content_sync_enabled:
            return {
                "ok": True,
                "enabled": False,
                "reason": "repo_content_sync_disabled",
                "dry_run": dry_run,
            }

        checked_at = utc_now()
        authenticated = bool(getattr(self.client, "token", None))
        if not authenticated:
            logger.warning(
                "Repo content sync is running UNAUTHENTICATED (no GITHUB_TOKEN); GitHub "
                "throttles anonymous requests to 60/hr and will 403 across the repo allowlist.",
            )
        state = self._load_state()
        tracked: dict[str, Any] = dict(state.get("files") or {})
        root_paths = self.config.repo_content_sync_root_paths or DEFAULT_REPO_CONTENT_ROOT_PATHS
        tree_prefixes = self.config.repo_content_sync_tree_prefixes or DEFAULT_REPO_CONTENT_TREE_PREFIXES
        tree_extensions = (
            self.config.repo_content_sync_tree_extensions or DEFAULT_REPO_CONTENT_TREE_EXTENSIONS
        )
        max_files = max(1, self.config.repo_content_sync_max_files_per_repo)
        max_bytes = max(256, self.config.repo_content_sync_max_bytes_per_file)

        repo_results: list[dict[str, Any]] = []
        ingested_files = 0
        skipped_files = 0
        blocked_files = 0
        improved = False
        skip_totals: dict[str, int] = {}
        blocked_reasons: dict[str, int] = {}

        def _checkpoint(tracked_files: dict[str, Any]) -> None:
            """Persist progress so a killed run resumes where it stopped."""
            if dry_run:
                return
            state["version"] = STATE_VERSION
            state["files"] = tracked_files
            try:
                self._save_state(state)
            except OSError as exc:
                # A checkpoint failure must not abort a sync that is otherwise
                # working; worst case is the old behaviour of redoing files.
                logger.warning(
                    "Repo content sync could not checkpoint state: %s", exc.__class__.__name__
                )

        def _record_skip(repo_result: dict[str, Any], reason: str) -> None:
            nonlocal skipped_files
            skipped_files += 1
            repo_result["skipped"] += 1
            reasons = repo_result["skipped_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
            skip_totals[reason] = skip_totals.get(reason, 0) + 1

        for full_name in self._resolved_repos():
            repo_result: dict[str, Any] = {
                "repo": full_name,
                "paths_discovered": 0,
                "ingested": 0,
                "skipped": 0,
                "skipped_reasons": {},
                "blocked": 0,
                "blocked_paths": [],
                "errors": [],
            }
            try:
                branch = self.client.fetch_default_branch(full_name)
                ref = self.client.fetch_commit_sha(full_name, ref=branch)
                paths = discover_repo_paths(
                    self.client,
                    full_name,
                    ref=ref,
                    root_paths=root_paths,
                    tree_prefixes=tree_prefixes,
                    tree_extensions=tree_extensions,
                    max_files=max_files,
                )
                repo_result["paths_discovered"] = len(paths)
                repo_result["ref"] = ref

                for path in paths:
                    key = f"{full_name}/{path}"
                    try:
                        file = self.client.fetch_file_text(full_name, path, ref=ref)
                    except GitHubAPIError as exc:
                        repo_result["errors"].append({"path": path, "error": str(exc)[:200]})
                        continue
                    if file is None:
                        _record_skip(repo_result, "unsupported_encoding")
                        continue
                    if len(file.content.encode("utf-8")) > max_bytes:
                        _record_skip(repo_result, "too_large")
                        continue

                    previous = tracked.get(key) if isinstance(tracked.get(key), dict) else {}
                    unchanged = (
                        not force
                        and previous.get("sha") == file.sha
                        and previous.get("content_hash") == file.content_hash
                    )
                    if unchanged:
                        _record_skip(repo_result, "unchanged")
                        continue

                    scan = scan_text_entries(
                        [
                            SecurityScanEntry(
                                source="repo_content",
                                location=key,
                                text=file.content,
                            )
                        ],
                        block_severity=self.config.github_sync_security_block_severity,
                    )
                    if scan.get("blocked"):
                        blocked_files += 1
                        repo_result["blocked"] += 1
                        # A block silently drops a file from the vault. Without
                        # the path and the rule, a scanner false positive is
                        # invisible: the ledger says "blocked: 7" and nothing
                        # else, and reconstructing which seven meant
                        # re-implementing the scan by hand. Categories and
                        # severities are already redacted by public_dict().
                        categories = sorted(
                            {
                                str(finding.get("category"))
                                for finding in scan.get("findings", [])
                            }
                        )
                        for category in categories:
                            blocked_reasons[category] = blocked_reasons.get(category, 0) + 1
                        repo_result["blocked_paths"].append(
                            {
                                "path": path,
                                "highest_severity": scan.get("highest_severity"),
                                "finding_count": scan.get("finding_count"),
                                "categories": categories,
                            }
                        )
                        logger.warning(
                            "Repo content sync BLOCKED %s: severity=%s findings=%s rules=%s",
                            key,
                            scan.get("highest_severity"),
                            scan.get("finding_count"),
                            ",".join(categories) or "unknown",
                        )
                        continue

                    document = format_repo_content_document(file, checked_at=checked_at)
                    if dry_run:
                        ingested_files += 1
                        repo_result["ingested"] += 1
                        tracked[key] = {
                            "sha": file.sha,
                            "content_hash": file.content_hash,
                            "last_seen_at": checked_at,
                            "dry_run": True,
                        }
                        continue

                    outcome = await self.learning.learn(
                        document,
                        dataset=self.config.repo_content_sync_dataset,
                        session_id=self.config.repo_content_sync_session,
                        tags=[
                            "github",
                            "repo-content",
                            "product-knowledge",
                            full_name.split("/")[-1],
                            Path(path).suffix.lstrip(".") or "text",
                        ],
                        operation="repo_content_sync",
                        # Improve ONCE after the whole sync, not per file. See
                        # LearningProcess.improve_once for why.
                        run_improve=False,
                        detect_conflicts=False,
                    )
                    if any(result.accepted for result in outcome.all_ingests):
                        ingested_files += 1
                        repo_result["ingested"] += 1
                        tracked[key] = {
                            "sha": file.sha,
                            "content_hash": file.content_hash,
                            "last_ingested_at": checked_at,
                        }
                        # Checkpoint immediately. State used to be written only
                        # after every repo finished, so a run killed part-way
                        # ingested files into cognee and recorded none of them,
                        # and the next run redid the same work. A killed run
                        # must be able to resume, not restart.
                        _checkpoint(tracked)
                    else:
                        _record_skip(repo_result, "ingest_rejected")
            except GitHubAPIError as exc:
                repo_result["errors"].append({"error": str(exc)[:240]})
            repo_results.append(repo_result)

        # One improve pass for the whole sync. Per-file improve made a full
        # forced sync cost ~2 min/file, so 60 files needed ~2 h against a 300 s
        # platform request ceiling and could never finish.
        if not dry_run and ingested_files and self.config.repo_content_sync_run_improve:
            outcome = await self.learning.improve_once(
                dataset=self.config.repo_content_sync_dataset,
                session_ids=[self.config.repo_content_sync_session],
            )
            improved = not (isinstance(outcome, dict) and outcome.get("ok") is False)
            if not improved:
                logger.warning(
                    "Repo content sync ingested %d file(s) but the improve pass failed",
                    ingested_files,
                )

        if not dry_run:
            state["version"] = STATE_VERSION
            state["last_checked_at"] = checked_at
            state["files"] = tracked
            self._save_state(state)

        repos_errored = [result for result in repo_results if result["errors"]]
        all_repos_errored = (
            bool(repo_results)
            and len(repos_errored) == len(repo_results)
            and ingested_files == 0
        )

        logger.info(
            "Repo content sync finished: repos=%d discovered=%d ingested=%d skipped=%d "
            "blocked=%d blocked_rules=%s dry_run=%s",
            len(repo_results),
            sum(int(result.get("paths_discovered") or 0) for result in repo_results),
            ingested_files,
            skipped_files,
            blocked_files,
            blocked_reasons or "-",
            dry_run,
        )
        return {
            "ok": not all_repos_errored,
            "enabled": True,
            "authenticated": authenticated,
            "org": self.org,
            "checked_at": checked_at,
            "repos_scanned": len(repo_results),
            "repos_errored": len(repos_errored),
            "files_ingested": ingested_files,
            "files_skipped": skipped_files,
            "files_skipped_by_reason": skip_totals,
            "files_blocked": blocked_files,
            "files_blocked_by_reason": blocked_reasons,
            "improved": improved,
            "dry_run": dry_run,
            "repositories": repo_results,
        }


async def _cli_main() -> None:
    parser = argparse.ArgumentParser(description="Sync allowlisted repository content into Citadel.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    syncer = RepoContentSyncer(Citadel.from_env())
    result = await syncer.run(force=args.force, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))


def main() -> None:
    asyncio.run(_cli_main())


if __name__ == "__main__":
    main()
