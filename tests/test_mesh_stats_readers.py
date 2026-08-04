"""No surface may read a removed or renamed field from the top level of the
mesh stats payload.

ADR-0019 publishes `searches`, `feedback`, `upgrades`, `errors` and
`pending_chunks` only under `stats.since_restart`, and removes
`failed_chunks`. ADR-0020 does the same for the two remaining top-level names
that did not mean what they said: `indexed_chunks` is removed (it duplicated
`nodes` by construction, and the honest restart-scoped accumulator is
`since_restart.indexed_chunks`), and `documents` is renamed `tracked_sources`
(it held tracked github repos + repo-content files + linear issues, not a
document count). A reader left on the old shape does not throw:
`(stats || {}).errors` and `stats?.errors` both evaluate to undefined,
coerce to 0, and paint the healthy state. The failure is silent and points
in the reassuring direction, exactly the kind of number ADR-0019 exists to
stop. A plain grep for `stats.errors` cannot find it, because the
defensive-access idioms put `|| {})`, `?.` or brackets between the object
and the field. renderHome's health chip shipped through review that way.

So the guard is a scan tolerant of those idioms, over every surface that
renders the payload: the hand-written dashboard (kb/static), the Next.js
source (web/src), and the committed export production actually serves
(kb/webui; Railway runs no Node, so a stale build keeps a reader the
source no longer has). The scanner is itself tested against the exact
lines that evaded review, so it cannot rot into a guard that scans
nothing and passes.
"""

from __future__ import annotations

import re
from pathlib import Path

import kb.server as server_module
from kb.cli import _render_mesh

REPO = Path(server_module.__file__).resolve().parent.parent

# Names that must never appear at the top level of `stats` again. The first six
# are the ADR-0019 activity counters; the last two are the ADR-0020 totals.
# `documents` and `indexed_chunks` still exist under `since_restart`, where they
# are correct, which is exactly why the scan below has to be scope-aware rather
# than a plain grep for the name.
REMOVED = (
    "searches",
    "feedback",
    "upgrades",
    "errors",
    "pending_chunks",
    "failed_chunks",
    "documents",
    "indexed_chunks",
)

# `stats` (bare or as an identifier suffix, e.g. homeStats) followed within a
# statement by an access of a removed field, with no `since_restart` hop in
# between. The tempered window tolerates `|| {})`, `?.`, casts and brackets.
TOP_LEVEL_READ = re.compile(
    r"stats\b"
    r"(?:(?!since_restart)[^;\n]){0,60}?"
    r"[.\[]\s*[\"']?"
    r"(?:" + "|".join(REMOVED) + r")\b",
    re.IGNORECASE,
)

SCAN_GLOBS = (
    ("kb/static", "*.js"),
    ("kb/static", "*.html"),
    ("web/src", "*.ts"),
    ("web/src", "*.tsx"),
    ("kb/webui", "*.js"),
    ("kb/webui", "*.html"),
)


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for root, pattern in SCAN_GLOBS:
        files.extend(sorted((REPO / root).rglob(pattern)))
    return files


def top_level_reads(text: str) -> list[str]:
    return [match.group(0) for match in TOP_LEVEL_READ.finditer(text)]


def test_no_surface_reads_activity_counters_from_the_top_level() -> None:
    files = scanned_files()
    # An empty scan passes vacuously; pin the surfaces that must be present.
    names = {file.name for file in files}
    assert "app.js" in names, "kb/static/app.js missing from the scan"
    assert any(file.suffix == ".js" and "webui" in file.parts for file in files), (
        "no committed webui chunks scanned: the export production serves is unguarded"
    )
    assert any(file.suffix == ".tsx" for file in files), "web/src pages missing from the scan"

    offenders: list[str] = []
    for file in files:
        text = file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for hit in top_level_reads(line):
                offenders.append(f"{file.relative_to(REPO)}:{lineno}: {hit}")

    assert not offenders, (
        "reader(s) still on the pre-ADR-0019/ADR-0020 payload shape; these "
        "evaluate to undefined and render as 0/healthy:\n" + "\n".join(offenders)
    )


def test_scanner_catches_the_idioms_that_evaded_review() -> None:
    """The scan can actually fail. Each snippet below is a verbatim reader that
    shipped or nearly shipped; a scanner that misses any of them is inert."""
    evasive_readers = [
        # renderHome's health chip: double `|| {}` guard defeats `stats.errors`.
        "const errors = Number(((state.snapshot || {}).stats || {}).errors || 0);",
        # The Next.js Home pill: optional chaining.
        "const errors = mesh.data?.stats?.errors ?? 0;",
        # The same reader as Turbopack minified it in the committed export.
        "g=l.data?.stats?.errors??0,y=(0,a.relativeTime)(p.data?.last_checked_at)",
        # Plain and bracket access.
        "timelineStatValues.failed.textContent = String(stats.failed_chunks || 0);",
        'const searches = snapshot.stats["searches"];',
        # A reader that renamed the intermediate variable.
        "const n = Number((meshStats || {}).errors || 0);",
        # ADR-0020. These four are the verbatim pre-rename readers: they are the
        # ones a plain revert of kb/static restores, and each renders a number
        # under a name the payload no longer uses.
        'document.getElementById("statDocuments").textContent = snapshot.stats.documents;',
        "knowledgeSnapshotCount.textContent = String(snapshot.stats.documents || 0);",
        '{ label: "Notes", value: Number(stats.documents || 0), tone: "primary" },',
        "timelineStatValues.indexed.textContent = String(stats.indexed_chunks || 0);",
    ]
    for snippet in evasive_readers:
        assert top_level_reads(snippet), f"scanner missed: {snippet}"

    correct_readers = [
        "const errors = Number((homeStats.since_restart || {}).errors || 0);",
        "const errors = mesh.data?.stats?.since_restart?.errors ?? 0;",
        "const sinceRestart = snapshot.stats.since_restart || {};",
        "timelineStatValues.pending.textContent = String(since.pending_chunks || 0);",
        # ADR-0020: the renamed field, the surviving since_restart accumulators,
        # and a `documents` that has nothing to do with the stats payload
        # (/api/sources rows carry a per-source document count).
        'document.getElementById("statDocuments").textContent = snapshot.stats.tracked_sources;',
        "timelineStatValues.indexed.textContent = String(since.indexed_chunks || 0);",
        "const captured = snapshot.stats.since_restart.documents;",
        "<p>${escapeHtml(source.documents || 0)} notes</p>",
    ]
    for snippet in correct_readers:
        assert not top_level_reads(snippet), f"false positive on: {snippet}"


def test_cli_mesh_renderer_reads_since_restart() -> None:
    """`citadel status` renders searches from since_restart; against a payload
    with no top-level counters, a reverted reader would print `0 searches`."""
    rendered = _render_mesh(
        {
            "stats": {
                "tracked_sources": 12,
                "nodes": 30,
                "edges": 45,
                "since_restart": {"searches": 3, "errors": 1, "started_at": "2026-08-03T00:00:00Z"},
            }
        },
        color=False,
    )

    assert "3 searches since restart" in rendered
    assert "12 tracked sources" in rendered
