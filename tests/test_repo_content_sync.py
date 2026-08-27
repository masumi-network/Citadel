from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
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
    DEFAULT_REPO_CONTENT_ALL_TEXT_EXTENSIONS,
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
        self.lifecycle_store: Any | None = None
        self.tombstone_calls: list[dict[str, Any]] = []

    async def tombstone_source(self, **kwargs: Any) -> tuple[Any, ...]:
        self.tombstone_calls.append(kwargs)
        return (object(),)


class FakeLearningProcess:
    def __init__(self, *, reasons: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.improve_calls: list[dict[str, Any]] = []
        self.reasons = list(reasons or [])

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

        reason = self.reasons.pop(0) if self.reasons else "accepted"

        class Outcome:
            ingest = IngestResult(
                True, reason, kwargs.get("dataset", "x"), (), cognee_result
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
        self.complete_tree_calls = 0
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

    def fetch_complete_tree(self, full_name: str, *, ref: str) -> list[str]:
        self.complete_tree_calls += 1
        prefix = f"{full_name}/"
        return sorted(key[len(prefix) :] for key in self.files if key.startswith(prefix))

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


def test_all_text_discovery_uses_complete_tree_and_blocks_non_source_paths() -> None:
    client = FakeRepoContentClient()
    paths = {
        ".env",
        ".env.example",
        "Dockerfile",
        "Makefile",
        "LICENSE",
        "README.md",
        "src/app.py",
        "src/config.json",
        "docs/guide.md",
        "node_modules/pkg/index.js",
        "dist/app.js",
        "target/generated.rs",
        "secrets/production.yaml",
        "package-lock.json",
        "image.png",
        "src/app.pyc",
        "src/app.min.js",
    }
    client.files = {
        f"o/r/{path}": {"sha": path, "content": "text"} for path in paths
    }

    selected = discover_repo_paths(
        client,
        "o/r",
        ref="sha",
        root_paths=(),
        tree_prefixes=(),
        tree_extensions=DEFAULT_REPO_CONTENT_ALL_TEXT_EXTENSIONS,
        max_files=0,
        all_text=True,
    )

    assert selected == [
        ".env.example",
        "Dockerfile",
        "LICENSE",
        "Makefile",
        "README.md",
        "docs/guide.md",
        "src/app.py",
        "src/config.json",
    ]
    assert client.complete_tree_calls == 1
    assert client.tree_calls == 0
    assert client.probe_calls == 0


def test_all_text_discovery_respects_positive_cap_but_zero_is_unbounded() -> None:
    client = FakeRepoContentClient()
    client.files = {
        f"o/r/file-{index}.py": {"sha": str(index), "content": "text"}
        for index in range(3)
    }

    capped = discover_repo_paths(
        client,
        "o/r",
        ref="sha",
        root_paths=(),
        tree_prefixes=(),
        tree_extensions=(".py",),
        max_files=2,
        all_text=True,
    )
    unbounded = discover_repo_paths(
        client,
        "o/r",
        ref="sha",
        root_paths=(),
        tree_prefixes=(),
        tree_extensions=(".py",),
        max_files=0,
        all_text=True,
    )

    assert capped == ["file-0.py", "file-1.py"]
    assert unbounded == ["file-0.py", "file-1.py", "file-2.py"]


@pytest.mark.asyncio
async def test_all_text_sync_uses_light_learning_tier(tmp_path: Path) -> None:
    config = CitadelConfig(
        repo_content_sync_all_text=True,
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_tree_extensions=(),
        repo_content_sync_max_files_per_repo=0,
        repo_content_sync_run_improve=False,
        repo_content_sync_state_path=str(tmp_path / "state.json"),
    )
    client = FakeRepoContentClient()
    client.files = {
        "masumi-network/sokosumi-cli/src/app.py": {
            "sha": "app",
            "content": "print('ok')",
        }
    }
    learning = FakeLearningProcess()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=learning,  # type: ignore[arg-type]
    )

    result = await syncer.run()

    assert result["files_ingested"] == 1
    assert learning.calls[0]["tier"] == "light"


@pytest.mark.asyncio
async def test_all_text_sync_fails_closed_when_one_repository_errors(tmp_path: Path) -> None:
    class MixedClient(FakeRepoContentClient):
        def fetch_default_branch(self, full_name: str) -> str:
            if full_name.endswith("broken"):
                raise GitHubAPIError("GitHub API returned 500")
            return super().fetch_default_branch(full_name)

    config = CitadelConfig(
        repo_content_sync_all_text=True,
        repo_content_sync_repos=("sokosumi-cli", "broken"),
        repo_content_sync_state_path=str(tmp_path / "state.json"),
        repo_content_sync_run_improve=False,
    )
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=MixedClient(),
        state_path=config.repo_content_sync_state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )

    result = await syncer.run()
    status = await syncer.status()

    assert result["repos_errored"] == 1
    assert result["content_scan_complete"] is False
    assert result["ok"] is False
    assert result["reason"] == "repository_content_scan_incomplete"
    assert status["ok"] is False
    assert status["last_run_ok"] is False
    assert status["last_run_reason"] == "repository_content_scan_incomplete"


