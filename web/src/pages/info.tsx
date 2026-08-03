import Head from "next/head";
import type { ReactNode } from "react";

import { CommitChart } from "@/components/commit-chart";
import { HeroBand } from "@/components/hero-band";
import { SectionIndex, type Section } from "@/components/section-index";
import {
  Band,
  CODE,
  Card,
  CARD_P,
  CARD_TAG,
  CHIP_TONE,
  Chip,
  DEEP_B,
  DEEP_H4,
  DEEP_P,
  DEEP_UL,
  DeepLi,
  EYEBROW,
  FOOT_NOTE,
  GoDeeper,
  H1_WIDE,
  LEDE,
  META,
  METRICS,
  Metric,
  PILL,
  PILLARS,
  ROWS,
  ROW_K,
  Row,
  SecHead,
  TLDR_P,
  TLDR_P_LAST,
  Tldr,
  Verified,
} from "@/components/ui";
import { relativeTime, useVaultState, versionLabel } from "@/lib/vault-state";

const SECTIONS: Section[] = [
  { id: "state", label: "Current state" },
  { id: "live", label: "What's live" },
  { id: "releases", label: "Releases" },
  { id: "next", label: "What's next" },
];

/* The figures that are stamped into the markup and then replaced. They are the
   last published values, so a visitor who arrives while the node is unreachable
   reads something true and slightly old rather than a row of dashes. */
const STAMPED = {
  version: "v0.4.0",
  mcpTools: 22,
};

const AS_OF = "Releases are as of v0.4.0, 2026-07-22.";

/* A closing footer that is itself a band is full-bleed, so it carries no top
   margin: the gap an in-column footer wants would show as a stripe of --ground
   between two bands. */
const BAND_FOOTER = "relative mt-0 py-[74px] max-[620px]:py-12";

/* The live half of /info, resolved from one /api/state read.
 *
 * The endpoint and its shape belong to kb/server.py; nothing here asks it for
 * anything it does not already return. Three states matter and are all
 * different: not answered yet (the stamped values, unqualified), answered (live
 * values, dated), and failed (the stamped values, labelled as such).
 */
function useInfoTiles() {
  const { state, settled } = useVaultState();
  const failed = settled && state === null;
  const repo = state?.repo;

  const github = state?.sources?.find((source) => source.type === "github");
  const repositories = state?.totals?.github_repositories || github?.documents || 0;
  const syncedAt = relativeTime(github?.last_synced_at);
  const updatedAt = relativeTime(state?.updated_at);
  const repoAge = relativeTime(repo?.refreshed_at);

  // mcp_tools is computed fresh on every /api/state call (a policy-table
  // length, not a cache), so it carries no "refreshed X ago" note here.
  // Commit and ADR counts used to live here too; they are gone from the page
  // by design (repo trivia, not evidence the system works), but the weekly
  // commit chart stays as recent git activity.
  let repoNote: string;
  if (repo?.source !== "github") {
    // No successful fetch yet: the chart is showing the baked series.
    repoNote = " The commit-activity chart has not refreshed yet.";
  } else if (repo.stale) {
    repoNote = ` The commit-activity chart last refreshed ${repoAge}.`;
  } else {
    repoNote = ` The commit-activity chart refreshed ${repoAge}.`;
  }

  return {
    live: state !== null,
    failed,
    repo,
    version: state ? versionLabel(state.version) || STAMPED.version : STAMPED.version,
    repositories,
    docsSub: state
      ? `GitHub org synced${syncedAt ? ` · ${syncedAt}` : ""}`
      : failed
        ? "GitHub org sync (live data unavailable)"
        : "GitHub org sync",
    mcpTools: typeof repo?.mcp_tools === "number" ? repo.mcp_tools : STAMPED.mcpTools,
    stateUpdated: state
      ? `Live tiles updated${updatedAt ? ` ${updatedAt}` : ""}.${repoNote} ${AS_OF}`
      : null,
    footNote: state
      ? `State-of-the-vault report · live tiles from /api/state${
          updatedAt ? ` (updated ${updatedAt})` : ""
        } · window v0.2.0 → v0.4.0.`
      : null,
  };
}

