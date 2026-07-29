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

/** Full-bleed band. Each holds one 940px measure. */
export const BAND = "relative py-[74px] max-[620px]:py-12";
export const BAND_IN = "mx-auto max-w-[940px] px-[26px] max-[620px]:px-4";
/** The sticky index, not the nav, is what an anchor jump has to clear. */
export const SECTION = `${BAND} scroll-mt-[46px]`;

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
}: {
  id?: string;
  tone: Tone;
  children: ReactNode;
}) {
  return (
    <section className={`${id ? SECTION : BAND} ${TONE[tone]}`} id={id}>
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
    into a column five lines deep. */
export const H1_WIDE = `${H1} max-w-[24ch]`;
export const LEDE = "mb-[30px] max-w-[64ch] text-base leading-[1.6] text-ink-2";
export const CODE = "bg-surface-2 px-1.5 py-[1.5px] font-mono text-[.84em] text-ink";
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
  "rounded-full border border-border bg-surface px-[13px] py-1.5 text-[12.5px] font-medium text-ink-2";
export const META = "flex flex-wrap items-center gap-2";

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
    <div className="border border-border bg-surface px-6 py-[22px] transition-[border-color] duration-150 hover:border-border-2">
      <h3 className="m-0 mb-2.5 flex items-center gap-2.5 text-base font-semibold">
        {/* Square, like everything else. The one deliberately round thing on a
            card is nothing. */}
        <span className="size-2 shrink-0 bg-accent" />
        {/* Wrapped, so the title is one flex item however it is composed. A
            bare text node in a flex container is an anonymous flex item, which
            is fine while the title is a single string and wrong the moment it
            contains an inline element: each run becomes its own item and the
            gap opens up between them. That is the bug that put `.verified`'s
            links on their own lines. */}
        <span>{title}</span>
      </h3>
      {children}
    </div>
  );
}

/* --- metrics ------------------------------------------------------------ */

export const METRICS = "grid grid-cols-4 gap-3 max-[760px]:grid-cols-2";
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
    <div className="border border-border bg-surface p-[18px]">
      <div className={`${METRIC_N} ${accent ? "text-accent-ink" : "text-ink"}`}>{value}</div>
      <div className="mt-2 text-[12.5px] leading-[1.4] text-ink-2">{label}</div>
    </div>
  );
}

/** The provenance line under a block of numbers. */
export function Verified({ children }: { children: ReactNode }) {
  return (
    <p className="mt-4 flex items-center gap-2 text-[12.5px] leading-[1.5] text-ink-3">
      <span className="size-[6px] shrink-0 rounded-full bg-good" />
      <span>{children}</span>
    </p>
  );
}

/* --- TL;DR panel -------------------------------------------------------- */

export const TLDR_P = "mb-3.5 text-[17.5px] leading-[1.6]";
/** The closing line of a TL;DR steps back down to body weight and colour. */
export const TLDR_P_LAST = "m-0 text-base leading-[1.6] text-ink-2";
export const PLAINLIST =
  "mt-3.5 flex list-disc flex-col gap-[9px] pl-[18px] text-[14.5px] leading-[1.55] text-ink-2 marker:text-ink-3";
export const PLAINLIST_TLDR = `${PLAINLIST} mt-1 text-[15.5px]`;

export function Tldr({ label, children }: { label: string; children: ReactNode }) {
  return (
    // Inside a hero band the surface underneath is already white, so the panel
    // takes the next surface down rather than disappearing behind its hairline.
    <div className="relative mt-9 overflow-hidden border border-border bg-surface-2 px-8 py-7">
      <span className="absolute inset-y-0 left-0 w-[3px] bg-[linear-gradient(var(--accent),var(--accent-ink))]" />
      <p className="mb-3.5 text-xs font-semibold uppercase tracking-[.16em] text-accent-ink">
        {label}
      </p>
      {children}
    </div>
  );
}

/* --- disclosure --------------------------------------------------------- */

const SUMMARY =
  "flex cursor-pointer items-center gap-3 px-[22px] py-[17px] text-[15px] font-medium text-ink focus-visible:-outline-offset-2 focus-visible:outline-2 focus-visible:outline-accent";

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
        <span>{title}</span>
        <span className="ml-auto text-[11px] font-semibold uppercase tracking-[.1em] text-ink-3 max-[620px]:hidden">
          Go deeper
        </span>
      </summary>
      <div className="px-6 pb-6 pt-0.5">{children}</div>
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
export const CARD_ROW = `${ROW_GRID} border border-border bg-surface px-6 py-[22px]`;
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
