"""Durable sync-state files: strict load, atomic save (#148).

A syncer state file has exactly two legitimate states: absent (a genuine
first run) or a parseable JSON object. Anything else is corruption — most
likely a restart that interrupted a non-atomic write — and must never be
flattened to an empty state, because empty is indistinguishable from "first
run": the next pass would treat every tracked item as new and emit a false
"everything changed" digest, then save over the corrupt file and destroy
the evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StateFileError(RuntimeError):
    """A sync state file exists but cannot be read as a JSON object.

    Raised instead of returning an empty state so corruption fails the run
    loudly. Recovery is an operator decision: restore the file from a backup,
    or delete it to explicitly start over from a first run.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            f"Sync state file {path} exists but is unreadable ({reason}). "
            "Refusing to treat corruption as a first run — restore the file "
            "or delete it to explicitly start over."
        )
        self.path = path
        self.reason = reason


def load_state_file(path: Path) -> dict[str, Any] | None:
    """Return the parsed state object, or None when the file has never existed.

    Raises :class:`StateFileError` when the file exists but cannot be read or
    is not a JSON object. Callers map None to their first-run default.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateFileError(path, exc.__class__.__name__) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateFileError(path, "invalid JSON") from exc
    if not isinstance(data, dict):
        raise StateFileError(path, f"expected a JSON object, got {type(data).__name__}")
    return data


def save_state_file(path: Path, payload: dict[str, Any]) -> None:
    """Write the state atomically (temp file + rename, the kb/access.py pattern).

    A restart mid-save leaves the previous file intact instead of a truncated
    one, so :func:`load_state_file` never has corruption to refuse.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)
