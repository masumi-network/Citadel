from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

from typing import Any

import pytest

from kb.cli import _doctor, _ingest, _search, _token_set, _update, _wizard_roots
from kb.status import Check, StatusReport


def _ingest_args(**kw):
    base = dict(data="a note", tag=[], json=True, node_url="https://node.example",
                local=False, dataset=None, session=None, no_cognify=True)
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
    assert out["sections"]["central"][0]["text"] == "hello vault"
    assert out["results"][0]["text"] == "hello vault"
    assert out["results"][0]["snippet"] == "hello vault"
    assert out["ok"] is True


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
        query="kuzu", top_k=10, json=False, node_url="https://node.example",
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


def test_search_timeout_returns_truncated_json(monkeypatch, capsys) -> None:
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
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["timed_out"] is True
    assert out["truncated"] is True
    assert out["ok"] is True
    assert out["code"] == "TIMEOUT"
    assert out["results"] == []


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
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["truncated"] is True and out["timed_out"] is True


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


def test_ingest_cognifies_by_default(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")
    seen: dict = {}

    def fake_ingest(base_url, token, data, tags, cognify=False, **k):
        seen["cognify"] = cognify  # the Node is asked to cognify inline
        return {"accepted": True, "dataset": "seat:alice", "cognified": True}

    monkeypatch.setattr("kb.status.ingest_node", fake_ingest)
    rc = asyncio.run(_ingest(_ingest_args(no_cognify=False)))
    assert rc == 0
    assert seen["cognify"] is True
    assert json.loads(capsys.readouterr().out)["cognified"] is True


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
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("pipx", "/usr/bin/pipx"))
    monkeypatch.setattr(
        "kb.cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="citadel-archive is already at latest version 0.2.1 (location: …)",
            stderr="",
        ),
    )
    rc = asyncio.run(_update(argparse.Namespace()))
    assert rc == 0
    assert "already up to date" in capsys.readouterr().out


def test_update_pipx_upgraded(monkeypatch, capsys) -> None:
    monkeypatch.setattr("kb.cli._install_channel", lambda: ("pipx", "/usr/bin/pipx"))
    monkeypatch.setattr(
        "kb.cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="upgraded package citadel-archive from 0.2.1 to 0.3.0 (location: …)",
            stderr="",
        ),
    )
    rc = asyncio.run(_update(argparse.Namespace()))
    assert rc == 0
    assert "upgraded package citadel-archive" in capsys.readouterr().out


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

    _wire_detected_tools("https://node.example", color=False)
    out = capsys.readouterr().out
    assert applied == ["cursor", "zed", "pi"]  # pi is the always-shown note
    assert "Cursor" in out and "wrote" in out
    assert "paste into" in out  # zed snippet printed


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
    _wire_detected_tools("https://node.example", color=False)
    assert applied == []


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
