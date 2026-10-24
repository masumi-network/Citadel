"""Patch the exact FastEmbed 0.8.0 wheel metadata to permit Pillow 13."""

from __future__ import annotations

import argparse
import base64
import csv
from email.parser import BytesParser
import hashlib
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen
import zipfile

FASTEMBED_VERSION = "0.8.0"
WHEEL_FILENAME = "fastembed-0.8.0-py3-none-any.whl"
UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/2a/e8/"
    "26b7d78bb8972498c467ca34cb12ee2e60d26ba5eae6d8443189a1af37a5/" + WHEEL_FILENAME
)
UPSTREAM_SHA256 = "40bee672657574a1009e35ec50030a55f2b426842cb011845379817641bbbbd0"
WHEEL_SHA256 = "3d089e0f8f60f6869fa06205e06319ee70d109154537c41c0afe69fd0b96d3df"
METADATA_NAME = "fastembed-0.8.0.dist-info/METADATA"
RECORD_NAME = "fastembed-0.8.0.dist-info/RECORD"
WHEEL_NAME = "fastembed-0.8.0.dist-info/WHEEL"
ORIGINAL_PILLOW_REQUIREMENTS = (
    'Requires-Dist: pillow (>=10.3.0,<13.0) ; python_version >= "3.10" and python_version < "3.13"',
    'Requires-Dist: pillow (>=11.0.0,<13.0) ; python_version == "3.13"',
    'Requires-Dist: pillow (>=12.0.0,<13.0) ; python_version >= "3.14"',
)
PATCHED_PILLOW_REQUIREMENTS = tuple(
    requirement.replace("<13.0", "<14.0")
    for requirement in ORIGINAL_PILLOW_REQUIREMENTS
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_upstream_hash(path: Path) -> None:
    actual = sha256_file(path)
    if actual != UPSTREAM_SHA256:
        raise RuntimeError(
            f"FastEmbed upstream hash mismatch: expected {UPSTREAM_SHA256}, got {actual}"
        )


def download_wheel(destination: Path) -> None:
    with (
        urlopen(UPSTREAM_URL, timeout=120) as response,
        destination.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)
    try:
        _check_upstream_hash(destination)
    except RuntimeError:
        destination.unlink()
        raise


def _record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def make_record(files: dict[str, bytes], record_name: str) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, content in files.items():
        writer.writerow((name, _record_hash(content), len(content)))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode("utf-8")


def _verify_record(archive: zipfile.ZipFile) -> None:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise RuntimeError("FastEmbed wheel has duplicate members")
    if RECORD_NAME not in names:
        raise RuntimeError("FastEmbed wheel is missing RECORD")
    rows = list(
        csv.reader(StringIO(archive.read(RECORD_NAME).decode("utf-8"), newline=""))
    )
    if any(len(row) != 3 for row in rows):
        raise RuntimeError("FastEmbed RECORD contains a malformed row")
    recorded_names = [row[0] for row in rows]
    if len(recorded_names) != len(set(recorded_names)) or set(recorded_names) != set(
        names
    ):
        raise RuntimeError("FastEmbed RECORD does not match wheel files")
    for name, digest, size in rows:
        if name == RECORD_NAME:
            if digest or size:
                raise RuntimeError("FastEmbed RECORD must have no self hash")
        else:
            content = archive.read(name)
            if digest != _record_hash(content) or size != str(len(content)):
                raise RuntimeError(f"FastEmbed RECORD hash or size mismatch: {name}")


def _verify_metadata(content: bytes, expected_pillow: tuple[str, ...]) -> None:
    metadata = BytesParser().parsebytes(content)
    if (
        metadata.get("Name") != "fastembed"
        or metadata.get("Version") != FASTEMBED_VERSION
    ):
        raise RuntimeError(
            f"unexpected FastEmbed wheel identity: {metadata.get('Name')} {metadata.get('Version')}"
        )
    pillow = [
        line
        for line in content.decode("utf-8").splitlines()
        if line.lower().startswith("requires-dist: pillow")
    ]
    if pillow != list(expected_pillow):
        raise RuntimeError(f"FastEmbed Pillow requirement drifted: {pillow}")


def patch_wheel(upstream_path: Path, output_path: Path) -> None:
    _check_upstream_hash(upstream_path)
    if upstream_path.resolve() == output_path.resolve():
        raise RuntimeError("FastEmbed input and output wheel paths must differ")
    with zipfile.ZipFile(upstream_path) as upstream:
        _verify_record(upstream)
        names = upstream.namelist()
        if METADATA_NAME not in names or WHEEL_NAME not in names:
            raise RuntimeError("FastEmbed wheel is missing METADATA or WHEEL")
        metadata = upstream.read(METADATA_NAME)
        _verify_metadata(metadata, ORIGINAL_PILLOW_REQUIREMENTS)
        patched_metadata = metadata.decode("utf-8")
        for original, replacement in zip(
            ORIGINAL_PILLOW_REQUIREMENTS, PATCHED_PILLOW_REQUIREMENTS, strict=True
        ):
            if patched_metadata.splitlines().count(original) != 1:
                raise RuntimeError(f"FastEmbed Pillow requirement drifted: {original}")
            patched_metadata = patched_metadata.replace(original, replacement, 1)
        files = {name: upstream.read(name) for name in names if name != RECORD_NAME}
        files[METADATA_NAME] = patched_metadata.encode("utf-8")
        files[RECORD_NAME] = make_record(files, RECORD_NAME)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w") as output:
            output.comment = upstream.comment
            for info in upstream.infolist():
                # Stored members avoid platform-specific zlib output in the pinned wheel hash.
                info.compress_type = zipfile.ZIP_STORED
                output.writestr(info, files[info.filename])
    verify_wheel(output_path)


def verify_wheel(wheel_path: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel_path) as archive:
        _verify_record(archive)
        names = archive.namelist()
        if METADATA_NAME not in names or WHEEL_NAME not in names:
            raise RuntimeError("FastEmbed wheel is missing METADATA or WHEEL")
        content = archive.read(METADATA_NAME)
        _verify_metadata(content, PATCHED_PILLOW_REQUIREMENTS)
    return {
        "name": "fastembed",
        "version": FASTEMBED_VERSION,
        "wheel_sha256": sha256_file(wheel_path),
    }


def build(output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = output_dir / WHEEL_FILENAME
    with tempfile.TemporaryDirectory(prefix="citadel-fastembed-build-") as temporary:
        upstream_path = Path(temporary) / WHEEL_FILENAME
        download_wheel(upstream_path)
        patch_wheel(upstream_path, wheel_path)
    evidence = verify_wheel(wheel_path)
    if evidence["wheel_sha256"] != WHEEL_SHA256:
        raise RuntimeError(
            f"FastEmbed wheel hash mismatch: expected {WHEEL_SHA256}, "
            f"got {evidence['wheel_sha256']}"
        )
    manifest = {
        "upstream_url": UPSTREAM_URL,
        "upstream_sha256": UPSTREAM_SHA256,
        "patch": list(
            zip(ORIGINAL_PILLOW_REQUIREMENTS, PATCHED_PILLOW_REQUIREMENTS, strict=True)
        ),
        "pillow_requirements": list(PATCHED_PILLOW_REQUIREMENTS),
        **evidence,
    }
    manifest_path = output_dir / "fastembed-0.8.0-citadel-build.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return wheel_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel_path, manifest_path = build(args.output)
    print(wheel_path)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
