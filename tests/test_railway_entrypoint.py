from __future__ import annotations

from typing import Any

import tomllib

from scripts import run_railway


def test_railway_toml_uses_testable_dispatcher() -> None:
    with open("railway.toml", "rb") as file:
        config = tomllib.load(file)

    assert config["deploy"]["startCommand"] == "python -m scripts.run_railway"


def test_web_mode_execs_uvicorn(monkeypatch: Any) -> None:
    calls: list[tuple[str, list[str]]] = []

    def execvp(binary: str, args: list[str]) -> None:
        calls.append((binary, args))
        raise RuntimeError("stop")

    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setattr(run_railway.os, "execvp", execvp)

    try:
        run_railway.run("web")
    except RuntimeError as exc:
        assert str(exc) == "stop"

    assert calls == [
        (
            "python",
            [
                "python",
                "-m",
                "uvicorn",
                "kb.server:app",
                "--host",
                "0.0.0.0",
                "--port",
                "9000",
            ],
        )
    ]


def test_learning_agent_mode_runs_github_sync(monkeypatch: Any) -> None:
    from scripts import run_github_sync

    calls: list[str] = []

    def run_sync() -> int:
        calls.append("github-sync")
        return 0

    monkeypatch.setattr(run_github_sync, "run", run_sync)

    assert run_railway.run("learning-agent") == 0
    assert calls == ["github-sync"]


def test_backup_mirror_mode_runs_backup_job(monkeypatch: Any) -> None:
    from scripts import run_backup_mirror

    calls: list[str] = []

    def run_mirror() -> int:
        calls.append("backup-mirror")
        return 0

    monkeypatch.setattr(run_backup_mirror, "run", run_mirror)

    assert run_railway.run("backup-mirror") == 0
    assert calls == ["backup-mirror"]


