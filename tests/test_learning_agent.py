from __future__ import annotations

from typing import Any

import pytest

from kb.config import CitadelConfig
from kb.learning_agent import LearningAgent


def github_run_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ok": True,
        "org": "masumi-network",
        "source_url": "https://github.com/orgs/masumi-network/repositories",
        "checked_at": "2026-06-10T08:00:00Z",
        "window_started_at": "2026-06-09T08:00:00Z",
        "repos_scanned": 2,
        "private_repo_count": 0,
        "contains_private_repositories": False,
        "changed_count": 1,
        "event_count": 1,
        "commit_count": 1,
        "open_pull_request_count": 1,
        "merged_pull_request_count": 0,
        "changed_repositories": [
            {"name": "agent", "full_name": "masumi-network/agent"}
        ],
        "recent_commits": [
            {"repo": "masumi-network/agent", "sha": "abc123def456", "message": "add retry"}
        ],
        "open_pull_requests": [
            {
                "repo": "masumi-network/agent",
                "number": 7,
                "title": "Add retry helper",
                "author": "dev",
                "url": "https://github.com/masumi-network/agent/pull/7",
            }
        ],
        "merged_pull_requests": [],
        "active_repositories": [
            {
                "repo": "masumi-network/agent",
                "score": 5,
                "changed_repos": 1,
                "pull_requests": 1,
                "commits": 1,
                "events": 1,
            }
        ],
        "recent_events": [],
        "ingested": True,
        "improved": False,
        "dry_run": False,
    }
    base.update(overrides)
    return base


def repo_content_run_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ok": True,
        "enabled": True,
        "org": "masumi-network",
        "checked_at": "2026-06-10T08:00:00Z",
        "repos_scanned": 2,
        "files_ingested": 3,
        "files_skipped": 1,
        "files_blocked": 0,
        "improved": True,
        "dry_run": False,
        "repositories": [],
    }
    base.update(overrides)
    return base


class FakeCitadel:
    config = CitadelConfig(
        organization_digest_llm_enabled=False,
        search_default_dataset="masumi-network",
    )

    def __init__(self, *, search_error: Exception | None = None) -> None:
        self.search_error = search_error
        self.search_calls: list[dict[str, Any]] = []

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append({"query": query, **kwargs})
        if self.search_error:
            raise self.search_error
        return [
            {
                "id": "vault-note-1",
                "title": "Decision: harden retries",
                "content": "raw body must not leak into the digest packet",
                "metadata": {"dataset": kwargs["dataset"]},
            }
        ]


class FakeSyncer:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.runs: list[dict[str, Any]] = []

    async def status(self) -> dict[str, Any]:
        return {"ok": True, "org": "masumi-network"}

    async def run(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        allow_llm: bool = True,
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        call = {"force": force, "dry_run": dry_run}
        if not allow_llm:
            call["allow_llm"] = False
        self.runs.append(call)
        return github_run_result(dry_run=dry_run)


class FailedResultSyncer(FakeSyncer):
    async def run(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        allow_llm: bool = True,
    ) -> dict[str, Any]:
        call = {"force": force, "dry_run": dry_run}
        if not allow_llm:
            call["allow_llm"] = False
        self.runs.append(call)
        return github_run_result(ok=False, dry_run=dry_run)


class UnhealthySyncer(FakeSyncer):
    async def status(self) -> dict[str, Any]:
        return {"ok": False, "reason": "sync state is stale"}


class FakeRepoContentSyncer:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.runs: list[dict[str, Any]] = []

    async def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "source_type": "github_repo_content",
            "org": "masumi-network",
            "enabled": True,
            "tracked_files": 12,
        }

    async def run(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        allow_llm: bool = True,
    ) -> dict[str, Any]:
        if self.error:
            raise self.error
        call = {"force": force, "dry_run": dry_run}
        if not allow_llm:
            call["allow_llm"] = False
        self.runs.append(call)
        return repo_content_run_result(dry_run=dry_run)


class FakeGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.posts: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {"enabled": True}

    def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.posts.append({"text": text, "message_id": message_id})
        return {"ok": True, "sent": True, "status_category": "success"}


def agent(
    *,
    citadel: FakeCitadel | None = None,
    syncer: FakeSyncer | None = None,
    repo_content_syncer: FakeRepoContentSyncer | None = None,
    gateway: FakeGateway | None = None,
) -> tuple[LearningAgent, FakeCitadel, FakeSyncer, FakeRepoContentSyncer, FakeGateway]:
    fake_citadel = citadel or FakeCitadel()
    fake_syncer = syncer or FakeSyncer()
    fake_repo_content_syncer = repo_content_syncer or FakeRepoContentSyncer()
    fake_gateway = gateway or FakeGateway()
    learning_agent = LearningAgent(
        fake_citadel,
        github_syncer=fake_syncer,
        repo_content_syncer=fake_repo_content_syncer,
        google_chat=fake_gateway,
    )
    return learning_agent, fake_citadel, fake_syncer, fake_repo_content_syncer, fake_gateway


