"""`citadel onboard` — one-shot teammate setup.

Collapses the manual rollout (token + git hook + SessionEnd hook + MCP server +
capture roots) into a single idempotent command. Every step is safe to re-run
and merges into existing config rather than clobbering it.

Security: the seat token is written to exactly one place (the shell rc). The
`.mcp.json` block references it via ``${CITADEL_MCP_ACCESS_TOKEN}`` — the secret
is never duplicated into project config.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from kb.capture_config import DEFAULT_NODE_URL
from kb.hooks.search_inject import BASE_URL_ENV, SEARCH_MODULE
from kb.hooks.sync_start import AGENT_POLICY_REMINDER

TOKEN_ENV = "CITADEL_MCP_ACCESS_TOKEN"
AGENTS_MD_FILENAME = "AGENTS.md"
GEMINI_MD_FILENAME = "GEMINI.md"
CURSOR_POLICY_RULE_FILENAME = "citadel-agent-policy.mdc"
WINDSURF_POLICY_RULE_FILENAME = "citadel-agent-policy.md"
POLICY_MARKER_START = "<!-- citadel-agent-policy:start -->"
POLICY_MARKER_END = "<!-- citadel-agent-policy:end -->"
MCP_SERVER_NAME = "citadel"
PUSH_MODULE = "kb.hooks.sync_push"
SESSION_MODULE = "kb.hooks.sync_session"
START_MODULE = "kb.hooks.sync_start"
# Used both to install the hooks and to detect them on re-run.
_SESSION_HOOK_MARKER = SESSION_MODULE
_START_HOOK_MARKER = START_MODULE

_SEARCH_HOOK_MARKER = SEARCH_MODULE

def _hook_python() -> str:
    """The interpreter that has `kb` installed (so the hooks can import it)."""
    return sys.executable or "python3"


def git_root_or_cwd() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def detect_shell_rc(home: Path | None = None) -> Path:
    home = home or Path.home()
    shell = os.getenv("SHELL", "")
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    return home / ".profile"


def claude_home() -> Path:
    """Home dir for user-scope Claude config (CITADEL_HOME overrides, for tests)."""
    override = os.getenv("CITADEL_HOME")
    return Path(override) if override else Path.home()


def claude_user_settings_path() -> Path:
    """User-scope Claude Code settings — where cross-repo session hooks must live.

    Claude Code reads ``~/.claude/settings.json`` for session hooks across every
    repo; installing them into a project's ``.claude/settings.json`` (the old
    behavior) made them fire only inside the onboard repo (#38).
    """
    return claude_home() / ".claude" / "settings.json"


def mask_token(token: str) -> str:
    # Reveal only the last 4 chars — no contiguous bytes from the secret's start.
    token = token.strip()
    return f"…{token[-4:]}" if len(token) > 10 else "****"


def _sh_single_quote(value: str) -> str:
    """POSIX-safe single-quoting (close, escaped quote, reopen) for shell rc."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def mcp_server_block(base_url: str = DEFAULT_NODE_URL) -> dict[str, Any]:
    return {
        "type": "http",
        "url": f"{base_url.rstrip('/')}/mcp/",
        "headers": {"Authorization": "Bearer ${" + TOKEN_ENV + "}"},
    }


def _session_hook(python: str | None = None) -> dict[str, Any]:
    py = python or _hook_python()
    return {
        "type": "command",
        "command": f'"{py}" -m {SESSION_MODULE}',
        "timeout": 20,
        "allowedEnvVars": [TOKEN_ENV],
    }


def _session_start_hook(python: str | None = None) -> dict[str, Any]:
    py = python or _hook_python()
    return {
        "type": "command",
        "command": f'"{py}" -m {START_MODULE}',
        "timeout": 10,
        "allowedEnvVars": [TOKEN_ENV],
    }


def _user_prompt_submit_hook(python: str | None = None) -> dict[str, Any]:
    py = python or _hook_python()
    return {
        "type": "command",
        "command": f'"{py}" -m {SEARCH_MODULE}',
        "timeout": 10,
        "allowedEnvVars": [TOKEN_ENV, BASE_URL_ENV],
    }


def _event_has_marker(event_list: list[Any], marker: str) -> bool:
    """True if any hook group in the event list runs a command containing marker."""
    return any(
        isinstance(group, dict)
        and any(
            isinstance(hook, dict) and marker in str(hook.get("command", ""))
            for hook in group.get("hooks", [])
        )
        for group in event_list
    )


def pre_push_hook_script(python: str | None = None) -> str:
    """Self-contained git pre-push hook that runs the bundled module, fail-silent."""
    py = python or _hook_python()
    return (
        "#!/bin/sh\n"
        "# Citadel autosync — installed by `citadel onboard`.\n"
        "# Non-blocking: never fails `git push`. Warnings (e.g. a path that is not\n"
        "# an Approved Capture Root) go to stderr instead of being swallowed (#43).\n"
        f'"{py}" -m {PUSH_MODULE} "$@" || true\n'
        "exit 0\n"
    )


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"corrupt {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"corrupt {path}: expected a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def ensure_env_in_rc(rc_path: Path, var_name: str, value: str, *, comment: str) -> str:
    """Write/refresh ``export VAR=value`` in the shell rc.

    Idempotent, and rotation-aware: if the var is already exported with a
    *different* value, that line is rewritten (returns ``updated``) instead of
    being left stale. The value is single-quoted POSIX-safely.
    """
    value = value.strip()
    export_line = f"export {var_name}={_sh_single_quote(value)}"
    lines = rc_path.read_text().splitlines() if rc_path.exists() else []
    # Rewrite the LAST matching line — that's the one a real shell honors, so
    # a rotation can never be shadowed by a duplicate export further down.
    match_index = None
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith(f"export {var_name}=") or stripped.startswith(f"{var_name}="):
            match_index = index
    if match_index is not None:
        if lines[match_index].strip() == export_line:
            return "present"
        lines[match_index] = export_line
        rc_path.write_text("\n".join(lines) + "\n")
        return "updated"
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    with rc_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n# {comment}\n{export_line}\n")
    return "added"


def ensure_token_in_rc(rc_path: Path, token: str) -> str:
    """Write ``export CITADEL_MCP_ACCESS_TOKEN=…`` to the shell rc (see ensure_env_in_rc)."""
    return ensure_env_in_rc(
        rc_path,
        TOKEN_ENV,
        token,
        comment="Citadel seat token (added by `citadel onboard`)",
    )


def read_token_from_rc(rc_path: Path) -> str:
    """Best-effort recovery of the exported token value from the shell rc.

    Inverse of ensure_token_in_rc for the line shapes we write (single-quoted)
    plus hand-added exports: shlex handles quoting and trailing comments, and
    the LAST matching line wins — the same line a real shell would honor.
    Lets `citadel onboard` say "already configured — keep or replace?" even in
    a fresh shell where the env var is not exported yet. Returns "" when absent.
    """
    try:
        lines = rc_path.read_text().splitlines()
    except OSError:
        return ""
    token = ""
    for raw in lines:
        stripped = raw.strip()
        for prefix in (f"export {TOKEN_ENV}=", f"{TOKEN_ENV}="):
            if stripped.startswith(prefix):
                value = stripped[len(prefix):].strip()
                try:
                    parts = shlex.split(value, comments=True)
                except ValueError:  # unbalanced quotes — treat as unreadable
                    parts = []
                token = parts[0] if parts else ""
    return token


def merge_mcp_config(path: Path, base_url: str = DEFAULT_NODE_URL) -> str:
    """Merge the citadel MCP server into .mcp.json, preserving other servers."""
    data = _load_json_object(path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"corrupt {path}: mcpServers must be an object")
    block = mcp_server_block(base_url)
    if servers.get(MCP_SERVER_NAME) == block:
        return "unchanged"
    status = "updated" if MCP_SERVER_NAME in servers else "added"
    servers[MCP_SERVER_NAME] = block
    _write_json(path, data)
    return status


def merge_claude_settings(path: Path, python: str | None = None) -> str:
    """Merge the SessionEnd, SessionStart, and UserPromptSubmit hooks.

    SessionEnd distills the closing session to the dev's node; SessionStart
    injects a recent-activity digest; UserPromptSubmit injects task search
    context. All hooks are idempotent (detected by module marker) so the merge
    never duplicates them on re-run.
    """
    data = _load_json_object(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"corrupt {path}: hooks must be an object")
    session_end = hooks.setdefault("SessionEnd", [])
    if not isinstance(session_end, list):
        raise ValueError(f"corrupt {path}: hooks.SessionEnd must be an array")
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        raise ValueError(f"corrupt {path}: hooks.SessionStart must be an array")
    user_prompt_submit = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(user_prompt_submit, list):
        raise ValueError(f"corrupt {path}: hooks.UserPromptSubmit must be an array")

    changed = False
    if not _event_has_marker(session_end, _SESSION_HOOK_MARKER):
        session_end.append({"hooks": [_session_hook(python)]})
        changed = True
    if not _event_has_marker(session_start, _START_HOOK_MARKER):
        session_start.append({"matcher": "startup|resume", "hooks": [_session_start_hook(python)]})
        changed = True
    if not _event_has_marker(user_prompt_submit, _SEARCH_HOOK_MARKER):
        user_prompt_submit.append({"hooks": [_user_prompt_submit_hook(python)]})
        changed = True
    allowed = data.setdefault("httpHookAllowedEnvVars", [])
    if not isinstance(allowed, list):
        raise ValueError(f"corrupt {path}: httpHookAllowedEnvVars must be an array")
    for env_name in (TOKEN_ENV, BASE_URL_ENV):
        if env_name not in allowed:
            allowed.append(env_name)
            changed = True
    if not changed:
        return "unchanged"
    _write_json(path, data)
    return "added"


def agent_policy_section() -> str:
    """Marked policy block for AGENTS.md / GEMINI.md idempotent merge."""
    body = AGENT_POLICY_REMINDER.strip()
    return f"{POLICY_MARKER_START}\n{body}\n{POLICY_MARKER_END}\n"


def _merge_marked_section(existing: str, section: str) -> tuple[str, bool]:
    """Insert or replace a marked section; return (content, changed)."""
    if POLICY_MARKER_START in existing and POLICY_MARKER_END in existing:
        before, rest = existing.split(POLICY_MARKER_START, 1)
        _, after = rest.split(POLICY_MARKER_END, 1)
        merged = f"{before.rstrip()}\n\n{section}{after.lstrip()}"
        if not before.rstrip():
            merged = f"{section}{after.lstrip()}"
        if merged == existing:
            return existing, False
        return merged, True
    if existing.strip():
        merged = f"{existing.rstrip()}\n\n{section}"
    else:
        merged = section
    if merged == existing:
        return existing, False
    return merged, True


def _write_text_file_idempotent(path: Path, payload: str) -> str:
    """Write ``payload`` to ``path`` when missing or different."""
    if path.exists():
        try:
            if path.read_text() == payload:
                return "unchanged"
        except OSError:
            pass
        status = "updated"
    else:
        status = "added"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)
    return status


