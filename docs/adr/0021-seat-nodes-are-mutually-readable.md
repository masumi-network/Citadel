# Seat Nodes Are Mutually Readable; Promotion Guards Curation, Not Secrecy

- Status: Proposed. The direction is decided; this document is not yet sound
  enough to accept. Consent has no success criterion (see Decision), the
  post-flip search arity depends on a filter primitive ADR-0020 says does not
  exist, and the blast-radius analysis below is new and unreviewed.
- Date: 2026-08-04
- Supersedes: the READ-isolation half of
  [ADR-0009](0009-mesh-read-isolation-presence-vs-content.md) and
  [ADR-0003](0003-seat-node-central-private-memory.md)'s "reads never cross seat
  nodes". Both remain in force for everything else they say. Also supersedes
  [ADR-0011](0011-shared-session-traces.md)'s statement that "reads never cross
  seat Nodes holds literally and without exception", and the CONTEXT.md glossary
  entries stating that seats are not intended to read each other's Nodes. Those
  are superseded on merge and must be revised in the same change.
- Unaffected: [ADR-0007](0007-seat-capture-promotion-write-policy.md). The write
  path does not change.
- Relates to: [ADR-0011](0011-shared-session-traces.md),
  [ADR-0012](0012-attested-trust-vs-content-hint.md),
  [ADR-0020](0020-knowledge-index-is-an-exact-scan.md).

ADR-0009 considered exactly this option and rejected it. Its Considered Options
list names the "Transparency amendment (glass walls): let every member see every
seat's content and supersede ADR-0003", rejected because "it breaks the Node
privacy promise already made to the team mid-rollout and contradicts the pentest
posture." Both objections are answered below, and the first is answered by doing
something rather than by arguing.

**Why the boundary is being removed.** The value of a shared vault is
discoverability, and every read boundary is a tax on it. For a team of about
twelve people building one product, that tax is close to pure loss: the answer
to "has anyone hit this before" should be yes and here it is, not silence
because the person who knew sits behind a wall.

The architecture already concedes the point. ADR-0011 built an entire third
tier to move content across this boundary: a `session-traces` dataset, a
reference-only trust demotion, deferred coalesced cognify, an author-seat stamp.
That machinery exists solely to get around the default. Sharing is also a
manual act, "never automatically in v1", and manual sharing acts have poor
adoption. The likely present state is full privacy cost with close to zero sharing
benefit.

**Why it is safe enough.** The ingest secret scan is not tier-gated:
`kb/learning.py:105-117` runs `scan_text_entries` over the whole document
*before* the tier branch at `:118`, so light-tier seat memory passes the same
block gate as org-bound content. Opening Nodes exposes sensitivity, not
credentials. Those are different risks and only the second is an incident.

**What promotion was really for.** With Nodes mutually readable, promotion no
longer moves content across a visibility boundary. It moves content across a
curation and trust boundary: unreviewed working memory on one side, synthesized
and security-reviewed **Structured Knowledge** on the other. That was always the
honest justification. Secrecy was doing work it should not have been asked to do,
and promotion becomes more load-bearing under this ADR, not less.

**Decision**

- Seat **Node** content is readable by every **Seat**. Seats remain
  organisational and attribution units; they stop being walls.
- The write path is unchanged. Seats write only to their own **Node**; **Central**
  is reached only through the **Promotion Agent** (ADR-0007). Read access is not
  write access, and the same enforcement chokepoint must be split into separate
  read and write predicates rather than simply opened.
- Cross-seat **Node** hits carry a trust demotion. Node content is light tier:
  indexed, never enriched, never reviewed. It is consultable prior work with no
  claim of being true, which is what ADR-0012's attested tiers already express.
  Readable is not authoritative, and a reader must be able to see the difference
  at a glance.
- **Capture Root Tags become the only privacy boundary.** `personal` and
  custom-tagged roots stay private; `org-work` roots become org-readable. The
  boundary moves from "whose Node is it" to "what did you declare this directory
  to be", which is both more permissive and more defensible, because the person
  who owns the content made the declaration.
- **The privacy promise is retired explicitly, not quietly, and consent has a
  definition.** ADR-0009 rejected this option partly because a promise was made
  to the team mid-rollout. Withdrawing it requires all of: every current seat
  holder is told in writing before any code changes; each is given a stated
  window to re-tag or purge their own **Node**; and each acknowledges. A seat
  holder who declines keeps their **Node** private, which the per-deployment
  setting already permits at seat granularity, so one refusal does not veto the
  change and does not force anyone to publish. Silence is not consent, and an
  unacknowledged seat stays private by default.
- **This is masumi's policy, not the product's only policy.** The isolation
  mechanism is kept and the default is what changes. Any future deployment
  serving another organisation may need genuine isolation, so the behaviour is a
  per-deployment setting.
- Visibility opens **going forward only**. Content already captured was written
  under an expectation of privacy, and flipping the default retroactively
  exposes material people would have tagged differently had they known. See the
  sequencing note on the backlog below.

**On the pentest posture, and on blast radius**

