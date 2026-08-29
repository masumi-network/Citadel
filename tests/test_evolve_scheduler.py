from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from kb.config import CitadelConfig


class _AsyncNullContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeCognee:
    def maintenance(self) -> _AsyncNullContext:
        return _AsyncNullContext()


class _RaisingMaintenance:
    async def __aenter__(self) -> None:
        raise RuntimeError("maintenance failed")

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _RaisingMaintenanceCognee:
    def maintenance(self) -> _RaisingMaintenance:
        return _RaisingMaintenance()


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
        self.resume_calls: list[bool] = []
        self.cognee = _FakeCognee()

    def resume_lifecycle_queue(self, *, include_deferred: bool = False) -> bool:
        # The scheduler kicks the projection drain after every pass, because
        # starts are suppressed while Phase 1 holds the writer lock and Phase 2
        # never drains the lifecycle queue itself. Recording the kwarg pins the
        # split: the pre-Phase-2 kick is vector-only (False), the post-pass
        # kick includes the deferred graph lane (True).
        self.resume_calls.append(include_deferred)
        return True

    async def cognify_dataset(self, *, force: bool = False, verify: bool = False) -> dict[str, Any]:
        self._cognify_calls.append(force)
        # The scheduler runs the verify canary (#27) and records its verdict.
        assert verify is True
        return {
            "ok": True,
            "graph_after": {"nodes": 7, "edges": 4},
            "graph_grew": True,
            "verification": {
                "marker": "COGNIFY_TEST_MARKER_x",
                "search_hit": True,
                "projection_chain_ok": None,
                "ok": True,
            },
        }


class _BlockingCognifyCitadel(_FakeCitadel):
    def __init__(self, cognify_calls: list[bool]) -> None:
        super().__init__(cognify_calls)
        self.cognify_started = asyncio.Event()
        self.cognify_tasks: list[asyncio.Task[Any]] = []

    async def cognify_dataset(
        self, *, force: bool = False, verify: bool = False
    ) -> dict[str, Any]:
        self._cognify_calls.append(force)
        assert verify is True
        task = asyncio.current_task()
        if task is not None:
            self.cognify_tasks.append(task)
        self.cognify_started.set()
        await asyncio.Event().wait()
        return {}


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
    fake_citadel = _FakeCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)

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
    # Every pass ends by kicking the projection drain: starts are suppressed
    # while Phase 1 holds the writer lock, and Phase 2 cognifies directly
    # without draining the lifecycle queue, so a job accepted mid-pass would
    # otherwise wait for the next external ingest or the next pass.
    assert len(fake_citadel.resume_calls) >= 1


