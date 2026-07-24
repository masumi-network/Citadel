"""Agent-feedback §9 canary — unit mocks (+ optional live Node).

Run (unit, default — no network):

    pytest -q tests/test_agent_canary.py

Or via the script wrapper:

    python scripts/agent_canary.py
    python scripts/agent_canary.py --live   # optional; needs CITADEL_MCP_ACCESS_TOKEN

Covers:
1. MCP status state machine (missing / needsAuth / readyButUnconfigured / ready)
2. Search JSON schema + --limit + filters
3. Spec-mode ranking / MIP-/endpoint cues
4. Soft timeout envelope (truncated + timed_out, exit 0)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kb import status as status_mod
from kb.agent_workflows import shape_prepare_pr_context, shape_verify_report
from kb.cli import _search
from kb.search_format import is_spec_mode_query, shape_search_payload


def _search_args(**kw: Any) -> argparse.Namespace:
    base = dict(
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
        timeout=None,
        budget_ms=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.canary
def test_canary_mcp_status_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("CITADEL_CURSOR_MCP_PATH", str(tmp_path / "no-cursor.json"))

    missing = status_mod.assess_mcp_setup(tmp_path)
    assert missing.data["state"] == status_mod.MCP_STATE_MISSING

    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "citadel": {"type": "http", "url": "https://citadel.example/mcp/"}
                }
            }
        )
    )
    needs_auth = status_mod.assess_mcp_setup(tmp_path)
    assert needs_auth.data["state"] == status_mod.MCP_STATE_NEEDS_AUTH

    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"citadel": {"command": "uv", "args": ["run"]}}})
    )
    # stdio-only + no token → readyButUnconfigured (not needsAuth)
    assert (
        status_mod.assess_mcp_setup(tmp_path).data["state"]
        == status_mod.MCP_STATE_READY_BUT_UNCONFIGURED
    )

    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_x")
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "citadel": {"type": "http", "url": "https://citadel.example/mcp/"}
                }
            }
        )
    )
    ready = status_mod.assess_mcp_setup(tmp_path)
    assert ready.ok and ready.data["state"] == status_mod.MCP_STATE_READY


@pytest.mark.canary
def test_canary_search_json_schema_limit_filters() -> None:
    payload = {
        "results": [
            {
                "id": "1",
                "title": "MIP-003",
                "path": "MIPs/MIP-003/MIP-003.md",
                "url": "https://github.com/masumi-network/masumi-improvement-proposals/x",
                "text": "purchase request body schema",
                "score": 0.8,
                "_citadel": {"dataset": "masumi-network", "rank": 1},
            },
            {
                "id": "2",
                "text": "GitHub org daily digest",
                "score": 0.99,
                "_citadel": {"dataset": "masumi-network", "rank": 2},
            },
        ]
    }
    shaped = shape_search_payload(
        payload,
        query="MIP-003 availability type masumi-agent",
        types=["spec"],
        path="**/MIP-003/**",
    )
    assert shaped["ok"] is True
    assert shaped["spec_mode"] is True
    assert len(shaped["results"]) == 1
    hit = shaped["results"][0]
    for key in (
        "title",
        "url",
        "repo",
        "path",
        "doc_type",
        "score",
        "snippet",
        "trust_tier",
        "content_hint",
    ):
        assert key in hit
    assert hit["doc_type"] == "spec"
    # Shape is reported; authority is not claimed (nothing in the vault is attested).
    assert hit["content_hint"] == "looks-like-spec"
    assert hit["trust_tier"] == "unattested"
    assert hit["rank"] == 1 or hit["rank"] is not None


@pytest.mark.canary
def test_canary_spec_mode_ranking() -> None:
    assert is_spec_mode_query("MIP-003 endpoint OpenAPI schema")
    payload = {
        "results": [
            {"text": "GitHub org daily digest", "score": 0.95},
            {
                "title": "MIP-003",
                "path": "MIPs/MIP-003/MIP-003.md",
                "text": "status enum",
                "score": 0.4,
            },
        ]
    }
    shaped = shape_search_payload(payload, query="MIP-003 endpoint schema")
    assert shaped["spec_mode"] is True
    assert "MIP-003" in str(shaped["results"][0].get("title") or shaped["results"][0].get("path"))


@pytest.mark.canary
def test_canary_soft_timeout_envelope(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr("kb.cli.capture_token", lambda: "ctdl_x")

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise urllib.error.URLError("The read operation timed out")

    monkeypatch.setattr("kb.status.search_node", boom)
    rc = asyncio.run(_search(_search_args(budget_ms=500)))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["timed_out"] is True
    assert out["truncated"] is True
    assert out["ok"] is True
    assert isinstance(out["results"], list)


@pytest.mark.canary
def test_canary_verify_and_prepare_pr_shapes(tmp_path: Path) -> None:
    path = tmp_path / "payment.md"
    path.write_text("MIP-003 payment purchase endpoint schema token header\n")
    payload = {
        "results": [
            {
                "title": "MIP-003",
                "path": "MIPs/MIP-003/MIP-003.md",
                "url": "https://github.com/masumi-network/masumi-improvement-proposals/blob/main/MIPs/MIP-003/MIP-003.md",
                "text": "purchase endpoint",
                "score": 0.9,
            }
        ]
    }
    report = shape_verify_report(
        path=path, file_text=path.read_text(), search_payload=payload, query="MIP-003 schema"
    )
    assert report["doc_shaped_sources"]
    brief = shape_prepare_pr_context(
        repo="cardano-dev-skills", topic="masumi", search_payload=payload
    )
    assert brief["agent_instruction"]
    assert brief["ok"] is True


@pytest.mark.canary
@pytest.mark.live
def test_canary_live_node_optional() -> None:
    """Optional live probe — skipped unless CITADEL_CANARY_LIVE=1 and token set."""
    if os.getenv("CITADEL_CANARY_LIVE", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("set CITADEL_CANARY_LIVE=1 to exercise the production Node")
    token = (os.getenv("CITADEL_MCP_ACCESS_TOKEN") or "").strip()
    if not token:
        pytest.skip("CITADEL_MCP_ACCESS_TOKEN required for live canary")
    from kb.capture_config import DEFAULT_NODE_URL

    base = (os.getenv("CITADEL_NODE_URL") or DEFAULT_NODE_URL).rstrip("/")
    payload = status_mod.search_node(
        base, token, "MIP-003 endpoint schema", top_k=5, timeout=15.0
    )
    shaped = shape_search_payload(payload, query="MIP-003 endpoint schema")
    assert shaped["ok"] is True
    assert "results" in shaped
