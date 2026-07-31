# Internal Update Agent Architecture

Citadel should support a modular internal update agent without making that agent
part of the Citadel Archive repository long term.

## Repository Boundary

**Citadel Archive owns:**

- Organization Vault access control, audit logs, MCP, search, ingest, and source
  sync contracts.
- Stable HTTP/MCP contracts that let an agent retrieve source-linked context.
- A compatibility learning-agent path for existing Railway cron deployments.
- Sanitized status and audit records for gateway test calls.

**The separate update-agent repository owns:**

- Agent orchestration and schedule policy.
- Source providers that compose GitHub, Citadel search, and future approved
  company sources.
- Digest policy and prompt/versioning.
- Delivery gateway adapters such as Google Chat, Agent Messenger, Slack, email,
  webhook, or other internal surfaces.
- Retry, dedupe, and posting state for outbound messages.

## Initial Contract

The first external agent can use Citadel's existing preview-only learning-agent
run as the source contract:

```http
POST /api/learning-agent/run
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "force": false,
  "dry_run": false,
  "post_to_chat": false,
  "include_digest_preview": true
}
```

The external agent should treat `organization_digest.preview` as the message
body and `organization_digest.summary` plus `sources.*` as metadata. It should
post to its own gateways, not ask Citadel to post.

This keeps the first split small:

1. Citadel still knows how to build the current digest.
2. The external repo owns delivery gateways immediately.
3. Later, GitHub/source collection and digest generation can move behind
   provider interfaces in the external repo.

## Gateway Contract

Every gateway adapter should expose the same shape:

```python
class NotificationGateway(Protocol):
    def status(self) -> dict[str, Any]: ...
    def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]: ...
```

Gateway results must be sanitized:

- `sent`
- `reason`
- `status_category`
- `status_code`
- external message/thread identifiers

Gateway results must not include credentials, raw HTTP error bodies, or full
message bodies.

## Recommended Separate Repo Layout

```text
citadel-update-agent/
  README.md
  .env.example
  pyproject.toml
  src/citadel_update_agent/
    __init__.py
    __main__.py
    config.py
    citadel_client.py
    digest_provider.py
    gateway.py
    google_chat.py
    runner.py
  tests/
    test_runner.py
    test_google_chat.py
```

## Runtime Flow

1. Cron starts the external update agent.
2. The agent calls Citadel for a preview digest with `post_to_chat=false`.
3. If the digest is not meaningful and quiet-mode is enabled, the agent exits.
4. The agent computes an idempotent message id from the source window.
5. The gateway registry posts the digest to enabled gateways.
6. The agent logs only sanitized delivery status.

## Migration Notes

- ~~Keep Citadel's built-in Google Chat path as a compatibility fallback until
  the external repo is deployed.~~ **Google Chat is dropped (2026-07-31)** —
  there is no fallback to keep. A connector will be chosen later and implements
  the existing `NotificationGateway` Protocol; the seam it plugs into is
  unchanged.
- In production, enable only one poster. Either Citadel cron posts, or the
  external update-agent repo posts.
- Prefer one Citadel admin token scoped to the external agent identity. Rotate it
  separately from human tokens.
- Do not ingest Google Chat messages back into Citadel in this phase.
- Do not make Citadel an Agent Messenger actor. Scout owns Agent Messenger
  communication and uses Citadel only as shared memory.
