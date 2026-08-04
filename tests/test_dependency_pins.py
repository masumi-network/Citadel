"""#150: the cognee pin drifted between pyproject.toml (<2.0.0) and
requirements.txt (<1.3.0). Railway installs from requirements.txt, so the
looser pyproject bound was inert in production but resolved cognee 1.4.0 for
any `pip install -e .` — and an uncapped side is what let production run
cognee 1.3.0 for ~28h in 2026-07 and stamp a foreign migration revision.
Keep both install paths on the same specifier so neither can drift alone.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cognee_requirement(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("cognee"):
            return stripped.rstrip(",").strip('"')
    raise AssertionError("no cognee requirement found")


def test_cognee_pin_matches_between_pyproject_and_requirements() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    server_deps = pyproject["project"]["optional-dependencies"]["server"]
    pyproject_pin = _cognee_requirement([str(item) for item in server_deps])
    requirements_pin = _cognee_requirement(
        (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    )
    assert pyproject_pin == requirements_pin