@pytest.mark.asyncio
async def test_all_text_sync_reports_unreadable_and_oversized_files_as_unretained(
    tmp_path: Path,
) -> None:
    class PartialRetentionClient(FakeRepoContentClient):
        def fetch_file_text(
            self, full_name: str, path: str, *, ref: str
        ) -> RepoContentFile | None:
            if path == "README.md":
                return None
            return super().fetch_file_text(full_name, path, ref=ref)

    config = CitadelConfig(
        repo_content_sync_all_text=True,
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_tree_extensions=(),
        repo_content_sync_max_files_per_repo=0,
        repo_content_sync_max_bytes_per_file=256,
        repo_content_sync_run_improve=False,
        repo_content_sync_state_path=str(tmp_path / "state.json"),
    )
    client = PartialRetentionClient()
    client.files["masumi-network/sokosumi-cli/skills/sokosumi/SKILL.md"]["content"] = (
        "x" * 257
    )
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )

    result = await syncer.run()
    status = await syncer.status()

    assert result["ok"] is False
    assert result["reason"] == "repository_content_retention_incomplete"
    assert result["retention_complete"] is False
    assert result["files_unretained"] == 2
    assert result["files_skipped_by_reason"] == {
        "too_large": 1,
        "unsupported_encoding": 1,
    }
    assert status["retention_complete"] is False
    assert status["unretained_files"] == 2


@pytest.mark.asyncio
async def test_all_text_sync_reports_security_blocked_file_as_unretained(
    tmp_path: Path,
) -> None:
    config = CitadelConfig(
        repo_content_sync_all_text=True,
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_tree_extensions=(),
        repo_content_sync_max_files_per_repo=0,
        repo_content_sync_run_improve=False,
        repo_content_sync_state_path=str(tmp_path / "state.json"),
    )
    client = FakeRepoContentClient()
    client.files["masumi-network/sokosumi-cli/README.md"]["content"] = (
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    )
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )

    result = await syncer.run()
    status = await syncer.status()

    assert result["ok"] is False
    assert result["reason"] == "repository_content_retention_incomplete"
    assert result["retention_complete"] is False
    assert result["files_blocked"] == 1
    assert result["files_unretained"] == 1
    assert status["retention_complete"] is False
    assert status["unretained_files"] == 1


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
async def test_repo_content_sync_tombstones_confirmed_deleted_path(tmp_path: Path) -> None:
    config = _pinning_config(tmp_path)
    client = FakeRepoContentClient()
    citadel = FakeCitadel(config)
    citadel.lifecycle_store = object()
    syncer = RepoContentSyncer(
        citadel,
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )
    assert (await syncer.run())["files_ingested"] == 2
    client.files.pop("masumi-network/sokosumi-cli/README.md")

    second = await syncer.run()

    assert second["files_tombstoned"] == 1
    assert citadel.tombstone_calls[0]["source_key"] == (
        "github:masumi-network/sokosumi-cli:path:README.md"
    )
    state = json.loads(Path(config.repo_content_sync_state_path).read_text(encoding="utf-8"))
    assert "masumi-network/sokosumi-cli/README.md" not in state["files"]


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


