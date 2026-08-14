"""The Next.js export, its canonical public routes, and the safe fallback.

The rebuilt frontend is checked in for self-hosted installs. The public routes
serve it when present, while an unbuilt source checkout keeps the hand-written
pages usable. The authenticated dashboard stays on its established route until
its graph and admin flows have parity.
"""

from __future__ import annotations

from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

import kb.server as server_module

from kb.server import app


def _client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _exported_html() -> list[Path]:
    return sorted(server_module.WEBUI_DIR.rglob("*.html"))


# Every preview route, and one phrase from each page that only appears if the
# page actually rendered rather than merely returning a shell.
PREVIEWS = (
    ("/next", "Citadel remembers your"),
    ("/next/info", "Shared, governed memory for the team"),
    ("/next/use-cases", "What people run Citadel for,"),
    ("/next/contact", "Tell us what you are building,"),
    ("/next/login", "Open your vault."),
)


def test_every_preview_route_is_served_under_the_strict_policy() -> None:
    """The previews are real pages, under the site's default policy, all five.

    `script-src 'self'` is the line that matters. Next.js's App Router emits its
    render payload as executable inline <script> blocks, which a static export
    cannot nonce (a nonce has to be unique per response, and these files are
    written once, at build time). That is why the app is built on the Pages
    Router, whose payload is a <script type="application/json"> data block the
    browser never executes. If this assertion ever fails, the router changed, or
    something started injecting inline script, and the fix is the markup, never
    the policy.
    """
    client = _client()

    for path, phrase in PREVIEWS:
        response = client.get(path)

        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path
        assert phrase in response.text, f"{path} did not render its own content"

        policy = response.headers["content-security-policy"]
        assert "script-src 'self';" in policy, path
        assert "style-src 'self';" in policy, path
        assert "'unsafe-inline'" not in policy, path
        assert "default-src 'self';" in policy, path
        assert "object-src 'none'" in policy, path

    # No preview path is in the one opt-in list that can relax the policy, and
    # none may join it. The list is empty today; while / held the opt-in, this
    # is exactly where it would have been copied across by reflex.
    assert not any(path.startswith("/next") for path in server_module.CSP_INLINE_STYLE_PATHS)


def test_the_preview_route_set_is_closed() -> None:
    """A path parameter used as a filename is a directory traversal waiting to
    happen, so the route serves an allow-list and nothing else."""
    client = _client()

    for path in ("/next/nope", "/next/..%2f..%2fserver.py", "/next/index"):
        assert client.get(path).status_code == 404, path


def test_the_theme_bootstrap_is_served_as_executable_javascript() -> None:
    """Every exported page loads /next/theme.js from <head>, before paint.

    The site sends X-Content-Type-Options: nosniff, so the browser executes
    this file only if it arrives with a JavaScript media type. Anything else
    (the JSON body of a 404 included) is refused, the bootstrap never runs,
    and a visitor whose remembered choice is dark gets light on every /next
    page while the live pages still honour it.
    """
    client = _client()

    response = client.get("/next/theme.js")

    assert response.status_code == 200
    media_type = response.headers["content-type"].split(";")[0].strip()
    assert media_type in {"text/javascript", "application/javascript"}
    assert response.content == (server_module.WEBUI_DIR / "theme.js").read_bytes()

    # The route is one literal filename, not a pattern: the page allow-list
    # below it stays closed to every other name.
    assert client.get("/next/theme.css").status_code == 404
    assert client.get("/next/landing.js").status_code == 404

    # And every exported document really does depend on it.
    for page in _exported_html():
        assert 'src="/next/theme.js"' in page.read_text(encoding="utf-8"), (
            f"{page.name} does not load the theme bootstrap"
        )


def test_the_landing_preview_ships_its_diagram_as_markup() -> None:
    """The React Flow bundle is fetched when the diagram scrolls into view.
    The export still carries a correct picture (the numbered path plus the
    layered stack) so a phone or a no-JS client is not blank.
    """
    body = _client().get("/next").text

    for step in ("Capture", "Your Node", "Promotion", "Central"):
        assert step in body
    assert "xyflow" not in body


