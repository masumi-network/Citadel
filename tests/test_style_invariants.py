"""Invariants for the classes of visual bug that have actually shipped here.

Three landed on the live site and each was found by a human noticing, one at a
time: the static pipeline diagram rendering on top of the interactive one, the
final letter of a gradient headline shaved off, and a sentence's links dropping
onto their own lines. All three were one instance of a class, and each was fixed
as one instance, which leaves the class intact for the next element.

These tests are per class, not per instance. Each one is a property of every
element on every surface: the hand-written pages under `kb/static/`, the login
page generated inside `kb/server.py`, and the Next.js export in `kb/webui/`.
"""

from __future__ import annotations

from html.parser import HTMLParser
import re
from pathlib import Path

import pytest

import kb.server as server_module


REPO = Path(server_module.__file__).resolve().parent.parent
STATIC = REPO / "kb" / "static"
WEBUI = REPO / "kb" / "webui"

# Void elements never have children, so they never open a scope.
VOID = {"br", "img", "input", "hr", "meta", "link", "source", "area", "col", "embed", "wbr"}


def exported_pages() -> list[Path]:
    return sorted(WEBUI.rglob("*.html"))


def served_documents() -> dict[str, str]:
    """Every HTML document this service can put in front of a browser.

    Keys carry their directory. Both surfaces have a `contact.html`, and keying
    on the bare filename silently dropped the hand-written one from every check
    that used this.
    """
    pages = {
        f"kb/static/{p.name}": p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.html"))
    }
    pages["kb/server.py:LOGIN_HTML"] = server_module.LOGIN_HTML
    pages.update(
        {
            f"kb/webui/{p.relative_to(WEBUI)}": p.read_text(encoding="utf-8")
            for p in exported_pages()
        }
    )
    return pages


# --------------------------------------------------------------------------
# class 1 — the `hidden` attribute must beat any author `display`
# --------------------------------------------------------------------------


def test_every_stylesheet_makes_the_hidden_attribute_win() -> None:
    """`[hidden]` is a user-agent rule, so any author `display` outranks it.

    `.spine { display: flex }` did exactly that. landing.js hid the static
    diagram by setting `hidden` when React Flow mounted over it, the flex rule
    won, and both diagrams rendered at once on the live site for hours. The
    original fix was a `.spine[hidden]` rule, which fixes one element and leaves
    the trap set for every element that gets a display value next.

    So the invariant is per stylesheet, not per element: each one carries a
    global rule. `styles.css` always had it, which is why the dashboard never
    had this bug, and Tailwind's preflight ships it for the export.
    """
    sheets = {
        "kb/static/info.css": (STATIC / "info.css").read_text(encoding="utf-8"),
        "kb/static/styles.css": (STATIC / "styles.css").read_text(encoding="utf-8"),
    }
    # For the export, the sheet that matters is the one every document links,
    # not every chunk: React Flow's own stylesheet is loaded on demand and is
    # vendor code, so requiring the rule of it would be requiring it of a file
    # we do not write.
    linked = set()
    for page in exported_pages():
        linked.update(re.findall(r'<link rel="stylesheet" href="/next(/[^"]+\.css)"', page.read_text(encoding="utf-8")))
    assert linked, "no exported document links a stylesheet"
    for href in sorted(linked):
        css = WEBUI / href.lstrip("/")
        sheets[f"kb/webui{href}"] = css.read_text(encoding="utf-8")

    # A global rule: `[hidden]` as its own selector, not qualified by a class.
    guard = re.compile(r"(?<![\w.\-\]])\[hidden\][^{,]*\{[^}]*display\s*:\s*none")

    for name, css in sheets.items():
        assert guard.search(css), (
            f"{name} has no global [hidden] rule. Any element given a display "
            "value can now ignore the hidden attribute, which renders it and "
            "whatever replaced it at the same time."
        )


# --------------------------------------------------------------------------
# class 2 — background-clip: text shaves overhanging ink
# --------------------------------------------------------------------------


