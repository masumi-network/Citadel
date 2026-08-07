"""Static contract checks for the Next search filter slice.

The Next export is generated separately, and this repository has no browser or
component test runner for the page. Keep this test against authored TypeScript
so a filter cannot silently drift from the backend request contract or the
static-export CSP rules.
"""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SEARCH = (ROOT / "web/src/pages/app/search.tsx").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "web/src/lib/dashboard.ts").read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


def test_session_contract_and_scope_resolver_use_only_session_datasets() -> None:
    assert "default_dataset?: string | null;" in DASHBOARD
    assert "search_datasets?: string[] | null;" in DASHBOARD
    resolver = _block(DASHBOARD, "export function searchScopeDatasets", "export function canUse")

    assert "if (!session?.seat_slug) return { central: null, node: null };" in resolver
    assert "session.search_datasets" in resolver
    assert "session.default_dataset" in resolver
    assert 'const SESSION_TRACES_DATASET = "session-traces";' in DASHBOARD
    assert "dataset !== SESSION_TRACES_DATASET" in resolver


def test_source_options_are_the_backend_provenance_identities() -> None:
    options = _block(
        DASHBOARD,
        "export const SEARCH_SOURCE_OPTIONS = [",
        "export type SearchSource",
    )
    assert re.findall(r'value: "([^"]+)"', options) == ["repo-content", "linear-issue"]


def test_search_request_contains_only_supported_filter_fields() -> None:
    request_type = _block(SEARCH, "type SearchRequest = {", "const FILTER_BUTTON")
    fields = re.findall(r"^\s+([a-z_]+)\??:", request_type, re.MULTILINE)
    assert fields == ["query", "top_k", "dataset", "source"]

    builder = _block(SEARCH, "function buildSearchRequest", "const SECTION_ORDER")
    assert "if (dataset) request.dataset = dataset;" in builder
    assert 'if (source !== "all") request.source = source;' in builder
    assert 'scope:' not in builder
    assert 'source_type' not in builder
    assert 'filters:' not in builder
    assert "buildSearchRequest(query, selectedDataset, source)" in SEARCH


def test_filter_controls_preserve_query_url_and_static_csp_navigation() -> None:
    assert '<form method="get" action="/next/app/search"' in SEARCH
    assert 'name="q"' in SEARCH
    assert 'new URLSearchParams(window.location.search).get("q")' in SEARCH
    assert 'type="button"' in SEARCH
    assert 'aria-pressed=' in SEARCH
    assert 'label: "Everything"' in SEARCH
    assert 'label: "Central only"' in SEARCH
    assert 'label: "My Node only"' in SEARCH
    assert "next/link" not in SEARCH
    assert "history.pushState" not in SEARCH
    assert "window.location.assign" not in SEARCH


def test_single_literal_queries_render_flat_ranked_results() -> None:
    assert "function isSingleLiteralQuery(query: string | null)" in SEARCH
    assert "if (isSingleLiteralQuery(query))" in SEARCH
    assert 'return [{ key: "ranked", label: "Results", hits: results }];' in SEARCH
    assert "resultGroups(response, query)" in SEARCH
