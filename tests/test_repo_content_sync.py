from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kb.config import CitadelConfig
from kb.github_sync import GitHubAPIError
from kb.models import IngestResult
from kb.repo_content_sync import (
    DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS,
    RepoContentFile,
    RepoContentGitHubClient,
    RepoContentSyncer,
    discover_org_repos,
    discover_repo_paths,
    format_repo_content_document,
    resolve_repo_full_name,
)
from kb.repository_update import GitHubRepo


class FakeCitadel:
    def __init__(self, config: CitadelConfig) -> None:
        self.config = config


class FakeLearningProcess:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.improve_calls: list[dict[str, Any]] = []

    async def improve_once(self, **kwargs: Any) -> Any:
        self.improve_calls.append(kwargs)
        return {"ok": True}

    async def learn(self, data: str, **kwargs: Any) -> Any:
        self.calls.append({"data": data, **kwargs})

        class Outcome:
            ingest = IngestResult(True, "accepted", kwargs.get("dataset", "x"), ())
            chunk_ingests = ()
            improve = {"ok": True} if kwargs.get("run_improve") else None

            @property
            def all_ingests(self) -> tuple[IngestResult, ...]:
                return (self.ingest,)

            @property
            def improved(self) -> bool:
                return bool(self.improve)

        return Outcome()


class FakeRepoContentClient(RepoContentGitHubClient):
    def __init__(self) -> None:
        super().__init__(token=None)
        self.files: dict[str, dict[str, str]] = {
            "masumi-network/sokosumi-cli/README.md": {
                "sha": "abc",
                "content": "# Sokosumi CLI\n\nHeadless mode docs.",
            },
            "masumi-network/sokosumi-cli/skills/sokosumi/SKILL.md": {
                "sha": "def",
                "content": "# Skill\n\nAgent workflow.",
            },
        }
        self.directories: dict[str, list[dict[str, Any]]] = {
            "masumi-network/sokosumi-cli/skills": [
                {"path": "skills/sokosumi", "type": "dir"},
            ],
            "masumi-network/sokosumi-cli/skills/sokosumi": [
                {"path": "skills/sokosumi/SKILL.md", "type": "file"},
            ],
        }
        # The single-tree read (one request per repo). Without this override the
        # fake inherits the REAL fetch_tree and the test hits api.github.com.
        self.tree_truncated = False
        self.tree_fails = False
        self.tree_calls = 0
        self.probe_calls = 0

    def fetch_tree(self, full_name: str, *, ref: str) -> tuple[list[str], bool] | None:
        self.tree_calls += 1
        if self.tree_fails:
            return None
        prefix = f"{full_name}/"
        paths = [key[len(prefix) :] for key in self.files if key.startswith(prefix)]
        return sorted(paths), self.tree_truncated

    def fetch_default_branch(self, full_name: str) -> str:
        return "main"

    def fetch_commit_sha(self, full_name: str, *, ref: str) -> str:
        return "commit123"

    def file_exists(self, full_name: str, path: str, *, ref: str) -> bool:
        self.probe_calls += 1
        return f"{full_name}/{path}" in self.files

    def fetch_file_text(self, full_name: str, path: str, *, ref: str) -> RepoContentFile | None:
        payload = self.files.get(f"{full_name}/{path}")
        if payload is None:
            return None
        return RepoContentFile(
            repo=full_name,
            path=path,
            sha=payload["sha"],
            ref=ref,
            content=payload["content"],
            html_url=f"https://github.com/{full_name}/blob/{ref}/{path}",
        )

    def list_directory(self, full_name: str, path: str, *, ref: str) -> list[dict[str, Any]]:
        self.probe_calls += 1
        return self.directories.get(f"{full_name}/{path}", [])


def test_resolve_repo_full_name() -> None:
    assert resolve_repo_full_name("sokosumi", "masumi-network") == "masumi-network/sokosumi"
    assert resolve_repo_full_name("masumi-network/sokosumi", "masumi-network") == "masumi-network/sokosumi"


