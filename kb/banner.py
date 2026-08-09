"""Citadel CLI branding — Pixel Bastion mark + TTY-aware ANSI color.

Stdlib only. The banner and color are shown only on a real terminal (and never
in `--json` output), so piped/headless usage stays clean and parseable.

The mark is Pixel Bastion (brand.md): a 7×7 crenellated fortress painted in the
magenta→cyan brand gradient. The bare `citadel` home shows Pixel Bastion
(cascade on wide TTY+color) with CITADEL + tagline beside the mark; compact
`banner()` is also the in-command header and the narrow-terminal home.
"""

from __future__ import annotations

import os
import sys
import time
from typing import IO, Iterator

# Pixel Bastion — 7×7 bitmask (1 = lit). Rows: battlements, solid keep ×3,
# gate shaft ×3. This is the mark as it ships on the web nav, the favicon, and
# the brand SVGs; the CLI draws the same one so there is a single fortress.
PIXEL_SIZE = 7
PIXEL_FLAGS: tuple[int, ...] = (
    1,
    0,
    1,
    0,
    1,
    0,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    0,
    1,
    0,
    1,
    1,
    1,
    1,
    0,
    1,
    0,
    1,
    1,
    1,
    1,
    0,
    1,
    0,
    1,
    1,
)
# Iris Flower magenta stepped to its deep stop. The web mark runs the same two
# stops as a per-cell gradient, which a terminal cannot do, so the ramp is laid
# across the columns instead.
PIXEL_COLS_HEX: tuple[str, ...] = (
    "#FF51FF",
    "#F142F0",
    "#E434E0",
    "#D726D0",
    "#CA18C0",
    "#C010A0",
    "#B00A90",
)
# Interior cells on rows 2–3 — blink for idle ceremony.
WINDOW_INDICES: tuple[int, ...] = (16, 18, 23, 25)

# Right-side labels keyed by pixel-mark row.
_LABELS: dict[int, tuple[str, tuple[str, ...]]] = {
    2: ("CITADEL", ("bold", "cyan")),
    3: ("the organization vault", ("dim",)),
}

# Large hero — figlet "CITADEL" wordmark in brand colors. Available via
# `banner_large()` for callers that want the ASCII wordmark; bare `citadel`
# home uses Pixel Bastion only (compact `banner()` / cascade).
_WORDMARK = (
    "  ____  ___  _____     _     ____   _____  _     ",
    " / ___||_ _||_   _|   / \\   |  _ \\ | ____|| |    ",
    "| |     | |   | |    / _ \\  | | | ||  _|  | |    ",
    "| |___  | |   | |   / ___ \\ | |_| || |___ | |___ ",
    " \\____||___|  |_|  /_/   \\_\\|____/ |_____||_____|",
)
_HERO_INDENT = "  "
# Brand anchors (see brand.md): Masumi magenta #FA008C fading into cyan.
_BRAND_MAGENTA = (250, 0, 140)
_BRAND_CYAN = (34, 211, 238)

# Widest hero line — home falls back to the compact banner when narrower.
HERO_WIDTH = len(_HERO_INDENT) + max(len(line) for line in _WORDMARK)

_CELL_ON = "██"
_CELL_OFF = "  "


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


PIXEL_COLS_RGB: tuple[tuple[int, int, int], ...] = tuple(_hex_to_rgb(c) for c in PIXEL_COLS_HEX)


def supports_truecolor() -> bool:
    """24-bit color support, per the de-facto COLORTERM convention."""
    return os.getenv("COLORTERM", "").lower() in ("truecolor", "24bit")


def _lerp_rgb(
    start: tuple[int, int, int], end: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return tuple(round(s + (e - s) * t) for s, e in zip(start, end))


def _ansi256(rgb: tuple[int, int, int]) -> int:
    """Nearest xterm-256 color-cube index for an RGB triple."""
    r, g, b = (round(c / 255 * 5) for c in rgb)
    return 16 + 36 * r + 6 * g + b


def _fg_code(rgb: tuple[int, int, int], *, truecolor: bool) -> str:
    if truecolor:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"\033[38;5;{_ansi256(rgb)}m"


def _gradient_line(line: str, *, width: int, truecolor: bool) -> str:
    """Paint one wordmark row with the brand gradient, column by column."""
    out: list[str] = [_ANSI["bold"]]
    for column, char in enumerate(line):
        if char != " ":
            rgb = _lerp_rgb(_BRAND_MAGENTA, _BRAND_CYAN, column / max(width - 1, 1))
            out.append(_fg_code(rgb, truecolor=truecolor))
        out.append(char)
    out.append(_ANSI["reset"])
    return "".join(out)


_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
}

