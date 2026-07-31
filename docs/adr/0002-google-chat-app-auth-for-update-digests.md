# Use Google Chat App Authentication For Organization Update Digests

> **Withdrawn 2026-07-31 — Google Chat is dropped.** Citadel will not ship a
> Google Chat delivery surface. **Organization Update Digests** will be
> delivered through a connector chosen later; the provider is deliberately
> undecided.
>
> What survives this withdrawal is the seam, not the adapter. `kb/notification_gateways.py`
> defines a `NotificationGateway` **Protocol** (`status()` + `post_digest()`) with
> `configured_gateways()` returning a provider map, so delivery was never coupled
> to Google Chat at the call site. A future connector implements the same Protocol
> and registers itself there; nothing upstream of it changes.
>
> Two decisions recorded here are worth keeping regardless of provider, because
> they were the reasons the digest surface is safe: delivery is **outbound-only**
> with no inbound command path, and a digest is an **adapter, not a source of
> vault truth** — `kb/organization_digest.py` never writes to the vault. Any
> replacement connector inherits both constraints.
>
> The rest of this ADR is retained for history. It no longer describes intent.

Citadel will deliver Phase 1 **Organization Update Digests** to Google Chat as outbound-only messages from a Google Chat app using Chat API app authentication, rather than incoming webhooks. Incoming webhooks are faster to set up, but they are space-specific and do not establish the durable app identity or future inbound-command path that Citadel needs for an organization-wide communication surface.

**Considered Options**

- Incoming webhooks: lowest setup cost, one-way only, tied to a single space URL.
- Chat API app authentication: more setup, but one app can be installed in multiple spaces and later support inbound Google Chat events.
- Full interactive Chat app immediately: useful for mentions and commands, but too much surface area for Phase 1.

**Consequences**

- Phase 1 is outbound-only; mentions, slash commands, and Chat-to-vault ingestion are out of scope.
- Google service account credentials become production secrets and must not be logged, committed, mirrored, or exposed in Chat output.
- Digest delivery is a communication adapter over the **Organization Vault**, not a separate source of vault truth.
