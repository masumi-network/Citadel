/* The shared pieces of the design system.
 *
 * A port of the reusable half of kb/static/info.css. Anything used by more than
 * one page lives here so the five pages cannot drift apart the way five
 * hand-written HTML files did; anything used once is written where it is used.
 *
 * Radius is absent everywhere on purpose. The theme zeroes Tailwind's whole
 * radius scale, so square is what you get without asking, and `rounded-full` is
 * the only way to curve anything (status dots and pills).
 */
import type { ReactNode } from "react";

/* --- layout ------------------------------------------------------------- */

/** Full-bleed band. Content sits in a 1200px measure, same as the dashboard. */
export const MEASURE = "mx-auto max-w-[1200px] px-6 max-[620px]:px-4";
export const BAND = "relative py-14 max-[620px]:py-10";
export const BAND_IN = MEASURE;
/** Anchor jumps clear sticky TopNav plus section subnav when present. */
export const SECTION = `${BAND} scroll-mt-[var(--chrome-h)]`;

/** White, grey, and the accent tint that is reserved for a fork in the path. */
export const TONE = {
  white: "bg-surface",
  grey: "bg-surface-2",
  tint: "bg-accent-soft",
} as const;

export type Tone = keyof typeof TONE;

export function Band({
  id,
  tone,
  children,
  hidden,
}: {
  id?: string;
  tone: Tone;
  children: ReactNode;
  hidden?: boolean;
}) {
  return (
    <section
      className={`${id ? SECTION : BAND} ${TONE[tone]}`}
      id={id}
      hidden={hidden || undefined}
    >
      <div className={BAND_IN}>{children}</div>
    </section>
  );
}

/* --- type --------------------------------------------------------------- */

export const EYEBROW =
  "mb-[18px] text-xs font-semibold uppercase tracking-[.16em] text-accent-ink";
export const H1 =
  "mb-[22px] text-balance text-[clamp(30px,5.4vw,62px)] font-light leading-[1.03] tracking-[-.038em]";
/** / opens on three words; these pages open on a sentence, which at 15ch stacks
    into a column five lines deep. 24ch is wider than a 390px measure, so phone
    drops the cap and uses the band. */
export const H1_WIDE = `${H1} max-w-[24ch] max-[620px]:max-w-none`;
/** Hero lede. Home is a single paragraph at 72ch. Inner pages use HeroFact. */
export const HERO_P = "max-w-[72ch] text-[17.5px] leading-[1.6] text-ink-2";
/** Full-width labelled facts under a hero. Kicker plus one line per cell,
    hairline between, never two essays. Stacks at 620px. */
export const HERO_STRIP =
  "mb-[30px] grid grid-cols-2 gap-px border-y border-border bg-border max-[620px]:grid-cols-1";
/** Three equal asks. Stacks at 900px because three columns go tight before 620. */
export const HERO_STRIP_3 =
  "mb-[30px] grid grid-cols-3 gap-px border-y border-border bg-border max-[900px]:grid-cols-1";

export function HeroFact({ kicker, children }: { kicker: string; children: ReactNode }) {
  return (
    <div className="bg-surface px-6 py-5 max-[620px]:px-4">
      <dt className="mb-1.5 font-mono text-[10.5px] font-semibold uppercase tracking-[.14em] text-accent-ink">
        {kicker}
      </dt>
      <dd className="m-0 min-w-0 text-[17.5px] leading-[1.6] text-ink-2">{children}</dd>
    </div>
  );
}

export const LEDE = "mb-[30px] max-w-[64ch] text-base leading-[1.6] text-ink-2";
export const CODE = "break-words bg-surface-2 px-1.5 py-[1.5px] font-mono text-[.84em] text-ink";
export const FOOT_NOTE = "mt-[18px] text-[13.5px] text-ink-3";

/** Segmented-line section header: rule, title, kicker on the right. */
export function SecHead({ kicker, title }: { kicker: string; title: string }) {
  return (
    <div className="mb-[26px] flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 border-t border-border pt-5 max-[620px]:gap-y-1">
      <span className="order-2 text-[11.5px] font-semibold uppercase tracking-[.16em] text-ink-3">
        {kicker}
      </span>
      <h2 className="order-1 m-0 text-[clamp(24px,3.4vw,33px)] font-light tracking-[-.025em]">
        {title}
      </h2>
    </div>
  );
}

/* --- buttons and pills --------------------------------------------------- */