# Semantic status glyphs — one shared vocabulary across home/status/onboard.
OK = "✓"
FAIL = "✗"
WARN = "!"
SKIP = "⊘"
BULLET = "•"


def supports_color(stream: IO[str] | None = None) -> bool:
    """True on a real TTY (or when forced) that hasn't opted out.

    Opt-outs (NO_COLOR / TERM=dumb) win over force flags. FORCE_COLOR /
    CLICOLOR_FORCE then enable color even when piped (e.g. `… | less -R`).
    Empty / ``0`` / ``false`` force values are ignored (Cursor sets
    ``FORCE_COLOR=0`` in some agent shells).
    """
    stream = stream if stream is not None else sys.stdout
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("TERM") == "dumb":
        return False
    if _env_force_color():
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _env_force_color() -> bool:
    for name in ("FORCE_COLOR", "CLICOLOR_FORCE"):
        raw = os.getenv(name)
        if raw is None:
            continue
        if raw.strip().lower() in {"", "0", "false", "no", "off"}:
            continue
        return True
    return False


def paint(text: str, *styles: str, enable: bool = True) -> str:
    """Wrap text in ANSI styles when enabled, e.g. paint('●', 'green')."""
    if not enable or not styles:
        return text
    codes = "".join(_ANSI.get(style, "") for style in styles)
    return f"{codes}{text}{_ANSI['reset']}" if codes else text


def glyph(ok: bool) -> str:
    """The bare pass/fail glyph (✓ / ✗) — shape carries the signal sans color."""
    return OK if ok else FAIL


def mark(ok: bool, *, enable: bool = True) -> str:
    """The status glyph painted green (pass) or red (fail)."""
    return paint(glyph(ok), "green" if ok else "red", enable=enable)


def pixel_cells(
    *,
    blank_windows: bool = False,
) -> list[dict[str, object]]:
    """49 cells for web/SVG consumers: `{lit, color, row, col, index}`.

    Window cells are lit (eyes open) unless ``blank_windows`` — matches the Bot
    idle blink; the static bitmask alone leaves those cells empty.
    """
    cells: list[dict[str, object]] = []
    for index, flag in enumerate(PIXEL_FLAGS):
        row, col = divmod(index, PIXEL_SIZE)
        is_window = index in WINDOW_INDICES
        lit = (bool(flag) or is_window) and not (blank_windows and is_window)
        cells.append(
            {
                "index": index,
                "row": row,
                "col": col,
                "lit": lit,
                "color": PIXEL_COLS_HEX[col] if lit else "transparent",
                "window": is_window,
            }
        )
    return cells


def _paint_cell(rgb: tuple[int, int, int], *, color: bool, gradient: bool, truecolor: bool) -> str:
    if not color:
        return _CELL_ON
    if gradient:
        return f"{_fg_code(rgb, truecolor=truecolor)}{_CELL_ON}{_ANSI['reset']}"
    return paint(_CELL_ON, "cyan", enable=True)


def render_pixel_mark(
    *,
    color: bool = True,
    blank_windows: bool = False,
    cascade_upto: tuple[int, int] | None = None,
) -> list[str]:
    """Render Pixel Bastion as 7 lines of double-width cells (no labels).

    ``cascade_upto`` is ``(row, col)`` inclusive — cells after that render empty
    (for left→right cascade animation). Window cells are lit unless blanked.
    """
    truecolor = supports_truecolor()
    gradient = truecolor or "256color" in os.getenv("TERM", "")
    lines: list[str] = []
    for row in range(PIXEL_SIZE):
        parts: list[str] = ["  "]  # indent
        for col in range(PIXEL_SIZE):
            index = row * PIXEL_SIZE + col
            if cascade_upto is not None:
                cr, cc = cascade_upto
                if row > cr or (row == cr and col > cc):
                    parts.append(_CELL_OFF)
                    continue
            is_window = index in WINDOW_INDICES
            lit = (bool(PIXEL_FLAGS[index]) or is_window) and not (blank_windows and is_window)
            if lit:
                parts.append(
                    _paint_cell(
                        PIXEL_COLS_RGB[col],
                        color=color,
                        gradient=gradient,
                        truecolor=truecolor,
                    )
                )
            else:
                parts.append(_CELL_OFF)
        lines.append("".join(parts))
    return lines