def test_fetch_complete_tree_walks_non_recursive_trees_after_truncation() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    class CompleteTreeClient(RepoContentGitHubClient):
        def _get_json(self, path: str, params: dict[str, Any]) -> Any:
            requests.append((path, params))
            if path.endswith("/git/trees/headsha") and params == {"recursive": "1"}:
                return {"truncated": True, "tree": [{"type": "blob", "path": "partial.py"}]}
            if path.endswith("/git/commits/headsha"):
                return {"tree": {"sha": "root-tree"}}
            if path.endswith("/git/trees/root-tree"):
                return {
                    "truncated": False,
                    "tree": [
                        {"type": "tree", "path": "src", "sha": "src-tree"},
                        {"type": "blob", "path": "README.md", "mode": "100644"},
                    ],
                }
            if path.endswith("/git/trees/src-tree"):
                return {
                    "truncated": False,
                    "tree": [{"type": "blob", "path": "app.py", "mode": "100644"}],
                }
            raise AssertionError(f"unexpected request: {path} {params}")

    paths = CompleteTreeClient(token=None).fetch_complete_tree("o/r", ref="headsha")

    assert paths == ["README.md", "src/app.py"]
    assert requests == [
        ("/repos/o/r/git/trees/headsha", {"recursive": "1"}),
        ("/repos/o/r/git/commits/headsha", {}),
        ("/repos/o/r/git/trees/root-tree", {}),
        ("/repos/o/r/git/trees/src-tree", {}),
    ]


def test_fetch_complete_tree_refuses_truncated_non_recursive_tree() -> None:
    class TruncatedChildClient(RepoContentGitHubClient):
        def _get_json(self, path: str, params: dict[str, Any]) -> Any:
            if path.endswith("/git/trees/headsha") and params == {"recursive": "1"}:
                return {"truncated": True, "tree": []}
            if path.endswith("/git/commits/headsha"):
                return {"tree": {"sha": "root-tree"}}
            return {"truncated": True, "tree": []}

    with pytest.raises(GitHubAPIError, match="non-recursive tree"):
        TruncatedChildClient(token=None).fetch_complete_tree("o/r", ref="headsha")


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
    status = await syncer.status()

    assert result["ok"] is False
    assert result["reason"] == "all_repository_scans_failed"
    assert result["authenticated"] is False
    assert result["repos_errored"] == 2
    assert result["files_ingested"] == 0
    assert all(repo["errors"] for repo in result["repositories"])
    assert status["ok"] is False
    assert status["last_run_reason"] == "all_repository_scans_failed"


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
        repos = [
            self._repo("masumi-agent-messenger"),
            self._repo("no-markers-here"),
            self._repo("archived-repo", archived=True),
            self._repo("sokosumi-cli"),
        ]
        return repos if max_repos <= 0 else repos[:max_repos]

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


def test_discover_org_repos_without_markers_returns_all_non_archived_repos() -> None:
    joined = discover_org_repos(
        FakeAutoJoinClient(),
        "masumi-network",
        markers=(),
        max_repos=50,
    )
    assert joined == [
        "masumi-network/masumi-agent-messenger",
        "masumi-network/no-markers-here",
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


def test_resolved_repos_all_org_mode_skips_marker_filtering() -> None:
    config = CitadelConfig(
        repo_content_sync_all_repos=True,
        repo_content_sync_autojoin_max_repos=50,
    )
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeAutoJoinClient(),
        state_path="unused",
    )

    assert syncer._resolved_repos() == [
        "masumi-network/masumi-agent-messenger",
        "masumi-network/no-markers-here",
        "masumi-network/sokosumi-cli",
    ]


@pytest.mark.asyncio
async def test_all_org_sync_fails_when_repository_discovery_is_empty(tmp_path: Path) -> None:
    class EmptyOrgClient(FakeAutoJoinClient):
        def fetch_repos(
            self,
            org: str,
            *,
            max_repos: int,
            include_private: bool = True,
        ) -> list[GitHubRepo]:
            return []

    config = CitadelConfig(
        repo_content_sync_all_repos=True,
        repo_content_sync_state_path=str(tmp_path / "state.json"),
    )
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=EmptyOrgClient(),
        state_path=config.repo_content_sync_state_path,
    )

    result = await syncer.run()
    status = await syncer.status()

    assert result["ok"] is False
    assert result["reason"] == "repo_discovery_empty"
    assert result["repo_discovery_complete"] is False
    assert status["ok"] is False
    assert status["last_run_ok"] is False
    assert status["last_run_reason"] == "repo_discovery_empty"


def test_all_org_mode_does_not_fall_back_to_explicit_repos() -> None:
    config = CitadelConfig(
        repo_content_sync_all_repos=True,
        repo_content_sync_repos=("sokosumi-cli",),
    )

    class EmptyOrgClient(FakeAutoJoinClient):
        def fetch_repos(
            self,
            org: str,
            *,
            max_repos: int,
            include_private: bool = True,
        ) -> list[GitHubRepo]:
            return []

    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=EmptyOrgClient(),
        state_path="unused",
    )

    assert syncer._resolved_repos() == []


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
    assert learning.improve_calls[0]["session_ids"] is None
    assert result["improved"] is True


