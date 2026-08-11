from __future__ import annotations

import json
from pathlib import Path

from scripts.release_runtime_probe import build_jsonrpc_request, main
from tests.test_release_acceptance import _evidence, _manifest


FIXTURE = Path(__file__).parent / "fixtures" / "citadel_v050_seed_v1.json"


def test_jsonrpc_helper_has_no_transport_credentials() -> None:
    request = build_jsonrpc_request("tools/call", {"name": "citadel_search"})

    assert request == {
        "jsonrpc": "2.0",
        "id": "release-probe",
        "method": "tools/call",
        "params": {"name": "citadel_search"},
    }


def test_probe_writes_redacted_receipt(tmp_path: Path, capsys) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(_evidence(_manifest())), encoding="utf-8")

    code = main(["--manifest", str(FIXTURE), "--evidence", str(evidence)])

    assert code == 0
    output = capsys.readouterr().out
    assert "CITADEL-V050-SEED-CENTRAL" not in output
    assert "marker_sha256" in output
