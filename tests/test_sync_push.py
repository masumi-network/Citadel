from __future__ import annotations

import io
import json
import os
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kb.hooks import sync_push


def test_parse_pre_push_lines_skips_deletes() -> None:
    zero = "0" * 40
    text = "\n".join(
        [
            f"refs/heads/main abcdef0123456789abcdef0123456789abcdef0 refs/heads/main {zero}",
            f"refs/heads/del {zero} refs/heads/del abcdef0123456789abcdef0123456789abcdef0",
        ]
    )
    rows = sync_push.parse_pre_push_lines(text)
    assert len(rows) == 1
    assert rows[0]["local_sha"].startswith("abcdef")


def test_format_commit_snapshot_includes_metadata() -> None:
    note = sync_push.format_commit_snapshot(
        commit_hash="a" * 40,
        short_hash="abc1234",
        author="John Doe",
        email="john@example.com",
        committed_at="2026-06-25 10:00:00 +0000",
        subject="feat: add git push sync",
        body="Optional body line with secrets=should-not-appear",
        branch="main",
        remote_name="origin",
        remote_ref="refs/heads/main",
        repo_name="Citadel-Archive",
        changed_files=["kb/foo.py", "tests/test_sync_push.py"],
    )
    assert "Git commit snapshot" in note
    assert "abc1234" in note
    assert "John Doe" in note
    assert "feat: add git push sync" in note
    assert "kb/foo.py" in note
    assert "origin (main)" in note
    assert "Optional body line" not in note
    assert "should-not-appear" not in note


def test_missing_token_no_post(monkeypatch: Any) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    recorder: list[Any] = []

    def fake_sync(*args: Any, **kwargs: Any) -> None:
        recorder.append((args, kwargs))

    monkeypatch.setattr(sync_push, "_sync_one", fake_sync)
    stdin = io.StringIO(
        "refs/heads/main abcdef0123456789abcdef0123456789abcdef0 refs/heads/main "
        + "0" * 40
        + "\n"
    )
    assert sync_push.run(stdin, remote_name="origin") == 0
    assert recorder == []


def test_post_omits_dataset_field(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setenv("CITADEL_BASE_URL", "https://example.invalid")
    # Fail-closed allowlist: approve the fake repo root used below.
    config = tmp_path / "capture.json"
    _write_capture_config(config, [{"path": "/tmp/repo", "tags": ["personal"]}])
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(config))

    captured: dict[str, Any] = {}

    class _FakeResp:
        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def fake_urlopen(request: Any, timeout: int | None = None) -> _FakeResp:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = dict(request.header_items())
        return _FakeResp()

    monkeypatch.setattr(sync_push.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        sync_push,
        "build_commit_snapshot",
        lambda *args, **kwargs: "# Git commit snapshot\n\n**test**",
    )
    monkeypatch.setattr(sync_push, "git_toplevel", lambda cwd="": "/tmp/repo")
    monkeypatch.setattr(sync_push, "build_tags", lambda cwd, branch="": ["git-push"])

    stdin = io.StringIO(
        "refs/heads/feature abcdef0123456789abcdef0123456789abcdef0 "
        "refs/heads/feature "
        + "0" * 40
        + "\n"
    )
    assert sync_push.run(stdin, remote_name="origin") == 0
    body = captured["body"]
    assert "dataset" not in body
    assert "git-push" in body["tags"]
    assert "personal" in body["tags"]
    assert captured["headers"].get("Authorization") == "Bearer ctdl_test_token"


def test_post_refuses_non_https() -> None:
    with pytest.raises(ValueError):
        sync_push.post_ingest("http://example.invalid", "ctdl_x", "note", ["git-push"])


def test_run_swallows_post_errors(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    config = tmp_path / "capture.json"
    _write_capture_config(config, [{"path": "/tmp/repo", "tags": ["personal"]}])
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(config))
    monkeypatch.setattr(sync_push, "git_toplevel", lambda cwd="": "/tmp/repo")

    called: list[bool] = []

    def boom(*args: Any, **kwargs: Any) -> None:
        called.append(True)
        raise RuntimeError("network down")

    monkeypatch.setattr(sync_push, "_sync_one", boom)
    stdin = io.StringIO(
        "refs/heads/main abcdef0123456789abcdef0123456789abcdef0 refs/heads/main "
        + "0" * 40
        + "\n"
    )
    assert sync_push.run(stdin, remote_name="origin") == 0
    assert called  # proves the swallowed-exception path actually executed


def test_ref_branch_name() -> None:
    assert sync_push.ref_branch_name("refs/heads/feature/foo") == "feature/foo"


def _write_capture_config(path: Path, roots: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps({"version": 1, "roots": roots}))