@pytest.mark.asyncio
async def test_user_repo_sync_disables_all_llm_work(tmp_path: Path) -> None:
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

    result = await syncer.run(allow_llm=False)

    assert result["files_ingested"] == 2
    assert all(call["allow_llm"] is False for call in learning.calls)
    assert all(call["defer_cognify"] is True for call in learning.calls)
    assert learning.improve_calls == []
    assert result["improved"] is False


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
@pytest.mark.parametrize("pending_reason", ["queued_not_confirmed", "not_scheduled"])
async def test_pending_projection_checkpoint_is_retried_before_unchanged(
    tmp_path: Path, pending_reason: str
) -> None:
    """A source receipt without confirmed projection must stay retryable."""
    state_path = tmp_path / "state.json"
    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_dataset="masumi-network",
        repo_content_sync_session="masumi-repo-content",
        repo_content_sync_state_path=str(state_path),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md",),
        # Keep this regression focused on one source file.
        repo_content_sync_tree_prefixes=("missing/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
        repo_content_sync_run_improve=False,
    )
    learning = FakeLearningProcess(reasons=[pending_reason, "accepted"])
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=learning,  # type: ignore[arg-type]
    )

    first = await syncer.run()

    assert first["files_ingested"] == 1
    pending_entry = json.loads(state_path.read_text(encoding="utf-8"))["files"][
        "masumi-network/sokosumi-cli/README.md"
    ]
    assert pending_entry["projection_status"] == "pending"
    assert pending_entry["projection_reason"] == pending_reason
    assert "cognee_data_ids" not in pending_entry

    second = await syncer.run()

    assert second["files_ingested"] == 1
    assert second["files_skipped_by_reason"] == {}
    assert len(learning.calls) == 2
    completed_entry = json.loads(state_path.read_text(encoding="utf-8"))["files"][
        "masumi-network/sokosumi-cli/README.md"
    ]
    assert completed_entry["cognee_data_ids"]
    assert "projection_status" not in completed_entry