def install_markdown_policy_file(path: Path) -> str:
    """Merge Citadel agent policy into a markdown instruction file (idempotent)."""
    section = agent_policy_section()
    try:
        existing = path.read_text() if path.exists() else ""
    except OSError:
        existing = ""
    merged, changed = _merge_marked_section(existing, section)
    if not changed:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(merged)
    return "updated" if existing.strip() else "added"


def cursor_agent_policy_rule_text() -> str:
    """Cursor rule mirroring the SessionStart hook's static agent policy."""
    return (
        "---\n"
        "description: Citadel agent policy — search before coding, traces are reference-only\n"
        "alwaysApply: true\n"
        "---\n\n"
        f"{AGENT_POLICY_REMINDER}\n"
    )


def windsurf_agent_policy_rule_text() -> str:
    """Windsurf rule with ``always_on`` trigger — same core policy as other agents."""
    return (
        "---\n"
        "description: Citadel agent policy — search before coding, traces are reference-only\n"
        "trigger: always_on\n"
        "---\n\n"
        f"{AGENT_POLICY_REMINDER}\n"
    )


def install_cursor_agent_policy_rule(repo: Path) -> str:
    """Install the Citadel agent policy into ``.cursor/rules/`` (idempotent)."""
    dst = repo / ".cursor" / "rules" / CURSOR_POLICY_RULE_FILENAME
    return _write_text_file_idempotent(dst, cursor_agent_policy_rule_text())


