#!/usr/bin/env python3
"""Run the agent-feedback §9 canary (unit mocks; optional live Node).

Usage:
  python scripts/agent_canary.py
  python scripts/agent_canary.py --live

Prefer ``pytest -q -m canary`` for CI. This wrapper is for humans/agents.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also run the optional live Node probe (needs token + CITADEL_CANARY_LIVE)",
    )
    args = parser.parse_args()
    env = os.environ.copy()
    cmd = [sys.executable, "-m", "pytest", "-q", str(ROOT / "tests" / "test_agent_canary.py")]
    if args.live:
        env["CITADEL_CANARY_LIVE"] = "1"
        cmd.extend(["-m", "canary"])
    else:
        cmd.extend(["-m", "canary and not live"])
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