def test_the_landing_architecture_reaches_phones_and_names_what_runs() -> None:
    """Two regressions pinned at once.

    Mobile: the React Flow canvas mounts at every width (fitView, pan off on
    coarse pointers). The layered markup stays in the export so a phone with
    no JavaScript still reads the architecture.

    Content: the layers name what actually runs at head, the stores
    kb/lite_runtime.py configures and the lifecycle wording of kb/lifecycle.py,
    and carry no cadence figure. "hourly" sat on the org sources while the
    shipped default interval was six hours (kb/config.py
    evolve_interval_seconds), so its absence is pinned the way the other
    unrecomputable figures are.
    """
    body = _client().get("/next").text

    layers = re.search(
        r'<div[^>]*aria-label="The architecture, layer by layer"[^>]*>', body
    )
    assert layers is not None, "the layered architecture block is not in the export"
    assert "max-[620px]:hidden" not in layers.group(0)

    spine = re.search(r'<ol[^>]*aria-label="How work reaches the vault"[^>]*>', body)
    assert spine is not None, "the capture-to-read path is not in the export"
    assert "max-[620px]:hidden" not in spine.group(0)

    for fact in (
        "Lifecycle ledger",
        "SQLite · Qdrant · Ladybug",
        "BAAI/bge-small-en-v1.5",
        "evolve pass",
    ):
        assert fact in body, f"the exported architecture no longer names: {fact}"

    # In the source too, not only the last rebuild, mirroring how
    # test_public_page_claims.py treats the other dead figures.
    data = (
        server_module.WEBUI_DIR.parent.parent / "web" / "src" / "components" / "pipeline-data.ts"
    ).read_text(encoding="utf-8")
    for surface in (body, data):
        assert "hourly" not in surface


def test_the_export_carries_no_inline_script_and_no_inline_style() -> None:
    """The property that lets /next send the strict policy, checked at the file.

    A response header is easy to keep strict; what is hard is keeping the HTML
    honest about it. This reads the committed export directly, so a rebuild that
    reintroduces an inline <script>, an inline <style> or a style="" attribute
    fails here rather than in a browser console nobody is watching.
    """
    pages = _exported_html()
    assert pages, "no exported HTML — run `npm run build:web`"

    for page in pages:
        html = page.read_text(encoding="utf-8")

        for match in re.finditer(r"<script([^>]*)>", html):
            attributes = match.group(1)
            if "src=" in attributes:
                continue
            # An inline <script> is only acceptable when it is not script: a
            # non-JavaScript type makes the element a data block that the HTML
            # parser never executes and CSP therefore never applies to.
            assert 'type="application/json"' in attributes, (
                f"{page.name} carries an executable inline script: {attributes.strip()}"
            )

        assert "<style" not in html, f"{page.name} carries an inline <style> element"
        assert not re.search(r'\sstyle="', html), f"{page.name} carries a style attribute"


APP_VIEWS = (
    "/next/app",
    "/next/app/search",
    "/next/app/sources",
    "/next/app/graph",
    "/next/app/review",
    "/next/app/admin",
)


def _seated_client(access_key: str) -> TestClient:
    """A TestClient holding a session cookie at the role that key carries."""
    # `from tests.test_server import ...` does not resolve: tests/ has no
    # __init__.py, so it is not a package. For a non-package test directory
    # pytest prepends the test file's own directory to sys.path, which makes
    # the sibling module importable by its bare name and nothing else.
    from test_server import authed_client

    return authed_client(access_key)


def test_the_dashboard_preview_is_closed_to_anonymous_callers() -> None:
    """Every view behind the same door /app uses.

    The export is a static file, so nothing about the document itself is
    private. The route is what is private, and it has to answer the way /app
    does: a redirect to the sign-in page, not the landing page, because the
    caller asked for the app by name.
    """
    client = TestClient(app, base_url="https://testserver")

    for path in APP_VIEWS:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path


def test_dashboard_views_are_role_gated_at_the_route() -> None:
    """A seat that cannot open a view never receives its markup.

    The dashboard this replaces ships every page's markup to every seat and
    hides what the role cannot use, so a writer really does receive the Admin
    markup and only client-side code keeps it off screen. A static export cannot
    vary its HTML by role at all, so the gate moves to the route: the locked
    page is served in its place, with a 403, and the view's markup never leaves
    the server.
    """
    expected = {
        "test-reader": {
            "/next/app": 200,
            "/next/app/search": 200,
            "/next/app/sources": 200,
            "/next/app/review": 403,
            "/next/app/admin": 403,
        },
        "test-writer": {
            "/next/app": 200,
            "/next/app/search": 200,
            "/next/app/sources": 200,
            "/next/app/review": 200,
            "/next/app/admin": 403,
        },
        "test-admin": {
            "/next/app": 200,
            "/next/app/search": 200,
            "/next/app/sources": 200,
            "/next/app/review": 200,
            "/next/app/admin": 200,
        },
    }

    for access_key, paths in expected.items():
        client = _seated_client(access_key)
        for path, status in paths.items():
            response = client.get(path, follow_redirects=False)
            assert response.status_code == status, f"{access_key} {path}"
            if status == 403:
                assert "not open to your seat" in response.text, f"{access_key} {path}"


