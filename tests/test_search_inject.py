from __future__ import annotations

import io
import json
from typing import Any

import pytest

from kb.hooks import search_inject
from kb.onboard import merge_claude_settings
from kb.hooks.sync_start import AGENT_POLICY_REMINDER


class _Response:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class _Opener:
    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[Any, float]] = []

    def open(self, request: Any, *, timeout: float) -> _Response:
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_extract_task_query_keeps_issue_repo_and_path_without_long_logs() -> None:
    prompt = """
    Fix #123 in masumi-network/Citadel at kb/hooks/search_inject.py.
    Preserve task-aware search terms.
    ```python
    def noisy_example():
        return 'remove this fenced code'
    ```
    2026-08-30T10:00:00Z ERROR worker failed: this long log should not enter the query
    Traceback (most recent call last):
    $ bash -lc 'pytest -q tests/test_search_inject.py'
    `pytest -q tests/test_search_inject.py`
    https://example.com/issues/123
    """

    query = search_inject.extract_task_query(prompt)

    assert query is not None
    assert "#123" in query
    assert "masumi-network/Citadel" in query
    assert "kb/hooks/search_inject.py" in query
    assert "noisy_example" not in query
    assert "2026-08-30T10:00:00Z" not in query
    assert "bash -lc" not in query
    assert "pytest -q" not in query


