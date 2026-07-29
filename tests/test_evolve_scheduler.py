from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
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


def _fake_citadel(*, enabled: bool, interval: int = 21600) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            evolve_scheduler_enabled=enabled,
            evolve_interval_seconds=interval,
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


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:  # pragma: no cover - only on cancel
        pass


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


async def test_evolve_scheduler_loop_runs_subprocess_then_cognifies(monkeypatch: Any) -> None:
    import kb.server as server

    sub_envs: list[dict[str, Any]] = []
    cognify_calls: list[bool] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        sub_envs.append(kwargs.get("env", {}))
        return _FakeProc(0)

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server, "get_citadel", lambda: _FakeCitadel(cognify_calls))

    task = asyncio.create_task(server._evolve_scheduler_loop(0.001))
    try:
        for _ in range(300):
            if len(cognify_calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(sub_envs) >= 2
    # Phase 1: heavy stages in a subprocess with cognify disabled (frees the lock).
    assert sub_envs[0]["CITADEL_RUN_MODE"] == "evolve"
    assert sub_envs[0]["CITADEL_EVOLVE_COGNIFY_ENABLED"] == "false"
    # ...and add-only so its per-ingest background cognify never writes Kuzu (#47).
    assert sub_envs[0]["CITADEL_SUPPRESS_INLINE_COGNIFY"] == "true"
    # Phase 2: cognify ran in-loop after the subprocess.
    assert len(cognify_calls) >= 2
    # The verify canary verdict is recorded for /readyz (#27).
    assert server._LAST_CANARY is not None and server._LAST_CANARY["ok"] is True


async def test_evolve_scheduler_loop_cognifies_even_if_subprocess_fails(monkeypatch: Any) -> None:
    import kb.server as server

    cognify_calls: list[bool] = []

    async def boom_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        raise RuntimeError("spawn boom")

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", boom_exec)
    monkeypatch.setattr(server, "get_citadel", lambda: _FakeCitadel(cognify_calls))

    task = asyncio.create_task(server._evolve_scheduler_loop(0.001))
    try:
        for _ in range(300):
            if len(cognify_calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # A failed stages-subprocess (caught) must not skip the in-loop cognify.
    assert len(cognify_calls) >= 2


async def test_evolve_scheduler_logs_error_when_stages_exit_nonzero(
    monkeypatch: Any, caplog: Any
) -> None:
    """A partially failed cycle must be loud (#89).

    Production ran for days with github_sync and linear_sync failing on the
    Kuzu lock every hour while this logged "stages finished (exit=0)" at INFO.
    Now the subprocess returns nonzero and the scheduler says so at ERROR.
    """
    import logging

    import kb.server as server

    cognify_calls: list[bool] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        return _FakeProc(1)

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server, "get_citadel", lambda: _FakeCitadel(cognify_calls))

    with caplog.at_level(logging.ERROR, logger=server.logger.name):
        task = asyncio.create_task(server._evolve_scheduler_loop(0.001))
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