Nothing here weakens authentication, the dataset allowlist as a *write* gate,
the no-existence-oracle property of drill-down, or **Seat Presence**. In terms
of which predicate changes, the answer is one: whether a **Seat** may read
another **Seat**'s `org-work` content.

That framing is not the real objection, and an earlier draft of this ADR stopped
there. ADR-0009's pentest concern is **blast radius**, and this decision
increases it. What changes is the unit of read access: under this proposal one
seat's access covers the organisation's whole `org-work` working memory, where
the design it replaces scopes a seat to its own **Node** plus **Central**. The
attack surface is unchanged; the consequence of anything going wrong on it is
larger. That is the actual cost of the decision and it is accepted deliberately
rather than argued away.

Three things follow, and they are conditions of the change rather than optional
hardening. Token lifetime and rotation must be revisited, because a
never-expiring token would then hold a materially larger prize; long-lived
tokens are the sharpest case and are tracked separately. Detection matters more
than it did: reading an entire organisation's memory through one token should be
visible in telemetry, which is one of the few places this decision and ADR-0022
genuinely reinforce each other. And the carve-out has to get stronger, because
it inherits the whole burden the seat boundary used to carry.

A reader weighing this ADR should weigh that trade explicitly: the vault becomes
more useful to twelve people and more valuable to one attacker.

**Consequences**

- **Enforcing the carve-out is the largest piece of work this decision
  creates, and it is not yet built.** Capture Root Tags today inform promotion
  decisions; making them a read boundary requires a partition primitive that
  does not exist, plus origin labelling on write paths that currently carry
  none. The gap analysis and the specific call sites are recorded in the private
  audit, not here. This is the reason the visibility flip is sequenced last.
- **Some already-captured content cannot be classified retroactively from the
  data alone.** Origin is not recorded uniformly across write paths, so a
  server-side sweep cannot size the carve-out on its own. Owner review is
  therefore part of the sequence rather than an optional extra.
- **The backlog is handled by a line in time plus opt-in.** Everything ingested
  before the provenance work stays seat-private; everything after is governed by
  tags. Each seat holder then reviews their own **Node** and releases what they
  choose. This avoids both an impossible audit and a retroactive consent
  violation, and it puts the judgement with the person who created the content.
- **The search fan-out shrinks to two, not to one, and even that is not free.**
  `resolve_search_datasets` (`kb/server.py:1702-1727`) returns three datasets for
  a seat today. The naive reading of this ADR is to add every seat, which would
  multiply ADR-0020's exact-scan cost linearly. But the opposite reading, that
  the fan-out collapses to a single unfiltered scan, is also wrong and an earlier
  draft of this ADR claimed it. "Everything is readable" is false under this
  ADR's own decisions at every point in time after the flip: `personal` roots
  stay private, and the whole pre-flip backlog stays seat-private permanently.
  A reader's set is therefore the shared `org-work` pool plus their own private
  partition, which is not one contiguous region. One unfiltered scan would
  over-return other seats' private rows. The honest floor is **two** scans, or
  one scan plus a vector-layer filter. ADR-0020 establishes that no filter
  currently reaches the vector layer, so the cheaper option's prerequisite does
  not exist yet. The post-flip arity must be settled before ADR-0020's latency
  model can be trusted, because that model assumes an arity nobody has fixed.
- **The specification stops having a single shape.** For `org-work` content
  the target behaviour becomes mutual readability; for the private partition it
  stays exclusion. One chokepoint has to express both, and neither can be
  asserted until the partition exists. That asymmetry is precisely why the
  partition must be built before the flip, and why this ADR does not license
  relaxing anything ahead of that sequence.
- **`session-traces` is reframed, not retired.** Sharing stops being a
  visibility act, but the tier still provides enrichment (dead-end distillation)
  and an attested demotion that raw **Node** traces do not carry. Retiring it
  before extending demotion to seat datasets would make cross-seat consumption
  *less* honest than today. Note also that ADR-0011's TTL and `citadel unshare`
  were never implemented, so the retraction story it describes does not exist.
- **`author_seat` needs a source that survives this change, and the dataset
  name is not it.** Today it is parsed from the body, which is forgeable. Reading
  it from the dataset name would be attested, but only while each seat has its
  own dataset; if `org-work` content merges into one shared pool, the dataset
  name stops identifying an author. Attribution must therefore come from a
  per-document field written at ingest, not from the partition layout. This is
  unresolved and blocks the traceability the decision is partly motivated by.
- **Tests are split, not inverted.** The same chokepoint gates reads and writes,
  and several tests assert both in one body. Inverting them wholesale would
  silently delete write-isolation coverage, which is this codebase's documented
  failure mode. Every read assertion that flips must leave its write assertion
  standing.
- **Ordering.** Consent and this ADR first; then server-side root tags; then
  close the labelling gaps on the session, push and MCP paths; then build the
  private partition; then the pre-flight audit and migration; then a secret
  re-scan of Node content at current block severity, because the ingest scan
  becomes the last gate before org-wide exposure; then read-time trust demotion;
  then split the allowlist predicate and flip. Each step is independently
  verifiable, and the flip is last.
