from __future__ import annotations

from typing import Any

from scripts import run_self_improve


def test_post_json_converts_read_timeout_to_clean_result(monkeypatch: Any) -> None:
    # #116: a read-phase timeout is a bare TimeoutError, not a URLError, and
    # would otherwise escape this helper's HTTPError/URLError-only handling
    # as a raw traceback (the #39 bug, unbackported to this copy).
    def boom(request: Any, timeout: int) -> None:
        raise TimeoutError("the read operation timed out")

    monkeypatch.setattr(run_self_improve, "urlopen", boom)
    status, body = run_self_improve._post_json(
        "https://citadel.example/api/learning-agent/optimize",
        payload={},
        access_key="secret",
        timeout=5,
    )
    assert status == 599
    assert "timed out" in body["detail"].lower()
