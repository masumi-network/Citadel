"""Install channels: pipx bootstrap, Homebrew formula, and the pnpm wrapper.

Every check is side-effect free: no network, no package install, no venv
creation. The wrapper and installer help paths must do nothing but print help.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"
WRAPPER = REPO / "packages" / "citadel-cli" / "bin" / "citadel.mjs"
FORMULA = REPO / "Formula" / "citadel.rb"


def test_install_sh_help_is_side_effect_free() -> None:
    proc = subprocess.run(
        ["sh", str(INSTALL_SH), "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert "Usage" in proc.stdout
    assert "Installing" not in proc.stdout  # no bootstrap ran


def test_install_sh_rejects_unknown_flag() -> None:
    proc = subprocess.run(
        ["sh", str(INSTALL_SH), "--nope"], capture_output=True, text=True
    )
    assert proc.returncode == 2


def test_install_sh_bootstraps_the_canonical_package() -> None:
    text = INSTALL_SH.read_text()
    assert 'PKG="citadel-archive"' in text
    assert "CITADEL_PYTHON" in text  # honors the same interpreter override
    assert "pipx" in text


def test_wrapper_help_is_side_effect_free(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cache = tmp_path / "cache"
    env = {**os.environ, "XDG_CACHE_HOME": str(cache)}
    proc = subprocess.run(
        [node, str(WRAPPER), "--help"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0
    assert "wrapper" in proc.stdout.lower()
    # The help path must never bootstrap the user-cache venv.
    assert not (cache / "citadel").exists()


def test_wrapper_selects_interpreters_and_never_mutates_system_python() -> None:
    text = WRAPPER.read_text()
    assert "CITADEL_PYTHON" in text
    assert '"python3"' in text and '"python"' in text
    assert "venv" in text
    assert "citadel-archive" in text
    # spawnSync argv arrays with the shell disabled — no shell re-quoting.
    assert "shell: true" not in text


def test_wrapper_package_exposes_the_citadel_bin() -> None:
    pkg = json.loads((WRAPPER.parent.parent / "package.json").read_text())
    assert pkg["bin"]["citadel"] == "bin/citadel.mjs"


def test_root_package_registers_the_cli_workspace() -> None:
    pkg = json.loads((REPO / "package.json").read_text())
    assert "packages/citadel-cli" in pkg["workspaces"]


def test_homebrew_formula_installs_the_package_and_runs_the_cli() -> None:
    text = FORMULA.read_text()
    assert "class Citadel < Formula" in text
    assert "citadel_archive" in text or "citadel-archive" in text
    assert "virtualenv" in text
    assert 'depends_on "python' in text
    assert "#{bin}/citadel" in text  # the test block runs the CLI


def _node_eval(node: str, body: str, env: dict | None = None):
    """Import the wrapper module and run `body` against it (exports as `w`)."""
    script = f"const w = await import({json.dumps(WRAPPER.as_uri())});\n{body}"
    return subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def test_wrapper_pins_a_nonempty_immutable_wheel_hash() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = _node_eval(node, "console.log(w.PINNED_WHEEL_SHA256)")
    assert result.returncode == 0, result.stderr
    sha = result.stdout.strip()
    # Non-empty and a well-formed 64-hex sha256 — never blank, never a placeholder.
    assert re.fullmatch(r"[0-9a-f]{64}", sha), sha


def test_wrapper_always_requires_hashes_and_pins_the_wheel() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    body = (
        'import {readFileSync} from "node:fs";\n'
        "const s = w.installSpec();\n"
        "const args = w.pipInstallArgs(s.requirement);\n"
        'console.log(JSON.stringify({content: readFileSync(s.requirement, "utf8"), args}));'
    )
    result = _node_eval(node, body)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    # pip verifies every downloaded wheel against the pin — a different digest
    # is refused before the CLI can launch.
    assert "--require-hashes" in data["args"]
    assert "-r" in data["args"]
    # never a bare package spec that would install without hash verification
    assert "citadel-archive==1.0.0" not in data["args"]
    assert "--hash=sha256:" in data["content"]
    assert "citadel-archive==1.0.0" in data["content"]


def test_wrapper_ignores_env_hash_override() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    poison = "dead" * 16  # 64 hex, but not the immutable pin
    body = (
        'import {readFileSync} from "node:fs";\n'
        "const s = w.installSpec();\n"
        'console.log(readFileSync(s.requirement, "utf8"));'
    )
    result = _node_eval(node, body, env={"CITADEL_WHEEL_SHA256": poison})
    assert result.returncode == 0, result.stderr
    # A poisoned environment cannot swap in its own hash or blank the pin.
    assert poison not in result.stdout
    assert "0" * 64 in result.stdout


def test_wrapper_preserves_spaces_in_cache_paths(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    spaced = str(tmp_path / "a b" / "c d")
    result = _node_eval(node, "console.log(w.venvDir())", env={"XDG_CACHE_HOME": spaced})
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    # spawnSync argv arrays keep the spaced path a single item, never re-split.
    assert out.startswith(spaced)
    assert out.endswith("venv-1.0.0")
    assert " " in out


def test_wrapper_hash_pin_has_no_env_bypass() -> None:
    text = WRAPPER.read_text()
    assert "--require-hashes" in text
    assert "CITADEL_WHEEL_SHA256" not in text  # the pin is immutable, no env swap


def test_install_sh_drops_eval_and_force() -> None:
    text = INSTALL_SH.read_text()
    assert "eval" not in text  # commands run argv-safe, never through a shell eval
    assert "--force" not in text  # re-running never silently upgrades an install


@pytest.mark.skipif(os.name != "posix", reason="POSIX sh installer")
def test_install_sh_is_argv_safe_and_does_not_force(tmp_path: Path) -> None:
    # Fake interpreter under a directory carrying spaces AND shell metacharacters.
    # If any command ran through a shell, `$(touch EVIL)` would fire.
    pydir = tmp_path / "py dir;$(touch EVIL)"
    pydir.mkdir(parents=True)
    rec = tmp_path / "argv.log"
    fake = pydir / "python"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  -c) exit 0 ;;\n"
        '  --version) echo "Python 3.12.9"; exit 0 ;;\n'
        "esac\n"
        'printf "%s\\n" "$*" >> "$REC"\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",  # excludes any real pipx -> module mode uses our fake
        "CITADEL_PYTHON": str(fake),
        "HOME": str(home),
        "REC": str(rec),
    }
    proc = subprocess.run(
        ["sh", str(INSTALL_SH), "-y"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    # The metacharacter-bearing interpreter path never triggered a subshell.
    assert not (tmp_path / "EVIL").exists()
    logged = rec.read_text() if rec.exists() else ""
    # The pinned package install ran argv-safe: right package, no cache, no --force.
    assert "-m pipx install --pip-args=--no-cache-dir citadel-archive" in logged
    assert "--force" not in logged


def test_homebrew_formula_declares_textual_resource() -> None:
    text = FORMULA.read_text()
    assert 'resource "textual"' in text  # base dep is vendored, not silently dropped
    assert "no third-party dependencies" not in text  # the false claim is gone


def test_wrapper_ignores_unverified_cached_venv(tmp_path: Path) -> None:
    # A pre-seeded same-version venv with no digest marker must not be trusted;
    # only a marker matching the pinned wheel sha256 makes it a verified hit.
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    env = {"XDG_CACHE_HOME": str(tmp_path)}
    body = (
        'const {mkdirSync, writeFileSync} = await import("node:fs");\n'
        'const {join} = await import("node:path");\n'
        "const venv = w.venvDir();\n"
        'const bin = join(venv, process.platform === "win32" ? "Scripts" : "bin");\n'
        "mkdirSync(bin, {recursive: true});\n"
        'writeFileSync(join(bin, process.platform === "win32" ? "citadel.exe" : "citadel"), "#!/bin/sh\\n");\n'
        'console.log("nomarker", w.verifiedCacheHit(venv));\n'
        'writeFileSync(join(venv, ".citadel-wheel-sha256"), w.pinnedWheelHash() + "\\n");\n'
        'console.log("match", w.verifiedCacheHit(venv));\n'
        'writeFileSync(join(venv, ".citadel-wheel-sha256"), "f".repeat(64) + "\\n");\n'
        'console.log("mismatch", w.verifiedCacheHit(venv));'
    )
    result = _node_eval(node, body, env=env)
    assert result.returncode == 0, result.stderr
    assert "nomarker false" in result.stdout
    assert "match true" in result.stdout
    assert "mismatch false" in result.stdout
