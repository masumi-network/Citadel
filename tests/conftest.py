from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _isolate_claude_home(tmp_path_factory, monkeypatch):
    """Point user-scope Claude config at a throwaway home for every test.

    Onboarding now writes session hooks to ~/.claude/settings.json (#38); without
    this guard the test suite would mutate the developer's real config. kb.onboard
    .claude_home() honors CITADEL_HOME, so this fully isolates it.
    """
    home = tmp_path_factory.mktemp("citadel_home")
    monkeypatch.setenv("CITADEL_HOME", str(home))
    yield


@pytest.fixture(autouse=True)
def _no_macos_gui_setenv(monkeypatch):
    """Onboard publishes the token via `launchctl setenv` on Darwin.

    Tests must not write the developer's GUI session env. Individual tests that
    assert the publish step patch ``kb.cli.publish_token_to_macos_gui``.
    """
    monkeypatch.setattr(
        "kb.cli.publish_token_to_macos_gui",
        lambda token: "skipped:not-darwin",
    )
    yield


@pytest.fixture(autouse=True)
def _no_credentials_for_a_real_node(monkeypatch):
    """Stop the suite authenticating to whatever Node the developer is pointed at.

    `citadel setup --root` does not only write the local config: it calls
    sync_local_capture_roots_to_server, which resolves the seat from the token in
    the environment and merges the local roots into that seat on the Node. A test
    that passes --root without also passing --node-url therefore registers its own
    tmp_path against a live seat. One did, and 47 of the 50 approved capture roots
    on the seat were pytest temp directories, one per run of the suite.

    That is worse than clutter. The server merge is a union, so the list only
    grows, and capture_roots_sync refuses locally once it would exceed
    MAX_APPROVED_CAPTURE_ROOTS (50) — a state its own comment describes as a loop
    that cannot end on its own, because the rejected write never lands and so the
    "unchanged" short-circuit never fires.

    Clearing the token is enough: _sync_local_capture_roots_once returns
    "skipped" on an empty token before it resolves a base URL, so no request is
    made at all. A test that wants a token still sets one with monkeypatch, which
    runs after this fixture and wins.
    """
    for name in (
        "CITADEL_MCP_ACCESS_TOKEN",
        "CITADEL_WRITER_KEYS",
        "CITADEL_ADMIN_KEY",
        "CITADEL_ADMIN_TOKEN",
        "CITADEL_ACCESS_KEYS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_auth_throttle():
    """Empty the failed-auth buckets around every test (M4).

    They are module globals, and TestClient presents a single client host, so
    the suite's 401 assertions all land in one per-IP bucket whose limit is 10.
    Without this the suite passes or fails on test ordering.
    """
    server = sys.modules.get("kb.server")
    if server is not None:
        server.reset_auth_throttle()
    yield
    server = sys.modules.get("kb.server")
    if server is not None:
        server.reset_auth_throttle()


@pytest.fixture(autouse=True)
def _reset_app_state():
    """Restore kb.server.app.state after every test that assigns to it (#120).

    The route dependencies in kb/server.py resolve their collaborators off the
    module-level ``app`` singleton, so tests swap in fakes by assigning to
    app.state and the assignment outlives the test. Snapshot and restore instead
    of asking 249 assignment sites to clean up after themselves.

    kb.server is looked up through sys.modules rather than imported at module
    scope because conftest loads for the whole session and kb.server pulls in
    cognee; test modules are imported at collection, so kb.server is already
    there whenever a test actually touches it. The snapshot is shallow: this
    fixes attribute leakage, not in-place mutation of a stored object.
    """
    server = sys.modules.get("kb.server")
    snapshot = dict(server.app.state._state) if server is not None else None
    yield
    server = sys.modules.get("kb.server")
    if server is None:
        return
    state = server.app.state._state
    state.clear()
    if snapshot:
        state.update(snapshot)
