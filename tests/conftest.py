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