def test_run_redacts_outbound_query_before_transmission(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "ctdl_abcdefghijklmnopqrstuvwxyz012345"
    monkeypatch.setenv(search_inject.TOKEN_ENV, token)
    monkeypatch.setenv(search_inject.BASE_URL_ENV, "https://vault.example")
    captured: list[str] = []

    def fake_fetch(base_url: str, sent_token: str, query: str, *, limit: int = 3) -> dict[str, Any]:
        captured.append(query)
        return {"search_id": "search:redaction", "results": []}

    monkeypatch.setattr(search_inject, "fetch_task_hits", fake_fetch)
    prompt = (
        "Fix search for api_key=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789 "
        f"github ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 and my token {token} in kb/service.py"
    )
    assert search_inject.run(io.StringIO(json.dumps({"prompt": prompt}))) == 0
    assert captured, "query was not sent"
    sent = captured[0]
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789" not in sent
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" not in sent
    assert token not in sent
    assert "kb/service.py" in sent


def test_extract_task_query_drops_unbounded_log_lines() -> None:
    prompt = "Investigate #123 in kb/hooks/search_inject.py.\n" + ("raw log " + "x" * 5000)

    query = search_inject.extract_task_query(prompt)

    assert query is not None
    assert "#123" in query
    assert "kb/hooks/search_inject.py" in query
    assert "raw log" not in query


def test_extract_task_query_preserves_identifiers_from_task_url() -> None:
    query = search_inject.extract_task_query(
        "Fix https://github.com/masumi-network/Citadel/issues/123"
    )

    assert query is not None
    assert "masumi-network/Citadel" in query
    assert "#123" in query
    assert "https://" not in query


def test_extract_task_query_retains_long_valid_task_line() -> None:
    prompt = "Implement " + ("task-aware search " * 30) + "preserve-domain-term"
    assert len(prompt) > search_inject.MAX_LOG_LINE_CHARS

    query = search_inject.extract_task_query(prompt)

    assert query is not None
    assert "preserve-domain-term" in query
    assert len(query) <= search_inject.MAX_QUERY_CHARS


def test_extract_task_query_skips_traceback_body_and_exception() -> None:
    prompt = """Fix #123 in kb/hooks/search_inject.py.
Traceback (most recent call last):
  File "worker.py", line 4, in <module>
    source_call()
ValueError: traceback body must not become the task query

Continue the task-aware search implementation.
"""

    query = search_inject.extract_task_query(prompt)

    assert query is not None
    assert "#123" in query
    assert "kb/hooks/search_inject.py" in query
    assert "Continue the task-aware search implementation." in query
    assert "Traceback" not in query
    assert "source_call" not in query
    assert "ValueError" not in query


def test_extract_task_query_keeps_prose_after_message_less_traceback() -> None:
    prompt = """Fix #123 in kb/hooks/search_inject.py.
Traceback (most recent call last):
  File "worker.py", line 4, in <module>
ValueError

Continue the task-aware search implementation.
"""

    query = search_inject.extract_task_query(prompt)

    assert query is not None
    assert "Continue the task-aware search implementation." in query
    assert "ValueError" not in query


def test_extract_task_query_skips_chained_traceback_separator() -> None:
    prompt = """Fix #123 in kb/hooks/search_inject.py.
Traceback (most recent call last):
  File "worker.py", line 4, in <module>
ValueError: first failure

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "worker.py", line 8, in <module>
RuntimeError: second failure

Continue the task-aware search implementation.
"""

    query = search_inject.extract_task_query(prompt)

    assert query is not None
    assert "Continue the task-aware search implementation." in query
    assert "During handling of the above exception" not in query
    assert "RuntimeError" not in query


def test_extract_task_query_keeps_direct_post_traceback_prose() -> None:
    prompt = """Traceback (most recent call last):
  File "worker.py", line 4, in <module>
ValueError
Please fix #123 in kb/hooks/search_inject.py.
"""

    query = search_inject.extract_task_query(prompt)

    assert query is not None
    assert "Please fix #123 in kb/hooks/search_inject.py." in query
    assert "ValueError" not in query


def test_fetch_task_hits_posts_authenticated_bounded_search_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _Opener(_Response(b'{"search_id": "search-1", "results": []}'))
    monkeypatch.setattr(search_inject, "_OPENER", opener)

    payload = search_inject.fetch_task_hits(
        "https://node.example/",
        "ctdl_secret",
        "Fix #123",
        limit=20,
    )

    request, timeout = opener.calls[0]
    assert request.full_url == "https://node.example/search"
    assert json.loads(request.data) == {"query": "Fix #123", "top_k": 3}
    assert request.get_header("Authorization") == "Bearer ctdl_secret"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == search_inject.HTTP_TIMEOUT_SECONDS
    # Advisor-set bound (2026-08-31): p90 7.45s / max 7.71s measured on prod;
    # 10s converts every sampled 5s miss into a hit with 2.3s headroom.
    assert timeout == 10
    assert payload["search_id"] == "search-1"


def test_format_task_context_includes_three_hit_metadata() -> None:
    payload = {
        "search_id": "search-123",
        "dataset": "seat:alice",
        "results": [
            {
                "id": "result-1",
                "title": "search_inject.py",
                "snippet": "Task-aware injection implementation.",
                "_citadel": {
                    "result_id": "result-1",
                    "trust_tier": "unattested",
                    "provenance": {
                        "source": "repo-content",
                        "repo": "masumi-network/Citadel",
                        "path": "kb/hooks/search_inject.py",
                    },
                },
            },
            {"id": "result-2", "title": "onboard.py", "snippet": "Installer."},
            {"id": "result-3", "title": "sync_start.py", "snippet": "Policy."},
            {"id": "result-4", "title": "ignored.py", "snippet": "Too many."},
        ],
    }

    context = search_inject.format_task_context(payload)

    assert context.count("result-") == 3
    assert "search-123" in context
    assert "result-1" in context
    assert "unattested" in context
    assert "repo-content" in context
    assert "masumi-network/Citadel" in context
    assert "kb/hooks/search_inject.py" in context
    assert "ignored.py" not in context
    assert "untrusted context" in context.lower()


def test_format_task_context_redacts_secret_like_text() -> None:
    payload = {
        "search_id": "search-123",
        "results": [
            {
                "id": "result-1",
                "title": "Bearer super-secret-value-123456",
                "snippet": "api_key=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
                "provenance": {"source": "token=ctdl_abcdefghijklmnopqrstuvwxyz"},
            }
        ],
    }

    context = search_inject.format_task_context(payload)

    assert "super-secret-value-123456" not in context
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789" not in context
    assert "ctdl_abcdefghijklmnopqrstuvwxyz" not in context
    assert "[REDACTED]" in context


def test_run_without_token_emits_policy_only(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv(search_inject.TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        search_inject,
        "fetch_task_hits",
        lambda *args, **kwargs: pytest.fail("fetch must not run without a token"),
    )

    assert search_inject.run(io.StringIO(json.dumps({"prompt": "search #123"}))) == 0
    assert capsys.readouterr().out.strip() == AGENT_POLICY_REMINDER.strip()


def test_run_non_https_and_redirect_emit_policy_only(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv(search_inject.TOKEN_ENV, "ctdl_test-token")
    monkeypatch.setenv(search_inject.BASE_URL_ENV, "http://node.example")
    opener = _Opener(_Response(b'{"results": []}', status=302))
    monkeypatch.setattr(search_inject, "_OPENER", opener)

    assert search_inject.run(io.StringIO(json.dumps({"prompt": "search #123"}))) == 0
    assert opener.calls == []
    assert capsys.readouterr().out.strip() == AGENT_POLICY_REMINDER.strip()

    monkeypatch.setenv(search_inject.BASE_URL_ENV, "https://node.example")
    assert search_inject.run(io.StringIO(json.dumps({"prompt": "search #123"}))) == 0
    assert capsys.readouterr().out.strip() == AGENT_POLICY_REMINDER.strip()
    assert len(opener.calls) == 1


def test_run_timeout_and_malformed_response_emit_policy_only(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv(search_inject.TOKEN_ENV, "ctdl_test-token")
    monkeypatch.setenv(search_inject.BASE_URL_ENV, "https://node.example")
    opener = _Opener(TimeoutError("timed out"))
    monkeypatch.setattr(search_inject, "_OPENER", opener)

    assert search_inject.run(io.StringIO(json.dumps({"prompt": "search #123"}))) == 0
    assert capsys.readouterr().out.strip() == AGENT_POLICY_REMINDER.strip()

    opener.response = _Response(b"not-json")
    assert search_inject.run(io.StringIO(json.dumps({"prompt": "search #123"}))) == 0
    assert capsys.readouterr().out.strip() == AGENT_POLICY_REMINDER.strip()


def test_run_calls_fetch_and_injects_task_context(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(search_inject.TOKEN_ENV, "ctdl_test-token")
    monkeypatch.setenv(search_inject.BASE_URL_ENV, "https://node.example")
    calls: list[tuple[str, str, str, int]] = []

    def fake_fetch(base_url: str, token: str, query: str, *, limit: int) -> dict[str, Any]:
        calls.append((base_url, token, query, limit))
        return {
            "search_id": "search-123",
            "results": [{"id": "result-1", "title": "Task result", "snippet": "Useful context."}],
        }

    monkeypatch.setattr(search_inject, "fetch_task_hits", fake_fetch)

    assert search_inject.run(io.StringIO(json.dumps({"prompt": "Fix #123 in repo/path.py"}))) == 0
    output = capsys.readouterr().out
    assert calls and calls[0][0] == "https://node.example"
    assert calls[0][1] == "ctdl_test-token"
    assert "#123" in calls[0][2]
    assert calls[0][3] == 3
    assert "search-123" in output
    assert "Task result" in output
    assert AGENT_POLICY_REMINDER in output


def test_merge_claude_settings_adds_user_prompt_submit_once(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"SessionEnd": [], "SessionStart": []}}))

    assert merge_claude_settings(path, python="/usr/bin/python3") == "added"
    first = json.loads(path.read_text())
    assert len(first["hooks"]["SessionEnd"]) == 1
    assert len(first["hooks"]["SessionStart"]) == 1
    assert len(first["hooks"]["UserPromptSubmit"]) == 1
    assert "kb.hooks.search_inject" in first["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert search_inject.TOKEN_ENV in first["httpHookAllowedEnvVars"]
    assert search_inject.BASE_URL_ENV in first["httpHookAllowedEnvVars"]

    assert merge_claude_settings(path, python="/usr/bin/python3") == "unchanged"
    assert json.loads(path.read_text()) == first
