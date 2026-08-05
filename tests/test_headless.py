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
    assert "{run,lint,ci,compare,report}" in out


def test_cognify_force_reaches_service(monkeypatch, capsys) -> None:
    calls: list[dict[str, object]] = []

    class FakeCitadel:
        async def cognify_dataset(self, **kwargs: object) -> dict[str, bool]:
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(
        "kb.service.Citadel.from_env",
        classmethod(lambda cls: FakeCitadel()),
    )

    assert _run(["cognify", "--dataset", "masumi-network", "--force"]) == 0
    assert calls == [{"dataset": "masumi-network", "verify": False, "force": True}]
    assert '"ok": true' in capsys.readouterr().out


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
    monkeypatch.setattr("kb.cli.post_capture", lambda *a, **k: {"status": "ok"})

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
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    names = {s["name"] for s in out["steps"]}
    assert "git pre-push hook" in names and "SessionEnd hook" in names
    assert "…" in out["token_masked"]  # masked, never the raw token
    assert "ctdl_headless_abcdef1234" not in json.dumps(out)
