from __future__ import annotations

import threading
from typing import Any

import pytest

from kb.config import CitadelConfig
from kb.google_chat import GoogleChatDelivery
from kb.learning_agent import LearningAgent
from kb.organization_digest import build_organization_digest, has_meaningful_source_changes


class FakeRepoContentSyncer:
    async def status(self) -> dict[str, Any]:
        return {"ok": True, "source_type": "github_repo_content", "enabled": True}

    async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "enabled": True,
            "files_ingested": 0,
            "files_skipped": 0,
            "improved": False,
            "dry_run": dry_run,
        }


def _learning_result() -> dict[str, Any]:
    return {
        "ok": True,
        "agent": "citadel-learning-agent",
        "sources": {
            "github": {
                "org": "masumi-network",
                "source_url": "https://github.com/orgs/masumi-network/repositories",
                "checked_at": "2026-06-03T08:00:00Z",
                "window_started_at": "2026-06-02T08:00:00Z",
                "repos_scanned": 3,
                "changed_count": 1,
                "event_count": 1,
                "commit_count": 1,
                "open_pull_request_count": 1,
                "merged_pull_request_count": 1,
                "open_pull_requests": [
                    {
                        "repo": "masumi-network/citadel",
                        "number": 42,
                        "title": "Ship organization digest",
                        "author": "sarthib7",
                        "url": "https://github.com/masumi-network/citadel/pull/42",
                    }
                ],
                "merged_pull_requests": [
                    {
                        "repo": "masumi-network/citadel",
                        "number": 41,
                        "title": "Add source packet",
                        "author": "sarthib7",
                        "url": "https://github.com/masumi-network/citadel/pull/41",
                    }
                ],
                "active_repositories": [
                    {
                        "repo": "masumi-network/citadel",
                        "score": 7,
                        "pull_requests": 2,
                        "commits": 1,
                        "events": 1,
                    }
                ],
                "recent_commits": [],
                "recent_events": [],
            },
            "vault": {
                "ok": True,
                "dataset": "masumi-network",
                "recent_context": [
                    {
                        "id": "decision-1",
                        "title": "Decision: use app auth for Google Chat",
                        "source": "citadel_search",
                        "metadata": {"dataset": "masumi-network"},
                    }
                ],
            },
        },
    }


def test_organization_digest_detects_meaningful_updates() -> None:
    assert has_meaningful_source_changes(_learning_result()) is True


def test_vault_context_alone_does_not_trigger_digest_post() -> None:
    result = {
        "sources": {
            "github": {
                "changed_count": 0,
                "event_count": 0,
                "commit_count": 0,
                "open_pull_request_count": 0,
                "merged_pull_request_count": 0,
            },
            "vault": {
                "recent_context": [
                    {
                        "id": "note-1",
                        "title": "Existing context without source freshness",
                    }
                ]
            },
        }
    }

    assert has_meaningful_source_changes(result) is False