@pytest.mark.asyncio
async def test_lifecycle_projection_checkpoint_polls_without_duplicate_ingest(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_dataset="masumi-network",
        repo_content_sync_session="masumi-repo-content",
        repo_content_sync_state_path=str(state_path),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md",),
        repo_content_sync_tree_prefixes=("missing/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
        repo_content_sync_run_improve=False,
    )

    class LifecycleCitadel(FakeCitadel):
        operation_state = "pending"
        resume_calls = 0

        def lifecycle_operation(self, projection_job_id: str) -> dict[str, Any]:
            assert projection_job_id == "job-1"
            return {
                "projection_job_id": projection_job_id,
                "source_revision_id": "source-1",
                "state": self.operation_state,
            }

        def resume_lifecycle_queue(self) -> None:
            self.resume_calls += 1

    class LifecycleLearning(FakeLearningProcess):
        async def learn(self, data: str, **kwargs: Any) -> Any:
            self.calls.append({"data": data, **kwargs})

            class Outcome:
                ingest = IngestResult(
                    True,
                    "queued_not_confirmed",
                    kwargs.get("dataset", "x"),
                    (),
                    source_revision_id="source-1",
                    projection_job_id="job-1",
                    projection_state="pending",
                )
                chunk_ingests = ()
                improve = None

                @property
                def all_ingests(self) -> tuple[IngestResult, ...]:
                    return (self.ingest,)

                @property
                def improved(self) -> bool:
                    return False

            return Outcome()

    citadel = LifecycleCitadel(config)
    learning = LifecycleLearning()
    syncer = RepoContentSyncer(
        citadel,
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=learning,  # type: ignore[arg-type]
    )

    first = await syncer.run()
    second = await syncer.run()
    # Under evolve Phase 1 the resume must NOT fire: this branch walks nearly
    # the whole tracked corpus every pass, and an unconditional resume started
    # a drain that parked on the writer lock Phase 1 holds (2026-08-13). The
    # scheduler resumes the queue itself once the pass ends.
    from kb.cognee_client import suppress_inline_cognify

    with suppress_inline_cognify():
        suppressed_pass = await syncer.run()
    citadel.operation_state = "searchable"
    third = await syncer.run()

    assert first["files_ingested"] == 1
    assert second["files_skipped_by_reason"] == {"projection_pending": 1}
    assert suppressed_pass["files_skipped_by_reason"] == {"projection_pending": 1}
    assert third["files_skipped_by_reason"] == {"unchanged": 1}
    assert len(learning.calls) == 1
    # Exactly one resume: the unsuppressed second run. The suppressed run
    # recorded the same skip without kicking the drain.
    assert citadel.resume_calls == 1


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


@pytest.mark.asyncio
async def test_run_makes_no_github_call_on_the_event_loop(tmp_path: Path) -> None:
    """No GitHub round trip may run on the event loop.

    That is the whole guarantee, and the name says exactly that much. The loop
    is NOT free for the duration of a run: ``scan_text_entries`` and the
    per-file state checkpoint still execute inline. Both are bounded local
    work, both run only for a file that actually changed, and neither ran at
    all in the production incident below (ingested=0 means zero scans and zero
    checkpoints), so 100% of that stall was network. Dispatching them would
    also not buy what dispatching I/O buys: the scan is CPU-bound Python, so a
    worker thread contends for the GIL instead of parking on a socket.

    ``RepoContentSyncer.run`` is awaited from the web process's single event
    loop by the evolve scheduler, and its GitHub client is synchronous urllib.
    Called inline, each round trip freezes EVERY route on the node. Measured in
    production 2026-08-03: ``Evolve stage repo_content_sync`` ran
    18:14:06.578Z to 18:14:36.610Z, 30.03s, during which a ``POST /mcp/`` took
    19.18s and a ``GET /api/contributions/recent`` gave up with a 499. That pass
    ingested nothing at all (ingested=0 skipped=69), so the node was frozen for
    half a minute to do no work.

    This asserts the OBSERVABLE property rather than the implementation: while
    ``run`` is in flight, a concurrent coroutine still gets scheduled. A
    heartbeat is the only thing that can tell "dispatched to a thread" apart
    from "still inline", because both return byte-identical results. Asserting
    on the presence of ``to_thread`` in the source would pass just as happily
    against a call that was moved but still awaited synchronously.
    """
    stall = 0.02

    class BlockingClient(FakeRepoContentClient):
        """Stands in for real urllib: every fetch blocks the calling thread."""

        def __init__(self) -> None:
            super().__init__()
            self.blocking_calls = 0

        def _stall(self) -> None:
            self.blocking_calls += 1
            time.sleep(stall)

        def fetch_default_branch(self, full_name: str) -> str:
            self._stall()
            return super().fetch_default_branch(full_name)

        def fetch_commit_sha(self, full_name: str, *, ref: str) -> str:
            self._stall()
            return super().fetch_commit_sha(full_name, ref=ref)

        def fetch_tree(self, full_name: str, *, ref: str) -> tuple[list[str], bool] | None:
            self._stall()
            return super().fetch_tree(full_name, ref=ref)

        def fetch_last_commit_sha(self, full_name: str, path: str, *, ref: str) -> str:
            self._stall()
            return super().fetch_last_commit_sha(full_name, path, ref=ref)

        def fetch_file_text(
            self, full_name: str, path: str, *, ref: str
        ) -> RepoContentFile | None:
            self._stall()
            return super().fetch_file_text(full_name, path, ref=ref)

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
    )
    client = BlockingClient()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        result = await syncer.run()
    finally:
        beat.cancel()

    assert result["files_ingested"] == 2, "the fix must not change what the sync does"
    assert client.blocking_calls >= 5, (
        "the fake must actually block, or this test proves nothing: "
        f"only {client.blocking_calls} blocking calls were made"
    )

    # Each stalled call holds a thread for 20ms. Off the loop, the heartbeat
    # keeps ticking through every one of them (~20 ticks per call). Inline, the
    # loop is frozen for the whole run and the heartbeat is starved. Five ticks
    # per blocking call sits far below the threaded floor and far above the
    # inline ceiling, so timing jitter cannot flip the verdict either way.
    floor = client.blocking_calls * 5
    assert ticks >= floor, (
        f"event loop was starved: {ticks} heartbeats across "
        f"{client.blocking_calls} blocking GitHub calls (need >= {floor}). "
        "Synchronous urllib is running inline on the loop, so every route on "
        "the node is frozen for the duration of the sync."
    )