export const BTN =
  "inline-flex items-center justify-center border border-border-2 bg-surface px-5 py-[11px] text-sm font-medium text-ink no-underline transition-[border-color,color,filter] duration-150 hover:border-ink-3 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent max-[620px]:flex-[1_1_100%]";
export const BTN_PRIMARY =
  "inline-flex items-center justify-center border border-accent bg-accent px-5 py-[11px] text-sm font-medium text-on-accent no-underline transition-[filter] duration-150 hover:brightness-[1.06] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent max-[620px]:flex-[1_1_100%]";
export const CTA = "mb-[26px] flex flex-wrap gap-2.5";

export const PILL =
  "max-w-full rounded-full border border-border bg-surface px-[13px] py-1.5 text-[12.5px] font-medium text-ink-2 max-[620px]:px-2.5";
export const META = "mt-0 mb-0 flex flex-wrap items-center gap-2 max-[620px]:gap-1.5";

const CHIP = "whitespace-nowrap px-[11px] py-[5px] text-[11px] font-semibold uppercase";
export const CHIP_TONE = {
  ship: `${CHIP} tracking-[.04em] bg-good-bg text-good`,
  prog: `${CHIP} tracking-[.04em] bg-warn-bg text-warn`,
  plan: `${CHIP} tracking-[.04em] bg-surface-2 text-ink-2`,
  step: `${CHIP} tracking-[.08em] bg-accent-soft text-accent-ink`,
} as const;

export function Chip({ tone, children }: { tone: keyof typeof CHIP_TONE; children: ReactNode }) {
  return <span className={CHIP_TONE[tone]}>{children}</span>;
}

/* --- cards -------------------------------------------------------------- */

export const CARD_P = "m-0 text-[14.5px] leading-[1.6] text-ink-2";
export const CARD_TAG =
  "mt-3.5 inline-block bg-surface-2 px-[9px] py-[3px] font-mono text-[11px] tracking-[.02em] text-ink-3";
export const PILLARS = "grid grid-cols-2 gap-3.5 max-[620px]:grid-cols-1";

export function Card({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div className="border border-border bg-surface px-6 py-[22px] transition-[border-color] duration-150 hover:border-border-2 max-[620px]:px-4 max-[620px]:py-5">
      <h3 className="m-0 mb-2.5 flex items-center gap-2.5 text-base font-semibold">
        {/* Square, like everything else. The one deliberately round thing on a
            card is nothing. */}
        <span className="size-2 shrink-0 bg-accent" />
        {/* Wrapped, so the title is one flex item however it is composed. A
            bare text node in a flex container is an anonymous flex item, which
            is fine while the title is a single string and wrong the moment it
            contains an inline element: each run becomes its own item and the
            gap opens up between them. That is the bug that put `.verified`'s
            links on their own lines. min-w-0 lets a long title wrap inside
            the card instead of stretching the page. */}
        <span className="min-w-0">{title}</span>
      </h3>
      {children}
    </div>
  );
}

/* --- metrics ------------------------------------------------------------ */

/* Three across, because there are six of them. The row was four across while
   two of the tiles were repo trivia (a test count and a LOC count) that had
   gone stale on the page; dropping those left six, and six in a four-column
   grid is a full row and a stranded pair. */
export const METRICS = "grid grid-cols-3 gap-3 max-[760px]:grid-cols-2";
const METRIC_N =
  "font-mono text-[26px] font-medium leading-[1.1] tracking-[-.02em] tabular-nums min-[1120px]:text-[28px]";

export function Metric({
  value,
  label,
  accent,
}: {
  value: ReactNode;
  label: ReactNode;
  accent?: boolean;
}) {
  return (
    <div className="border border-border bg-surface p-[18px] max-[620px]:p-3.5">
      <div className={`${METRIC_N} ${accent ? "text-accent-ink" : "text-ink"}`}>{value}</div>
      <div className="mt-2 text-[12.5px] leading-[1.4] text-ink-2">{label}</div>
    </div>
  );
}

/** The provenance line under a block of numbers. */
export function Verified({ children }: { children: ReactNode }) {
  return (
    <p className="mt-4 flex items-start gap-2 text-[12.5px] leading-[1.5] text-ink-3">
      <span className="mt-[7px] size-[6px] shrink-0 rounded-full bg-good" />
      <span className="min-w-0">{children}</span>
    </p>
  );
}

/* --- lists -------------------------------------------------------------- */

