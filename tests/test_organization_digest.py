from __future__ import annotations

import io
import json
import logging
import threading
from typing import Any
from urllib.error import HTTPError

import pytest

from kb.config import CitadelConfig
from kb.google_chat import GoogleChatDelivery
from kb.learning_agent import LearningAgent
from kb.llm_enrichment import DEFAULT_LLM_MODEL
from kb.organization_digest import (
    build_organization_digest,
    has_meaningful_source_changes,
    llm_agent_read,
    resolve_openrouter_model,
)


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


# --- OpenRouter model resolution + diagnosable failure logging -----------------


def _clear_llm_env(monkeypatch: Any) -> None:
    for name in (
        "CITADEL_ORG_DIGEST_LLM_MODEL",
        "LLM_MODEL",
        "CITADEL_LLM_MODEL",
        "LLM_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)


def _digest_packet() -> dict[str, Any]:
    return {
        "kind": "organization_update_digest_source_packet",
        "summary": {"org": "masumi-network"},
    }


class _FakeCompletionResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeCompletionResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_resolve_model_strips_litellm_prefix_from_llm_model(monkeypatch: Any) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "openrouter/deepseek/deepseek-v4-flash")

    assert resolve_openrouter_model() == (
        "openrouter/deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-flash",
    )


def test_resolve_model_keeps_bare_openrouter_id(monkeypatch: Any) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-flash")

    assert resolve_openrouter_model() == (
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-flash",
    )


def test_resolve_model_org_digest_override_wins_in_either_form(monkeypatch: Any) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "openrouter/vendor/from-llm-model")

    monkeypatch.setenv("CITADEL_ORG_DIGEST_LLM_MODEL", "openrouter/qwen/qwen3-coder")
    assert resolve_openrouter_model() == ("openrouter/qwen/qwen3-coder", "qwen/qwen3-coder")

    monkeypatch.setenv("CITADEL_ORG_DIGEST_LLM_MODEL", "qwen/qwen3-coder")
    assert resolve_openrouter_model() == ("qwen/qwen3-coder", "qwen/qwen3-coder")


def test_resolve_model_keeps_native_openrouter_vendor_id(monkeypatch: Any) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "openrouter/auto")

    assert resolve_openrouter_model() == ("openrouter/auto", "openrouter/auto")


def test_resolve_model_default_and_citadel_llm_model_fallbacks(monkeypatch: Any) -> None:
    _clear_llm_env(monkeypatch)
    assert resolve_openrouter_model() == (DEFAULT_LLM_MODEL, DEFAULT_LLM_MODEL)

    monkeypatch.setenv("CITADEL_LLM_MODEL", "openrouter/z-ai/glm-5")
    assert resolve_openrouter_model() == ("openrouter/z-ai/glm-5", "z-ai/glm-5")


def test_llm_agent_read_sends_stripped_model_to_openrouter(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-openrouter-key")
    monkeypatch.setenv("LLM_MODEL", "openrouter/deepseek/deepseek-v4-flash")
    seen: list[dict[str, Any]] = []

    def fake_open_secure(request: Any, *, timeout: float) -> _FakeCompletionResponse:
        seen.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _FakeCompletionResponse(
            {"choices": [{"message": {"content": "- one\n- two\n- three"}}]}
        )

    monkeypatch.setattr("kb.organization_digest.open_secure", fake_open_secure)

    with caplog.at_level(logging.INFO, logger="kb.organization_digest"):
        lines = llm_agent_read(_digest_packet())

    assert lines == ["one", "two", "three"]
    assert len(seen) == 1
    assert seen[0]["payload"]["model"] == "deepseek/deepseek-v4-flash"
    assert seen[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert "stripped litellm provider prefix" in caplog.text


def test_llm_agent_read_sends_bare_model_unchanged(monkeypatch: Any) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-openrouter-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-flash")
    seen: list[dict[str, Any]] = []

    def fake_open_secure(request: Any, *, timeout: float) -> _FakeCompletionResponse:
        seen.append(json.loads(request.data.decode("utf-8")))
        return _FakeCompletionResponse(
            {"choices": [{"message": {"content": "- one\n- two\n- three"}}]}
        )

    monkeypatch.setattr("kb.organization_digest.open_secure", fake_open_secure)

    assert llm_agent_read(_digest_packet()) == ["one", "two", "three"]
    assert seen[0]["model"] == "deepseek/deepseek-v4-flash"


def test_llm_agent_read_logs_model_and_response_body_on_http_400(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-openrouter-key")
    monkeypatch.setenv("LLM_MODEL", "openrouter/deepseek/deepseek-v4-flash")
    calls: list[str] = []

    def fake_open_secure(request: Any, *, timeout: float) -> Any:
        calls.append(request.full_url)
        raise HTTPError(
            request.full_url,
            400,
            "Bad Request",
            None,
            io.BytesIO(
                b'{"error":{"message":'
                b'"deepseek/deepseek-v4-flash is not a valid model ID","code":400}}'
            ),
        )

    monkeypatch.setattr("kb.organization_digest.open_secure", fake_open_secure)

    with caplog.at_level(logging.WARNING, logger="kb.organization_digest"):
        assert llm_agent_read(_digest_packet()) is None

    # One attempt only: a 400 is not transient, so no retries.
    assert calls == ["https://openrouter.ai/api/v1/chat/completions"]
    assert "HTTP 400" in caplog.text
    # Resolved and configured ids are both recorded, distinguishably.
    assert (
        "for model deepseek/deepseek-v4-flash (configured openrouter/deepseek/deepseek-v4-flash)"
    ) in caplog.text
    # The response body reaches the log, so the next occurrence is diagnosable.
    assert "is not a valid model ID" in caplog.text


def test_llm_agent_read_redacts_credentials_in_logged_error_body(
    monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-openrouter-key")
    monkeypatch.setenv("LLM_MODEL", "openrouter/deepseek/deepseek-v4-flash")
    # Synthesized fixture: matches the ctdl_ token shape, never a real value.
    leaked = "ctdl_unit_test_fake_token_1234567890"
    body = json.dumps(
        {"error": {"message": f"upstream rejected token {leaked}", "code": 400}}
    ).encode("utf-8")

    def fake_open_secure(request: Any, *, timeout: float) -> Any:
        raise HTTPError(request.full_url, 400, "Bad Request", None, io.BytesIO(body))

    monkeypatch.setattr("kb.organization_digest.open_secure", fake_open_secure)

    with caplog.at_level(logging.WARNING, logger="kb.organization_digest"):
        assert llm_agent_read(_digest_packet()) is None

    assert leaked not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_digest_falls_back_deterministically_when_llm_call_400s(monkeypatch: Any) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-openrouter-key")
    monkeypatch.setenv("LLM_MODEL", "openrouter/deepseek/deepseek-v4-flash")

    def fake_open_secure(request: Any, *, timeout: float) -> Any:
        raise HTTPError(request.full_url, 400, "Bad Request", None, io.BytesIO(b"{}"))

    monkeypatch.setattr("kb.organization_digest.open_secure", fake_open_secure)

    digest = build_organization_digest(
        _learning_result(),
        CitadelConfig(organization_digest_llm_enabled=True),
        include_preview=True,
    )

    assert digest["agent_read_source"] == "deterministic_fallback"
    assert "Agent read" in digest["preview"]