@pytest.mark.asyncio
async def test_run_autojoin_discovery_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """Auto-join discovery is the LARGEST blocking site, and it runs first.

    ``_resolved_repos`` reads as a config accessor, but with
    ``repo_content_sync_autojoin_enabled`` it calls ``discover_org_repos``,
    which issues one synchronous ``fetch_repos`` plus one synchronous
    ``file_exists`` probe per repo per marker. At the shipped defaults
    (``repo_content_sync_autojoin_max_repos`` 100, three entries in
    ``DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS``) that is up to 301 round trips,
    all of them evaluated before ``run`` reaches its first dispatched call.

    The sibling heartbeat test cannot see this: its config leaves autojoin at
    the default False, so discovery never runs and the whole gap is invisible.

    Only the discovery calls block here. Every other fetch is instant, so the
    tick count measures discovery and nothing else, and a discovery left on
    the loop starves the heartbeat outright instead of merely thinning it.
    """
    stall = 0.02

    class BlockingAutoJoinClient(FakeRepoContentClient):
        def __init__(self) -> None:
            super().__init__()
            # A root marker on the repo already in the allowlist, so discovery
            # genuinely finds something and the union still dedups.
            self.files["masumi-network/sokosumi-cli/AGENTS.md"] = {
                "sha": "ghi",
                "content": "# Agents\n\nHouse rules.",
            }
            self.autojoin_calls = 0

        def _repo(self, name: str, *, archived: bool = False) -> GitHubRepo:
            return GitHubRepo(
                name=name,
                full_name=f"masumi-network/{name}",
                html_url="",
                description=None,
                language=None,
                pushed_at=None,
                updated_at=None,
                default_branch="main",
                visibility="public",
                archived=archived,
                stargazers_count=0,
                forks_count=0,
                open_issues_count=0,
                topics=(),
                license_name=None,
            )

        def fetch_repos(
            self, org: str, *, max_repos: int, include_private: bool = True
        ) -> list[GitHubRepo]:
            self.autojoin_calls += 1
            time.sleep(stall)
            return [
                self._repo("no-markers-here"),
                self._repo("also-no-markers"),
                self._repo("archived-repo", archived=True),
                self._repo("sokosumi-cli"),
            ][:max_repos]

        def file_exists(self, full_name: str, path: str, *, ref: str) -> bool:
            self.autojoin_calls += 1
            time.sleep(stall)
            return f"{full_name}/{path}" in self.files

    config = CitadelConfig(
        repo_content_sync_enabled=True,
        repo_content_sync_dataset="masumi-network",
        repo_content_sync_session="masumi-repo-content",
        repo_content_sync_state_path=str(tmp_path / "repo_content_sync_state.json"),
        repo_content_sync_repos=("sokosumi-cli",),
        repo_content_sync_root_paths=("README.md", "AGENTS.md"),
        repo_content_sync_tree_prefixes=("skills/",),
        repo_content_sync_tree_extensions=(".md",),
        repo_content_sync_max_files_per_repo=10,
        repo_content_sync_autojoin_enabled=True,
        repo_content_sync_autojoin_markers=DEFAULT_REPO_CONTENT_AUTOJOIN_MARKERS,
        repo_content_sync_autojoin_max_repos=50,
    )
    client = BlockingAutoJoinClient()
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=client,
        state_path=config.repo_content_sync_state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        result = await syncer.run()
    finally:
        beat.cancel()

    # 1 fetch_repos + 3 misses + 3 misses + 0 (archived is skipped before any
    # probe) + 1 hit on the first marker = 8.
    assert client.autojoin_calls == 8, (
        "discovery did not run as expected, so this test proves nothing: "
        f"{client.autojoin_calls} blocking auto-join calls"
    )
    assert result["files_ingested"] == 3, "the fix must not change what the sync does"

    # Each discovery call holds a thread for 20ms; nothing else in this run
    # blocks. Off the loop the heartbeat ticks ~20 times per call. Inline the
    # loop is frozen for all 160ms of it and only the handful of dispatched
    # GitHub calls (which return instantly here) can let it through.
    floor = client.autojoin_calls * 5
    assert ticks >= floor, (
        f"event loop was starved: {ticks} heartbeats across "
        f"{client.autojoin_calls} blocking auto-join calls (need >= {floor}). "
        "_resolved_repos is being evaluated inline on the loop, so every route "
        "on the node is frozen for the whole org scan before the sync starts."
    )


