"""Strict sync-state loading (#148).

A state file has exactly two legitimate states: absent (first run) or a
parseable JSON object. Anything else is corruption — most likely a restart
that interrupted a non-atomic write — and must raise instead of flattening
to an empty state, because empty is indistinguishable from "first run" and
the next pass would emit a false "everything changed" digest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kb.state_io import StateFileError, load_state_file, save_state_file


def test_missing_file_is_first_run(tmp_path: Path) -> None:
    assert load_state_file(tmp_path / "never_written.json") is None


def test_truncated_json_raises_instead_of_flattening(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    # A restart mid-write truncates the file; the tail of the object is gone.
    path.write_text('{"version": 1, "repos": {"masumi-network/agen', encoding="utf-8")

    with pytest.raises(StateFileError) as excinfo:
        load_state_file(path)

    # The message must carry enough for an operator to act on it.
    assert str(path) in str(excinfo.value)


def test_non_object_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(StateFileError):
        load_state_file(path)


def test_save_then_load_round_trips_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    payload: dict[str, Any] = {"version": 1, "files": {"a.md": {"sha": "abc"}}}

    save_state_file(path, payload)

    assert load_state_file(path) == payload
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_interrupted_save_leaves_previous_state_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write is temp-file + rename, so dying mid-save cannot truncate."""
    path = tmp_path / "state.json"
    save_state_file(path, {"version": 1, "repos": {"a": 1}})

    def die(self: Path, target: Path) -> None:
        raise OSError("killed before rename")

    monkeypatch.setattr(Path, "replace", die)
    with pytest.raises(OSError):
        save_state_file(path, {"version": 1, "repos": {}})
    monkeypatch.undo()

    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1, "repos": {"a": 1}}
