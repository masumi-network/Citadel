#!/usr/bin/env python3
"""Autonomous personal-KB sync for Citadel Archive — git pre-push hook.

Invoked from ``templates/git-pre-push.sh`` on every ``git push``. Snapshots
commit metadata (hash, message, author, branch, changed paths) and POSTs a
short note to the developer's private Citadel **Node** (``seat:{slug}``).

Commit snapshot payload (markdown ``data`` field):

* ``# Git commit snapshot``
* Commit full + short hash, author, ISO commit time
* Branch, remote name/ref, repo basename
* Subject + optional body (trimmed)
* ``## Changed files`` — paths from ``git diff-tree --name-only``

Design contract (same invariants as ``sync_session.py``):

* **One-token setup / personal-by-default.** ``CITADEL_MCP_ACCESS_TOKEN`` only;
  POST omits ``dataset`` so the seat-writer token routes to ``seat:{slug}``.
* **Metadata, not raw diffs.** File paths and commit **subject** only — no
  patch bodies and no commit message body (keeps snapshots small and avoids
  shipping long-form text that may contain secrets).
* **Allowlist-required (fail-closed).** Without ``~/.citadel/capture.json`` (or
  with an empty/corrupt allowlist), the hook captures nothing. ``citadel
  onboard`` / ``citadel setup`` must approve roots first.
* **Fail-silent / non-blocking.** Always exits 0; never blocks ``git push``.
* **HTTPS only.** Refuses non-``https://`` base URLs.
* **Size cap.** Truncates to ``CITADEL_MCP_MAX_INGEST_BYTES`` (default 200000).
* **Stdlib only.**

Pre-push stdin (one line per ref)::

    <local ref> SP <local sha1> SP <remote ref> SP <remote sha1> LF

Zero sha on local side means branch delete — skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MAX_INGEST_BYTES = 200_000
DEFAULT_BASE_URL = "https://citadel-archive-production.up.railway.app"
TOKEN_ENV = "CITADEL_MCP_ACCESS_TOKEN"
HTTP_TIMEOUT_SECONDS = 10
MAX_CHANGED_FILES = 80


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urllib.request.install_opener(urllib.request.build_opener(_NoRedirectHandler))


def _max_ingest_bytes() -> int:
    raw_value = os.getenv("CITADEL_MCP_MAX_INGEST_BYTES")
    if not raw_value:
        return DEFAULT_MAX_INGEST_BYTES
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_MAX_INGEST_BYTES
    return max(1, value)


def _base_url() -> str:
    configured = os.getenv("CITADEL_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return DEFAULT_BASE_URL


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def _git_run(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or None,
        capture_output=True,
        text=True,
        timeout=8,
    )


def git_toplevel(cwd: str = "") -> str:
    try:
        result = _git_run(cwd, "rev-parse", "--show-toplevel")
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return cwd or os.getcwd()


def ref_branch_name(ref: str) -> str:
    prefix = "refs/heads/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return ref.rsplit("/", 1)[-1] if ref else ""


def capture_config_path() -> Path:
    """Locate ~/.citadel/capture.json (the Approved Capture Roots allowlist)."""
    override = os.getenv("CITADEL_CAPTURE_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    home = os.getenv("CITADEL_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".citadel"
    return base / "capture.json"


def _norm_path(value: str) -> str:
    """Expand ~/$VARs, make absolute, and resolve symlinks (realpath).

    Symlink resolution matters on macOS where ``git rev-parse --show-toplevel``
    reports the physical path (``/private/tmp/x``) while a config root may be the
    symlinked path (``/tmp/x``); without it, an approved repo would be skipped.
    """
    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    return os.path.realpath(os.path.abspath(expanded))


def load_capture_roots() -> list[dict[str, Any]]:
    """Approved Capture Roots from the local config.

    Always fail-closed: missing, empty, or corrupt ``capture.json`` returns
    ``[]`` (approve nothing). Global capture without an explicit allowlist is
    never enabled — run ``citadel onboard`` or ``citadel setup`` first.
    """
    path = capture_config_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    roots: list[dict[str, Any]] = []
    for item in data.get("roots") or []:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path", "")).strip()
        if not raw_path:
            continue
        roots.append(
            {
                "path": _norm_path(raw_path),
                "tags": [
                    str(tag).strip().lower()
                    for tag in (item.get("tags") or [])
                    if str(tag).strip()
                ],
            }
        )
    return roots


def matched_root(repo_root: str, roots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the approved root containing ``repo_root``, if any."""
    target = _norm_path(repo_root)
    for root in roots:
        base = _norm_path(root["path"])
        prefix = base.rstrip(os.sep) + os.sep  # handles a root of "/" and trailing slashes
        if target == base or target.startswith(prefix):
            return root
    return None


