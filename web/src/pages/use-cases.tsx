import Head from "next/head";

import { HeroBand } from "@/components/hero-band";
import { SectionIndex, type Section } from "@/components/section-index";
import {
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
  TLDR_P_LAST,
  Tldr,
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

export default function UseCases() {
  return (
    <>
      <Head>
        <title>Citadel use cases and partnering</title>
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
        <div className={META}>
          <span className={PILL}>utxo AG · Zug, Switzerland</span>
          <span className={PILL}>Looking for: one work package</span>
        </div>
        <Tldr label="The short version">
          <p className={TLDR_P}>
            <b>For a team.</b> Citadel gathers the records you already produce, code, tickets,
            documents, coding sessions, and keeps them in one place where each item is{" "}
            <b>traceable to its source</b>, only visible to the people allowed to see it, and logged
            every time it is read or changed. You and your agents ask it questions instead of asking
            each other the same ones again.
          </p>
          <p className={TLDR_P_LAST}>
            <b>For a consortium.</b> That same system is what utxo AG brings to an EU project, as a
            partner running one work package. We are a Swiss company, and Switzerland is part of the
            Digital Europe Programme, so we count toward your minimum number of countries rather
            than sitting outside it. We apply for Swiss national co-funding toward our own share,
            and we scope the work package with you rather than off a price list.
          </p>
        </Tldr>
      </HeroBand>

      <SectionIndex sections={SECTIONS} />

      <Band tone="white" id="teams">
        <SecHead kicker="Use cases" title="Four things teams run it for" />
        <p className={LEDE}>
          None of these need anyone to file anything. The capture already happened while people were
          working.
        </p>
        <div className={PILLARS}>
          <Card title="The first two weeks of a new engineer">
            <p className={CARD_P}>
              Why is it built this way, what was tried before, who decided. A new joiner asks the
              vault and gets an answer with the commit, issue, or decision record behind it, instead
              of interrupting the three people who remember.
            </p>
            <span className={CARD_TAG}>onboarding without a tax on the team</span>
          </Card>
          <Card title="Agents that know your codebase">
            <p className={CARD_P}>
              Claude Code, Cursor, or your own agent connects over MCP and answers from your
              team&apos;s memory under your seat and your read scope. The agent stops guessing at
              context it was never given.
            </p>
            <span className={CARD_TAG}>MCP · same seat, same isolation</span>
          </Card>
          <Card title="One question across every tool">
            <p className={CARD_P}>
              The answer to &quot;what happened with X&quot; is usually split across a commit, a
              ticket, a document, and a coding session nobody wrote down. One search reads all of
              them and tells you which memory answered.
            </p>
            <span className={CARD_TAG}>code · tickets · docs · sessions</span>
          </Card>
          <Card title="Evidence you can hand to someone else">
            <p className={CARD_P}>
              Every retrieved item carries a content fingerprint and points back at where it came
              from, and every read and write is logged. When a report or a review needs proof, it is
              already assembled.
            </p>
            <span className={CARD_TAG}>source linked · audited</span>
          </Card>
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
        {/* The TL;DR panel does double duty here: this is the section a
            coordinator should read first, so it gets the same accent bar. */}
        <div className="relative overflow-hidden border border-border bg-surface px-8 py-7">
          <span className="absolute inset-y-0 left-0 w-[3px] bg-[linear-gradient(var(--accent),var(--accent-ink))]" />
          <p className="mb-3.5 text-xs font-semibold uppercase tracking-[.16em] text-accent-ink">
            Read this before you believe the rest
          </p>
          <p className={TLDR_P}>
            Partner profiles list capabilities and stop. These are the gaps, checked against the
            live system rather than copied from our own documentation.
          </p>
          <ul className={`${PLAINLIST} mt-1 text-[15.5px]`}>
            <li>
              <b className={DEEP_B}>Items are fingerprinted, but not yet traceable.</b> We can prove
              a record hasn&apos;t changed; we cannot yet show you, on every record, exactly which
              document it came from. That is the single biggest thing a project would fund, and we
              would rather fund it than claim it.
            </li>
            <li>
              <b className={DEEP_B}>Disagreement detection is shallow.</b> Today it compares
              document titles. Comparing the actual claims is designed and specified, but not built.
            </li>
            <li>
              <b className={DEEP_B}>We are proven at team scale, not national scale.</b> The system
              runs daily for a working team. Handling a country&apos;s reporting volume is real
              project work, and we scope it as such.
            </li>
          </ul>
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

      <section className="relative py-[74px] max-[620px]:py-12 bg-accent-soft" id="talk">
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
