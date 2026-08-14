import Head from "next/head";

import { HeroBand } from "@/components/hero-band";
import { SectionIndex, type Section } from "@/components/section-index";
import {
  BAND,
  BAND_IN,
  BTN,
  BTN_PRIMARY,
  Band,
  CARD_P,
  CARD_ROW,
  CARD_TAG,
  CTA,
  Card,
  Chip,
  DEEP_B,
  DEEP_UL,
  DeepLi,
  EYEBROW,
  FOOT_NOTE,
  GoDeeper,
  H1_WIDE,
  HERO_STRIP,
  HeroFact,
  LEDE,
  META,
  PILL,
  PILLARS,
  PLAINLIST,
  ROWS,
  ROW_H3,
  ROW_K,
  Row,
  SecHead,
  TLDR_P,
  Verified,
} from "@/components/ui";

const SECTIONS: Section[] = [
  { id: "teams", label: "Use cases" },
  { id: "fit", label: "Where we fit" },
  { id: "can", label: "What we can and can't do" },
  { id: "honest", label: "What we don't claim" },
  { id: "wp", label: "Work package" },
  { id: "ask", label: "The ask" },
  { id: "verify", label: "Check us" },
];

const TEAM_CASES = [
  {
    n: "01",
    title: "The first two weeks of a new engineer",
    body: "Why is it built this way, what was tried before, who decided. A new joiner asks the vault and gets an answer with the commit, issue, or decision record behind it, instead of interrupting the three people who remember.",
    tag: "onboarding without a tax on the team",
  },
  {
    n: "02",
    title: "Agents that know your codebase",
    body: "Claude Code, Cursor, or your own agent connects over MCP and answers from your team's memory under your seat and your read scope. The agent stops guessing at context it was never given.",
    tag: "MCP · same seat, same isolation",
  },
  {
    n: "03",
    title: "One question across every tool",
    body: 'The answer to "what happened with X" is usually split across a commit, a ticket, a document, and a coding session nobody wrote down. One search reads all of them and tells you which memory answered.',
    tag: "code · tickets · docs · sessions",
  },
  {
    n: "04",
    title: "Evidence you can hand to someone else",
    body: "Every retrieved item carries a content fingerprint and points back at where it came from, and every read and write is logged. When a report or a review needs proof, it is already assembled.",
    tag: "source linked · audited",
  },
] as const;