def parse_pre_push_lines(text: str) -> list[dict[str, str]]:
    """Parse pre-push stdin into push ref dicts."""
    rows: list[dict[str, str]] = []
    zero = "0" * 40
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if local_sha == zero:
            continue
        rows.append(
            {
                "local_ref": local_ref,
                "local_sha": local_sha,
                "remote_ref": remote_ref,
                "remote_sha": remote_sha,
            }
        )
    return rows


def _commit_fields(cwd: str, sha: str) -> dict[str, str]:
    result = _git_run(
        cwd,
        "show",
        "-s",
        "--format=%H%x00%h%x00%an%x00%ae%x00%ci%x00%s%x00%b",
        sha,
    )
    if result.returncode != 0:
        return {}
    parts = result.stdout.split("\x00", 6)
    if len(parts) < 6:
        return {}
    keys = ("hash", "short", "author", "email", "committed_at", "subject", "body")
    data = dict(zip(keys, parts + [""] * (len(keys) - len(parts)), strict=False))
    return {key: value.strip() for key, value in data.items()}


def _changed_files(cwd: str, sha: str) -> list[str]:
    result = _git_run(cwd, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    if result.returncode != 0:
        return []
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return files[:MAX_CHANGED_FILES]


def _repo_name(cwd: str) -> str:
    top = git_toplevel(cwd)
    return os.path.basename(top.rstrip("/")) if top else ""


def format_commit_snapshot(
    *,
    commit_hash: str,
    short_hash: str,
    author: str,
    email: str,
    committed_at: str,
    subject: str,
    body: str = "",  # accepted for API compat; never included in the note
    branch: str,
    remote_name: str,
    remote_ref: str,
    repo_name: str,
    changed_files: list[str],
) -> str:
    """Build the markdown note posted to Citadel (pure function for tests)."""
    lines = ["# Git commit snapshot", ""]
    lines.append(f"- **Commit:** `{short_hash}` (`{commit_hash}`)")
    if author:
        lines.append(f"- **Author:** {author} <{email}>".rstrip())
    if committed_at:
        lines.append(f"- **Committed:** {committed_at}")
    if branch:
        lines.append(f"- **Branch:** {branch}")
    if remote_name:
        remote_bit = remote_name
        if remote_ref:
            remote_bit = f"{remote_name} ({ref_branch_name(remote_ref)})"
        lines.append(f"- **Remote:** {remote_bit}")
    if repo_name:
        lines.append(f"- **Repo:** {repo_name}")

    lines.append("")
    lines.append(f"**{subject or '(no subject)'}**")
    # Commit message bodies are intentionally omitted (subject + paths only).

    if changed_files:
        lines.append("")
        lines.append("## Changed files")
        for path in changed_files:
            lines.append(f"- {path}")

    return "\n".join(lines).strip()


def build_commit_snapshot(
    cwd: str,
    sha: str,
    *,
    local_ref: str = "",
    remote_name: str = "",
    remote_ref: str = "",
) -> str:
    """Collect git metadata and format the snapshot note."""
    root = git_toplevel(cwd)
    fields = _commit_fields(root, sha)
    if not fields:
        return ""
    return format_commit_snapshot(
        commit_hash=fields.get("hash") or sha,
        short_hash=fields.get("short") or sha[:7],
        author=fields.get("author", ""),
        email=fields.get("email", ""),
        committed_at=fields.get("committed_at", ""),
        subject=fields.get("subject", ""),
        body=fields.get("body", ""),
        branch=ref_branch_name(local_ref) if local_ref else _git_branch(root),
        remote_name=remote_name,
        remote_ref=remote_ref,
        repo_name=_repo_name(root),
        changed_files=_changed_files(root, sha),
    )


def _git_branch(cwd: str) -> str:
    try:
        result = _git_run(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        branch = result.stdout.strip()
        if result.returncode == 0 and branch and branch != "HEAD":
            return branch
    except Exception:
        pass
    return ""


def build_tags(cwd: str, branch: str = "") -> list[str]:
    tags = ["git-push"]
    if branch:
        tags.append(branch)
    repo = _repo_name(cwd)
    if repo and repo not in tags:
        tags.append(repo)
    return tags


def post_ingest(base_url: str, token: str, data: str, tags: list[str]) -> dict[str, Any] | None:
    """POST {data, tags} to {base}/ingest over HTTPS. No dataset field.

    Returns the parsed JSON response body (or ``None`` when the 2xx body is not
    a JSON object): a 2xx alone is not proof of storage, because the server
    states its decision in the body's ``accepted``/``reason`` fields and the
    receipt must record that decision, not the transport outcome.
    """
    if not base_url.lower().startswith("https://"):
        raise ValueError("refusing non-HTTPS Citadel base URL")
    url = f"{base_url}/ingest"
    body = json.dumps({"data": data, "tags": tags}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        raw = response.read()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def receipt_summary(response: dict[str, Any] | None, *, short_sha: str, branch: str) -> str:
    """Receipt line for a completed POST, mirroring the server's decision.

    Only an explicit ``accepted: true`` is reported as a capture. The test used
    to be ``is not False``, which reads a MISSING key as success — so an older
    Node, a proxy, or any response-shape change would have written "captured"
    for a write nobody ever confirmed. A verdict we did not observe is unknown,
    and unknown is not the optimistic value.
    """
    if response is None:
        return f"commit {short_sha} sent: server replied 2xx but the response body was unreadable"
    accepted = response.get("accepted")
    if accepted is True:
        return f"captured commit {short_sha} on {branch} → your Node"
    if accepted is False:
        reason = response.get("reason") or "rejected"
        return f"commit {short_sha} not stored: server rejected the write ({reason})"
    return (
        f"commit {short_sha} capture unconfirmed: the server's 2xx response did not "
        "state whether the write was accepted"
    )


def _send_failure_summary(exc: BaseException, *, short_sha: str) -> str:
    """Receipt line for a POST that raised before a response was read."""
    if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
        # A client timeout is not proof of failure: the server can finish the
        # write after the deadline, so the honest verdict is "unconfirmed".
        return (
            f"commit {short_sha} capture unconfirmed: no response within "
            f"{HTTP_TIMEOUT_SECONDS}s; the write may still have completed on the server"
        )
    # Class name only: an exception message could echo request details.
    return f"commit {short_sha} not captured: send failed ({exc.__class__.__name__})"


# Receipt kinds. A capture attempt, a deliberate skip, and a crash are three
# different events; they shared one kind (or, for skips and crashes, produced no
# line at all), so `citadel activity --local` could not tell "ran and had nothing
# to send" from "the token got unset" or "it blew up".
RECEIPT_KIND = "push"
RECEIPT_KIND_SKIP = "push-skip"
RECEIPT_KIND_ERROR = "push-error"


def _write_receipt(summary: str, kind: str = RECEIPT_KIND) -> None:
    """Best-effort DX-5 receipt (never raises, never surfaces the token)."""
    try:
        from kb.hooks.receipt import write_receipt

        write_receipt(kind, summary)
    except Exception:
        pass


def _sync_one(
    cwd: str,
    sha: str,
    *,
    local_ref: str,
    remote_name: str,
    remote_ref: str,
    token: str,
    capture_tags: list[str] | tuple[str, ...] = (),
) -> None:
    note = build_commit_snapshot(
        cwd,
        sha,
        local_ref=local_ref,
        remote_name=remote_name,
        remote_ref=remote_ref,
    )
    if not note.strip():
        _write_receipt(
            f"commit {sha[:7]} skipped: no snapshot could be built from git metadata",
            kind=RECEIPT_KIND_SKIP,
        )
        return
    note = _truncate_utf8(note, _max_ingest_bytes())
    branch = ref_branch_name(local_ref) if local_ref else _git_branch(cwd)
    tags = build_tags(cwd, branch)
    for tag in capture_tags:
        if tag not in tags:
            tags.append(tag)
    # DX-5 receipt: make the silent capture visible AND truthful. The receipt
    # records what the server said (or that we cannot know), never an
    # unconditional "captured". A failed send is recorded here rather than
    # raised, so one bad ref still leaves a receipt and never blocks the push.
    try:
        response = post_ingest(_base_url(), token, note, tags)
    except Exception as exc:
        _write_receipt(_send_failure_summary(exc, short_sha=sha[:7]))
        return
    _write_receipt(receipt_summary(response, short_sha=sha[:7], branch=branch))


def run(stdin: Any, remote_name: str = "") -> int:
    """Hook entrypoint. ALWAYS returns 0 — fail-silent, non-blocking.

    Fail-silent means never blocking the push, not leaving no trace. Every exit
    below writes a receipt, because exit 0 with no output was the same observable
    outcome for "ran and legitimately had nothing to send", "the token env var is
    unset", "capture.json is corrupt", and "it crashed" — and the product's
    "capture runs without you" promise rests on being able to tell those apart.
    """
    try:
        token = os.getenv(TOKEN_ENV)
        if not token:
            _write_receipt(
                f"push skipped: {TOKEN_ENV} is not set in this environment "
                "(run `citadel onboard`)",
                kind=RECEIPT_KIND_SKIP,
            )
            return 0

        cwd = git_toplevel()

        # ADR-0007 P4.3 (fail-closed): only push from an Approved Capture Root.
        # Missing/empty/corrupt ~/.citadel/capture.json → capture nothing.
        roots = load_capture_roots()
        capture_tags: list[str] = []
        if not roots:
            sys.stderr.write(
                "citadel: no Approved Capture Roots configured; skipping "
                "capture (run `citadel onboard` or `citadel setup`).\n"
            )
            _write_receipt(
                "push skipped: no Approved Capture Roots configured "
                "(run `citadel onboard` or `citadel setup`)",
                kind=RECEIPT_KIND_SKIP,
            )
            return 0
        match = matched_root(cwd, roots)
        if match is None:
            sys.stderr.write(
                f"citadel: {cwd} is not an Approved Capture Root; skipping "
                "capture (run `citadel setup` to approve it).\n"
            )
            # Basename only: stderr is ephemeral, the receipt log is not, and the
            # full path adds nothing a dev running in the repo cannot already see.
            _write_receipt(
                f"push skipped: {os.path.basename(cwd.rstrip(os.sep)) or cwd} is not an "
                "Approved Capture Root (run `citadel setup` to approve it)",
                kind=RECEIPT_KIND_SKIP,
            )
            return 0
        capture_tags = list(match["tags"])

        raw = stdin.read() if hasattr(stdin, "read") else ""
        pushes = parse_pre_push_lines(raw)

        if pushes:
            seen: set[str] = set()
            for row in pushes:
                sha = row["local_sha"]
                if sha in seen:
                    continue
                seen.add(sha)
                _sync_one(
                    cwd,
                    sha,
                    local_ref=row["local_ref"],
                    remote_name=remote_name,
                    remote_ref=row["remote_ref"],
                    token=token,
                    capture_tags=capture_tags,
                )
            return 0

        # Manual invocation (no pre-push stdin): snapshot HEAD once.
        head = _git_run(cwd, "rev-parse", "HEAD")
        sha = head.stdout.strip() if head.returncode == 0 else ""
        if not sha:
            _write_receipt(
                "push skipped: git could not resolve HEAD, so there is nothing to snapshot",
                kind=RECEIPT_KIND_SKIP,
            )
            return 0
        _sync_one(
            cwd,
            sha,
            local_ref="",
            remote_name=remote_name,
            remote_ref="",
            token=token,
            capture_tags=capture_tags,
        )
    except Exception as exc:
        # Class name only: an exception message can echo local paths or request
        # details, and this line lands in a file that outlives the push.
        _write_receipt(
            f"push not run: the hook failed before sending ({exc.__class__.__name__})",
            kind=RECEIPT_KIND_ERROR,
        )
        return 0
    return 0


def main() -> None:
    remote = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(run(sys.stdin, remote_name=remote))


if __name__ == "__main__":
    main()