def install_windsurf_agent_policy_rule(repo: Path) -> str:
    """Install the Citadel agent policy into ``.windsurf/rules/`` (idempotent)."""
    dst = repo / ".windsurf" / "rules" / WINDSURF_POLICY_RULE_FILENAME
    return _write_text_file_idempotent(dst, windsurf_agent_policy_rule_text())


def install_agent_policies(repo: Path, *, detected: list[str] | None = None) -> list[tuple[str, str]]:
    """Install Citadel agent policy for every supported coding agent.

    * **AGENTS.md** — Codex (CLI + app), Cursor (fallback), Pi, Cline, Zed, and
      other AGENTS.md-aware tools (always installed at repo root).
    * **Cursor** — ``.cursor/rules/*.mdc`` with ``alwaysApply`` when Cursor is
      detected.
    * **Windsurf** — ``.windsurf/rules/*.md`` with ``trigger: always_on`` when
      Windsurf is detected.
    * **Gemini CLI** — ``GEMINI.md`` when Gemini is detected (native filename).
    * **Claude Code** — policy is injected by the SessionStart hook installed via
      ``merge_claude_settings`` (user-scope, cross-repo).
    """
    if detected is None:
        from kb.tool_detect import detect

        detected = detect()

    steps: list[tuple[str, str]] = [
        (f"Agent policy ({AGENTS_MD_FILENAME})", install_markdown_policy_file(repo / AGENTS_MD_FILENAME)),
    ]
    if "cursor" in detected:
        steps.append(
            ("Cursor agent policy (.cursor/rules)", install_cursor_agent_policy_rule(repo))
        )
    if "windsurf" in detected:
        steps.append(
            ("Windsurf agent policy (.windsurf/rules)", install_windsurf_agent_policy_rule(repo))
        )
    if "gemini" in detected:
        steps.append(
            (f"Gemini agent policy ({GEMINI_MD_FILENAME})", install_markdown_policy_file(repo / GEMINI_MD_FILENAME))
        )
    return steps


