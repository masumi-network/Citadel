"""Release workflow gates that cannot be covered by the local test suite."""

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"


def test_publish_requires_main_ancestry_and_current_ci_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    guard = workflow.split("  release-guard:\n", 1)[1].split("\n  build:\n", 1)[0]

    assert 'git merge-base --is-ancestor "$GITHUB_SHA" "origin/main"' in guard
    assert "commits/${GITHUB_SHA}/check-runs" in guard
    assert 'select(.name == "CI gate" and .conclusion == "success")' in guard
    assert "needs: release-guard" in workflow
    assert "needs: [release-guard, build]" in workflow


def test_publish_keeps_pypi_and_release_permissions_on_publish_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    publish = workflow.split("  publish:\n", 1)[1]

    assert "environment: pypi" in publish
    assert "id-token: write" in publish
    assert "contents: write" in publish
