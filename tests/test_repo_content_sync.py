from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

# The real cognee return type. Imported, never hand-rolled: a fake that invents a
# third-party's shape can only prove that our parser parses the fake.
from cognee.modules.pipelines.models.PipelineRunInfo import PipelineRunCompleted

from kb.config import CitadelConfig
from kb.github_sync import GitHubAPIError
from kb.models import IngestResult
from kb.repo_content_sync import (
    DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS,
    STATE_VERSION,
    _cognee_data_ids,
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

        # The REAL cognee return type, imported from the installed package, not a
        # dict invented here. The previous fake returned
        # {"added": {"data_ingestion_info": [...]}}; cognee actually returns a
        # PipelineRunInfo model wrapped as {"added": <model>}, so _cognee_data_ids
        # extracted nothing in production while this fake made it look correct.
        # Every direction of the test passed against a shape that does not exist.
        # A fake may only specify a contract we own; for a third-party return
        # value it has to be the third party's own type.
        #
        # The id is derived from the content, because cognee is content-addressed
        # and returns the SAME data_id for a byte-identical document. That is the
        # premise ADR-0016 relies on, and a per-call counter contradicted it.
        cognee_result = {
            "added": PipelineRunCompleted(
                pipeline_run_id=uuid5(NAMESPACE_URL, f"run:{data}"),
                dataset_id=uuid5(NAMESPACE_URL, str(kwargs.get("dataset", "x"))),
                dataset_name=str(kwargs.get("dataset", "x")),
                payload=None,
                data_ingestion_info=[
                    {"data_id": str(uuid5(NAMESPACE_URL, f"data:{data}"))}
                ],
            ),
            "background_cognify": True,
        }

        class Outcome:
            ingest = IngestResult(
                True, "accepted", kwargs.get("dataset", "x"), (), cognee_result
            )
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
        # The repo HEAD. Mutable so a test can push an unrelated commit —
        # HEAD moves, the tracked files do not.
        self.head = "commit123"
        # Overrides for the commit that last touched a path. By default it is
        # derived from the blob sha, mirroring the real coupling: it moves
        # exactly when the file's content does, and not when HEAD does.
        self.last_commits: dict[str, str] = {}
        self.last_commit_calls = 0

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
        return self.head

    def fetch_last_commit_sha(self, full_name: str, path: str, *, ref: str) -> str:
        self.last_commit_calls += 1
        key = f"{full_name}/{path}"
        payload = self.files.get(key)
        if payload is None:
            raise GitHubAPIError(f"Could not resolve the last commit for {key}.")
        return self.last_commits.get(key, f"touched-{payload['sha']}")

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
    document = format_repo_content_document(file)
    assert "masumi-network/sokosumi-cli/README.md" in document
    assert "Commit: commit123" in document
    assert "# Title" in document


def test_repo_content_document_is_stable_for_an_unchanged_file() -> None:
    """Two syncs of an unchanged file must produce byte-identical documents.

    A `Retrieved: <timestamp>` line used to make every re-sync textually
    distinct, which defeated dedup at both ingest and query time and let one
    README occupy 8 of 8 result slots under 8 document ids.
    """
    file = RepoContentFile(
        repo="masumi-network/sokosumi-cli",
        path="README.md",
        sha="abc",
        ref="commit123",
        content="# Title",
        html_url="https://example.com",
    )

    first = format_repo_content_document(file)
    second = format_repo_content_document(file)

    assert first == second
    assert "Retrieved:" not in first, "a per-sync timestamp reintroduces duplicates"


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


# --- header pinning: the body must not depend on the repo HEAD ---------------
#
# 2026-07-31: one README blob (a4b30a45) existed as THREE documents under three
# commit values. `Commit:` and the sha inside `Source:` carried the repo HEAD,
# which moves when ANY file in the repo changes, so every re-render of an
# unchanged file minted a new content hash and cognee's content-addressed
# ingestion created a copy. The header is now pinned to the commit that last
# touched the file itself, so the render is a function of the file's own
# history — byte-stable across syncs even when the state file did not survive
# a deploy.


def _pinning_config(tmp_path: Path) -> CitadelConfig:
    return CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_dataset="masumi-network",
        repo_content_sync_session="masumi-repo-content",
        repo_content_sync_state_path=str(tmp_path / "state.json"),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md",),
        repo_content_sync_tree_prefixes=("skills/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
        repo_content_sync_run_improve=False,
    )


@pytest.mark.asyncio
async def test_unchanged_file_renders_byte_identically_when_only_head_moves(
    tmp_path: Path,
) -> None:
    """Two syncs of an unchanged file must produce the SAME BYTES, even when
    the repo HEAD moved in between and the state file did not survive.

    Lost state is the realistic worst case (deploys are ephemeral): the
    `unchanged` guard has nothing to compare, so the file IS re-rendered and
    re-ingested — and dedup then rests entirely on the render being
    byte-identical. A HEAD-valued header made exactly this path mint a
    duplicate document per unrelated push.
    """
    config = _pinning_config(tmp_path)
    client = FakeRepoContentClient()
    first_learning = FakeLearningProcess()
    first_syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=first_learning,  # type: ignore[arg-type]
    )
    first = await first_syncer.run()
    assert first["files_ingested"] == 2

    # An unrelated push moves HEAD; the tracked files themselves are untouched.
    client.head = "commit456"
    # The deploy was ephemeral: the checkpoint is gone.
    Path(config.repo_content_sync_state_path).unlink()

    second_learning = FakeLearningProcess()
    second_syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=second_learning,  # type: ignore[arg-type]
    )
    second = await second_syncer.run()

    # With no state, both files really were re-rendered and re-ingested...
    assert second["files_ingested"] == 2
    first_docs = [call["data"] for call in first_learning.calls]
    second_docs = [call["data"] for call in second_learning.calls]
    # ...and every rendered document is byte-identical to the first sync's,
    # so cognee resolves each to the SAME document instead of a copy.
    assert second_docs == first_docs
    for document in second_docs:
        assert "commit123" not in document, "the repo HEAD leaked into the body"
        assert "commit456" not in document, "the repo HEAD leaked into the body"


