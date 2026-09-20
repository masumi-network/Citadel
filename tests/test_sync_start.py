from __future__ import annotations

import io
import json
import subprocess
from typing import Any

import pytest

from kb.hooks import sync_start


def _assert_policy_only(capsys: Any) -> None:
    out = capsys.readouterr().out
    assert out == sync_start.AGENT_POLICY_REMINDER + "\n"
    assert "# Citadel workspace continuity" not in out


def test_no_token_skips_identity_and_workspace_search(monkeypatch, capsys) -> None:
    monkeypatch.delenv(sync_start.TOKEN_ENV, raising=False)
    identity_calls: list[Any] = []

    def fake_identity(cwd: Any) -> tuple[str, str]:
        identity_calls.append(cwd)
        return ("Citadel", "main")

    monkeypatch.setattr(sync_start, "_workspace_identity", fake_identity)
    search_calls: list[Any] = []

    def fail_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        search_calls.append((args, kwargs))
        raise AssertionError("workspace search must be gated on the token")

    monkeypatch.setattr(sync_start, "_search_workspace", fail_search)

    assert sync_start.run(io.StringIO('{"cwd": "/workspace"}')) == 0
    assert identity_calls == []
    assert search_calls == []
    _assert_policy_only(capsys)


def test_no_strict_git_identity_skips_workspace_search(monkeypatch, capsys) -> None:
    monkeypatch.setenv(sync_start.TOKEN_ENV, "ctdl_test_token")
    monkeypatch.setattr(sync_start, "_workspace_identity", lambda cwd: None)
    search_calls: list[Any] = []

    def fail_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        search_calls.append((args, kwargs))
        raise AssertionError("workspace search requires a strict Git identity")

    monkeypatch.setattr(sync_start, "_search_workspace", fail_search)

    assert sync_start.run(io.StringIO('{"cwd": "/workspace"}')) == 0
    assert search_calls == []
    _assert_policy_only(capsys)


def test_run_formats_bounded_workspace_continuity(monkeypatch, capsys) -> None:
    token = "ctdl_test_token"
    monkeypatch.setenv(sync_start.TOKEN_ENV, token)
    monkeypatch.setenv("CITADEL_BASE_URL", "https://node.example/")
    monkeypatch.setattr(
        sync_start,
        "_workspace_identity",
        lambda cwd: ("Citadel", "feature/continuity"),
    )
    calls: list[tuple[str, str, str, str]] = []
    search_payload = {
        "results": [
            {
                "text": "candidate one",
                "repo": "Citadel",
                "id": "internal-hit-001",
                "_citadel": {
                    "dataset": "central",
                    "trust_tier": "attested",
                    "result_id": "internal-result-001",
                    "provenance": {
                        "repo": "Citadel",
                        "source_url": "https://docs.example/continuity/one",
                        "source_id": "internal-source-001",
                    },
                },
            },
            {
                "text": "candidate two",
                "id": "internal-hit-002",
                "_citadel": {"provenance": {"repo": "org/Citadel"}},
            },
            {
                "text": "candidate three",
                "citation": {"repo": "Citadel"},
                "id": "internal-hit-003",
                "_citadel": {
                    "trust": "reference-only",
                    "provenance": {
                        "repo": "Citadel",
                        "url": "https://docs.example/continuity/three",
                    },
                },
            },
            {"text": "candidate four", "repo": "Citadel", "id": "internal-hit-004"},
            {"text": "candidate body-only mentions Citadel", "id": "internal-hit-005"},
        ],
        "candidate_page": {
            "limit": 5,
            "fetched": 5,
            "matched": 5,
            "returned": 5,
            "selection_trimmed": True,
            "upstream_truncation": False,
        },
        "absence": {"proven": False, "reason": "no exact workspace match"},
    }

    def fake_search(
        base_url: str,
        received_token: str,
        *,
        repo_name: str,
        branch: str,
    ) -> dict[str, Any]:
        calls.append((base_url, received_token, repo_name, branch))
        return search_payload

    monkeypatch.setattr(sync_start, "_search_workspace", fake_search)

    assert sync_start.run(io.StringIO('{"cwd": "/workspace"}')) == 0
    assert calls == [
        ("https://node.example", token, "Citadel", "feature/continuity")
    ]

    out = capsys.readouterr().out
    assert "# Citadel workspace continuity" in out
    assert "Task prompt was unavailable" in out
    assert "not exhaustive" in out
    assert "limit=5" in out
    assert "fetched=5" in out
    assert "matched=5" in out
    assert "returned=5" in out
    assert "selection trimmed=yes" in out
    assert "upstream truncation=no" in out
    assert "absence proven=no" in out
    assert "absence is not proven." in out
    assert "- candidate one" in out
    assert "- candidate two" in out
    assert "- candidate three" in out
    assert out.count("- candidate ") == 3
    assert "candidate four" not in out
    assert "candidate body-only" not in out
    assert "dataset=central" in out
    assert "trust=attested" in out
    assert "source=" not in out
    assert "https://docs.example/continuity/one" not in out
    assert "https://docs.example/continuity/three" not in out
    assert token not in out
    for internal_id in (
        "internal-hit-001",
        "internal-hit-002",
        "internal-result-001",
        "internal-source-001",
    ):
        assert internal_id not in out


