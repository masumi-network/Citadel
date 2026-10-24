from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile

import pytest

from scripts import build_secure_fastembed


def _upstream_wheel(
    path: Path, *, pillow_requirements: tuple[str, ...] | None = None
) -> None:
    requirements = (
        build_secure_fastembed.ORIGINAL_PILLOW_REQUIREMENTS
        if pillow_requirements is None
        else pillow_requirements
    )
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: fastembed\n"
        "Version: 0.8.0\n"
        "Requires-Dist: numpy>=1.21\n"
        + "".join(f"{requirement}\n" for requirement in requirements)
        + "\n"
    )
    metadata_name = "fastembed-0.8.0.dist-info/METADATA"
    record_name = "fastembed-0.8.0.dist-info/RECORD"
    files = {
        "fastembed/__init__.py": b"# untouched source\n",
        metadata_name: metadata.encode(),
        "fastembed-0.8.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
    }
    record = build_secure_fastembed.make_record(files, record_name)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)
        wheel.writestr(record_name, record)


def test_patch_changes_only_three_audited_pillow_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.whl"
    patched = tmp_path / build_secure_fastembed.WHEEL_FILENAME
    _upstream_wheel(original)
    monkeypatch.setattr(
        build_secure_fastembed,
        "UPSTREAM_SHA256",
        build_secure_fastembed.sha256_file(original),
    )

    build_secure_fastembed.patch_wheel(original, patched)
    evidence = build_secure_fastembed.verify_wheel(patched)

    assert evidence["name"] == "fastembed"
    assert evidence["version"] == "0.8.0"
    with zipfile.ZipFile(original) as before, zipfile.ZipFile(patched) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            assert before.getinfo(name).compress_type == zipfile.ZIP_DEFLATED
            assert after.getinfo(name).compress_type == zipfile.ZIP_STORED
            if name.endswith(".dist-info/METADATA"):
                old = before.read(name).decode()
                new = after.read(name).decode()
                for source, replacement in zip(
                    build_secure_fastembed.ORIGINAL_PILLOW_REQUIREMENTS,
                    build_secure_fastembed.PATCHED_PILLOW_REQUIREMENTS,
                    strict=True,
                ):
                    old = old.replace(source, replacement, 1)
                assert new == old
            elif not name.endswith(".dist-info/RECORD"):
                assert after.read(name) == before.read(name)


def test_patch_rejects_metadata_drift_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.whl"
    patched = tmp_path / build_secure_fastembed.WHEEL_FILENAME
    _upstream_wheel(
        original,
        pillow_requirements=build_secure_fastembed.ORIGINAL_PILLOW_REQUIREMENTS[:2],
    )
    monkeypatch.setattr(
        build_secure_fastembed,
        "UPSTREAM_SHA256",
        build_secure_fastembed.sha256_file(original),
    )

    with pytest.raises(RuntimeError, match="Pillow requirement drifted"):
        build_secure_fastembed.patch_wheel(original, patched)

    assert not patched.exists()


def test_patch_rejects_unverified_upstream_bytes(tmp_path: Path) -> None:
    original = tmp_path / "original.whl"
    _upstream_wheel(original)

    with pytest.raises(RuntimeError, match="upstream hash mismatch"):
        build_secure_fastembed.patch_wheel(
            original, tmp_path / build_secure_fastembed.WHEEL_FILENAME
        )


def test_verify_rejects_record_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.whl"
    patched = tmp_path / build_secure_fastembed.WHEEL_FILENAME
    _upstream_wheel(original)
    monkeypatch.setattr(
        build_secure_fastembed,
        "UPSTREAM_SHA256",
        build_secure_fastembed.sha256_file(original),
    )
    build_secure_fastembed.patch_wheel(original, patched)

    with zipfile.ZipFile(patched, "a") as wheel:
        wheel.writestr("fastembed/added.py", b"not in RECORD")

    with pytest.raises(RuntimeError, match="RECORD does not match wheel files"):
        build_secure_fastembed.verify_wheel(patched)


def test_download_rejects_unverified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response(BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    monkeypatch.setattr(
        build_secure_fastembed, "urlopen", lambda *_args, **_kwargs: Response(b"wrong")
    )

    with pytest.raises(RuntimeError, match="upstream hash mismatch"):
        build_secure_fastembed.download_wheel(tmp_path / "source.whl")