def test_format_repo_content_document() -> None:
    file = RepoContentFile(
        repo="masumi-network/sokosumi-cli",
        path="README.md",
        sha="abc",
        ref="commit123",
        content="# Title",
        html_url="https://example.com",
    )
    document = format_repo_content_document(file, checked_at="2026-06-16T00:00:00Z")
    assert "masumi-network/sokosumi-cli/README.md" in document
    assert "Commit: commit123" in document
    assert "# Title" in document


def test_discover_repo_paths_includes_root_and_tree_files() -> None:
    client = FakeRepoContentClient()
    paths = discover_repo_paths(
        client,
        "masumi-network/sokosumi-cli",
        ref="commit123",
        root_paths=("README.md", "MISSING.md"),
        tree_prefixes=("skills/",),
        tree_extensions=(".md",),
        max_files=10,
    )
    assert paths == ["README.md", "skills/sokosumi/SKILL.md"]


@pytest.mark.asyncio
async def test_repo_content_syncer_ingests_changed_files(tmp_path: Path) -> None:
    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_dataset="masumi-network",
        repo_content_sync_session="masumi-repo-content",
        repo_content_sync_state_path=str(tmp_path / "repo_content_sync_state.json"),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md",),
        repo_content_sync_tree_prefixes=("skills/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
        repo_content_sync_run_improve=True,
    )
    learning = FakeLearningProcess()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=config.repo_content_sync_state_path,
        learning=learning,  # type: ignore[arg-type]
    )

    first = await syncer.run()
    assert first["files_ingested"] == 2
    assert len(learning.calls) == 2
    assert learning.calls[0]["tags"] == [
        "github",
        "repo-content",
        "product-knowledge",
        "sokosumi-cli",
        "md",
    ]

    second = await syncer.run()
    assert second["files_ingested"] == 0
    assert second["files_skipped"] == 2
    assert second["files_skipped_by_reason"] == {"unchanged": 2}
    assert second["repositories"][0]["skipped_reasons"] == {"unchanged": 2}

    state = json.loads(Path(config.repo_content_sync_state_path).read_text(encoding="utf-8"))
    assert state["files"]["masumi-network/sokosumi-cli/README.md"]["sha"] == "abc"


@pytest.mark.asyncio
async def test_repo_content_syncer_respects_disabled_flag() -> None:
    config = CitadelConfig(repo_content_sync_enabled=False)
    syncer = RepoContentSyncer(FakeCitadel(config), client=FakeRepoContentClient())
    result = await syncer.run()
    assert result["enabled"] is False


class FailingRepoContentClient(RepoContentGitHubClient):
    def __init__(self) -> None:
        super().__init__(token=None)

    def fetch_default_branch(self, full_name: str) -> str:
        raise GitHubAPIError(
            "GitHub API returned 403: API rate limit exceeded for 95.90.238.57"
        )


@pytest.mark.asyncio
async def test_repo_content_syncer_marks_failure_when_all_repos_error(tmp_path: Path) -> None:
    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_state_path=str(tmp_path / "repo_content_sync_state.json"),
        repo_content_sync_repos=("sokosumi-cli", "sokosumi-docs"),
    )
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FailingRepoContentClient(),
        state_path=config.repo_content_sync_state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )

    result = await syncer.run()

    assert result["ok"] is False
    assert result["authenticated"] is False
    assert result["repos_errored"] == 2
    assert result["files_ingested"] == 0
    assert all(repo["errors"] for repo in result["repositories"])


class FakeAutoJoinClient(RepoContentGitHubClient):
    """Org with: a marker repo, a markerless repo, and an archived marker repo."""

    def __init__(self) -> None:
        super().__init__(token=None)
        self.markers: set[str] = {
            "masumi-network/masumi-agent-messenger/AGENTS.md",
            "masumi-network/archived-repo/AGENTS.md",
            "masumi-network/sokosumi-cli/SKILL.md",
        }

    def _repo(self, name: str, *, archived: bool = False, branch: str | None = "main") -> GitHubRepo:
        return GitHubRepo(
            name=name,
            full_name=f"masumi-network/{name}",
            html_url="",
            description=None,
            language=None,
            pushed_at=None,
            updated_at=None,
            default_branch=branch,
            visibility="public",
            archived=archived,
            stargazers_count=0,
            forks_count=0,
            open_issues_count=0,
            topics=(),
            license_name=None,
        )

    def fetch_repos(self, org: str, *, max_repos: int, include_private: bool = True) -> list[GitHubRepo]:
        return [
            self._repo("masumi-agent-messenger"),
            self._repo("no-markers-here"),
            self._repo("archived-repo", archived=True),
            self._repo("sokosumi-cli"),
        ][:max_repos]

    def file_exists(self, full_name: str, path: str, *, ref: str) -> bool:
        return f"{full_name}/{path}" in self.markers


