"""GET /command/execution/{id}?format=json — end to end.

Drives the real CommandService over a fake state repo (rather than stubbing the
service) so the router → service → parse-policy path is actually exercised,
including the backward-compatibility guarantee that an unchanged caller gets an
unchanged response body.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_command_service
from app.core.exceptions import CommandExecutionException
from app.domain.command import CommandOutputFormat, CommandState, CommandStatus
from app.main import create_app
from app.services.command_service import CommandService


def _token(client, account="test_admin"):
    r = client.post("/token", data={"username": account, "password": "secret"})
    return r.json()["access_token"]


def _state(cid="c1", **over):
    base = dict(
        command_id=cid,
        status=CommandStatus.SUCCESS,
        host="h",
        resolved_ip="1.1.1.1",
        port=22,
        username="root",
        ssh_config="default",
        request_id="r",
        exec_command="python3 report.py",
        killable=False,
        output_format=CommandOutputFormat.JSON,
        output='{"disk": "ok"}',
        exit_code=0,
    )
    base.update(over)
    return CommandState(**base)


class _FakeRepo:
    """In-memory repo that also honours ``update_if``, so a RUNNING state can
    actually travel through the heal path rather than short-circuiting."""

    def __init__(self, state):
        self._state = state

    async def get(self, command_id):
        if self._state is None or command_id != self._state.command_id:
            raise CommandExecutionException(f"{command_id} not found")
        return self._state

    async def update_if(self, command_id, condition, updater, ttl_seconds=None):
        if not condition(self._state):
            return False
        result = updater(self._state)
        if hasattr(result, "__await__"):
            await result
        return True


def _client_for(state):
    app = create_app()
    app.dependency_overrides[get_command_service] = lambda: CommandService(
        repo=_FakeRepo(state),
        inventory_repo=None,
    )
    client = TestClient(app)
    return app, client


@pytest.fixture
def make_client():
    created = []

    def _make(state):
        app, client = _client_for(state)
        created.append((app, client))
        client.__enter__()
        return client

    yield _make
    for app, client in created:
        client.__exit__(None, None, None)
        app.dependency_overrides.clear()


def _get(client, cid="c1", **params):
    return client.get(
        f"/api/v1/command/execution/{cid}",
        params=params,
        headers={"Authorization": f"Bearer {_token(client)}"},
    )


# ── backward compatibility ───────────────────────────────────────────────────


def test_no_format_param_leaves_response_unchanged(make_client):
    client = make_client(_state())
    body = _get(client).json()["data"]
    assert body["output"] == '{"disk": "ok"}'  # still the raw string
    assert body["output_json"] is None
    assert body["output_json_error"] is None


def test_format_raw_is_identical_to_omitting_it(make_client):
    client = make_client(_state())
    assert _get(client).json()["data"] == _get(client, format="raw").json()["data"]


def test_text_command_polled_without_format_still_works(make_client):
    # The compatibility guarantee is structural: a command that never declared
    # JSON is unaffected as long as the caller does not ask for it.
    client = make_client(
        _state(output_format=CommandOutputFormat.TEXT, output="root\n")
    )
    r = _get(client)
    assert r.status_code == 200
    assert r.json()["data"]["output"] == "root\n"


# ── format=json ──────────────────────────────────────────────────────────────


def test_json_format_parses_and_keeps_raw_output(make_client):
    client = make_client(_state())
    body = _get(client, format="json").json()["data"]
    assert body["output_json"] == {"disk": "ok"}
    assert body["output_json_error"] is None
    # `output` is never replaced — that is what makes the new field additive.
    assert body["output"] == '{"disk": "ok"}'


def test_json_format_on_text_command_is_400(make_client):
    client = make_client(
        _state(output_format=CommandOutputFormat.TEXT, output="root\n")
    )
    r = _get(client, format="json")
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "COMMAND_EXECUTION_ERROR"


def test_invalid_format_value_is_422(make_client):
    client = make_client(_state())
    assert _get(client, format="yaml").status_code == 422


def test_parse_failure_returns_200_with_the_result_intact(make_client):
    # The whole point of not using a 4xx here: the command succeeded and the
    # caller must still receive status / exit_status / output.
    client = make_client(_state(output="Warning: something\n{"))
    r = _get(client, format="json")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["output_json"] is None
    assert body["output_json_error"] == "parse_failed"
    assert body["status"] == "success"
    assert body["exit_status"] == 0
    assert body["output"] == "Warning: something\n{"


def test_failed_command_reports_not_applicable(make_client):
    client = make_client(
        _state(
            status=CommandStatus.FAILED,
            exit_code=2,
            output="PLAY RECAP\nfailed=1",
            message="exit 2",
        )
    )
    body = _get(client, format="json").json()["data"]
    assert body["output_json_error"] == "not_applicable"
    assert body["output"] == "PLAY RECAP\nfailed=1"


def test_healed_success_reports_output_unavailable(make_client):
    client = make_client(_state(output=""))
    body = _get(client, format="json").json()["data"]
    assert body["output_json_error"] == "output_unavailable"


def test_unknown_command_id_is_404_regardless_of_format(make_client):
    client = make_client(_state())
    assert _get(client, "nope", format="json").status_code == 404


# ── discoverability ──────────────────────────────────────────────────────────


def test_output_format_is_visible_in_the_openapi_schema(make_client):
    client = make_client(_state())
    schema = client.get("/openapi.json").json()
    props = schema["components"]["schemas"]["CommandWhitelistConfig"]["properties"]
    assert "output_format" in props, "callers discover JSON-capable commands here"


# ── the heal path: RUNNING → healed SUCCESS → parse ──────────────────────────


def test_polling_a_running_logged_command_heals_then_reports_unavailable(
    monkeypatch,
    make_client,
):
    """The interaction the feature most depends on, end to end.

    A `logged` run whose launching pod died is still RUNNING in Redis. Polling
    it with ?format=json must heal from the control_node marker first, and the
    heal reconstructs the exit code WITHOUT stdout — so the correct answer is
    output_unavailable, not parse_failed.
    """
    from app.services import command_state_helpers as helpers

    async def _fake_marker(self, state):
        return 0  # the run finished successfully on the control_node

    monkeypatch.setattr(helpers.StateHelpers, "_read_run_exit_marker", _fake_marker)

    client = make_client(
        _state(
            status=CommandStatus.RUNNING,
            output=None,
            exit_code=None,
            run_log_path="/var/log/ansible-runs/c1.log",
        )
    )
    body = _get(client, format="json").json()["data"]

    assert body["status"] == "success", "the heal must have run"
    assert body["exit_status"] == 0
    assert body["output_json"] is None
    assert body["output_json_error"] == "output_unavailable"


def test_running_command_not_yet_finished_reports_not_applicable(
    monkeypatch,
    make_client,
):
    from app.services import command_state_helpers as helpers

    async def _no_marker(self, state):
        return None  # genuinely still running

    monkeypatch.setattr(helpers.StateHelpers, "_read_run_exit_marker", _no_marker)

    client = make_client(
        _state(
            status=CommandStatus.RUNNING,
            output=None,
            exit_code=None,
            run_log_path="/var/log/ansible-runs/c1.log",
        )
    )
    body = _get(client, format="json").json()["data"]
    assert body["status"] == "running"
    assert body["output_json_error"] == "not_applicable"


def test_error_response_body_never_echoes_command_output(make_client):
    """The 400 rejection is serialized to the client — check the real bytes."""
    client = make_client(
        _state(
            output_format=CommandOutputFormat.TEXT,
            output='{"token": "s3cr3t-do-not-echo"}',
        )
    )
    r = _get(client, format="json")
    assert r.status_code == 400
    assert "s3cr3t" not in r.text