def test_organization_digest_formats_constructive_preview(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    digest = build_organization_digest(
        _learning_result(),
        CitadelConfig(organization_digest_llm_enabled=False),
        include_preview=True,
    )

    assert digest["meaningful"] is True
    assert digest["agent_read_source"] == "deterministic_fallback"
    assert "Agent read" in digest["preview"]
    assert "Open PRs worth attention" in digest["preview"]
    assert "Ship organization digest" in digest["preview"]
    assert "Decision: use app auth for Google Chat" in digest["preview"]


def test_organization_digest_does_not_send_private_metadata_to_llm(monkeypatch: Any) -> None:
    result = _learning_result()
    result["sources"]["github"]["private_repo_count"] = 1
    result["sources"]["github"]["contains_private_repositories"] = True

    def fail_llm(packet: dict[str, Any]) -> list[str] | None:
        raise AssertionError("private repository metadata must not be sent to LLM")

    monkeypatch.setattr("kb.organization_digest.llm_agent_read", fail_llm)

    digest = build_organization_digest(
        result,
        CitadelConfig(organization_digest_llm_enabled=True),
        include_preview=True,
    )

    assert digest["agent_read_source"] == "deterministic_private_metadata"
    assert digest["summary"]["private_repositories"] == 1


def test_google_chat_delivery_posts_sanitized_threaded_message(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"name":"spaces/AAA/messages/BBB","thread":{"name":"spaces/AAA/threads/T"}}'

    def fake_urlopen(request: Any, *, timeout: int) -> FakeResponse:
        calls.append(
            {
                "url": request.full_url,
                "payload": request.data.decode("utf-8"),
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr("kb.google_chat.open_secure", fake_urlopen)
    delivery = GoogleChatDelivery(
        space_name="spaces/AAA",
        thread_key="citadel-org-digest",
        token_provider=lambda: "access-token",
    )

    result = delivery.post_digest("Digest body", message_id="2026-06-03T08:00:00Z")

    assert result["sent"] is True
    assert result["message_name"] == "spaces/AAA/messages/BBB"
    assert "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" in calls[0]["url"]
    assert "messageId=client-citadel-org-digest-2026-06-03t08-00-00z" in calls[0]["url"]
    assert "citadel-org-digest" in calls[0]["payload"]
    assert "access-token" not in str(result)


@pytest.mark.asyncio
async def test_learning_agent_manual_run_previews_without_posting(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    class FakeCitadel:
        config = CitadelConfig(organization_digest_llm_enabled=False)

        async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "id": "vault-note-1",
                    "title": "Decision: use app auth for Google Chat",
                    "content": "this body should not appear directly",
                    "metadata": {"dataset": kwargs["dataset"], "unsafe": "ignore"},
                }
            ]

    class FakeSyncer:
        async def status(self) -> dict[str, Any]:
            return {"ok": True}

        async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
            return _learning_result()["sources"]["github"]

    class FakeChat:
        def status(self) -> dict[str, Any]:
            return {"enabled": True}

        def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]:
            raise AssertionError("manual preview should not post")

    agent = LearningAgent(
        FakeCitadel(),
        github_syncer=FakeSyncer(),
        repo_content_syncer=FakeRepoContentSyncer(),
        google_chat=FakeChat(),
    )

    result = await agent.run()

    assert result["organization_digest"]["preview"]
    assert "this body should not appear directly" not in result["organization_digest"]["preview"]
    assert result["sources"]["vault"]["recent_context"][0]["title"] == (
        "Decision: use app auth for Google Chat"
    )
    assert result["notifications"]["google_chat"]["reason"] == "preview_only"


@pytest.mark.asyncio
async def test_learning_agent_posts_when_explicitly_requested(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    posted: list[str] = []

    class FakeCitadel:
        config = CitadelConfig(organization_digest_llm_enabled=False)

    class FakeSyncer:
        async def status(self) -> dict[str, Any]:
            return {"ok": True}

        async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
            return _learning_result()["sources"]["github"]

    class FakeChat:
        def status(self) -> dict[str, Any]:
            return {"enabled": True}

        def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]:
            posted.append(text)
            return {"ok": True, "sent": True, "status_category": "success"}

    agent = LearningAgent(
        FakeCitadel(),
        github_syncer=FakeSyncer(),
        repo_content_syncer=FakeRepoContentSyncer(),
        google_chat=FakeChat(),
    )

    result = await agent.run(post_to_chat=True, include_digest_preview=False)

    assert "preview" not in result["organization_digest"]
    assert result["notifications"]["google_chat"]["sent"] is True
    assert "Ship organization digest" in posted[0]


@pytest.mark.asyncio
async def test_learning_agent_posts_to_configured_gateways(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    posted: list[str] = []

    class FakeCitadel:
        config = CitadelConfig(organization_digest_llm_enabled=False)

    class FakeSyncer:
        async def status(self) -> dict[str, Any]:
            return {"ok": True}

        async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
            return _learning_result()["sources"]["github"]

    class FakeGateway:
        def status(self) -> dict[str, Any]:
            return {"enabled": True, "kind": "test"}

        def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]:
            posted.append(text)
            return {"ok": True, "sent": True, "status_category": "success"}

    agent = LearningAgent(
        FakeCitadel(),
        github_syncer=FakeSyncer(),
        repo_content_syncer=FakeRepoContentSyncer(),
        gateways={"internal_webhook": FakeGateway()},
    )

    result = await agent.run(post_to_chat=True, include_digest_preview=False)

    assert result["notifications"]["gateways"]["internal_webhook"]["sent"] is True
    assert result["notifications"]["google_chat"]["reason"] == "google_chat_disabled"
    assert "Ship organization digest" in posted[0]


@pytest.mark.asyncio
async def test_learning_agent_posts_gateways_concurrently(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    started: list[str] = []
    started_lock = threading.Lock()
    all_started = threading.Event()

    class FakeCitadel:
        config = CitadelConfig(organization_digest_llm_enabled=False)

    class FakeSyncer:
        async def status(self) -> dict[str, Any]:
            return {"ok": True}

        async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
            return _learning_result()["sources"]["github"]

    class BlockingGateway:
        def __init__(self, name: str) -> None:
            self.name = name

        def status(self) -> dict[str, Any]:
            return {"enabled": True, "kind": "test"}

        def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]:
            with started_lock:
                started.append(self.name)
                if len(started) == 2:
                    all_started.set()
            if not all_started.wait(timeout=1):
                return {"ok": False, "sent": False, "status_category": "not_concurrent"}
            return {"ok": True, "sent": True, "status_category": "success"}

    agent = LearningAgent(
        FakeCitadel(),
        github_syncer=FakeSyncer(),
        repo_content_syncer=FakeRepoContentSyncer(),
        gateways={
            "alpha": BlockingGateway("alpha"),
            "bravo": BlockingGateway("bravo"),
        },
    )

    result = await agent.run(post_to_chat=True, include_digest_preview=False)

    assert sorted(started) == ["alpha", "bravo"]
    assert result["notifications"]["gateways"]["alpha"]["sent"] is True
    assert result["notifications"]["gateways"]["bravo"]["sent"] is True