async def test_run_happy_path_builds_digest_and_posts_to_gateway() -> None:
    learning_agent, fake_citadel, fake_syncer, fake_repo_content, fake_gateway = agent()

    result = await learning_agent.run(post_to_chat=True)

    assert result["ok"] is True
    assert result["ingested"] is True
    assert result["improved"] is True
    assert fake_syncer.runs == [{"force": False, "dry_run": False}]
    assert fake_repo_content.runs == [{"force": False, "dry_run": False}]
    assert result["sources"]["repo_content"]["files_ingested"] == 3
    assert result["organization_digest"]["meaningful"] is True
    assert result["notifications"]["google_chat"]["sent"] is True
    assert len(fake_gateway.posts) == 1
    assert "Masumi Org Digest" in fake_gateway.posts[0]["text"]
    assert fake_citadel.search_calls[0]["top_k"] == 5
    assert "raw body must not leak" not in fake_gateway.posts[0]["text"]


async def test_user_run_disables_source_and_digest_llm_work(monkeypatch: Any) -> None:
    class UserCitadel(FakeCitadel):
        config = CitadelConfig(
            organization_digest_llm_enabled=True,
            search_default_dataset="masumi-network",
        )

    def explode(_packet: dict[str, Any]) -> list[str]:
        raise AssertionError("user-triggered learning-agent run called an LLM")

    monkeypatch.setattr("kb.organization_digest.llm_agent_read", explode)
    learning_agent, _, github, repo_content, _ = agent(citadel=UserCitadel())

    result = await learning_agent.run(allow_llm=False)

    assert github.runs == [{"force": False, "dry_run": False, "allow_llm": False}]
    assert repo_content.runs == [
        {"force": False, "dry_run": False, "allow_llm": False}
    ]
    assert result["organization_digest"]["agent_read_source"] == "deterministic_fallback"


async def test_run_preview_mode_does_not_post() -> None:
    learning_agent, _, _, _, fake_gateway = agent()

    result = await learning_agent.run(post_to_chat=False)

    assert fake_gateway.posts == []
    assert result["notifications"]["google_chat"]["reason"] == "preview_only"


async def test_dry_run_skips_gateway_delivery() -> None:
    learning_agent, _, _, _, fake_gateway = agent()

    result = await learning_agent.run(post_to_chat=True, dry_run=True)

    assert fake_gateway.posts == []
    assert result["notifications"]["google_chat"]["reason"] == "dry_run"


async def test_github_sync_failure_propagates() -> None:
    learning_agent, _, _, _, _ = agent(syncer=FakeSyncer(error=RuntimeError("github down")))

    with pytest.raises(RuntimeError, match="github down"):
        await learning_agent.run()


async def test_repo_content_sync_failure_propagates() -> None:
    learning_agent, _, _, _, _ = agent(
        repo_content_syncer=FakeRepoContentSyncer(error=RuntimeError("repo content down"))
    )

    with pytest.raises(RuntimeError, match="repo content down"):
        await learning_agent.run()


async def test_vault_search_failure_degrades_to_empty_context() -> None:
    learning_agent, _, _, _, _ = agent(citadel=FakeCitadel(search_error=RuntimeError("cognee down")))

    result = await learning_agent.run()

    vault = result["sources"]["vault"]
    assert vault["ok"] is False
    assert vault["recent_context"] == []
    assert vault["error_type"] == "RuntimeError"
    assert result["ok"] is True
    assert result["degraded"] is True
    assert result["degraded_reasons"] == ["vault_context_search"]


async def test_run_reports_returned_source_failure() -> None:
    learning_agent, _, _, _, _ = agent(syncer=FailedResultSyncer())

    result = await learning_agent.run()

    assert result["ok"] is False
    assert result["source_sync_ok"] is False
    assert result["degraded_reasons"] == ["github_sync"]


async def test_status_reports_returned_source_failure() -> None:
    learning_agent, _, _, _, _ = agent(syncer=UnhealthySyncer())

    result = await learning_agent.status()

    assert result["ok"] is False


async def test_gateway_exception_is_reported_not_raised() -> None:
    learning_agent, _, _, _, _ = agent(gateway=FakeGateway(error=RuntimeError("chat down")))

    result = await learning_agent.run(post_to_chat=True)

    google_chat = result["notifications"]["google_chat"]
    assert google_chat["sent"] is False
    assert google_chat["status_category"] == "delivery_exception"
    assert google_chat["error_type"] == "RuntimeError"


async def test_gateway_delivery_test_reports_disabled_gateway() -> None:
    learning_agent = LearningAgent(
        FakeCitadel(),
        github_syncer=FakeSyncer(),
        repo_content_syncer=FakeRepoContentSyncer(),
        gateways={},
    )

    result = await learning_agent.test_gateway_delivery("google_chat")

    assert result["sent"] is False
    assert result["reason"] == "google_chat_disabled"


async def test_status_reports_sources_and_gateways() -> None:
    learning_agent, _, _, _, _ = agent()

    status = await learning_agent.status()

    assert status["ok"] is True
    assert status["sources"]["github"]["org"] == "masumi-network"
    assert status["sources"]["repo_content"]["source_type"] == "github_repo_content"
    assert status["notifications"]["google_chat"] == {"enabled": True}
