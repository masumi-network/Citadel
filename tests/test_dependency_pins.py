"""#150: the cognee pin drifted between pyproject.toml (<2.0.0) and
requirements.txt (<1.3.0). Railway installs from requirements.txt, so the
looser pyproject bound was inert in production but resolved cognee 1.4.0 for
any `pip install -e .` — and an uncapped side is what let production run
cognee 1.3.0 for ~28h in 2026-07 and stamp a foreign migration revision.
Keep both install paths on the same specifier so neither can drift alone.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

REPO_ROOT = Path(__file__).resolve().parents[1]
COGNEE_VERSION = "1.4.1"
CRYPTOGRAPHY_VERSION = "50.0.0"
LADYBUG_VERSION = "0.18.2"


def _cognee_requirement(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("cognee"):
            return stripped.rstrip(",").strip('"')
    raise AssertionError("no cognee requirement found")


def _requirement(lines: list[str], package: str) -> str:
    for line in lines:
        stripped = line.strip().rstrip(",").strip('"')
        if stripped.startswith(package):
            return stripped
    raise AssertionError(f"no {package} requirement found")


def test_cognee_pin_matches_between_pyproject_and_requirements() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    server_deps = pyproject["project"]["optional-dependencies"]["server"]
    pyproject_pin = _cognee_requirement([str(item) for item in server_deps])
    requirements_pin = _cognee_requirement(
        (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    )
    assert pyproject_pin == requirements_pin
    assert pyproject_pin == f"cognee[fastembed]=={COGNEE_VERSION}"


def test_server_install_does_not_depend_on_the_community_qdrant_package() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    server_deps = [str(item) for item in pyproject["project"]["optional-dependencies"]["server"]]
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert not any("cognee-community-vector-adapter-qdrant" in item for item in server_deps)
    assert not any("cognee-community-vector-adapter-qdrant" in item for item in requirements)


def test_qdrant_client_pin_matches_between_pyproject_and_requirements() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    server_deps = [str(item) for item in pyproject["project"]["optional-dependencies"]["server"]]
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    pyproject_pin = _requirement(server_deps, "qdrant-client")
    requirements_pin = _requirement(requirements, "qdrant-client")

    assert pyproject_pin == requirements_pin == "qdrant-client==1.19.0"


def test_ladybug_pin_matches_its_license_exemption() -> None:
    """Any ladybug bump must re-verify its license by hand.

    dependency-review-action's purlsMatch() compares package type and name
    only, so the allow-dependencies-licenses purl in
    .github/workflows/dependency-review.yml exempts ladybug from the license
    gate at EVERY version, not just the one in its @ suffix. The exemption
    exists because the dependency graph misreads MIT ladybug 0.18.2 as
    GPL-3.0-or-later. This equality is the compensating control: bumping
    ladybug fails here until the new version is confirmed MIT and the pins,
    the workflow purl, and this constant move together.
    """
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    workflow = (REPO_ROOT / ".github" / "workflows" / "dependency-review.yml").read_text(
        encoding="utf-8"
    )

    assert f'"ladybug=={LADYBUG_VERSION}"' in pyproject_text
    assert _requirement(requirements, "ladybug") == f"ladybug=={LADYBUG_VERSION}"
    assert f"pkg:pypi/ladybug@{LADYBUG_VERSION}" in workflow


def test_project_supports_python_3_12() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    supported = SpecifierSet(str(pyproject["project"]["requires-python"]))

    assert supported.contains("3.12", prereleases=True)


def test_installed_cognee_141_metadata_accepts_secure_cryptography() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    uv_overrides = [
        str(item)
        for item in pyproject.get("tool", {}).get("uv", {}).get("override-dependencies", [])
    ]
    cognee = metadata.distribution("cognee")
    cryptography_requirements = [
        Requirement(item)
        for item in (cognee.requires or [])
        if Requirement(item).name == "cryptography"
    ]

    assert cognee.version == COGNEE_VERSION
    assert metadata.version("cryptography") == CRYPTOGRAPHY_VERSION
    assert cryptography_requirements, "Cognee metadata has no cryptography requirement"
    metadata_accepts_secure = all(
        requirement.specifier.contains(CRYPTOGRAPHY_VERSION, prereleases=True)
        for requirement in cryptography_requirements
    )

    if not metadata_accepts_secure:
        assert any(
            Requirement(item).name == "cryptography"
            and Requirement(item).specifier.contains(CRYPTOGRAPHY_VERSION, prereleases=True)
            for item in uv_overrides
        ), (
            f"Cognee {COGNEE_VERSION} metadata rejects cryptography {CRYPTOGRAPHY_VERSION}: "
            f"{cryptography_requirements}. uv override is also missing a matching rule in "
            "pyproject.toml tool.uv.override-dependencies."
        )