export const PLAINLIST =
  "mt-3.5 flex list-disc flex-col gap-[9px] pl-[18px] text-[14.5px] leading-[1.55] text-ink-2 marker:text-ink-3";
export const TLDR_P = "mb-3 text-[15.5px] leading-[1.55] text-ink";

/* --- disclosure --------------------------------------------------------- */

const SUMMARY =
  "flex cursor-pointer items-center gap-3 px-[22px] py-[17px] text-[15px] font-medium text-ink focus-visible:-outline-offset-2 focus-visible:outline-2 focus-visible:outline-accent max-[620px]:px-4";

/** "Go deeper": the technical version, folded away by default. */
export function GoDeeper({
  title,
  open,
  children,
}: {
  title: ReactNode;
  open?: boolean;
  children: ReactNode;
}) {
  return (
    <details
      open={open}
      className="godeeper mt-3.5 overflow-hidden border border-border bg-surface open:border-border-2"
    >
      <summary className={SUMMARY}>
        <span className="chev shrink-0 text-[13px] text-accent-ink transition-transform duration-200">
          ▸
        </span>
        {/* One flex item, whatever the title is made of. See Card above. */}
        <span className="min-w-0">{title}</span>
        <span className="ml-auto text-[11px] font-semibold uppercase tracking-[.1em] text-ink-3 max-[620px]:hidden">
          Go deeper
        </span>
      </summary>
      <div className="px-6 pb-6 pt-0.5 max-[620px]:px-4">{children}</div>
    </details>
  );
}

export const DEEP_P = "mb-3 text-[14.5px] leading-[1.6] text-ink-2";
export const DEEP_H4 =
  "mb-[9px] mt-[18px] text-xs font-semibold uppercase tracking-[.12em] text-ink";
export const DEEP_UL = "flex list-none flex-col gap-[9px] p-0";
export const DEEP_LI = "relative pl-5 text-[14.5px] leading-[1.55] text-ink-2";
export const DEEP_DOT = "absolute left-[3px] top-[9px] size-[5px] rounded-full bg-accent";
export const DEEP_B = "text-ink font-semibold";

/** One bullet in a "go deeper" list, with its accent dot. */
export function DeepLi({ children }: { children: ReactNode }) {
  return (
    <li className={DEEP_LI}>
      <span className={DEEP_DOT} />
      {children}
    </li>
  );
}

/* --- labelled rows ------------------------------------------------------ */

export const ROWS = "flex flex-col gap-3";
const ROW_GRID =
  "grid grid-cols-[118px_1fr] items-start gap-5 min-[1120px]:grid-cols-[150px_1fr] max-[620px]:grid-cols-1 max-[620px]:gap-[11px]";
/** A card with a label gutter. */
export const CARD_ROW = `${ROW_GRID} border border-border bg-surface px-6 py-[22px] max-[620px]:px-4`;
/** The same row as a hairline, used where the section is a list rather than a
    set of cards. */
export const HAIRLINE_ROW = `${ROW_GRID} border-t border-border py-6 last:border-b`;
export const ROW_H3 = "mb-[7px] text-base font-semibold";
/** The emphasis inside a row's prose: the phrase that carries the claim. */
export const ROW_K = "text-ink font-semibold";

export function Row({
  label,
  title,
  variant = "card",
  children,
}: {
  label: ReactNode;
  title: string;
  variant?: "card" | "hairline";
  children: ReactNode;
}) {
  return (
    <div className={variant === "card" ? CARD_ROW : HAIRLINE_ROW}>
      <div className="flex pt-[3px]">{label}</div>
      <div>
        <h3 className={ROW_H3}>{title}</h3>
        <p className={CARD_P}>{children}</p>
      </div>
    </div>
  );
}

/* --- forms -------------------------------------------------------------- */

export const FIELD_LABEL = "text-[13px] font-medium text-ink";
export const FIELD_INPUT =
  "w-full border border-border-2 bg-surface px-3.5 py-3 font-mono text-sm text-ink transition-[border-color] duration-150 placeholder:text-ink-3 hover:border-ink-3 focus:border-accent focus:outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";
export const FIELD_HINT = "mt-1 text-[13px] leading-[1.6] text-ink-2";
export const SUBMIT =
  "mt-3 cursor-pointer border border-accent bg-accent px-4 py-3 text-sm font-medium text-on-accent transition-[filter,opacity] duration-150 hover:brightness-[1.06] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent aria-busy:cursor-default aria-busy:opacity-60 disabled:cursor-default disabled:opacity-60";