def test_discover_org_repos_joins_only_non_archived_marker_repos() -> None:
    joined = discover_org_repos(
        FakeAutoJoinClient(),
        "masumi-network",
        markers=DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS,
        max_repos=50,
    )
    assert joined == [
        "masumi-network/masumi-agent-messenger",
        "masumi-network/sokosumi-cli",
    ]


def test_resolved_repos_unions_autojoin_with_dedup() -> None:
    config = CitadelConfig(
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_autojoin_enabled=True,
        repo_content_sync_autojoin_markers=("AGENTS.md",),
        repo_content_sync_autojoin_max_repos=50,
    )
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeAutoJoinClient(),
        state_path="unused",
    )
    resolved = syncer._resolved_repos()
    assert resolved == [
        "masumi-network/sokosumi-cli",
        "masumi-network/masumi-agent-messenger",
    ]


def test_resolved_repos_autojoin_disabled_skips_discovery() -> None:
    config = CitadelConfig(repo_content_sync_repos=("sokosumi-cli",))

    fetch_calls: list[int] = []

    class TrackingClient(FakeAutoJoinClient):
        def fetch_repos(self, org: str, *, max_repos: int, include_private: bool = True) -> list[GitHubRepo]:
            fetch_calls.append(1)
            return super().fetch_repos(org, max_repos=max_repos, include_private=include_private)

    syncer = RepoContentSyncer(FakeCitadel(config), client=TrackingClient(), state_path="unused")
    assert syncer._resolved_repos() == ["masumi-network/sokosumi-cli"]
    assert fetch_calls == []


# --- single-tree discovery: the refactor must be behaviour-preserving --------


def _rich_client() -> FakeRepoContentClient:
    """A repo shape with every edge the two code paths could disagree on."""
    client = FakeRepoContentClient()
    client.files = {
        "o/r/README.md": {"sha": "1", "content": "x"},
        "o/r/AGENTS.md": {"sha": "2", "content": "x"},
        "o/r/skills/a/SKILL.md": {"sha": "3", "content": "x"},
        "o/r/skills/a/b/deep.md": {"sha": "4", "content": "x"},
        "o/r/skills/a/b/c/d/too-deep.md": {"sha": "5", "content": "x"},
        "o/r/skills/notes.txt": {"sha": "6", "content": "x"},  # wrong extension
        "o/r/docs/guide.md": {"sha": "7", "content": "x"},
        "o/r/skillsets/decoy.md": {"sha": "8", "content": "x"},  # prefix lookalike
    }
    client.directories = {
        "o/r/skills": [
            {"path": "skills/a", "type": "dir"},
            {"path": "skills/notes.txt", "type": "file"},
        ],
        "o/r/skills/a": [
            {"path": "skills/a/SKILL.md", "type": "file"},
            {"path": "skills/a/b", "type": "dir"},
        ],
        "o/r/skills/a/b": [
            {"path": "skills/a/b/deep.md", "type": "file"},
            {"path": "skills/a/b/c", "type": "dir"},
        ],
        "o/r/skills/a/b/c": [{"path": "skills/a/b/c/d", "type": "dir"}],
        "o/r/skills/a/b/c/d": [{"path": "skills/a/b/c/d/too-deep.md", "type": "file"}],
        "o/r/docs": [{"path": "docs/guide.md", "type": "file"}],
        "o/r/skillsets": [{"path": "skillsets/decoy.md", "type": "file"}],
    }
    return client


_DISCOVERY_KW = dict(
    root_paths=("README.md", "AGENTS.md", "MISSING.md"),
    tree_prefixes=("skills/", "docs/"),
    tree_extensions=(".md",),
    max_files=10,
    max_depth=3,
)


