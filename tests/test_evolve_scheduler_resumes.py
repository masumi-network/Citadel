"""#153: a redeploy must resume the evolve interval, not restart it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from kb.evolve_state import (
    first_sleep_seconds,
    read_last_completed,
    record_completed,
    staleness,
)

HOUR = 3600
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def test_a_redeploy_resumes_the_interval_instead_of_restarting_it() -> None:
    """The defect itself.

    Seven deploys on 2026-07-29 produced exactly one evolve pass, because each
    boot restarted a 3600s timer that no gap between deploys outlived. With the
    remainder carried across the restart, a boot 50 minutes into the interval
    waits the remaining 10, so the pass survives a busy merge day.
    """
    last = NOW - timedelta(minutes=50)
    assert first_sleep_seconds(HOUR, last, now=NOW) == 600.0


def test_deploys_closer_together_than_the_interval_still_reach_a_pass() -> None:
    """The scenario from the issue, as arithmetic rather than as a claim.

    The 2026-07-29 gaps with the one long window removed, which is the case the
    issue is actually about: a day where every deploy lands inside the interval.
    A restarting clock fires ZERO times across 79 minutes of uptime at a
    60-minute interval. A resuming clock fires once, on schedule.
    """
    gaps = [18, 7, 20, 1, 0, 33]
    assert sum(gaps) * 60 > HOUR, "the run must be long enough that a pass is due"
    assert all(g * 60 < HOUR for g in gaps), "every gap is shorter than the interval"

    restarting = sum(1 for g in gaps if g * 60 >= HOUR)
    assert restarting == 0, "this is the defect: no single gap ever reaches the interval"

    remaining, resuming = float(HOUR), 0
    for gap in gaps:
        if gap * 60 >= remaining:
            resuming += 1
            remaining = float(HOUR) - (gap * 60 - remaining)
        else:
            remaining -= gap * 60
    assert resuming >= 1, "resuming must reach a pass where restarting never can"


def test_a_genuine_first_boot_still_waits_a_full_interval() -> None:
    """The boot delay is deliberate and stays: no record means a full wait.

    The bug is the reset, not the delay. A fix that also removed the delay would
    make every redeploy trigger a heavy cycle on boot, which is what the delay
    exists to prevent.
    """
    assert first_sleep_seconds(HOUR, None, now=NOW) == float(HOUR)


def test_an_overdue_interval_runs_immediately_rather_than_waiting_again() -> None:
    last = NOW - timedelta(hours=9)
    assert first_sleep_seconds(HOUR, last, now=NOW) == 0.0


def test_a_future_timestamp_does_not_park_the_scheduler() -> None:
    """A clock jump or a copied state file must not stall the cycle silently.

    Without the clamp this returns interval + skew, so one bad timestamp parks
    evolve for an unbounded time while every counter still reads healthy. That is
    the monotone-cursor failure this codebase has already had once.
    """
    assert first_sleep_seconds(HOUR, NOW + timedelta(days=30), now=NOW) == float(HOUR)


def test_the_completion_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "evolve_state.json"
    record_completed(path, when=NOW)
    assert read_last_completed(path) == NOW


def test_a_corrupt_state_file_costs_the_resume_and_nothing_else(tmp_path: Path) -> None:
    """Fail-soft, and in the safe direction: wait longer, never redo work."""
    path = tmp_path / "evolve_state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert read_last_completed(path) is None
    assert first_sleep_seconds(HOUR, read_last_completed(path), now=NOW) == float(HOUR)


def test_staleness_is_reportable_so_a_stopped_cycle_is_visible(tmp_path: Path) -> None:
    """#153: 'a node that has not evolved in a week looks identical to one that
    evolved five minutes ago'. It must not."""
    path = tmp_path / "evolve_state.json"

    fresh = staleness(path, HOUR, now=NOW)
    assert fresh == {
        "last_completed_at": None,
        "age_seconds": None,
        "overdue": None,
        "last_run_ok": None,
        "last_run_reason": None,
    }

    record_completed(path, when=NOW - timedelta(minutes=5))
    recent = staleness(path, HOUR, now=NOW)
    assert recent["age_seconds"] == 300
    assert recent["overdue"] is False

    record_completed(path, when=NOW - timedelta(days=7))
    stopped = staleness(path, HOUR, now=NOW)
    assert stopped["overdue"] is True
    assert stopped["age_seconds"] == 7 * 24 * HOUR


def test_failed_cycle_is_recorded_as_failed(tmp_path: Path) -> None:
    path = tmp_path / "evolve_state.json"

    record_completed(path, when=NOW, ok=False, reason="stages_exit_1")

    state = staleness(path, HOUR, now=NOW)
    assert state["last_run_ok"] is False
    assert state["last_run_reason"] == "stages_exit_1"


def test_a_failed_stamp_is_logged_rather_than_swallowed(tmp_path: Path, caplog) -> None:
    """Degrading to a full interval is safe; degrading silently is not.

    CodeQL flagged the bare `except: pass` here, and it was right for a reason
    beyond style: if the completion cannot be written, the next boot restarts its
    interval instead of resuming, which is exactly the defect this module exists
    to fix. Swallowed without a word, #153 comes back while every counter still
    reads healthy.
    """
    import logging

    # A directory where the file should be: save cannot succeed, and the failure
    # is a real OSError rather than a mocked one.
    blocked = tmp_path / "evolve_state.json"
    blocked.mkdir()

    with caplog.at_level(logging.WARNING, logger="kb.evolve_state"):
        record_completed(blocked, when=NOW)
    messages = [r.getMessage() for r in caplog.records]
    assert any("record the evolve completion" in m for m in messages), (
        f"a failed stamp must say so; silence here reintroduces #153 invisibly: {messages}"
    )
    # The consequence has to be in the line, not just the fact of a failure:
    # someone reading it at 2am needs to know what it costs them.
    assert any("resuming" in m for m in messages)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kb.evolve_state"):
        assert read_last_completed(blocked) is None
    assert caplog.records, "an unreadable state file must be reported, not treated as a first boot"
