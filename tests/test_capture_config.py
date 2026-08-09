from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from kb.capture_config import (
    DEFAULT_NODE_URL,
    CaptureConfig,
    CaptureRoot,
    capture_config_path,
    load_capture_config,
    normalize_path,
    normalize_tags,
    save_capture_config,
)
from kb.cli import _parse_root_arg, _setup


def test_normalize_tags_strips_lowercases_dedupes() -> None:
    assert normalize_tags([" Personal ", "personal", "", "Org-Work"]) == (
        "personal",
        "org-work",
    )


def test_normalize_tags_defaults_to_personal() -> None:
    assert normalize_tags([]) == ("personal",)
    assert normalize_tags(["  "]) == ("personal",)


def test_normalize_path_expands_user_and_makes_absolute() -> None:
    assert normalize_path("~/x").startswith(str(Path.home()))
    assert os.path.isabs(normalize_path("rel/path"))


def test_with_root_replaces_same_path() -> None:
    config = CaptureConfig().with_root("/tmp/a", ["personal"])
    config = config.with_root("/tmp/a", ["org-work"])

    assert len(config.roots) == 1
    assert config.roots[0] == CaptureRoot(path="/tmp/a", tags=("org-work",))


def test_capture_config_path_honors_override(monkeypatch) -> None:
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", "/tmp/custom/capture.json")
    assert capture_config_path() == Path("/tmp/custom/capture.json")


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    config = (
        CaptureConfig(node_url="https://node.example")
        .with_root("/tmp/a", ["personal"])
        .with_root("/tmp/b", ["org-work", "Notes"])
    )
    save_capture_config(config, path=path, updated_at="2026-06-27T00:00:00+00:00")

    assert oct(path.stat().st_mode & 0o777) == "0o600"
    loaded = load_capture_config(path)
    assert loaded.node_url == "https://node.example"
    assert {r.path for r in loaded.roots} == {"/tmp/a", "/tmp/b"}
    assert loaded.roots[1].tags == ("org-work", "notes")
    assert loaded.updated_at == "2026-06-27T00:00:00+00:00"


def test_load_missing_returns_defaults(tmp_path: Path) -> None:
    config = load_capture_config(tmp_path / "absent.json")
    assert config.node_url == DEFAULT_NODE_URL
    assert config.roots == ()


def test_load_corrupt_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    path.write_text("{ not valid json")
    with pytest.raises(ValueError, match="corrupt capture config"):
        load_capture_config(path)


def test_load_non_object_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="expected a JSON object"):
        load_capture_config(path)


def test_from_dict_skips_bad_roots_and_version() -> None:
    config = CaptureConfig.from_dict(
        {
            "version": "not-an-int",
            "node_url": "https://node.example",
            "roots": ["bad-string", {"path": ""}, {"path": "/tmp/ok", "tags": ["personal"]}],
        }
    )
    assert config.version == 1
    assert [r.path for r in config.roots] == ["/tmp/ok"]


def test_parse_root_arg() -> None:
    assert _parse_root_arg("/tmp/a") == ("/tmp/a", ())
    assert _parse_root_arg("/tmp/a=personal,org-work") == (
        "/tmp/a",
        ("personal", "org-work"),
    )


def test_save_creates_private_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "dot-citadel" / "capture.json"
    save_capture_config(CaptureConfig().with_root("/tmp/a", ["personal"]), path=nested)
    assert oct(nested.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(nested.stat().st_mode & 0o777) == "0o600"


def test_setup_interactive_wizard(tmp_path: Path, monkeypatch) -> None:
    from kb import cli as cli_mod

    path = tmp_path / "capture.json"
    repo = tmp_path / "repo"
    answers = iter(
        [
            "https://wizard-node.example",  # Node URL prompt
            "n",  # decline the offered cwd default
            str(repo),  # first root path
            "org-work, notes",  # tags
            "",  # empty path -> finish
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli_mod.sys, "stdin", _Tty())

    args = argparse.Namespace(
        config=str(path), node_url=None, root=None, non_interactive=False, show=False
    )
    rc = asyncio.run(cli_mod._setup(args))

    assert rc == 0
    loaded = load_capture_config(path)
    assert loaded.node_url == "https://wizard-node.example"
    assert len(loaded.roots) == 1
    assert loaded.roots[0].tags == ("org-work", "notes")


def test_setup_non_interactive_writes_roots(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    args = argparse.Namespace(
        config=str(path),
        node_url="https://my-node.example/",
        root=["/tmp/work=org-work", "/tmp/notes"],
        non_interactive=True,
        show=False,
    )
    asyncio.run(_setup(args))

    loaded = load_capture_config(path)
    assert loaded.node_url == "https://my-node.example"
    by_path = {r.path: r.tags for r in loaded.roots}
    assert by_path["/tmp/work"] == ("org-work",)
    assert by_path["/tmp/notes"] == ("personal",)
    assert loaded.updated_at is not None


def test_setup_syncs_capture_roots_to_node(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "capture.json"
    calls: list[Any] = []

    def fake_sync(config: CaptureConfig, **kwargs: Any) -> Any:
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
        config=str(path),
        node_url="https://my-node.example",
        root=["/tmp/work=org-work"],
        non_interactive=True,
        show=False,
        json=False,
    )
    asyncio.run(_setup(args))
    assert len(calls) == 1
    assert calls[0][0].node_url == "https://my-node.example"


def test_setup_sync_failure_still_succeeds(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "capture.json"

    def fake_sync(config: CaptureConfig, **kwargs: Any) -> Any:
        from kb.capture_roots_sync import CaptureRootsSyncResult

        return CaptureRootsSyncResult(
            ok=False,
            status="failed",
            detail="timeout",
            seat_slug="alice",
        )

    monkeypatch.setattr("kb.cli.sync_local_capture_roots_to_server", fake_sync)
    args = argparse.Namespace(
        config=str(path),
        node_url="https://my-node.example",
        root=["/tmp/work=org-work"],
        non_interactive=True,
        show=False,
        json=False,
    )
    rc = asyncio.run(_setup(args))
    assert rc == 0
    assert "timeout" in capsys.readouterr().err