def test_tree_and_probe_discovery_select_the_same_files() -> None:
    """The refactor must not change WHICH files get synced, only how we find them.

    A silent change in selection would be invisible in production: the vault
    would just quietly hold different content.
    """
    via_tree = _rich_client()
    via_probe = _rich_client()
    via_probe.tree_fails = True  # force the old path

    tree_paths = discover_repo_paths(via_tree, "o/r", ref="sha", **_DISCOVERY_KW)
    probe_paths = discover_repo_paths(via_probe, "o/r", ref="sha", **_DISCOVERY_KW)

    assert tree_paths == probe_paths, f"selection drifted: {tree_paths} != {probe_paths}"
    assert tree_paths == [
        "README.md",
        "AGENTS.md",
        "skills/a/SKILL.md",
        "skills/a/b/deep.md",
        "docs/guide.md",
    ]
    # The edges: wrong extension, too deep, and a prefix lookalike are all excluded.
    assert "skills/notes.txt" not in tree_paths
    assert "skills/a/b/c/d/too-deep.md" not in tree_paths
    assert "skillsets/decoy.md" not in tree_paths


def test_the_tree_path_makes_one_request_instead_of_many() -> None:
    """The point of the change: stop firing requests that 404."""
    via_tree = _rich_client()
    via_probe = _rich_client()
    via_probe.tree_fails = True

    discover_repo_paths(via_tree, "o/r", ref="sha", **_DISCOVERY_KW)
    discover_repo_paths(via_probe, "o/r", ref="sha", **_DISCOVERY_KW)

    assert via_tree.tree_calls == 1
    assert via_tree.probe_calls == 0, "the tree path must not probe at all"
    assert via_probe.probe_calls >= 7, "the old path really did fire this many"


def test_a_truncated_tree_falls_back_instead_of_syncing_a_partial_repo() -> None:
    """A truncated tree is missing files, which looks identical to 'no files'.

    That is the silent-empty shape, so it must fall back rather than quietly
    sync less than the repo actually has.
    """
    client = _rich_client()
    client.tree_truncated = True

    paths = discover_repo_paths(client, "o/r", ref="sha", **_DISCOVERY_KW)

    assert client.probe_calls > 0, "truncation must fall back to probing"
    assert paths == ["README.md", "AGENTS.md", "skills/a/SKILL.md", "skills/a/b/deep.md", "docs/guide.md"]


def test_an_unreadable_tree_falls_back_and_still_finds_everything() -> None:
    client = _rich_client()
    client.tree_fails = True

    paths = discover_repo_paths(client, "o/r", ref="sha", **_DISCOVERY_KW)

    assert client.probe_calls > 0
    assert "README.md" in paths and "skills/a/SKILL.md" in paths


def test_max_files_cap_is_respected_identically_on_both_paths() -> None:
    via_tree = _rich_client()
    via_probe = _rich_client()
    via_probe.tree_fails = True
    kw = {**_DISCOVERY_KW, "max_files": 3}

    assert (
        discover_repo_paths(via_tree, "o/r", ref="sha", **kw)
        == discover_repo_paths(via_probe, "o/r", ref="sha", **kw)
    )


def _depth_ordered_client() -> FakeRepoContentClient:
    """A prefix whose nested file arrives BEFORE its top-level one.

    This is the shape the rich fixture cannot express: its only file directly
    under ``skills/`` has the wrong extension, so it is filtered out before the
    cap can ever choose between the two depths. Here ``skills/a`` sorts before
    ``skills/zzz.md``, so the recursive tree lists ``skills/a/SKILL.md`` first
    while the directory walk saw ``skills/zzz.md`` first.
    """
    client = FakeRepoContentClient()
    client.files = {
        "o/r/skills/a/SKILL.md": {"sha": "1", "content": "x"},
        "o/r/skills/zzz.md": {"sha": "2", "content": "x"},
    }
    client.directories = {
        "o/r/skills": [
            {"path": "skills/a", "type": "dir"},
            {"path": "skills/zzz.md", "type": "file"},
        ],
        "o/r/skills/a": [{"path": "skills/a/SKILL.md", "type": "file"}],
    }
    return client


