#!/usr/bin/env node
// npm/pnpm wrapper for the Citadel Python CLI.
//
// It never touches system Python. On first real use it selects an interpreter
// (CITADEL_PYTHON, then python3, then python), creates a private venv under the
// user cache, installs the pinned Citadel wheel into that venv, and then execs
// the venv's `citadel` with the caller's arguments. All child processes run
// through argv arrays with the shell disabled, so arguments are never re-quoted
// or interpreted by a shell.
//
// `citadel --help` (and a bare invocation) print this wrapper's help and exit
// without any network or filesystem side effects.

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";

const PKG = "citadel-archive";
// Kept in lockstep with this wrapper's package.json version.
const PINNED_VERSION = "1.0.0";
// sha256 of the published wheel, rewritten by the publish workflow in lockstep
// with PINNED_VERSION. The all-zero sentinel keeps unpublished source trees
// fail-closed: pip --require-hashes rejects any real wheel against it. There is
// deliberately no environment override — the pin is immutable, so a poisoned
// environment cannot blank the hash or swap in an attacker-supplied wheel.
export const PINNED_WHEEL_SHA256 =
  "0000000000000000000000000000000000000000000000000000000000000000";

const IS_WINDOWS = process.platform === "win32";

const WRAPPER_HELP = `citadel — npm/pnpm wrapper for the Citadel CLI

Usage:
  citadel <command> [options]     run a Citadel command (bootstraps a private venv)
  citadel --help                  show this message

Interpreter selection order: CITADEL_PYTHON, python3, python (Python >= 3.11).
The wrapper installs ${PKG}==${PINNED_VERSION} into a user-cache venv and never
modifies system Python. Run \`citadel <command> --help\` for command help.`;

function fail(message) {
  process.stderr.write(`citadel: ${message}\n`);
  process.exit(1);
}

function isWrapperHelp(args) {
  return args.length === 0 || args[0] === "-h" || args[0] === "--help";
}

function findPython() {
  const candidates = [process.env.CITADEL_PYTHON, "python3", "python"].filter(Boolean);
  const probe = "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)";
  for (const py of candidates) {
    const result = spawnSync(py, ["-c", probe], { stdio: "ignore" });
    if (result.status === 0) return py;
  }
  return null;
}

export function venvDir() {
  const base = process.env.XDG_CACHE_HOME || join(homedir(), ".cache");
  return join(base, "citadel", `venv-${PINNED_VERSION}`);
}

function venvCitadel(venv) {
  const binDir = IS_WINDOWS ? join(venv, "Scripts") : join(venv, "bin");
  return {
    python: join(binDir, IS_WINDOWS ? "python.exe" : "python"),
    citadel: join(binDir, IS_WINDOWS ? "citadel.exe" : "citadel"),
  };
}

export function pinnedWheelHash() {
  if (!/^[0-9a-f]{64}$/.test(PINNED_WHEEL_SHA256)) {
    fail(
      "packaging error: the pinned wheel sha256 is missing or malformed. " +
        "Reinstall the citadel wrapper from a published release.",
    );
  }
  return PINNED_WHEEL_SHA256;
}

export function installSpec() {
  const dir = mkdtempSync(join(tmpdir(), "citadel-req-"));
  const file = join(dir, "requirements.txt");
  writeFileSync(file, `${PKG}==${PINNED_VERSION} --hash=sha256:${pinnedWheelHash()}\n`);
  return { requirement: file };
}

export function pipInstallArgs(requirement) {
  // --require-hashes is always on: the wheel hash is pinned, never optional, so
  // pip refuses any downloaded wheel whose digest does not match the pin.
  return [
    "-m",
    "pip",
    "install",
    "--upgrade",
    "--disable-pip-version-check",
    "--require-hashes",
    "-r",
    requirement,
  ];
}

function ensureVenv() {
  const venv = venvDir();
  const bins = venvCitadel(venv);
  if (existsSync(bins.citadel)) return bins.citadel;

  const py = findPython();
  if (!py) {
    fail("no Python >= 3.11 found. Set CITADEL_PYTHON or install python3.");
  }
  mkdirSync(venv, { recursive: true });
  let result = spawnSync(py, ["-m", "venv", venv], { stdio: "inherit" });
  if (result.status !== 0) fail("could not create the citadel venv");

  const { requirement } = installSpec();
  result = spawnSync(bins.python, pipInstallArgs(requirement), { stdio: "inherit" });
  if (result.status !== 0) fail(`could not install ${PKG}==${PINNED_VERSION}`);
  return bins.citadel;
}

function main() {
  const args = process.argv.slice(2);
  if (isWrapperHelp(args)) {
    process.stdout.write(`${WRAPPER_HELP}\n`);
    process.exit(0);
  }
  const citadel = ensureVenv();
  const result = spawnSync(citadel, args, { stdio: "inherit" });
  if (result.error) fail(String(result.error.message || result.error));
  process.exit(result.status == null ? 1 : result.status);
}

const isEntrypoint =
  Boolean(process.argv[1]) && fileURLToPath(import.meta.url) === process.argv[1];
if (isEntrypoint) main();
