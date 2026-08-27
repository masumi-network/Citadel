"""When the evolve cycle last completed, and how long to wait before the next one.

The scheduler sleeps a full interval before its first pass so that a redeploy
never triggers a heavy cycle on boot. That is deliberate and stays. The defect
(#153) is that the clock RESTARTS on every boot rather than resuming, so on a day
with deploys closer together than the interval the cycle never fires at all.
Seven deploys on 2026-07-29 produced exactly one pass, inside the only gap that
happened to exceed the interval.

Nothing reported it. There was no record of a last successful pass anywhere, so a
node that had not evolved in a week looked identical to one that evolved five
minutes ago. That is the same silent-success shape as #89, #114, #148 and #151:
the absence of a signal read as the absence of a problem.

Two pieces, deliberately small: persist the completion time, and compute the
first sleep from it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any

from kb.state_io import StateFileError, load_state_file, save_state_file

logger = logging.getLogger(__name__)

STATE_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_last_completed(path: str | Path) -> datetime | None:
    """The last recorded completion, or None when there is no usable record.

    Fail-soft on purpose. A missing or corrupt file must not stop the scheduler
    from running; it only costs the resume, which degrades to today's behaviour
    of a full first interval. That trade is the opposite of #148's (where
    flattening a corrupt state to empty caused silent re-ingestion), because here
    the fallback is to wait LONGER, never to redo work.
    """
    try:
        data = load_state_file(Path(path))
    except (StateFileError, OSError) as exc:
        # Degrading to a full first interval is safe; degrading SILENTLY is not.
        # Without this line an unreadable state file looks exactly like a first
        # boot, and the resume this module exists for is quietly not happening.
        logger.warning(
            "Evolve state at %s is unreadable (%s); the interval will restart "
            "rather than resume this boot",
            path,
            exc.__class__.__name__,
        )
        return None
    if not isinstance(data, dict):
        return None
    return _parse(data.get("last_completed_at"))


def record_completed(
    path: str | Path,
    when: datetime | None = None,
    *,
    ok: bool = True,
    reason: str | None = None,
) -> None:
    """Stamp a finished pass and its truthful outcome."""
    try:
        save_state_file(
            Path(path),
            {
                "version": STATE_VERSION,
                "last_completed_at": (when or _now()).isoformat().replace("+00:00", "Z"),
                "last_run_ok": ok,
                "last_run_reason": reason,
            },
        )
    except (StateFileError, OSError) as exc:
        # Swallowed on purpose: a bookkeeping write must never fail an evolve
        # pass that has already done its work. But it is LOGGED, because the
        # cost is the next boot restarting its interval instead of resuming,
        # which is the defect this module fixes. A silent failure here would
        # reintroduce #153 while every counter still read healthy.
        logger.warning(
            "Could not record the evolve completion at %s (%s); the next boot "
            "will wait a full interval instead of resuming",
            path,
            exc.__class__.__name__,
        )


def first_sleep_seconds(
    interval_seconds: int,
    last_completed: datetime | None,
    now: datetime | None = None,
) -> float:
    """How long to wait before the FIRST pass after a boot.

    With no record, a full interval, preserving the deliberate boot delay. With a
    record, the remainder of the interval that was already running, so a restart
    resumes instead of resetting.

    A completion timestamp in the future is treated as "just now" rather than
    trusted. A clock that jumped, or a state file copied from elsewhere, would
    otherwise park the scheduler for an unbounded time while every counter still
    reported healthy -- the failure mode recorded in the monotone-cursor lesson,
    where one future date stalled all writes and reported ok.
    """
    interval = max(0, int(interval_seconds))
    if last_completed is None:
        return float(interval)
    elapsed = ((now or _now()) - last_completed).total_seconds()
    if elapsed < 0:
        return float(interval)
    return float(max(0, interval - elapsed))


def staleness(
    path: str | Path, interval_seconds: int, now: datetime | None = None
) -> dict[str, Any]:
    """A reader-visible answer to "has the cycle actually been running?".

    `overdue` compares against twice the interval rather than one, so a pass that
    merely started late does not read as broken while a cycle that genuinely
    stopped does.
    """
    try:
        state = load_state_file(Path(path))
    except (StateFileError, OSError):
        state = {}
    last = _parse(state.get("last_completed_at")) if isinstance(state, dict) else None
    if last is None:
        return {
            "last_completed_at": None,
            "age_seconds": None,
            "overdue": None,
            "last_run_ok": None,
            "last_run_reason": None,
        }
    age = max(0.0, ((now or _now()) - last).total_seconds())
    return {
        "last_completed_at": last.isoformat().replace("+00:00", "Z"),
        "age_seconds": int(age),
        "overdue": age > 2 * max(1, int(interval_seconds)),
        "last_run_ok": state.get("last_run_ok"),
        "last_run_reason": state.get("last_run_reason"),
    }