def test_search_workspace_posts_bounded_query_and_filters_repo(monkeypatch) -> None:
    server_payload = {
        "results": [
            {"text": "matching direct", "repo": "cItAdEl"},
            {
                "text": "matching nested",
                "_citadel": {"provenance": {"repo": "org/Citadel"}},
            },
            {"text": "wrong repo prefix", "repo": "Citadel/Other"},
            {"text": "body-only Citadel mention"},
        ],
        "retrieval_receipt": {
            "candidate_page": {
                "limit": 5,
                "fetched": 4,
                "matched": 4,
                "returned": 4,
            },
            "absence": {"proven": False, "reason": "no matching workspace notes"},
        },
    }
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, size: int) -> bytes:
            captured["read_size"] = size
            return json.dumps(server_payload).encode("utf-8")

    def fake_urlopen(request: Any, timeout: int) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(sync_start.urllib.request, "urlopen", fake_urlopen)

    shaped = sync_start._search_workspace(
        "https://node.example",
        "ctdl_test_token",
        repo_name="Citadel",
        branch="feature/continuity",
    )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert captured["timeout"] == sync_start.HTTP_TIMEOUT_SECONDS
    assert captured["read_size"] == sync_start.MAX_RESPONSE_BYTES + 1
    assert request.full_url == "https://node.example/search"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer ctdl_test_token"
    assert body == {
        "query": "Repo: Citadel Branch: feature/continuity",
        "repo": "Citadel",
        "top_k": 5,
    }
    assert set(body) == {"query", "repo", "top_k"}
    assert shaped == {
        "results": [
            {"text": "matching direct", "repo": "cItAdEl"},
            {
                "text": "matching nested",
                "_citadel": {"provenance": {"repo": "org/Citadel"}},
            },
        ],
        "candidate_page": {
            "limit": 5,
            "fetched": 4,
            "matched": 4,
            "returned": 4,
        },
        "absence": {"proven": False, "reason": "no matching workspace notes"},
    }