def install_pre_push_hook(repo: Path, python: str | None = None) -> str:
    """Install a self-contained pre-push hook that runs the bundled sync module.

    Merge-not-clobber: a pre-existing *foreign* hook (not Citadel-managed) is
    backed up to ``pre-push.citadel-bak`` rather than silently destroyed.
    """
    if not (repo / ".git").is_dir():
        return "skipped:not-git"
    dst = repo / ".git" / "hooks" / "pre-push"
    payload = pre_push_hook_script(python)
    result = "installed"
    if dst.exists():
        existing = dst.read_text()
        if existing == payload:
            return "unchanged"
        if "Citadel autosync" not in existing:
            dst.with_name("pre-push.citadel-bak").write_text(existing)
            result = "installed (backed up existing hook → pre-push.citadel-bak)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(payload)
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return result


def is_legacy_stdio_mcp_block(block: Any) -> bool:
    """True when a citadel MCP block uses stdio/command instead of hosted HTTP."""
    if not isinstance(block, dict):
        return False
    if block.get("command"):
        return True
    mcp_type = str(block.get("type") or "").lower()
    if mcp_type in ("stdio", "sse"):
        return True
    return bool(mcp_type and mcp_type != "http")


def is_http_citadel_mcp_block(block: Any) -> bool:
    """True when the citadel block is the supported hosted HTTP shape."""
    if not isinstance(block, dict) or is_legacy_stdio_mcp_block(block):
        return False
    return block.get("type") == "http" and bool(block.get("url"))


def read_citadel_mcp_block(path: Path) -> dict[str, Any] | None:
    """Return the citadel server block from a JSON MCP config, or None."""
    if not path.exists():
        return None
    try:
        servers = (_load_json_object(path).get("mcpServers") or {})
    except ValueError:
        return None
    if not isinstance(servers, dict):
        return None
    block = servers.get(MCP_SERVER_NAME)
    return block if isinstance(block, dict) else None


