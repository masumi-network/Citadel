"""Single-instance lock and Lite configuration error.

Leaf module: imports nothing from ``kb``. Both ``kb.lite_runtime`` and
``kb.generation_backup`` need these two names; keeping them here breaks the
``lite_runtime <-> generation_backup`` import cycle.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import socket
from typing import IO


class LiteConfigurationError(RuntimeError):
    pass


_LOCK_HANDLE: IO[str] | None = None


def acquire_single_instance_lock(
    root: Path,
    *,
    state_root: Path | None = None,
) -> IO[str]:
    global _LOCK_HANDLE
    lock_path = (state_root or root / "citadel-state") / "lite-runtime.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise LiteConfigurationError(
            "SQLite Lite allows exactly one Citadel process for this data volume"
        ) from error
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} host={socket.gethostname()}\n")
    handle.flush()
    os.fsync(handle.fileno())
    os.set_inheritable(handle.fileno(), True)
    _LOCK_HANDLE = handle
    return handle