export default function Info() {
  const tiles = useInfoTiles();

  return (
    <>
      <Head>
        <title>Citadel: State of the Vault</title>
        <meta
          name="description"
          content="Citadel: shared, governed memory for your team and its AI agents. Current state, shipped releases, and roadmap."
        />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>

      <HeroBand current="/info" wide>
        <p className={EYEBROW}>State of the vault · internal report</p>
        <h1 className={H1_WIDE}>
          Shared, governed memory for the team <span className="grad">and its AI agents</span>.
        </h1>
        <div className={META}>
          <span className={PILL}>Railway + PyPI</span>
          <span className={PILL}>Window: v0.2.0 → v0.4.0</span>
        </div>
        <Tldr label="TL;DR: read this, skim the rest">
          <p className={TLDR_P}>
            This is the running node reporting on itself: what is deployed right now, what shipped
            across v0.2.0 → v0.4.0, and what is being built next. If you are new to Citadel, the{" "}
            <a href="/">home page</a> covers what it is, how it&apos;s built, and how to start; this
            page is the numbers.
          </p>
          <p className={TLDR_P_LAST}>
            Across v0.2 → v0.4 we shipped zero-dependency onboarding, autonomous ingestion, Linear
            sync, read-side isolation, and <b>Shared Session Traces</b> (share a dead end,
            reference-only, without leaking private memory). Next: a <b>GitHub App</b> for PR
            context + checks, a <b>Google Chat digest bot</b>, and one <b>central agent hub</b> over
            all org knowledge. Every section below expands. Open the ones you care about.
          </p>
        </Tldr>
      </HeroBand>

      <SectionIndex sections={SECTIONS} />

      <Band tone="white" id="state">
        <SecHead kicker="01 · Current state" title="Where things stand today" />
        <p className={LEDE}>
          Health and scale. The live tiles below refresh from the running node each time this page
          loads; the rest are dated measurements, and the line under them says when each was taken.
        </p>
        <div className={METRICS}>
          <Metric accent value={tiles.version} label="deployed & healthy on Railway" />
          <Metric
            value={
              tiles.live ? (
                <>
                  {tiles.repositories} <small className="text-sm font-normal text-ink-3">repos</small>
                </>
              ) : (
                "—"
              )
            }
            label={tiles.docsSub}
          />
          <Metric accent value="10" label="releases shipped (v0.1.0 → v0.4.0)" />
          <Metric value={tiles.mcpTools} label="MCP tools for agents" />
          <Metric
            value={<span className="text-[19px]">~$55/mo</span>}
            label="to self-host, measured 2026-07-31"
          />
          <Metric value="269 ms" label="median search round-trip, from a client" />
        </div>
        <Verified>
          {tiles.stateUpdated ??
            (tiles.failed ? (
              "Live data unavailable right now. Showing the last published repo figures, as of v0.4.0, 2026-07-22."
            ) : (
              <>
                Live tiles pull from <code className={CODE}>/api/state</code>. MCP tools refresh on
                every load. {AS_OF}
              </>
            ))}
        </Verified>
        {/* Two different kinds of snapshot, and the sentence has to say which
            is which. cost_model.py holds 2026-07-31's Railway averages as a
            constant, so re-running it reprints the same total by construction
            and can never show drift. search_bench.py really does re-measure.
            Calling both "reproducible" flattened that difference, which is how
            a frozen number starts reading as a live one. */}
        <Verified>
          Both are snapshots from 2026-07-31, not live calls, and the method is in the repo&apos;s{" "}
          <a href="https://github.com/masumi-network/Citadel/tree/main/scripts/bench">
            bench harness
          </a>
          . The cost model carries that day&apos;s resource averages in its source, so re-running it
          reprints the figure rather than re-measuring it; the round-trip is re-measurable with{" "}
          <code className={CODE}>search_bench.py</code>, and being a client round-trip it reads
          higher than server-side timing does.
        </Verified>
      </Band>

      <Band tone="grey" id="live">
        <SecHead kicker="02 · Shipped & live" title="What you can use right now" />
        <p className={LEDE}>
          The capabilities that are in production today. The three you&apos;ll touch most are
          session sharing, autonomous capture, and Linear sync.
        </p>
        <div className={PILLARS}>
          <Card title="Shared Session Context">
            <p className={CARD_P}>
              SessionEnd distills how you approached a problem into a private Session Trace. Hit a
              dead end worth flagging? <code className={CODE}>citadel_share_session</code> shares a
              redacted, compacted version to a shared dataset.
            </p>
            <span className={CARD_TAG}>reference-only · never promotes</span>
          </Card>
          <Card title="Autonomous ingestion">
            <p className={CARD_P}>
              A git pre-push hook and Claude Code SessionEnd hook snapshot work to your Node. Both
              are fail-silent. An hourly evolve cycle folds GitHub, Linear, and repo content into
              the graph.
            </p>
            <span className={CARD_TAG}>hooks + hourly evolve</span>
          </Card>
          <Card title="Linear sync">
            <p className={CARD_P}>
              The full workspace syncs to Central; issues assigned to you mirror into your Node as a
              Seat-Scoped Mirror, so an agent answers &quot;what do I need to do?&quot; from your
              memory.
            </p>
            <span className={CARD_TAG}>workspace → Central · yours → Node</span>
          </Card>
          <Card title="Knowledge Mesh & portal">
            <p className={CARD_P}>
              A web UI renders org knowledge as a concept map and a live sync/search/ingest
              timeline. Portal Phase 1: paste your token, land on <b>My Node</b> (stats, checklist,
              deep links).
            </p>
            <span className={CARD_TAG}>Pixel Bastion brand · caller-scoped</span>
          </Card>
        </div>

        <GoDeeper title="How Shared Session Traces actually work">
          <p className={DEEP_P}>
            Shipped in v0.4.0. The goal: let the team learn from each other&apos;s dead ends
            without exposing anyone&apos;s raw private working memory.
          </p>
          <ul className={DEEP_UL}>
            <DeepLi>
              <b className={DEEP_B}>Explicit only.</b> SessionEnd always writes a private Node trace
              (light tier). Sharing is a deliberate call (
              <code className={CODE}>citadel_share_session</code> or{" "}
              <code className={CODE}>POST /api/share-session</code>) and requires an{" "}
              <b className={DEEP_B}>Approved Capture Root</b> (server-side{" "}
              <code className={CODE}>cwd</code> check).
            </DeepLi>
            <DeepLi>
              <b className={DEEP_B}>Compact Session Context.</b> The client distills + redacts the
              trace; the server runs an LLM dead-end refinement only when real tool-error pairs
              exist, then <b className={DEEP_B}>dual-writes</b> to your Node and the shared{" "}
              <code className={CODE}>session-traces</code> dataset.
            </DeepLi>
            <DeepLi>
              <b className={DEEP_B}>Deferred cognify</b> (~5–15 min, coalesced): sharing
              doesn&apos;t block the tool call, and your private Node memory is never enriched.
            </DeepLi>
            <DeepLi>
              <b className={DEEP_B}>Trust demotion.</b> Default{" "}
              <code className={CODE}>citadel_search</code> includes traces with a{" "}
              <code className={CODE}>reference-only</code> tag. Traces never promote to Central and
              never feed the daily improve loop.
            </DeepLi>
          </ul>
        </GoDeeper>

        <GoDeeper title="The autonomous ingestion pipeline">
          <p className={DEEP_P}>
            Zero per-session ceremony. Three capture paths feed your Node; one scheduled cycle keeps
            Central fresh.
          </p>
          <h4 className={DEEP_H4}>Capture (→ your Node)</h4>
          <ul className={DEEP_UL}>
            <DeepLi>
              <b className={DEEP_B}>git pre-push hook</b>: a commit-metadata snapshot on every push
              from an Approved Capture Root.
            </DeepLi>
            <DeepLi>
              <b className={DEEP_B}>SessionEnd hook</b>: distills a coding session and posts it to
              your seat. Reuses the one token you already set. HTTPS-only, refuses redirects,
              fail-silent.
            </DeepLi>
            <DeepLi>
              <b className={DEEP_B}>
                <code className={CODE}>citadel capture</code>
              </b>
              : summarizes each approved root (git metadata + README, never raw files).
            </DeepLi>
          </ul>
          <h4 className={DEEP_H4}>Evolve (→ Central, hourly)</h4>
          <ul className={DEEP_UL}>
            <DeepLi>
              GitHub org digest + repo content sync + Linear sync run as staged subprocesses, then
              cognify runs in-loop on the web service, the single Kuzu writer. Cadence went 6h → 1h
              in v0.2.1.
            </DeepLi>
          </ul>
        </GoDeeper>
      </Band>

      <Band tone="white" id="releases">
        <SecHead kicker="03 · Release history" title="v0.2.0 → v0.4.0" />
        <p className={LEDE}>
          Every tag shipped to PyPI and deployed to Railway. Expand any release for its full notes.
        </p>
        <CommitChart repo={tiles.repo} />
        <Releases />
      </Band>

      <Band tone="grey" id="next">
        <SecHead kicker="04 · The road ahead" title="What's next" />
        <p className={LEDE}>
          Honest status: some of this is in active design, some is still a sketch we&apos;re
          pressure-testing. Nothing here is claimed as shipped.
        </p>
        <div className="mb-[22px] flex flex-wrap gap-2.5">
          <Chip tone="prog">In design</Chip>
          <Chip tone="plan">Brainstorming</Chip>
        </div>
        <div className={ROWS}>
          <Row
            label={<Chip tone="plan">Brainstorming</Chip>}
            title="GitHub App: PR context injection + verifying checks"
          >
            A GitHub App that injects relevant vault context into pull requests and runs{" "}
            <span className={ROW_K}>verifying checks</span> as a PR status, kept{" "}
            <span className={ROW_K}>in sync with Linear</span> and other connected apps, so a review
            sees the same knowledge an agent would. Direction sketched via the modular update-agent
            architecture; not built yet.
          </Row>
          <Row label={<Chip tone="prog">In design</Chip>} title="One central agent hub">
            A single point of access to <span className={ROW_K}>all org knowledge and context</span>
            : an internal update agent plus hosted MCP consolidating GitHub, Citadel search, Linear,
            and future approved sources behind one contract. Repository boundary and initial
            contract are drafted.
          </Row>
          <Row label={<Chip tone="prog">In design</Chip>} title="Google Chat digest bot">
            A daily <span className={ROW_K}>Organization Update Digest</span> to one Google Chat
            space: what changed, what&apos;s open, what merged, plus a cautious source-linked
            &quot;Agent read.&quot; Outbound-only in Phase 1; app-auth and schedule are settled.
            Silent on quiet days.
          </Row>
          <Row
            label={<Chip tone="prog">In design</Chip>}
            title="Structured Knowledge: own the representation"
          >
            Make durable, first-class <span className={ROW_K}>Structured Knowledge</span> the source
            of truth Citadel owns, with the retrieval layer demoted to a rebuildable index. Plus a
            retrieval eval harness (<code className={CODE}>citadel bench</code>) and vault lint (
            <code className={CODE}>citadel lint</code>). This is how the vault stops depending on
            any one engine to hold the truth.
          </Row>
          <Row
            label={<Chip tone="plan">Brainstorming</Chip>}
            title="Session Traces v1.1 + more surfaces"
          >
            Retraction controls (<code className={CODE}>citadel unshare</code>, ~90-day TTL, admin
            hard-delete), overlap-ranked <span className={ROW_K}>prior-work retrieval</span>, and
            more delivery gateways (Agent Messenger, Slack, email, webhook) off the same
            update-agent contract.
          </Row>
        </div>
      </Band>

      <footer className={`${BAND_FOOTER} bg-surface`}>
        <div className="mx-auto max-w-[940px] px-[26px] text-[15px] text-ink-2 max-[620px]:px-4">
          <p>
            Verify any of this yourself with <code className={CODE}>citadel status</code>, or read
            the source at{" "}
            <a href="https://github.com/masumi-network/Citadel">github.com/masumi-network/Citadel</a>
            . Live node: <code className={CODE}>citadel-archive-production.up.railway.app</code>
          </p>
          <p>
            New here? <a href="/">Start on the home page</a>. Building an EU-funded project?{" "}
            <a href="/use-cases">See the use cases and how we partner</a>.
          </p>
          <p className={FOOT_NOTE}>
            {tiles.footNote ?? (
              <>
                State-of-the-vault report · live tiles from <code className={CODE}>/api/state</code>{" "}
                · window v0.2.0 → v0.4.0.
              </>
            )}
          </p>
        </div>
      </footer>
    </>
  );
}