def test_no_seat_receives_admin_markup_in_the_document() -> None:
    """The stronger half of the same guarantee, checked at the file.

    The nav's gated entries are rendered after `/api/session` resolves, so the
    Admin entry is in no exported document at any role. A route gate stops a
    writer opening Admin; this stops the link being in the page they were
    served in the first place.
    """
    for page in sorted((server_module.WEBUI_DIR / "app").glob("*.html")) + [
        server_module.WEBUI_DIR / "app.html"
    ]:
        markup = page.read_text(encoding="utf-8")
        assert 'href="/next/app/admin"' not in markup, f"{page.name} ships the Admin nav entry"
        assert 'href="/next/app/review"' not in markup, f"{page.name} ships the Review nav entry"


def test_the_dashboard_view_set_is_closed() -> None:
    client = _seated_client("test-admin")

    for path in ("/next/app/nope", "/next/app/locked", "/next/app/overview"):
        assert client.get(path, follow_redirects=False).status_code == 404, path


def test_graph_view_is_a_real_next_dashboard_route() -> None:
    body = (server_module.WEBUI_DIR / "app" / "graph.html").read_text(encoding="utf-8")
    source = (
        Path(server_module.__file__).resolve().parent.parent
        / "web"
        / "src"
        / "pages"
        / "app"
        / "graph.tsx"
    ).read_text(encoding="utf-8")

    assert "Knowledge graph" in body
    assert "/api/mesh/graph?limit=200" in source
    assert "Caller-scoped graph projection" in body
    assert "visible_nodes" in source
    assert "Presence-only view. No content nodes are visible for this scope." in source
    assert "No content nodes are visible for this scope." in source

    compiled = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (server_module.WEBUI_DIR / "_next/static/chunks/pages/app").glob("graph-*.js")
    )
    assert "visible_nodes" in compiled
    assert "Presence-only view. No content nodes are visible for this scope." in compiled


def test_sources_view_is_read_only_and_uses_separate_health_endpoints() -> None:
    body = (server_module.WEBUI_DIR / "app" / "sources.html").read_text(encoding="utf-8")
    source = (
        Path(server_module.__file__).resolve().parent.parent
        / "web"
        / "src"
        / "pages"
        / "app"
        / "sources.tsx"
    ).read_text(encoding="utf-8")

    assert "Sources and index health" in body
    assert 'useEndpoint<SourcesResponse>("/api/sources")' in source
    assert 'useEndpoint<IndexesResponse>("/api/indexes")' in source
    for mutation in ("/run", "/ingest", "/promote", "/sync/push", 'method: "POST"'):
        assert mutation not in source, f"Sources view contains a mutation endpoint: {mutation!r}"


def test_review_makes_no_claim_the_api_cannot_support() -> None:
    """Two fields the design spec asked for and the API cannot honestly give.

    No secret scan runs over a promotion candidate (contract map gap 7), so the
    row must carry no scan result and must not substitute the `sensitive` flag
    from LLM enrichment, which is a weaker and different claim. And a pending
    item is one candidate note, so there is no document count to show (gap 6).

    Keyed on the rendered words rather than on the absence of a field name,
    because the failure worth catching is a visitor reading a false assurance.
    """
    markup = (server_module.WEBUI_DIR / "app" / "review.html").read_text(encoding="utf-8")

    for claim in ("secret scan", "Secret scan", "scan passed", "sensitive"):
        assert claim not in markup, f"Review renders an unsupported claim: {claim!r}"
    for counted in ("documents in this", "document count"):
        assert counted not in markup, f"Review renders a per-item document count: {counted!r}"


