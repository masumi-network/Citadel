from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kb.cli import _onboard
from kb.onboard import (
    TOKEN_ENV,
    POLICY_MARKER_END,
    POLICY_MARKER_START,
    agent_policy_section,
    claude_user_settings_path,
    cursor_agent_policy_rule_text,
    detect_shell_rc,
    diagnose_mcp_config,
    ensure_token_in_rc,
    format_claude_mcp_next_steps,
    install_agent_policies,
    install_cursor_agent_policy_rule,
    install_markdown_policy_file,
    install_pre_push_hook,
    install_windsurf_agent_policy_rule,
    is_http_citadel_mcp_block,
    is_legacy_stdio_mcp_block,
    mask_token,
    mcp_server_block,
    merge_claude_settings,
    merge_mcp_config,
    read_token_from_rc,
    windsurf_agent_policy_rule_text,
)
from kb.hooks.sync_start import AGENT_POLICY_REMINDER
from kb.status import Check


def test_detect_shell_rc(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert detect_shell_rc(tmp_path).name == ".zshrc"
    monkeypatch.setenv("SHELL", "/usr/bin/bash")
    assert detect_shell_rc(tmp_path).name == ".bashrc"
    monkeypatch.setenv("SHELL", "/bin/fish")
    assert detect_shell_rc(tmp_path).name == ".profile"


def test_mask_token() -> None:
    assert mask_token("short") == "****"
    # Only the last 4 chars — no contiguous bytes from the token's start.
    assert mask_token("ctdl_abcdef1234567890") == "…7890"


def test_mcp_block_references_env_not_secret() -> None:
    block = mcp_server_block("https://node.example/")
    assert block["url"] == "https://node.example/mcp/"
    # the token is an env reference, never a literal secret
    assert block["headers"]["Authorization"] == "Bearer ${CITADEL_MCP_ACCESS_TOKEN}"


def test_ensure_token_in_rc_adds_then_idempotent(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("# existing\nexport PATH=$PATH:/x\n")

    assert ensure_token_in_rc(rc, "ctdl_secret") == "added"
    body = rc.read_text()
    assert "export CITADEL_MCP_ACCESS_TOKEN='ctdl_secret'" in body
    assert "export PATH=$PATH:/x" in body  # preserved

    # re-run does not duplicate
    assert ensure_token_in_rc(rc, "ctdl_secret") == "present"
    assert rc.read_text().count("CITADEL_MCP_ACCESS_TOKEN") == 1


def test_ensure_token_rotation_updates_in_place(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    assert ensure_token_in_rc(rc, "ctdl_old") == "added"
    assert ensure_token_in_rc(rc, "ctdl_new") == "updated"
    body = rc.read_text()
    assert "ctdl_new" in body and "ctdl_old" not in body
    assert body.count("CITADEL_MCP_ACCESS_TOKEN") == 1  # rewritten, not appended


def test_ensure_token_shell_quote_safe(tmp_path: Path) -> None:
    import subprocess

    rc = tmp_path / ".bashrc"
    nasty = "ctdl_a'b$(whoami)`id`"  # quotes + shell metachars
    ensure_token_in_rc(rc, nasty)
    out = subprocess.run(
        ["sh", "-c", f". {rc} >/dev/null 2>&1; printf %s \"$CITADEL_MCP_ACCESS_TOKEN\""],
        capture_output=True,
        text=True,
    )
    assert out.stdout == nasty  # sourced value is exactly the token, no execution


def test_merge_claude_settings_corrupt_allowed_envvars_raises(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"httpHookAllowedEnvVars": "not-a-list"}))
    with pytest.raises(ValueError, match="httpHookAllowedEnvVars must be an array"):
        merge_claude_settings(path, python="/usr/bin/python3")


def test_install_pre_push_backs_up_foreign_hook(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    foreign = repo / ".git" / "hooks" / "pre-push"
    foreign.write_text("#!/bin/sh\n# husky\nnpm test\n")

    status = install_pre_push_hook(repo, python="/usr/bin/python3")
    assert "backed up" in status
    backup = repo / ".git" / "hooks" / "pre-push.citadel-bak"
    assert backup.exists() and "husky" in backup.read_text()
    assert "kb.hooks.sync_push" in foreign.read_text()  # new hook installed


def test_merge_mcp_config_preserves_other_servers(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"type": "stdio"}}}))

    assert merge_mcp_config(path) == "added"
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"type": "stdio"}
    assert data["mcpServers"]["citadel"]["type"] == "http"

    assert merge_mcp_config(path) == "unchanged"


