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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from kb.cognee_client import ingest_indexing_state
from kb.github_sync import GitHubAPIError, GitHubOrgClient, utc_now
from kb.learning import LearningProcess
from kb.security_scan import SecurityScanEntry, scan_text_entries
from kb.service import Citadel
from kb.state_io import StateFileError, load_state_file, save_state_file

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

# One lock per state file, NOT per syncer instance. ``get_repo_content_syncer``
# in kb.server builds a fresh ``RepoContentSyncer`` on every call (app.state
# .repo_content_syncer is never assigned anywhere), and ``LearningAgent``
# constructs its own in __init__, so the evolve scheduler's syncer and the one
# behind POST /api/repo-content-sync/run are different objects. An instance
# attribute lock would therefore have shipped inert: it would guard nothing
# while looking exactly like a working guard. The state file is the shared
# resource, so it is what the key has to be.
_RUN_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _run_lock(state_path: Path) -> asyncio.Lock:
    # An asyncio.Lock binds to the loop that first awaits it and raises if a
    # different loop uses it afterwards, so the loop is part of the identity.
    # The web process has exactly one loop for its whole life; the CLI runs a
    # fresh asyncio.run per invocation.
    key = str(Path(state_path).resolve())
    loop = asyncio.get_running_loop()
    entry = _RUN_LOCKS.get(key)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _RUN_LOCKS[key] = entry
    return entry[1]


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


def _cognee_data_ids(outcome: Any) -> list[str]:
    """Pull the data ids cognee assigned out of an ingest outcome.

    ``cognee.add()`` returns a ``PipelineRunInfo`` pydantic model (concretely a
    ``PipelineRunCompleted``), and ``data_ingestion_info`` is an ATTRIBUTE on it.
    ``CogneePublicClient.remember`` wraps that object unmodified as
    ``{"added": <PipelineRunInfo>}``, so it is never a dict at this point.

    The previous version gated on ``isinstance(added, dict)`` and reasoned that
    an empty list was safe because the next sync would re-ingest the file. That
    was wrong in both halves. The gate was always taken, so this returned ``[]``
    for every file ever ingested, and an empty list is NOT safe: it leaves the
    ``unchanged`` guard permanently unsatisfiable, the next run re-ingests, the
    process-lifetime dedupe in :meth:`Citadel.ingest` rejects it as
    ``duplicate_in_process``, and that rejection writes no state at all. The
    result was a livelock in which no sync could ever converge.

    Read the attribute first and fall back to a mapping, so a future cognee that
    returns a plain dict still works. Never gate on the container type alone.
    """
    ids: list[str] = []
    for result in getattr(outcome, "all_ingests", ()) or ():
        payload = getattr(result, "cognee_result", None)
        added = payload.get("added") if isinstance(payload, Mapping) else None
        if added is None:
            continue
        info_list = getattr(added, "data_ingestion_info", None)
        if info_list is None and isinstance(added, Mapping):
            info_list = added.get("data_ingestion_info")
        for info in info_list or ():
            data_id = (
                info.get("data_id")
                if isinstance(info, Mapping)
                else getattr(info, "data_id", None)
            )
            if data_id:
                ids.append(str(data_id))
    return list(dict.fromkeys(ids))


