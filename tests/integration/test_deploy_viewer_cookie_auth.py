"""Viewer auth for the deploy job log viewer, mirroring the command viewer.

The browser opens /jobs/{id}/view (unauthed HTML shell), whose JS polls
/jobs/{id}/trace/ui. A browser cannot attach a Bearer header, so /trace/ui
must also accept the JWT from an HttpOnly cookie that /token sets on login.

This mirrors the user's requirement: log in once (via Swagger's /token), then
the viewer works in the browser; without logging in, /trace/ui stays 401.

Like test_command_viewer_cookie_auth.py, we override the Redis-backed
dependency (the trace cache) with an in-memory fake. The fake returns a cached
trace, which the repository serves WITHOUT any GitLab call — so the authed
path resolves to a deterministic 200 and the test needs neither Redis nor a
live GitLab.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.core.dependencies import get_trace_cache_repository


class _CannedTraceCache:
    """In-memory trace cache that always reports a finished, cached trace.

    Returning a cache hit makes GitlabPipelineRepository.get_job_trace_range
    serve from cache and skip GitLab entirely, so the authed /trace/ui request
    completes offline with a 200.
    """

    async def get(self, project_id, job_id):
        return ("success", b"hello from cache\n")

    async def set(self, *args, **kwargs):
        return None


@pytest.fixture
def trace_client():
    app = create_app()
    app.dependency_overrides[get_trace_cache_repository] = lambda: _CannedTraceCache()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _login(client):
    r = client.post("/token", data={"username": "test_admin", "password": "secret"})
    assert r.status_code == 200, r.text
    return r


def test_token_sets_access_token_cookie(trace_client):
    r = _login(trace_client)
    # /token must set an access_token cookie so the browser viewer is authed.
    assert "access_token" in r.cookies


def test_trace_ui_accepts_cookie_token(trace_client):
    # Log in -> cookie is stored on the client's cookie jar.
    _login(trace_client)
    # No Authorization header here; the browser only has the cookie.
    r = trace_client.get("/api/v1/deploy/jobs/123/trace/ui")
    # Cookie satisfies auth+scope, so we reach the service and the cached
    # trace is served back -> 200, NOT a 401.
    assert r.status_code == 200, r.text


def test_trace_ui_still_401_without_cookie_or_header(trace_client):
    r = trace_client.get("/api/v1/deploy/jobs/123/trace/ui")
    assert r.status_code in (401, 403)


def test_trace_ui_still_accepts_bearer_header(trace_client):
    tok = _login(trace_client).json()["access_token"]
    # Clear the cookie jar so only the header is exercised.
    trace_client.cookies.clear()
    r = trace_client.get(
        "/api/v1/deploy/jobs/123/trace/ui",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text


def test_view_shell_is_unauthed_html(trace_client):
    # The HTML shell itself stays open (same posture as the command viewer);
    # auth is enforced on the /trace/ui endpoint the page polls.
    r = trace_client.get("/api/v1/deploy/jobs/123/view")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