/* The release rail. The vertical line and the node dots are drawn as elements
   rather than pseudo-elements, because a pseudo-element is a stylesheet and
   these are structure. */
function Releases() {
  return (
    <div className="relative pl-[30px]">
      <span className="absolute bottom-3 left-[5px] top-3 w-0.5 bg-[linear-gradient(var(--accent),var(--accent-ink))] opacity-35" />

      <Release version="v0.4.0" date="2026-07-22 · latest" tip open
        title="Shared team memory, the seat portal, and a real brand.">
        <DeepLi>
          <b className={DEEP_B}>Shared Session Traces v1</b>: explicit in-session share via MCP +{" "}
          <code className={CODE}>/api/share-session</code>; Compact Session Context, reference-only
          in search, deferred cognify.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>Multi-agent policy on onboard</b>: the same agent policy installed
          to AGENTS.md, Cursor, Windsurf, GEMINI.md, and Claude Code.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>Seat portal Phase 1</b>: members log in and land on &quot;My
          Node&quot; with doc counts, activity, and a checklist.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>Pixel Bastion brand + analytics panels</b>: CLI mark, README banner,
          favicon, dashboard chrome; CSP-safe charts.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>Security</b>: Obsidian vaults enforce ownership;{" "}
          <code className={CODE}>/api/knowledge/events</code> and{" "}
          <code className={CODE}>/feedback</code> are now caller-scoped; pip-audit CI gate.
        </DeepLi>
      </Release>

      <Release version="v0.3.0" date="2026-07-16"
        title="Read-side privacy release + graph legibility.">
        <DeepLi>
          <b className={DEEP_B}>Mesh read isolation</b>: graph, activity, and document
          drill-down are caller-scoped; seat presence stays universal.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>Knowledge Mesh reads as a concept map</b>: per-hub aggregation,
          human labels, drill-down, kind-filter legend.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>
            <code className={CODE}>citadel activity</code>
          </b>
          : dev-side view of your vault; <code className={CODE}>--watch</code> and a{" "}
          <code className={CODE}>--global</code> seat-presence board.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>Agent-onboarding hardening</b>: seat-bound token mandate, headless
          SKILL runbook, <code className={CODE}>--json</code> error parity.
        </DeepLi>
      </Release>

      <Release version="v0.2.2 – v0.2.3" date="2026-07-02 · 07-07"
        title="Onboarding & token friction, removed.">
        <DeepLi>
          <b className={DEEP_B}>Seat-bound token minting</b>:{" "}
          <code className={CODE}>token create --seat</code> inherits the seat&apos;s role and
          private dataset; interactive picker replaces the service-account footgun.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>
            <code className={CODE}>citadel token set</code> +{" "}
            <code className={CODE}>citadel update</code>
          </b>
          : rotate a token or self-update without re-running onboard; stale-shell auth hints tell
          you the actual fix.
        </DeepLi>
      </Release>

      <Release version="v0.2.0 – v0.2.1" date="2026-06-29"
        title="Top-to-bottom CLI DX overhaul (PyPI + Railway).">
        <DeepLi>
          <b className={DEEP_B}>Seat-scoped ingest that works</b>:{" "}
          <code className={CODE}>ingest</code>/<code className={CODE}>search</code> HTTP-backed by
          default and routed to your seat; inline cognify makes notes searchable immediately.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>First-run onboarding + multi-tool MCP</b>:{" "}
          <code className={CODE}>citadel mcp add</code> wires Cursor, Codex, Gemini, Windsurf;
          guided first run; <code className={CODE}>citadel doctor</code> repairs setup drift.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>Seat / token commands + faster status</b>: concurrent health checks;
          the old TUI folded into <code className={CODE}>citadel status</code>.
        </DeepLi>
        <DeepLi>
          <b className={DEEP_B}>Evolve cadence 6h → 1h</b>: GitHub / Linear / repo sync + cognify
          now run hourly.
        </DeepLi>
      </Release>
    </div>
  );
}