def test_the_frontend_never_navigates_client_side() -> None:
    """`next/link` would break the Content-Security-Policy. This is the guard.

    Next's Pages Router swaps stylesheets on a client-side route change by
    building a <style> element and appending the new page's CSS as a text node.
    A DOM-created <style> is still an inline style as far as CSP is concerned,
    so under `style-src 'self'` the browser drops it and the visitor lands on an
    unstyled page. Next's own answer is a per-response nonce, which a static
    export cannot have.

    So every link on this site is a plain <a>: a full document load, which
    fetches the next page's stylesheet as a <link> the policy allows. That is
    not a stylistic preference, it is the reason the policy holds, and it is one
    convenience import away from being undone.

    Skipped rather than failed when web/ is absent, because an installed wheel
    ships the built export and none of the sources.
    """
    web_src = Path(server_module.__file__).resolve().parent.parent / "web" / "src"
    if not web_src.is_dir():
        pytest.skip("frontend sources are not part of an installed wheel")

    # Matched as code, not as prose: several of these files explain in a comment
    # why they do not use the router, and a guard that its own rationale trips
    # is a guard someone deletes.
    banned = re.compile(r'from\s+"next/(?:link|router|navigation)"|\buseRouter\(|\brouter\.push\(')
    offenders = [
        path.name
        for path in sorted(web_src.rglob("*.tsx"))
        if banned.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"client-side navigation reached {offenders}. See the CSP note in web/README.md: "
        "this needs a nonce the static export cannot produce."
    )


def test_the_info_preview_ships_the_last_published_figures() -> None:
    """/info hydrates from /api/state, and has to read correctly before it does.

    The tiles are stamped with the last published values rather than left blank,
    so a visitor who arrives while the node is unreachable, or with JavaScript
    off, reads something true and slightly old instead of a row of dashes. The
    commit chart is the deliberate exception: its bar heights are computed, and
    a pre-rendered bar would carry a `style` attribute that `style-src 'self'`
    drops, so it is drawn after mount exactly as the hand-written page drew it
    from a deferred script.

    Four counts were dropped from these tiles: commits, decision records, a test
    count and a LOC count. None was evidence the system works and every one of
    them had drifted stale on the page. A dated cost snapshot and a dated search
    round-trip, both traceable to `scripts/bench/`, took two of the slots and the
    row went from eight tiles to six.
    """
    body = _client().get("/next/info").text

    assert "Live · v0.5.0" in body
    assert "Window: v0.2.0 → v0.5.0" in body
    assert "Window: v0.2.0 → v0.4.1" not in body
    assert "~$38/mo" in body
    assert "~$55/mo" not in body
    assert "269 ms" in body
    assert "commits on main" not in body
    assert "architecture decision records" not in body
    assert "tests across 52 files" not in body
    assert "53 modules" not in body
    assert "Live tiles pull from" in body


def test_the_contact_preview_keeps_the_honeypot_and_posts_nowhere_else() -> None:
    """The bot trap is part of the endpoint's contract, not decoration.

    A human never sees the field, so anything in it marks a bot and the server
    answers 200 regardless, which is what stops a bot learning it was filtered.
    It has to be in the served markup for any of that to happen.
    """
    body = _client().get("/next/contact").text

    assert 'name="website"' in body
    assert 'id="cf-web"' in body
    # Off-screen, not display:none, which some bots skip.
    assert "-left-[9999px]" in body


def test_next_assets_are_served_and_cached_as_immutable_content() -> None:
    """The chunks are content-hashed, so they are safe to cache in the open."""
    index = (server_module.WEBUI_DIR / "index.html").read_text(encoding="utf-8")
    chunk = re.search(r'src="(/next/_next/static/chunks/[^"]+\.js)"', index)
    assert chunk, "the exported document references no chunk under /next/_next/"

    response = _client().get(chunk.group(1))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]


def test_canonical_public_routes_serve_the_built_export() -> None:
    """Built public pages are served byte-for-byte from the committed export."""
    client = _client()

    for path, source in (
        ("/", server_module.WEBUI_DIR / "index.html"),
        ("/info", server_module.WEBUI_DIR / "info.html"),
        ("/use-cases", server_module.WEBUI_DIR / "use-cases.html"),
        ("/contact", server_module.WEBUI_DIR / "contact.html"),
        ("/login", server_module.WEBUI_DIR / "login.html"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content == source.read_bytes(), path
        assert response.headers["content-type"].startswith("text/html"), path


def test_public_routes_fall_back_to_hand_written_pages_without_an_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A source checkout without `build:web` keeps every public route usable."""
    monkeypatch.setattr(server_module, "WEBUI_DIR", tmp_path)
    client = _client()
    static = server_module.STATIC_DIR

    for path, source in (
        ("/", static / "landing.html"),
        ("/info", static / "info.html"),
        ("/use-cases", static / "use-cases.html"),
        ("/contact", static / "contact.html"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content == source.read_bytes(), path

    login = client.get("/login")
    assert login.status_code == 200
    assert login.text == server_module.LOGIN_HTML
