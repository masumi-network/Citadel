"""The suite must not be able to authenticate to a real Node.

Three autouse fixtures in conftest already exist because the suite mutated
something real: the developer's ~/.claude config, a module-global throttle, and
app.state. This is the fourth, and the damage it prevents is the least visible,
because a capture root synced to a live seat leaves no trace in the test output.
"""

from __future__ import annotations

import os

from kb.capture import capture_token
from kb.capture_config import MAX_APPROVED_CAPTURE_ROOTS
from kb.capture_roots_sync import _sync_local_capture_roots_once


def test_no_seat_token_is_visible_to_a_test() -> None:
    """capture_token reads the environment directly, so this is the real check.

    Reduced to a bool on a separate line on purpose. This test fails precisely
    when a real credential is present, and pytest's assertion introspection walks
    the expression tree and reports every sub-expression it evaluated: both
    `assert capture_token() == ""` and `assert bool(capture_token()) is False`
    print the token they just caught, into CI logs, for a public repository.
    Only a name already bound to a bool keeps the value out of the report.
    """
    leaked = bool(capture_token())

    assert leaked is False, (
        "a seat token is visible to the suite; _no_credentials_for_a_real_node "
        "should have cleared CITADEL_MCP_ACCESS_TOKEN / CITADEL_WRITER_KEYS"
    )


def test_no_admin_credential_is_visible_to_a_test() -> None:
    for name in ("CITADEL_ADMIN_KEY", "CITADEL_ADMIN_TOKEN", "CITADEL_ACCESS_KEYS"):
        present = bool(os.getenv(name))

        assert present is False, f"{name} is visible to the suite"


def test_capture_root_sync_skips_instead_of_calling_a_node(tmp_path) -> None:
    """The specific path that put 47 pytest temp dirs on a production seat.

    A root is configured, so the "no local roots" short-circuit does not apply;
    the sync must stop at the missing token instead, before any request.
    """
    from kb.capture_config import CaptureConfig, CaptureRoot

    config = CaptureConfig(roots=[CaptureRoot(path=str(tmp_path), tags=("personal",))])

    result = _sync_local_capture_roots_once(config)

    assert result.status == "skipped"
    assert "no seat token" in result.detail
    assert result.seat_slug is None


def test_the_ceiling_this_protects_is_still_where_we_think() -> None:
    """If this constant moves, the headroom argument in conftest moves with it."""
    assert MAX_APPROVED_CAPTURE_ROOTS == 50
