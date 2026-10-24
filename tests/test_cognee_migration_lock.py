from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


CHILD_START_TIMEOUT_SECONDS = 60
CHILD_COMPLETION_TIMEOUT_SECONDS = 60
LOCK_CONTENTION_SECONDS = 0.5
POLL_INTERVAL_SECONDS = 0.1
MIGRATION_KEY = 42

CHILD_SCRIPT = """
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

from cognee.modules.migrations.runner import _migration_lock

db_path = Path(sys.argv[1])
attempted = Path(sys.argv[2])
acquired = Path(sys.argv[3])
key = int(sys.argv[4])
engine = SimpleNamespace(
    engine=SimpleNamespace(
        dialect=SimpleNamespace(name="sqlite"),
        url=SimpleNamespace(database=str(db_path)),
    )
)

async def acquire():
    attempted.write_text("attempted")
    async with _migration_lock(engine, key):
        acquired.write_text("acquired")

asyncio.run(acquire())
"""


def _sqlite_engine(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        engine=SimpleNamespace(
            dialect=SimpleNamespace(name="sqlite"),
            url=SimpleNamespace(database=str(db_path)),
        )
    )


def test_cognee_sqlite_migration_lock_serializes_processes(tmp_path: Path) -> None:
    from cognee.modules.migrations.runner import _migration_lock

    db_path = tmp_path / "store.db"
    attempted = tmp_path / "attempted"
    acquired = tmp_path / "acquired"

    async def check_lock() -> None:
        process: subprocess.Popen[str] | None = None
        try:
            async with _migration_lock(_sqlite_engine(db_path), MIGRATION_KEY):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        CHILD_SCRIPT,
                        str(db_path),
                        str(attempted),
                        str(acquired),
                        str(MIGRATION_KEY),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + CHILD_START_TIMEOUT_SECONDS
                while not attempted.exists() and process.poll() is None:
                    if loop.time() >= deadline:
                        raise AssertionError("child did not reach lock acquisition")
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                if process.poll() is not None:
                    output, _ = process.communicate()
                    raise AssertionError(f"child exited before lock acquisition: {output}")
                await asyncio.sleep(LOCK_CONTENTION_SECONDS)
                assert not acquired.exists(), "child entered while parent held the lock"

            assert process is not None
            output, _ = process.communicate(timeout=CHILD_COMPLETION_TIMEOUT_SECONDS)
            assert process.returncode == 0, output
            assert acquired.exists(), "child did not enter after parent released the lock"
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    asyncio.run(check_lock())
