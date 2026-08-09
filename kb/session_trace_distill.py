"""Stdlib-only Session Trace distillation (SessionEnd hook safe)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import subprocess
from typing import Any, Literal

Resolution = Literal["solved", "superseded", "dead_end"]

_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bctdl_[A-Za-z0-9_-]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_KEY]"),
    (
        re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s'\"`<>]+"),
        "[REDACTED_DATABASE_URL]",
    ),
    (re.compile(r"(?i)(?:--token=|--api-key=|--password=)[^\s]+"), "=[REDACTED]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b"), "Bearer [REDACTED]"),
)


@dataclass(frozen=True)
class ToolErrorPair:
    tool_use: str
    is_error: bool
    error_text: str


@dataclass(frozen=True)
class DeadEnd:
    tried: str
    failed_because: str
    resolution: Resolution


@dataclass(frozen=True)
class SessionTraceRecord:
    task: str
    approach: str
    dead_ends: tuple[DeadEnd, ...]
    files: tuple[str, ...]
    commands: tuple[str, ...]
    repo: str
    branch: str
    author_seat: str
    created_at: str
    tool_error_pairs: tuple[ToolErrorPair, ...] = ()

    @property
    def has_tool_errors(self) -> bool:
        return any(pair.is_error for pair in self.tool_error_pairs)


def _truncate(text: str, limit: int) -> str:
    normalized = text.replace("\n", " ").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def iter_transcript_entries(transcript_path: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return entries
    return entries


def content_blocks(entry: dict[str, Any]) -> list[Any]:
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def text_of(block: Any) -> str:
    if isinstance(block, dict) and isinstance(block.get("text"), str):
        return block["text"].strip()
    if isinstance(block, str):
        return block.strip()
    return ""


def first_user_prompt(entries: list[dict[str, Any]]) -> str:
    for entry in entries:
        if entry.get("type") != "user":
            continue
        for block in content_blocks(entry):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                continue
            text = text_of(block)
            if text:
                return text
    return ""


def git_branch(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = result.stdout.strip()
        if result.returncode == 0 and branch and branch != "HEAD":
            return branch
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def repo_name(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
        top = result.stdout.strip()
        if result.returncode == 0 and top:
            return os.path.basename(top)
    except (OSError, subprocess.SubprocessError):
        pass
    if cwd:
        return os.path.basename(cwd.rstrip("/"))
    return ""


def redact_commands(commands: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    redacted: list[str] = []
    for command in commands:
        text = command
        for pattern, replacement in _REDACT_PATTERNS:
            text = pattern.sub(replacement, text)
        redacted.append(_truncate(text, 200))
    return tuple(redacted)


def _deterministic_dead_ends(pairs: list[ToolErrorPair]) -> tuple[DeadEnd, ...]:
    dead_ends: list[DeadEnd] = []
    for pair in pairs:
        if not pair.is_error:
            continue
        dead_ends.append(
            DeadEnd(
                tried=_truncate(f"{pair.tool_use}", 200),
                failed_because=_truncate(pair.error_text or "tool failed", 200),
                resolution="dead_end",
            )
        )
    return tuple(dead_ends[:6])


def distill_trace(
    entries: list[dict[str, Any]],
    *,
    cwd: str,
    author_seat: str,
    created_at: str | None = None,
) -> SessionTraceRecord:
    from datetime import datetime, timezone

    files: list[str] = []
    seen_files: set[str] = set()
    commands: list[str] = []
    tool_error_pairs: list[ToolErrorPair] = []
    last_assistant_text = ""
    pending_tool: str | None = None

    for entry in entries:
        etype = entry.get("type")
        for block in content_blocks(entry):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = str(block.get("name") or "tool")
                pending_tool = name
                inp = block.get("input")
                if isinstance(inp, dict):
                    if isinstance(inp.get("command"), str):
                        commands.append(inp["command"])
                    path = inp.get("file_path") or inp.get("notebook_path")
                    if isinstance(path, str) and path and path not in seen_files:
                        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                            seen_files.add(path)
                            files.append(path)
            elif btype == "tool_result":
                is_error = bool(block.get("is_error"))
                error_text = text_of(block)
                tool_name = pending_tool or "tool"
                tool_error_pairs.append(
                    ToolErrorPair(
                        tool_use=tool_name,
                        is_error=is_error,
                        error_text=_truncate(error_text, 200),
                    )
                )
                pending_tool = None
            elif btype == "text" and etype == "assistant":
                text = text_of(block)
                if text:
                    last_assistant_text = text

    task = _truncate(first_user_prompt(entries), 280)
    approach = _truncate(last_assistant_text, 500)
    timestamp = created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return SessionTraceRecord(
        task=task or "Session captured (no extractable task).",
        approach=approach or "No extractable outcome.",
        dead_ends=_deterministic_dead_ends(tool_error_pairs),
        files=tuple(files[:40]),
        commands=redact_commands(tuple(commands[:12])),
        repo=repo_name(cwd),
        branch=git_branch(cwd),
        author_seat=author_seat,
        created_at=timestamp,
        tool_error_pairs=tuple(tool_error_pairs),
    )


def distill_node_note(entries: list[dict[str, Any]]) -> str:
    files: list[str] = []
    seen_files: set[str] = set()
    last_assistant_text = ""
    decisions: list[str] = []
    decision_markers = (
        "decided",
        "decision",
        "chose",
        "we'll use",
        "going with",
        "approach",
        "instead of",
        "root cause",
    )

    for entry in entries:
        etype = entry.get("type")
        for block in content_blocks(entry):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name")
                inp = block.get("input")
                if isinstance(inp, dict) and name in (
                    "Edit",
                    "Write",
                    "MultiEdit",
                    "NotebookEdit",
                ):
                    path = inp.get("file_path") or inp.get("notebook_path")
                    if isinstance(path, str) and path and path not in seen_files:
                        seen_files.add(path)
                        files.append(path)
            elif btype == "text" and etype == "assistant":
                text = text_of(block)
                if not text:
                    continue
                last_assistant_text = text
                lowered = text.lower()
                if any(marker in lowered for marker in decision_markers):
                    snippet = _truncate(text, 200)
                    if snippet not in decisions:
                        decisions.append(snippet)

    first_prompt = first_user_prompt(entries)
    if not (first_prompt or last_assistant_text or decisions or files):
        # Nothing extractable. Return "" so the caller can skip the send: a
        # placeholder note would be byte-identical for every empty session, so
        # it could be stored at most once and each later send would be refused
        # as already-seen content while the receipt claimed capture.
        return ""
    lines: list[str] = ["# Dev session note"]
    recap_bits: list[str] = []
    if first_prompt:
        recap_bits.append(f"Task: {_truncate(first_prompt, 280)}")
    if last_assistant_text:
        recap_bits.append(f"Outcome: {_truncate(last_assistant_text, 280)}")
    if not recap_bits:
        recap_bits.append("Session captured (no extractable prompt/outcome text).")
    lines.append("")
    lines.extend(recap_bits)
    if decisions:
        lines.append("")
        lines.append("## Key decisions / facts")
        for item in decisions[:8]:
            lines.append(f"- {item}")
    if files:
        lines.append("")
        lines.append("## Files changed")
        for path in files[:40]:
            lines.append(f"- {path}")
    return "\n".join(lines).strip()


def format_compact_context(record: SessionTraceRecord) -> str:
    lines = [
        "# Shared Session Trace",
        f"Author-Seat: {record.author_seat}",
        f"Created-At: {record.created_at}",
    ]
    if record.repo:
        lines.append(f"Repo: {record.repo}")
    if record.branch:
        lines.append(f"Branch: {record.branch}")
    lines.extend(["", f"Task: {record.task}", f"Approach: {record.approach}"])
    if record.dead_ends:
        lines.extend(["", "## Dead ends"])
        for item in record.dead_ends:
            lines.append(
                f"- tried: {item.tried} | failed_because: {item.failed_because} "
                f"| resolution: {item.resolution}"
            )
    if record.files:
        lines.extend(["", "## Files"])
        for path in record.files:
            lines.append(f"- {path}")
    if record.commands:
        lines.extend(["", "## Commands (redacted)"])
        for command in record.commands:
            lines.append(f"- {command}")
    return "\n".join(lines).strip()
