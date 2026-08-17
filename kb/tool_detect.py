"""Detect installed coding tools and wire Citadel's MCP server into each.

Citadel's MCP is a hosted, remote, streamable-HTTP server (``<node>/mcp/`` with
an ``Authorization: Bearer <ctdl_token>`` header). Tools differ in two ways that
matter here:

  * config location + format — JSON (Cursor, Gemini, Windsurf, Cline, Zed,
    Claude user scope) vs TOML (Codex);
  * whether the tool expands an env var inside the header, so the token can stay
    only in the shell rc, vs needing the literal token written into the file.

v1 AUTO-WRITES only the tools that keep the secret in the rc via an env
reference (Cursor → ``${env:VAR}``, Codex → ``bearer_token_env_var``). Every
other tool is offered as a copy-paste snippet so the user controls where a
plaintext token lands (Claude project scope is already wired by
``citadel onboard`` via ``.mcp.json``; the snippet here is the user-scope
option). Pi has no native MCP, so it gets an informational note only.

All facts (config paths, field names, env-interpolation support) were verified
against current tool docs in June 2026; re-check when a tool changes its schema.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kb.capture_config import DEFAULT_NODE_URL
from kb.onboard import TOKEN_ENV

HOME = Path.home()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    mode: str  # "write" | "snippet" | "note"
    config_hint: str


# Order here is the order tools are offered during onboarding.
SPECS: dict[str, ToolSpec] = {
    "claude": ToolSpec("claude", "Claude Code (user scope)", "write", "~/.claude.json (or `claude mcp add --scope user`)"),
    "cursor": ToolSpec("cursor", "Cursor", "write", "~/.cursor/mcp.json"),
    "codex": ToolSpec("codex", "Codex CLI", "write", "~/.codex/config.toml"),
    "gemini": ToolSpec("gemini", "Gemini CLI", "write", "~/.gemini/settings.json"),
    "windsurf": ToolSpec("windsurf", "Windsurf", "write", "~/.codeium/windsurf/mcp_config.json"),
    "cline": ToolSpec("cline", "Cline", "snippet", "cline_mcp_settings.json (token in plaintext)"),
    "zed": ToolSpec("zed", "Zed", "snippet", "~/.config/zed/settings.json (token in plaintext)"),
    "pi": ToolSpec("pi", "Pi", "note", "(no native MCP)"),
}

ALL_TOOLS = list(SPECS)


@dataclass
class ToolResult:
    tool: str
    action: str  # "wrote" | "unchanged" | "snippet" | "note" | "error"
    detail: str
    snippet: str | None = None


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _exists(*paths: str) -> bool:
    return any(Path(p).expanduser().exists() for p in paths)


def _cline_settings() -> Path:
    """Cline's MCP settings file in the VS Code globalStorage dir (mac/Linux)."""
    rel = "globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"
    mac = HOME / "Library/Application Support/Code/User" / rel
    if mac.parent.parent.exists():
        return mac
    return HOME / ".config/Code/User" / rel


def detect() -> list[str]:
    """Names of installed coding tools we know how to wire, in display order."""
    found: set[str] = set()
    if _which("cursor") or _exists("~/.cursor"):
        found.add("cursor")
    if _which("codex") or _exists("~/.codex"):
        found.add("codex")
    if _which("claude") or _exists("~/.claude.json", "~/.claude"):
        found.add("claude")
    if _which("gemini") or _exists("~/.gemini"):
        found.add("gemini")
    if _exists("~/.codeium/windsurf"):
        found.add("windsurf")
    if _exists("~/.config/zed"):
        found.add("zed")
    if _cline_settings().parent.parent.exists():
        found.add("cline")
    if _which("pi") or _exists("~/.pi"):
        found.add("pi")
    return [t for t in ALL_TOOLS if t in found]


def mcp_url(node_url: str) -> str:
    return f"{node_url.rstrip('/')}/mcp/"


def _merge_json_mcp(path: Path, tool: str, servers_key: str, block: dict) -> ToolResult:
    """Merge a ``citadel`` server into a JSON config, never clobbering siblings."""
    data: dict = {}
    if path.exists():
        try:
            raw = path.read_text()  # read once; reuse for parse AND backup
        except OSError as exc:
            # Unreadable: never overwrite a file we couldn't read/back up.
            return ToolResult(tool, "error", f"{path}: unreadable ({exc})")
        try:
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise ValueError("config root is not a JSON object")
            data = loaded
        except ValueError:
            # Corrupt/foreign: back it up BEFORE overwriting, with restricted perms
            # (it may hold a third-party token), rather than clobbering blind.
            backup = path.with_suffix(path.suffix + ".citadel-bak")
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(raw)
            except OSError:
                pass
            data = {}
    servers = data.setdefault(servers_key, {})
    if not isinstance(servers, dict):
        return ToolResult(tool, "error", f"{path}: {servers_key} is not an object")
    if servers.get("citadel") == block:
        return ToolResult(tool, "unchanged", str(path))
    servers["citadel"] = block
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        return ToolResult(tool, "error", f"{path}: {exc}")
    return ToolResult(tool, "wrote", str(path))


