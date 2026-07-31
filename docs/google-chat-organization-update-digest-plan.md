# Google Chat Organization Update Digest Plan

> **Withdrawn 2026-07-31 — Google Chat is dropped.** This plan is retained for
> history and no longer describes intent. Delivery of **Organization Update
> Digests** will go through a connector chosen later, implementing the existing
> `NotificationGateway` Protocol in `kb/notification_gateways.py`. See
> [ADR-0002](adr/0002-google-chat-app-auth-for-update-digests.md).
>
> The provider-independent decisions in this plan still stand and any
> replacement connector inherits them: delivery is outbound-only, best-effort
> (a delivery failure must never fail the digest run), posts only redacted
> summaries, and never ingests messages back into the vault.

Citadel will post a daily **Organization Update Digest** to one dedicated Google
Chat space. The digest is outbound-only in Phase 1 and is generated from
source-linked Organization Vault context, with GitHub activity as the required
source.

## Decisions

- Phase 1 is outbound-only. Google Chat mentions, slash commands, and inbound
  Chat-to-vault ingestion are out of scope.
- Delivery uses Google Chat API app authentication, not incoming webhooks. See
  [ADR 0002](adr/0002-google-chat-app-auth-for-update-digests.md).
- The destination is one dedicated Google Chat space.
- A Railway scheduled service posts daily at `10:00 Europe/Berlin`.
- The digest covers the previous 24 hours.
- If there are no meaningful source-linked updates, the bot stays silent.
- Scheduled runs post automatically.
- Manual admin-triggered runs preview only unless explicitly requested to post.
- Google Chat delivery is best-effort. A Chat delivery failure must not fail the
  GitHub/Citadel learning job.

## Digest Scope

The digest should answer: what changed, what is open, what merged, where the
repository momentum is, and what the agent thinks the last 24 hours mean.

Include:

- Open pull requests worth attention.
- Merged pull requests and what they changed.
- Repository activity across the org.
- Most active repositories.
- Source-linked decisions or ongoing-work notes already ingested into Citadel.
- A cautious, source-linked "Agent read" section.

Exclude:

- Raw Google Chat transcripts.
- People productivity rankings.
- Raw commit spam.
- Secrets or raw security matches.
- Long private note bodies.
- Unapproved source material.

## Message Shape

```text
Masumi Org Digest - Last 24h

Agent read
- What appears to be happening across the org.
- What looks close to shipping.
- What may need attention, framed constructively.

Open PRs worth attention
- repo#123: why it matters

Merged work
- repo#120: what changed

Repository momentum
- Most active: repo-a, repo-b

Links
- Citadel dashboard/search link
```

The tone should be constructive and action-oriented:

```text
what happened -> why it matters -> suggested next step if needed
```

The digest may include actor names where they are source-linked to a PR, but it
must not rank people or summarize individual productivity.

## Security Findings

Potential secrets or high-risk issues are separate **Security Findings**, not
regular digest content.

For future GitHub App/check work:

- Use GitHub check runs/annotations for exact PR context.
- Post only redacted summaries to Google Chat.
- Never post the secret value or raw match text.
- Cross-reference Citadel context later for known env names, architecture rules,
  prior incidents, and allowed patterns.

## Architecture

Phase 1 adds:

- A digest producer that gathers a 24-hour source packet from GitHub and Citadel.
- An LLM summarizer that generates the source-linked Agent read using existing
  OpenRouter/Citadel model configuration.
- A deterministic fallback digest when the model is unavailable.
- A delivery gateway interface with Google Chat as the first adapter, using Chat
  API app authentication.
- A Railway cron entry/service configured for `10:00 Europe/Berlin`.

Gateway adapters are delivery channels, not source learning. They should not
fetch GitHub data, mutate the Organization Vault, or ingest Chat messages.

The target modular shape is a separate internal update-agent repository that
owns scheduling and gateways while Citadel exposes source-linked context. See
[`internal-update-agent-architecture.md`](internal-update-agent-architecture.md).

## Operational Rules

- Store Google service account credentials as Railway secrets.
- Put Chat credentials on the cron service first, not broadly on the web service.
  If the cron calls the web service with `CITADEL_GITHUB_SYNC_TARGET_URL`, put
  the credentials on the web service too because that process performs delivery.
- Do not log credentials, raw message bodies, raw error bodies, or secret
  matches.
- Log or return only sanitized delivery status: sent/skipped/failed, status
  category, counts, and destination label.
- Use bounded retries for transient Chat API failures.
- Send one summary message per run.

## Environment

Google Workspace setup:

1. Create or choose the Google Cloud project that will own the Chat app.
2. Enable the Google Chat API.
3. Configure the Chat app name, avatar, description, and visibility for the
   Masumi organization.
4. Create a service account in the same project and grant it access to call the
   Chat API as the app.