export default function UseCases() {
  return (
    <>
      <Head>
        <title>Citadel</title>
        <meta
          name="description"
          content="What teams run Citadel for, and how utxo AG joins EU consortia as a work-package partner: an open-source system that keeps a project's records organised, access-controlled and auditable."
        />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>

      <HeroBand current="/use-cases" wide>
        <p className={EYEBROW}>Use cases · teams and consortia</p>
        <h1 className={H1_WIDE}>
          What people run Citadel for, <span className="grad">and where we partner</span>.
        </h1>
        <div className={`${META} mb-8`}>
          <span className={PILL}>utxo AG · Zug, Switzerland</span>
          <span className={PILL}>Looking for: one work package</span>
        </div>
        <dl className={HERO_STRIP}>
          <HeroFact kicker="Team">
            Citadel gathers your code, tickets, documents, and sessions, each with a{" "}
            <b>content fingerprint</b>.
          </HeroFact>
          <HeroFact kicker="Consortium">
            utxo AG brings this system to an EU project as one work package.
          </HeroFact>
        </dl>
      </HeroBand>

      <SectionIndex sections={SECTIONS} />

      <Band tone="white" id="teams">
        <SecHead kicker="Use cases" title="Four things teams run it for" />
        <p className={LEDE}>
          None of these need anyone to file anything. The capture already happened while people were
          working.
        </p>
        <div className="grid grid-cols-2 gap-px border border-border bg-border max-[620px]:grid-cols-1">
          {TEAM_CASES.map((item) => (
            <article key={item.n} className="bg-surface px-6 py-6 max-[620px]:px-4 max-[620px]:py-5">
              <p className="mb-3 font-mono text-[11px] tracking-[.16em] text-accent-ink">{item.n}</p>
              <h3 className="m-0 mb-2.5 text-base font-semibold">{item.title}</h3>
              <p className={CARD_P}>{item.body}</p>
              <span className={CARD_TAG}>{item.tag}</span>
            </article>
          ))}
        </div>
        <Verified>
          The rest of this page is the same system offered to EU consortia as a work-package
          partner. If you are here as a team rather than a coordinator, <a href="/">the home page</a>{" "}
          and <a href="/info">the live status</a> are the shorter read.
        </Verified>
      </Band>

      <Band tone="grey" id="fit">
        <SecHead kicker="Partnering · where we fit" title="Two jobs consortia give us" />
        <p className={LEDE}>
          The software is the same in both. Only what we connect it to changes.
        </p>
        <div className={PILLARS}>
          <Card title="Proving compliance without the paper chase">
            <p className={CARD_P}>
              The evidence a report needs is scattered across a dozen systems, and by the time it is
              collected it is out of date. We keep it gathered continuously, each item traceable
              back to where it came from, so a report can be produced, and defended, from records
              rather than recollection.
            </p>
            <span className={CARD_TAG}>gather → check → report</span>
          </Card>
          <Card title="Shared knowledge across partner organisations">
            <p className={CARD_P}>
              Five organisations over 24 months usually run their knowledge on shared drives and
              email. We give each partner a private space and the consortium a shared one, with
              clear rules for what moves between them and a log of who saw what.
            </p>
            <span className={CARD_TAG}>private per partner · shared per project</span>
          </Card>
        </div>
      </Band>

      <Band tone="white" id="can">
        <SecHead kicker="What we can and can't do" title="Three honest categories" />
        <p className={LEDE}>
          Rather than one long capability list, here is the only distinction that matters to you
          when you are assembling a consortium: what already works, what we would build with project
          funding, and what you need somebody else for.
        </p>
        <div className={ROWS}>
          <div className={CARD_ROW}>
            <div className="flex pt-[3px]">
              <Chip tone="ship">Works now</Chip>
            </div>
            <div>
              <h3 className={ROW_H3}>
                Running in production today: you can check it before committing
              </h3>
              <p className={CARD_P}>
                Records flow in automatically from code repositories, issue trackers and documents.
                Every item carries a <span className={ROW_K}>digital fingerprint</span> so you can
                prove it hasn&apos;t been altered. Access is controlled per person and per
                organisation, and every read and write is logged. People use a command-line tool or
                a web page; AI assistants connect through a standard interface.
              </p>
              <GoDeeper title="The technical version">
                <ul className={DEEP_UL}>
                  <DeepLi>Apache-2.0, public repository, CI on every push.</DeepLi>
                  <DeepLi>
                    Live hosted node with a public state report and a no-secrets status endpoint.
                  </DeepLi>
                  <DeepLi>Seat-bound tokens, role-scoped tool access, per-call audit.</DeepLi>
                  <DeepLi>
                    Read isolation between each seat&apos;s private Node and shared Central, with a
                    multi-gate promotion engine controlling what moves between them.
                  </DeepLi>
                  <DeepLi>
                    SHA-256 content digest returned on every retrieved item; secret scanning on
                    every write path.
                  </DeepLi>
                  <DeepLi>
                    Production connectors for a GitHub organisation, repository content and an issue
                    tracker.
                  </DeepLi>
                  <DeepLi>
                    Three access surfaces: hosted MCP, a zero-dependency CLI, and an HTTP API.
                  </DeepLi>
                  <DeepLi>Basic conflict detection across ingested documents.</DeepLi>
                </ul>
              </GoDeeper>
            </div>
          </div>

          <div className={CARD_ROW}>
            <div className="flex pt-[3px]">
              <Chip tone="prog">We&apos;d build</Chip>
            </div>
            <div>
              <h3 className={ROW_H3}>Specified in our roadmap, funded by the project</h3>
              <p className={CARD_P}>
                Making every single item traceable back to the exact document and moment it came
                from. Spotting when two records <span className={ROW_K}>disagree</span> and keeping
                both rather than quietly overwriting one. Spotting when a record has gone{" "}
                <span className={ROW_K}>stale</span>. Writing the rules a regulation implies in a
                form software can check automatically, then reporting what evidence is still
                missing. Connectors for whatever systems your pilot actually uses.
              </p>
              <GoDeeper title="The technical version">
                <ul className={DEEP_UL}>
                  <DeepLi>
                    <b className={DEEP_B}>Attested per-item provenance</b>: source-snapshot
                    pointers, confidence and match type on every retrieved item, with unresolvable
                    claims stripped rather than shown.
                  </DeepLi>
                  <DeepLi>
                    <b className={DEEP_B}>Claim-level contradiction ledger</b>: records both sides
                    of a disagreement instead of silently overwriting.
                  </DeepLi>
                  <DeepLi>
                    Durable structured knowledge owned by Citadel, with the search index rebuildable
                    underneath it.
                  </DeepLi>
                  <DeepLi>
                    Machine-readable requirement models, and mapping from evidence to the obligation
                    it satisfies.
                  </DeepLi>
                  <DeepLi>
                    Retrieval benchmark and vault lint running in CI: the project&apos;s measurable
                    quality indicators.
                  </DeepLi>
                  <DeepLi>
                    Export packages carrying source links, timestamps and content digests.
                  </DeepLi>
                </ul>
              </GoDeeper>
            </div>
          </div>

          <div className={CARD_ROW}>
            <div className="flex pt-[3px]">
              <Chip tone="plan">Not us</Chip>
            </div>
            <div>
              <h3 className={ROW_H3}>You need another partner for these</h3>
              <p className={CARD_P}>
                Techniques for analysing data without exposing it. Digital identity wallets,
                electronic seals and other trust services. Connectors into the European data spaces,
                and the shared vocabularies a particular industry uses. We work <i>with</i> all of
                these. We don&apos;t build them, and we would rather say so now than at the
                technical review.
              </p>
            </div>
          </div>
        </div>
      </Band>

      <Band tone="grey" id="honest">
        <SecHead kicker="What we don't claim" title="Where we would fail a technical review" />
        {/* Accent bar stays: this is the section a coordinator should read
            first. The old 65ch box left the right of the 1200px measure empty,
            so shipped vs still-open sit in two columns instead. */}
        <div className="relative border border-border bg-surface">
          <span className="absolute inset-y-0 left-0 w-[3px] bg-accent" />
          <div className="px-6 py-5 pl-7 max-[620px]:px-4 max-[620px]:py-4 max-[620px]:pl-5">
            <p className="mb-2.5 text-[11px] font-semibold uppercase tracking-[.16em] text-accent-ink">
              Read this before you believe the rest
            </p>
            <p className={TLDR_P}>
              Partner profiles list capabilities and stop. These are the gaps, checked against the
              live system rather than copied from our own documentation. A few items that used to
              sit here have shipped. They are listed first so this page does not keep advertising
              them as holes.
            </p>
            <div className="mt-4 grid grid-cols-2 gap-x-12 gap-y-6 max-[620px]:grid-cols-1 max-[620px]:gap-y-0">
              <div className="max-[620px]:pb-6">
                <p className="mb-2 text-[11px] font-semibold uppercase tracking-[.16em] text-good">
                  Shipped since v0.3
                </p>
                <ul className={`${PLAINLIST} mt-1`}>
                  <li>
                    <b className={DEEP_B}>Mesh read isolation</b> (v0.3.0): graph, activity, and
                    document drill-down are caller-scoped.
                  </li>
                  <li>
                    <b className={DEEP_B}>Shared Session Traces</b> (v0.4.0): share a dead end,
                    reference-only, without leaking private memory.
                  </li>
                  <li>
                    <b className={DEEP_B}>Next public pages</b> (v0.4.1 preview at{" "}
                    <code className="bg-surface-2 px-1.5 py-[1.5px] font-mono text-[.84em] text-ink">
                      /next
                    </code>
                    ): this site, as a static export, not yet the default public surface.
                  </li>
                </ul>
              </div>
              <div className="max-[620px]:border-t max-[620px]:border-border max-[620px]:pt-6">
                <p className="mb-2 text-[11px] font-semibold uppercase tracking-[.16em] text-warn">
                  Still open, and on the roadmap
                </p>
                <ul className={`${PLAINLIST} mt-1`}>
                  <li>
                    <b className={DEEP_B}>Items are fingerprinted, but not yet attested.</b> Search
                    can say a source is linked. That is not per-item provenance with a confidence
                    and a match type on every record. A project would fund that, and we would rather
                    fund it than claim it.
                  </li>
                  <li>
                    <b className={DEEP_B}>Disagreement detection is shallow.</b> Today it compares
                    document titles. Comparing the actual claims is designed, and specified, but not
                    built.
                  </li>
                  <li>
                    <b className={DEEP_B}>We are proven at team scale, not national scale.</b> The
                    system runs daily for a working team. Handling a country&apos;s reporting volume
                    is real project work, and we scope it as such.
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      </Band>

      <Band tone="white" id="wp">
        <SecHead kicker="Draft work package" title="Written so you can lift it and edit" />
        <p className={LEDE}>
          A coordinator assembling a proposal under time pressure needs text, not a brochure. Six
          tasks across 24 months, all negotiable against the structure you already have.
        </p>
        <div className={ROWS}>
          <Row label={<Chip tone="plan">M1–M4</Chip>} title="Work out what has to be proved">
            Turn the obligations you are targeting into rules software can check, and agree the
            interfaces with the partners supplying data and the partner building the reporting
            front-end.
          </Row>
          <Row label={<Chip tone="plan">M3–M10</Chip>} title="Connect the pilot's systems">
            Build the connectors that pull records out of whatever the pilot actually runs on, with
            access rules and secret scanning applied as they arrive.
          </Row>
          <Row label={<Chip tone="plan">M5–M14</Chip>} title="Make every record traceable">
            Each item points back to the document and moment it came from, with a confidence level.
            Anything that can&apos;t be traced is dropped rather than presented as evidence.
          </Row>
          <Row label={<Chip tone="plan">M8–M16</Chip>} title="Check the evidence holds up">
            Flag records that disagree, records that have gone stale, and obligations with no
            evidence behind them at all.
          </Row>
          <Row
            label={<Chip tone="plan">M10–M20</Chip>}
            title="Control access and produce the package"
          >
            Per-organisation access rules, a log of every read and write, and an export carrying
            source links, timestamps and fingerprints for onward transmission.
          </Row>
          <Row label={<Chip tone="plan">M6–M24</Chip>} title="Measure it, continuously">
            Automated quality checks running throughout the project, producing the numbers your
            reporting needs rather than a one-off assessment at the end.
          </Row>
        </div>
        <Verified>
          <b className={DEEP_B}>We depend on other partners for</b> data capture from the
          pilot&apos;s systems, privacy techniques, identity and trust services, and the industry
          vocabulary. <b className={DEEP_B}>We provide</b> the store everything lands in, the way to
          query it, and the export that leaves it.
        </Verified>
      </Band>

      <Band tone="grey" id="ask">
        <SecHead kicker="The ask" title="One work package, scoped to your call" />
        {/* The four-tile metrics block that used to sit here is gone. Scope and
            cost are worked out after a contact request, not published: a figure
            on a public page is wrong for almost every project that reads it.
            Two facts from those tiles survive in the rows below rather than
            vanishing with them — Apache-2.0 as a delivery commitment in Role,
            and the extra country in Eligibility, which already said it. */}
        <p className={LEDE}>
          Effort and cost depend on the call, the pilot systems, and which partners cover what
          around us. We work those out with you rather than publishing a figure that would be wrong
          for your project. Send us the call and the gap, and we come back with a costed work
          package written against your structure.
        </p>
        <div className={ROWS}>
          <Row label={<Chip tone="ship">Role</Chip>} title="A partner running one work package">
            We run the work package and hand over everything under Apache-2.0, plus support on the
            open-source release and dissemination. If your consortium is already full, we are just
            as happy as an <span className={ROW_K}>associated partner</span> or a subcontractor, and
            what we build does not change.
          </Row>
          <Row
            label={<Chip tone="ship">Eligibility</Chip>}
            title="Switzerland is inside the Digital Europe Programme"
          >
            Since 2025, Swiss organisations take part as full partners and can even lead, across{" "}
            <span className={ROW_K}>Specific Objectives 1, 2, 4 and 5</span>, though not 3 or 6. So
            we add a country to your count instead of complicating it.
          </Row>
          <Row
            label={<Chip tone="prog">Co-funding</Chip>}
            title="We apply for Swiss money toward our own share"
          >
            Swiss participants can ask SERI to co-fund the part the EU grant doesn&apos;t cover,
            which lowers what the consortium carries on our line. It is granted on request, so we
            treat it as likely rather than certain.
          </Row>
        </div>
      </Band>

      <Band tone="white" id="verify">
        <SecHead kicker="Check us" title="Verify before you commit to anything" />
        <p className={LEDE}>
          The page you are reading is served by the system it describes. Nothing here needs to be
          taken on trust.
        </p>
        <div className={PILLARS}>
          <Card title="The source code">
            <p className={CARD_P}>
              <a href="https://github.com/masumi-network/Citadel">
                github.com/masumi-network/Citadel
              </a>{" "}
              is Apache-2.0, with the full commit and test history, and the written record of every
              significant design decision.
            </p>
            <span className={CARD_TAG}>read every line</span>
          </Card>
          <Card title="The running system">
            <p className={CARD_P}>
              <a href="/info">Its own live status report</a>: current numbers, what shipped when,
              and what is planned, generated by the system rather than written about it.
            </p>
            <span className={CARD_TAG}>served by the node</span>
          </Card>
        </div>
      </Band>

      <section className={`${BAND} bg-accent-soft`} id="talk">
        <div className={BAND_IN}>
          <SecHead kicker="Talk to us" title="Tell us the call and the gap" />
          <p className={LEDE}>
            Send the call identifier, the topic, and the piece you need covered. We reply with a
            work package written against your structure rather than ours, usually within two working
            days.
          </p>
          <div className={CTA}>
            <a className={BTN_PRIMARY} href="/contact">
              Contact us
            </a>
            <a className={BTN} href="/info">
              See the live status
            </a>
          </div>
          <footer className="mt-[34px] text-[15px] text-ink-2">
            <p className={FOOT_NOTE}>
              Use cases and partnering profile · the live pill above reads from{" "}
              <code className="bg-surface-2 px-1.5 py-[1.5px] font-mono text-[.84em] text-ink">
                /api/state
              </code>{" "}
              on this node.
            </p>
          </footer>
        </div>
      </section>
    </>
  );
}