async def test_evolve_scheduler_does_not_resume_after_phase2_cancellation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Cancellation mid-Phase-2 must add NO extra resume.

    The post-stages kick (before Phase 2) is expected and has already fired;
    tearing the loop down must not kick the drain again on a dying loop.
    """
    import kb.server as server

    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _BlockingCognifyCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(tmp_path / "evolve_state.json"))
    )
    await asyncio.wait_for(fake_citadel.cognify_started.wait(), timeout=3)
    resumes_before_cancel = list(fake_citadel.resume_calls)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert cognify_calls
    assert fake_citadel.resume_calls == resumes_before_cancel


async def test_drain_resume_fires_before_phase2(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The pass pauses the queue at start, so the drain kick must NOT wait
    behind Phase 2: a hung cognify (observed live 2026-08-28) would otherwise
    park a full-generation rebuild indefinitely.
    """
    import kb.server as server

    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _BlockingCognifyCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(tmp_path / "evolve_state.json"))
    )
    try:
        await asyncio.wait_for(fake_citadel.cognify_started.wait(), timeout=3)
        # Phase 2 is blocked right now and will never return; the drain must
        # already have been kicked, and vector-only: the pre-Phase-2 kick must
        # not (re)start the graph lane behind a possibly hung Phase 2.
        assert fake_citadel.resume_calls == [False], (
            "resume_lifecycle_queue must fire after Phase 1, before Phase 2, "
            "with include_deferred=False"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_hung_phase2_is_bounded_and_still_stamps(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A cognify that never returns must hit the Phase 2 bound: the canary
    flips unhealthy with the timeout, the pass still stamps (so the next boot
    resumes the interval instead of re-running forever), and the loop lives on.
    """
    import json as json_module

    import kb.server as server

    monkeypatch.setattr(server, "_evolve_cognify_timeout_seconds", lambda: 0.05)
    # Keep the orphan-skip budget out of the way: this test pins the timeout
    # verdict, not the wedge escalation, and the 1ms interval would reach the
    # default budget before the assertions run.
    monkeypatch.setenv("CITADEL_EVOLVE_ORPHAN_MAX_SKIPS", "1000000")
    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _BlockingCognifyCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(state_path))
    )
    try:
        for _ in range(300):
            if state_path.exists():
                break
            await asyncio.sleep(0.01)
        assert state_path.exists(), "the timed-out pass must still stamp"
        state = json_module.loads(state_path.read_text())
        assert state["last_run_ok"] is False
        assert state["last_run_reason"] == "cognify_timeout"
        assert server._LAST_CANARY is not None
        assert server._LAST_CANARY["ok"] is False
        assert server._LAST_CANARY.get("error") == "TimeoutError"
        assert not task.done(), "the loop must survive a Phase 2 timeout"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _cancel_phase2_orphans()


class _RaisingCitadel:
    """cognify_dataset raises, i.e. the #27 failure the canary exists to catch."""

    def __init__(self, cognify_calls: list[bool]) -> None:
        self._cognify_calls = cognify_calls
        self.cognee = _FakeCognee()

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


async def test_evolve_scheduler_records_failed_maintenance_entry(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import kb.server as server

    cognify_calls: list[bool] = []
    _patch_phase1(monkeypatch)
    fake_citadel = _FakeCitadel(cognify_calls)
    fake_citadel.cognee = _RaisingMaintenanceCognee()
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)

    state_path = tmp_path / "evolve_state.json"
    task = asyncio.create_task(server._evolve_scheduler_loop(0.001, str(state_path)))
    try:
        for _ in range(300):
            if state_path.exists() and cognify_calls:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_run_ok"] is False
    assert state["last_run_reason"] == "maintenance_exception"
    assert cognify_calls, "the recovery projection pass was not attempted"
    assert fake_citadel.resume_calls, "the paused lifecycle queue was not resumed"


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

    state_path = tmp_path / "evolve_state.json"
    with caplog.at_level(logging.ERROR, logger=server.logger.name):
        task = asyncio.create_task(server._evolve_scheduler_loop(0.001, str(state_path)))
        try:
            for _ in range(300):
                if state_path.exists():
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    failures = [r for r in caplog.records if "stages finished with failures" in r.message]
    assert failures, "a nonzero stages exit must be logged at ERROR"
    assert failures[0].levelno == logging.ERROR
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_run_ok"] is False
    assert state["last_run_reason"] == "stages_exit_1"


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


async def _cancel_phase2_orphans() -> None:
    """Test cleanup only: reap deliberately un-cancelled Phase 2 orphans."""
    for orphan in asyncio.all_tasks():
        if orphan.get_name() == "evolve-phase2-cognify" and not orphan.done():
            orphan.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await orphan


class _CountingMaintenance:
    def __init__(self, owner: "_CountingCognee") -> None:
        self._owner = owner

    async def __aenter__(self) -> None:
        self._owner.entries += 1

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _CountingCognee:
    def __init__(self) -> None:
        self.entries = 0

    def maintenance(self) -> _CountingMaintenance:
        return _CountingMaintenance(self)


class _LockCognee:
    """Real asyncio.Lock-backed maintenance, mirroring the gateway's."""

    def __init__(self) -> None:
        self.maintenance_lock = asyncio.Lock()
        self.entries = 0

    @contextlib.asynccontextmanager
    async def maintenance(self) -> Any:
        async with self.maintenance_lock:
            self.entries += 1
            yield


def _record_stamps(monkeypatch: Any) -> list[dict[str, Any]]:
    """Count record_completed calls; the loop imports it by name at start."""
    import kb.evolve_state as evolve_state

    real = evolve_state.record_completed
    stamps: list[dict[str, Any]] = []

    def recording(path: Any, **kwargs: Any) -> None:
        stamps.append(dict(kwargs))
        real(path, **kwargs)

    monkeypatch.setattr(evolve_state, "record_completed", recording)
    return stamps


async def test_pre_phase2_resume_is_vector_only(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The kick between Phase 1 and Phase 2 must be include_deferred=False
    (baseline lanes take no maintenance/writer lock, so the drain can never
    park behind Phase 2); the post-pass finally kick stays True so deferred
    graph work still drains after a healthy pass.
    """
    import kb.server as server

    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _FakeCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(tmp_path / "evolve_state.json"))
    )
    try:
        for _ in range(300):
            if len(fake_citadel.resume_calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(fake_citadel.resume_calls) >= 2
    assert fake_citadel.resume_calls[0] is False, "pre-Phase-2 kick must be vector-only"
    assert fake_citadel.resume_calls[1] is True, "post-pass kick must include deferred"


async def test_phase2_timeout_leaves_cognify_uncancelled(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """P1-B: no cancellation may reach a cognee write path in-process. A timed
    out Phase 2 stamps cognify_timeout and flips the canary, but the inner
    cognify task is left running un-cancelled (shield, not cancel).
    """
    import json as json_module

    import kb.server as server

    monkeypatch.setattr(server, "_evolve_cognify_timeout_seconds", lambda: 0.05)
    monkeypatch.setenv("CITADEL_EVOLVE_ORPHAN_MAX_SKIPS", "1000000")
    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _BlockingCognifyCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(state_path))
    )
    try:
        for _ in range(300):
            if state_path.exists():
                break
            await asyncio.sleep(0.01)
        assert state_path.exists(), "the timed-out pass must still stamp"
        state = json_module.loads(state_path.read_text())
        assert state["last_run_reason"] == "cognify_timeout"
        assert server._LAST_CANARY is not None
        assert server._LAST_CANARY["ok"] is False
        assert server._LAST_CANARY.get("error") == "TimeoutError"
        assert fake_citadel.cognify_tasks, "cognify was never attempted"
        first = fake_citadel.cognify_tasks[0]
        assert first.get_name() == "evolve-phase2-cognify"
        assert not first.cancelled(), (
            "the timed-out cognify must NOT be cancelled (P1-B)"
        )
        assert not first.done(), (
            "the hung cognify must be left running un-cancelled"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _cancel_phase2_orphans()


async def test_orphaned_phase2_skips_next_pass_without_pausing(
    monkeypatch: Any, caplog: Any, tmp_path: Path
) -> None:
    """While a Phase 2 orphan lives, later passes are SKIPPED: no second
    pause (a skip can never re-park the drain), no maintenance entry, no
    second stamp, and the skip is loud.
    """
    import logging

    import kb.server as server

    monkeypatch.setattr(server, "_evolve_cognify_timeout_seconds", lambda: 0.05)
    monkeypatch.setenv("CITADEL_EVOLVE_ORPHAN_MAX_SKIPS", "1000000")
    _patch_phase1(monkeypatch)
    stamps = _record_stamps(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _BlockingCognifyCitadel(cognify_calls)
    fake_citadel.cognee = _CountingCognee()
    fake_citadel.pause_calls = 0

    def _pause() -> None:
        fake_citadel.pause_calls += 1

    fake_citadel.pause_lifecycle_queue = _pause
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    with caplog.at_level(logging.ERROR, logger=server.logger.name):
        task = asyncio.create_task(
            server._evolve_scheduler_loop(0.001, str(state_path))
        )
        try:
            for _ in range(300):
                if any("pass skipped" in r.message for r in caplog.records):
                    break
                await asyncio.sleep(0.01)
            # Let a few more intervals elapse: every one must keep skipping.
            await asyncio.sleep(0.05)
            skips = [r for r in caplog.records if "pass skipped" in r.message]
            assert skips, "a skipped pass must be logged at ERROR"
            assert fake_citadel.pause_calls == 1, (
                "a skipped pass must not pause the queue again"
            )
            assert fake_citadel.cognee.entries == 1, (
                "a skipped pass must not enter maintenance"
            )
            assert len(stamps) == 1, "a skipped pass must not stamp"
            assert not task.done()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await _cancel_phase2_orphans()


async def test_orphan_skip_budget_flips_canary_unhealthy(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Past CITADEL_EVOLVE_ORPHAN_MAX_SKIPS skipped passes the canary records
    Phase2OrphanWedged so /readyz goes unhealthy and escalates to a restart.
    """
    import kb.server as server

    monkeypatch.setattr(server, "_evolve_cognify_timeout_seconds", lambda: 0.05)
    monkeypatch.setenv("CITADEL_EVOLVE_ORPHAN_MAX_SKIPS", "1")
    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _BlockingCognifyCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(tmp_path / "evolve_state.json"))
    )
    try:
        for _ in range(300):
            if (
                server._LAST_CANARY is not None
                and server._LAST_CANARY.get("error") == "Phase2OrphanWedged"
            ):
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _cancel_phase2_orphans()

    assert server._LAST_CANARY is not None
    assert server._LAST_CANARY["ok"] is False
    assert server._LAST_CANARY.get("error") == "Phase2OrphanWedged"


class _OrphanRecoveringCitadel(_FakeCitadel):
    """First cognify hangs until released, then completes; later ones pass."""

    def __init__(self, cognify_calls: list[bool]) -> None:
        super().__init__(cognify_calls)
        self.cognify_started = asyncio.Event()
        self.release = asyncio.Event()
        self._first = True

    async def cognify_dataset(
        self, *, force: bool = False, verify: bool = False
    ) -> dict[str, Any]:
        if self._first:
            self._first = False
            self.cognify_started.set()
            await self.release.wait()
        return await super().cognify_dataset(force=force, verify=verify)


async def test_orphan_completion_restores_normal_passes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Once the orphan finishes, the next pass runs Phase 1 + Phase 2 normally
    and stamps a full healthy cycle (orphan slot and skip counter reset).
    """
    import json as json_module

    import kb.server as server

    monkeypatch.setattr(server, "_evolve_cognify_timeout_seconds", lambda: 0.05)
    monkeypatch.setenv("CITADEL_EVOLVE_ORPHAN_MAX_SKIPS", "1000000")
    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _OrphanRecoveringCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(state_path))
    )
    try:
        for _ in range(300):
            if state_path.exists():
                break
            await asyncio.sleep(0.01)
        assert state_path.exists()
        assert (
            json_module.loads(state_path.read_text())["last_run_reason"]
            == "cognify_timeout"
        )
        # The hung cognify resolves between ticks; the orphan completes and the
        # next pass must run in full again.
        fake_citadel.release.set()
        for _ in range(300):
            if (
                state_path.exists()
                and json_module.loads(state_path.read_text())["last_run_ok"] is True
            ):
                break
            await asyncio.sleep(0.01)
        state = json_module.loads(state_path.read_text())
        assert state["last_run_ok"] is True, "a full pass must stamp again"
        assert server._LAST_CANARY is not None
        assert server._LAST_CANARY["ok"] is True
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _cancel_phase2_orphans()


class _InterleavingCitadel(_FakeCitadel):
    """Phase 2's cognify waits on maintenance behind a hung graph-lane job.

    Mirrors the gateway: cognee.cognify acquires the maintenance lock before
    writing, and the graph lane holds the same lock across its run_once.
    """

    def __init__(self, cognify_calls: list[bool]) -> None:
        super().__init__(cognify_calls)
        self.cognee = _LockCognee()
        self.graph_lane_task: asyncio.Task[Any] | None = None
        self.graph_lane_holding = asyncio.Event()
        self.pause_calls = 0

    def pause_lifecycle_queue(self) -> None:
        self.pause_calls += 1

    async def _graph_lane(self) -> None:
        async with self.cognee.maintenance():
            self.graph_lane_holding.set()
            await asyncio.Event().wait()

    async def cognify_dataset(
        self, *, force: bool = False, verify: bool = False
    ) -> dict[str, Any]:
        self._cognify_calls.append(force)
        assert verify is True
        # A graph-lane job woken by the shared projection gate got the FIFO
        # maintenance lock ahead of this cognify and hangs while holding it.
        self.graph_lane_task = asyncio.create_task(self._graph_lane())
        await self.graph_lane_holding.wait()
        async with self.cognee.maintenance():
            return {}


async def test_maintenance_lock_interleaving_times_out_waiter_only(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """THE interleaving case: Phase 2 waits behind a graph-lane job on the
    maintenance lock and hits its budget. Only the WAITER times out: the
    graph-lane holder is untouched, the pass stamps cognify_timeout, and the
    next tick takes the orphan-skip branch so Phase 1 never blocks on the
    held lock.
    """
    import json as json_module

    import kb.server as server

    monkeypatch.setattr(server, "_evolve_cognify_timeout_seconds", lambda: 0.05)
    monkeypatch.setenv("CITADEL_EVOLVE_ORPHAN_MAX_SKIPS", "1000000")
    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _InterleavingCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(state_path))
    )
    try:
        for _ in range(300):
            if state_path.exists():
                break
            await asyncio.sleep(0.01)
        assert state_path.exists(), "the timed-out pass must still stamp"
        state = json_module.loads(state_path.read_text())
        assert state["last_run_reason"] == "cognify_timeout"
        # The graph-lane holder is untouched: not cancelled, still holding.
        graph_lane = fake_citadel.graph_lane_task
        assert graph_lane is not None
        assert not graph_lane.cancelled()
        assert not graph_lane.done()
        assert fake_citadel.cognee.maintenance_lock.locked(), (
            "the graph-lane job must still hold the maintenance lock"
        )
        # Later ticks take the orphan-skip branch: no new pause, no Phase 1
        # blocking on the held lock (entries stay at Phase 1 + graph lane).
        entries_after_timeout = fake_citadel.cognee.entries
        pauses_after_timeout = fake_citadel.pause_calls
        await asyncio.sleep(0.05)
        assert fake_citadel.pause_calls == pauses_after_timeout == 1
        assert fake_citadel.cognee.entries == entries_after_timeout == 2
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if fake_citadel.graph_lane_task is not None:
            fake_citadel.graph_lane_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fake_citadel.graph_lane_task
        await _cancel_phase2_orphans()


async def test_phase1_maintenance_entry_is_bounded(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Maintenance held by an outside task (no orphan): Phase 1's bounded
    entry times out, the pass resumes the queue (include_deferred=True), does
    not stamp, and defers to the next interval instead of hanging.
    """
    import kb.server as server

    monkeypatch.setattr(
        server, "_evolve_maintenance_acquire_timeout_seconds", lambda: 0.05
    )
    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _FakeCitadel(cognify_calls)
    fake_citadel.cognee = _LockCognee()
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    holding = asyncio.Event()

    async def _hold() -> None:
        async with fake_citadel.cognee.maintenance():
            holding.set()
            await asyncio.Event().wait()

    holder = asyncio.create_task(_hold())
    await asyncio.wait_for(holding.wait(), timeout=1)

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(state_path))
    )
    try:
        for _ in range(300):
            if True in fake_citadel.resume_calls:
                break
            await asyncio.sleep(0.01)
        assert True in fake_citadel.resume_calls, (
            "a deferred pass must resume the paused queue with include_deferred=True"
        )
        assert not state_path.exists(), "a deferred pass must not stamp"
        assert cognify_calls == [], "Phase 2 must not run when Phase 1 was deferred"
        assert not task.done(), "the loop must survive a busy maintenance lock"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        holder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await holder