@pytest.mark.asyncio
async def test_a_second_overlapping_run_is_refused_instead_of_losing_state(
    tmp_path: Path,
) -> None:
    """Two passes over one state file must not race.

    ``run`` snapshots ``tracked`` from the state file at the start and writes
    the whole dict back at the end, so overlapping passes are a last-writer-
    wins lost update: the loser's ingested entries vanish from state and those
    files are fetched and re-sent on the next pass.

    Before the GitHub calls moved off the loop this could not happen: a pass
    that ingested nothing had no ``await`` that yielded, so a second caller
    could not start until it finished. Freeing the loop is what opens the
    window, which is why the guard ships in the same change.

    The two syncers are deliberately SEPARATE instances sharing one state
    path, because that is the real shape: ``get_repo_content_syncer`` in
    kb.server builds a new syncer per call and ``LearningAgent.__init__``
    builds its own, so a lock held as an instance attribute would guard
    nothing while looking like a guard.
    """
    state_path = str(tmp_path / "repo_content_sync_state.json")

    def _config() -> CitadelConfig:
        return CitadelConfig(
            repo_content_sync_enabled=True,
            repo_content_sync_dataset="masumi-network",
            repo_content_sync_session="masumi-repo-content",
            repo_content_sync_state_path=state_path,
            repo_content_sync_repos=("sokosumi-cli",),
            repo_content_sync_root_paths=("README.md",),
            repo_content_sync_tree_prefixes=("skills/",),
            repo_content_sync_tree_extensions=(".md",),
            repo_content_sync_max_files_per_repo=10,
        )

    class SlowClient(FakeRepoContentClient):
        """Holds the first pass open long enough for a second to be attempted."""

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def fetch_default_branch(self, full_name: str) -> str:
            self.calls += 1
            time.sleep(0.2)
            return super().fetch_default_branch(full_name)

    first_client = SlowClient()
    second_client = SlowClient()
    second_learning = FakeLearningProcess()
    first = RepoContentSyncer(
        FakeCitadel(_config()),
        client=first_client,
        state_path=state_path,
        learning=FakeLearningProcess(),  # type: ignore[arg-type]
    )
    second = RepoContentSyncer(
        FakeCitadel(_config()),
        client=second_client,
        state_path=state_path,
        learning=second_learning,  # type: ignore[arg-type]
    )

    in_flight = asyncio.create_task(first.run())
    # Let the first pass get inside its first dispatched call. It is only
    # reachable at all because that call yields.
    await asyncio.sleep(0.05)
    assert not in_flight.done(), "the first pass finished too early to overlap"

    overlapping = await second.run()
    first_result = await in_flight

    assert overlapping.get("skipped") is True
    assert overlapping.get("reason") == "repo_content_sync_already_running"
    assert second_client.calls == 0, "the refused pass must not touch GitHub"
    assert second_learning.calls == [], "the refused pass must not ingest anything"
    # Not recordable as a sync: no checked_at, no counts. kb.server must not
    # stamp the source "synced" for a pass that did no work.
    assert "checked_at" not in overlapping
    assert "files_ingested" not in overlapping

    assert first_result["files_ingested"] == 2
    written = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert sorted(written["files"]) == [
        "masumi-network/sokosumi-cli/README.md",
        "masumi-network/sokosumi-cli/skills/sokosumi/SKILL.md",
    ]

    # And the lock frees afterwards: this is a guard, not a one-shot latch.
    again = await second.run()
    assert again.get("skipped") is None
    assert again["files_skipped"] == 2


# --------------------------------------------------------------------------
# A refusal the content itself caused is terminal until the content changes.
# --------------------------------------------------------------------------


