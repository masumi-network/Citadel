from __future__ import annotations

import io

from kb.banner import (
    PIXEL_FLAGS,
    WINDOW_INDICES,
    banner,
    banner_large,
    paint,
    pixel_cells,
    supports_color,
    tagline,
)


def test_tagline_is_brand_highlighted(monkeypatch) -> None:
    assert tagline(color=False) == "the organization vault"  # plain when piped
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert "\033[38;2;250;0;140m" in tagline(color=True)  # brand magenta
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert "\033[38;5;" in tagline(color=True)  # 256-color approximation
    monkeypatch.setenv("TERM", "xterm")
    assert "\033[36m" in tagline(color=True)  # cyan fallback


def test_banner_large_is_the_brand_wordmark(monkeypatch) -> None:
    plain = banner_large(color=False)
    assert "____" in plain        # figlet CITADEL
    assert "\033[" not in plain   # plain when piped
    assert len(plain.splitlines()) == 5  # just the wordmark, nothing else

    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    basic = banner_large(color=True)
    assert "\033[1m" in basic and "\033[36m" in basic  # bold cyan fallback

    monkeypatch.setenv("TERM", "xterm-256color")
    approx = banner_large(color=True)
    assert "\033[38;5;" in approx  # 256-color gradient approximation

    monkeypatch.setenv("COLORTERM", "truecolor")
    gradient = banner_large(color=True)
    assert "\033[38;2;250;0;140m" in gradient  # starts at brand magenta #FA008C
    assert "\033[38;2;34;211;238m" in gradient  # ends at brand cyan


def test_banner_plain_has_wordmark_and_no_ansi() -> None:
    out = banner(color=False)
    assert "CITADEL" in out
    assert "the organization vault" in out
    assert "██" in out  # Pixel Bastion lit cells
    assert len(out.splitlines()) == 7
    assert "\033[" not in out  # no ANSI when color off


def test_banner_color_has_ansi(monkeypatch) -> None:
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    out = banner(color=True)
    assert "\033[36m" in out  # cyan pixels (basic fallback)
    assert "\033[1m" in out  # bold wordmark
    assert "\033[0m" in out  # reset


def test_banner_truecolor_uses_column_gradient(monkeypatch) -> None:
    monkeypatch.setenv("COLORTERM", "truecolor")
    out = banner(color=True)
    assert "\033[38;2;255;81;255m" in out  # first column Iris
    assert "\033[38;2;176;10;144m" in out  # last column deep Iris


def test_pixel_cells_match_canonical_grid() -> None:
    cells = pixel_cells()
    assert len(cells) == 49
    # The window cells sit inside the solid keep, so they are lit in the mask
    # itself; blanking them is the blink's off frame, not a missing bit.
    assert sum(1 for c in cells if c["lit"]) == sum(PIXEL_FLAGS)
    assert cells[0]["color"] == "#FF51FF"
    assert cells[6]["color"] == "#B00A90"
    blank = pixel_cells(blank_windows=True)
    assert sum(1 for c in blank if c["lit"]) == sum(PIXEL_FLAGS) - len(WINDOW_INDICES)
    assert all(not c["lit"] for c in blank if c["window"])


def test_cli_mark_matches_the_shipped_web_mark() -> None:
    """The CLI, the favicon, and the web nav must draw one fortress.

    The web copy drifted from banner.py once already (windows dropped, gate
    narrowed) and nothing caught it, because no test compared the two. This
    pins the bitmask that kb/static/favicon.svg and info.js render.
    """
    shipped = (
        "1010101"
        "1111111"
        "1111111"
        "1111111"
        "1101011"
        "1101011"
        "1101011"
    )
    assert "".join(str(flag) for flag in PIXEL_FLAGS) == shipped


def test_paint_passthrough_when_disabled() -> None:
    assert paint("x", "green", enable=False) == "x"
    assert paint("x", enable=True) == "x"  # no styles
    assert paint("x", "green", enable=True) == "\033[32mx\033[0m"


def test_supports_color_false_for_non_tty(monkeypatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert supports_color(io.StringIO()) is False


def test_supports_color_respects_no_color(monkeypatch) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert supports_color(_Tty()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert supports_color(_Tty()) is False


def test_supports_color_ignores_force_color_zero(monkeypatch) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FORCE_COLOR", "0")
    assert supports_color(io.StringIO()) is False
    assert supports_color(_Tty()) is True
