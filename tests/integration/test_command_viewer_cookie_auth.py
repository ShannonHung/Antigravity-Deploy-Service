"""Viewer auth: the browser opens /view (unauthed HTML shell), whose JS polls
/trace/ui. A browser cannot attach a Bearer header, so /trace/ui must also
accept the JWT from an HttpOnly cookie that /token sets on login.

This mirrors the user's requirement: log in once (via Swagger's /token), then
the viewer works in the browser; without logging in, /trace/ui stays 401.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.core.dependencies import get_command_state_repository
from app.core.exceptions import CommandExecutionException


class _EmptyCommandStateRepo:
    async def get(self, command_id: str):
        raise CommandExecutionException(f"Execution record {command_id} not found.")


@pytest.fixture
def trace_client():
    app = create_app()
    app.dependency_overrides[get_command_state_repository] = (
        lambda: _EmptyCommandStateRepo()
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_token_sets_access_token_cookie(trace_client):
    r = trace_client.post(
        "/token", data={"username": "test_admin", "password": "secret"}
    )
    assert r.status_code == 200, r.text
    # /token must set an access_token cookie so the browser viewer is authed.
    assert "access_token" in r.cookies


def test_trace_ui_accepts_cookie_token(trace_client):
    # Log in -> cookie is stored on the client's cookie jar.
    r = trace_client.post(
        "/token", data={"username": "test_admin", "password": "secret"}
    )
    assert r.status_code == 200, r.text
    # No Authorization header here; the browser only has the cookie.
    r2 = trace_client.get("/api/v1/command/execution/does-not-exist/trace/ui")
    # Cookie satisfies auth+scope, so we reach the service and get a 404
    # (unknown command_id), NOT a 401.
    assert r2.status_code == 404, r2.text


def test_trace_ui_still_401_without_cookie_or_header(trace_client):
    r = trace_client.get("/api/v1/command/execution/whatever/trace/ui")
    assert r.status_code in (401, 403)


def test_trace_ui_still_accepts_bearer_header(trace_client):
    tok = trace_client.post(
        "/token", data={"username": "test_admin", "password": "secret"}
    ).json()["access_token"]
    # Clear the cookie jar so only the header is exercised.
    trace_client.cookies.clear()
    r = trace_client.get(
        "/api/v1/command/execution/does-not-exist/trace/ui",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 404, r.text
