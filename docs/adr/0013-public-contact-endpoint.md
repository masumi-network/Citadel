# ADR-0013: the partnering contact form relays to Chat, never into the vault

- Status: Accepted
- Date: 2026-07-28
- Relates to: ADR-0012 (the cheapest write path is not authenticated), ADR-0007
  (seat capture and promotion write policy).

## Context

`/contact` is a public page aimed at EU consortium coordinators and at teams
evaluating Citadel, who need a way to reach us from the site they are reading. That means an unauthenticated write
path on a service whose entire design premise is that writes are seat scoped and
promotion gated.

Three destinations were considered:

1. **Into the vault.** Land the enquiry as Structured Knowledge, or into
   Central. Rejected. ADR-0012 already documents what happens when unvetted text
   reaches the knowledge substrate: a GitHub issue title flipped a whole digest
   to `canonical`. An open, unauthenticated form writing into the same substrate
   is the same failure with the barrier removed, and it hands an attacker a
   direct channel into the memory that agents read as authority.
2. **A Linear issue per enquiry.** Trackable, but public issue creation is
   spammable, and the Linear mirror is seat scoped, so an enquiry from outside
   has no seat to belong to.
3. **The existing Google Chat gateway.** The org already reads that space, the
   delivery adapter already exists (`kb/google_chat.py`, ADR-0002), and the
   message lands somewhere with a human on the other end.

A fourth option, a hosted form service, is ruled out by the CSP: `form-action`
and `connect-src` are both `'self'`.

## Decision

`POST /contact` accepts a partnering enquiry and relays it to the Google Chat
space through the existing gateway. It never touches the vault.

Constraints that make the endpoint safe enough to expose:

- **Its own thread.** Enquiries post under `citadel-partner-contact`, never the
  organization digest thread, so an enquiry cannot be read as generated content.
- **Fail closed.** No configured gateway is a 503 telling the sender to email
  instead. An enquiry is never accepted into a void, because a message that
  silently disappears is worse than an error nobody has to guess about.
- **Formatting stripped.** Google Chat renders `<url|label>` as a link and
  `` *_~` `` as formatting, so submitted text could otherwise forge a message
  that reads like Citadel itself. Those characters are dropped, not escaped:
  the text is only ever read by a human.
- **Honeypot answered with 200.** A filled hidden field is dropped silently, so
  a bot never learns it was filtered.
- **Two rate limits.** Per IP (3 per 15 min) and global (30 per hour). The
  per-IP bucket is spoofable behind the Railway proxy, so the global ceiling is
  what actually bounds the damage. Both are in-process, which is enough for one
  instance and fails toward refusing.
- **Capped at the model boundary.** Every field has a maximum length, so an
  oversized body is rejected by validation before reaching the scrubber.

## Consequences

- The service now has exactly one unauthenticated write path, and it is a relay
  with no persistence. That property is worth defending: anything added here
  later should be measured against it.
- Enquiries live in Chat, not in Citadel. There is no record inside the product,
  no search over past enquiries, and no audit trail beyond the Chat space and a
  log line. If that becomes a problem, the fix is a seat-scoped inbox, not
  relaxing this endpoint.
- If the Chat gateway is unconfigured in an environment, the form is visibly
  broken (503 on submit) rather than quietly useless. That is deliberate, but it
  means the gateway is now part of what `/contact` depends on to work.
