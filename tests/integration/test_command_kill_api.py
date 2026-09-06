"""The kill endpoint must not lie.

Found in manual testing: POST /execution/{id}/kill on a RUNNING command that is
`killable: false` returned `{"status": "accepted", "message": "Kill request
accepted"}` — but the service correctly refused to kill it (it's non-killable),
so nothing happened. The 'accepted' response was misleading. A non-killable
command must be rejected up front with a 409, mirroring the existing
"wrong state" rejection.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.core.dependencies import get_command_state_repository, get_command_service
from app.domain.command import CommandState, CommandStatus


def _state(killable: bool, status=CommandStatus.RUNNING):
    return CommandState(
        command_id="c1",
        status=status,
        host="localhost",
        resolved_ip="127.0.0.1",
        port=2224,
        username="root",
        ssh_config="control_node",
        request_id="r1",
        exec_command="x",
        killable=killable,
        run_log_path="/var/log/ansible-runs/c1.log",
    )


class _Repo:
    def __init__(self, state):
        self._state = state

    async def get(self, command_id):
        return self._state


def _client(state, kill_called):
    app = create_app()
    app.dependency_overrides[get_command_state_repository] = lambda: _Repo(state)

    class _Svc:
        repo = _Repo(state)

        async def kill_command(self, command_id, message="Killed", force=False):
            kill_called.append((command_id, force))

    app.dependency_overrides[get_command_service] = lambda: _Svc()
    return app


def _token(client):
    r = client.post("/token", data={"username": "test_admin", "password": "secret"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_kill_non_killable_without_force_is_rejected_409():
    called = []
    app = _client(_state(killable=False), called)
    with TestClient(app) as c:
        tok = _token(c)
        r = c.post(
            "/api/v1/command/execution/c1/kill",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 409, r.text
    assert "accepted" not in r.text.lower()
    # The 409 must point the user at the override.
    assert "force" in r.text.lower()
    # The service kill must not even be invoked.
    assert called == []
    app.dependency_overrides.clear()


def test_kill_non_killable_with_force_overrides():
    # Human override: ?force=true kills a non-killable command, forwarding
    # force=True to the service so it bypasses its own killable guard.
    called = []
    app = _client(_state(killable=False), called)
    with TestClient(app) as c:
        tok = _token(c)
        r = c.post(
            "/api/v1/command/execution/c1/kill?force=true",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "accepted"
    assert called == [("c1", True)]
    app.dependency_overrides.clear()


def test_kill_killable_running_is_accepted():
    called = []
    app = _client(_state(killable=True), called)
    with TestClient(app) as c:
        tok = _token(c)
        r = c.post(
            "/api/v1/command/execution/c1/kill",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "accepted"
    # Normal kill: force defaults to False.
    assert called == [("c1", False)]
    app.dependency_overrides.clear()


def test_kill_non_running_still_rejected_409():
    # Regression: existing "wrong state" rejection must still hold — even with
    # force (a finished command has nothing to kill).
    called = []
    app = _client(_state(killable=True, status=CommandStatus.SUCCESS), called)
    with TestClient(app) as c:
        tok = _token(c)
        r = c.post(
            "/api/v1/command/execution/c1/kill?force=true",
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 409, r.text
    assert called == []
    app.dependency_overrides.clear()
