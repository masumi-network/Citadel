#!/usr/bin/env python3
"""Validate a captured v0.5 runtime receipt without making network requests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from kb.release_acceptance import ReleaseAcceptanceError, load_seed_manifest, validate_release_evidence


def build_jsonrpc_request(
    method: str, params: dict[str, Any], request_id: str = "release-probe"
) -> dict[str, Any]:
    """Build raw MCP JSON-RPC body. Transport and authorization stay with the operator."""
    if not method.strip() or not request_id.strip():
        raise ValueError("method and request_id must be non-empty")
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_seed_manifest(args.manifest)
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        receipt = validate_release_evidence(manifest, evidence)
    except (OSError, json.JSONDecodeError, ReleaseAcceptanceError, ValueError):
        print("release probe failed; inspect captured evidence locally", file=sys.stderr)
        return 1
    rendered = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
