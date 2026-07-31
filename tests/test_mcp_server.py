from __future__ import annotations

import asyncio
from io import BytesIO
import inspect
import json
from typing import Any
from urllib.error import HTTPError

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import kb.mcp_server as mcp_server
from kb.mcp_server import (
    MAX_AUDIT_LIMIT,
    MAX_SEARCH_TOP_K,
    MCP_AGENT_INSTRUCTIONS,
    TOOL_POLICIES,
    CitadelHttpClient,
    CitadelMcpError,
    create_mcp_server,
)


class FakeHttpClient:
    def __init__(self) -> None:
        self.gets: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.public_gets: list[str] = []

    def get(
        self,
        path: str,
        *,
        tool_name: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.gets.append(
            {
                "path": path,
                "tool_name": tool_name,
                "extra_headers": extra_headers or {},
            }
        )
        return {
            "ok": True,
            "path": path,
            "tool_name": tool_name,
            "extra_headers": extra_headers or {},
        }

    def get_public(self, path: str) -> dict[str, Any]:
        self.public_gets.append(path)
        return {"ok": True, "path": path, "public": True}

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        tool_name: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.posts.append(
            {"path": path, "payload": payload, "tool_name": tool_name, "timeout": timeout}
        )
        return {"ok": True, "path": path, "payload": payload, "tool_name": tool_name}


def tool_fn(server: Any, name: str) -> Any:
    return server._tool_manager.get_tool(name).fn


def run_tool(server: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    result = tool_fn(server, name)(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def test_record_feedback_does_not_require_qa_id() -> None:
    """The documented flow is "pass qa_id OR result_id".

    qa_id had no default, so FastMCP generated it as a REQUIRED parameter and
    the documented result_id-only call failed schema validation before it ever
    reached the server — which does accept result_id alone.
    """
    server = create_mcp_server(FakeHttpClient())

    schema = server._tool_manager.get_tool("citadel_record_feedback").parameters

    assert "qa_id" not in (schema.get("required") or [])
    assert "qa_id" in schema["properties"]


def test_tools_list_session_resolved_in_process_not_via_self_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tools/list must resolve the caller's role in-process, never by a sync
    GET /api/session self-call.

    The self-call runs on the same event loop that must serve it, so after the
    HTTP client's 3 retries (30s each) tools/list took ~90s and clients
    registered zero tools (#100). access_key_identity reads the access store
    in-process and returns in milliseconds.
    """
    from types import SimpleNamespace

    import kb.server as server_mod

    monkeypatch.setattr(
        server_mod,
        "access_key_identity",
        lambda token: (SimpleNamespace(role="reader", seat_slug="sarthi"), "cookie"),
    )

    session = mcp_server._session_from_token_inprocess("ctdl_x")
    assert session == {"ok": True, "role": "reader", "seat_slug": "sarthi"}

    # Fails open (no tool list blanking) when the store lookup errors or misses.
    def boom(token: str) -> Any:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(server_mod, "access_key_identity", boom)
    assert mcp_server._session_from_token_inprocess("ctdl_x") is None

    monkeypatch.setattr(server_mod, "access_key_identity", lambda token: None)
    assert mcp_server._session_from_token_inprocess("ctdl_x") is None


def test_streamable_http_uses_json_response_not_sse() -> None:
    """tools/list must return an immediate application/json body, not an SSE stream.

    The hosted proxy buffered the SSE stream and held it open ~91s, so a trivial
    tools/list hung and clients reported "connected · tools fetch failed" (#100).
    json_response mode answers each request with a plain JSON body instead.
    """
    import httpx

    server = create_mcp_server(FakeHttpClient(), stateless_http=True)
    assert server.settings.json_response is True

    app = server.streamable_http_app()

    async def _roundtrip() -> tuple[str, int]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                }
                init = await client.post(
                    "/",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "1"},
                        },
                    },
                )
                sid = init.headers.get("mcp-session-id")
                if sid:
                    headers = {**headers, "mcp-session-id": sid}
                await client.post(
                    "/", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
                resp = await client.post(
                    "/",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                return resp.headers.get("content-type", ""), resp.text.count('"name"')

    content_type, tool_names = asyncio.run(_roundtrip())
    assert content_type.startswith("application/json")
    assert "text/event-stream" not in content_type
    assert tool_names == len(TOOL_POLICIES)


def test_registered_tools_include_safety_annotations() -> None:
    server = create_mcp_server(FakeHttpClient())

    for name, policy in TOOL_POLICIES.items():
        tool = server._tool_manager.get_tool(name)

        assert tool is not None
        assert tool.annotations == policy.annotations


def test_discovery_tool_authenticates_then_fetches_public_manifest() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    result = run_tool(server, "citadel_discovery", None)

    assert result["path"] == "/.well-known/citadel.json"
    assert result["tool_name"] is None
    assert client.gets == [
        {"path": "/api/session", "tool_name": "citadel_discovery", "extra_headers": {}},
        {"path": "/.well-known/citadel.json", "tool_name": None, "extra_headers": {}},
    ]


def test_discovery_forwarded_headers_are_validated() -> None:
    class FakeRequest:
        headers = {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "citadel-archive-production.up.railway.app",
        }
        url = "http://127.0.0.1:8000/mcp"

    class FakeRequestContext:
        request = FakeRequest()

    class FakeContext:
        request_context = FakeRequestContext()

    assert mcp_server._public_url_headers_from_context(FakeContext()) == {
        "X-Forwarded-Host": "citadel-archive-production.up.railway.app",
        "X-Forwarded-Proto": "https",
    }


def test_discovery_forwarded_headers_reject_malformed_values() -> None:
    class FakeRequest:
        headers = {
            "x-forwarded-proto": "javascript",
            "x-forwarded-host": "evil.example/path",
        }
        url = "http://127.0.0.1:8000/mcp"

    class FakeRequestContext:
        request = FakeRequest()

    class FakeContext:
        request_context = FakeRequestContext()

    assert mcp_server._public_url_headers_from_context(FakeContext()) == {}


def test_discovery_resource_reads_public_manifest_only() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    resource = asyncio.run(server._resource_manager.get_resource("citadel://discovery"))

    assert resource is not None
    assert json.loads(resource.fn()) == {
        "ok": True,
        "path": "/.well-known/citadel.json",
        "public": True,
    }
    assert client.public_gets == ["/.well-known/citadel.json"]
    assert client.gets == []


def test_authed_resource_uses_caller_token_on_hosted_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    # Hosted (HTTP) transport has no fallback client, so an authed resource MUST
    # read the caller's bearer token from the live request context. Regression
    # for #29: the resource handlers passed resolve_client(None) and always
    # raised "No access token" on the hosted /mcp endpoint while tools worked.
    server = create_mcp_server()

    fake_ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(
                headers={"authorization": "Bearer ctdl_resourcetoken"}
            )
        )
    )
    monkeypatch.setattr(server, "get_context", lambda: fake_ctx)

    captured: dict[str, Any] = {}

    class StubClient:
        def __init__(self, *, base_url: str | None = None, access_token: str = "") -> None:
            captured["token"] = access_token

        def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
            captured["path"] = path
            return {"ok": True, "path": path}

    monkeypatch.setattr(mcp_server, "CitadelHttpClient", StubClient)

    resource = asyncio.run(server._resource_manager.get_resource("citadel://indexes"))
    payload = json.loads(resource.fn())

    assert captured["token"] == "ctdl_resourcetoken"
    assert captured["path"] == "/api/indexes"
    assert payload == {"ok": True, "path": "/api/indexes"}


def test_search_clamps_top_k_and_tracks_tool_name() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    result = run_tool(server, "citadel_search", " source state ", None, top_k=999)

    assert result["payload"]["query"] == "source state"
    assert result["payload"]["top_k"] == MAX_SEARCH_TOP_K
    assert result["tool_name"] == "citadel_search"
    assert client.posts[0]["path"] == "/search"


def test_search_forwards_filter_args_to_server() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    result = run_tool(
        server,
        "citadel_search",
        " MIP payment schema ",
        None,
        types=["spec", "skill"],
        repo="masumi-network/agent",
        path="docs/MIP-003",
        canonical_only=True,
    )

    assert result["payload"]["query"] == "MIP payment schema"
    assert result["payload"]["types"] == ["spec", "skill"]
    assert result["payload"]["repo"] == "masumi-network/agent"
    assert result["payload"]["path"] == "docs/MIP-003"
    assert result["payload"]["canonical_only"] is True
    assert "canonical_only" in client.posts[0]["payload"]


def test_search_omits_dataset_for_server_side_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITADEL_MCP_DEFAULT_DATASET", "masumi-network")
    client = FakeHttpClient()
    server = create_mcp_server(client)

    result = run_tool(server, "citadel_search", " source state ", None)
    explicit = run_tool(server, "citadel_search", " notes ", None, dataset="personal")

    assert result["payload"]["dataset"] is None
    assert explicit["payload"]["dataset"] == "personal"


def test_backup_mirror_tools_forward_admin_calls() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    status = run_tool(server, "citadel_backup_mirror_status", None)
    run = run_tool(server, "citadel_run_backup_mirror", None)
    write = run_tool(server, "citadel_run_backup_mirror", None, dry_run=False)

    assert status["path"] == "/api/backup-mirror"
    assert status["tool_name"] == "citadel_backup_mirror_status"
    assert run["path"] == "/api/backup-mirror/run"
    assert run["payload"] == {"dry_run": True}
    assert run["tool_name"] == "citadel_run_backup_mirror"
    assert write["payload"] == {"dry_run": False}


def test_audit_tool_uses_bounded_server_view() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    default = run_tool(server, "citadel_audit_events", None)
    failures = run_tool(server, "citadel_audit_events", None, view="failures", limit=999)

    assert default["path"] == "/api/audit?view=mcp&limit=50"
    assert default["tool_name"] == "citadel_audit_events"
    assert failures["path"] == f"/api/audit?view=failures&limit={MAX_AUDIT_LIMIT}"

    with pytest.raises(ToolError, match="view must be one of"):
        run_tool(server, "citadel_audit_events", None, view="everything")


def test_write_tools_reject_empty_or_oversized_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    with pytest.raises(ToolError, match="data must not be empty"):
        run_tool(server, "citadel_ingest", "   ", None)

    monkeypatch.setenv("CITADEL_MCP_MAX_INGEST_BYTES", "4")
    with pytest.raises(ToolError, match="payload is 5 bytes"):
        run_tool(server, "citadel_ingest", "12345", None)

    with pytest.raises(ToolError, match="pass qa_id or result_id"):
        run_tool(server, "citadel_record_feedback", None, "")

    rated = run_tool(
        server,
        "citadel_record_feedback",
        None,
        result_id="hit-1",
        correct=True,
        text="useful hit",
    )
    assert rated["ok"] is True
    feedback_post = next(p for p in client.posts if p["path"] == "/feedback")
    assert feedback_post["payload"]["qa_id"] == "hit-1"
    assert feedback_post["payload"]["result_id"] == "hit-1"
    assert feedback_post["payload"]["correct"] is True


def test_ingest_tool_requests_inline_cognify_by_default() -> None:
    # #53: the MCP ingest tool must send cognify=true (parity with the CLI) so an
    # agent-ingested note is searchable immediately, not stuck on background cognify.
    client = FakeHttpClient()
    server = create_mcp_server(client)

    run_tool(server, "citadel_ingest", "a durable note", None)

    assert len(client.posts) == 1
    post = client.posts[0]
    assert post["path"] == "/ingest"
    assert post["payload"]["cognify"] is True
    # Inline cognify can exceed the default 30s budget, so the tool extends the timeout.
    assert post["timeout"] == mcp_server._INGEST_COGNIFY_TIMEOUT


def test_ingest_tool_honors_cognify_opt_out() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    run_tool(server, "citadel_ingest", "a durable note", None, cognify=False)

    post = client.posts[0]
    assert post["payload"]["cognify"] is False
    # No extended budget when not blocking on cognify.
    assert post["timeout"] is None


class _OkResp:
    def __enter__(self) -> Any:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return b'{"ok": true}'


def test_http_client_retries_transient_5xx_on_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    # #50: idempotent reads ride out a transient 503 instead of failing ~20%.
    monkeypatch.setenv("CITADEL_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("CITADEL_RETRY_MAX_ATTEMPTS", "3")
    attempts: list[int] = []

    def fake_urlopen(request: Any, timeout: float) -> Any:
        attempts.append(1)
        if len(attempts) < 2:
            raise HTTPError(request.full_url, 503, "busy", {}, BytesIO(b"{}"))
        return _OkResp()

    monkeypatch.setattr(mcp_server, "urlopen", fake_urlopen)
    client = CitadelHttpClient(base_url="http://localhost:8000", access_token="ctdl_t")

    result = client.get("/api/session", tool_name="citadel_session")
    assert result["ok"] is True
    assert len(attempts) == 2  # one retry, then success


def test_http_client_does_not_retry_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    # #50: writes are never retried (avoid duplicate ingests).
    monkeypatch.setenv("CITADEL_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("CITADEL_RETRY_MAX_ATTEMPTS", "3")
    attempts: list[int] = []

    def fake_urlopen(request: Any, timeout: float) -> Any:
        attempts.append(1)
        raise HTTPError(request.full_url, 503, "busy", {}, BytesIO(b"{}"))

    monkeypatch.setattr(mcp_server, "urlopen", fake_urlopen)
    client = CitadelHttpClient(base_url="http://localhost:8000", access_token="ctdl_t")

    with pytest.raises(CitadelMcpError):
        client.post("/ingest", {"data": "x"}, tool_name="citadel_ingest")
    assert len(attempts) == 1  # no retry on a write


def test_http_client_request_honors_explicit_timeout_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}

    def fake_urlopen(request: Any, timeout: float) -> Any:
        captured["timeout"] = timeout

        class _Resp:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return b"{}"

        return _Resp()

    monkeypatch.setattr(mcp_server, "urlopen", fake_urlopen)
    client = CitadelHttpClient(base_url="http://localhost:8000", access_token="ctdl_t")

    client.post("/ingest", {"data": "x"}, tool_name="citadel_ingest", timeout=180.0)
    assert captured["timeout"] == 180.0

    client.post("/ingest", {"data": "x"}, tool_name="citadel_ingest")
    assert captured["timeout"] == client.timeout


_ADMIN_TOOLS = {
    "citadel_audit_events",
    "citadel_improve",
    "citadel_backup_mirror_status",
    "citadel_run_learning_agent",
    "citadel_run_repo_content_sync",
    "citadel_run_backup_mirror",
}


def test_tools_list_filters_by_role_and_seat() -> None:
    # #33: tools/list must not advertise tools the caller's role/seat cannot use.
    from kb.mcp_server import _filter_tools_for_session

    server = create_mcp_server(FakeHttpClient())
    all_tools = asyncio.run(server.list_tools())
    names = {t.name for t in all_tools}
    assert _ADMIN_TOOLS <= names  # sanity: unfiltered list has the admin tools

    def visible(session: Any) -> set[str]:
        return {t.name for t in _filter_tools_for_session(all_tools, session)}

    # Non-seat writer: admin tools hidden; contribute + ingest visible.
    writer = visible({"role": "writer", "seat_slug": None})
    assert not (_ADMIN_TOOLS & writer)
    assert {"citadel_contribute", "citadel_ingest", "citadel_share_session"} <= writer

    # Seat writer: contribute additionally hidden (Central read-only from seat MCP).
    seat = visible({"role": "writer", "seat_slug": "sarthi"})
    assert "citadel_contribute" not in seat
    assert "citadel_ingest" in seat

    # Reader: writer + admin tools hidden; read tools visible.
    reader = visible({"role": "reader", "seat_slug": None})
    assert not (_ADMIN_TOOLS & reader)
    assert "citadel_ingest" not in reader
    assert "citadel_search" in reader

    # Admin: full set.
    assert _ADMIN_TOOLS <= visible({"role": "admin", "seat_slug": None})

    # Fail open: a missing or unknown-role session never blanks the tool list.
    assert _filter_tools_for_session(all_tools, None) == all_tools
    assert _filter_tools_for_session(all_tools, {"role": "bogus"}) == all_tools


def test_citadel_search_tool_description_nudges_task_start() -> None:
    server = create_mcp_server(FakeHttpClient())
    all_tools = asyncio.run(server.list_tools())
    search = next(t for t in all_tools if t.name == "citadel_search")
    assert "task start" in search.description.lower()
    assert "before editing code" in search.description.lower()


def test_mcp_agent_instructions_cli_fallback_and_no_false_vault_authority() -> None:
    # When Cursor only shows mcp_auth / needsAuth, instructions must steer agents
    # to CLI (then official docs) — never invent vault-backed authority.
    text = MCP_AGENT_INSTRUCTIONS.lower()
    assert "mcp_auth" in text
    assert "citadel status" in text
    assert "citadel search" in text
    assert "citadel doctor" in text
    assert "openapi" in text or "mip" in text
    assert "vault-backed" in text
    assert "successful search hit" in text
    assert "citadel confirms" in text
    assert "usdcx" in text or "asset id" in text or "payment token" in text
    assert "no authoritative hit" in text
    assert "do not invent vault citations" in text or "not invent vault citations" in text
    assert "readiness" in text or "search is unavailable" in text


def test_promotion_decision_tools_require_admin_in_policy() -> None:
    # #48: discovery metadata must match the server's admin/sources:sync gate so an
    # agent doesn't read "writer" and try (then 403) approve/reject.
    for name in ("citadel_promotion_approve", "citadel_promotion_reject"):
        policy = TOOL_POLICIES[name]
        assert policy.role == "admin", name
        assert policy.scope == "sources:sync", name


def test_tools_list_protocol_handler_applies_role_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #33: prove the override is wired into the live tools/list protocol handler
    # (not just the pure helper) and resolves the caller's session to filter.
    # Must not use a sync self-HTTP call (event-loop deadlock → Cursor mcp_auth-only).
    from mcp import types as mcp_types

    server = create_mcp_server(FakeHttpClient())

    monkeypatch.setattr(server, "get_context", lambda: object())
    monkeypatch.setattr(mcp_server, "_bearer_from_context", lambda ctx: "ctdl_tok")
    mcp_server.set_tools_list_session_resolver(
        lambda token: {"role": "writer", "seat_slug": "sarthi"} if token else None
    )

    http_calls: list[str] = []

    class _SessionClient:
        def __init__(self, **_: Any) -> None: ...

        def get(self, path: str, **_: Any) -> dict[str, Any]:
            http_calls.append(path)
            raise AssertionError("tools/list must not fall back to HTTP when resolver works")

    monkeypatch.setattr(mcp_server, "CitadelHttpClient", _SessionClient)

    try:
        handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
        result = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
        names = {t.name for t in result.root.tools}

        assert not (_ADMIN_TOOLS & names)
        assert "citadel_contribute" not in names  # seat writer
        assert "citadel_ingest" in names
        assert http_calls == []
    finally:
        mcp_server.set_tools_list_session_resolver(None)


def test_tools_list_uses_threaded_http_fallback_when_resolver_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import types as mcp_types

    server = create_mcp_server(FakeHttpClient())
    monkeypatch.setattr(server, "get_context", lambda: object())
    monkeypatch.setattr(mcp_server, "_bearer_from_context", lambda ctx: "ctdl_tok")
    mcp_server.set_tools_list_session_resolver(lambda _token: None)

    class _SessionClient:
        def __init__(self, **_: Any) -> None: ...

        def get(self, path: str, **_: Any) -> dict[str, Any]:
            assert path == "/api/session"
            return {"role": "writer", "seat_slug": "sarthi"}

    monkeypatch.setattr(mcp_server, "CitadelHttpClient", _SessionClient)
    try:
        handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
        result = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
        names = {t.name for t in result.root.tools}
        assert "citadel_ingest" in names
        assert not (_ADMIN_TOOLS & names)
    finally:
        mcp_server.set_tools_list_session_resolver(None)


def test_tools_list_protocol_handler_fails_open_without_context() -> None:
    # No HTTP request context (stdio) → unfiltered, since call-time authz applies.
    from mcp import types as mcp_types

    server = create_mcp_server(FakeHttpClient())
    handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    result = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
    names = {t.name for t in result.root.tools}
    assert _ADMIN_TOOLS <= names


def test_remote_http_base_url_is_rejected_without_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CITADEL_MCP_ALLOW_INSECURE_HTTP", raising=False)

    with pytest.raises(CitadelMcpError, match="Refusing insecure remote Citadel URL"):
        CitadelHttpClient(base_url="http://citadel.example", access_token="ctdl_test")

    monkeypatch.setenv("CITADEL_MCP_ALLOW_INSECURE_HTTP", "true")
    client = CitadelHttpClient(base_url="http://citadel.example", access_token="ctdl_test")

    assert client.base_url == "http://citadel.example"


def test_public_client_targets_self_base_url_not_localhost_8000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hosted /mcp has no fallback client, so the public path builds a client with
    # base_url=None. On Railway the app listens on $PORT, not 8000, so the default
    # must resolve to the in-process self base URL, never http://localhost:8000.
    monkeypatch.delenv("CITADEL_HTTP_BASE_URL", raising=False)
    monkeypatch.delenv("CITADEL_MCP_SELF_BASE_URL", raising=False)
    monkeypatch.setenv("PORT", "9137")

    client = CitadelHttpClient(base_url=None, access_token="")

    assert client.base_url == mcp_server._self_base_url()
    assert client.base_url == "http://127.0.0.1:9137"
    assert client.base_url != "http://localhost:8000"


def test_missing_access_token_error_does_not_leak_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CITADEL_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CITADEL_ACCESS_TOKEN", raising=False)
    client = CitadelHttpClient(base_url="http://localhost:8000", access_token=None)

    with pytest.raises(CitadelMcpError) as exc_info:
        client.get("/api/session", tool_name="citadel_session")

    message = str(exc_info.value)
    assert "CITADEL_MCP_ACCESS_TOKEN" in message
    assert "ctdl_" not in message


def test_http_errors_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "ctdl_secret_token"

    def fake_urlopen(request: Any, timeout: float) -> Any:
        assert request.get_header("X-citadel-mcp-tool") == "citadel_search"
        raise HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            BytesIO(
                b'{"detail":"bearer ctdl_secret_token token: ctdl_other api_key=sk-test"}'
            ),
        )

    monkeypatch.setattr(mcp_server, "urlopen", fake_urlopen)
    client = CitadelHttpClient(base_url="http://localhost:8000", access_token=token)

    with pytest.raises(CitadelMcpError) as exc_info:
        client.post("/search", {"query": "anything"}, tool_name="citadel_search")

    message = str(exc_info.value)
    assert token not in message
    assert "ctdl_other" not in message
    assert "sk-test" not in message
    assert "[REDACTED]" in message


def test_contribute_tool_posts_through_the_contribute_endpoint() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    result = run_tool(
        server,
        "citadel_contribute",
        " Decision: adopt deepseek ",
        "We standardized on deepseek/deepseek-v4-flash for enrichment.",
        None,
        tags=["decision"],
        source_url="https://github.com/masumi-network/Citadel-Archive",
    )

    assert result["path"] == "/api/contribute"
    assert result["tool_name"] == "citadel_contribute"
    assert result["payload"]["title"] == "Decision: adopt deepseek"
    assert result["payload"]["tags"] == ["decision"]
    assert result["payload"]["source_url"] == (
        "https://github.com/masumi-network/Citadel-Archive"
    )


def test_list_sources_includes_repo_content_sync() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    result = run_tool(server, "citadel_list_sources", None)

    paths = [call["path"] for call in client.gets]
    assert "/api/repo-content-sync" in paths
    assert "/api/sources" in paths
    assert result["repo_content_sync"]["path"] == "/api/repo-content-sync"


def test_run_repo_content_sync_tool_posts_to_admin_endpoint() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    result = run_tool(server, "citadel_run_repo_content_sync", None, force=True, dry_run=True)

    assert result["path"] == "/api/repo-content-sync/run"
    assert client.posts[-1]["payload"] == {"force": True, "dry_run": True}


def test_recent_contributions_tool_reads_audit_feed() -> None:
    client = FakeHttpClient()
    server = create_mcp_server(client)

    result = run_tool(server, "citadel_recent_contributions", None, limit=5, mine=True)

    assert result["path"] == "/api/contributions/recent?limit=5&mine=true"
    assert client.gets[-1]["tool_name"] == "citadel_recent_contributions"


def test_contribute_tool_rejects_empty_or_oversized_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = create_mcp_server(FakeHttpClient())

    with pytest.raises(ToolError, match="title must not be empty"):
        run_tool(server, "citadel_contribute", "  ", "Body", None)

    with pytest.raises(ToolError, match="content must not be empty"):
        run_tool(server, "citadel_contribute", "Title", "   ", None)

    monkeypatch.setenv("CITADEL_MCP_MAX_INGEST_BYTES", "4")
    with pytest.raises(ToolError, match="payload is 5 bytes"):
        run_tool(server, "citadel_contribute", "Title", "12345", None)


def test_search_does_not_hand_an_agent_every_hit_twice() -> None:
    """`/search` returns each hit in `results` AND again inside `sections`.

    The dashboard needs that grouping and renders a heading per section, so the
    endpoint is right to send it. An agent is not: it reads `results`, and each
    hit already carries its own `_citadel.dataset`.

    Measured against production, one top_k=5 call returned 84,376 characters of
    which 41,986 were the `sections` copy. That was large enough to blow the MCP
    tool output limit, so the caller received an error and a file path instead
    of an answer. Halving it is the difference between a usable search and one
    that fails outright.
    """
    from kb.mcp_server import _compact_search_for_agent

    # Deliberately under the truncation cap: this test is about the duplicate
    # `sections` copy, not about hit-text length, which has its own test.
    hit = {"id": "a", "text": "x" * 900, "_citadel": {"dataset": "masumi-network"}}
    other = {"id": "b", "text": "y" * 900, "_citadel": {"dataset": "seat:alice"}}
    payload = {
        "results": [hit, other],
        "sections": {"central": [hit], "node": [other], "session_traces": []},
        "search_id": "s-1",
    }
    before = len(json.dumps(payload))

    compacted = _compact_search_for_agent(payload)

    assert "sections" not in compacted, "hit copies still being sent to the agent"
    # The grouping survives as counts, so a caller can still see the split.
    assert compacted["section_counts"] == {"central": 1, "node": 1, "session_traces": 0}
    # Nothing a caller actually reads was dropped.
    assert compacted["results"] == [hit, other]
    assert compacted["search_id"] == "s-1"
    assert len(json.dumps(compacted)) < before / 1.8, "payload was not meaningfully reduced"


def test_search_compaction_leaves_unexpected_shapes_alone() -> None:
    """Never turn a search failure into a crash inside the compactor.

    An error body, or a future server that stops sending `sections`, must pass
    through untouched rather than raising and costing the caller the result.
    """
    from kb.mcp_server import _compact_search_for_agent

    assert _compact_search_for_agent({"results": []}) == {"results": []}
    assert _compact_search_for_agent({"sections": None}) == {"sections": None}
    assert _compact_search_for_agent("not a dict") == "not a dict"
    assert _compact_search_for_agent(None) is None


def test_citadel_search_tool_strips_the_duplicate_sections(monkeypatch: Any) -> None:
    """Through the TOOL, not the helper.

    The two tests above call `_compact_search_for_agent` directly, so they would
    both stay green if the call were dropped from `citadel_search`. That is
    exactly how a guard shipped inert earlier: the unit test asserted the
    parser, nothing asserted the wiring. This one goes through the tool, so
    removing the call turns it red.
    """
    hit = {"id": "a", "text": "x" * 900, "_citadel": {"dataset": "masumi-network"}}
    body = {
        "results": [hit],
        "sections": {"central": [hit], "node": [], "session_traces": []},
        "search_id": "s-9",
    }

    class SearchClient(FakeHttpClient):
        def post(
            self,
            path: str,
            payload: dict[str, Any],
            *,
            tool_name: str | None = None,
            extra_headers: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            self.posts.append({"path": path, "payload": payload, "tool_name": tool_name})
            return dict(body)

    server = create_mcp_server(SearchClient())

    result = run_tool(server, "citadel_search", "hermes", None, top_k=5)

    assert "sections" not in result, "the tool handed the agent every hit twice"
    assert result["section_counts"] == {"central": 1, "node": 0, "session_traces": 0}
    assert result["results"] == [hit]
    assert result["search_id"] == "s-9"


def test_search_truncates_oversized_hit_text_and_says_so(monkeypatch: Any) -> None:
    """Dropping the duplicate `sections` was necessary and not sufficient.

    Production hits carry 6,000 to 7,000 characters each, so the DOCUMENTED
    default top_k=10 returned 209,109 characters and blew the tool-result budget
    even with the duplicate removed (~103,000 remained). A cap is needed too.

    Truncation must never be silent. An agent that cannot tell a full document
    from its first 500 words will summarise the fragment and present it as the
    whole, which is a worse failure than the bloat it fixes.
    """
    monkeypatch.delenv("CITADEL_MCP_MAX_HIT_TEXT_CHARS", raising=False)
    from kb.mcp_server import _compact_search_for_agent

    long_text = "\n".join(f"line {i} of the document body" for i in range(400))
    payload = {"results": [{"id": "doc-1", "text": long_text}], "sections": {}}

    compacted = _compact_search_for_agent(payload)
    hit = compacted["results"][0]

    assert len(hit["text"]) <= 2000
    assert hit["text_truncated"] is True
    assert hit["text_full_chars"] == len(long_text)
    # The escape hatch has to be actionable, so the id must survive.
    assert hit["id"] == "doc-1"
    assert "citadel_get_document" in hit["text_hint"]


def test_search_leaves_short_hits_and_honours_the_override(monkeypatch: Any) -> None:
    """A short hit must come back untouched and unflagged, and an operator who
    genuinely wants full text must be able to ask for it without a deploy."""
    from kb.mcp_server import _compact_search_for_agent

    monkeypatch.delenv("CITADEL_MCP_MAX_HIT_TEXT_CHARS", raising=False)
    short = {"id": "s", "text": "brief answer"}
    assert _compact_search_for_agent({"results": [short]})["results"][0] == short

    long_text = "x" * 9000
    monkeypatch.setenv("CITADEL_MCP_MAX_HIT_TEXT_CHARS", "0")
    untouched = _compact_search_for_agent({"results": [{"id": "d", "text": long_text}]})
    assert untouched["results"][0]["text"] == long_text, "0 must disable truncation"

    monkeypatch.setenv("CITADEL_MCP_MAX_HIT_TEXT_CHARS", "500")
    tight = _compact_search_for_agent({"results": [{"id": "d", "text": long_text}]})
    assert len(tight["results"][0]["text"]) <= 500

    monkeypatch.setenv("CITADEL_MCP_MAX_HIT_TEXT_CHARS", "not-a-number")
    safe = _compact_search_for_agent({"results": [{"id": "d", "text": long_text}]})
    assert len(safe["results"][0]["text"]) <= 2000, "a bad env value must not disable the cap"