def claude_user_mcp_path() -> Path:
    """User-scope Claude MCP config (``claude mcp add --scope user``)."""
    return claude_home() / ".claude.json"


def publish_token_to_macos_gui(token: str) -> str:
    """Put the seat token in the macOS GUI session env (Dock / Spotlight apps).

    ``launchctl setenv`` lasts until logout. This is not a LaunchAgent. The
    token is never interpolated into the returned status. No-op off Darwin.
    """
    if sys.platform != "darwin":
        return "skipped:not-darwin"
    try:
        subprocess.run(
            ["launchctl", "setenv", TOKEN_ENV, token],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error:{type(exc).__name__}"
    return "set"


def format_onboard_next_steps(
    rc_path: Path,
    *,
    tools: list[str] | None = None,
    gui_env: str = "",
) -> str:
    """One post-onboard block: reload the shell, then relaunch each wired client."""
    tools = tools or []
    lines = [
        f"\nNext: restart your shell (or `source {rc_path}`), then:",
    ]
    if "claude" in tools:
        lines.append(
            f"  • Claude Code: source the rc, then `claude`. Cloud: set {TOKEN_ENV} "
            "in your cloud environment settings. Verify: `claude mcp list`."
        )
        lines.append(
            "    project `.mcp.json` alone does not inject secrets into cloud sessions."
        )
        lines.append(
            "    In Claude, `/mcp` should list citadel tools (not zero tools)."
        )
    if "cursor" in tools:
        gui = "macOS GUI env is set until logout. " if gui_env == "set" else ""
        lines.append(
            f"  • Cursor: quit fully (Cmd-Q). {gui}Relaunch from this shell: `cursor .`"
        )
    if "codex" in tools:
        lines.append(
            "  • Codex CLI: restart the Codex session from this shell so it picks up the token."
        )
    if "gemini" in tools:
        lines.append("  • Gemini CLI: restart `gemini` from this shell.")
    if "windsurf" in tools:
        lines.append("  • Windsurf: quit and relaunch from this shell.")
    lines.append(
        '  • smoke test: "use citadel_search to find what we decided about the vault"'
    )
    lines.append(
        "  • when a route is worth reusing, ask, then `citadel_share_session` "
        "(traces are reference-only, not org truth)"
    )
    return "\n".join(lines)


def format_claude_mcp_next_steps(rc_path: Path) -> str:
    """Post-onboard hints for Claude Code local CLI + cloud MCP auth."""
    return format_onboard_next_steps(rc_path, tools=["claude"])


def diagnose_mcp_config(repo: Path) -> list[dict[str, str]]:
    """Doctor-style MCP issues: missing HTTP block, legacy stdio, user-scope drift."""
    issues: list[dict[str, str]] = []
    mcp_path = repo / ".mcp.json"
    block = read_citadel_mcp_block(mcp_path)
    if mcp_path.exists() and block is None:
        issues.append(
            {
                "problem": ".mcp.json has no citadel server entry",
                "fix": "citadel doctor --fix",
                "kind": "mcp",
            }
        )
    elif block is not None:
        if is_legacy_stdio_mcp_block(block):
            issues.append(
                {
                    "problem": ".mcp.json citadel entry uses legacy stdio/command transport",
                    "fix": "citadel doctor --fix  (replaces with hosted HTTP)",
                    "kind": "mcp",
                }
            )
        elif not is_http_citadel_mcp_block(block):
            issues.append(
                {
                    "problem": ".mcp.json citadel entry is not hosted HTTP (type:http + url)",
                    "fix": "citadel doctor --fix",
                    "kind": "mcp",
                }
            )

    claude_path = claude_user_mcp_path()
    claude_block = read_citadel_mcp_block(claude_path)
    if claude_block is not None and is_legacy_stdio_mcp_block(claude_block):
        issues.append(
            {
                "problem": f"{claude_path} has a legacy stdio citadel MCP entry",
                "fix": f"citadel mcp add claude  (or remove citadel from {claude_path})",
            }
        )
    return issues