@pytest.mark.asyncio
async def test_a_file_whose_own_content_changed_renders_differently(
    tmp_path: Path,
) -> None:
    """Pinning must not over-deduplicate: a real edit is a new document."""
    config = _pinning_config(tmp_path)
    client = FakeRepoContentClient()
    learning = FakeLearningProcess()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=learning,  # type: ignore[arg-type]
    )
    await syncer.run()
    old_readme_doc = learning.calls[0]["data"]

    # The README itself is edited: new blob, new last-touching commit.
    client.files["masumi-network/sokosumi-cli/README.md"] = {
        "sha": "abc2",
        "content": "# Sokosumi CLI\n\nHeadless mode docs, second edition.",
    }
    client.head = "commit789"
    learning.calls.clear()
    second = await syncer.run()

    assert second["files_ingested"] == 1
    assert second["files_skipped_by_reason"] == {"unchanged": 1}
    new_readme_doc = learning.calls[0]["data"]
    assert new_readme_doc != old_readme_doc
    assert "second edition" in new_readme_doc
    assert "Blob: abc2" in new_readme_doc


@pytest.mark.asyncio
async def test_the_pin_lookup_is_paid_only_for_ingested_files(tmp_path: Path) -> None:
    """Unchanged files skip before the per-file commits call, so a steady-state
    sync adds zero GitHub requests over the pre-pinning behaviour."""
    config = _pinning_config(tmp_path)
    client = FakeRepoContentClient()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )
    await syncer.run()
    assert client.last_commit_calls == 2

    second = await syncer.run()
    assert second["files_skipped_by_reason"] == {"unchanged": 2}
    assert client.last_commit_calls == 2, "an unchanged file must not pay the lookup"


