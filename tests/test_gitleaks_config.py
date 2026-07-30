"""Structural invariants for .gitleaks.toml.

A secret scanner that has been switched off and one that finds nothing produce
identical output, so nothing about a green CI run tells you the config still
works. These tests pin the shape that keeps it working.

Structure only, deliberately. Running the scanner needs the gitleaks binary,
which CI downloads in .github/workflows/secret-scan.yml but pytest has no way
to depend on. What can be checked without it is the class of edit that silently
disarms the config, and that is worth more than it sounds: this repo shipped
exactly that bug. One allowlist block written without ``targetRules`` relaxed
every rule in the file, a live token committed to an exempted path scanned
clean, and the config header asserted zero findings across full history. It was
true, and it was true because nothing was running.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

CONFIG_PATH = Path(__file__).resolve().parents[1] / ".gitleaks.toml"


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():  # pragma: no cover - the file is committed
        pytest.skip(f"{CONFIG_PATH.name} not present")
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _allowlists(config: dict[str, Any]) -> list[dict[str, Any]]:
    return list(config.get("allowlists") or ())


def test_the_config_parses(config: dict[str, Any]) -> None:
    """A malformed config is a scanner that does not run at all."""
    assert _allowlists(config), "no [[allowlists]] blocks found; the file shape changed"
    assert config.get("rules"), "no [[rules]] blocks found"


def test_every_regex_allowlist_names_the_rules_it_relaxes(config: dict[str, Any]) -> None:
    """A ``regexes`` entry without ``targetRules`` is a repo-wide off switch.

    Without ``targetRules`` a block applies to EVERY rule, so one pattern
    intended to quiet a single false positive silences the whole file wherever
    it matches. This is the exact edit that disarmed the scanner.

    A block with only ``paths`` and no ``regexes`` is allowed to omit
    ``targetRules``: exempting whole files from everything is a decision someone
    can read at a glance, which is not true of a bare regex.
    """
    offenders = [
        index
        for index, block in enumerate(_allowlists(config))
        if block.get("regexes") and not block.get("targetRules")
    ]
    assert not offenders, (
        f"allowlist block(s) {offenders} carry `regexes` with no `targetRules`, "
        "which relaxes every rule in the file rather than the intended one"
    )


def test_path_and_regex_allowlists_require_both_to_match(config: dict[str, Any]) -> None:
    """gitleaks defaults to ``condition = "OR"``, where either half alone exempts.

    A block pairing a path with a shape almost always means "this shape, in this
    place". Under the default that reads as "this shape anywhere, OR anything at
    all in this place", so a real credential in the exempted directory passes.
    """
    offenders = [
        index
        for index, block in enumerate(_allowlists(config))
        if block.get("paths")
        and block.get("regexes")
        and block.get("condition", "OR").upper() != "AND"
    ]
    assert not offenders, (
        f"allowlist block(s) {offenders} pair `paths` with `regexes` but do not set "
        'condition = "AND", so either half alone exempts a finding'
    )


# Built-in gitleaks rule ids this config's allowlists target. The config sets
# `[extend] useDefault = true`, so most rule ids come from gitleaks itself and
# cannot be read out of this file. Maintained by hand, which is the point: adding
# a name here is a deliberate act, and a typo in .gitleaks.toml is not.
KNOWN_BUILTIN_RULES = frozenset(
    {
        "curl-auth-header",
        "generic-api-key",
        "github-pat",
    }
)


def test_target_rules_reference_rules_that_exist(config: dict[str, Any]) -> None:
    """A typo in ``targetRules`` makes the block silently do nothing.

    Fail-safe rather than fail-open, so it cannot hide a secret. What it does is
    bring back the false positive the block was written for, while the block
    still looks like it handles it, which is how a scoped allowlist quietly
    becomes an unscoped one at the next "fix".
    """
    local = {str(rule.get("id")) for rule in config.get("rules") or () if rule.get("id")}
    known = local | KNOWN_BUILTIN_RULES
    unknown: dict[int, list[str]] = {}
    for index, block in enumerate(_allowlists(config)):
        missing = [
            name for name in (block.get("targetRules") or ()) if str(name) not in known
        ]
        if missing:
            unknown[index] = missing
    assert not unknown, (
        f"targetRules naming unknown rules: {unknown}. Either it is a typo, or a "
        "new gitleaks built-in is being targeted and belongs in KNOWN_BUILTIN_RULES."
    )


def test_every_rule_has_an_id(config: dict[str, Any]) -> None:
    """``targetRules`` addresses rules by id, so a rule without one cannot be scoped."""
    missing = [
        index for index, rule in enumerate(config.get("rules") or ()) if not rule.get("id")
    ]
    assert not missing, f"rule(s) {missing} have no id, so no allowlist can target them"