def test_load_capture_roots_empty_when_absent(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(tmp_path / "absent.json"))
    assert sync_push.load_capture_roots() == []


def test_run_absent_config_fails_closed(monkeypatch: Any, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_push, "git_toplevel", lambda cwd="": "/tmp/some-repo")

    posted: list[Any] = []
    monkeypatch.setattr(sync_push, "_sync_one", lambda *a, **k: posted.append(k))

    stdin = io.StringIO(
        "refs/heads/main abcdef0123456789abcdef0123456789abcdef0 refs/heads/main "
        + "0" * 40
        + "\n"
    )
    assert sync_push.run(stdin, remote_name="origin") == 0
    assert posted == []
    assert "no Approved Capture Roots" in capsys.readouterr().err


def test_load_capture_roots_empty_on_corrupt(monkeypatch: Any, tmp_path: Path) -> None:
    config = tmp_path / "capture.json"
    config.write_text("{ not json")
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(config))
    # Fail closed: corrupt config approves nothing (empty list), not None.
    assert sync_push.load_capture_roots() == []


def test_matched_root_containment(tmp_path: Path) -> None:
    roots = [{"path": "/tmp/work", "tags": ["org-work"]}]
    assert sync_push.matched_root("/tmp/work/sub", roots)["tags"] == ["org-work"]
    assert sync_push.matched_root("/tmp/worktree", roots) is None


def test_matched_root_slash_matches_any() -> None:
    roots = [{"path": "/", "tags": ["personal"]}]
    assert sync_push.matched_root("/anywhere/at/all", roots)["tags"] == ["personal"]


def test_matched_root_resolves_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    roots = [{"path": str(link), "tags": ["org-work"]}]
    # git reports the physical path; it must still match the symlinked config root.
    assert sync_push.matched_root(str(real / "sub"), roots)["tags"] == ["org-work"]


def test_run_corrupt_config_fails_closed(monkeypatch: Any, tmp_path: Path) -> None:
    config = tmp_path / "capture.json"
    config.write_text("{ broken")
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(config))
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_push, "git_toplevel", lambda cwd="": "/tmp/some-repo")

    posted: list[Any] = []
    monkeypatch.setattr(sync_push, "_sync_one", lambda *a, **k: posted.append(k))

    stdin = io.StringIO(
        "refs/heads/main abcdef0123456789abcdef0123456789abcdef0 refs/heads/main "
        + "0" * 40
        + "\n"
    )
    assert sync_push.run(stdin, remote_name="origin") == 0
    assert posted == []  # corrupt allowlist captures nothing


def test_run_skips_repo_outside_allowlist(monkeypatch: Any, tmp_path: Path, capsys) -> None:
    config = tmp_path / "capture.json"
    _write_capture_config(config, [{"path": "/some/approved", "tags": ["personal"]}])
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(config))
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_push, "git_toplevel", lambda cwd="": "/tmp/other-repo")

    posted: list[Any] = []
    monkeypatch.setattr(sync_push, "_sync_one", lambda *a, **k: posted.append(k))

    stdin = io.StringIO(
        "refs/heads/main abcdef0123456789abcdef0123456789abcdef0 refs/heads/main "
        + "0" * 40
        + "\n"
    )
    assert sync_push.run(stdin, remote_name="origin") == 0
    assert posted == []
    assert "not an Approved Capture Root" in capsys.readouterr().err


def test_run_captures_approved_repo_with_root_tags(monkeypatch: Any, tmp_path: Path) -> None:
    config = tmp_path / "capture.json"
    _write_capture_config(config, [{"path": "/tmp/approved", "tags": ["org-work"]}])
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(config))
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_push, "git_toplevel", lambda cwd="": "/tmp/approved")

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sync_push, "_sync_one", lambda *a, **k: calls.append(k))

    stdin = io.StringIO(
        "refs/heads/main abcdef0123456789abcdef0123456789abcdef0 refs/heads/main "
        + "0" * 40
        + "\n"
    )
    assert sync_push.run(stdin, remote_name="origin") == 0
    assert len(calls) == 1
    assert calls[0]["capture_tags"] == ["org-work"]


# --- receipts tell the truth about what the server decided -------------------


_PUSH_STDIN = (
    "refs/heads/main abcdef0123456789abcdef0123456789abcdef0 refs/heads/main " + "0" * 40 + "\n"
)


def _approve_repo_for_real_sync(monkeypatch: Any, tmp_path: Path) -> None:
    """Approve /tmp/repo and stub git so _sync_one runs for real up to the POST."""
    config = tmp_path / "capture.json"
    _write_capture_config(config, [{"path": "/tmp/repo", "tags": []}])
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(config))
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_test_token")
    monkeypatch.setattr(sync_push, "git_toplevel", lambda cwd="": "/tmp/repo")
    monkeypatch.setattr(
        sync_push,
        "build_commit_snapshot",
        lambda *args, **kwargs: "# Git commit snapshot\n\n**test**",
    )
    monkeypatch.setattr(sync_push, "build_tags", lambda cwd, branch="": ["git-push"])


