from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from kb.config import CitadelConfig


# --- config parsing --------------------------------------------------------


def test_evolve_scheduler_config_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("CITADEL_EVOLVE_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("CITADEL_EVOLVE_INTERVAL_SECONDS", "3600")
    config = CitadelConfig.from_env(env_file=None)
    assert config.evolve_scheduler_enabled is True
    assert config.evolve_interval_seconds == 3600


def test_evolve_scheduler_config_defaults(monkeypatch: Any) -> None:
    monkeypatch.delenv("CITADEL_EVOLVE_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("CITADEL_EVOLVE_INTERVAL_SECONDS", raising=False)
    config = CitadelConfig.from_env(env_file=None)
    assert config.evolve_scheduler_enabled is False
    assert config.evolve_interval_seconds == 21600


# --- scheduler wiring ------------------------------------------------------


def _fake_citadel(
    *, enabled: bool, interval: int = 21600, state_path: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            evolve_scheduler_enabled=enabled,
            evolve_interval_seconds=interval,
            # #153: the scheduler resumes its interval from this file, so the
            # fake config has to carry it. Empty means "nowhere to persist",
            # which read/record both tolerate.
            evolve_state_path=state_path,
        )
    )


def test_start_evolve_scheduler_disabled_returns_none(monkeypatch: Any) -> None:
    import kb.server as server

    monkeypatch.setattr(server, "get_citadel", lambda: _fake_citadel(enabled=False))
    assert server._start_evolve_scheduler() is None


async def test_start_and_stop_evolve_scheduler_enabled(monkeypatch: Any) -> None:
    import kb.server as server

    # Huge interval: the loop sleeps before its first pass, so run_evolve never
    # fires here — we only assert the task starts and then cancels cleanly.
    monkeypatch.setattr(
        server, "get_citadel", lambda: _fake_citadel(enabled=True, interval=999_999)
    )
    task = server._start_evolve_scheduler()
    assert task is not None
    assert not task.done()
    await server._stop_evolve_scheduler(task)
    assert task.done()


async def test_stop_evolve_scheduler_handles_none() -> None:
    import kb.server as server

    # No-op when the scheduler was never started (disabled path).
    await server._stop_evolve_scheduler(None)


class _FakeCitadel:
    def __init__(self, cognify_calls: list[bool]) -> None:
        self._cognify_calls = cognify_calls

    async def cognify_dataset(self, *, force: bool = False, verify: bool = False) -> dict[str, Any]:
        self._cognify_calls.append(force)
        # The scheduler runs the verify canary (#27) and records its verdict.
        assert verify is True
        return {
            "ok": True,
            "graph_after": {"nodes": 7, "edges": 4},
            "graph_grew": True,
            "verification": {"marker": "COGNIFY_TEST_MARKER_x", "search_hit": True, "ok": True},
        }


def _patch_phase1(monkeypatch: Any, *, code: int = 0, raises: bool = False) -> list[bool]:
    """Replace Phase 1 with a stub and report whether it saw add-only mode.

    Patches scripts.run_railway.run_evolve_in_loop, which kb.server imports by
    name when the scheduler coroutine starts, so this must be set before the
    task is created.
    """
    from kb.cognee_client import _suppress_inline_cognify
    import scripts.run_railway as run_railway

    suppressed: list[bool] = []

    async def fake_phase1() -> int:
        suppressed.append(_suppress_inline_cognify())
        if raises:
            raise RuntimeError("phase 1 exploded")
        return code

    monkeypatch.setattr(run_railway, "run_evolve_in_loop", fake_phase1)
    return suppressed


async def test_evolve_scheduler_loop_runs_stages_in_loop_then_cognifies(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Phase 1 runs in THIS process now, not a subprocess (#88).

    A second process can never open the Kuzu graph: cognee holds an exclusive
    file lock for the lifetime of whichever process opens it, and that is always
    the web. github_sync and linear_sync failed on that lock every hour.
    """
    import kb.server as server

    cognify_calls: list[bool] = []
    suppressed = _patch_phase1(monkeypatch)

    spawned: list[Any] = []

    async def forbidden_exec(*args: Any, **kwargs: Any) -> Any:
        spawned.append(args)
        raise AssertionError("the evolve scheduler must not spawn a subprocess")

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", forbidden_exec)
    monkeypatch.setattr(server, "get_citadel", lambda: _FakeCitadel(cognify_calls))

    task = asyncio.create_task(server._evolve_scheduler_loop(0.001, str(tmp_path / "evolve_state.json")))
    try:
        for _ in range(300):
            if len(cognify_calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert spawned == [], "no subprocess may be spawned"
    # Phase 1 ran, and saw add-only mode so its per-ingest background cognify
    # cannot storm the writer lock — what the subprocess env used to express.
    assert suppressed and all(suppressed)
    # Phase 2: cognify ran in-loop afterwards.
    assert len(cognify_calls) >= 2
    # The verify canary verdict is recorded for /readyz (#27).
    assert server._LAST_CANARY is not None and server._LAST_CANARY["ok"] is True


class _RaisingCitadel:
    """cognify_dataset raises, i.e. the #27 failure the canary exists to catch."""

    def __init__(self, cognify_calls: list[bool]) -> None:
        self._cognify_calls = cognify_calls

    async def cognify_dataset(self, *, force: bool = False, verify: bool = False) -> dict[str, Any]:
        self._cognify_calls.append(force)
        raise RuntimeError("cognify exploded mid-pass")


async def test_crashed_cognify_records_the_canary_unhealthy(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A raising cognify pass must stamp the canary ok=False, not leave a stale
    verdict standing, or /readyz reads GREEN through the exact break it gates on
    (BLK-2026-08-10-14 / #27).
    """
    import kb.server as server

    # Seed a prior healthy verdict, which is what makes the bug observable: the
    # crash must overwrite it, not be swallowed and leave it True.
    server._LAST_CANARY = {"ok": True, "search_hit": True, "graph_grew": True, "marker": "old"}

    cognify_calls: list[bool] = []
    _patch_phase1(monkeypatch)
    monkeypatch.setattr(server, "get_citadel", lambda: _RaisingCitadel(cognify_calls))

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(tmp_path / "evolve_state.json"))
    )
    try:
        for _ in range(300):
            if cognify_calls:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert cognify_calls, "cognify was never attempted"
    assert server._LAST_CANARY is not None
    assert server._LAST_CANARY["ok"] is False, "a crashed cognify must flip the canary unhealthy"
    assert server._LAST_CANARY.get("error") == "RuntimeError"


async def test_add_only_mode_does_not_leak_outside_the_pass(tmp_path: Path) -> None:
    """The suppression is scoped to the task tree, never process-wide (#88).

    It used to be an env var on a subprocess. In the web process an env var
    would make a teammate's ingest arriving mid-pass go add-only too.
    """
    from kb.cognee_client import _suppress_inline_cognify, suppress_inline_cognify

    assert _suppress_inline_cognify() is False

    async def observer() -> bool:
        return _suppress_inline_cognify()

    with suppress_inline_cognify():
        assert _suppress_inline_cognify() is True
        # Child tasks created inside the context inherit it...
        assert await asyncio.create_task(observer()) is True

    assert _suppress_inline_cognify() is False
    # ...and a task created outside it never sees it.
    assert await asyncio.create_task(observer()) is False


async def test_inline_projection_suppression_honours_the_context_variable() -> None:
    """The service-side checker must read the same flag Phase 1 actually sets.

    Phase 1 moved into the web process (#88) and marks itself with the
    _SUPPRESS_INLINE_COGNIFY context variable, deliberately NOT the environment
    variable, which is process-wide. `Citadel._inline_projection_suppressed`
    read only os.getenv, so it answered False for the whole of Phase 1 and let
    accept_source start the projection drain while Phase 1 held the graph
    writer lock. On 2026-08-13 that parked a job on writer_lock.acquire() for
    66 minutes while the lease heartbeat renewed it, so nothing reclaimed it.
    """
    from kb.cognee_client import suppress_inline_cognify
    from kb.service import Citadel

    assert Citadel._inline_projection_suppressed() is False
    with suppress_inline_cognify():
        assert Citadel._inline_projection_suppressed() is True
    assert Citadel._inline_projection_suppressed() is False


async def test_evolve_scheduler_loop_cognifies_even_if_phase1_raises(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import kb.server as server

    cognify_calls: list[bool] = []
    _patch_phase1(monkeypatch, raises=True)
    monkeypatch.setattr(server, "get_citadel", lambda: _FakeCitadel(cognify_calls))

    task = asyncio.create_task(server._evolve_scheduler_loop(0.001, str(tmp_path / "evolve_state.json")))
    try:
        for _ in range(300):
            if len(cognify_calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # A raising Phase 1 (caught) must not skip the in-loop cognify.
    assert len(cognify_calls) >= 2


async def test_evolve_scheduler_logs_error_when_stages_exit_nonzero(
    monkeypatch: Any, caplog: Any, tmp_path: Path
) -> None:
    """A partially failed cycle must be loud (#89).

    Production ran for days with github_sync and linear_sync failing on the
    Kuzu lock every hour while this logged "stages finished (exit=0)" at INFO.
    Now a partial failure returns nonzero and the scheduler says so at ERROR.
    """
    import logging

    import kb.server as server

    cognify_calls: list[bool] = []
    _patch_phase1(monkeypatch, code=1)
    monkeypatch.setattr(server, "get_citadel", lambda: _FakeCitadel(cognify_calls))

    with caplog.at_level(logging.ERROR, logger=server.logger.name):
        task = asyncio.create_task(server._evolve_scheduler_loop(0.001, str(tmp_path / "evolve_state.json")))
        try:
            for _ in range(300):
                if cognify_calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    failures = [r for r in caplog.records if "stages finished with failures" in r.message]
    assert failures, "a nonzero stages exit must be logged at ERROR"
    assert failures[0].levelno == logging.ERROR


async def test_a_dying_scheduler_task_is_logged(monkeypatch: Any, caplog: Any) -> None:
    """A scheduler that dies on its first line must not vanish silently.

    The done-callback used to be a bare set.discard, which swallows the
    exception: the task would simply stop existing. That is the same
    silent-failure shape as #89, and it matters more now that the scheduler
    imports and runs the stage code itself instead of shelling out (#88).
    """
    import logging

    import kb.server as server

    async def boom(_interval: float, _state_path: str) -> None:
        raise RuntimeError("scheduler could not start")

    monkeypatch.setattr(server, "_evolve_scheduler_loop", boom)
    monkeypatch.setattr(
        server, "get_citadel", lambda: _fake_citadel(enabled=True, interval=999_999)
    )

    with caplog.at_level(logging.ERROR, logger=server.logger.name):
        task = server._start_evolve_scheduler()
        assert task is not None
        with contextlib.suppress(RuntimeError):
            await task
        await asyncio.sleep(0)  # let the done-callback run

    died = [r for r in caplog.records if "died" in r.message]
    assert died, "a scheduler task that raised must be logged at ERROR"
    assert "evolve-scheduler" in died[0].getMessage()
