from pathlib import Path

from fastapi.testclient import TestClient

import kb.server as server_module

from kb.server import ADMIN_COOKIE, app


def _client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def test_next_login_routes_readers_to_next_and_privileged_roles_to_legacy_app() -> None:
    source = (
        Path(server_module.__file__).resolve().parent.parent / "web" / "src" / "pages" / "login.tsx"
    ).read_text(encoding="utf-8")

    assert 'session.role === "reader" ? "/next/app" : "/app"' in source
    assert 'window.location.assign("/app")' not in source

    compiled = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (server_module.WEBUI_DIR / "_next/static/chunks/pages").glob("login-*.js")
    )
    assert "reader" in compiled
    assert '"/next/app"' in compiled
    assert '"/app"' in compiled


def test_browser_logout_redirects_to_login_and_deletes_cookie() -> None:
    client = _client()
    client.cookies.set(ADMIN_COOKIE, "session-cookie", domain="testserver", path="/")

    response = client.post(
        "/admin/logout",
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert f'{ADMIN_COOKIE}=""' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_api_logout_keeps_json_contract_and_deletes_cookie() -> None:
    client = _client()
    client.cookies.set(ADMIN_COOKIE, "session-cookie", domain="testserver", path="/")

    response = client.post(
        "/admin/logout",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "location" not in response.headers
    assert f'{ADMIN_COOKIE}=""' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_legacy_fetch_without_html_accept_keeps_json_logout_contract() -> None:
    response = _client().post(
        "/admin/logout",
        headers={"Accept": "*/*"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