def test_phase2_timeout_floor_respects_canary_budget(monkeypatch: Any) -> None:
    """P2: the Phase 2 bound must always cover the canary wait plus margin,
    and the floor tracks a raised canary budget (formula, not constant).
    """
    import kb.server as server

    monkeypatch.delenv("CITADEL_CANARY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("CITADEL_EVOLVE_COGNIFY_TIMEOUT_SECONDS", "60")
    assert server._evolve_cognify_timeout_seconds() >= 900.0
    monkeypatch.setenv("CITADEL_CANARY_TIMEOUT_SECONDS", "2000")
    assert server._evolve_cognify_timeout_seconds() >= 2300.0


async def test_scheduler_teardown_cancels_phase2_task(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Loop teardown is the ONE path that still cancels the inner cognify
    (repaired at next boot by recover_stale_cognify_runs); the cancelled pass
    must not stamp.
    """
    import kb.server as server

    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _BlockingCognifyCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(state_path))
    )
    await asyncio.wait_for(fake_citadel.cognify_started.wait(), timeout=3)
    assert len(fake_citadel.cognify_tasks) == 1
    inner = fake_citadel.cognify_tasks[0]
    assert inner.get_name() == "evolve-phase2-cognify"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    with contextlib.suppress(asyncio.CancelledError):
        await inner

    assert inner.cancelled(), "teardown must cancel the inner Phase 2 task"
    assert not state_path.exists(), "a cancelled pass must not stamp"


async def test_maintenance_busy_skips_consume_the_skip_budget(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A pass that cannot enter maintenance is a SKIP, same budget as the
    orphan guard: past CITADEL_EVOLVE_ORPHAN_MAX_SKIPS deferred passes the
    canary records MaintenanceWedged so /readyz cannot stay green while the
    scheduler defers forever.
    """
    import kb.server as server

    monkeypatch.setattr(
        server, "_evolve_maintenance_acquire_timeout_seconds", lambda: 0.05
    )
    monkeypatch.setenv("CITADEL_EVOLVE_ORPHAN_MAX_SKIPS", "1")
    monkeypatch.setattr(server, "_LAST_CANARY", None)
    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _FakeCitadel(cognify_calls)
    fake_citadel.cognee = _LockCognee()
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    holding = asyncio.Event()

    async def _hold() -> None:
        async with fake_citadel.cognee.maintenance():
            holding.set()
            await asyncio.Event().wait()

    holder = asyncio.create_task(_hold())
    await asyncio.wait_for(holding.wait(), timeout=1)

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(state_path))
    )
    try:
        for _ in range(300):
            if (
                server._LAST_CANARY is not None
                and server._LAST_CANARY.get("error") == "MaintenanceWedged"
            ):
                break
            await asyncio.sleep(0.01)
        assert server._LAST_CANARY is not None, (
            "exhausted maintenance-busy skips must flip the canary"
        )
        assert server._LAST_CANARY["ok"] is False
        assert server._LAST_CANARY.get("error") == "MaintenanceWedged"
        assert not state_path.exists(), "a skipped pass must not stamp"
        assert cognify_calls == [], "Phase 2 must not run on a skipped pass"
        assert not task.done(), "the loop must survive budget exhaustion"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        holder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await holder