def _fake_urlopen_with_body(monkeypatch: Any, body: bytes) -> None:
    class _Resp:
        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return body

    monkeypatch.setattr(
        sync_push.urllib.request, "urlopen", lambda request, timeout=None: _Resp()
    )


def _receipt_text() -> str:
    from kb.hooks.receipt import activity_log_path

    path = activity_log_path()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_receipt_on_accepted_says_captured(monkeypatch: Any, tmp_path: Path) -> None:
    _approve_repo_for_real_sync(monkeypatch, tmp_path)
    _fake_urlopen_with_body(
        monkeypatch,
        json.dumps({"accepted": True, "reason": "accepted", "cognee_result": {}}).encode(),
    )
    assert sync_push.run(io.StringIO(_PUSH_STDIN), remote_name="origin") == 0
    receipt = _receipt_text()
    assert "captured commit abcdef0" in receipt
    assert "ctdl_test_token" not in receipt


def test_receipt_records_rejection_not_capture(monkeypatch: Any, tmp_path: Path) -> None:
    _approve_repo_for_real_sync(monkeypatch, tmp_path)
    _fake_urlopen_with_body(
        monkeypatch,
        json.dumps(
            {
                "accepted": False,
                "reason": "duplicate_in_process",
                "dataset": "seat:test",
                "tags": ["git-push"],
                "cognee_result": None,
            }
        ).encode(),
    )
    assert sync_push.run(io.StringIO(_PUSH_STDIN), remote_name="origin") == 0
    receipt = _receipt_text()
    assert "not stored" in receipt
    assert "duplicate_in_process" in receipt
    assert "captured commit" not in receipt
    assert "ctdl_test_token" not in receipt


def test_receipt_on_unreadable_body_does_not_claim_capture(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _approve_repo_for_real_sync(monkeypatch, tmp_path)
    _fake_urlopen_with_body(monkeypatch, b"not json")
    assert sync_push.run(io.StringIO(_PUSH_STDIN), remote_name="origin") == 0
    receipt = _receipt_text()
    assert "unreadable" in receipt
    assert "captured commit" not in receipt


def test_receipt_on_timeout_says_write_may_have_completed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _approve_repo_for_real_sync(monkeypatch, tmp_path)

    def boom(request: Any, timeout: int | None = None) -> None:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(sync_push.urllib.request, "urlopen", boom)
    assert sync_push.run(io.StringIO(_PUSH_STDIN), remote_name="origin") == 0
    receipt = _receipt_text()
    assert "may still have completed" in receipt
    assert "captured commit" not in receipt
    assert "ctdl_test_token" not in receipt


def test_receipt_on_wrapped_timeout_says_write_may_have_completed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    # urllib wraps a connect timeout as URLError(reason=TimeoutError).
    _approve_repo_for_real_sync(monkeypatch, tmp_path)

    def boom(request: Any, timeout: int | None = None) -> None:
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(sync_push.urllib.request, "urlopen", boom)
    assert sync_push.run(io.StringIO(_PUSH_STDIN), remote_name="origin") == 0
    receipt = _receipt_text()
    assert "may still have completed" in receipt
    assert "captured commit" not in receipt


def test_receipt_on_send_failure_names_class_only(monkeypatch: Any, tmp_path: Path) -> None:
    _approve_repo_for_real_sync(monkeypatch, tmp_path)

    def boom(request: Any, timeout: int | None = None) -> None:
        raise RuntimeError("boom with sensitive detail")

    monkeypatch.setattr(sync_push.urllib.request, "urlopen", boom)
    assert sync_push.run(io.StringIO(_PUSH_STDIN), remote_name="origin") == 0
    receipt = _receipt_text()
    assert "not captured" in receipt
    assert "RuntimeError" in receipt
    assert "sensitive detail" not in receipt
    assert "captured commit" not in receipt


def test_unreachable_node_still_exits_zero(monkeypatch: Any, tmp_path: Path) -> None:
    # The pre-push hook must never block a `git push`, whatever the network does.
    _approve_repo_for_real_sync(monkeypatch, tmp_path)

    def boom(request: Any, timeout: int | None = None) -> None:
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(sync_push.urllib.request, "urlopen", boom)
    assert sync_push.run(io.StringIO(_PUSH_STDIN), remote_name="origin") == 0
    receipt = _receipt_text()
    assert "not captured" in receipt
    assert "captured commit" not in receipt