function Release({
  version,
  date,
  title,
  tip,
  open,
  children,
}: {
  version: string;
  date: string;
  title: string;
  tip?: boolean;
  open?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="relative mb-3 last:mb-0">
      <span
        className={`absolute -left-[30px] top-[19px] z-[1] size-3 rounded-full ${
          tip
            ? "bg-accent shadow-[0_0_0_4px_var(--accent-soft)]"
            : "border-2 border-accent bg-ground"
        }`}
      />
      <details
        open={open}
        className="godeeper overflow-hidden border border-border bg-surface open:border-border-2"
      >
        <summary className="flex cursor-pointer items-center gap-3 px-[22px] py-[17px] text-[15px] font-medium text-ink focus-visible:-outline-offset-2 focus-visible:outline-2 focus-visible:outline-accent">
          <span className="chev shrink-0 text-[13px] text-accent-ink transition-transform duration-200">
            ▸
          </span>
          <span className="flex flex-wrap items-baseline gap-3">
            <span className="font-mono text-[15px] font-semibold text-accent-ink">{version}</span>
            <span className="font-mono text-[11.5px] text-ink-3">{date}</span>
            <span className="mt-0.5 basis-full text-[13.5px] text-ink-2">{title}</span>
          </span>
        </summary>
        <div className="px-6 pb-6 pt-0.5">
          <ul className={DEEP_UL}>{children}</ul>
        </div>
      </details>
    </div>
  );
}