def test_cognify_mode_runs_cognify_without_verify(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    class FakeCitadel:
        async def cognify_dataset(
            self, *, dataset: Any, verify: bool, force: bool
        ) -> dict[str, Any]:
            calls.append({"dataset": dataset, "verify": verify, "force": force})
            return {
                "ok": True,
                "dataset": dataset,
                "graph_grew": True,
                "verify": verify,
            }

    import kb.service as service

    monkeypatch.setattr(service.Citadel, "from_env", classmethod(lambda cls: FakeCitadel()))
    monkeypatch.delenv("CITADEL_COGNIFY_DATASET", raising=False)

    assert run_railway.run("cognify") == 0
    assert calls == [{"dataset": None, "verify": False, "force": False}]


def test_cognify_mode_passes_force_and_fails_on_top_level_false(monkeypatch: Any) -> None:
    calls: list[bool] = []

    class FakeCitadel:
        async def cognify_dataset(
            self, *, dataset: Any, verify: bool, force: bool
        ) -> dict[str, Any]:
            calls.append(force)
            return {"ok": False, "dataset": dataset, "verify": verify, "force": force}

    import kb.service as service

    monkeypatch.setattr(service.Citadel, "from_env", classmethod(lambda cls: FakeCitadel()))
    monkeypatch.setenv("CITADEL_COGNIFY_FORCE", "true")

    assert run_railway.run("cognify") == 1
    assert calls == [True]


def test_cognify_stage_refuses_in_process_when_scheduler_enabled(monkeypatch: Any) -> None:
    # #47: a second OS process must not open Kuzu while the web's in-process evolve
    # scheduler owns it — refuse and require routing through the API.
    monkeypatch.delenv("CITADEL_COGNIFY_TARGET_URL", raising=False)
    monkeypatch.setenv("CITADEL_EVOLVE_SCHEDULER_ENABLED", "true")

    called: list[bool] = []
    monkeypatch.setattr(
        run_railway, "_cognify_mode", lambda *, verify: (called.append(verify), 0)[1]
    )

    assert run_railway._cognify_stage() == 1
    assert called == []  # never opened an in-process Kuzu writer


def test_cognify_verify_mode_fails_when_verification_fails(monkeypatch: Any) -> None:
    class FakeCitadel:
        async def cognify_dataset(
            self, *, dataset: Any, verify: bool, force: bool
        ) -> dict[str, Any]:
            return {
                "ok": True,
                "dataset": dataset,
                "graph_grew": False,
                "verify": verify,
                "force": force,
                "verification": {"ok": False, "search_hit": False, "graph_grew": False},
            }

    import kb.service as service

    monkeypatch.setattr(service.Citadel, "from_env", classmethod(lambda cls: FakeCitadel()))
    monkeypatch.delenv("CITADEL_COGNIFY_DATASET", raising=False)

    assert run_railway.run("cognify-verify") == 1


def test_unknown_mode_fails() -> None:
    assert run_railway.run("not-real") == 1


def _patch_stages(
    monkeypatch: Any,
    calls: list[str],
    *,
    github_code: int = 0,
    backup_code: int = 0,
    self_improve_code: int = 0,
    github_raises: bool = False,
    skills_raises: bool = False,
) -> None:
    from scripts import run_backup_mirror, run_github_sync, run_self_improve

    import kb.skills as skills

    def fake_github() -> int:
        calls.append("github_sync")
        if github_raises:
            raise RuntimeError("github exploded")
        return github_code

    def fake_skills_refresh(state_path: Any = None) -> dict[str, Any]:
        calls.append("skills_refresh")
        if skills_raises:
            raise RuntimeError("skills exploded")
        return {"ok": True, "skills": 3, "changed": [], "added": [], "removed": []}

    def fake_self_improve() -> int:
        calls.append("self_improve")
        return self_improve_code

    def fake_backup() -> int:
        calls.append("backup_mirror")
        return backup_code

    def fake_repo_content() -> int:
        calls.append("repo_content_sync")
        return 0

    monkeypatch.setattr(run_github_sync, "run", fake_github)
    monkeypatch.setattr(run_railway, "_repo_content_sync_stage", fake_repo_content)
    monkeypatch.setattr(skills, "refresh_skill_catalog", fake_skills_refresh)
    monkeypatch.setattr(run_self_improve, "run", fake_self_improve)
    monkeypatch.setattr(run_backup_mirror, "run", fake_backup)


def _clear_pipeline_env(monkeypatch: Any) -> None:
    for name in (
        "CITADEL_PIPELINE_GITHUB_SYNC_ENABLED",
        "CITADEL_PIPELINE_REPO_CONTENT_SYNC_ENABLED",
        "CITADEL_PIPELINE_SKILLS_REFRESH_ENABLED",
        "CITADEL_PIPELINE_BACKUP_MIRROR_ENABLED",
        "CITADEL_SELF_IMPROVE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_pipeline_runs_all_enabled_stages_in_order(monkeypatch: Any) -> None:
    _clear_pipeline_env(monkeypatch)
    monkeypatch.setenv("CITADEL_SELF_IMPROVE_ENABLED", "true")
    calls: list[str] = []
    _patch_stages(monkeypatch, calls)

    assert run_railway.run("pipeline") == 0
    assert calls == ["github_sync", "repo_content_sync", "skills_refresh", "self_improve", "backup_mirror"]


def test_pipeline_self_improve_stage_is_off_by_default(monkeypatch: Any) -> None:
    _clear_pipeline_env(monkeypatch)
    calls: list[str] = []
    _patch_stages(monkeypatch, calls)

    assert run_railway.run("pipeline") == 0
    assert calls == ["github_sync", "repo_content_sync", "skills_refresh", "backup_mirror"]


def test_pipeline_stage_toggles_disable_individual_stages(monkeypatch: Any) -> None:
    _clear_pipeline_env(monkeypatch)
    monkeypatch.setenv("CITADEL_PIPELINE_SKILLS_REFRESH_ENABLED", "false")
    monkeypatch.setenv("CITADEL_PIPELINE_BACKUP_MIRROR_ENABLED", "false")
    calls: list[str] = []
    _patch_stages(monkeypatch, calls)

    assert run_railway.run("pipeline") == 0
    assert calls == ["github_sync", "repo_content_sync"]


def test_pipeline_continues_past_a_failed_stage(monkeypatch: Any) -> None:
    _clear_pipeline_env(monkeypatch)
    calls: list[str] = []
    _patch_stages(monkeypatch, calls, github_raises=True)

    # Every later stage still runs (that is the "continues" part), and the pass
    # reports itself as failed rather than clean (#89).
    assert run_railway.run("pipeline") == 1
    assert calls == ["github_sync", "repo_content_sync", "skills_refresh", "backup_mirror"]


def test_pipeline_exits_zero_only_when_every_enabled_stage_succeeds(monkeypatch: Any) -> None:
    _clear_pipeline_env(monkeypatch)
    monkeypatch.setenv("CITADEL_PIPELINE_SKILLS_REFRESH_ENABLED", "false")
    monkeypatch.setenv("CITADEL_PIPELINE_REPO_CONTENT_SYNC_ENABLED", "false")
    calls: list[str] = []
    _patch_stages(monkeypatch, calls, github_raises=True, backup_code=1)

    assert run_railway.run("pipeline") == 1
    assert calls == ["github_sync", "backup_mirror"]


def test_pipeline_mode_aliases_with_everything_disabled(monkeypatch: Any) -> None:
    _clear_pipeline_env(monkeypatch)
    monkeypatch.setenv("CITADEL_PIPELINE_GITHUB_SYNC_ENABLED", "false")
    monkeypatch.setenv("CITADEL_PIPELINE_REPO_CONTENT_SYNC_ENABLED", "false")
    monkeypatch.setenv("CITADEL_PIPELINE_SKILLS_REFRESH_ENABLED", "false")
    monkeypatch.setenv("CITADEL_PIPELINE_BACKUP_MIRROR_ENABLED", "false")
    calls: list[str] = []
    _patch_stages(monkeypatch, calls)

    assert run_railway.run("all") == 0
    assert run_railway.run("cron") == 0
    assert calls == []


# --- Evolve run-mode (ADR-0005 step 3) -------------------------------------

_EVOLVE_STAGE_ATTRS = [
    ("github_sync", "_github_sync_stage"),
    ("repo_content_sync", "_repo_content_sync_stage"),
    ("self_improve", "_self_improve_stage"),
    ("promotion", "_promotion_stage"),
    ("linear_sync", "_linear_sync_stage"),
    ("cognify", "_cognify_stage"),
]


def _patch_evolve_stages(
    monkeypatch: Any,
    calls: list[str],
    *,
    fail_codes: dict[str, int] | None = None,
    raise_stage: str | None = None,
) -> None:
    fail_codes = fail_codes or {}

    def make_stage(stage_name: str) -> Any:
        def stage() -> int:
            calls.append(stage_name)
            if stage_name == raise_stage:
                raise RuntimeError(f"{stage_name} exploded")
            return fail_codes.get(stage_name, 0)

        return stage

    for stage_name, attr in _EVOLVE_STAGE_ATTRS:
        monkeypatch.setattr(run_railway, attr, make_stage(stage_name))


def _clear_evolve_env(monkeypatch: Any) -> None:
    for name in (
        "CITADEL_EVOLVE_GITHUB_SYNC_ENABLED",
        "CITADEL_EVOLVE_REPO_CONTENT_SYNC_ENABLED",
        "CITADEL_EVOLVE_SELF_IMPROVE_ENABLED",
        "CITADEL_EVOLVE_PROMOTION_ENABLED",
        "CITADEL_EVOLVE_LINEAR_SYNC_ENABLED",
        "CITADEL_EVOLVE_COGNIFY_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_evolve_runs_all_stages_in_chain_order(monkeypatch: Any) -> None:
    _clear_evolve_env(monkeypatch)
    calls: list[str] = []
    _patch_evolve_stages(monkeypatch, calls)

    assert run_railway.run("evolve") == 0
    assert calls == [
        "github_sync",
        "repo_content_sync",
        "self_improve",
        "promotion",
        "linear_sync",
        "cognify",
    ]


def test_evolve_stage_toggles_disable_individual_stages(monkeypatch: Any) -> None:
    _clear_evolve_env(monkeypatch)
    monkeypatch.setenv("CITADEL_EVOLVE_SELF_IMPROVE_ENABLED", "false")
    monkeypatch.setenv("CITADEL_EVOLVE_PROMOTION_ENABLED", "false")
    monkeypatch.setenv("CITADEL_EVOLVE_LINEAR_SYNC_ENABLED", "false")
    calls: list[str] = []
    _patch_evolve_stages(monkeypatch, calls)

    assert run_railway.run("evolve") == 0
    assert calls == ["github_sync", "repo_content_sync", "cognify"]


def test_evolve_continues_past_a_failed_stage(monkeypatch: Any) -> None:
    _clear_evolve_env(monkeypatch)
    calls: list[str] = []
    _patch_evolve_stages(monkeypatch, calls, raise_stage="github_sync")

    # This is the production shape of #88/#46: github_sync dies on the Kuzu
    # lock, the rest of the cycle proceeds, and the pass must not claim success.
    assert run_railway.run("evolve") == 1
    assert calls == [
        "github_sync",
        "repo_content_sync",
        "self_improve",
        "promotion",
        "linear_sync",
        "cognify",
    ]


def test_evolve_exits_nonzero_when_all_enabled_stages_fail(monkeypatch: Any) -> None:
    _clear_evolve_env(monkeypatch)
    for name in (
        "CITADEL_EVOLVE_REPO_CONTENT_SYNC_ENABLED",
        "CITADEL_EVOLVE_SELF_IMPROVE_ENABLED",
        "CITADEL_EVOLVE_PROMOTION_ENABLED",
        "CITADEL_EVOLVE_LINEAR_SYNC_ENABLED",
        "CITADEL_EVOLVE_COGNIFY_ENABLED",
    ):
        monkeypatch.setenv(name, "false")
    calls: list[str] = []
    _patch_evolve_stages(monkeypatch, calls, raise_stage="github_sync")

    assert run_railway.run("evolve") == 1
    assert calls == ["github_sync"]


# --- Evolve cognify stage routing (web-API vs in-process) ------------------


def test_cognify_stage_routes_to_api_when_target_url_set(monkeypatch: Any) -> None:
    monkeypatch.setenv("CITADEL_COGNIFY_TARGET_URL", "http://localhost:8080")
    monkeypatch.delenv("CITADEL_EVOLVE_COGNIFY_FORCE", raising=False)

    api_calls: list[tuple[str, bool]] = []

    def fake_api(url: str, *, force: bool) -> int:
        api_calls.append((url, force))
        return 0

    def fail_mode(*, verify: bool, force: bool) -> int:
        raise AssertionError("cognify must go through the API, not asyncio.run")

    monkeypatch.setattr(run_railway, "_cognify_via_api", fake_api)
    monkeypatch.setattr(run_railway, "_cognify_mode", fail_mode)

    assert run_railway._cognify_stage() == 0
    assert api_calls == [("http://localhost:8080", False)]


def test_cognify_stage_runs_in_process_without_target_url(monkeypatch: Any) -> None:
    monkeypatch.delenv("CITADEL_COGNIFY_TARGET_URL", raising=False)

    mode_calls: list[bool] = []

    def fake_mode(*, verify: bool, force: bool) -> int:
        mode_calls.append(verify)
        return 0

    monkeypatch.setattr(run_railway, "_cognify_mode", fake_mode)

    assert run_railway._cognify_stage() == 0
    assert mode_calls == [False]


def test_cognify_via_api_posts_to_endpoint(monkeypatch: Any) -> None:
    import json
    import urllib.request

    captured: dict[str, Any] = {}

    class _FakeResp:
        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"graph_before": 1, "graph_after": 9, "graph_grew": true}'

    def fake_urlopen(request: Any, timeout: Any = None) -> "_FakeResp":
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["auth"] = request.headers.get("Authorization")
        return _FakeResp()

    monkeypatch.setenv("CITADEL_ADMIN_KEY", "test-admin-key")
    monkeypatch.delenv("CITADEL_COGNIFY_DATASET", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert run_railway._cognify_via_api("http://localhost:8080", force=True) == 0
    assert captured["url"] == "http://localhost:8080/api/cognify/run"
    assert json.loads(captured["data"]) == {"force": True, "verify": False}
    assert captured["auth"] == "Bearer test-admin-key"


def test_cognify_via_api_fails_without_admin_key(monkeypatch: Any) -> None:
    monkeypatch.delenv("CITADEL_ADMIN_KEY", raising=False)
    assert run_railway._cognify_via_api("http://localhost:8080", force=False) == 1


def test_cognify_via_api_fails_when_response_reports_failure(monkeypatch: Any) -> None:
    import urllib.request

    class _FakeResp:
        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"ok": false, "error": "cognify failed"}'

    monkeypatch.setenv("CITADEL_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: _FakeResp())

    assert run_railway._cognify_via_api("http://localhost:8080", force=False) == 1


# --- Evolve single-loop restructure (#69) ----------------------------------


def test_stage_loop_shares_one_event_loop() -> None:
    # #69: cognee binds its async engine to the first loop, so every cognee-touching
    # stage body must run on ONE shared loop. run_async provides it; outside a shared
    # loop it falls back to asyncio.run (a fresh loop per call, the old behaviour).
    import asyncio

    from scripts.stage_loop import run_async, stage_loop

    async def running_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    with stage_loop():
        first = run_async(running_loop())
        second = run_async(running_loop())
    assert first is second  # both stage bodies ran on the SAME loop object

    # With no shared loop active, run_async still works (asyncio.run fallback).
    assert run_async(running_loop()) is not None


def test_evolve_runs_cognee_stage_bodies_on_one_loop(monkeypatch: Any) -> None:
    # #69 (integration): the whole evolve chain shares one loop, so two stage bodies
    # dispatched through run_async observe the SAME running loop — the fix that stops
    # "got Future attached to a different loop" on every cognee stage after the first.
    import asyncio

    _clear_evolve_env(monkeypatch)
    seen: list[int] = []

    async def _record() -> int:
        seen.append(id(asyncio.get_running_loop()))
        return 0

    monkeypatch.setattr(run_railway, "_github_sync_stage", lambda: 0)
    monkeypatch.setattr(run_railway, "_repo_content_sync_stage", lambda: run_railway.run_async(_record()))
    monkeypatch.setattr(run_railway, "_self_improve_stage", lambda: 0)
    monkeypatch.setattr(run_railway, "_promotion_stage", lambda: 0)
    monkeypatch.setattr(run_railway, "_linear_sync_stage", lambda: run_railway.run_async(_record()))
    monkeypatch.setattr(run_railway, "_cognify_stage", lambda: 0)

    assert run_railway.run("evolve") == 0
    assert len(seen) == 2
    assert seen[0] == seen[1]  # repo_content_sync and linear_sync shared one loop


def test_evolve_stage_lists_stay_in_sync() -> None:
    """The sync and in-loop evolve stage lists must not drift (#88).

    evolve_stages() drives the CLI/cron entrypoint and evolve_stages_async()
    drives the web scheduler. They are separate literals, so this pins that they
    name the same stages, in the same order, with the same enabled flags.
    cognify is the one deliberate exception: the scheduler runs it itself as
    Phase 2, which the old subprocess expressed via an env toggle.
    """
    sync_stages = [(name, enabled) for name, enabled, _ in run_railway.evolve_stages()]
    async_stages = [
        (name, enabled) for name, enabled, _ in run_railway.evolve_stages_async()
    ]

    assert [name for name, _ in sync_stages if name != "cognify"] == [
        name for name, _ in async_stages
    ]
    assert "cognify" in dict(sync_stages), "the sync list still owns the cognify stage"
    for name, enabled in async_stages:
        assert dict(sync_stages)[name] == enabled, f"{name} toggle drifted"


def test_evolve_async_stages_are_all_coroutine_functions() -> None:
    """A sync callable here would silently never run inside the web loop."""
    import inspect

    for name, _, runner in run_railway.evolve_stages_async():
        assert inspect.iscoroutinefunction(runner), f"{name} is not awaitable"


def test_cognify_via_api_converts_read_timeout_to_clean_failure(monkeypatch: Any) -> None:
    # #116: a read-phase timeout is a bare TimeoutError, not a URLError, and
    # would otherwise escape this helper's HTTPError/URLError-only handling
    # as a raw traceback (the #39 bug, unbackported to this copy). The
    # urlopen import is function-local, so patch the stdlib symbol itself.
    import urllib.request

    def boom(request: Any, timeout: int) -> None:
        raise TimeoutError("the read operation timed out")

    monkeypatch.setenv("CITADEL_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(urllib.request, "urlopen", boom)

    assert run_railway._cognify_via_api("http://localhost:8080", force=False) == 1
