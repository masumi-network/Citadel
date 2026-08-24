from __future__ import annotations

import base64
import hashlib

from fastapi.testclient import TestClient

import kb.server as server_module
from kb.server import app


def test_list_skills() -> None:
    client = TestClient(app, base_url="https://citadel.example")
    response = client.get("/skills")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    payload = response.json()
    assert payload["ok"] is True
    slugs = {item["slug"] for item in payload["skills"]}
    assert slugs == {
        "citadel",
        "boundary",
        "cli",
        "connect",
        "debug",
        "onboard",
        "proactive-ingest",
        "search",
        "vault",
    }
    connect = next(item for item in payload["skills"] if item["slug"] == "connect")
    assert connect["url"] == "https://citadel.example/skills/connect"
    assert "mcp" in connect["aliases"]
    assert connect["size_bytes"] > 0
    assert len(connect["sha256"]) == 64
    assert connect["integrity"].startswith("sha256-")


def test_list_skills_uses_forwarded_public_url() -> None:
    client = TestClient(app, base_url="http://internal.local")

    response = client.get(
        "/skills",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "citadel-archive-production.up.railway.app",
        },
    )

    assert response.status_code == 200
    connect = next(item for item in response.json()["skills"] if item["slug"] == "connect")
    assert connect["url"] == "https://citadel-archive-production.up.railway.app/skills/connect"


def test_discovery_manifest_is_public_and_verifiable(monkeypatch) -> None:
    monkeypatch.setattr(server_module, "_BUILD_ID", None)
    client = TestClient(app, base_url="https://citadel.example")
    response = client.get("/.well-known/citadel.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    payload = response.json()
    assert payload["ok"] is True
    from kb.server import _SERVICE_VERSION

    assert payload["service"] == {
        "name": "Citadel Archive",
        "kind": "organization_vault",
        "version": _SERVICE_VERSION,
        "build_id": None,
        "deployment_id": None,
        "base_url": "https://citadel.example",
    }
    assert payload["public_endpoints"]["discovery"] == (
        "https://citadel.example/.well-known/citadel.json"
    )
    assert payload["mcp"]["endpoint"] == "https://citadel.example/mcp/"
    assert payload["mcp"]["authentication"] == {
        "required": True,
        "scheme": "bearer",
        "token_prefix": "ctdl_",
        "header": "Authorization",
    }
    tool_names = {tool["name"] for tool in payload["mcp"]["tools"]}
    assert {"citadel_session", "citadel_search", "citadel_ingest"} <= tool_names
    assert "citadel_ingest" in payload["mcp"]["approval_recommended_for"]
    connect = next(item for item in payload["skills"] if item["slug"] == "connect")
    assert connect["url"] == "https://citadel.example/skills/connect"
    assert len(connect["sha256"]) == 64
    assert connect["integrity"].startswith("sha256-")
    assert "vault search results and source documents" in payload["security"]["private_data"]
    serialized = response.text
    assert "test-admin" not in serialized
    assert "test-reader" not in serialized
    assert "test-writer" not in serialized


def test_discovery_manifest_reports_captured_build_id(monkeypatch) -> None:
    monkeypatch.setattr(server_module, "_BUILD_ID", "a" * 40)
    client = TestClient(app, base_url="https://citadel.example")

    first = client.get("/.well-known/citadel.json").json()
    assert first["service"]["build_id"] == "a" * 40

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "b" * 40)
    second = client.get("/.well-known/citadel.json").json()
    assert second["service"]["build_id"] == "a" * 40


