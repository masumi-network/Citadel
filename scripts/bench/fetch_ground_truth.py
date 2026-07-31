"""Cache the ground-truth documents named by the golden question set.

Retrieval scoring must not depend on a hit carrying its source path: only the
first chunk of a repo-content document does. Caching the real file text lets the
benchmark match a hit by content overlap instead, which survives chunking.

Usage:
    GITHUB_TOKEN=... python scripts/bench/fetch_ground_truth.py
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "ground_truth"


def fetch(repo_path: str, token: str | None) -> str | None:
    owner, repo, path = repo_path.split("/", 2)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED {repo_path}: {exc.__class__.__name__}")
        return None
    content = body.get("content")
    if not content:
        return None
    return base64.b64decode(content).decode("utf-8", errors="replace")


def main() -> int:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    questions = json.loads((HERE / "golden_questions.json").read_text(encoding="utf-8"))
    CACHE.mkdir(exist_ok=True)
    wanted: set[str] = set()
    for question in questions["questions"]:
        wanted.update(question["expect_any"])

    for repo_path in sorted(wanted):
        if "/" not in repo_path or repo_path.startswith("linear:"):
            # Linear ground truth is an identifier, not a fetchable path; it is
            # matched off the `# Linear SOK-123:` header instead.
            continue
        target = CACHE / (repo_path.replace("/", "__"))
        if target.exists():
            continue
        text = fetch(repo_path, token)
        if text is None:
            continue
        target.write_text(text, encoding="utf-8")
        print(f"  cached {repo_path} ({len(text)} chars)")

    print(f"\n{len(list(CACHE.iterdir()))} ground-truth documents cached in {CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