def _wire_codex(url: str) -> ToolResult:
    """Prefer the `codex mcp add` CLI (no hand-rolled TOML); else marker-append."""
    if _which("codex"):
        try:
            subprocess.run(
                ["codex", "mcp", "add", "citadel", "--url", url, "--bearer-token-env-var", TOKEN_ENV],
                check=True,
                capture_output=True,
                timeout=30,
            )
            return ToolResult("codex", "wrote", "via `codex mcp add`")
        except (OSError, subprocess.SubprocessError):
            pass  # fall back to editing config.toml directly
    path = Path("~/.codex/config.toml").expanduser()
    marker = "[mcp_servers.citadel]"
    existing = ""
    if path.exists():
        try:
            existing = path.read_text()
        except OSError as exc:
            return ToolResult("codex", "error", f"{path}: {exc}")
    if marker in existing:
        wanted = f'url = "{url}"'
        start = existing.index(marker)
        rest = existing.find("\n[", start + len(marker))
        section = existing[start:] if rest < 0 else existing[start:rest]
        if wanted in section:
            return ToolResult("codex", "unchanged", str(path))
        new_section = re.sub(r'url\s*=\s*"[^"]*"', wanted, section, count=1)
        if new_section == section:
            return ToolResult("codex", "unchanged", str(path))
        updated = existing[:start] + new_section + (existing[rest:] if rest >= 0 else "")
        try:
            path.write_text(updated)
        except OSError as exc:
            return ToolResult("codex", "error", f"{path}: {exc}")
        return ToolResult("codex", "wrote", str(path))
    block = f"\n{marker}\nurl = \"{url}\"\nbearer_token_env_var = \"{TOKEN_ENV}\"\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block)
    except OSError as exc:
        return ToolResult("codex", "error", f"{path}: {exc}")
    return ToolResult("codex", "wrote", str(path))


def _ensure_claude_project_mcp(node_base: str) -> None:
    """Merge hosted HTTP citadel into the repo's `.mcp.json` (Claude project scope)."""
    from kb.onboard import git_root_or_cwd, merge_mcp_config

    try:
        repo = git_root_or_cwd()
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        repo = Path.cwd()
    try:
        merge_mcp_config(repo / ".mcp.json", node_base)
    except (ValueError, OSError):
        pass


def _wire_claude(url: str, *, node_base: str) -> ToolResult:
    """Prefer `claude mcp add --scope user`; else merge ~/.claude.json (#36)."""
    block = {"type": "http", "url": url, "headers": {"Authorization": f"Bearer ${{{TOKEN_ENV}}}"}}
    if _which("claude"):
        try:
            subprocess.run(
                [
                    "claude", "mcp", "add", "--transport", "http", "citadel", url,
                    "--scope", "user",
                    "--header", f"Authorization: Bearer ${{{TOKEN_ENV}}}",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            _ensure_claude_project_mcp(node_base)
            return ToolResult("claude", "wrote", "via `claude mcp add --scope user` + project .mcp.json")
        except (OSError, subprocess.SubprocessError):
            pass  # fall back to editing ~/.claude.json directly
    result = _merge_json_mcp(
        Path("~/.claude.json").expanduser(),
        "claude",
        "mcpServers",
        block,
    )
    _ensure_claude_project_mcp(node_base)
    if result.action == "wrote":
        result = ToolResult("claude", result.action, f"{result.detail} + project .mcp.json")
    return result


def apply(name: str, *, node_url: str = DEFAULT_NODE_URL) -> ToolResult:
    """Wire (write tier) or produce a snippet/note (others) for one tool."""
    spec = SPECS.get(name)
    if spec is None:
        return ToolResult(name, "error", f"unknown tool: {name}")
    url = mcp_url(node_url)

    if name == "cursor":
        # Cursor expands ${env:VAR} in headers — token stays in the shell rc.
        return _merge_json_mcp(
            Path("~/.cursor/mcp.json").expanduser(),
            "cursor",
            "mcpServers",
            {"url": url, "headers": {"Authorization": "Bearer ${env:%s}" % TOKEN_ENV}},
        )
    if name == "codex":
        return _wire_codex(url)
    if name == "gemini":
        # Gemini expands $VAR in headers (NOT the ${env:} form) — token stays in rc.
        return _merge_json_mcp(
            Path("~/.gemini/settings.json").expanduser(),
            "gemini",
            "mcpServers",
            {"httpUrl": url, "headers": {"Authorization": f"Bearer ${TOKEN_ENV}"}},
        )
    if name == "windsurf":
        # Windsurf expands ${env:VAR} in headers — token stays in rc.
        return _merge_json_mcp(
            Path("~/.codeium/windsurf/mcp_config.json").expanduser(),
            "windsurf",
            "mcpServers",
            {"serverUrl": url, "headers": {"Authorization": "Bearer ${env:%s}" % TOKEN_ENV}},
        )
    if name == "claude":
        return _wire_claude(url, node_base=node_url.rstrip("/"))
    if name == "cline":
        # `type` MUST be the camelCase "streamableHttp" or Cline falls back to
        # legacy SSE and 405s. No header env-interpolation → literal token.
        snippet = json.dumps(
            {
                "mcpServers": {
                    "citadel": {
                        "type": "streamableHttp",
                        "url": url,
                        "headers": {"Authorization": "Bearer <paste your ctdl_ token>"},
                        "disabled": False,
                        "autoApprove": [],
                    }
                }
            },
            indent=2,
        )
        return ToolResult("cline", "snippet", spec.config_hint, snippet)
    if name == "zed":
        # No header env-interpolation in Zed yet → literal token in settings.json.
        snippet = json.dumps(
            {"context_servers": {"citadel": {"url": url, "headers": {"Authorization": "Bearer <paste your ctdl_ token>"}}}},
            indent=2,
        )
        return ToolResult("zed", "snippet", spec.config_hint, snippet)
    if name == "pi":
        return ToolResult(
            "pi",
            "note",
            "Pi has no native auto-writable MCP config; it reaches Citadel via its "
            f"MCP gateway with a Bearer token to {url}.",
        )
    return ToolResult(name, "error", f"unknown tool: {name}")