def format_repo_content_document(file: RepoContentFile) -> str:
    """Render a repo file as a vault document.

    Deliberately carries NO retrieval timestamp. It used to include
    ``Retrieved: {checked_at}``, which made the body of an unchanged file
    different on every sync. That defeated content-hash dedup at ingest and the
    text-keyed dedup at query time, so each re-sync added another copy: one
    README was occupying 8 of 8 result slots under 8 document ids, and half the
    average result set was repeats of a single file.

    ``Commit`` and ``Blob`` pin the exact version this text came from, and they
    must be stable while the file is unchanged, which is the property that
    makes a document deduplicable. When the file changes they change with it.

    That only holds if ``file.ref`` is the commit that last touched THIS file,
    never the repo HEAD. HEAD moves when ANY file in the repo changes, so a
    HEAD-valued ``Commit:`` line (and the same sha inside ``Source:``) re-wrote
    the body of every unchanged file on every re-render, minted a new content
    hash, and cognee's content-addressed ingestion dutifully created a new
    document: one README blob (a4b30a45) ended up as three documents under
    three commit values. The syncer resolves the per-file commit before
    rendering; see the pin step in :meth:`RepoContentSyncer.run`.
    """
    return "\n".join(
        [
            f"# {file.repo}/{file.path}",
            "",
            f"Repository: {file.repo}",
            f"Source: {file.html_url}",
            f"Commit: {file.ref}",
            f"Blob: {file.sha}",
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

    def fetch_last_commit_sha(self, full_name: str, path: str, *, ref: str) -> str:
        """The commit that last touched ``path``, as of ``ref``.

        This is what the document header must carry. The repo HEAD is a fact
        about the whole repository; the last-touching commit is a fact about
        the file, stable for exactly as long as the file's content is, and it
        stays a working immutable permalink. Neither the recursive tree nor the
        contents API carries commit information, so this is its own request —
        paid only for files that are actually being ingested.
        """
        data = self._get_json(
            f"/repos/{quote(full_name, safe='/')}/commits",
            {"path": path, "sha": ref, "per_page": 1},
        )
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise GitHubAPIError(
                f"Could not resolve the last commit for {full_name}/{path}."
            )
        commit_sha = data[0].get("sha")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise GitHubAPIError(
                f"Could not resolve the last commit for {full_name}/{path}."
            )
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
        # Absent file = genuine first run. A corrupt file raises instead of
        # flattening to empty: an empty state makes nothing "unchanged", so the
        # entire allowlist re-ingests while reporting ok: True (#148).
        data = load_state_file(self.state_path)
        if data is None:
            return {"version": STATE_VERSION, "files": {}}
        files = data.get("files")
        if not isinstance(files, dict):
            files = {}
        return {"version": STATE_VERSION, "files": files, **{k: v for k, v in data.items() if k != "files"}}

    def _save_state(self, state: dict[str, Any]) -> None:
        # Atomic (temp file + rename) so a restart mid-write cannot leave the
        # truncated file _load_state would refuse (#148).
        save_state_file(self.state_path, state)

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
        # A corrupt state file must show as a red source, not a 500 (#148).
        state_error: str | None = None
        try:
            state = self._load_state()
        except StateFileError as exc:
            state = {}
            state_error = str(exc)
        files = state.get("files") if isinstance(state.get("files"), dict) else {}
        # Off the loop for the same reason as in ``run``: with autojoin on this
        # is up to 1 + max_repos * len(markers) synchronous urllib round trips,
        # and GET /api/repo-content-sync is served from the web process's single
        # event loop.
        repos = await asyncio.to_thread(self._resolved_repos)
        return {
            "ok": state_error is None,
            "state_error": state_error,
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
            "repos": repos,
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
        """Serialise passes over one state file, then run.

        Two overlapping passes are a lost update, not just wasted work:
        ``tracked`` is snapshotted from the state file at the start of a pass
        and the whole dict is written back at the end, so whichever pass
        finishes last erases the entries the other one recorded. Those files
        then look un-ingested and are fetched and re-sent on the next pass.

        Before the GitHub calls moved off the event loop this was unreachable
        by construction: a pass that ingested nothing contained no ``await``
        that could yield (exactly the ingested=0 shape seen in production), so
        a second caller could not start until the first had returned. Freeing
        the loop is what makes the window real, so the guard belongs in the
        same change.

        The lock is keyed by state file rather than held on the instance
        because callers do not share an instance: ``get_repo_content_syncer``
        (kb/server.py) constructs a new ``RepoContentSyncer`` per call and
        ``LearningAgent.__init__`` builds its own, so the evolve scheduler's
        syncer and the one behind POST /api/repo-content-sync/run are
        different objects.

        A second caller is refused rather than queued. Queueing would park the
        request behind a full pass, which is minutes of work against a request
        ceiling, and would then redo the work the first pass just did.
        """
        lock = _run_lock(self.state_path)
        if lock.locked():
            logger.warning(
                "Repo content sync already in progress for %s; skipping this run",
                self.state_path,
            )
            return {
                "ok": True,
                "enabled": True,
                # Callers must not record this as a sync: it has no
                # checked_at and no counts, and writing it to the mesh would
                # stamp the source "synced" for a pass that did nothing.
                "skipped": True,
                "reason": "repo_content_sync_already_running",
                "dry_run": dry_run,
            }
        async with lock:
            return await self._run_locked(force=force, dry_run=dry_run)

    async def _run_locked(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
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
        # Split of ingested_files by what this run OBSERVED about the add:
        # cognee assigned data ids (confirmed) or it did not (unconfirmed — the
        # cognee_data_ids guard makes the next pass re-ingest those).
        add_confirmed_files = 0
        add_unconfirmed_files = 0
        # Disposition(s) of the graph-indexing requests behind the accepted
        # adds. Normally one value; a set so a mixed run cannot misreport.
        indexing_states: set[str] = set()
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

        # Also off the loop, and it is the largest of the blocking sites, not
        # the smallest. ``_resolved_repos`` looks like a config read, but with
        # repo_content_sync_autojoin_enabled it calls ``discover_org_repos``,
        # which issues one synchronous ``fetch_repos`` plus one synchronous
        # ``file_exists`` probe per repo per marker: at the defaults
        # (repo_content_sync_autojoin_max_repos=100, three markers in
        # DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS) up to 301 round trips, all of
        # them before the loop below reaches its first ``to_thread``.
        repos = await asyncio.to_thread(self._resolved_repos)
        for full_name in repos:
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
                # Every ``self.client`` call below is SYNCHRONOUS urllib, and
                # ``run`` is awaited from the web process's single event loop by
                # the evolve scheduler. Called inline they freeze every route on
                # the node for the whole sync: measured in production
                # 2026-08-03 as a 30.03s stall (``Evolve stage
                # repo_content_sync`` 18:14:06.578Z -> 18:14:36.610Z) in which a
                # POST /mcp/ took 19.18s and a GET /api/contributions/recent
                # gave up with a 499 — while that pass ingested nothing at all
                # (ingested=0 skipped=69). ``to_thread`` hands each round trip
                # to a worker and yields, so the loop keeps serving.
                #
                # ``discover_repo_paths`` is dispatched whole rather than
                # per-request: it is a sync function that itself makes several
                # calls (fetch_tree, then a probe fallback), so wrapping only
                # its callees would leave the walk between them on the loop.
                branch = await asyncio.to_thread(self.client.fetch_default_branch, full_name)
                ref = await asyncio.to_thread(self.client.fetch_commit_sha, full_name, ref=branch)
                paths = await asyncio.to_thread(
                    discover_repo_paths,
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
                        file = await asyncio.to_thread(
                            self.client.fetch_file_text, full_name, path, ref=ref
                        )
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
                    # An entry must also carry the id cognee assigned. Without
                    # it, "unchanged" only means "we told ourselves we ingested
                    # this", which is exactly how 12 files stayed permanently
                    # skipped while absent from the index: ingest reports
                    # accepted as soon as cognee.add() returns, but the graph
                    # write is a detached background cognify that can silently
                    # never run. Entries written before this field existed are
                    # therefore treated as needing re-ingestion, which repairs
                    # the state on the next sync instead of requiring force.
                    #
                    # Re-ingesting an unchanged file is cheap and safe only
                    # because the rendered document is byte-identical, so
                    # cognee's content-addressed ingestion resolves it to the
                    # same id rather than a copy. That takes BOTH halves:
                    # ADR-0016 removed the retrieval timestamp, and the pin
                    # step below keeps the Commit/Source header off the moving
                    # repo HEAD. With either half missing, every path that
                    # re-renders (force, lost state, entries predating
                    # cognee_data_ids) mints a duplicate document instead.
                    unchanged = (
                        not force
                        and previous.get("sha") == file.sha
                        and previous.get("content_hash") == file.content_hash
                        and bool(previous.get("cognee_data_ids"))
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

                    # Pin the header to the commit that last touched THIS
                    # file. `ref` is the repo HEAD, which moves when ANY file
                    # in the repo changes; rendering it into the body made an
                    # unchanged file hash differently on every re-render and
                    # duplicated the corpus (one blob under three commit
                    # values). The last-touching commit is a function of the
                    # file's own history, so the render is byte-stable across
                    # syncs even when the state file did not survive — and it
                    # stays an immutable permalink for citation. On failure,
                    # record the error and retry next sync rather than ever
                    # writing a document with a volatile ref.
                    try:
                        file_ref = await asyncio.to_thread(
                            self.client.fetch_last_commit_sha, full_name, path, ref=ref
                        )
                    except GitHubAPIError as exc:
                        repo_result["errors"].append({"path": path, "error": str(exc)[:200]})
                        continue
                    file = replace(
                        file,
                        ref=file_ref,
                        html_url=(
                            f"https://github.com/{full_name}/blob/"
                            f"{file_ref}/{quote(path, safe='/')}"
                        ),
                    )

                    document = format_repo_content_document(file)
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
                        data_ids = _cognee_data_ids(outcome)
                        # Count from the observation, not the request:
                        # `accepted` only says cognee.add() returned. Data ids
                        # in the outcome are the store's own receipt for the
                        # add; their absence is reported as unconfirmed instead
                        # of being folded into the success counter.
                        if data_ids:
                            add_confirmed_files += 1
                        else:
                            add_unconfirmed_files += 1
                        for result in outcome.all_ingests:
                            if result.accepted:
                                indexing_states.add(
                                    ingest_indexing_state(result.cognee_result)
                                )
                        tracked[key] = {
                            "sha": file.sha,
                            "content_hash": file.content_hash,
                            "last_ingested_at": checked_at,
                            # What cognee actually assigned. Stored so a later
                            # run can check its own claim against the index; a
                            # state file holding only sha and content_hash can
                            # never verify what it asserted.
                            "cognee_data_ids": data_ids,
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
            # files_ingested counts adds the store ACCEPTED; it does not
            # observe that anything became searchable. The split below is what
            # this run actually saw: cognee returned data ids for the add
            # (confirmed) or it did not (unconfirmed → re-ingested next pass
            # via the cognee_data_ids guard). `indexing` is the disposition of
            # the asynchronous graph-write request(s), never their outcome.
            "files_add_confirmed": add_confirmed_files,
            "files_add_unconfirmed": add_unconfirmed_files,
            "indexing": sorted(indexing_states),
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