def test_gradient_text_pads_the_painted_box() -> None:
    """`background-clip: text` clips to the inline box, which is tight to the
    glyphs, so any letter whose ink overhangs its advance width gets shaved on
    the right. It showed up as a cut-off `r` in "and where we partner".

    The fix is horizontal padding on the painted box, pulled back out of the
    layout with an equal negative margin so nothing moves.
    """
    sheets = [STATIC / "info.css", REPO / "web" / "src" / "styles" / "globals.css"]
    checked = 0

    for sheet in sheets:
        if not sheet.is_file():
            continue
        css = sheet.read_text(encoding="utf-8")
        for rule in re.finditer(r"([^{}]*)\{([^}]*)\}", css):
            body = rule.group(2)
            if not re.search(r"background-clip\s*:\s*text", body):
                continue
            checked += 1
            selector = rule.group(1).strip().splitlines()[-1].strip()
            assert re.search(r"padding\s*:\s*0? *\.?\d", body), (
                f"{sheet.name} `{selector}` clips a background to text without "
                "horizontal padding, so an overhanging letter will be shaved."
            )

    assert checked, "no background-clip: text rule found — did the selector move?"


# --------------------------------------------------------------------------
# class 3 — a flex container turns each run of content into its own item
# --------------------------------------------------------------------------


class _MixedContent(HTMLParser):
    """Finds flex/grid containers holding BOTH a bare text node and an element.

    That combination is the bug: the text becomes an anonymous flex item and the
    element becomes another, so the gap opens between them and they can wrap
    onto separate lines. `.verified` was `display: flex` around a sentence with
    links in it, and the links dropped onto their own lines.

    Element-only children (a row of chips) and text-only content are both fine.
    """

    FLEXY = re.compile(r"(?:^|[\s:])(?:flex|inline-flex|grid|inline-grid)(?:$|\s)")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[list] = []
        self.hits: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in VOID:
            return
        classes = dict(attrs).get("class", "")
        if self.stack:
            self.stack[-1][3] = True
        self.stack.append([tag, bool(self.FLEXY.search(classes)), False, False, classes])

    def handle_endtag(self, tag: str) -> None:
        while self.stack:
            frame = self.stack.pop()
            if frame[1] and frame[2] and frame[3]:
                self.hits.append((frame[0], frame[4]))
            if frame[0] == tag:
                break

    def handle_data(self, data: str) -> None:
        if data.strip() and self.stack:
            self.stack[-1][2] = True


def test_no_flex_container_mixes_bare_text_with_an_element() -> None:
    """Checked on the export, where the display value is in the class name.

    The hand-written pages take their display from `info.css` rules rather than
    from class names, so this parser cannot see them; that side was reviewed by
    hand and `.verified` was the only instance.
    """
    pages = exported_pages()
    assert pages, "no exported HTML — run `npm run build:web`"

    offenders = []
    for page in pages:
        scan = _MixedContent()
        scan.feed(page.read_text(encoding="utf-8"))
        scan.close()
        offenders += [(page.name, tag, classes) for tag, classes in scan.hits]

    assert not offenders, (
        "a flex or grid container holds a bare text node next to an element, so "
        "each becomes its own flex item and the gap opens between them: "
        f"{offenders[:5]}. Wrap the text in a span."
    )


# --------------------------------------------------------------------------
# class 4 — ids
# --------------------------------------------------------------------------


def test_no_document_repeats_an_id() -> None:
    """A duplicate id makes `getElementById` return the first match forever, so
    the second element silently stops being reachable."""
    for name, markup in served_documents().items():
        ids = re.findall(r'\bid="([^"]+)"', markup)
        duplicated = {found for found in ids if ids.count(found) > 1}
        assert not duplicated, f"{name} repeats {sorted(duplicated)}"


def test_no_public_script_reaches_for_an_id_that_exists_nowhere() -> None:
    """A dead id reference is markup that was renamed or deleted while the code
    reading it stayed behind.

    This deliberately checks existence *somewhere*, not on every page. The
    public scripts are shared across pages that do not carry the same markup on
    purpose: info.js draws a chart only /info has, landing.js upgrades a diagram
    only / has, and every one of those lookups is guarded. What is never
    intentional is an id no document contains at all, because then the guard is
    permanently false and the code behind it is dead.
    """
    markup = "\n".join(served_documents().values())
    present = set(re.findall(r'\bid="([^"]+)"', markup))

    for script in sorted(STATIC.glob("*.js")):
        if script.name == "app.js":
            continue  # the dashboard's own bundle, and not this port's surface
        source = script.read_text(encoding="utf-8")
        wanted = set(re.findall(r'getElementById\("([^"]+)"\)', source))
        dead = sorted(wanted - present)
        assert not dead, (
            f"{script.name} looks up {dead}, which no served document contains. "
            "Either the markup was renamed and the script was not, or this is "
            "dead code behind a guard that can never be true."
        )


