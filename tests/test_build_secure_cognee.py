from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tarfile
import zipfile

import pytest

from scripts import build_secure_cognee


def test_patch_changes_only_the_audited_requirement(tmp_path: Path) -> None:
    source = tmp_path / "cognee-1.4.1"
    source.mkdir()
    pyproject = source / "pyproject.toml"
    pyproject.write_text(
        f"[project]\ndependencies = [\n{build_secure_cognee.ORIGINAL_REQUIREMENT}\n]\n",
        encoding="utf-8",
    )

    build_secure_cognee.patch_pyproject(source)

    assert pyproject.read_text(encoding="utf-8") == (
        "[project]\ndependencies = [\n"
        f"{build_secure_cognee.PATCH_NOTICE}\n"
        f"{build_secure_cognee.PATCHED_REQUIREMENT}\n]\n"
    )
    with pytest.raises(RuntimeError, match="audited source"):
        build_secure_cognee.patch_pyproject(source)


def test_extract_rejects_parent_path(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    payload = b"unsafe"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("../outside")
        member.size = len(payload)
        archive.addfile(member, BytesIO(payload))

    with pytest.raises(RuntimeError, match="unsafe path"):
        build_secure_cognee.extract_source(archive_path, tmp_path / "extract")


def test_verify_wheel_requires_patched_metadata_and_notices(tmp_path: Path) -> None:
    wheel_path = tmp_path / "cognee-1.4.1-py3-none-any.whl"
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: cognee\n"
        "Version: 1.4.1\n"
        "Requires-Dist: cryptography<51,>=43.0.0\n\n"
    )
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.writestr("cognee-1.4.1.dist-info/METADATA", metadata)
        archive.writestr("cognee-1.4.1.dist-info/licenses/LICENSE", "Apache-2.0")
        archive.writestr("cognee-1.4.1.dist-info/licenses/NOTICE.md", "Cognee")

    evidence = build_secure_cognee.verify_wheel(wheel_path)

    assert evidence["name"] == "cognee"
    assert evidence["version"] == "1.4.1"
    assert evidence["cryptography_requirement"] == "cryptography<51,>=43.0.0"
    assert len(evidence["wheel_sha256"]) == 64


def test_expected_wheel_hash_matches_a_sha256_digest() -> None:
    assert len(build_secure_cognee.WHEEL_SHA256) == 64
    assert all(character in "0123456789abcdef" for character in build_secure_cognee.WHEEL_SHA256)


def test_build_rejects_an_unpinned_hatchling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(build_secure_cognee.metadata, "version", lambda package: "1.30.1")

    with pytest.raises(RuntimeError, match="hatchling 1.31.0 is required"):
        build_secure_cognee.build_wheel(tmp_path, tmp_path / "dist")
