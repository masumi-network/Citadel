"""#150: the cognee pin drifted between pyproject.toml (<2.0.0) and
requirements.txt (<1.3.0). Railway installs from requirements.txt, so the
looser pyproject bound was inert in production but resolved cognee 1.4.0 for
any `pip install -e .` — and an uncapped side is what let production run
cognee 1.3.0 for ~28h in 2026-07 and stamp a foreign migration revision.
Keep both install paths on the same specifier so neither can drift alone.
"""

from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

REPO_ROOT = Path(__file__).resolve().parents[1]
COGNEE_VERSION = "1.4.1"
CRYPTOGRAPHY_VERSION = "50.0.0"
QDRANT_ADAPTER_COMMIT = "7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29"
QDRANT_ADAPTER_URL = "https://github.com/topoteretes/cognee-community.git"
QDRANT_ADAPTER_SUBDIRECTORY = "packages/vector/qdrant"
QDRANT_ADAPTER_REQUIREMENT = (
    "cognee-community-vector-adapter-qdrant @ "
    f"git+{QDRANT_ADAPTER_URL}@{QDRANT_ADAPTER_COMMIT}"
    f"#subdirectory={QDRANT_ADAPTER_SUBDIRECTORY}"
)


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


def test_official_qdrant_adapter_commit_matches_between_install_paths() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    server_deps = [str(item) for item in pyproject["project"]["optional-dependencies"]["server"]]
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    package = "cognee-community-vector-adapter-qdrant"
    pyproject_pin = _requirement(server_deps, package)
    requirements_pin = _requirement(requirements, package)

    assert pyproject_pin == requirements_pin == QDRANT_ADAPTER_REQUIREMENT


def test_qdrant_client_pin_matches_between_pyproject_and_requirements() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    server_deps = [str(item) for item in pyproject["project"]["optional-dependencies"]["server"]]
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    pyproject_pin = _requirement(server_deps, "qdrant-client")
    requirements_pin = _requirement(requirements, "qdrant-client")

    assert pyproject_pin == requirements_pin == "qdrant-client==1.19.0"


def test_project_supports_python_3_12() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    supported = SpecifierSet(str(pyproject["project"]["requires-python"]))

    assert supported.contains("3.12", prereleases=True)


def test_installed_cognee_141_metadata_accepts_secure_cryptography() -> None:
    cognee = metadata.distribution("cognee")
    cryptography_requirements = [
        Requirement(item)
        for item in (cognee.requires or [])
        if Requirement(item).name == "cryptography"
    ]

    assert cognee.version == COGNEE_VERSION
    assert metadata.version("cryptography") == CRYPTOGRAPHY_VERSION
    assert cryptography_requirements, "Cognee metadata has no cryptography requirement"
    assert all(
        requirement.specifier.contains(CRYPTOGRAPHY_VERSION, prereleases=True)
        for requirement in cryptography_requirements
    ), (
        f"Cognee {COGNEE_VERSION} metadata rejects cryptography {CRYPTOGRAPHY_VERSION}: "
        f"{cryptography_requirements}"
    )


def test_installed_qdrant_adapter_matches_exact_source() -> None:
    adapter = metadata.distribution("cognee-community-vector-adapter-qdrant")
    direct_url_text = adapter.read_text("direct_url.json")

    assert direct_url_text is not None, "adapter installation has no direct_url.json"
    direct_url = json.loads(direct_url_text)
    assert direct_url["url"] == QDRANT_ADAPTER_URL
    assert direct_url["subdirectory"] == QDRANT_ADAPTER_SUBDIRECTORY
    assert direct_url["vcs_info"]["commit_id"] == QDRANT_ADAPTER_COMMIT
    assert direct_url["vcs_info"]["requested_revision"] == QDRANT_ADAPTER_COMMIT
