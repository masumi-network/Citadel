from __future__ import annotations

import json
from pathlib import Path


from kb.promotion_refs import (
    assess_org_reference,
    extract_repo_hints,
    load_tracked_org_repos,
    parse_capture_tags_from_text,
)


class FakeCitadel:
    def __init__(self, central_hits: bool = False) -> None:
        self.central_hits = central_hits

    async def search(self, query: str, **kwargs: object) -> list[dict[str, str]]:
        if self.central_hits:
            return [{"text": f"Central knowledge about {query}"}]
        return []


def test_extract_repo_hints_from_capture_summary() -> None:
    text = """
    # Capture summary: demo
    - Remote: `https://github.com/other-org/new-project.git`
    - Capture Root Tags: org-work
    """
    hints = extract_repo_hints(text)
    assert "other-org/new-project" in hints


def test_parse_capture_tags_from_text() -> None:
    assert parse_capture_tags_from_text("- Capture Root Tags: personal, capture") == (
        "personal",
        "capture",
    )


def test_load_tracked_org_repos_reads_github_state(tmp_path: Path) -> None:
    state_path = tmp_path / "github-state.json"
    state_path.write_text(
        json.dumps({"repos": {"masumi-network/Citadel-Archive": {"updated_at": "now"}}}),
        encoding="utf-8",
    )
    repos = load_tracked_org_repos(state_path, "masumi-network")
    assert "masumi-network/citadel-archive" in repos
    assert "citadel-archive" in repos


async def test_assess_org_reference_known_repo(tmp_path: Path) -> None:
    state_path = tmp_path / "github-state.json"
    state_path.write_text(
        json.dumps({"repos": {"masumi-network/Citadel-Archive": {}}}),
        encoding="utf-8",
    )
    text = "Work on https://github.com/masumi-network/Citadel-Archive/pull/1"
    result = await assess_org_reference(
        FakeCitadel(),
        candidate_text=text,
        central_dataset="masumi-network",
        github_state_path=state_path,
        github_org="masumi-network",
    )
    assert result.status == "known_org_work"


async def test_assess_org_reference_new_project(tmp_path: Path) -> None:
    state_path = tmp_path / "github-state.json"
    state_path.write_text(
        json.dumps({"repos": {"masumi-network/Citadel-Archive": {}}}), encoding="utf-8"
    )
    text = "Remote: https://github.com/other-org/brand-new-app.git"
    result = await assess_org_reference(
        FakeCitadel(central_hits=False),
        candidate_text=text,
        central_dataset="masumi-network",
        github_state_path=state_path,
        github_org="masumi-network",
    )
    assert result.status == "new_org_project"


async def test_assess_org_reference_central_match_without_repo_hint() -> None:
    text = "Shared interface contract with no repository reference."
    result = await assess_org_reference(
        FakeCitadel(central_hits=True),
        candidate_text=text,
        central_dataset="masumi-network",
        github_state_path=Path("/nonexistent/state.json"),
        github_org="masumi-network",
    )
    assert result.status == "known_org_work"
    assert result.reason == "central_match_no_repo"


async def test_assess_org_reference_no_hint_no_central_match() -> None:
    text = "Random note with no repo and no Central overlap."
    result = await assess_org_reference(
        FakeCitadel(central_hits=False),
        candidate_text=text,
        central_dataset="masumi-network",
        github_state_path=Path("/nonexistent/state.json"),
        github_org="masumi-network",
    )
    assert result.status == "no_reference_signal"