class RefusingLearningProcess(FakeLearningProcess):
    """Refuses every document with a fixed reason, like the real ingest guards."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason

    async def learn(self, data: str, **kwargs: Any) -> Any:
        self.calls.append({"data": data, **kwargs})
        rejection = IngestResult(False, self.reason, kwargs.get("dataset", "x"), ())

        class Outcome:
            ingest = rejection
            chunk_ingests = ()
            improve = None

            @property
            def all_ingests(self) -> tuple[IngestResult, ...]:
                return (self.ingest,)

            @property
            def improved(self) -> bool:
                return False

        return Outcome()


def _refusal_config(state_path: Path) -> CitadelConfig:
    return CitadelConfig(
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


@pytest.mark.asyncio
async def test_a_refusal_records_the_reason_the_ingest_actually_gave(
    tmp_path: Path,
) -> None:
    """``ingest_rejected`` is not a reason, it is a category.

    Reading ``{"ingest_rejected": 2}`` in a sync report cannot tell anyone whether
    a human has to look at a file (``unchunkable_content``) or whether the same
    bytes were simply already in flight (``duplicate_in_process``).
    """
    state_path = tmp_path / "state.json"
    config = _refusal_config(state_path)
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=RefusingLearningProcess("unchunkable_content"),  # type: ignore[arg-type]
    )

    result = await syncer.run()

    assert result["files_ingested"] == 0
    assert result["files_skipped_by_reason"] == {"ingest_rejected:unchunkable_content": 2}


@pytest.mark.asyncio
async def test_content_the_ingest_can_never_accept_is_not_resubmitted_every_sync(
    tmp_path: Path,
) -> None:
    """The refusal branch used to record nothing in ``tracked``.

    ``unchunkable_content`` is a function of the bytes, so re-submitting the same
    bytes buys a second identical refusal. The cost is not the refusal: reaching
    it spends a ``fetch_file_text`` and a ``fetch_last_commit_sha`` per file per
    pass, on a scheduler that runs about every 1h45m, forever.
    """
    state_path = tmp_path / "state.json"
    config = _refusal_config(state_path)
    learning = RefusingLearningProcess("unchunkable_content")
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=learning,  # type: ignore[arg-type]
    )

    first = await syncer.run()
    assert len(learning.calls) == 2

    second = await syncer.run()

    assert len(learning.calls) == 2, "the same bytes were submitted for a second refusal"
    assert second["files_skipped_by_reason"] == {
        "refused_unchanged:unchunkable_content": 2
    }
    entry = json.loads(state_path.read_text(encoding="utf-8"))["files"][
        "masumi-network/sokosumi-cli/README.md"
    ]
    assert entry["rejected_reason"] == "unchunkable_content"
    assert entry["sha"] and entry["content_hash"]
    assert not entry.get("cognee_data_ids"), "a refused file must not look ingested"
    assert first["ok"] is False
    assert first["reason"] == "repository_content_retention_incomplete"
    assert first["retention_complete"] is False
    assert first["files_rejected"] == 2
    assert second["ok"] is False
    assert second["retention_complete"] is False


@pytest.mark.asyncio
async def test_a_refusal_that_is_not_about_the_content_is_retried(
    tmp_path: Path,
) -> None:
    """``duplicate_in_process`` is scoped to one process, so it must not stick.

    ``Citadel._seen_ingest_keys`` is per-process state. Recording it as terminal
    would let one restart's bookkeeping suppress a file for every later run.
    """
    state_path = tmp_path / "state.json"
    config = _refusal_config(state_path)
    learning = RefusingLearningProcess("duplicate_in_process")
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=learning,  # type: ignore[arg-type]
    )

    await syncer.run()
    second = await syncer.run()

    assert len(learning.calls) == 4, "a process-scoped refusal was made permanent"
    assert second["files_skipped_by_reason"] == {
        "ingest_rejected:duplicate_in_process": 2
    }


@pytest.mark.asyncio
async def test_raising_the_chunk_budget_retries_what_the_old_budget_refused(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The budget is an observation, and the knob to change it has to do something.

    ``kb.chunk_window`` says out loud to raise CITADEL_CHUNK_BUDGET_TOKENS when the
    detector reports a content class its sweep did not contain. A terminal skip
    that ignored the budget would make that raise a no-op for every file the old
    budget had already refused, which is a guard shipping inert.
    """
    state_path = tmp_path / "state.json"
    config = _refusal_config(state_path)
    learning = RefusingLearningProcess("unchunkable_content")
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=learning,  # type: ignore[arg-type]
    )

    monkeypatch.setenv("CITADEL_CHUNK_BUDGET_TOKENS", "256")
    await syncer.run()
    assert len(learning.calls) == 2

    monkeypatch.setenv("CITADEL_CHUNK_BUDGET_TOKENS", "512")
    await syncer.run()

    assert len(learning.calls) == 4, "the raised budget never retried the refused files"


@pytest.mark.asyncio
async def test_a_refused_file_is_not_counted_as_a_tracked_file(tmp_path: Path) -> None:
    """``tracked_files`` is read as how much of the corpus this connector holds.

    Refusal entries live in the same ``files`` map as ingested ones, so counting
    the map would report a file as tracked precisely because it is absent from
    the index. They are reported, but under their own name.
    """
    state_path = tmp_path / "state.json"
    config = _refusal_config(state_path)
    syncer = RepoContentSyncer(
        FakeCitadel(config),
        client=FakeRepoContentClient(),
        state_path=str(state_path),
        learning=RefusingLearningProcess("unchunkable_content"),  # type: ignore[arg-type]
    )

    await syncer.run()
    status = await syncer.status()

    assert status["tracked_files"] == 0, "a refused file is not in the index"
    assert status["refused_files"] == 2
