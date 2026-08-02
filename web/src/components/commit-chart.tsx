import { useEffect, useState } from "react";

import type { RepoBlock } from "@/lib/vault-state";

type Week = { label: string; commits: number; tag?: string };

/* The baked series is the fallback. It came from git log at report time, which
   is also why it cannot refresh itself: the deployed node has no git and no
   repository, only the built image. /api/state carries the live weekly counts
   from GitHub, and the chart re-draws with those once the fetch lands. */
const BAKED: Week[] = [
  { label: "May 18", commits: 9 },
  { label: "May 25", commits: 24 },
  { label: "Jun 1", commits: 30 },
  { label: "Jun 8", commits: 26 },
  { label: "Jun 15", commits: 20 },
  { label: "Jun 22", commits: 91, tag: "v0.1.x" },
  { label: "Jun 29", commits: 78, tag: "v0.2.0–2.2" },
  { label: "Jul 6", commits: 5, tag: "v0.2.3" },
  { label: "Jul 13", commits: 41, tag: "v0.3.0" },
  { label: "Jul 20", commits: 53, tag: "v0.4.0" },
];

/* Release markers are a local fact, not something GitHub returns, so they are
   carried across onto whichever live week they belong to. */
const TAG_BY_LABEL = new Map(
  BAKED.filter((week) => week.tag).map((week) => [week.label, week.tag as string])
);

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function weekLabel(iso: string): string {
  const parts = String(iso).split("-");
  if (parts.length !== 3) return String(iso);
  const month = MONTHS[parseInt(parts[1], 10) - 1];
  if (!month) return String(iso);
  return `${month} ${parseInt(parts[2], 10)}`;
}

function liveSeries(repo: RepoBlock | undefined): Week[] | null {
  if (!repo?.weeks?.length) return null;
  // The layout fits ~12 columns; GitHub can report up to 52 weeks. Newest win.
  return repo.weeks.slice(-12).map((week) => {
    const label = weekLabel(week.start);
    return { label, commits: week.commits, tag: TAG_BY_LABEL.get(label) };
  });
}

function describe(series: Week[]): string {
  const peak = series.reduce((best, week) => (week.commits > best.commits ? week : best), series[0]);
  return (
    `Commits per week on the main branch, ${series[0].label} to ` +
    `${series[series.length - 1].label}, peaking at ${peak.commits} in the week of ${peak.label}.`
  );
}

/* Commits per week, as bars.
 *
 * Bar heights are the one thing on these pages that has to be computed at
 * runtime, and they are set through the CSSOM: React writes `node.style.height`
 * rather than emitting a `style` attribute, and CSP governs style attributes in
 * markup, not programmatic style writes. That only holds because the bars are
 * rendered after mount and never pre-rendered, which is what `drawn` is for. A
 * server-rendered bar would put `style="height:73px"` into the exported HTML
 * and `style-src 'self'` would drop it.
 *
 * Rendering after mount also matches what it replaces: the hand-written page
 * drew this chart from a deferred script, so with JavaScript off both versions
 * show an empty frame.
 */
export function CommitChart({ repo }: { repo: RepoBlock | undefined }) {
  const [drawn, setDrawn] = useState(false);
  useEffect(() => setDrawn(true), []);

  const series = liveSeries(repo) ?? BAKED;
  const max = series.reduce((most, week) => Math.max(most, week.commits), 1);

  return (
    <div className="mb-[30px] border border-border bg-surface px-[26px] pb-5 pt-6">
      <div className="mb-5 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-semibold">Commits per week</span>
        <span className="text-[11.5px] text-ink-3">
          brand-color bars = a release shipped that week
        </span>
      </div>
      <div
        className="flex h-[168px] items-end gap-[3%]"
        role="img"
        aria-label={drawn ? describe(series) : "Commits per week."}
      >
        {drawn
          ? series.map((week, i) => (
              <div
                key={week.label}
                className="group flex h-full min-w-0 flex-1 flex-col items-center justify-end gap-1.5"
                title={`${week.commits} commits · week of ${week.label}${
                  week.tag ? ` · ${week.tag}` : ""
                }`}
              >
                <div
                  className={`font-mono text-[11px] tabular-nums max-[620px]:text-[8.5px] ${
                    week.tag ? "font-medium text-accent-ink" : "text-ink-3"
                  }`}
                >
                  {week.commits}
                </div>
                <div
                  style={{ height: `${Math.max(3, Math.round((week.commits / max) * 104))}px` }}
                  className={`w-[74%] max-w-[30px] min-h-[3px] transition-[filter] duration-150 group-hover:brightness-[1.05] ${
                    week.tag
                      ? "bg-[linear-gradient(var(--accent),var(--accent-ink))]"
                      : "border border-border-2 bg-surface-2"
                  }`}
                />
                {/* At phone widths a column is ~17-24px while a tag is
                    31-36px of mono text, and releases land on consecutive
                    weeks, so the tags collide into one string; the legend
                    already marks release weeks by bar color. */}
                <div className="min-h-[11px] text-center font-mono text-[9px] font-medium leading-[1.2] text-accent-ink max-[620px]:text-[8.5px] max-[470px]:hidden">
                  {week.tag ?? ""}
                </div>
                {/* Below 400px even the week labels touch: keep alternates. */}
                <div
                  className={`text-center text-[9.5px] leading-[1.3] text-ink-3${
                    i % 2 ? " max-[400px]:hidden" : ""
                  }`}
                >
                  {week.label}
                </div>
              </div>
            ))
          : null}
      </div>
    </div>
  );
}