def test_legacy_stdio_mcp_detection() -> None:
    assert is_legacy_stdio_mcp_block({"command": "uv", "args": ["run"]})
    assert is_legacy_stdio_mcp_block({"type": "stdio", "command": "npx"})
    assert not is_legacy_stdio_mcp_block(mcp_server_block("https://node.example"))
    assert is_http_citadel_mcp_block(mcp_server_block("https://node.example"))


def test_diagnose_mcp_config_flags_legacy_stdio(tmp_path: Path) -> None:
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "citadel": {
                        "command": "uv",
                        "args": ["run", "python", "-m", "kb.mcp_server"],
                    }
                }
            }
        )
    )
    issues = diagnose_mcp_config(tmp_path)
    assert any("legacy stdio" in i["problem"] for i in issues)
    assert issues[0]["kind"] == "mcp"


def test_format_claude_mcp_next_steps_mentions_env_and_verify(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    text = format_claude_mcp_next_steps(rc)
    assert TOKEN_ENV in text
    assert "claude mcp list" in text
    assert "cloud environment" in text
    assert str(rc) in text


def test_merge_mcp_config_corrupt_raises(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text("{ not json")
    with pytest.raises(ValueError, match="corrupt"):
        merge_mcp_config(path)


def test_merge_claude_settings_adds_then_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": []}]}}))

    assert merge_claude_settings(path, python="/usr/bin/python3") == "added"
    data = json.loads(path.read_text())
    assert "PreToolUse" in data["hooks"]  # preserved
    cmds = [
        h["command"]
        for g in data["hooks"]["SessionEnd"]
        for h in g["hooks"]
    ]
    assert any("kb.hooks.sync_session" in c for c in cmds)
    assert TOKEN_ENV in data["httpHookAllowedEnvVars"]
    start_groups = data["hooks"]["SessionStart"]
    start_cmds = [h["command"] for g in start_groups for h in g["hooks"]]
    assert any("kb.hooks.sync_start" in c for c in start_cmds)
    assert start_groups[0]["matcher"] == "startup|resume"

    assert merge_claude_settings(path, python="/usr/bin/python3") == "unchanged"
    data2 = json.loads(path.read_text())
    assert len(data2["hooks"]["SessionEnd"]) == 1  # not duplicated
    assert len(data2["hooks"]["SessionStart"]) == 1  # not duplicated


def _make_repo(tmp_path: Path) -> Path:
    # No vendored skill needed — the hook runs the bundled kb.hooks module.
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    return tmp_path


def test_install_pre_push_hook(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert install_pre_push_hook(repo, python="/usr/bin/python3") == "installed"
    dst = repo / ".git" / "hooks" / "pre-push"
    assert dst.exists()
    assert dst.stat().st_mode & 0o100  # executable
    assert "kb.hooks.sync_push" in dst.read_text()
    assert install_pre_push_hook(repo, python="/usr/bin/python3") == "unchanged"


def test_install_pre_push_hook_not_git(tmp_path: Path) -> None:
    assert install_pre_push_hook(tmp_path) == "skipped:not-git"


def test_agent_policy_section_matches_session_start() -> None:
    section = agent_policy_section()
    assert POLICY_MARKER_START in section
    assert POLICY_MARKER_END in section
    assert AGENT_POLICY_REMINDER.strip() in section
    assert "citadel_search" in section
    assert "reference-only" in section
    assert "citadel_share_session" in section
    assert "vault-backed" in section
    assert "official/canonical" in section
    assert "USDCx" in section
    assert "skills/masumi" in section
    assert "no authoritative hit" in section
    assert "Citadel confirms" in section

def test_cursor_agent_policy_rule_matches_session_start() -> None:
    text = cursor_agent_policy_rule_text()
    assert "alwaysApply: true" in text
    assert "citadel_search" in text
    assert "reference-only" in text
    assert "citadel_share_session" in text


def test_windsurf_agent_policy_rule_matches_session_start() -> None:
    text = windsurf_agent_policy_rule_text()
    assert "trigger: always_on" in text
    assert "citadel_search" in text
    assert "reference-only" in text
    assert "citadel_share_session" in text


def test_install_markdown_policy_file_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    assert install_markdown_policy_file(path) == "added"
    first = path.read_text()
    assert POLICY_MARKER_START in first
    assert install_markdown_policy_file(path) == "unchanged"
    assert path.read_text() == first


def test_install_markdown_policy_file_merges_existing(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("# Team rules\n\nRun tests before pushing.\n")
    assert install_markdown_policy_file(path) == "updated"
    body = path.read_text()
    assert "# Team rules" in body
    assert "Run tests before pushing." in body
    assert POLICY_MARKER_START in body
    assert install_markdown_policy_file(path) == "unchanged"


def test_install_markdown_policy_file_replaces_marked_section(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("# Team rules\n\n" + agent_policy_section())
    old = path.read_text()
    assert install_markdown_policy_file(path) == "unchanged"
    assert path.read_text() == old


def test_install_cursor_agent_policy_rule_idempotent(tmp_path: Path) -> None:
    assert install_cursor_agent_policy_rule(tmp_path) == "added"
    rule = tmp_path / ".cursor" / "rules" / "citadel-agent-policy.mdc"
    assert rule.exists()
    first = rule.read_text()
    assert install_cursor_agent_policy_rule(tmp_path) == "unchanged"
    assert rule.read_text() == first


def test_install_windsurf_agent_policy_rule_idempotent(tmp_path: Path) -> None:
    assert install_windsurf_agent_policy_rule(tmp_path) == "added"
    rule = tmp_path / ".windsurf" / "rules" / "citadel-agent-policy.md"
    assert rule.exists()
    first = rule.read_text()
    assert install_windsurf_agent_policy_rule(tmp_path) == "unchanged"
    assert rule.read_text() == first


def test_install_agent_policies_always_writes_agents_md(tmp_path: Path) -> None:
    steps = install_agent_policies(tmp_path, detected=[])
    labels = [label for label, _ in steps]
    assert "Agent policy (AGENTS.md)" in labels
    assert (tmp_path / "AGENTS.md").exists()
    assert "citadel_search" in (tmp_path / "AGENTS.md").read_text()


def test_install_agent_policies_native_formats_when_detected(tmp_path: Path) -> None:
    steps = install_agent_policies(
        tmp_path,
        detected=["cursor", "windsurf", "gemini", "codex", "claude", "pi"],
    )
    labels = [label for label, _ in steps]
    assert "Agent policy (AGENTS.md)" in labels
    assert "Cursor agent policy (.cursor/rules)" in labels
    assert "Windsurf agent policy (.windsurf/rules)" in labels
    assert "Gemini agent policy (GEMINI.md)" in labels
    assert (tmp_path / ".cursor" / "rules" / "citadel-agent-policy.mdc").exists()
    assert (tmp_path / ".windsurf" / "rules" / "citadel-agent-policy.md").exists()
    assert (tmp_path / "GEMINI.md").exists()


def test_install_agent_policies_skips_native_formats_without_detection(tmp_path: Path) -> None:
    steps = install_agent_policies(tmp_path, detected=["codex"])
    labels = [label for label, _ in steps]
    assert labels == ["Agent policy (AGENTS.md)"]
    assert not (tmp_path / ".cursor" / "rules" / "citadel-agent-policy.mdc").exists()
    assert not (tmp_path / ".windsurf" / "rules" / "citadel-agent-policy.md").exists()
    assert not (tmp_path / "GEMINI.md").exists()


def test_bundled_hooks_are_importable_modules() -> None:
    # The published CLI installs hooks as `python -m kb.hooks.*`; the modules
    # must import and expose a runnable main() with no server deps.
    from kb.hooks import sync_push, sync_session, sync_start

    assert callable(sync_push.main)
    assert callable(sync_session.main)
    assert callable(sync_start.main)


def test_pre_push_hook_script_is_failsafe() -> None:
    from kb.onboard import pre_push_hook_script

    script = pre_push_hook_script(python="/usr/bin/python3")
    assert script.startswith("#!/bin/sh")
    assert "-m kb.hooks.sync_push" in script
    assert "|| true" in script and script.rstrip().endswith("exit 0")


def test_onboard_non_interactive_full_run(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    rc = tmp_path / ".zshrc"
    args = argparse.Namespace(
        token="ctdl_test_seat_token",
        repo=str(repo),
        shell_rc=str(rc),
        no_mcp=False,
        no_capture=True,
        non_interactive=True,
    )
    rc_code = asyncio.run(_onboard(args))
    assert rc_code == 0

    assert "CITADEL_MCP_ACCESS_TOKEN='ctdl_test_seat_token'" in rc.read_text()
    assert (repo / ".git" / "hooks" / "pre-push").exists()
    # Session hooks land in user-scope ~/.claude/settings.json (#38), not the repo.
    assert not (repo / ".claude" / "settings.json").exists()
    settings = json.loads(claude_user_settings_path().read_text())
    assert any(
        "kb.hooks.sync_session" in h["command"]
        for g in settings["hooks"]["SessionEnd"]
        for h in g["hooks"]
    )
    mcp = json.loads((repo / ".mcp.json").read_text())
    assert mcp["mcpServers"]["citadel"]["url"].endswith("/mcp/")
    agents_md = repo / "AGENTS.md"
    assert agents_md.exists()
    assert "citadel_search" in agents_md.read_text()


def test_onboard_no_mcp_flag_skips_mcp(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    args = argparse.Namespace(
        token="ctdl_test",
        repo=str(repo),
        shell_rc=str(tmp_path / ".zshrc"),
        no_mcp=True,
        no_capture=True,
        non_interactive=True,
    )
    assert asyncio.run(_onboard(args)) == 0
    assert not (repo / ".mcp.json").exists()


def test_onboard_no_token_non_interactive_exits_one(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    args = argparse.Namespace(
        token=None,
        repo=str(tmp_path),
        shell_rc=str(tmp_path / ".zshrc"),
        no_mcp=False,
        no_capture=True,
        non_interactive=True,
    )
    rc = asyncio.run(_onboard(args))
    assert rc == 1
    assert "no token" in capsys.readouterr().err


def test_read_token_from_rc_roundtrips_nasty_token(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    nasty = "ctdl_a'b$(whoami)`id`"
    ensure_token_in_rc(rc, nasty)
    assert read_token_from_rc(rc) == nasty  # exact inverse of the writer


def test_read_token_from_rc_handles_manual_lines(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(f"{TOKEN_ENV}=ctdl_plain_no_quotes\n")
    assert read_token_from_rc(rc) == "ctdl_plain_no_quotes"
    rc.write_text(f'export {TOKEN_ENV}="ctdl_double_quoted"\n')
    assert read_token_from_rc(rc) == "ctdl_double_quoted"
    assert read_token_from_rc(tmp_path / "missing") == ""


def test_read_token_from_rc_quoted_value_with_trailing_comment(tmp_path: Path) -> None:
    # A hand-edited line with a comment must not leak the quote chars into the
    # token (Bearer 'ctdl_…' would 401 a perfectly valid setup).
    rc = tmp_path / ".zshrc"
    rc.write_text(f"export {TOKEN_ENV}='ctdl_abc123'  # citadel seat\n")
    assert read_token_from_rc(rc) == "ctdl_abc123"


def test_read_token_from_rc_last_export_wins_like_the_shell(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(
        f"export {TOKEN_ENV}='ctdl_first_1234567890'\n"
        f"export {TOKEN_ENV}=ctdl_last_0987654321\n"
    )
    assert read_token_from_rc(rc) == "ctdl_last_0987654321"


def test_ensure_env_in_rc_rewrites_last_duplicate(tmp_path: Path) -> None:
    # The shell honors the LAST export; rotation must rewrite that one, or the
    # new token is silently shadowed by the trailing duplicate.
    from kb.onboard import ensure_env_in_rc

    rc = tmp_path / ".zshrc"
    rc.write_text(
        f"export {TOKEN_ENV}='ctdl_old_a'\n"
        f"export {TOKEN_ENV}='ctdl_old_b'\n"
    )
    assert ensure_env_in_rc(rc, TOKEN_ENV, "ctdl_new_c", comment="x") == "updated"
    lines = rc.read_text().splitlines()
    assert lines[0] == f"export {TOKEN_ENV}='ctdl_old_a'"  # untouched (shadowed anyway)
    assert lines[1] == f"export {TOKEN_ENV}='ctdl_new_c'"  # the shell-effective line
    assert read_token_from_rc(rc) == "ctdl_new_c"  # reader agrees with the shell


def _interactive_onboard_args(tmp_path: Path) -> argparse.Namespace:
    repo = _make_repo(tmp_path)
    return argparse.Namespace(
        token=None,
        repo=str(repo),
        shell_rc=str(tmp_path / ".zshrc"),
        no_mcp=True,
        no_capture=True,
        no_tools=True,
        non_interactive=False,
    )


def _auth_ok() -> Check:
    return Check(
        "auth", ok=True, detail="valid",
        data={"seat_slug": "sarthi", "role": "writer", "capabilities": {"read": True, "write": True}},
    )


def test_onboard_offers_keep_or_replace_for_configured_token(tmp_path: Path, monkeypatch, capsys) -> None:
    # Token already wired in the rc (fresh shell: env unset) → onboard must show
    # it masked and let the user swap in a new one instead of silently reusing it.
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    args = _interactive_onboard_args(tmp_path)
    rc_file = Path(args.shell_rc)
    ensure_token_in_rc(rc_file, "ctdl_old_1234567890")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["n"])  # replace, don't keep
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    pastes = iter(["ctdl_new_abcdef12345", ""])  # new token, then skip OpenRouter key
    monkeypatch.setattr("kb.cli.getpass.getpass", lambda prompt="": next(pastes))
    monkeypatch.setattr("kb.status.check_auth", lambda *a, **k: _auth_ok())

    assert asyncio.run(_onboard(args)) == 0
    out = capsys.readouterr().out
    assert "…7890" in out  # the existing token shown masked
    body = rc_file.read_text()
    assert "ctdl_new_abcdef12345" in body and "ctdl_old_1234567890" not in body


def test_onboard_keep_answer_reuses_env_token(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(TOKEN_ENV, "ctdl_env_1234567890")
    args = _interactive_onboard_args(tmp_path)

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter([""])  # enter → keep
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    pastes = iter([""])  # skip OpenRouter key
    monkeypatch.setattr("kb.cli.getpass.getpass", lambda prompt="": next(pastes))
    monkeypatch.setattr("kb.status.check_auth", lambda *a, **k: _auth_ok())

    assert asyncio.run(_onboard(args)) == 0
    assert "ctdl_env_1234567890" in Path(args.shell_rc).read_text()
    out = capsys.readouterr().out
    assert "authenticated" in out  # identity panel shown up front


def test_onboard_rc_token_wins_over_stale_env(tmp_path: Path, monkeypatch, capsys) -> None:
    # A rotation written to the rc must not be silently reverted by onboarding
    # from an old shell whose env still exports the superseded token.
    monkeypatch.setenv(TOKEN_ENV, "ctdl_stale_env_67890")
    args = _interactive_onboard_args(tmp_path)
    rc_file = Path(args.shell_rc)
    ensure_token_in_rc(rc_file, "ctdl_rotated_rc_4321")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter([""])  # keep
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    pastes = iter([""])  # skip OpenRouter key
    monkeypatch.setattr("kb.cli.getpass.getpass", lambda prompt="": next(pastes))
    monkeypatch.setattr("kb.status.check_auth", lambda *a, **k: _auth_ok())

    assert asyncio.run(_onboard(args)) == 0
    out = capsys.readouterr().out
    assert "…4321" in out  # the rc (rotated) token is the one offered
    body = rc_file.read_text()
    assert "ctdl_rotated_rc_4321" in body and "ctdl_stale_env_67890" not in body


def test_onboard_rejected_token_offers_immediate_replacement(tmp_path: Path, monkeypatch, capsys) -> None:
    # A 401 must surface before the other prompts, with an inline re-paste —
    # not "saved anyway" at the very end of the run.
    monkeypatch.setenv(TOKEN_ENV, "ctdl_stale_234567890")
    args = _interactive_onboard_args(tmp_path)

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["", "y"])  # keep existing → then agree to re-paste after the 401
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    pastes = iter(["ctdl_fresh_bcdef12345", ""])  # replacement token, skip OpenRouter key
    monkeypatch.setattr("kb.cli.getpass.getpass", lambda prompt="": next(pastes))
    verdicts = iter([
        Check("auth", ok=False, detail="HTTP Error 401: Unauthorized"),
        _auth_ok(),
    ])
    monkeypatch.setattr("kb.status.check_auth", lambda *a, **k: next(verdicts))

    assert asyncio.run(_onboard(args)) == 0
    out = capsys.readouterr().out
    assert "rejected" in out and "401" in out
    body = Path(args.shell_rc).read_text()
    assert "ctdl_fresh_bcdef12345" in body and "ctdl_stale_234567890" not in body


def test_onboard_kept_rejected_token_warns_in_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    # Keeping a 401'd token must not end on an all-green summary (probe finding:
    # a new user walked away from a dead token thinking setup succeeded).
    monkeypatch.setenv(TOKEN_ENV, "ctdl_stale_234567890")
    args = _interactive_onboard_args(tmp_path)

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["", ""])  # keep existing → decline the re-paste offer
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    pastes = iter([""])  # skip OpenRouter key
    monkeypatch.setattr("kb.cli.getpass.getpass", lambda prompt="": next(pastes))
    monkeypatch.setattr(
        "kb.status.check_auth",
        lambda *a, **k: Check("auth", ok=False, detail="HTTP Error 401: Unauthorized"),
    )

    assert asyncio.run(_onboard(args)) == 0
    out = capsys.readouterr().out
    assert "Node rejected this token" in out  # on the token step line + closing warning
    assert "citadel token set" in out


def test_onboard_syncs_capture_roots_to_node(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    rc = tmp_path / ".zshrc"
    cfg_path = tmp_path / "capture.json"
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(cfg_path))
    calls: list[Any] = []

    def fake_sync(config, **kwargs):
        calls.append((config, kwargs))
        from kb.capture_roots_sync import CaptureRootsSyncResult

        return CaptureRootsSyncResult(
            ok=True,
            status="synced",
            detail="synced 1 approved capture root(s) to Node",
            seat_slug="alice",
            merged_count=1,
        )

    monkeypatch.setattr("kb.cli.sync_local_capture_roots_to_server", fake_sync)
    args = argparse.Namespace(
        token="ctdl_test_seat_token",
        repo=str(repo),
        shell_rc=str(rc),
        no_mcp=True,
        no_capture=False,
        non_interactive=True,
        node_url=None,
        json=False,
        no_tools=True,
    )
    assert asyncio.run(_onboard(args)) == 0
    assert len(calls) == 1
    assert calls[0][1]["token"] == "ctdl_test_seat_token"
    assert cfg_path.exists()
