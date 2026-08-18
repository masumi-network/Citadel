import Head from "next/head";

import { HeroBand } from "@/components/hero-band";
import { PipelineDiagram } from "@/components/pipeline-diagram";
import { WithSectionRail, type Section } from "@/components/section-index";
import { SiteFooter } from "@/components/site-footer";
import {
  BAND,
  BAND_IN,
  BTN,
  BTN_PRIMARY,
  Band,
  CARD_P,
  CODE,
  CTA,
  Chip,
  DEEP_B,
  DEEP_H4,
  DEEP_P,
  DEEP_UL,
  DeepLi,
  EYEBROW,
  GoDeeper,
  H1,
  HAIRLINE_ROW,
  HERO_P,
  LEDE,
  ROW_H3,
  SecHead,
} from "@/components/ui";

/* The front door.
 *
 * A port of kb/static/landing.html, copy for copy. Full-bleed bands, each
 * holding a 1200px measure, alternating white and grey with one accent-tinted
 * band at the fork. Every band colour is a token, so dark mode inverts with the
 * rest of the ramp.
 */
const SECTIONS: Section[] = [
  { id: "what", label: "What it is" },
  { id: "how", label: "How it works" },
  { id: "next", label: "Two ways in" },
  { id: "start", label: "Get started" },
];

const ROLLING_WORDS = ["decisions.", "dead ends.", "reasons.", "sessions.", "context."];

/* Static repo facts. Live numbers stay on /info, deliberately: a tile that can
   go stale on the front door is worse than one that is dated on a report. */
const TILE_N =
  "font-mono text-[26px] font-medium leading-[1.1] tracking-[-.02em] tabular-nums text-ink";
const TILE_K = "mt-2 text-[12.5px] leading-[1.4] text-ink-2";

const CMD =
  "my-4 overflow-x-auto whitespace-nowrap border border-border bg-surface px-4 py-3.5 font-mono text-[13.5px] text-ink";
const END_LINK =
  "border-b border-border-2 text-ink-2 no-underline hover:border-accent hover:text-accent-ink";

