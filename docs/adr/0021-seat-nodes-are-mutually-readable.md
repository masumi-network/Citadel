# Seat Nodes Are Mutually Readable; Promotion Guards Curation, Not Secrecy

- Status: Accepted as a decision. Implementation is gated on the sequence in
  Consequences, and the visibility flip must not ship before it.
- Date: 2026-08-04
- Supersedes: the READ-isolation half of
  [ADR-0009](0009-mesh-read-isolation-presence-vs-content.md) and
  [ADR-0003](0003-seat-node-central-private-memory.md)'s "reads never cross seat
  nodes". Both remain in force for everything else they say.
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
- **The privacy promise is retired explicitly, not quietly.** ADR-0009 rejected
  this option partly because a promise was made to the team mid-rollout. That
  promise is withdrawn by telling the team before anything changes and giving
  them the chance to re-tag, not by shipping a flag. Consent is a precondition.
- **This is masumi's policy, not the product's only policy.** The isolation
  mechanism is kept and the default is what changes. Any future deployment
  serving another organisation may need genuine isolation, so the behaviour is a
  per-deployment setting.
- Visibility opens **going forward only**. Content already captured was written
  under an expectation of privacy, and flipping the default retroactively
  exposes material people would have tagged differently had they known. See the
  sequencing note on the backlog below.

**On the pentest posture**

ADR-0009's second objection stands and is answered by scope, not by dismissal.
Nothing here weakens authentication, the dataset allowlist as a *write* gate,
the no-existence-oracle property of drill-down, or **Seat Presence**. What
changes is one predicate: whether a **Seat** may read another **Seat**'s
`org-work` content. The security posture that must get *stronger* is the
carve-out, because it inherits the whole burden the seat boundary used to carry.

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
- **The search fan-out should collapse, not multiply.** `resolve_search_datasets`
  (`kb/server.py:1702-1727`) returns three datasets for a seat today. The naive
  reading of this ADR is to add every seat, which would multiply ADR-0020's
  exact-scan cost linearly. The correct reading is the opposite: if everything is
  readable, partitioning the *search* buys nothing, and `dataset` becomes a
  per-document attribute for provenance and filtering. All datasets already share
  one `DocumentChunk_text` collection with no filter passed, so the partition is
  already logical rather than physical. This is ADR-0020's remedy reached from
  the other direction.
- **Some behaviour previously recorded as defective becomes the
  specification** for `org-work` content, while remaining defective with respect
  to the private partition. That asymmetry is precisely why the partition must
  exist before the flip, and why this ADR does not license relaxing anything
  ahead of that sequence.
- **`session-traces` is reframed, not retired.** Sharing stops being a
  visibility act, but the tier still provides enrichment (dead-end distillation)
  and an attested demotion that raw **Node** traces do not carry. Retiring it
  before extending demotion to seat datasets would make cross-seat consumption
  *less* honest than today. Note also that ADR-0011's TTL and `citadel unshare`
  were never implemented, so the retraction story it describes does not exist.
- **`author_seat` becomes attested.** Today it is parsed from the body. Derived
  from the dataset name it is a fact the server knows, which is a genuine
  improvement that arrives free with this change.
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