def test_fetch_last_commit_sha_asks_for_the_path_scoped_history() -> None:
    """Pin the request shape and the parsing of GitHub's commits payload."""
    requests: list[tuple[str, dict[str, Any]]] = []

    class RecordingClient(RepoContentGitHubClient):
        def _get_json(self, path: str, params: dict[str, Any]) -> Any:
            requests.append((path, params))
            return [{"sha": "feedface" * 5}]

    client = RecordingClient(token=None)
    sha = client.fetch_last_commit_sha("o/r", "docs/guide.md", ref="headsha")

    assert sha == "feedface" * 5
    assert requests == [
        ("/repos/o/r/commits", {"path": "docs/guide.md", "sha": "headsha", "per_page": 1})
    ]


def test_fetch_last_commit_sha_refuses_an_empty_history() -> None:
    """No commit means no immutable permalink; fail the file, never invent one."""

    class EmptyHistoryClient(RepoContentGitHubClient):
        def _get_json(self, path: str, params: dict[str, Any]) -> Any:
            return []

    with pytest.raises(GitHubAPIError):
        EmptyHistoryClient(token=None).fetch_last_commit_sha("o/r", "README.md", ref="headsha")


@pytest.mark.asyncio
async def test_a_failed_pin_lookup_skips_the_file_instead_of_shipping_head(
    tmp_path: Path,
) -> None:
    """A volatile ref must never reach the vault; the file retries next sync."""
    config = _pinning_config(tmp_path)

    class PinLookupDown(FakeRepoContentClient):
        def fetch_last_commit_sha(self, full_name: str, path: str, *, ref: str) -> str:
            raise GitHubAPIError("GitHub API returned 500")

    learning = FakeLearningProcess()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=PinLookupDown(),
        state_path=config.repo_content_sync_state_path,
        learning=learning,  # type: ignore[arg-type]
    )
    result = await syncer.run()

    assert result["files_ingested"] == 0
    assert learning.calls == []
    assert len(result["repositories"][0]["errors"]) == 2


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


