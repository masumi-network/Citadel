"""Headless / agent-facing CLI surface — every teammate command emits clean,
parseable JSON to stdout under `--json`, never prompts, and sets exit codes."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

import kb.cli
from kb.cli import build_parser


def _run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(args.handler(args))


def test_setup_json_emits_pure_config(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "cap.json"
    rc = _run(
        [
            "setup", "--non-interactive", "--json",
            "--node-url", "https://node.example",
            "--root", f"{tmp_path}=org-work",
            "--config", str(cfg),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)  # pure JSON, no "Saved …" prose
    assert out["node_url"] == "https://node.example"
    assert out["roots"][0]["tags"] == ["org-work"]


def test_bare_citadel_shows_home_screen(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CITADEL_ADMIN_KEY", raising=False)
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CITADEL_WRITER_KEYS", raising=False)
    monkeypatch.setattr(sys, "argv", ["citadel"])
    with pytest.raises(SystemExit) as exc:
        kb.cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # Strip ANSI so colored Pixel Bastion / labels still match.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    assert "the organization vault" in plain         # tagline beside the mark
    assert "██" in plain                              # Pixel Bastion mark
    assert "CITADEL" in plain                         # wordmark label beside mark
    assert "____" not in plain                        # no figlet hero on home
    assert "onboard" in plain and "status" in plain     # curated command menu
    assert "Get started" in plain                     # grouped menu


def test_unknown_command_suggests_closest(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["stauts"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unknown command" in err
    assert "citadel status" in err  # fuzzy suggestion


def test_unknown_command_no_match_is_clean(capsys) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["zzzzz"])
    err = capsys.readouterr().err
    assert "unknown command" in err
    assert "see all commands" in err


def test_bad_flag_choice_not_labeled_unknown_subcommand(capsys) -> None:
    # A bad value to a --flag with choices= must fall through to argparse,
    # NOT be relabeled as an unknown subcommand.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["feedback", "qa1", "--score", "5"])
    err = capsys.readouterr().err
    assert "unknown subcommand" not in err
    assert "--score" in err


def test_bench_defaults_to_run_and_preserves_arguments() -> None:
    args = build_parser().parse_args(["bench", "run", "--repeats", "3"])
    assert args.command == "bench"
    assert args.bench_args == ["run", "--repeats", "3"]


def test_bench_delegates_to_packaged_retrieval_eval(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        calls.append(argv)
        return 7

    monkeypatch.setattr("kb.retrieval_eval.main", fake_main)
    assert _run(["bench", "lint", "--questions", "questions.json"]) == 7
    assert calls == [["lint", "--questions", "questions.json"]]


def test_bench_help_reaches_nested_parser(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["citadel", "bench", "--help"])
    with pytest.raises(SystemExit) as exc:
        kb.cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "{run,lint,ci,compare,enforce,report}" in out


def test_cognify_force_is_scheduled_only(monkeypatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    class FakeCitadel:
        async def cognify_dataset(self, **kwargs: object) -> dict[str, bool]:
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(
        "kb.service.Citadel.from_env",
        classmethod(lambda cls: FakeCitadel()),
    )

    assert _run(["cognify", "--dataset", "masumi-network", "--force"]) == 2
    assert calls == []
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["reason"] == "llm_scheduled_only"


def test_reindex_apply_is_scheduled_only(monkeypatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    class FakeCitadel:
        async def reconcile_corpus(self, **kwargs: object) -> dict[str, bool]:
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(
        "kb.service.Citadel.from_env",
        classmethod(lambda cls: FakeCitadel()),
    )

    assert _run(["reindex", "--dataset", "notes", "--apply", "--force"]) == 2
    assert calls == []
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["reason"] == "llm_scheduled_only"


def test_reindex_force_requires_apply(capsys) -> None:
    assert _run(["reindex", "--force"]) == 1
    assert "--force requires --apply" in capsys.readouterr().err


def test_reindex_recover_is_scheduled_only(monkeypatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    class FakeCitadel:
        async def reconcile_corpus(self, **kwargs: object) -> dict[str, bool]:
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(
        "kb.service.Citadel.from_env",
        classmethod(lambda cls: FakeCitadel()),
    )

    assert _run(["reindex", "--apply", "--force", "--recover"]) == 2
    assert calls == []
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["reason"] == "llm_scheduled_only"


def test_reindex_recover_requires_apply(capsys) -> None:
    assert _run(["reindex", "--recover"]) == 1
    assert "--recover requires --apply" in capsys.readouterr().err


def test_reindex_oversized_apply_is_scheduled_only(monkeypatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    class FakeCitadel:
        async def reconcile_oversized_chunks(self, **kwargs: object) -> dict[str, bool]:
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(
        "kb.service.Citadel.from_env",
        classmethod(lambda cls: FakeCitadel()),
    )

    assert _run(["reindex", "--oversized", "--apply", "--force"]) == 2
    assert calls == []
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["reason"] == "llm_scheduled_only"


def test_setup_json_never_prompts_even_on_tty(tmp_path: Path, monkeypatch, capsys) -> None:
    # --json implies non-interactive: must not call input() even with a TTY.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("prompted under --json"))
    cfg = tmp_path / "cap.json"
    rc = _run(["setup", "--json", "--config", str(cfg)])
    assert rc == 0
    json.loads(capsys.readouterr().out)  # valid JSON, no wizard prose


def test_capture_json_dry_run_is_clean(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "cap.json"
    (tmp_path / "README.md").write_text("a summary line\n")
    _run(["setup", "--non-interactive", "--json", "--root", f"{tmp_path}=personal", "--config", str(cfg)])
    capsys.readouterr()

    rc = _run(["capture", "--dry-run", "--json", "--config", str(cfg)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    assert out[0]["tags"] == ["personal", "capture"]


def test_capture_json_real_post_shape(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = tmp_path / "cap.json"
    _run(["setup", "--non-interactive", "--json", "--node-url", "https://node.example",
          "--root", f"{tmp_path}=personal", "--config", str(cfg)])
    capsys.readouterr()
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_headless_token")
    # The real server always states its decision; a body without accepted: true
    # now reads as unconfirmed, so the "real POST shape" fixture must carry it.
    monkeypatch.setattr("kb.cli.post_capture", lambda *a, **k: {"accepted": True, "status": "ok"})

    rc = _run(["capture", "--json", "--config", str(cfg)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["results"][0]["ok"] is True


def test_onboard_json_no_prompts(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = tmp_path
    (repo / ".git" / "hooks").mkdir(parents=True)
    monkeypatch.setenv("CITADEL_CAPTURE_CONFIG_PATH", str(tmp_path / "cap.json"))
    # Token from env (not argv) — the secure headless path; never echoed.
    monkeypatch.setenv("CITADEL_MCP_ACCESS_TOKEN", "ctdl_headless_abcdef1234")

    rc = _run(
        [
            "onboard", "--non-interactive", "--json",
            "--repo", str(repo),
            "--shell-rc", str(tmp_path / ".zshrc"),
            "--no-capture",
            "--no-tools",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    names = {s["name"] for s in out["steps"]}
    assert "git pre-push hook" in names and "SessionEnd hook" in names
    assert "…" in out["token_masked"]  # masked, never the raw token
    assert "ctdl_headless_abcdef1234" not in json.dumps(out)


def test_default_status_json_is_redacted_and_allowlisted(monkeypatch, capsys) -> None:
    # Default `citadel status --json` must never smoke /search, never pull the
    # full mesh graph, and must carry no graph nodes/edges/events, query labels,
    # or token-derived values.
    from kb.status import Check, StatusReport

    seen: list[tuple[str, object]] = []

    def fake_gather(node_url, token, **kw):
        seen.append(("with_search", kw.get("with_search")))
        return StatusReport(
            node_url=node_url,
            healthy=True,
            identity={"seat_slug": "alice", "role": "writer", "scopes": ["kb:search"]},
            checks=[
                Check("node", True, "healthy"),
                Check("auth", True, "valid"),
                Check("token", True, "…6789"),
            ],
            recent=[
                {
                    "title": "feat: safe",
                    "created_at": "2026-06-27T10:00:00",
                    "actor_id": "principal_secret",
                    "token": "ctdl_hostiletoken",
                    "dataset": "seat:alice",
                    "detail": {"query": "confidential text"},
                }
            ],
            repo=".",
        )

    def fake_summary(node_url, token, **kw):
        return {"detail": "summary", "tracked_sources": 3}

    def forbidden_full_mesh(*_a, **_k):
        raise AssertionError("default --json must not fetch the full mesh graph")

    monkeypatch.setattr("kb.cli.gather_status", fake_gather)
    monkeypatch.setattr("kb.cli.fetch_mesh_summary", fake_summary)
    monkeypatch.setattr("kb.cli.fetch_mesh", forbidden_full_mesh)
    rc = _run(["status", "--json", "--node-url", "https://node.example"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    blob = json.dumps(out)
    assert "6789" not in blob
    token_check = next(item for item in out["checks"] if item["name"] == "token")
    assert token_check["detail"] == "configured"
    assert ("with_search", False) in seen  # never the /search smoke by default
    assert "nodes" not in blob and "edges" not in blob and "events" not in blob
    assert "query" not in blob
    assert "…" not in blob  # no masked token suffix
    # Hostile recent-row fields are gone; only display scalars survive.
    for leaked in ("principal_secret", "ctdl_hostiletoken", "seat:alice", "confidential text"):
        assert leaked not in blob
    assert out["recent"] == [{"title": "feat: safe", "created_at": "2026-06-27T10:00:00"}]
