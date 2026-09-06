"""Browser-first login page.

Swagger's POST /token is an XHR and the browser does not persist its
Set-Cookie, so the HTML log viewer never gets the cookie. GET /login serves a
real form; POST /login validates, sets the access_token cookie, and 302s back
to ``next`` so the browser viewer is authenticated by a plain page navigation.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


def test_login_page_renders_form(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<form" in r.text
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_login_page_preserves_next(client):
    r = client.get("/login", params={"next": "/api/v1/command/execution/abc/view"})
    assert r.status_code == 200
    assert "/api/v1/command/execution/abc/view" in r.text


def test_login_post_sets_cookie_and_redirects(client):
    r = client.post(
        "/login",
        data={
            "username": "test_admin",
            "password": "secret",
            "next": "/api/v1/command/execution/abc/view",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/api/v1/command/execution/abc/view"
    assert "access_token" in r.cookies


def test_login_post_wrong_password_shows_error(client):
    r = client.post(
        "/login",
        data={"username": "test_admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 200  # re-render form, not a redirect
    assert "access_token" not in r.cookies
    assert "<form" in r.text


def test_login_rejects_offsite_next(client):
    # Open-redirect guard: an absolute/offsite next must not be honoured.
    r = client.post(
        "/login",
        data={
            "username": "test_admin",
            "password": "secret",
            "next": "https://evil.example.com/phish",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert "evil.example.com" not in r.headers["location"]
    assert r.headers["location"].startswith("/")


@pytest.mark.e2e
def test_login_then_view_trace_authorised(client):
    # Full browser flow: login (cookie) -> trace/ui authorised (404 unknown id,
    # not 401). Hits the SSH command API, which reads command state from Redis,
    # so this needs a real Redis — hence e2e (skipped unless RUN_E2E=1).
    client.post(
        "/login",
        data={"username": "test_admin", "password": "secret"},
        follow_redirects=False,
    )
    r = client.get("/api/v1/command/execution/does-not-exist/trace/ui")
    assert r.status_code != 401