def test_search_workspace_rejects_oversize_response(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class OversizedResponse:
        def __enter__(self) -> "OversizedResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, size: int) -> bytes:
            captured["read_size"] = size
            return b"x" * size

    def fake_urlopen(request: Any, timeout: int) -> OversizedResponse:
        return OversizedResponse()

    monkeypatch.setattr(sync_start.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ValueError, match="search response too large"):
        sync_start._search_workspace(
            "https://node.example",
            "ctdl_test_token",
            repo_name="Citadel",
            branch="main",
        )
    assert captured["read_size"] == sync_start.MAX_RESPONSE_BYTES + 1


def test_search_workspace_rejects_invalid_response(monkeypatch) -> None:
    invalid_payload = {
        "results": [],
        "retrieval_receipt": {
            "candidate_page": {},
            "absence": {"proven": True, "reason": "invalid proven state"},
        },
    }

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        return io.BytesIO(json.dumps(invalid_payload).encode("utf-8"))

    monkeypatch.setattr(sync_start.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ValueError, match="unexpected search response"):
        sync_start._search_workspace(
            "https://node.example",
            "ctdl_test_token",
            repo_name="Citadel",
            branch="main",
        )


@pytest.mark.parametrize("failure", ["search", "network", "schema"])
def test_run_swallows_search_network_and_schema_errors(
    monkeypatch,
    capsys,
    failure: str,
) -> None:
    monkeypatch.setenv(sync_start.TOKEN_ENV, "ctdl_test_token")
    monkeypatch.setattr(sync_start, "_workspace_identity", lambda cwd: ("Citadel", "main"))
    calls: list[tuple[Any, ...]] = []

    def failing_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        if failure == "schema":
            return {"results": []}
        if failure == "network":
            raise OSError("network down")
        raise RuntimeError("search failed")

    monkeypatch.setattr(sync_start, "_search_workspace", failing_search)

    assert sync_start.run(io.StringIO('{"cwd": "/workspace"}')) == 0
    assert len(calls) == 1
    _assert_policy_only(capsys)


def test_search_workspace_refuses_non_https() -> None:
    with pytest.raises(ValueError, match="non-HTTPS"):
        sync_start._search_workspace(
            "http://node.example",
            "ctdl_test_token",
            repo_name="Citadel",
            branch="main",
        )


def test_workspace_identity_returns_repo_basename_and_branch(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "Citadel"
    repo.mkdir()
    calls: list[tuple[str, tuple[str, ...]]] = []
    root_result = subprocess.CompletedProcess(
        ["git", "rev-parse"],
        0,
        stdout=f"{repo}\n",
        stderr="",
    )
    branch_result = subprocess.CompletedProcess(
        ["git", "branch"],
        0,
        stdout="feature/continuity\n",
        stderr="",
    )

    def fake_git_run(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
        calls.append((cwd, args))
        if args == ("rev-parse", "--show-toplevel"):
            return root_result
        if args == ("branch", "--show-current"):
            return branch_result
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(sync_start, "_git_run", fake_git_run)

    identity = sync_start._workspace_identity(str(repo))

    assert identity == ("Citadel", "feature/continuity")
    assert str(repo) not in str(identity)
    assert calls == [
        (str(repo), ("rev-parse", "--show-toplevel")),
        (str(repo), ("branch", "--show-current")),
    ]


@pytest.mark.parametrize("missing", ["root", "branch"])
def test_workspace_identity_requires_git_root_and_current_branch(
    monkeypatch,
    tmp_path,
    missing: str,
) -> None:
    repo = tmp_path / "Citadel"
    repo.mkdir()

    def fake_git_run(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
        if args == ("rev-parse", "--show-toplevel"):
            stdout = "" if missing == "root" else f"{repo}\n"
            return subprocess.CompletedProcess(["git"], 0, stdout=stdout, stderr="")
        stdout = "" if missing == "branch" else "main\n"
        return subprocess.CompletedProcess(["git"], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(sync_start, "_git_run", fake_git_run)

    assert sync_start._workspace_identity(str(repo)) is None


def test_policy_write_failure_is_fail_silent(monkeypatch) -> None:
    monkeypatch.delenv(sync_start.TOKEN_ENV, raising=False)

    class BrokenStdout:
        def write(self, text: str) -> int:
            raise OSError("stdout closed")

    monkeypatch.setattr(sync_start.sys, "stdout", BrokenStdout())

    assert sync_start.run(io.StringIO("{}")) == 0