async def test_scheduler_exit_reaps_detached_phase2_orphan(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """After a timeout detaches Phase 2 as the orphan, cancelling the sleeping
    scheduler must still reap that task: lifespan awaits only the scheduler,
    so a leaked evolve-phase2-cognify would overlap shutdown and a later
    lifespan.
    """
    import json as json_module

    import kb.server as server

    monkeypatch.setattr(server, "_evolve_cognify_timeout_seconds", lambda: 0.05)
    monkeypatch.setenv("CITADEL_EVOLVE_ORPHAN_MAX_SKIPS", "1000000")
    _patch_phase1(monkeypatch)
    cognify_calls: list[bool] = []
    fake_citadel = _BlockingCognifyCitadel(cognify_calls)
    monkeypatch.setattr(server, "get_citadel", lambda: fake_citadel)
    state_path = tmp_path / "evolve_state.json"

    task = asyncio.create_task(
        server._evolve_scheduler_loop(0.001, str(state_path))
    )
    try:
        await asyncio.wait_for(fake_citadel.cognify_started.wait(), timeout=3)
        inner = fake_citadel.cognify_tasks[0]
        # Wait until the pass timed out and stamped, i.e. the inner task is
        # now the detached orphan and the scheduler is sleeping.
        for _ in range(300):
            if state_path.exists():
                break
            await asyncio.sleep(0.01)
        assert state_path.exists()
        assert (
            json_module.loads(state_path.read_text())["last_run_reason"]
            == "cognify_timeout"
        )
        assert not inner.done(), "precondition: the orphan is still running"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert inner.done(), (
            "scheduler exit must cancel and await the detached Phase 2 orphan"
        )
        assert inner.cancelled()
        assert inner not in server._BACKGROUND_TASKS
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _cancel_phase2_orphans()