5. Add the Chat app to the dedicated Google Chat space.
6. Record the target space resource name, which looks like `spaces/...`.
7. Store the service account JSON as a Railway secret, not in the repository.

```bash
CITADEL_ORG_DIGEST_ENABLED=true
CITADEL_ORG_DIGEST_WINDOW_HOURS=24
CITADEL_ORG_DIGEST_MAX_ITEMS=6
CITADEL_ORG_DIGEST_LLM_ENABLED=true
CITADEL_ORG_DIGEST_POST_ON_NO_UPDATES=false
CITADEL_ORG_DIGEST_POST_TO_CHAT=true
CITADEL_ORG_DIGEST_INCLUDE_PREVIEW_IN_CRON_OUTPUT=false

CITADEL_GOOGLE_CHAT_ENABLED=true
CITADEL_GOOGLE_CHAT_SPACE_NAME=spaces/...
CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
CITADEL_GOOGLE_CHAT_THREAD_KEY=citadel-org-digest
```

Manual API runs preview by default:

```bash
curl -fsS -X POST "$CITADEL_BASE_URL/api/learning-agent/run" \
  -H "Authorization: Bearer $CITADEL_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data '{"force":false,"dry_run":false}' \
  | python3 -m json.tool
```

Manual posting requires an explicit flag:

```bash
curl -fsS -X POST "$CITADEL_BASE_URL/api/learning-agent/run" \
  -H "Authorization: Bearer $CITADEL_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data '{"force":false,"dry_run":false,"post_to_chat":true}'
```

After the Google app, space, and Railway variables are configured, prove delivery
with a short test message:

```bash
curl -fsS -X POST "$CITADEL_BASE_URL/api/learning-agent/google-chat/test" \
  -H "Authorization: Bearer $CITADEL_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data '{"message":"Citadel Google Chat delivery test"}'
```

This endpoint requires admin `sources:sync` access, posts only the supplied
short test text, and audits the sanitized delivery outcome without storing the
test message body.

Scheduled cron runs should hide the preview in output so Railway logs do not
store raw Google Chat message bodies.

## Rollout Checklist

1. Add the Chat app to the dedicated Google Chat space.
2. Store `CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON` and
   `CITADEL_GOOGLE_CHAT_SPACE_NAME` on the Railway service that performs
   delivery.
3. Confirm `CITADEL_ORG_DIGEST_POST_TO_CHAT=true` only on the scheduled job.
4. Run the Google Chat test endpoint and verify one test message appears in the
   dedicated space.
5. Run `/api/learning-agent/run` without `post_to_chat` to inspect a preview.
6. Trigger one explicit posting run with `post_to_chat:true` if the preview looks
   right.
7. Leave the cron scheduled for `10:00 Europe/Berlin`.

## Railway Observed State

Checked on 2026-06-03:

- Project: `Citadel Archive`
- Environment: `production`
- Web service: `Citadel-Archive`
- Cron service: `Citadel-GitHub-Sync`
- Current cron schedule: `0 3 * * *`, next run `2026-06-04T03:00:00Z`
- Current cron start command override: `python -m kb.github_sync --org masumi-network`

Target cron state for this feature:

- Schedule: `0 8 * * *` while Berlin is on CEST, or the UTC expression matching
  `10:00 Europe/Berlin`.
- Start command: use the repository `railway.toml` default
  `python scripts/run_railway.py`, or explicitly set
  `python scripts/run_github_sync.py`.
- Variables on the process that performs delivery:
  `CITADEL_RUN_MODE=learning-agent`,
  `CITADEL_ORG_DIGEST_POST_TO_CHAT=true`,
  `CITADEL_ORG_DIGEST_INCLUDE_PREVIEW_IN_CRON_OUTPUT=false`,
  `CITADEL_GOOGLE_CHAT_ENABLED=true`,
  `CITADEL_GOOGLE_CHAT_SPACE_NAME=spaces/...`, and
  `CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON`.

## Research Notes

- OpenClaw uses gateway/channel adapter patterns; Citadel should copy the shape,
  not the full gateway.
- Hermes uses a messaging gateway model with platform-specific adapters and a
  home channel for background delivery.
- Google Chat API app authentication gives Citadel a real app identity and a
  future path to inbound events, while incoming webhooks are lower setup but
  space-specific and one-way.

Useful docs:

- Google Chat messages API: https://developers.google.com/workspace/chat/create-messages
- Google Chat app auth: https://developers.google.com/workspace/chat/authenticate-authorize-chat-app
- Google Chat interaction events: https://developers.google.com/workspace/chat/receive-respond-interactions
- Google Chat usage limits: https://developers.google.com/workspace/chat/limits
- OpenClaw docs: https://docs.openclaw.ai/
- OpenClaw Google Chat channel: https://open-claw.bot/docs/channels/googlechat/
- Hermes messaging gateway: https://hermes-agent.nousresearch.com/docs/user-guide/messaging
- Hermes Google Chat channel: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/google_chat
