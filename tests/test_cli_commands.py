from __future__ import annotations

import argparse
import asyncio
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

from typing import Any

import pytest

from kb.cli import (
    _PYPI_PROJECT_JSON,
    _SKILLS_ADD_HINT,
    _activity,
    _cached_pypi_latest,
    _capture_has_custom_node_url,
    _declined_recently,
    _document,
    _doctor,
    _ingest,
    _maybe_prompt_update,
    _operation,
    _search,
    _skills,
    _should_prompt_update,
    _token_set,
    _update,
    _wizard_roots,
    build_parser,
)
from kb.capture_config import DEFAULT_NODE_URL, CaptureConfig, capture_config_path, save_capture_config
from kb.status import Check, StatusReport


def _ingest_args(**kw):
    base = dict(data="a note", tag=[], json=True, node_url="https://node.example",
                local=False, dataset=None, session=None, cognify=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _report(checks: list[Check], *, healthy: bool = True) -> StatusReport:
    return StatusReport(
        node_url="https://node.example",
        healthy=healthy,
        identity={"seat_slug": "alice", "role": "writer"},
        checks=checks,
        recent=[],
    )


def _all_ok() -> list[Check]:
    return [
        Check("node", True, "healthy"),
        Check("auth", True, "valid"),
        Check("token", True, "…1234"),
        Check("mcp", True, "present"),
        Check("pre_push_hook", True, "installed"),
        Check("session_hook", True, "installed"),
        Check("capture_roots", True, "none"),
    ]


def test_doctor_clean_reports_ok(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_x")
    monkeypatch.setattr("kb.cli.gather_status", lambda *a, **k: _report(_all_ok()))
    args = argparse.Namespace(
        repo=str(tmp_path), config=str(tmp_path / "cap.json"),
        node_url=None, json=True, fix=False,
    )
    rc = asyncio.run(_doctor(args))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["issues"] == []


def test_doctor_warns_when_token_in_rc_not_env(tmp_path: Path, monkeypatch, capsys) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("export CITADEL_MCP_ACCESS_TOKEN='ctdl_x'\n")
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("kb.cli.detect_shell_rc", lambda: rc)
    monkeypatch.setattr("kb.cli.gather_status", lambda *a, **k: _report(_all_ok()))
    args = argparse.Namespace(
        repo=str(tmp_path), config=str(tmp_path / "cap.json"),
        node_url=None, json=True, fix=False,
    )
    rc = asyncio.run(_doctor(args))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert any("not this shell's env" in i["problem"] for i in out["issues"])
    assert any("claude" in i["fix"].lower() for i in out["issues"])


def test_doctor_flags_legacy_stdio_mcp(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_x")
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"citadel": {"command": "uv", "args": ["run"]}}})
    )
    monkeypatch.setattr("kb.cli.gather_status", lambda *a, **k: _report(_all_ok()))
    args = argparse.Namespace(
        repo=str(tmp_path), config=str(tmp_path / "cap.json"),
        node_url=None, json=True, fix=False,
    )
    rc = asyncio.run(_doctor(args))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert any("legacy stdio" in i["problem"] for i in out["issues"])


def test_doctor_fix_installs_missing_local_setup(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_x")
    broken = [
        Check("node", True, "healthy"),
        Check("auth", True, "valid"),
        Check("mcp", False, "not configured"),
        Check("pre_push_hook", False, "missing"),
        Check("session_hook", False, "missing"),
    ]
    monkeypatch.setattr("kb.cli.gather_status", lambda *a, **k: _report(broken))
    args = argparse.Namespace(
        repo=str(tmp_path), config=str(tmp_path / "cap.json"),
        node_url="https://node.example", json=True, fix=True,
    )
    rc = asyncio.run(_doctor(args))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0  # all auto-fixable issues were applied → clean exit
    assert out["resolved"] is True
    assert set(out["fixed"]) == {"pre-push hook", "Claude hooks", "MCP server"}
    assert (tmp_path / ".git" / "hooks" / "pre-push").exists()
    # Claude hooks are installed at user scope (#38), isolated via CITADEL_HOME.
    from kb.onboard import claude_user_settings_path

    assert claude_user_settings_path().exists()
    assert (tmp_path / ".mcp.json").exists()


def test_doctor_needs_auth_not_false_green(tmp_path: Path, monkeypatch, capsys) -> None:
    """Hosted HTTP without a token must stay unresolved under doctor --fix."""
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("CITADEL_CURSOR_MCP_PATH", str(tmp_path / "no-cursor.json"))
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "citadel": {
                        "type": "http",
                        "url": "https://citadel.example/mcp/",
                    }
                }
            }
        )
    )
    from kb import status as status_mod

    mcp = status_mod.assess_mcp_setup(tmp_path)
    assert mcp.data["state"] == status_mod.MCP_STATE_NEEDS_AUTH
    checks = [
        Check("node", True, "healthy"),
        Check("auth", True, "valid"),
        mcp,
        Check("pre_push_hook", True, "ok"),
        Check("session_hook", True, "ok"),
    ]
    monkeypatch.setattr("kb.cli.gather_status", lambda *a, **k: _report(checks))
    args = argparse.Namespace(
        repo=str(tmp_path),
        config=str(tmp_path / "cap.json"),
        node_url="https://node.example",
        json=True,
        fix=True,
    )
    rc = asyncio.run(_doctor(args))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["resolved"] is False
    assert any("needsAuth" in i["problem"] for i in out["issues"])
    assert "MCP server" not in out["fixed"]


