"""Build the exact Cognee 1.4.1 source with Citadel's security metadata patch."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile


COGNEE_VERSION = "1.4.1"
COGNEE_TAG_COMMIT = "82bc3de9062af26ebcac3d61343d7e1a4f577586"
SOURCE_URL = (
    "https://files.pythonhosted.org/packages/d8/93/"
    "1277ebad661581e4eee8182e80cb898d26ae8f60f8aae2cf33329281c4db/"
    "cognee-1.4.1.tar.gz"
)
SOURCE_SHA256 = "9206075539935ef0adfab82cf410af6799e83c42969ba7c8fae5065de9aba7c9"
WHEEL_SHA256 = "2c1bec17b0ed9563ffa4f6ccdd4a02939cdec6dfd93db9faf852266ce3231a91"
HATCHLING_VERSION = "1.31.0"
ORIGINAL_REQUIREMENT = '    "cryptography>=43.0.0,<50",'
PATCHED_REQUIREMENT = '    "cryptography>=43.0.0,<51",'
PATCH_NOTICE = (
    "    # Modified by Citadel on 2026-08-08. Cryptography 50.0.0 fixes "
    "CVE-2026-69247."
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_source(destination: Path) -> None:
    with urlopen(SOURCE_URL, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = sha256_file(destination)
    if actual != SOURCE_SHA256:
        raise RuntimeError(
            f"Cognee source hash mismatch: expected {SOURCE_SHA256}, got {actual}"
        )


def extract_source(archive_path: Path, destination: Path) -> Path:
    root = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            target = (root / member.name).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"Cognee source contains an unsafe path: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                raise RuntimeError(f"Cognee source contains an unsafe member: {member.name}")
        archive.extractall(root, members=members, filter="data")
    candidates = [path for path in root.iterdir() if path.is_dir()]
    if len(candidates) != 1 or candidates[0].name != f"cognee-{COGNEE_VERSION}":
        raise RuntimeError(f"unexpected Cognee source root: {candidates}")
    return candidates[0]


def patch_pyproject(source_root: Path) -> Path:
    pyproject = source_root / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    if content.count(ORIGINAL_REQUIREMENT) != 1:
        raise RuntimeError("Cognee cryptography requirement did not match the audited source")
    if PATCH_NOTICE in content or PATCHED_REQUIREMENT in content:
        raise RuntimeError("Cognee source already contains the Citadel metadata patch")
    content = content.replace(
        ORIGINAL_REQUIREMENT,
        f"{PATCH_NOTICE}\n{PATCHED_REQUIREMENT}",
        1,
    )
    pyproject.write_text(content, encoding="utf-8")
    return pyproject


def build_wheel(source_root: Path, output_dir: Path) -> Path:
    try:
        installed_hatchling = metadata.version("hatchling")
    except metadata.PackageNotFoundError as error:
        raise RuntimeError(
            f"hatchling {HATCHLING_VERSION} is required to build Cognee"
        ) from error
    if installed_hatchling != HATCHLING_VERSION:
        raise RuntimeError(
            f"hatchling {HATCHLING_VERSION} is required, got {installed_hatchling}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--outdir",
            str(output_dir),
            str(source_root),
        ],
        check=True,
    )
    wheels = sorted(output_dir.glob("cognee-1.4.1-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one Cognee wheel, found {wheels}")
    return wheels[0]


def verify_wheel(wheel_path: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise RuntimeError("Cognee wheel does not contain exactly one METADATA file")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        requirements = metadata.get_all("Requires-Dist", [])
        cryptography = [item for item in requirements if item.lower().startswith("cryptography")]
        if len(cryptography) != 1 or "<51" not in cryptography[0] or "<50" in cryptography[0]:
            raise RuntimeError(f"Cognee wheel has unsafe cryptography metadata: {cryptography}")
        basenames = {Path(name).name for name in names}
        if "LICENSE" not in basenames or "NOTICE.md" not in basenames:
            raise RuntimeError("Cognee wheel is missing LICENSE or NOTICE.md")
    if metadata.get("Name") != "cognee" or metadata.get("Version") != COGNEE_VERSION:
        raise RuntimeError(
            f"unexpected Cognee wheel identity: {metadata.get('Name')} {metadata.get('Version')}"
        )
    return {
        "name": str(metadata["Name"]),
        "version": str(metadata["Version"]),
        "cryptography_requirement": cryptography[0],
        "wheel_sha256": sha256_file(wheel_path),
    }


def build(output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="citadel-cognee-build-") as temporary:
        temporary_path = Path(temporary)
        archive_path = temporary_path / f"cognee-{COGNEE_VERSION}.tar.gz"
        download_source(archive_path)
        source_root = extract_source(archive_path, temporary_path / "source")
        patch_pyproject(source_root)
        wheel_path = build_wheel(source_root, output_dir)
    evidence = verify_wheel(wheel_path)
    if evidence["wheel_sha256"] != WHEEL_SHA256:
        raise RuntimeError(
            "Cognee wheel hash mismatch: "
            f"expected {WHEEL_SHA256}, got {evidence['wheel_sha256']}"
        )
    manifest = {
        "upstream_project": "topoteretes/cognee",
        "upstream_version": COGNEE_VERSION,
        "upstream_tag_commit": COGNEE_TAG_COMMIT,
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "hatchling_version": HATCHLING_VERSION,
        "patch": f"{ORIGINAL_REQUIREMENT} -> {PATCHED_REQUIREMENT}",
        **evidence,
    }
    manifest_path = output_dir / "cognee-1.4.1-citadel-build.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
