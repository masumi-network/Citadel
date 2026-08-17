"""Verify the files that make a built Citadel distribution usable."""

from __future__ import annotations

import argparse
import base64
import hashlib
from importlib.resources import files
import os
from pathlib import Path
from pathlib import PurePosixPath
import sysconfig
import tarfile
from tempfile import TemporaryDirectory
from unittest.mock import patch
import zipfile


BUNDLED_TREES = (
    "scripts",
    "skills",
    "docs/adr",
    "kb/static",
    "kb/webui",
    "kb/data/tiktoken-cache",
    "kb/deploy_assets",
)
CANONICAL_SKILLS = {
    "boundary": "citadel-data-boundary",
    "cli": "citadel-cli",
    "connect": "citadel-mcp-connector",
    "debug": "citadel-debug",
    "onboard": "citadel-onboard",
    "proactive-ingest": "citadel-proactive-ingest",
    "vault": "citadel-vault",
}
EXPECTED_SKILL_ALIASES = {
    "boundary": ["citadel-data-boundary", "policy", "privacy", "public-private"],
    "cli": ["citadel-cli"],
    "connect": ["citadel-mcp-connector", "mcp", "mcp-connector"],
    "debug": ["citadel-debug"],
    "onboard": ["citadel-onboard"],
    "proactive-ingest": ["autosync", "citadel-proactive-ingest"],
    "vault": ["citadel-vault"],
}


