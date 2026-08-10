#!/usr/bin/env python3
"""Classify Citadel and Qdrant phase logs with explicit failure allowances."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys


_SEVERE = re.compile(
    r"\b(?:warn(?:ing)?|error|panic|fatal|oom|corrupt(?:ion)?|fail(?:ed|ure)?|"
    r"degrad(?:ed|ation)|exception|traceback)\b",
    re.I,
)
_NON_2XX = re.compile(r"\b(?:1\d\d|3\d\d|4\d\d|5\d\d)\b")


@dataclass(frozen=True)
class Finding:
    source: str
    line_number: int
    line: str
    reason: str
    expected: bool


def classify_logs(
    *,
    phase: str,
    app_lines: list[str],
    qdrant_lines: list[str],
    expected_patterns: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    """Return all severe/non-2xx lines. Only caller-declared regexes are allowed."""
    if not phase.strip():
        raise ValueError("phase must be non-empty")
    compiled = {
        source: [re.compile(pattern) for pattern in patterns]
        for source, patterns in (expected_patterns or {}).items()
    }
    findings: list[Finding] = []
    for source, lines in (("app", app_lines), ("qdrant", qdrant_lines)):
        for line_number, line in enumerate(lines, start=1):
            text = line.rstrip("\n")
            reasons: list[str] = []
            if _SEVERE.search(text):
                reasons.append("severe")
            if _NON_2XX.search(text):
                reasons.append("non_2xx")
            if reasons:
                expected = any(pattern.search(text) for pattern in compiled.get(source, []))
                findings.append(Finding(source, line_number, text, "+".join(reasons), expected))
    expected = [asdict(finding) for finding in findings if finding.expected]
    unexpected = [asdict(finding) for finding in findings if not finding.expected]
    return {
        "phase": phase,
        "ok": not unexpected,
        "inspected_lines": {"app": len(app_lines), "qdrant": len(qdrant_lines)},
        "expected": expected,
        "unexpected": unexpected,
    }


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _parse_expectation(values: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for value in values:
        source, separator, pattern = value.partition(":")
        if separator != ":" or source not in {"app", "qdrant"} or not pattern:
            raise ValueError("--expect must be app:REGEX or qdrant:REGEX")
        grouped.setdefault(source, []).append(pattern)
    return grouped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--app-log", type=Path, required=True)
    parser.add_argument("--qdrant-log", type=Path, required=True)
    parser.add_argument("--expect", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = classify_logs(
            phase=args.phase,
            app_lines=_read_lines(args.app_log),
            qdrant_lines=_read_lines(args.qdrant_log),
            expected_patterns=_parse_expectation(args.expect),
        )
    except (OSError, ValueError, re.error) as exc:
        print(f"log classification failed: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