def test_a_binding_cap_keeps_the_shallower_file_on_both_paths() -> None:
    """The cap must truncate the same list probing would have truncated.

    Selection drift is silent in production: paths_discovered still reports the
    cap, nothing is logged, and the files that fall off the end simply stop
    being refreshed while different ones start being ingested.
    """
    via_tree = _depth_ordered_client()
    via_probe = _depth_ordered_client()
    via_probe.tree_fails = True
    kw = {**_DISCOVERY_KW, "tree_prefixes": ("skills/",), "max_files": 1}

    tree_paths = discover_repo_paths(via_tree, "o/r", ref="sha", **kw)
    probe_paths = discover_repo_paths(via_probe, "o/r", ref="sha", **kw)

    assert probe_paths == ["skills/zzz.md"], "the walk takes the prefix's own files first"
    assert tree_paths == probe_paths, f"selection drifted under the cap: {tree_paths} != {probe_paths}"


# --- resumability -----------------------------------------------------------
#
# 2026-07-31: a forced sync was killed by Railway's 300 s request ceiling after
# ingesting files, and state was only written after the whole loop, so nothing
# recorded them. Root cause of the two hours: run_improve fired a full cognee
# improve pass per file (~2 min each). Both halves are pinned below.


@pytest.mark.asyncio
async def test_improve_runs_once_for_the_whole_sync_not_once_per_file(
    tmp_path: Path,
) -> None:
    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_dataset="masumi-network",
        repo_content_sync_session="masumi-repo-content",
        repo_content_sync_state_path=str(tmp_path / "state.json"),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md",),
        repo_content_sync_tree_prefixes=("skills/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
        repo_content_sync_run_improve=True,
    )
    learning = FakeLearningProcess()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=config.repo_content_sync_state_path,
        learning=learning,  # type: ignore[arg-type]
    )

    result = await syncer.run()

    assert result["files_ingested"] == 2
    # No per-file improve...
    assert all(call.get("run_improve") is False for call in learning.calls)
    # ...and exactly one improve for the whole run.
    assert len(learning.improve_calls) == 1
    assert learning.improve_calls[0]["dataset"] == "masumi-network"
    assert learning.improve_calls[0]["session_ids"] == ["masumi-repo-content"]
    assert result["improved"] is True


@pytest.mark.asyncio
async def test_no_improve_pass_when_nothing_was_ingested(tmp_path: Path) -> None:
    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_dataset="masumi-network",
        repo_content_sync_session="masumi-repo-content",
        repo_content_sync_state_path=str(tmp_path / "state.json"),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md",),
        repo_content_sync_tree_prefixes=("skills/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
        repo_content_sync_run_improve=True,
    )
    learning = FakeLearningProcess()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=config.repo_content_sync_state_path,
        learning=learning,  # type: ignore[arg-type]
    )

    await syncer.run()
    learning.improve_calls.clear()
    second = await syncer.run()  # everything unchanged now

    assert second["files_ingested"] == 0
    assert learning.improve_calls == []


@pytest.mark.asyncio
async def test_state_is_checkpointed_per_file_so_a_killed_run_resumes(
    tmp_path: Path,
) -> None:
    """A run that dies mid-loop must not lose the files it already ingested."""
    state_path = tmp_path / "state.json"
    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_dataset="masumi-network",
        repo_content_sync_session="masumi-repo-content",
        repo_content_sync_state_path=str(state_path),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md",),
        repo_content_sync_tree_prefixes=("skills/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
        repo_content_sync_run_improve=False,
    )

    class DiesAfterFirstFile(FakeLearningProcess):
        async def learn(self, data: str, **kwargs: Any) -> Any:
            if self.calls:
                raise RuntimeError("killed mid-sync, like a 300s request timeout")
            return await super().learn(data, **kwargs)

    learning = DiesAfterFirstFile()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=learning,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError):
        await syncer.run()

    # The first file's success survived the crash.
    assert state_path.exists(), "no checkpoint written before the crash"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(persisted["files"]) == 1, persisted

    # A fresh run skips what was already done instead of redoing it.
    resumed_learning = FakeLearningProcess()
    resumed = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=resumed_learning,  # type: ignore[arg-type]
    )
    result = await resumed.run()

    assert result["files_ingested"] == 1
    assert result["files_skipped_by_reason"].get("unchanged") == 1
    assert len(resumed_learning.calls) == 1
