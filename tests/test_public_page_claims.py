"""What the public pages promise, pinned so a page cannot quietly stop keeping it.

Everything here is about the five surfaces a visitor can reach without a seat:
the four hand-written pages under `kb/static/` and the login page generated
inside `kb/server.py`. The Next.js port under `web/src/` is checked too wherever
the claim is a link, because a broken link there ships the moment the bundle is
rebuilt and nothing errors when it does.

Two classes of bug live here, and both have happened on this site:

* A page states a number nobody can recompute. `900` tests, `906 tests across 52
  files` and a `300 to 500 ms` search range were all on the page at once while
  the suite collected a different count and the harness in this repo recorded a
  different latency. The fix was to delete them, so the tests below pin their
  absence, not a replacement figure.
* A link points at a heading that exists today and is renamed tomorrow. A URL
  fragment against a repo that no longer has that heading scrolls nowhere and
  returns 200, so no monitor and no browser reports it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import kb.server as server_module


REPO = Path(server_module.__file__).resolve().parent.parent
STATIC = REPO / "kb" / "static"
WEB_SRC = REPO / "web" / "src"

REPO_URL = "https://github.com/masumi-network/Citadel"


def public_surfaces() -> dict[str, str]:
    """Every document that carries the shared public top nav.

    Membership is derived from the nav itself rather than listed, so a page
    added later is covered the day it is written. `kb/static/index.html` is
    excluded by that derivation and should be: it is the signed-in dashboard
    shell, which has a sidebar and no top nav.
    """
    pages = {
        f"kb/static/{p.name}": text
        for p in sorted(STATIC.glob("*.html"))
        if 'class="topnav"' in (text := p.read_text(encoding="utf-8"))
    }
    pages["kb/server.py:LOGIN_HTML"] = server_module.LOGIN_HTML
    return pages


def test_the_public_surfaces_are_the_ones_we_think_they_are() -> None:
    """Guards every check below from passing over a shrunken set."""
    assert set(public_surfaces()) >= {
        "kb/static/landing.html",
        "kb/static/info.html",
        "kb/static/use-cases.html",
        "kb/static/contact.html",
        "kb/server.py:LOGIN_HTML",
    }


def authored_sources() -> dict[str, str]:
    """Every hand-written source for either frontend.

    `kb/webui/` is excluded on purpose: it is build output, regenerated once
    from the merged tree, and checking it here would report the state of the
    last rebuild rather than the state of the source.
    """
    pages = public_surfaces()
    pages.update(
        {
            f"web/src/{p.relative_to(WEB_SRC)}": p.read_text(encoding="utf-8")
            for p in sorted(WEB_SRC.rglob("*.tsx"))
        }
    )
    return pages


# --------------------------------------------------------------------------
# the repo link in the nav
# --------------------------------------------------------------------------


def test_every_public_surface_links_the_repository() -> None:
    """All five surfaces carry the GitHub link, or none of them should.

    A nav that differs between pages is the bug this catches: the link was added
    to four hand-written pages and the generated login page separately, which is
    five chances to miss one.
    """
    missing = [
        name
        for name, body in public_surfaces().items()
        if f'class="navicon" href="{REPO_URL}"' not in body
    ]
    assert missing == [], f"no repository link in the nav on: {missing}"


def test_the_navigation_repo_link_is_labelled_and_safe_to_open() -> None:
    """The link is an icon, so it needs a name; it opens a new tab, so it needs
    `rel=noopener`."""
    for name, body in public_surfaces().items():
        anchor = re.search(r'<a class="navicon"[^>]*>', body)
        assert anchor is not None, f"{name} has no .navicon anchor"
        tag = anchor.group(0)
        assert 'aria-label="GitHub repository"' in tag, name
        assert 'rel="noopener noreferrer"' in tag, name


# --------------------------------------------------------------------------
# the contact page's second route in
# --------------------------------------------------------------------------


def test_the_contact_page_offers_an_email_route() -> None:
    """The form relays into a team chat and has been down in production before.

    An address that only exists in the form's success path is not a route in
    when the form is the thing that is broken.
    """
    body = (STATIC / "contact.html").read_text(encoding="utf-8")
    assert 'href="mailto:' in body, "no mailto route on /contact"
    assert "sarthi.borkar@nmkr.io" in body


# --------------------------------------------------------------------------
# the proof tiles
# --------------------------------------------------------------------------


def test_the_landing_proof_tiles_say_what_they_measure() -> None:
    body = (STATIC / "landing.html").read_text(encoding="utf-8")
    for label in (
        "Open source, self-hosted",
        "Median search round-trip, from a client",
        "MCP tools for agents",
        "To self-host the whole node",
    ):
        assert label in body, f"missing landing tile: {label}"
    assert "~$55/mo" in body
    assert "269 ms" in body


def test_the_info_tiles_say_when_each_figure_was_taken() -> None:
    body = (STATIC / "info.html").read_text(encoding="utf-8")
    assert "to self-host, measured 2026-07-31" in body
    assert "median search round-trip, from a client" in body
    assert "~$55/mo" in body
    assert "269 ms" in body


@pytest.mark.parametrize(
    "figure",
    [
        # Recomputed on this branch: the suite collects far more than either of
        # these, and both were on the page in two different versions.
        ">900<",
        ">906<",
        "tests across 52 files",
        "53 modules",
        # Never came from a run this repo can perform. The per-surface p50s
        # behind it were one-off probe scripts, and scripts/bench records a
        # different figure entirely.
        "300 to 500",
        "300&ndash;500ms",
    ],
)
def test_no_public_page_carries_a_figure_this_repo_cannot_recompute(figure: str) -> None:
    guilty = [name for name, body in authored_sources().items() if figure in body]
    assert guilty == [], f"{figure!r} is back on: {guilty}"


def test_no_page_calls_the_cost_snapshot_reproducible() -> None:
    """`cost_model.py` holds a day's Railway averages as a module constant.

    Re-running it therefore reprints the same total by construction and can
    never detect drift, so "reproducible on demand" described the wrong half of
    the harness. The latency figure is genuinely re-measurable; the cost is not.
    """
    for name, body in authored_sources().items():
        assert "reproducible on demand" not in body, name


# --------------------------------------------------------------------------
# outbound links into our own repository
# --------------------------------------------------------------------------


def heading_slugs(markdown: Path) -> set[str]:
    """GitHub's anchor slug for every ATX heading in a markdown file.

    Lowercase, punctuation dropped, spaces to hyphens. Enough for the headings
    this repo actually writes; a heading with a duplicate slug would get a `-1`
    suffix on GitHub and is not modelled, which only ever makes this stricter.
    """
    slugs = set()
    for line in markdown.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if not match:
            continue
        text = match.group(1).lower()
        text = re.sub(r"[^\w\s-]", "", text)
        slugs.add(re.sub(r"\s+", "-", text.strip()))
    return slugs


def repo_links() -> list[tuple[str, str]]:
    pattern = re.compile(re.escape(REPO_URL) + r"[^\s\"'<>)]*")
    found = []
    for name, body in authored_sources().items():
        for url in pattern.findall(body):
            found.append((name, url))
    return found


def test_the_pages_do_link_the_repository_at_all() -> None:
    """Guards the check below from passing over an empty list."""
    assert len(repo_links()) >= 5


def test_every_link_into_our_repo_resolves_to_something_committed() -> None:
    """A path we link must exist here, and a fragment must be a real heading.

    GitHub serves a 200 and an unscrolled page for a fragment that matches no
    heading, so a renamed section breaks the link silently. `#self-host-the-
    server` was such a link: valid against README.md as written, and dead the
    moment the section was retitled.
    """
    broken = []
    for name, url in repo_links():
        tail = url[len(REPO_URL) :]
        path, _, fragment = tail.partition("#")

        if path in ("", "/", "/issues"):
            target = REPO / "README.md"
        elif match := re.match(r"^/(?:tree|blob)/main/(.+)$", path):
            target = REPO / match.group(1)
            if not target.exists():
                broken.append(f"{name}: {url} (no such path in the repo)")
                continue
        else:
            broken.append(f"{name}: {url} (unrecognised repo URL shape)")
            continue

        if not fragment:
            continue
        if target.suffix != ".md":
            broken.append(f"{name}: {url} (fragment on a non-markdown target)")
        elif fragment not in heading_slugs(target):
            broken.append(f"{name}: {url} (no heading with that slug in {target.name})")

    assert broken == [], "links into our own repo that go nowhere:\n" + "\n".join(broken)