def _tree_payload(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def _wheel_payload(wheel: Path, tree: str) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    tree_parts = PurePosixPath(tree).parts
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            parts = PurePosixPath(info.filename).parts
            if (
                not info.is_dir()
                and len(parts) > len(tree_parts)
                and parts[: len(tree_parts)] == tree_parts
            ):
                payload[PurePosixPath(*parts[len(tree_parts) :]).as_posix()] = archive.read(info)
    return payload


def _sdist_payload(sdist: Path, tree: str) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    tree_parts = PurePosixPath(tree).parts
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            archived_tree = parts[1 : 1 + len(tree_parts)]
            if (
                not member.isfile()
                or len(parts) <= 1 + len(tree_parts)
                or archived_tree != tree_parts
            ):
                continue
            extracted = archive.extractfile(member)
            assert extracted is not None
            payload[PurePosixPath(*parts[1 + len(tree_parts) :]).as_posix()] = extracted.read()
    return payload


def _assert_tree_payload(label: str, actual: dict[str, bytes], expected: dict[str, bytes]) -> None:
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    assert not missing and not extra, f"{label} file mismatch: missing={missing}, extra={extra}"
    changed = sorted(name for name, content in expected.items() if actual[name] != content)
    assert not changed, f"{label} content mismatch: {changed}"


def _installed_root(repo_root: Path) -> Path:
    import kb
    import scripts

    package_files = {
        "kb": Path(kb.__file__).resolve(),
        "scripts": Path(scripts.__file__).resolve(),
    }
    purelib = Path(sysconfig.get_path("purelib")).resolve()
    for package, path in package_files.items():
        assert not path.is_relative_to(repo_root), (
            f"{package} imported from source checkout instead of installed wheel: {path}"
        )
        assert path.is_relative_to(purelib), (
            f"{package} imported outside the clean environment's site-packages: {path}"
        )
    roots = {path.parent.parent for path in package_files.values()}
    assert len(roots) == 1, f"installed packages have different roots: {package_files}"
    return roots.pop()


def _verify_public_skill_routes(expected_adr_count: int) -> None:
    with TemporaryDirectory(prefix="citadel-package-verifier-") as temp_dir:
        root = Path(temp_dir)
        isolated_env = {
            "CACHE_ROOT_DIRECTORY": str(root / "cache"),
            "CITADEL_LITE_DATA_ROOT": str(root / "data"),
            "COGNEE_LOG_FILE": "false",
            "COGNEE_LOGS_DIR": str(root / "logs"),
            "DATA_ROOT_DIRECTORY": str(root / "data-storage"),
            "HF_HOME": str(root / "huggingface"),
            "HOME": str(root / "home"),
            "LADYBUG_HOME_DIRECTORY": str(root / "ladybug-home"),
            "SYSTEM_ROOT_DIRECTORY": str(root / "cognee-system"),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
        }
        with patch.dict(os.environ, isolated_env):
            from fastapi.testclient import TestClient
            from kb.server import app

            client = TestClient(app, base_url="https://citadel.example")
            try:
                discovery_response = client.get("/.well-known/citadel.json")
                assert discovery_response.status_code == 200, discovery_response.text
                assert discovery_response.headers["cache-control"] == "public, max-age=300"
                discovery = discovery_response.json()
                assert discovery["ok"] is True

                catalog_response = client.get("/skills")
                assert catalog_response.status_code == 200, catalog_response.text
                assert catalog_response.headers["cache-control"] == "public, max-age=300"
                catalog = catalog_response.json()
                assert catalog["ok"] is True
                assert discovery["skills"] == catalog["skills"]

                state_response = client.get("/api/state")
                assert state_response.status_code == 200, state_response.text
                state = state_response.json()
                assert state["repo"]["adrs"] == expected_adr_count

                skills_by_slug = {row["slug"]: row for row in catalog["skills"]}
                assert set(skills_by_slug) == set(CANONICAL_SKILLS)
                for slug, row in skills_by_slug.items():
                    assert row["aliases"] == EXPECTED_SKILL_ALIASES[slug]
                    response = client.get(f"/skills/{slug}")
                    assert response.status_code == 200, (
                        f"{slug}: {response.status_code} {response.text}"
                    )
                    assert response.headers["content-type"].startswith("text/markdown")
                    expected_frontmatter = f"---\nname: {CANONICAL_SKILLS[slug]}\n".encode()
                    assert response.content.startswith(expected_frontmatter)
                    digest = hashlib.sha256(response.content).digest()
                    sha256 = digest.hex()
                    integrity = f"sha256-{base64.b64encode(digest).decode('ascii')}"
                    assert row["url"] == f"https://citadel.example/skills/{slug}"
                    assert row["size_bytes"] == len(response.content)
                    assert row["sha256"] == sha256
                    assert row["integrity"] == integrity
                    assert response.headers["cache-control"] == "public, max-age=300"
                    assert response.headers["etag"] == f'"sha256-{sha256}"'
                    assert response.headers["x-citadel-skill-sha256"] == sha256
                    assert response.headers["x-citadel-skill-integrity"] == integrity
                    for alias in row["aliases"]:
                        alias_response = client.get(f"/skills/{alias}")
                        assert alias_response.status_code == 200, (
                            f"{alias}: {alias_response.status_code} {alias_response.text}"
                        )
                        assert alias_response.content == response.content
                        assert alias_response.headers["x-citadel-skill-sha256"] == sha256
            finally:
                client.close()


def verify(dist_dir: Path, *, public_routes: bool = False) -> None:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AssertionError(
            f"expected one wheel and one sdist, found {len(wheels)} and {len(sdists)}"
        )

    repo_root = Path(__file__).resolve().parent.parent
    installed_root = _installed_root(repo_root)

    webui = files("kb").joinpath("webui")
    assert webui.joinpath("index.html").is_file()
    assert webui.joinpath("404.html").is_file()
    assert webui.joinpath("_next").is_dir()
    assert files("kb").joinpath("retrieval_eval.py").is_file()
    # kb.server runs the evolve scheduler in-process and imports
    # scripts.run_railway for the loop body, so the scripts package must ship in
    # the wheel or the scheduler dies on boot with ModuleNotFoundError.
    assert files("scripts").joinpath("run_railway.py").is_file()
    from scripts.run_railway import run_evolve_in_loop

    assert callable(run_evolve_in_loop)
    tokenizer_dir = files("kb").joinpath("data", "tiktoken-cache")
    tokenizer_files = [path for path in tokenizer_dir.iterdir() if path.is_file()]
    assert len(tokenizer_files) == 1
    assert tokenizer_files[0].name.endswith(".gz")

    source_adr_dir = repo_root / "docs" / "adr"
    expected_adr_count = sum(
        1 for path in source_adr_dir.glob("*.md") if path.name[:4].isdigit()
    )
    assert expected_adr_count > 0
    from kb.repo_stats import count_adr_records

    assert count_adr_records() == expected_adr_count

    with tarfile.open(sdists[0], "r:gz") as archive:
        names = set(archive.getnames())
    assert any(name.endswith("/kb/webui/index.html") for name in names)
    assert any(name.endswith("/kb/retrieval_eval.py") for name in names)
    assert any(name.endswith("/scripts/run_railway.py") for name in names)
    tokenizer_prefix = "/kb/data/tiktoken-cache/"
    assert any(
        tokenizer_prefix in name and name.endswith(".gz") for name in names
    )

    for tree in BUNDLED_TREES:
        expected = _tree_payload(repo_root / tree)
        _assert_tree_payload(f"wheel {tree}", _wheel_payload(wheels[0], tree), expected)
        _assert_tree_payload(f"sdist {tree}", _sdist_payload(sdists[0], tree), expected)
        _assert_tree_payload(f"installed {tree}", _tree_payload(installed_root / tree), expected)

    if public_routes:
        _verify_public_skill_routes(expected_adr_count)
    checked = " and public routes" if public_routes else ""
    print(f"release artifact webui, benchmark, scripts, skills, ADRs{checked} verified")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument(
        "--public-routes",
        action="store_true",
        help="Call discovery, catalog, and every canonical skill route (requires server extra).",
    )
    args = parser.parse_args()
    verify(args.dist, public_routes=args.public_routes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