def test_search_http_renders_results(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    payload = {
        "results": [{"text": "hello vault", "_citadel": {"dataset": "masumi-network"}}],
        "sections": {
            "central": [{"text": "hello vault", "_citadel": {"dataset": "masumi-network"}}],
            "session_traces": [],
            "node": [],
        },
        "dataset": "masumi-network",
    }
    monkeypatch.setattr("kb.status.search_node", lambda *a, **k: payload)
    args = argparse.Namespace(
        query="hi", top_k=10, json=True, node_url="https://node.example",
        local=False, dataset=None, session=None,
        type=None, repo=None, path=None, canonical_only=False,
        exclude_ambient=False,
        mode=None,
        timeout=None, budget_ms=None,
    )
    rc = asyncio.run(_search(args))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["section_counts"] == {"central": 1, "session_traces": 0, "node": 0}
    assert "sections" not in out
    assert out["results"][0]["text"] == "hello vault"
    assert "snippet" not in out["results"][0]
    assert out["ok"] is True


def test_skills_command_lists_and_shows_management_skill(capsys) -> None:
    list_args = argparse.Namespace(skills_command="list", json=True)
    assert asyncio.run(_skills(list_args)) == 0
    listing = json.loads(capsys.readouterr().out)
    assert any(item["slug"] == "citadel" for item in listing["skills"])

    show_args = argparse.Namespace(skills_command="show", slug="cli", json=True)
    assert asyncio.run(_skills(show_args)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["skill"] == "cli"
    assert "citadel search" in shown["content"]

    search_args = argparse.Namespace(skills_command="show", slug="search", json=True)
    assert asyncio.run(_skills(search_args)) == 0
    search_skill = json.loads(capsys.readouterr().out)
    assert search_skill["skill"] == "search"
    assert "<exact anchor> <subject> <fact or decision needed>" in search_skill["content"]
    assert "citation.source_locator" in search_skill["content"]
    assert "_citadel.retrieval.mode" in search_skill["content"]
    assert "document_id" in search_skill["content"]


def test_operation_command_fetches_projection_receipts(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    payload = {
        "projection_job_id": "job-1",
        "state": "searchable",
        "receipts": [{"backend": "vector", "state": "searchable"}],
    }
    monkeypatch.setattr("kb.status.fetch_operation", lambda *args, **kwargs: payload)
    args = build_parser().parse_args(
        ["operation", "job-1", "--node-url", "https://node.example", "--json"]
    )

    rc = asyncio.run(_operation(args))

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_operation_command_can_require_searchable_state(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.status.fetch_operation",
        lambda *args, **kwargs: {
            "projection_job_id": "job-pending",
            "state": "pending",
            "receipts": [{"backend": "vector", "state": "pending"}],
        },
    )
    args = build_parser().parse_args(
        [
            "operation",
            "job-pending",
            "--require-searchable",
            "--node-url",
            "https://node.example",
            "--json",
        ]
    )

    rc = asyncio.run(_operation(args))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["code"] == "OPERATION_NOT_SEARCHABLE"
    assert payload["state"] == "pending"


def test_document_command_fetches_retained_source(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    payload = {
        "ok": True,
        "document": {
            "id": "document-1",
            "title": "Runbook",
            "body": "Rotate keys.",
            "content": "Rotate keys.",
            "text": "Rotate keys.",
        },
    }
    monkeypatch.setattr("kb.status.fetch_document", lambda *args, **kwargs: payload)
    args = build_parser().parse_args(
        ["document", "document-1", "--node-url", "https://node.example", "--json"]
    )

    rc = asyncio.run(_document(args))

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "document": {"id": "document-1", "title": "Runbook", "body": "Rotate keys."},
    }


def _search_args(**kw):
    base = dict(
        query="hi", top_k=10, json=True, node_url="https://node.example",
        local=False, dataset=None, session=None,
        type=None, repo=None, path=None, canonical_only=False,
        exclude_ambient=False, mode=None, timeout=None, budget_ms=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_search_dataset_without_local_is_rejected(monkeypatch, capsys) -> None:
    # --dataset used to be silently dropped on the HTTP path, so a scoped search
    # quietly returned everything. It must now error instead of running.
    called = {"n": 0}
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.status.search_node",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"results": []},
    )
    with pytest.raises(SystemExit) as exc:
        asyncio.run(_search(_search_args(dataset="masumi-network")))
    assert exc.value.code == 2
    assert called["n"] == 0  # never reached the Node
    assert "requires --local" in capsys.readouterr().err


def test_ingest_session_without_local_is_rejected(monkeypatch, capsys) -> None:
    called = {"n": 0}
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.status.ingest_node",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"accepted": True},
    )
    with pytest.raises(SystemExit) as exc:
        asyncio.run(_ingest(_ingest_args(session="s1")))
    assert exc.value.code == 2
    assert called["n"] == 0
    assert "requires --local" in capsys.readouterr().err


def test_search_local_still_accepts_dataset(monkeypatch) -> None:
    # The guard must not touch the --local path, where --dataset is valid.
    seen = {}

    async def fake_local(args):
        seen["dataset"] = args.dataset
        return 0

    monkeypatch.setattr("kb.cli._search_local", fake_local)
    rc = asyncio.run(_search(_search_args(local=True, dataset="seat:alice")))
    assert rc == 0
    assert seen["dataset"] == "seat:alice"


def test_search_local_contextless_query_does_not_start_provider(
    monkeypatch,
    capsys,
) -> None:
    def explode() -> Any:
        raise AssertionError("contextless local search started Citadel")

    monkeypatch.setattr("kb.service.Citadel.from_env", explode)

    rc = asyncio.run(
        _search(
            _search_args(
                query="What did we decide about this?",
                local=True,
            )
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "QUERY_CONTEXT_REQUIRED"
    assert payload["clarification_required"] is True
    assert payload["answerable"] is False


def test_search_human_output_explains_context_required(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.status.search_node",
        lambda *_args, **_kwargs: {
            "results": [],
            "code": "QUERY_CONTEXT_REQUIRED",
            "clarification_required": True,
            "answerable": False,
            "message": "Name the decision topic, issue, repository, file, symbol, or feature.",
        },
    )

    rc = asyncio.run(
        _search(_search_args(query="What did the team decide about this?", json=False))
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "QUERY_CONTEXT_REQUIRED" in output
    assert "Name the decision topic" in output
    assert "No results" not in output


def test_search_http_forwards_cli_filters(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    captured: dict[str, Any] = {}

    def fake_search(_base, _token, query, top_k=10, **kwargs):
        captured["query"] = query
        captured["top_k"] = top_k
        captured.update(kwargs)
        return {"results": [], "sections": {}, "dataset": "notes"}

    monkeypatch.setattr("kb.status.search_node", fake_search)
    args = argparse.Namespace(
        query="endpoint schema",
        top_k=7,
        json=True,
        node_url="https://node.example",
        local=False,
        dataset=None,
        session=None,
        type="spec,skill",
        repo="masumi-network/agent",
        path="docs/MIP-003",
        source="repo-content",
        canonical_only=True,
        exclude_ambient=False,
        mode=None,
        timeout=None,
        budget_ms=None,
    )
    rc = asyncio.run(_search(args))
    assert rc == 0
    assert captured["query"] == "endpoint schema"
    assert captured["top_k"] == 7
    assert captured["types"] == ["spec", "skill"]
    assert captured["repo"] == "masumi-network/agent"
    assert captured["path"] == "docs/MIP-003"
    assert captured["source"] == "repo-content"
    assert captured["canonical_only"] is True


def test_search_http_renders_trace_sections(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    trace_hit = {
        "text": "fixed the kuzu lock",
        "_citadel": {
            "dataset": "session-traces",
            "trust": "reference-only",
            "author_seat": "alice",
        },
    }
    payload = {
        "results": [trace_hit],
        "sections": {"central": [], "session_traces": [trace_hit], "node": []},
        "dataset": "masumi-network",
    }
    monkeypatch.setattr("kb.status.search_node", lambda *a, **k: payload)
    args = argparse.Namespace(
        query="kuzu lock", top_k=10, json=False, node_url="https://node.example",
        local=False, dataset=None, session=None,
        type=None, repo=None, path=None, canonical_only=False,
        exclude_ambient=False,
        mode=None,
        timeout=None, budget_ms=None,
    )
    rc = asyncio.run(_search(args))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Session traces (reference-only" in out
    assert "trust: reference-only" in out
    assert "author: alice" in out
    assert "fixed the kuzu lock" in out


def test_search_human_output_includes_provenance_and_drilldown(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    hit = {
        "id": "repo-chunk-1",
        "document_id": "repo-document-1",
        "text": "Repository token context",
        "_citadel": {
            "dataset": "masumi-network",
            "provenance": {
                "source": "repo-content",
                "repo": "masumi-network/sokosumi",
                "path": "apps/core/token.ts",
                "source_url": "https://github.com/masumi-network/sokosumi/blob/abc/apps/core/token.ts",
            },
            "retrieval": {
                "mode": "vector",
                "document_drilldown_available": True,
            },
        },
    }
    payload = {
        "results": [hit],
        "sections": {"central": [hit], "session_traces": [], "node": []},
    }
    monkeypatch.setattr("kb.status.search_node", lambda *a, **k: payload)

    rc = asyncio.run(_search(_search_args(query="token", json=False)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "source: repo-content" in out
    assert "mode: vector" in out
    assert "citation: https://github.com/masumi-network/sokosumi/blob/abc/apps/core/token.ts" in out
    assert "drilldown: citadel document repo-document-1" in out


def test_search_human_literal_query_flattens_ranked_results(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    central = {"text": "unrelated central note", "_citadel": {"dataset": "masumi-network"}}
    exact = {"text": "quokka-beacon-8823", "_citadel": {"dataset": "seat:alice"}}
    payload = {
        "results": [central, exact],
        "sections": {"central": [central], "session_traces": [], "node": [exact]},
    }
    monkeypatch.setattr("kb.status.search_node", lambda *a, **k: payload)

    rc = asyncio.run(_search(_search_args(query="quokka-beacon-8823", json=False)))

    assert rc == 0
    out = capsys.readouterr().out
    assert out.index("quokka-beacon-8823") < out.index("unrelated central note")
    assert "Central\n" not in out


def test_search_no_token_exits_one(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: None)
    args = argparse.Namespace(
        query="hi", top_k=10, json=False, node_url=None,
        local=False, dataset=None, session=None,
        type=None, repo=None, path=None, canonical_only=False,
        exclude_ambient=False,
        mode=None,
        timeout=None, budget_ms=None,
    )
    rc = asyncio.run(_search(args))
    assert rc == 1
    assert "no token" in capsys.readouterr().err


def test_search_no_token_json_auth_required(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: None)
    args = argparse.Namespace(
        query="hi", top_k=10, json=True, node_url=None,
        local=False, dataset=None, session=None,
        type=None, repo=None, path=None, canonical_only=False,
        exclude_ambient=False,
        mode=None,
        timeout=None, budget_ms=None,
    )
    rc = asyncio.run(_search(args))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "AUTH_REQUIRED"


def test_search_json_connection_error_emits_json(monkeypatch, capsys) -> None:
    # A non-timeout network failure under --json used to print to stderr and
    # leave stdout empty, so a scripted caller got nothing to parse.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("kb.status.search_node", boom)
    rc = asyncio.run(_search(_search_args(query="hi")))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "NODE_UNREACHABLE"


def test_search_json_http_error_emits_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise urllib.error.HTTPError("https://node.example", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr("kb.status.search_node", boom)
    rc = asyncio.run(_search(_search_args(query="hi")))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "HTTP_ERROR"
    assert out["http_status"] == 503


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (504, "SEARCH_TIMEOUT", "Search budget expired."),
        (503, "QDRANT_UNAVAILABLE", "Qdrant is unavailable."),
    ],
)
def test_search_json_preserves_typed_server_error(
    monkeypatch,
    capsys,
    status: int,
    code: str,
    message: str,
) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise urllib.error.HTTPError(
            "https://node.example",
            status,
            message,
            {},
            BytesIO(
                json.dumps({"detail": {"code": code, "message": message}}).encode()
            ),
        )

    monkeypatch.setattr("kb.status.search_node", boom)
    rc = asyncio.run(_search(_search_args(query="hi")))

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "ok": False,
        "error": message,
        "code": code,
        "http_status": status,
    }


def test_ingest_json_connection_error_emits_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("kb.status.ingest_node", boom)
    rc = asyncio.run(_ingest(_ingest_args()))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "NODE_UNREACHABLE"


def test_search_timeout_returns_typed_failure_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr("kb.status.search_node", boom)
    args = argparse.Namespace(
        query="MIP-003 endpoint schema",
        top_k=5,
        json=True,
        node_url="https://node.example",
        local=False,
        dataset=None,
        session=None,
        type=None,
        repo=None,
        path=None,
        canonical_only=False,
        exclude_ambient=False,
        mode=None,
        timeout=2.0,
        budget_ms=None,
    )
    rc = asyncio.run(_search(args))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "SEARCH_TIMEOUT"
    assert out["http_status"] == 504


def test_search_urlerror_timeout_deduped(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr("kb.status.search_node", boom)
    args = argparse.Namespace(
        query="x",
        top_k=5,
        json=True,
        node_url="https://node.example",
        local=False,
        dataset=None,
        session=None,
        type=None,
        repo=None,
        path=None,
        canonical_only=False,
        exclude_ambient=False,
        mode=None,
        timeout=None,
        budget_ms=1500,
    )
    rc = asyncio.run(_search(args))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "SEARCH_TIMEOUT"
    assert out["http_status"] == 504


def test_search_genuine_empty_is_a_normal_cli_success(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr("kb.status.search_node", lambda *_a, **_k: {"results": []})

    rc = asyncio.run(_search(_search_args(query="absent")))

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["results"] == []
    assert "code" not in out


def test_search_legacy_soft_timeout_envelope_is_a_typed_failure(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.status.search_node",
        lambda *_a, **_k: {
            "results": [],
            "timed_out": True,
            "truncated": True,
            "note": "server budget expired",
        },
    )

    rc = asyncio.run(_search(_search_args(query="absent")))

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "SEARCH_TIMEOUT"
    assert out["http_status"] == 504


def test_search_human_spec_mode_flattens_ranked(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    digest = {"text": "GitHub org daily digest", "score": 0.99}
    mip = {"title": "MIP-003", "path": "MIPs/MIP-003/MIP-003.md", "score": 0.4, "text": "statuses"}
    payload = {
        "results": [digest, mip],
        "sections": {"central": [digest, mip], "session_traces": [], "node": []},
    }
    monkeypatch.setattr("kb.status.search_node", lambda *a, **k: payload)
    args = argparse.Namespace(
        query="MIP-003 endpoint schema",
        top_k=10,
        json=False,
        node_url="https://node.example",
        local=False,
        dataset=None,
        session=None,
        type=None,
        repo=None,
        path=None,
        canonical_only=False,
        exclude_ambient=False,
        mode=None,
        timeout=None,
        budget_ms=None,
    )
    rc = asyncio.run(_search(args))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Central" not in out  # flattened for spec-mode ranking
    assert out.index("MIP-003") < out.index("daily digest")


def test_verify_and_prepare_pr_context(monkeypatch, tmp_path, capsys) -> None:
    from kb.cli import _prepare_pr_context, _verify

    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    ref = tmp_path / "payment-service.md"
    ref.write_text("# payment MIP-003 endpoint schema\nUse /purchase status enum\n")

    payload = {
        "results": [
            {
                "title": "MIP-003",
                "path": "MIPs/MIP-003/MIP-003.md",
                "url": "https://github.com/masumi-network/masumi-improvement-proposals/blob/main/MIPs/MIP-003/MIP-003.md",
                "text": "Agent statuses and purchase request body",
                "score": 0.8,
            },
            {
                "text": "GitHub org daily digest mentioning cardano-dev-skills",
                "score": 0.99,
            },
        ]
    }
    monkeypatch.setattr("kb.status.search_node", lambda *a, **k: payload)

    v_args = argparse.Namespace(
        file=str(ref),
        query=None,
        top_k=10,
        node_url="https://node.example",
        timeout=None,
        budget_ms=None,
    )
    assert asyncio.run(_verify(v_args)) == 0
    verify_out = json.loads(capsys.readouterr().out)
    assert verify_out["ok"] is True
    assert verify_out["doc_shaped_sources"]
    assert verify_out["doc_shaped_sources"][0]["trust_tier"] == "unattested"
    assert "agent_instruction" in verify_out

    p_args = argparse.Namespace(
        repo="cardano-dev-skills",
        topic="masumi",
        top_k=10,
        node_url="https://node.example",
        timeout=None,
        budget_ms=None,
    )
    assert asyncio.run(_prepare_pr_context(p_args)) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["ok"] is True
    assert brief["repo"] == "cardano-dev-skills"
    assert brief["doc_shaped_sources"] or brief["org_context"]
    assert "agent_instruction" in brief


def test_ingest_http_accepted(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr("kb.status.ingest_node", lambda *a, **k: {"accepted": True, "dataset": "seat:alice"})
    rc = asyncio.run(_ingest(_ingest_args()))
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["accepted"] is True


def test_ingest_http_rejected_exits_one(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr("kb.status.ingest_node", lambda *a, **k: {"accepted": False, "reason": "secret_content"})
    rc = asyncio.run(_ingest(_ingest_args()))
    assert rc == 1  # a hard rejection must not exit 0


def test_ingest_duplicate_is_benign(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr("kb.status.ingest_node", lambda *a, **k: {"accepted": False, "reason": "duplicate_in_process"})
    # A duplicate is idempotent: exit 0, friendly message, not a scary failure.
    rc = asyncio.run(_ingest(_ingest_args(json=False)))
    assert rc == 0
    assert "duplicate" in capsys.readouterr().out.lower()


def test_ingest_no_token_exits_one(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: None)
    rc = asyncio.run(_ingest(_ingest_args(json=False)))
    assert rc == 1
    assert "no token" in capsys.readouterr().err


def test_ingest_default_is_async(monkeypatch, capsys) -> None:
    # User ingest captures the source. Scheduled projection runs separately.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    seen: dict = {}

    def fake_ingest(base_url, token, data, tags, cognify=False, **k):
        seen["cognify"] = cognify
        return {"accepted": True, "dataset": "seat:alice", "reason": "queued_not_confirmed"}

    monkeypatch.setattr("kb.status.ingest_node", fake_ingest)
    args = build_parser().parse_args(
        ["ingest", "a durable note", "--json", "--node-url", "https://node.example"]
    )
    rc = asyncio.run(_ingest(args))
    assert rc == 0
    assert seen["cognify"] is False


def test_ingest_local_defers_projection(monkeypatch, capsys) -> None:
    seen: dict[str, Any] = {}

    class FakeLocalCitadel:
        async def ingest(self, data: str, **kwargs: Any) -> SimpleNamespace:
            seen.update({"data": data, **kwargs})
            return SimpleNamespace(
                accepted=True,
                reason="queued_not_confirmed",
                dataset=kwargs.get("dataset") or "notes",
                tags=tuple(kwargs.get("tags") or ()),
            )

    monkeypatch.setattr("kb.service.Citadel.from_env", lambda: FakeLocalCitadel())

    rc = asyncio.run(_ingest(_ingest_args(local=True, dataset="notes")))

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["accepted"] is True
    assert seen["defer_cognify"] is True


def test_ingest_no_cognify_flag_is_capture_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    seen: dict = {}

    def fake_ingest(base_url, token, data, tags, cognify=False, **k):
        seen["cognify"] = cognify
        return {"accepted": True, "dataset": "seat:alice"}

    monkeypatch.setattr("kb.status.ingest_node", fake_ingest)
    args = build_parser().parse_args(
        ["ingest", "a durable note", "--no-cognify", "--json", "--node-url", "https://node.example"]
    )
    rc = asyncio.run(_ingest(args))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["accepted"] is True
    assert seen["cognify"] is False


def test_ingest_cognify_flag_is_scheduler_only(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    args = build_parser().parse_args(
        ["ingest", "a durable note", "--cognify", "--json", "--node-url", "https://node.example"]
    )
    rc = asyncio.run(_ingest(args))
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["code"] == "COGNIFY_SCHEDULER_ONLY"


@pytest.mark.parametrize(
    "argv",
    [
        ["improve"],
        ["cognify"],
        ["reindex", "--apply"],
        ["promotion", "run", "--json"],
    ],
)
def test_cli_llm_jobs_are_scheduler_only(argv: list[str], capsys) -> None:
    args = build_parser().parse_args(argv)

    rc = asyncio.run(args.handler(args))

    assert rc == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "llm_scheduled_only"


def test_ingest_pending_receipt_says_scheduled_projection_will_follow(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.status.ingest_node",
        lambda *a, **k: {
            "accepted": True,
            "dataset": "seat:alice",
            "reason": "queued_not_confirmed",
            "cognified": False,
            "projection_job_id": "job-7",
        },
    )
    rc = asyncio.run(_ingest(_ingest_args(json=False)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Scheduled projection owns this work" in out
    assert "within minutes" not in out
    assert "citadel operation job-7" in out
    assert "didn't finish" not in out


# ---- citadel ingest <path> reads the file -------------------------------------


def test_ingest_path_argument_reads_file_content(tmp_path: Path, monkeypatch, capsys) -> None:
    # `citadel ingest NOTES.md` used to ship the literal string "NOTES.md" as
    # the note body — the file was never read, and production projected a
    # path-string note that later died on FileNotFoundError.
    note = tmp_path / "NOTES.md"
    note.write_text("the actual note body", encoding="utf-8")
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    seen: dict = {}

    def fake_ingest(base_url, token, data, tags, cognify=False, **k):
        seen["data"] = data
        return {"accepted": True, "dataset": "seat:alice"}

    monkeypatch.setattr("kb.status.ingest_node", fake_ingest)
    rc = asyncio.run(_ingest(_ingest_args(data=str(note))))
    assert rc == 0
    assert seen["data"] == "the actual note body"


def test_ingest_missing_path_stays_literal_text(monkeypatch, capsys) -> None:
    # A sentence may legitimately contain a slash; only an EXISTING file is read.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    seen: dict = {}

    def fake_ingest(base_url, token, data, tags, cognify=False, **k):
        seen["data"] = data
        return {"accepted": True, "dataset": "seat:alice"}

    monkeypatch.setattr("kb.status.ingest_node", fake_ingest)
    literal = "see docs/no-such-file.md for the full write-up"
    rc = asyncio.run(_ingest(_ingest_args(data=literal)))
    assert rc == 0
    assert seen["data"] == literal


def test_ingest_binary_file_is_a_clean_error(tmp_path: Path, monkeypatch, capsys) -> None:
    blob = tmp_path / "image.png"
    blob.write_bytes(b"\x89PNG\x00\xff\xfe")
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    called = {"n": 0}
    monkeypatch.setattr(
        "kb.status.ingest_node",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"accepted": True},
    )
    rc = asyncio.run(_ingest(_ingest_args(data=str(blob))))
    assert rc == 1
    assert called["n"] == 0  # never shipped
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "FILE_NOT_TEXT"


def test_ingest_oversized_payload_errors_client_side(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # The Node rejects oversized bodies with HTTP 413; the CLI must catch the
    # overrun locally (same limit + env as kb.mcp_server._max_ingest_bytes)
    # instead of shipping a payload the server refuses.
    monkeypatch.setenv("CITADEL_MCP_MAX_INGEST_BYTES", "8")
    big = tmp_path / "big.txt"
    big.write_text("0123456789abcdef", encoding="utf-8")
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    called = {"n": 0}
    monkeypatch.setattr(
        "kb.status.ingest_node",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"accepted": True},
    )
    rc = asyncio.run(_ingest(_ingest_args(data=str(big))))
    assert rc == 1
    assert called["n"] == 0
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "PAYLOAD_TOO_LARGE"


def test_ingest_directory_argument_is_rejected(tmp_path: Path, monkeypatch, capsys) -> None:
    # A directory used to fall through to literal text and ship the path string
    # as the note body — the exact defect class the file fix closed.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    called = {"n": 0}
    monkeypatch.setattr(
        "kb.status.ingest_node",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"accepted": True},
    )
    rc = asyncio.run(_ingest(_ingest_args(data=str(tmp_path))))
    assert rc == 1
    assert called["n"] == 0  # never shipped
    assert json.loads(capsys.readouterr().out)["code"] == "FILE_IS_DIRECTORY"


def test_ingest_oversized_file_rejected_before_reading(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # The size pre-check must fire on stat() alone: a huge file must not be
    # pulled into memory just to be refused.
    monkeypatch.setenv("CITADEL_MCP_MAX_INGEST_BYTES", "8")
    big = tmp_path / "big.txt"
    big.write_text("0123456789abcdef", encoding="utf-8")
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def forbidden_read(self, *a, **k):
        raise AssertionError("read_text must not be called for an oversized file")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    rc = asyncio.run(_ingest(_ingest_args(data=str(big))))
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["code"] == "PAYLOAD_TOO_LARGE"


def test_ingest_json_receipt_reports_file_source(tmp_path: Path, monkeypatch, capsys) -> None:
    # In --json mode the dim "reading …" line is suppressed for output purity,
    # so the receipt itself must carry the file-vs-literal signal.
    note = tmp_path / "NOTES.md"
    note.write_text("the actual note body", encoding="utf-8")
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.status.ingest_node", lambda *a, **k: {"accepted": True, "dataset": "seat:alice"}
    )
    rc = asyncio.run(_ingest(_ingest_args(data=str(note))))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ingest_source"] == "file"
    assert out["ingest_path"] == str(note)


def test_ingest_json_receipt_reports_text_source(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.status.ingest_node", lambda *a, **k: {"accepted": True, "dataset": "seat:alice"}
    )
    rc = asyncio.run(_ingest(_ingest_args(data="a plain literal note")))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ingest_source"] == "text"
    assert "ingest_path" not in out


def test_ingest_empty_file_is_a_clean_error(tmp_path: Path, monkeypatch, capsys) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    called = {"n": 0}
    monkeypatch.setattr(
        "kb.status.ingest_node",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"accepted": True},
    )
    rc = asyncio.run(_ingest(_ingest_args(data=str(empty))))
    assert rc == 1
    assert called["n"] == 0
    assert json.loads(capsys.readouterr().out)["code"] == "FILE_EMPTY"


# ---- citadel token set --------------------------------------------------------


def _token_set_args(tmp_path: Path, **kw) -> argparse.Namespace:
    base = dict(
        token="ctdl_rotated_4567890",
        shell_rc=str(tmp_path / ".zshrc"),
        node_url="https://node.example",
        skip_verify=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_token_set_verifies_then_writes_rc(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "kb.status.check_auth",
        lambda *a, **k: Check("auth", True, "valid", data={"seat_slug": "sarthi", "role": "writer"}),
    )
    rc = asyncio.run(_token_set(_token_set_args(tmp_path)))
    assert rc == 0
    assert "ctdl_rotated_4567890" in (tmp_path / ".zshrc").read_text()
    assert "…7890" in capsys.readouterr().out


def test_token_set_rejected_token_writes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "kb.status.check_auth", lambda *a, **k: Check("auth", False, "HTTP Error 401: Unauthorized")
    )
    rc = asyncio.run(_token_set(_token_set_args(tmp_path)))
    assert rc == 1
    assert not (tmp_path / ".zshrc").exists()  # a bad token must not clobber a working one
    assert "nothing written" in capsys.readouterr().err


def test_token_set_skip_verify_writes_offline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "kb.status.check_auth", lambda *a, **k: pytest.fail("verified despite --skip-verify")
    )
    rc = asyncio.run(_token_set(_token_set_args(tmp_path, skip_verify=True)))
    assert rc == 0
    assert "ctdl_rotated_4567890" in (tmp_path / ".zshrc").read_text()


def test_token_set_no_token_no_tty_exits_two(tmp_path: Path, capsys) -> None:
    rc = asyncio.run(_token_set(_token_set_args(tmp_path, token=None)))
    assert rc == 2
    assert "no TTY" in capsys.readouterr().err


# ---- citadel update -----------------------------------------------------------


def test_update_editable_install_is_left_alone(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("editable", "file:///src/citadel"))
    monkeypatch.setattr("kb.cli.subprocess.run", lambda *a, **k: pytest.fail("must not shell out"))
    rc = asyncio.run(_update(argparse.Namespace()))
    assert rc == 0
    assert "git pull" in capsys.readouterr().out


def test_update_pipx_already_latest(monkeypatch, capsys) -> None:
    wired: list[str] = []
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("pipx", "/usr/bin/pipx"))
    monkeypatch.setattr(
        "kb.cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="citadel-archive is already at latest version 0.2.1 (location: …)",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        "kb.cli._wire_write_tier_tools",
        lambda url: wired.append(url) or ([], []),
    )
    rc = asyncio.run(_update(argparse.Namespace()))
    assert rc == 0
    out = capsys.readouterr().out
    assert "already up to date" in out
    assert _SKILLS_ADD_HINT in out
    assert wired == [DEFAULT_NODE_URL]


def test_update_pipx_upgraded(monkeypatch, capsys) -> None:
    wired: list[str] = []
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("pipx", "/usr/bin/pipx"))
    monkeypatch.setattr(
        "kb.cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="upgraded package citadel-archive from 0.2.1 to 0.3.0 (location: …)",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        "kb.cli._wire_write_tier_tools",
        lambda url: wired.append(url) or ([], []),
    )
    rc = asyncio.run(_update(argparse.Namespace()))
    assert rc == 0
    out = capsys.readouterr().out
    assert "upgraded package citadel-archive" in out
    assert _SKILLS_ADD_HINT in out
    assert wired == [DEFAULT_NODE_URL]


def test_update_skips_rewire_when_capture_has_custom_node_url(monkeypatch, capsys) -> None:
    wired: list[str] = []
    save_capture_config(CaptureConfig(node_url="https://node.example"))
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("pipx", "/usr/bin/pipx"))
    monkeypatch.setattr(
        "kb.cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="upgraded package citadel-archive from 0.2.1 to 0.3.0 (location: …)",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        "kb.cli._wire_write_tier_tools",
        lambda url: wired.append(url) or ([], []),
    )
    assert _capture_has_custom_node_url() is True
    rc = asyncio.run(_update(argparse.Namespace()))
    assert rc == 0
    assert wired == []
    assert _SKILLS_ADD_HINT in capsys.readouterr().out


def test_update_pipx_failure_exits_one(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("pipx", "/usr/bin/pipx"))
    monkeypatch.setattr(
        "kb.cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    rc = asyncio.run(_update(argparse.Namespace()))
    assert rc == 1
    assert "boom" in capsys.readouterr().err


def test_update_unmanaged_install_prints_instructions(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("other", ""))
    rc = asyncio.run(_update(argparse.Namespace()))
    assert rc == 0
    assert "pip install --upgrade citadel-archive" in capsys.readouterr().out


def test_update_check_uses_pypi_json_not_github() -> None:
    assert _PYPI_PROJECT_JSON == "https://pypi.org/pypi/citadel-archive/json"
    assert "github.com" not in _PYPI_PROJECT_JSON


def test_cached_pypi_latest_uses_cache_within_24h(monkeypatch) -> None:
    now = 1_700_000_000.0
    cache = capture_config_path().parent / "pypi-version.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"latest": "0.9.9", "checked_at": now}) + "\n")
    monkeypatch.setattr("kb.cli._fetch_pypi_latest", lambda: pytest.fail("must not hit network"))
    assert _cached_pypi_latest(now=now + 60) == "0.9.9"


def test_cached_pypi_latest_refetches_after_24h(monkeypatch) -> None:
    now = 1_700_000_000.0
    cache = capture_config_path().parent / "pypi-version.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"latest": "0.9.9", "checked_at": now}) + "\n")
    monkeypatch.setattr("kb.cli._fetch_pypi_latest", lambda: "1.2.3")
    assert _cached_pypi_latest(now=now + 24 * 3600 + 1) == "1.2.3"
    stored = json.loads(cache.read_text())
    assert stored["latest"] == "1.2.3"
    assert stored["checked_at"] == now + 24 * 3600 + 1


def test_should_prompt_update_false_under_pytest(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert _should_prompt_update() is False


def test_should_prompt_update_false_non_tty(monkeypatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert _should_prompt_update() is False


def test_update_prompt_skipped_non_tty(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli._should_prompt_update", lambda: False)
    monkeypatch.setattr("kb.cli._cached_pypi_latest", lambda: "9.9.9")
    monkeypatch.setattr("kb.cli._cli_version", lambda: "0.5.1")
    asyncio.run(_maybe_prompt_update(color=False))
    assert capsys.readouterr().out == ""


def test_update_prompt_yes_runs_upgrade_and_wire(monkeypatch, capsys) -> None:
    wired: list[str] = []
    pipx_cmds: list[list[str]] = []
    prompts: list[str] = []
    monkeypatch.setattr("kb.cli._should_prompt_update", lambda: True)
    monkeypatch.setattr("kb.cli._cached_pypi_latest", lambda: "0.5.1")
    monkeypatch.setattr("kb.cli._cli_version", lambda: "0.4.0")
    monkeypatch.setattr("kb.cli._declined_recently", lambda now=None: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "y")
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("pipx", "/usr/bin/pipx"))

    def fake_run(cmd, **kwargs):
        pipx_cmds.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="upgraded package citadel-archive from 0.4.0 to 0.5.1",
            stderr="",
        )

    monkeypatch.setattr("kb.cli.subprocess.run", fake_run)
    monkeypatch.setattr(
        "kb.cli._wire_write_tier_tools",
        lambda url: wired.append(url) or ([], []),
    )
    monkeypatch.setattr("kb.cli._refresh_skills_pack", lambda: None)
    asyncio.run(_maybe_prompt_update(color=False))
    assert pipx_cmds and pipx_cmds[0][:2] == ["/usr/bin/pipx", "upgrade"]
    assert wired == [DEFAULT_NODE_URL]
    assert "Update available" in prompts[0]
    assert "0.5.1 on PyPI" in capsys.readouterr().out


def test_update_prompt_no_skips_until_cache(monkeypatch, capsys) -> None:
    prompts: list[str] = []
    monkeypatch.setattr("kb.cli._should_prompt_update", lambda: True)
    monkeypatch.setattr("kb.cli._cached_pypi_latest", lambda: "0.5.1")
    monkeypatch.setattr("kb.cli._cli_version", lambda: "0.4.0")
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "n")
    monkeypatch.setattr(
        "kb.cli._update",
        lambda args: pytest.fail("must not update on N"),
    )
    asyncio.run(_maybe_prompt_update(color=False))
    assert _declined_recently() is True
    assert "Update available" in prompts[0]
    assert "0.5.1 on PyPI" in capsys.readouterr().out


# ---- coding-tools checkbox + stale-token hint ----------------------------------


def test_checkbox_line_fallback_toggles_and_applies(monkeypatch) -> None:
    from kb.prompt import _select_lines

    answers = iter(["1 3", ""])  # toggle #1 off and #3 on, then apply
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    picked = _select_lines("pick:", ["cursor", "codex", "zed"], {0, 1})
    assert picked == {1, 2}


def test_checkbox_line_fallback_q_skips(monkeypatch) -> None:
    from kb.prompt import _select_lines

    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    assert _select_lines("pick:", ["cursor"], {0}) is None


def test_wire_detected_tools_applies_only_selection(monkeypatch, capsys) -> None:
    from kb.cli import _wire_detected_tools
    from kb.tool_detect import ToolResult

    applied: list[str] = []
    monkeypatch.setattr("kb.tool_detect.detect", lambda: ["cursor", "codex", "zed", "pi"])
    monkeypatch.setattr(
        "kb.tool_detect.apply",
        lambda name, node_url: applied.append(name)
        or ToolResult(name, "note" if name == "pi" else "wrote", "ok", snippet="{}"),
    )
    # The checkbox returns cursor + zed; codex (preselected) was deselected.
    monkeypatch.setattr("kb.prompt.checkbox_select", lambda header, options, checked: {0, 2})

    steps, wired = _wire_detected_tools("https://node.example", color=False)
    out = capsys.readouterr().out
    assert applied == ["cursor", "zed", "pi"]  # pi is the always-shown note
    assert ("Cursor", "wrote") in steps
    assert wired == ["cursor"]
    assert "paste into" in out  # zed snippet printed inline, not in the summary list


def test_wire_detected_tools_skip_selects_nothing(monkeypatch, capsys) -> None:
    from kb.cli import _wire_detected_tools
    from kb.tool_detect import ToolResult

    applied: list[str] = []
    monkeypatch.setattr("kb.tool_detect.detect", lambda: ["cursor", "codex"])
    monkeypatch.setattr(
        "kb.tool_detect.apply",
        lambda name, node_url: applied.append(name) or ToolResult(name, "wrote", "ok"),
    )
    monkeypatch.setattr("kb.prompt.checkbox_select", lambda *a: None)  # user pressed q
    assert _wire_detected_tools("https://node.example", color=False) == ([], [])
    assert applied == []


def test_humanize_error_status_is_failure() -> None:
    from kb.cli import _humanize_status

    text, ok, skipped = _humanize_status("error: permission denied")
    assert ok is False and skipped is False
    assert "permission denied" in text


def test_stale_env_hint_points_at_shell_rc(tmp_path: Path, monkeypatch) -> None:
    from kb.cli import _stale_env_hint
    from kb.onboard import ensure_token_in_rc

    rc = tmp_path / ".zshrc"
    ensure_token_in_rc(rc, "ctdl_fresh_1234567890")
    monkeypatch.setattr("kb.cli.detect_shell_rc", lambda: rc)
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_stale_1234567890")

    hint = _stale_env_hint(401)
    assert hint and "source" in hint and str(rc) in hint
    assert _stale_env_hint(500) is None  # only auth failures
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_fresh_1234567890")
    assert _stale_env_hint(401) is None  # env matches rc → different problem

    # Variable indirection can't be evaluated — a textual mismatch proves
    # nothing, so no misleading `source` advice.
    rc.write_text('export CITADEL_MCP_ACCESS_TOKEN="$WORK_CITADEL_TOKEN"\n')
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_whatever_890")
    assert _stale_env_hint(401) is None


# ---- capture-roots wizard -----------------------------------------------------


def test_wizard_offers_home_relative_guess_for_missing_root(tmp_path: Path, monkeypatch, capsys) -> None:
    # "/masumi" for ~/masumi is the common typo — the wizard should offer the
    # home-relative dir that actually exists instead of recording a dead root.
    from kb.capture_config import CaptureConfig

    (tmp_path / "masumi").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    answers = iter(["/masumi", "", "", ""])  # path → accept guess → default tags → finish
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    config = _wizard_roots(CaptureConfig(node_url="https://node.example"))
    assert [root.path for root in config.roots] == [str(tmp_path / "masumi")]


def test_wizard_enter_accepts_default_root(tmp_path: Path, monkeypatch) -> None:
    # The dir the user ran `citadel` from is offered as a press-Enter default —
    # no copy-pasting the path you're already standing in.
    from kb.capture_config import CaptureConfig

    answers = iter(["", "", ""])  # accept default → default tags → finish
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    config = _wizard_roots(CaptureConfig(node_url="https://node.example"), default_root=str(tmp_path))
    assert [root.path for root in config.roots] == [str(tmp_path)]
    assert "personal" in config.roots[0].tags


def test_wizard_default_root_is_declinable(tmp_path: Path, monkeypatch) -> None:
    # 'n' to the offered folder, Enter to finish — ending with NO roots must be
    # possible (an un-declinable default would auto-approve $HOME for capture).
    from kb.capture_config import CaptureConfig

    answers = iter(["n", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    config = _wizard_roots(CaptureConfig(node_url="https://node.example"), default_root=str(tmp_path))
    assert config.roots == ()


def test_read_key_handles_bare_escape_and_csi_tails() -> None:
    # Esc alone must not hang or swallow the next key; multi-byte CSI keys
    # (Delete = ESC [ 3 ~) must be consumed whole.
    import os as _os

    from kb.prompt import _ESC, _read_key

    r, w = _os.pipe()
    try:
        _os.write(w, b"\x1b")  # bare Esc (nothing follows within the poll)
        assert _read_key(r) == _ESC
        _os.write(w, b"\x1b[A\x1b[3~q")  # Up, Delete, then 'q'
        assert _read_key(r) == "\x1b[A"
        assert _read_key(r) == "\x1b[3~"  # fully consumed, no stray '~'
        assert _read_key(r) == "q"
    finally:
        _os.close(r)
        _os.close(w)


def test_wizard_default_suppressed_when_already_approved(tmp_path: Path, monkeypatch) -> None:
    from kb.capture_config import CaptureConfig

    existing = CaptureConfig(node_url="https://node.example").with_root(str(tmp_path), ("personal",))
    answers = iter([""])  # no default on offer → Enter just finishes
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    config = _wizard_roots(existing, default_root=str(tmp_path))
    assert len(config.roots) == 1  # not duplicated


# ---- citadel token create (seat binding) ---------------------------------------


SEATS = [
    {"seat_slug": "alice", "name": "Alice", "role": "writer", "disabled": False},
    {"seat_slug": "sarthi", "name": "Sarthi", "role": "writer", "disabled": False},
]


def _token_create_args(**kw):
    base = dict(
        name="ci-bot", seat=None, dataset=None, role=None, kind=None,
        expires_at=None, json=True, node_url="https://node.example",
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _wire_access(monkeypatch, *, seats=None):
    calls = {}
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CITADEL_WRITER_KEYS", raising=False)
    monkeypatch.delenv("CITADEL_ADMIN_KEY", raising=False)

    def fake_issue_seat_token(slug, **k):
        calls["seat"] = (slug, k.get("token_name"))
        return {"ok": True, "token": "ctdl_seat", "principal": {"seat_slug": slug}, "api_token": {}}

    def fake_create_token(**k):
        calls["standalone"] = k
        return {"ok": True, "token": "ctdl_standalone", "principal": {"id": "p1"}, "api_token": k}

    monkeypatch.setattr("kb.cli.list_seats", lambda **k: {"seats": seats if seats is not None else SEATS})
    monkeypatch.setattr("kb.cli.issue_seat_token", fake_issue_seat_token)
    monkeypatch.setattr("kb.cli.create_token", fake_create_token)
    return calls


def test_token_create_seat_and_dataset_conflict(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(seat="alice", dataset="x")))
    assert rc == 2
    assert "not both" in capsys.readouterr().err


def test_token_create_seat_rejects_role_flags(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(seat="alice", role="admin")))
    assert rc == 2
    assert "inherit" in capsys.readouterr().err


def test_token_create_seat_mints_via_seat_endpoint(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(seat="alice")))
    assert rc == 0
    assert calls["seat"] == ("alice", "ci-bot")
    assert "standalone" not in calls
    assert json.loads(capsys.readouterr().out)["token"] == "ctdl_seat"


def test_token_create_unknown_seat_lists_available(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(seat="bob")))
    assert rc == 1
    err = capsys.readouterr().err
    assert "no seat 'bob'" in err and "alice" in err
    assert not calls


def test_token_create_dataset_matching_seat_slug_redirects(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(dataset="sarthi")))
    assert rc == 1
    assert "--seat sarthi" in capsys.readouterr().err
    assert not calls


def test_token_create_seat_prefixed_unknown_dataset_fails(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(dataset="seat:ghost")))
    assert rc == 1
    assert "no seat 'ghost'" in capsys.readouterr().err
    assert not calls


def test_token_create_plain_dataset_stays_standalone(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(dataset="masumi-network")))
    assert rc == 0
    assert calls["standalone"]["default_dataset"] == "masumi-network"
    assert calls["standalone"]["role"] == "reader"  # default fills in
    assert json.loads(capsys.readouterr().out)["token"] == "ctdl_standalone"


def test_token_create_no_target_non_tty_skips_picker(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(json=False)))
    assert rc == 0
    assert "standalone" in calls and "seat" not in calls


def test_pick_seat_choices() -> None:
    from kb.cli import _PickerAborted, _pick_seat

    answers = iter(["7", "2"])  # out-of-range re-prompts, then a valid pick
    import builtins

    original = builtins.input
    builtins.input = lambda prompt="": next(answers)
    try:
        assert _pick_seat(SEATS) == "sarthi"
        builtins.input = lambda prompt="": "0"
        assert _pick_seat(SEATS) is None
        builtins.input = lambda prompt="": ""
        with pytest.raises(_PickerAborted):
            _pick_seat(SEATS)
    finally:
        builtins.input = original


def test_token_create_empty_seat_is_usage_error(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(seat="")))
    assert rc == 2
    assert "--seat needs a seat slug" in capsys.readouterr().err
    assert not calls


def test_token_create_blank_dataset_is_usage_error(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    rc = asyncio.run(_token_create(_token_create_args(dataset="  ")))
    assert rc == 2
    assert "--dataset needs a dataset name" in capsys.readouterr().err
    assert not calls


def test_token_create_standalone_flags_skip_picker_on_tty(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    calls = _wire_access(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "kb.cli._pick_seat",
        lambda seats: pytest.fail("picker must not run when standalone flags are explicit"),
    )
    rc = asyncio.run(
        _token_create(
            _token_create_args(json=False, role="writer", expires_at="2027-01-01T00:00:00Z")
        )
    )
    assert rc == 0
    assert calls["standalone"]["role"] == "writer"
    assert calls["standalone"]["expires_at"] == "2027-01-01T00:00:00Z"
    assert "seat" not in calls


# ---- citadel ingest --timeout / honest expiry ---------------------------------


def test_ingest_timeout_flag_reaches_request(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    seen: dict = {}

    def fake_ingest(base_url, token, data, tags, cognify=False, *, timeout=None):
        seen["timeout"] = timeout
        return {"accepted": True, "dataset": "seat:alice"}

    monkeypatch.setattr("kb.status.ingest_node", fake_ingest)
    rc = asyncio.run(_ingest(_ingest_args(timeout=5)))
    assert rc == 0
    assert seen["timeout"] == 5.0


def test_ingest_timeout_does_not_claim_an_outcome(monkeypatch, capsys) -> None:
    # A client timeout proves only that no response arrived in the budget — the
    # write may or may not have landed. The old message claimed "Your note is
    # saved" (and `saved: true`), which nothing verified.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr("kb.status.ingest_node", boom)
    rc = asyncio.run(_ingest(_ingest_args(timeout=2)))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "TIMEOUT"
    assert out["write_state"] == "unknown"
    assert "saved" not in out
    assert "not prove" in out["error"]


def test_ingest_timeout_plain_mode_is_honest(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr("kb.status.ingest_node", boom)
    rc = asyncio.run(_ingest(_ingest_args(json=False, timeout=2)))
    captured = capsys.readouterr()
    assert rc == 1
    assert "not prove" in captured.err
    assert "is saved" not in captured.err
    assert "is saved" not in captured.out


def test_ingest_wrapped_socket_timeout_is_a_timeout(monkeypatch, capsys) -> None:
    # urllib wraps connect-phase socket timeouts as URLError("timed out") — that
    # is a budget expiry, not an unreachable Node, and must say so.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a, **_k):
        raise urllib.error.URLError("urlopen error timed out")

    monkeypatch.setattr("kb.status.ingest_node", boom)
    rc = asyncio.run(_ingest(_ingest_args()))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "TIMEOUT"


def test_doctor_pre_push_from_non_repo_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    # From a cwd with no git repo, doctor must not report "hook missing" —
    # that reads as "your hook is gone" when there was simply nothing to inspect.
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_x")
    checks = [
        Check("node", True, "healthy"),
        Check("auth", True, "valid"),
        Check("mcp", True, "present"),
        Check(
            "pre_push_hook",
            False,
            f"no git repo at {tmp_path} — nothing to inspect (the hook is per-repo)",
            data={"repo": str(tmp_path), "git_repo": False},
        ),
        Check("session_hook", True, "installed"),
        Check("capture_roots", True, "none"),
    ]
    monkeypatch.setattr("kb.cli.gather_status", lambda *a, **k: _report(checks))
    args = argparse.Namespace(
        repo=str(tmp_path), config=str(tmp_path / "cap.json"),
        node_url=None, json=True, fix=False,
    )
    rc = asyncio.run(_doctor(args))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert not any("hook missing" in i["problem"] for i in out["issues"])
    assert any("no git repo" in i["problem"] for i in out["issues"])


def _activity_args(**kw):
    base = dict(
        local=False, config=None, node_url="https://node.example",
        watch=False, type=None, limit=20, json=True, global_broadcast=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_activity_global_json_emits_json_not_table(monkeypatch, capsys) -> None:
    # `citadel activity --global --json` must emit machine-readable JSON, not
    # the human "Team presence" table — a script parsing this got silent
    # garbage with exit 0 and no signal --json was ignored.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.cli.fetch_presence",
        lambda *a, **k: {"seats": [{"seat": "alice", "documents": 5}]},
    )
    rc = asyncio.run(_activity(_activity_args()))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Team presence" not in out
    payload = json.loads(out)
    assert payload["seats"] == [{"seat": "alice", "documents": 5}]


def test_activity_global_json_flag_order_independent(monkeypatch, capsys) -> None:
    # argparse.Namespace carries no flag-order info, so `--json --global` and
    # `--global --json` reach this handler identically — assert both parse
    # paths (represented by the same Namespace) take the JSON branch.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.cli.fetch_presence",
        lambda *a, **k: {"seats": [{"seat": "bob", "documents": 2}]},
    )
    rc = asyncio.run(_activity(_activity_args(json=True, global_broadcast=True)))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["seats"][0]["seat"] == "bob"


def test_activity_global_json_error_is_parseable(monkeypatch, capsys) -> None:
    # An unreachable Node under --global --json must still exit with a
    # parseable JSON object (matching the non-global error path), not a bare
    # stderr line with stdout empty.
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    monkeypatch.setattr(
        "kb.cli.fetch_presence",
        lambda *a, **k: {"error": "connection refused"},
    )
    rc = asyncio.run(_activity(_activity_args()))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["code"] == "NODE_UNREACHABLE"


# ---- role-aware home, writer self-mint, mcp picker -----------------------------


def _home_names(show_seat: bool) -> list[str]:
    from kb.cli import _home_menu

    return [name for _, rows in _home_menu(show_seat=show_seat) for name, _ in rows]


def _printed_command_names(out: str) -> list[str]:
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    names = []
    for line in plain.splitlines():
        if line.startswith("    "):
            part = line.strip().split()
            if part:
                names.append(part[0])
    return names


def test_writer_home_omits_seat_create() -> None:
    from kb.cli import _home_menu

    names = _home_names(False)
    assert "seat" not in names
    assert "token" in names
    blob = " ".join(desc for _, rows in _home_menu(show_seat=False) for _, desc in rows)
    assert "seat create" not in blob


def test_admin_home_includes_seat() -> None:
    names = _home_names(True)
    assert "seat" in names
    assert "token" in names


def test_home_fetch_fail_hides_seat(monkeypatch, capsys) -> None:
    from kb.cli import _print_home

    monkeypatch.delenv("CITADEL_ADMIN_KEY", raising=False)
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_writer")
    monkeypatch.setattr(
        "kb.cli.check_auth",
        lambda *a, **k: Check("auth", False, "HTTP Error 401: Unauthorized"),
    )
    _print_home()
    names = _printed_command_names(capsys.readouterr().out)
    assert "seat" not in names
    assert "token" in names


def test_admin_key_home_shows_seat_without_fetch(monkeypatch, capsys) -> None:
    from kb.cli import _print_home

    monkeypatch.setenv("CITADEL_ADMIN_KEY", "owner-admin")
    monkeypatch.setattr(
        "kb.cli.check_auth",
        lambda *a, **k: pytest.fail("home must not fetch identity when admin key is set"),
    )
    _print_home()
    names = _printed_command_names(capsys.readouterr().out)
    assert "seat" in names
    assert "token" in names


def test_seat_create_without_admin_key_exits_before_api(monkeypatch, capsys) -> None:
    from kb.cli import _seat_create

    monkeypatch.delenv("CITADEL_ADMIN_KEY", raising=False)
    monkeypatch.setattr("kb.cli.create_seat", lambda **k: pytest.fail("admin API"))
    args = argparse.Namespace(
        name="Alice", slug="alice", email=None, role="writer",
        no_token=False, json=False, node_url="https://node.example",
    )
    rc = asyncio.run(_seat_create(args))
    assert rc == 1
    err = capsys.readouterr().err
    assert "admin only" in err
    assert "citadel token create" in err


def test_token_create_self_mints_without_admin_key(monkeypatch, capsys) -> None:
    from kb.cli import _token_create

    monkeypatch.delenv("CITADEL_ADMIN_KEY", raising=False)
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_seat_tok")
    monkeypatch.setattr(
        "kb.cli.check_auth",
        lambda *a, **k: Check(
            "auth", True, "valid",
            data={"role": "writer", "seat_slug": "alice", "capabilities": {"write": True}, "scopes": []},
        ),
    )
    calls: dict = {}

    def fake_self(**k):
        calls["self"] = k
        return {
            "ok": True,
            "token": "ctdl_new",
            "api_token": {"role": "writer", "default_dataset": "seat:alice"},
        }

    monkeypatch.setattr("kb.cli.create_self_seat_token", fake_self)
    monkeypatch.setattr("kb.cli.issue_seat_token", lambda *a, **k: pytest.fail("admin seat path"))
    monkeypatch.setattr("kb.cli.create_token", lambda **k: pytest.fail("standalone admin path"))
    monkeypatch.setattr(
        "kb.access_client.admin_key",
        lambda: pytest.fail("must not read CITADEL_ADMIN_KEY"),
    )
    rc = asyncio.run(_token_create(_token_create_args(json=True)))
    assert rc == 0
    assert calls["self"]["token"] == "ctdl_seat_tok"
    assert calls["self"]["role"] == "writer"
    assert calls["self"]["token_name"] == "ci-bot"
    assert json.loads(capsys.readouterr().out)["token"] == "ctdl_new"


def test_mcp_no_args_uses_checkbox(monkeypatch) -> None:
    from kb.cli import _mcp
    from kb.tool_detect import ToolResult

    applied: list[str] = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("kb.tool_detect.detect", lambda: ["cursor", "codex"])
    monkeypatch.setattr(
        "kb.tool_detect.apply",
        lambda name, node_url: applied.append(name) or ToolResult(name, "wrote", "ok"),
    )
    monkeypatch.setattr("kb.prompt.checkbox_select", lambda *a, **k: {0})
    rc = asyncio.run(_mcp(argparse.Namespace(node_url="https://node.example", mcp_command=None)))
    assert rc == 0
    assert applied == ["cursor"]


def test_mcp_checkbox_skip_writes_nothing(monkeypatch) -> None:
    from kb.cli import _mcp
    from kb.tool_detect import ToolResult

    applied: list[str] = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("kb.tool_detect.detect", lambda: ["cursor"])
    monkeypatch.setattr(
        "kb.tool_detect.apply",
        lambda name, node_url: applied.append(name) or ToolResult(name, "wrote", "ok"),
    )
    monkeypatch.setattr("kb.prompt.checkbox_select", lambda *a, **k: None)
    rc = asyncio.run(_mcp(argparse.Namespace(node_url="https://node.example", mcp_command=None)))
    assert rc == 0
    assert applied == []


def test_mcp_install_help_contains_add_help(capsys) -> None:
    from kb.cli import _subparser_choice_help

    parser = build_parser()
    mcp = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["mcp"]
    sub = next(a for a in mcp._actions if isinstance(a, argparse._SubParsersAction))
    add_help = _subparser_choice_help(sub, "add")
    install_help = _subparser_choice_help(sub, "install")
    assert add_help == install_help
    assert "Add Citadel MCP to a tool" in add_help

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["mcp", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "add (install)" in out
    assert "Add Citadel MCP to a tool" in out
