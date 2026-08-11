"""`citadel capture` — summarize Approved Capture Roots and POST to the seat Node.

ADR-0007 P4.2. v1 is summary-based (git metadata + README blurb), not a raw file
dump: payloads stay small and we avoid shipping file bodies. The server Security
Finding gate and per-seat Capture Policy deny globs remain the authoritative
secret guard on the Node side.

The network primitive (`post_capture`) mirrors the hardening of the standalone
pre-push hook (`sync_push.post_ingest`): HTTPS except for loopback or a Railway
private service, no redirect following (so a 30x never re-sends the seat Bearer
token to another host), and a UTF-8 byte cap.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from kb.capture_config import CaptureRoot, normalize_tags
from kb.secure_http import InsecureEndpointError, require_secure_url

CAPTURE_TAG = "capture"
_README_NAMES = ("README.md", "README.rst", "README.txt", "README")
_GIT_TIMEOUT_SECONDS = 8
# Raised from 120: a production /ingest write was measured completing after
# ~134s, so a 120s client read timeout reported "timed out" while the server
# went on to store the document. Overridable per environment (see below).
DEFAULT_HTTP_TIMEOUT_SECONDS = 300.0
_HTTP_TIMEOUT_ENV = "CITADEL_CAPTURE_HTTP_TIMEOUT_SECONDS"
_README_LINE_LIMIT = 6
_README_LINE_MAX_CHARS = 500
DEFAULT_MAX_INGEST_BYTES = 200_000
_USERINFO_RE = re.compile(r"(https?://)[^/@\s]+@")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects so the Bearer token is never re-sent."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _max_ingest_bytes() -> int:
    raw = os.getenv("CITADEL_MCP_MAX_INGEST_BYTES")
    if not raw:
        return DEFAULT_MAX_INGEST_BYTES
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_INGEST_BYTES


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def http_timeout_seconds() -> float:
    """Effective capture read timeout: ``CITADEL_CAPTURE_HTTP_TIMEOUT_SECONDS`` or default."""
    raw = os.getenv(_HTTP_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_HTTP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HTTP_TIMEOUT_SECONDS
    return max(1.0, value)


def is_timeout_error(exc: BaseException) -> bool:
    """True when ``exc`` is a client-side timeout (bare or URLError-wrapped).

    A timeout only proves the client stopped waiting; the server may still
    finish the write after the deadline, so callers must report the outcome as
    unknown rather than failed.
    """
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, "reason", None), TimeoutError)


def capture_token() -> str:
    """Resolve the seat write token from the environment (never stored on disk).

    Only seat-scoped tokens are accepted: the admin-key fallback is intentionally
    omitted so capture never routes to Central with elevated privileges.
    """
    return (
        (os.getenv("CITADEL_MCP_ACCESS_TOKEN") or "").strip()
        or os.getenv("CITADEL_WRITER_KEYS", "").split(",")[0].strip()
    )


def _redact_url_userinfo(url: str) -> str:
    """Strip ``user:secret@`` credentials from an http(s) URL (defense in depth)."""
    return _USERINFO_RE.sub(r"\1", url)


def _git(root: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _readme_blurb(path: Path, limit: int = _README_LINE_LIMIT) -> str:
    for name in _README_NAMES:
        readme = path / name
        if not readme.exists():
            continue
        lines: list[str] = []
        try:
            with readme.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    lines.append(stripped[:_README_LINE_MAX_CHARS])
                    if len(lines) >= limit:
                        break
        except OSError:
            return ""
        return " ".join(lines)
    return ""


def summarize_root(root: CaptureRoot) -> str:
    """Build a compact markdown summary of a single approved root."""
    path = Path(root.path)
    name = path.name or root.path
    lines = [
        f"# Capture summary: {name}",
        "",
        f"- Path: `{root.path}`",
        f"- Capture Root Tags: {', '.join(root.tags)}",
    ]
    if not path.exists():
        lines.append("- Status: path not found on this machine")
        return "\n".join(lines)

    blurb = _readme_blurb(path)
    if (path / ".git").exists():
        remote = _git(root.path, "remote", "get-url", "origin")
        branch = _git(root.path, "rev-parse", "--abbrev-ref", "HEAD")
        last = _git(root.path, "log", "-1", "--format=%h %s")
        recent = _git(root.path, "log", "-5", "--format=- %h %s")
        if remote:
            lines.append(f"- Remote: {_redact_url_userinfo(remote)}")
        if branch:
            lines.append(f"- Branch: `{branch}`")
        if last:
            lines.append(f"- Latest: {last}")
        if blurb:
            lines += ["", "## README", blurb]
        if recent:
            lines += ["", "## Recent commits", recent]
    else:
        lines.append("- Status: non-git folder")
        if blurb:
            lines += ["", "## README", blurb]
    return "\n".join(lines)


def build_capture_payload(root: CaptureRoot) -> dict[str, Any]:
    """Ingest payload for a root: summary plus its tags and a `capture` marker.

    The summary is capped to ``CITADEL_MCP_MAX_INGEST_BYTES`` so an oversized
    README or commit log can never produce an unbounded POST body.
    """
    tags = list(normalize_tags([*root.tags, CAPTURE_TAG]))
    data = _truncate_utf8(summarize_root(root), _max_ingest_bytes())
    return {"data": data, "tags": tags}


def post_capture(
    node_url: str,
    token: str,
    payload: dict[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """POST a capture summary to the Node `/ingest` endpoint.

    ``timeout=None`` resolves to :func:`http_timeout_seconds` at call time so
    the env override applies without threading a value through every caller.
    """
    if timeout is None:
        timeout = http_timeout_seconds()
    try:
        require_secure_url(node_url)
    except InsecureEndpointError as error:
        raise ValueError("refusing non-HTTPS Node URL") from error
    req = urllib.request.Request(
        f"{node_url.rstrip('/')}/ingest",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # Local no-redirect opener (not the global one) so a 30x cannot leak the
    # Bearer token to a redirect target.
    with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())
