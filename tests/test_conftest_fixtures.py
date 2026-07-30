"""Pin the autouse fixtures in conftest.py, which nothing else covers.

The app.state fixture (#120) is invisible when it works: the suite passes
identically whether it restores state or does nothing at all. These two tests
run in order and the second one fails if the fixture stops restoring, which is
the only way a regression here would ever surface.
"""

from __future__ import annotations

import kb.server

_SENTINEL = "_conftest_leak_probe"


def test_app_state_assignment_inside_a_test() -> None:
    assert not hasattr(kb.server.app.state, _SENTINEL)
    setattr(kb.server.app.state, _SENTINEL, object())
    assert hasattr(kb.server.app.state, _SENTINEL)


def test_app_state_assignment_did_not_leak_into_the_next_test() -> None:
    assert not hasattr(kb.server.app.state, _SENTINEL)
