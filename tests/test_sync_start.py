from __future__ import annotations

import io

import pytest

from kb.hooks import sync_start


def test_no_token_still_injects_agent_policy(monkeypatch, capsys) -> None:
    monkeypatch.delenv(sync_start.TOKEN_ENV, raising=False)
    rc = sync_start.run(io.StringIO("{}"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Citadel vault — recent activity" not in out  # digest gated on token
    assert out.strip() == sync_start.AGENT_POLICY_REMINDER.strip()
    assert "CLI" in out
    assert "vault-backed" in out
    assert "official/canonical" in out
    assert "Citadel confirms" in out
    assert "USDCx" in out
    assert "skills/masumi" in out
    assert "no authoritative hit" in out


def test_agent_policy_token_authority_lines() -> None:
    text = sync_start.AGENT_POLICY_REMINDER
    assert "Never claim “Citadel confirms X”" in text or 'Never claim "Citadel confirms X"' in text
    assert "Never use Citadel as sole authority for Mainnet asset IDs" in text
    assert "USDCx" in text and "USDM" in text
    assert "skills/masumi" in text
    assert "no authoritative hit" in text
    assert "official docs / skill first" in text

def test_empty_recent_still_injects_agent_policy(monkeypatch, capsys) -> None:
    monkeypatch.setenv(sync_start.TOKEN_ENV, "ctdl_x")
    monkeypatch.setattr(sync_start, "fetch_recent", lambda *a, **k: [])
    rc = sync_start.run(io.StringIO("{}"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Citadel vault — recent activity" not in out
    assert "citadel_search" in out
    assert "CLI" in out
    assert "vault-backed" in out
    assert "reference-only" in out
    assert "citadel_share_session" in out


def test_injects_digest_when_recent(monkeypatch, capsys) -> None:
    monkeypatch.setenv(sync_start.TOKEN_ENV, "ctdl_x")
    monkeypatch.setattr(
        sync_start,
        "fetch_recent",
        lambda *a, **k: [{"created_at": "2026-06-29T10:00:00", "title": "Shipped CLI overhaul"}],
    )
    rc = sync_start.run(io.StringIO("{}"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Citadel vault — recent activity" in out
    assert "Shipped CLI overhaul" in out
    assert "citadel_search" in out  # static agent policy follows the digest
    assert "ctdl_x" not in out  # token never leaks into the digest


def test_failure_is_swallowed(monkeypatch, capsys) -> None:
    monkeypatch.setenv(sync_start.TOKEN_ENV, "ctdl_x")

    def boom(*a, **k):
        raise RuntimeError("node down")

    monkeypatch.setattr(sync_start, "fetch_recent", boom)
    assert sync_start.run(io.StringIO("{}")) == 0  # fail-silent, exit 0
    out = capsys.readouterr().out
    assert "Citadel vault — recent activity" not in out  # digest skipped on failure
    assert "citadel_search" in out  # policy still injected when token present
    assert "reference-only" in out
    assert "citadel_share_session" in out


def test_fetch_failure_skips_digest_but_keeps_policy(monkeypatch, capsys) -> None:
    monkeypatch.setenv(sync_start.TOKEN_ENV, "ctdl_x")

    def boom(*a, **k):
        raise ConnectionError("timeout")

    monkeypatch.setattr(sync_start, "fetch_recent", boom)
    rc = sync_start.run(io.StringIO("{}"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Citadel vault — recent activity" not in out
    assert out.strip() == sync_start.AGENT_POLICY_REMINDER.strip()


def test_fetch_recent_refuses_non_https() -> None:
    with pytest.raises(ValueError, match="non-HTTPS"):
        sync_start.fetch_recent("http://node.example", "ctdl_x")