export default function Home() {
  return (
    <>
      <Head>
        <title>Citadel</title>
        <meta
          name="description"
          content="Citadel is shared, governed memory for your team and its AI agents. Your work flows in automatically, no other seat can read your Node, and Central is the org's curated memory."
        />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>

      <HeroBand current="/" sections={SECTIONS}>
        <p className={EYEBROW}>The organization vault</p>
        <h1 className={`${H1} max-w-[15ch]`}>
          Citadel remembers your{" "}
          <span className="roll">
            <span className="roll-track">
              {ROLLING_WORDS.map((word) => (
                <span key={word} className="grad">
                  {word}
                </span>
              ))}
              {/* A sixth span repeating the first, so the wrap from the last
                  word back to the first rolls instead of cutting. */}
              <span className="grad" aria-hidden="true">
                {ROLLING_WORDS[0]}
              </span>
            </span>
          </span>
        </h1>
        <p className={`${HERO_P} mb-[30px]`}>
          Your team already wrote it down, in commits, sessions, docs, and issues. Citadel captures
          it as it happens and gives you, and the agents beside you, one place to ask.
        </p>
        <div className={CTA}>
          <a className={BTN_PRIMARY} href="#start">
            Get started
          </a>
          <a className={BTN} href="/use-cases">
            See what teams use it for
          </a>
        </div>
      </HeroBand>

      <WithSectionRail>
      <section className={`${BAND} bg-surface`}>
        <div className={BAND_IN}>
          <div className="grid grid-cols-4 gap-3 max-[900px]:grid-cols-2">
            <div className="border border-border bg-surface p-5 max-[620px]:p-4">
              <div className={`${TILE_N} text-[19px] text-accent-ink`}>Apache-2.0</div>
              <div className={TILE_K}>Open source, self-hosted</div>
            </div>
            <div className="border border-border bg-surface p-5 max-[620px]:p-4">
              <div className={`${TILE_N} text-[19px]`}>Seat + Node</div>
              <div className={TILE_K}>Per agent. Central stays in sync</div>
            </div>
            <div className="border border-border bg-surface p-5 max-[620px]:p-4">
              <div className={TILE_N}>24</div>
              <div className={TILE_K}>MCP tools for agents</div>
            </div>
            <div className="border border-border bg-surface p-5 max-[620px]:p-4">
              <div className={`${TILE_N} text-[19px]`}>~$23–$58/mo</div>
              <div className={TILE_K}>Self-host; mostly RAM, not a fixed bill</div>
            </div>
          </div>
          <p className="mt-[18px] text-[13px] leading-[1.6] text-ink-3">
            This page is served by the system it describes. Live numbers, releases, and the roadmap
            are on the <a href="/info">status page</a>.
          </p>
          {/* Cost range: 2026-08-17 24h ~$23 and 2026-08-14 7-day ~$58.
              Search 25 s and 269 ms stay in docs/performance.md, not this note. */}
          <p className="mt-[18px] text-[13px] leading-[1.6] text-ink-3">
            Self-host cost is mostly RAM: 24h 2026-08-17 about $23; 7-day 2026-08-14
            about $58. Not a ceiling; method in the{" "}
            <a href="https://github.com/masumi-network/Citadel/tree/main/scripts/bench">
              bench harness
            </a>
            .
          </p>
        </div>
      </section>

      <Band tone="grey" id="what">
        <SecHead kicker="01 · What it is" title="Memory with a boundary" />
        <p className={LEDE}>
          Most team knowledge tools ask you to file things. Citadel captures the work you were doing
          anyway, then keeps the personal and the shared strictly apart.
        </p>

        <PipelineDiagram />

        <div className="grid grid-cols-2 gap-px bg-border max-[620px]:grid-cols-1">
          <div className="bg-surface px-6 py-5 max-[620px]:px-4">
            <p className="mb-1.5 font-mono text-[10.5px] font-semibold uppercase tracking-[.14em] text-accent-ink">
              Your Node
            </p>
            <h3 className="m-0 mb-2 text-base font-semibold">Personal by default</h3>
            <p className={CARD_P}>
              Everything you capture lands here. No other seat can read it, and no job promotes it
              into shared memory. Sharing is an act, not a setting.
            </p>
          </div>
          <div className="bg-surface px-6 py-5 max-[620px]:px-4">
            <p className="mb-1.5 font-mono text-[10.5px] font-semibold uppercase tracking-[.14em] text-ink-3">
              Central
            </p>
            <h3 className="m-0 mb-2 text-base font-semibold">Curated, not a dump</h3>
            <p className={CARD_P}>
              The org&apos;s shared memory only holds what a person promoted into it, so it stays
              worth trusting instead of everyone&apos;s scratch notes.
            </p>
          </div>
          <div className="bg-surface px-6 py-4 max-[620px]:px-4">
            <h3 className="m-0 mb-1 text-[15px] font-semibold">Agents search as you</h3>
            <p className={CARD_P}>
              Claude Code, Cursor, or your own MCP client, same seat, same read isolation.
            </p>
          </div>
          <div className="bg-surface px-6 py-4 max-[620px]:px-4">
            <h3 className="m-0 mb-1 text-[15px] font-semibold">Every answer cites a source</h3>
            <p className={CARD_P}>
              A commit, an issue, a session. Check the claim instead of taking the vault&apos;s word.
            </p>
          </div>
        </div>

        <GoDeeper title="The access and data model in one pass">
          <p className={DEEP_P}>
            Citadel is a FastAPI service over a retrieval layer, but the moat is the governance
            around it.
          </p>
          <h4 className={DEEP_H4}>Read scope</h4>
          <ul className={DEEP_UL}>
            <DeepLi>
              A caller sees their own <b className={DEEP_B}>Node</b>,{" "}
              <b className={DEEP_B}>Central</b>, and non-seat datasets, resolved by a four-pass
              cross-dataset visibility algorithm.
            </DeepLi>
            <DeepLi>
              <b className={DEEP_B}>Seat presence is universal</b> (every seat is a hub with a slug
              and counts), but content is caller-scoped. Foreign-seat drill-down returns{" "}
              <b className={DEEP_B}>404, not 403</b>, so there is no existence oracle.
            </DeepLi>
            <DeepLi>Admin and env tokens bypass for operations; every call is audited.</DeepLi>
          </ul>
          <h4 className={DEEP_H4}>Write scope</h4>
          <ul className={DEEP_UL}>
            <DeepLi>
              All seat-scoped writes land on the owning Node. Untagged writes to Central are
              rejected (403).
            </DeepLi>
            <DeepLi>
              Every write path, HTTP, MCP, hooks, feedback, runs the seat write-policy guard and a
              secret scan.
            </DeepLi>
          </ul>
          <h4 className={DEEP_H4}>Promotion to Central</h4>
          <ul className={DEEP_UL}>
            <DeepLi>
              The <b className={DEEP_B}>Promotion Agent</b> cross-references GitHub org repos and
              Central, auto-promotes known work after secret-scan and LLM review, and queues
              new-project candidates for human approval (dashboard, MCP, or{" "}
              <code className={CODE}>citadel promotion</code>).
            </DeepLi>
          </ul>
        </GoDeeper>
      </Band>

      <Band tone="white" id="how">
        <SecHead kicker="02 · How it works" title="Three moving parts" />
        <p className={LEDE}>
          You install once. After that the interesting part is what you do not have to do.
        </p>
        <div>
          <div className={HAIRLINE_ROW}>
            <div className="flex pt-[3px]">
              <Chip tone="step">Capture</Chip>
            </div>
            <div>
              <h3 className={ROW_H3}>It runs without you.</h3>
              <p className={CARD_P}>
                A session hook and a git pre-push hook feed your Node while you work, and the
                evolve pass syncs the org&apos;s GitHub and Linear. Nothing to file, nothing to
                remember to save.
              </p>
            </div>
          </div>
          <div className={HAIRLINE_ROW}>
            <div className="flex pt-[3px]">
              <Chip tone="step">Search</Chip>
            </div>
            <div>
              <h3 className={ROW_H3}>One question, both memories.</h3>
              <p className={CARD_P}>
                <code className={CODE}>citadel search</code> and the MCP tools read your Node and
                Central together, and tell you which one answered.
              </p>
            </div>
          </div>
          <div className={HAIRLINE_ROW}>
            <div className="flex pt-[3px]">
              <Chip tone="step">Promote</Chip>
            </div>
            <div>
              <h3 className={ROW_H3}>Sharing stays deliberate.</h3>
              <p className={CARD_P}>
                When something is worth the whole org knowing, you promote it. Until then it stays
                on your seat, out of reach of the rest of the org and of their agents.
              </p>
            </div>
          </div>
        </div>
      </Band>

      <Band tone="tint" id="next">
        <SecHead kicker="03 · Where to go next" title="Two ways in" />
        <div className="grid grid-cols-2 gap-3.5 max-[620px]:grid-cols-1">
          {/* The left door is the one we want taken. */}
          <div className="flex flex-col border border-accent bg-surface px-7 py-[26px] max-[620px]:px-4 max-[620px]:py-5">
            <h3 className="mb-2 text-xl font-medium tracking-[-.02em]">Use it</h3>
            <p className="mb-[22px] text-[14.5px] leading-[1.6] text-ink-2">
              Run it on your own work. Install the CLI, hand it a seat token, and your agents search
              the same memory you do. Self-hosted and Apache-2.0, so you can read every line of what
              it does.
            </p>
            <div className={`${CTA} mb-0 mt-auto`}>
              <a className={BTN_PRIMARY} href="#start">
                Get started
              </a>
              <a className={BTN} href="/use-cases">
                Use cases
              </a>
            </div>
          </div>
          <div className="flex flex-col border border-border-2 bg-surface px-7 py-[26px] max-[620px]:px-4 max-[620px]:py-5">
            <h3 className="mb-2 text-xl font-medium tracking-[-.02em]">Work with us</h3>
            <p className="mb-[22px] text-[14.5px] leading-[1.6] text-ink-2">
              Build it into your project. utxo AG joins consortia as a work-package partner,
              bringing the vault and the team that wrote it.
            </p>
            <div className={`${CTA} mb-0 mt-auto`}>
              <a className={BTN} href="/use-cases#fit">
                Partnering profile
              </a>
              <a className={BTN} href="/contact">
                Contact us
              </a>
            </div>
          </div>
        </div>
      </Band>

      <Band tone="grey" id="start">
        <SecHead kicker="04 · Get started" title="Two commands" />
        <p className={LEDE}>
          You need a seat token from us to try the live node. This is not a public sandbox.{" "}
          <a href="/contact">Contact us</a>, we send an access token, then install the CLI and the
          agent skill.
        </p>
        <div className={CMD}>
          <span className="select-none text-accent-ink">$</span> pipx install citadel-archive
        </div>
        <div className={CMD}>
          <span className="select-none text-accent-ink">$</span> npx skills add masumi-network/citadel --skill citadel
        </div>
        <div className="mt-[26px] flex flex-wrap gap-x-6 gap-y-2 border-t border-border pt-5 text-[13.5px] text-ink-3">
          <span>
            Need a token?{" "}
            <a className={END_LINK} href="/contact">
              Contact us
            </a>
            .
          </span>
          <span>
            Already have a seat?{" "}
            <a className={END_LINK} href="/login">
              Sign in
            </a>
            .
          </span>
          <span>
            Watch the node on the{" "}
            <a className={END_LINK} href="/info">
              live status page
            </a>
            .
          </span>
          <span>
            Read the{" "}
            <a className={END_LINK} href="https://github.com/masumi-network/Citadel">
              source on GitHub
            </a>
            .
          </span>
        </div>
      </Band>
      <SiteFooter />
      </WithSectionRail>
    </>
  );
}