# --------------------------------------------------------------------------
# class 5 — absolutely positioned decoration escaping its container
# --------------------------------------------------------------------------


class _AbsoluteElements(HTMLParser):
    POSITIONED = re.compile(r"(?:^|\s)(?:relative|absolute|fixed|sticky)(?:$|\s)")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.hits: list[tuple[str, str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in VOID:
            return
        classes = dict(attrs).get("class", "")
        names = classes.split()
        if "absolute" in names or "hero-glow" in names:
            ancestor = next(
                (cls for _, cls in reversed(self.stack) if self.POSITIONED.search(cls)), None
            )
            self.hits.append((tag, classes, ancestor))
        self.stack.append((tag, classes))

    def handle_endtag(self, tag: str) -> None:
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


def test_the_hero_glow_cannot_escape_its_band() -> None:
    """The glow is an 840px circle anchored off-canvas at `top: -300px;
    right: -190px`, so only its falloff is meant to be on screen. If its band
    stops clipping, the blob extends past the right edge and the whole page
    gains a horizontal scrollbar."""
    for page in exported_pages():
        scan = _AbsoluteElements()
        scan.feed(page.read_text(encoding="utf-8"))
        scan.close()
        for tag, classes, ancestor in scan.hits:
            if "hero-glow" not in classes.split():
                continue
            assert ancestor is not None, f"{page.name}: the glow has no positioned ancestor"
            assert "overflow-hidden" in ancestor, (
                f"{page.name}: the glow's container does not clip "
                f"(class={ancestor!r}), so it will overflow the viewport."
            )

    # The hand-written pages put the glow in `.band.band-hero`, which clips in
    # the stylesheet rather than in a class name.
    info_css = (STATIC / "info.css").read_text(encoding="utf-8")
    band_hero = re.search(r"\.band-hero\s*\{([^}]*)\}", info_css)
    assert band_hero and "overflow: hidden" in band_hero.group(1), (
        "info.css .band-hero no longer clips, so the hero glow will overflow "
        "the viewport on every hand-written page."
    )


def test_absolute_elements_position_against_something_deliberate() -> None:
    """An absolutely positioned element with no positioned ancestor resolves
    against the viewport, which is almost never what the author meant.

    The one deliberate exception is the contact form's honeypot, which is parked
    far off-screen to the left on purpose. Leftward overflow does not extend the
    scrollable area in a left-to-right document, and it carries its own
    `overflow-hidden`.
    """
    for page in exported_pages():
        scan = _AbsoluteElements()
        scan.feed(page.read_text(encoding="utf-8"))
        scan.close()
        for tag, classes, ancestor in scan.hits:
            if ancestor is not None or "hero-glow" in classes.split():
                continue
            assert "-left-[9999px]" in classes, (
                f"{page.name}: <{tag} class={classes!r}> is absolutely positioned "
                "with no positioned ancestor, so it resolves against the viewport."
            )


# --------------------------------------------------------------------------
# class 6 — a hard-coded colour cannot follow the theme
# --------------------------------------------------------------------------


def test_the_port_takes_every_colour_from_a_token() -> None:
    """A hex literal in a component is a colour that cannot invert, and the
    failure is silent: it looks right in the theme it was written in.

    `#0a0a0a` on the accent fill was the real case. It is genuinely
    theme-invariant, because `--accent` is the same magenta in both themes, but
    writing the hex inline hides that pairing from anyone changing the accent.
    It is now the `--on-accent` token, declared in both theme blocks.
    """
    components = REPO / "web" / "src"
    if not components.is_dir():
        pytest.skip("frontend sources are not part of an installed wheel")

    offenders = []
    for path in sorted(components.rglob("*.tsx")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", line):
                offenders.append(f"{path.name}:{number}")

    assert not offenders, (
        f"a colour is hard-coded instead of coming from a token: {offenders}. "
        "Add it to the token set in web/src/styles/globals.css and reference it."
    )