def _with_labels(mark_lines: list[str], *, color: bool) -> str:
    out: list[str] = []
    for index, line in enumerate(mark_lines):
        rendered = line
        if index in _LABELS:
            text, styles = _LABELS[index]
            rendered += "   " + paint(text, *styles, enable=color)
        out.append(rendered)
    return "\n".join(out)


def tagline(*, color: bool = True) -> str:
    """The brand tagline under the hero — highlighted in brand magenta on
    capable terminals (256-color approximation below truecolor), cyan on
    basic ones, plain when piped."""
    text = "the organization vault"
    if not color:
        return text
    r, g, b = _BRAND_MAGENTA
    if supports_truecolor():
        return f"\033[38;2;{r};{g};{b}m{text}{_ANSI['reset']}"
    if "256color" in os.getenv("TERM", ""):
        return f"\033[38;5;{_ansi256(_BRAND_MAGENTA)}m{text}{_ANSI['reset']}"
    return paint(text, "cyan")


def banner_large(*, color: bool = True) -> str:
    """The big hero: the CITADEL wordmark in brand colors — a magenta→cyan
    gradient (24-bit, or the xterm-256 approximation), bold cyan on basic
    terminals, plain when piped."""
    width = max(len(line) for line in _WORDMARK)
    truecolor = supports_truecolor()
    gradient = truecolor or "256color" in os.getenv("TERM", "")
    out: list[str] = []
    for line in _WORDMARK:
        if color and gradient:
            out.append(_HERO_INDENT + _gradient_line(line, width=width, truecolor=truecolor))
        else:
            out.append(_HERO_INDENT + paint(line, "bold", "cyan", enable=color))
    return "\n".join(out)


def banner(*, color: bool = True, blank_windows: bool = False) -> str:
    """Pixel Bastion mark with CITADEL wordmark + tagline beside it."""
    return _with_labels(
        render_pixel_mark(color=color, blank_windows=blank_windows),
        color=color,
    )


def iter_cascade_frames(*, color: bool = True) -> Iterator[str]:
    """Yield full banner frames as pixels light left→right, top→bottom."""
    for row in range(PIXEL_SIZE):
        for col in range(PIXEL_SIZE):
            yield _with_labels(
                render_pixel_mark(color=color, cascade_upto=(row, col)),
                color=color,
            )


def print_banner_cascade(*, color: bool = True, delay: float = 0.022) -> None:
    """Animate Pixel Bastion cascade on a TTY; otherwise print static banner."""
    if not (color and sys.stdout.isatty()):
        print(banner(color=color))
        return
    # Hide cursor; redraw in place with ANSI cursor-up after first frame.
    sys.stdout.write("\033[?25l")
    try:
        lines = PIXEL_SIZE
        first = True
        for frame in iter_cascade_frames(color=color):
            if not first:
                sys.stdout.write(f"\033[{lines}A")
            first = False
            sys.stdout.write(frame + "\n")
            sys.stdout.flush()
            time.sleep(delay)
        # One idle blink of the windows.
        time.sleep(0.08)
        sys.stdout.write(f"\033[{lines}A")
        sys.stdout.write(banner(color=color, blank_windows=True) + "\n")
        sys.stdout.flush()
        time.sleep(0.12)
        sys.stdout.write(f"\033[{lines}A")
        sys.stdout.write(banner(color=color) + "\n")
        sys.stdout.flush()
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def print_banner_animated(*, color: bool = True, delay: float = 0.035) -> None:
    """Reveal the banner line-by-line (onboard ceremony)."""
    text = banner(color=color)
    if not (color and sys.stdout.isatty()):
        print(text)
        return
    for line in text.split("\n"):
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        time.sleep(delay)