def test_discovery_manifest_uses_forwarded_public_url() -> None:
    client = TestClient(app, base_url="http://internal.local")

    response = client.get(
        "/.well-known/citadel.json",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "citadel-archive-production.up.railway.app",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["service"]["base_url"] == "https://citadel-archive-production.up.railway.app"
    assert payload["mcp"]["endpoint"] == "https://citadel-archive-production.up.railway.app/mcp/"
    connect = next(item for item in payload["skills"] if item["slug"] == "connect")
    assert connect["url"] == "https://citadel-archive-production.up.railway.app/skills/connect"


def test_public_urls_ignore_malformed_forwarded_headers() -> None:
    client = TestClient(app, base_url="https://citadel.example")

    response = client.get(
        "/.well-known/citadel.json",
        headers={
            "x-forwarded-proto": "javascript",
            "x-forwarded-host": "evil.example/path",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["service"]["base_url"] == "https://citadel.example"
    assert payload["mcp"]["endpoint"] == "https://citadel.example/mcp/"
    connect = next(item for item in payload["skills"] if item["slug"] == "connect")
    assert connect["url"] == "https://citadel.example/skills/connect"


def test_get_skill_connect() -> None:
    client = TestClient(app)
    catalog = client.get("/skills").json()
    connect = next(item for item in catalog["skills"] if item["slug"] == "connect")

    response = client.get("/skills/connect")
    assert response.status_code == 200
    assert "citadel-mcp-connector" in response.text
    assert response.headers["content-type"].startswith("text/markdown")
    digest = hashlib.sha256(response.content).digest()
    sha256 = digest.hex()
    assert sha256 == connect["sha256"]
    assert len(response.content) == connect["size_bytes"]
    assert connect["integrity"] == f"sha256-{base64.b64encode(digest).decode('ascii')}"
    assert response.headers["x-citadel-skill-sha256"] == sha256
    assert response.headers["x-citadel-skill-integrity"] == connect["integrity"]
    assert response.headers["etag"] == f"\"sha256-{sha256}\""
    assert response.headers["cache-control"] == "public, max-age=300"


def test_static_assets_are_short_cacheable() -> None:
    client = TestClient(app)

    response = client.get("/static/login.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_get_skill_alias() -> None:
    client = TestClient(app)
    catalog = client.get("/skills").json()
    connect = next(item for item in catalog["skills"] if item["slug"] == "connect")

    response = client.get("/skills/mcp")
    assert response.status_code == 200
    assert "Citadel MCP Connector" in response.text
    assert response.headers["x-citadel-skill-sha256"] == connect["sha256"]


def test_get_skill_onboard() -> None:
    client = TestClient(app)
    catalog = client.get("/skills").json()
    onboard = next(item for item in catalog["skills"] if item["slug"] == "onboard")

    response = client.get("/skills/onboard")
    assert response.status_code == 200
    assert "citadel onboard" in response.text
    assert response.headers["x-citadel-skill-sha256"] == onboard["sha256"]

    alias = client.get("/skills/citadel-onboard")
    assert alias.status_code == 200
    assert alias.content == response.content


def test_get_skill_cli_and_debug() -> None:
    client = TestClient(app)
    cli = client.get("/skills/cli")
    assert cli.status_code == 200
    assert "citadel update" in cli.text
    debug = client.get("/skills/debug")
    assert debug.status_code == 200
    assert "/healthz" in debug.text
    assert "/api/health" in debug.text


def test_codex_plugin_skills_dir_resolves() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    plugin_skills = (root / "plugins/citadel-archive-mcp/skills").resolve()
    assert plugin_skills.is_dir()
    assert (plugin_skills / "citadel" / "SKILL.md").is_file()
    assert (plugin_skills / "citadel-cli" / "SKILL.md").is_file()


def test_sdist_force_includes_skills_tree() -> None:
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    )
    sdist = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sdist["force-include"]["skills"] == "skills"
    assert "plugins/citadel-archive-mcp/skills" in sdist["exclude"]


def test_get_skill_unknown() -> None:
    client = TestClient(app)
    response = client.get("/skills/not-a-real-skill")
    assert response.status_code == 404


def test_get_skill_boundary_alias() -> None:
    client = TestClient(app)
    response = client.get("/skills/privacy")
    assert response.status_code == 200
    assert "Public vs Private" in response.text


def test_refresh_skill_catalog_tracks_changes(tmp_path) -> None:
    import json

    from kb.skills import refresh_skill_catalog

    state = tmp_path / "skills_catalog.json"

    first = refresh_skill_catalog(state)
    assert first["ok"] is True
    assert first["skills"] == 9
    assert first["added"] == [
        "boundary",
        "citadel",
        "cli",
        "connect",
        "debug",
        "onboard",
        "proactive-ingest",
        "search",
        "vault",
    ]
    assert first["changed"] == []
    assert first["removed"] == []

    second = refresh_skill_catalog(state)
    assert second["added"] == []
    assert second["changed"] == []

    # Simulate a stale recorded hash: the next refresh reports the change.
    stored = json.loads(state.read_text(encoding="utf-8"))
    stored["skills"]["vault"] = "0" * 64
    state.write_text(json.dumps(stored), encoding="utf-8")

    third = refresh_skill_catalog(state)
    assert third["changed"] == ["vault"]


def test_refresh_skill_catalog_recovers_from_corrupt_state(tmp_path) -> None:
    from kb.skills import refresh_skill_catalog

    state = tmp_path / "skills_catalog.json"
    state.write_text("{not json", encoding="utf-8")

    result = refresh_skill_catalog(state)

    assert result["ok"] is True
    assert result["added"] == [
        "boundary",
        "citadel",
        "cli",
        "connect",
        "debug",
        "onboard",
        "proactive-ingest",
        "search",
        "vault",
    ]
