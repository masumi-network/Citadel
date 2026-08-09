"""Interactive terminal prompts — stdlib only.

`checkbox_select` is an arrow-key multi-select (↑/↓ move, space toggle, enter
apply) used by `citadel onboard` for the coding-tools step. On terminals where
raw mode isn't available (no termios — e.g. Windows — or stdin/stdout not a
real TTY) it degrades to a numeric toggle prompt, so the same call works
everywhere the wizard runs.
"""

from __future__ import annotations

import os
import shutil
import sys

from kb.banner import OK, paint, supports_color

_HELP = "↑/↓ move · space toggle · a all · n none · enter apply · q/esc skip"
_ESC = "esc"  # sentinel for a bare Escape keypress


def _read_key(fd: int) -> str:
    """One keypress, read straight from the fd (os.read — Python's buffered
    stdin would hide bytes from select). Decodes CSI escape sequences fully
    (arrows, Delete, PgUp …) so no stray bytes leak into the next read, and
    detects a *bare* Esc via a short select() poll instead of blocking on a
    byte that never comes."""
    import select

    def _one(timeout: float | None = None) -> str:
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return ""
        return os.read(fd, 1).decode(errors="replace")

    ch = _one()
    if ch != "\x1b":
        return ch
    nxt = _one(timeout=0.05)
    if not nxt:
        return _ESC
    if nxt != "[":
        return _ESC  # Alt-chord or unknown escape — treat as cancel intent
    seq = ""
    while True:
        part = _one(timeout=0.05)
        seq += part
        # A CSI sequence ends at its final byte (0x40–0x7E: letters, ~, …).
        if not part or "\x40" <= part <= "\x7e":
            return "\x1b[" + seq


def _clip(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[: max(budget - 1, 0)] + "…"


def _render_rows(
    options: list[str], checked: set[int], cursor: int, *, color: bool, width: int
) -> list[str]:
    rows = []
    for index, label in enumerate(options):
        box = paint(f"[{OK}]", "green", enable=color) if index in checked else "[ ]"
        pointer = paint("❯", "bold", "cyan", enable=color) if index == cursor else " "
        # Clip the RAW label so a row can never wrap — a wrapped row would
        # desync the cursor-up repaint and garble the whole block.
        label = _clip(label, width - 8)
        text = paint(label, "bold", enable=color) if index == cursor else label
        rows.append(f"  {pointer} {box} {text}")
    return rows


def _select_raw(header: str, options: list[str], checked: set[int]) -> set[int] | None:
    """Arrow-key checkbox UI. Repaints in place; cursor hidden while active."""
    import termios
    import tty

    color = supports_color()
    cursor = 0
    out = sys.stdout
    drawn = 0
    width = shutil.get_terminal_size((80, 24)).columns

    def render() -> None:
        nonlocal drawn
        if drawn:
            out.write(f"\033[{drawn}A")  # jump back to the top of our block
        rows = [
            header,
            *_render_rows(options, checked, cursor, color=color, width=width),
            "  " + paint(_clip(_HELP, width - 4), "dim", enable=color),
        ]
        for row in rows:
            out.write("\033[2K" + row + "\n")
        drawn = len(rows)
        out.flush()

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out.write("\033[?25l")
    try:
        # cbreak (not raw): keypresses arrive unbuffered but Ctrl-C still
        # signals, so the global KeyboardInterrupt → exit 130 path holds.
        tty.setcbreak(fd)
        while True:
            render()
            key = _read_key(fd)
            if key in ("\x1b[A", "k"):
                cursor = (cursor - 1) % len(options)
            elif key in ("\x1b[B", "j"):
                cursor = (cursor + 1) % len(options)
            elif key == " ":
                checked.symmetric_difference_update({cursor})
            elif key in ("a", "A"):
                checked = set(range(len(options)))
            elif key in ("n", "N"):
                checked = set()
            elif key in ("\r", "\n"):
                return checked
            elif key in ("q", "Q", _ESC) or not key:
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write("\033[?25h")
        out.flush()


def _select_lines(header: str, options: list[str], checked: set[int]) -> set[int] | None:
    """Line-based fallback: toggle by number, Enter applies, q skips."""
    color = supports_color()
    print(header)
    while True:
        for index, label in enumerate(options, 1):
            box = paint(f"[{OK}]", "green", enable=color) if index - 1 in checked else "[ ]"
            print(f"    {box} {index}. {label}")
        try:
            raw = (
                input("  Toggle by number (e.g. 1 3), a all, n none, q skip — Enter to apply: ")
                .strip()
                .lower()
            )
        except EOFError:
            print()
            return checked
        if not raw:
            return checked
        if raw in ("q", "quit"):
            return None
        if raw == "a":
            checked = set(range(len(options)))
            continue
        if raw == "n":
            checked = set()
            continue
        for part in raw.replace(",", " ").split():
            if part.isdigit() and 1 <= int(part) <= len(options):
                checked.symmetric_difference_update({int(part) - 1})


def checkbox_select(header: str, options: list[str], checked: set[int]) -> set[int] | None:
    """Multi-select over `options`; returns chosen indexes, or None on skip.

    `checked` marks the preselected rows. Uses the arrow-key UI on a capable
    TTY, the numeric fallback everywhere else.
    """
    if not options:
        return set()
    checked = {i for i in checked if 0 <= i < len(options)}
    raw_capable = os.getenv("TERM", "") != "dumb" and sys.stdin.isatty() and sys.stdout.isatty()
    if raw_capable:
        try:
            return _select_raw(header, options, set(checked))
        except KeyboardInterrupt:
            raise  # Ctrl-C keeps its meaning: abort cleanly (exit 130)
        except Exception:
            # No termios (Windows), termios.error from odd ptys/IDE terminals,
            # bad ioctls — a cosmetic UI must degrade, never crash onboarding.
            pass
    return _select_lines(header, options, set(checked))