@pytest.mark.asyncio
async def test_a_state_entry_without_a_cognee_id_is_re_ingested(tmp_path: Path) -> None:
    """State repair, and the reason 12 files stayed missing from the index.

    Ingest reports `accepted` as soon as cognee.add() returns, but add() writes
    only the relational and vector stores — the graph write is a DETACHED
    background cognify that can silently never run (no event loop, or the
    process exits first). A state entry holding just sha + content_hash
    therefore records "we told ourselves we ingested this", and `unchanged`
    made that permanent: the file was never retried, while being absent from
    the index.

    Requiring evidence of what cognee assigned repairs such entries on the next
    ordinary sync rather than needing a forced re-sync. Safe to re-ingest now
    that ADR-0016 made an unchanged file render byte-identically, so cognee's
    content-addressed ingestion resolves it to the same id instead of a copy.
    """
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

    # A legacy entry: correct sha and hash, no evidence it ever reached the graph.
    state_path.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "files": {
                    "masumi-network/sokosumi-cli/README.md": {
                        "sha": "abc",
                        "content_hash": FakeRepoContentClient()
                        .fetch_file_text(
                            "masumi-network/sokosumi-cli", "README.md", ref="commit123"
                        )
                        .content_hash,
                        "last_ingested_at": "2026-06-01T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    learning = FakeLearningProcess()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=learning,  # type: ignore[arg-type]
    )

    result = await syncer.run()

    assert result["files_ingested"] == 2, result["files_skipped_by_reason"]
    repaired = json.loads(state_path.read_text(encoding="utf-8"))["files"]
    entry = repaired["masumi-network/sokosumi-cli/README.md"]
    assert entry["cognee_data_ids"], "repaired entry must record what cognee assigned"

    # And once repaired, it goes quiet again rather than re-ingesting forever.
    second = await syncer.run()
    assert second["files_ingested"] == 0
    assert second["files_skipped_by_reason"] == {"unchanged": 2}


def test_cognee_data_ids_reads_the_real_cognee_return_type() -> None:
    """Pin the extraction against cognee's ACTUAL type, not a fake of our own.

    This is the test that was missing. `_cognee_data_ids` used to gate on
    `isinstance(added, dict)`, and every fake in the suite returned a dict, so
    the whole suite agreed with the bug. In production `cognee.add()` returns a
    `PipelineRunInfo` model and the gate was always taken, so the helper returned
    `[]` for every file and the sync could never converge.

    Constructing the real type here means a cognee upgrade that changes the shape
    fails HERE, loudly, instead of degrading into a silent livelock in prod.
    """
    data_id = "1b4196ac-5cb8-4e5f-abe2-a5ba8f42c2f2"
    real = PipelineRunCompleted(
        pipeline_run_id=uuid5(NAMESPACE_URL, "run"),
        dataset_id=uuid5(NAMESPACE_URL, "ds"),
        dataset_name="masumi-network",
        payload=None,
        data_ingestion_info=[{"data_id": data_id}],
    )

    def outcome(cognee_result: Any) -> Any:
        ingest = IngestResult(True, "accepted", "masumi-network", (), cognee_result)

        class Outcome:
            all_ingests = (ingest,)

        return Outcome()

    # It must NOT be a dict. If this ever becomes one, the fallback below is what
    # keeps working, but we want to know the contract changed.
    assert not isinstance(real, dict)

    # Every branch CogneePublicClient.remember can return (kb/cognee_client.py).
    for extra in ({"background_cognify": True}, {"cognify": "suppressed"}, {"cognify": "deferred"}):
        assert _cognee_data_ids(outcome({"added": real, **extra})) == [data_id]

    # Forward compatibility: a future cognee returning a plain mapping still works.
    assert _cognee_data_ids(
        outcome({"added": {"data_ingestion_info": [{"data_id": "d-9"}]}})
    ) == ["d-9"]

    # Genuinely empty stays empty, so the guard still refuses to trust a
    # claim it cannot check.
    assert _cognee_data_ids(outcome({"added": {}})) == []
    assert _cognee_data_ids(outcome(None)) == []


# --- #148: corrupt state must fail loudly, not re-ingest the whole allowlist -


@pytest.mark.asyncio
async def test_corrupt_state_raises_instead_of_full_reingest(tmp_path: Path) -> None:
    """#148: flattening corruption to empty made nothing "unchanged", so the
    entire allowlist re-ingested while reporting ok: True. The run must refuse
    the corrupt file and leave it as evidence."""
    from kb.state_io import StateFileError

    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_state_path=str(tmp_path / "repo_content_sync_state.json"),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md",),
        repo_content_sync_tree_prefixes=("skills/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
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
    corrupt = '{"version": 1, "files": {"masumi-network/sokosumi-cli/READ'
    Path(config.repo_content_sync_state_path).write_text(corrupt, encoding="utf-8")

    with pytest.raises(StateFileError):
        await syncer.run()

    assert len(learning.calls) == 2  # the seeding run only; nothing re-ingested
    assert Path(config.repo_content_sync_state_path).read_text(encoding="utf-8") == corrupt


@pytest.mark.asyncio
async def test_status_reports_corrupt_state_instead_of_raising(tmp_path: Path) -> None:
    """status() must catch the corruption so /api/sources shows red, not 500."""
    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_state_path=str(tmp_path / "state.json"),
        repo_content_sync_repos=("sokosumi-cli",),
    )
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=config.repo_content_sync_state_path,
    )
    Path(config.repo_content_sync_state_path).write_text("[1, 2", encoding="utf-8")

    status = await syncer.status()

    assert status["ok"] is False
    assert "state.json" in status["state_error"]
    assert status["tracked_files"] == 0
