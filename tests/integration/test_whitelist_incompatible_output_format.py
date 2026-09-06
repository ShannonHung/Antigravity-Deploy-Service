"""What an incompatible `output_format` declaration does to a live deployment.

Rejecting `output_format: "json"` alongside `logged: true` / `disconnects_ssh:
true` at load time is what keeps `output_json_error` honest (see
docs/adr/0001-parsed-command-output.md). But the whitelist is re-read on every
request, so a file that starts failing validation takes down every command for
that user — not only the JSON-capable one.

These tests pin that blast radius at the HTTP boundary, so the cost of the
design is recorded rather than discovered during an upgrade.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import command_executor as ce_mod


def _token(client, account="test_admin"):
    r = client.post("/token", data={"username": account, "password": "secret"})
    return r.json()["access_token"]


@pytest.fixture
def whitelist_dir(tmp_path, monkeypatch):
    """Point COMMAND_CONFIG_DIR at a temp dir holding one user's whitelist."""

    def _write(commands):
        path = tmp_path / "allow-commands-test_admin.json"
        path.write_text(
            json.dumps({"name": "test_admin", "allow_commands": commands}, indent=2)
        )
        return path

    # command_executor binds `settings` at import time, so patch that object
    # rather than the environment (mirrors tests/unit/test_whitelist_invalid_regex).
    monkeypatch.setattr(ce_mod.settings, "COMMAND_CONFIG_DIR", str(tmp_path))
    yield _write


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


_REBOOT = {
    "command_name": "reboot",
    "disconnects_ssh": True,
    "pipeline": [{"command": ["reboot"]}],
}
_JSON_LOGGED = {
    "command_name": "report",
    "logged": True,
    "output_format": "json",
    "pipeline": [{"command": ["python3", "report.py"]}],
}


def _info(client):
    return client.get(
        "/api/v1/command/info",
        headers={"Authorization": f"Bearer {_token(client)}"},
    )


def test_logged_json_whitelist_is_rejected_as_a_config_error(whitelist_dir, client):
    """A file the operator broke must surface as WHITELIST_CONFIG_ERROR (500).

    Not a 400 (the caller did nothing wrong) and not a bare INTERNAL_ERROR,
    which would leave the operator with no idea what to fix.
    """
    whitelist_dir([_JSON_LOGGED])

    r = _info(client)

    assert r.status_code == 500, r.text
    assert r.json()["error"]["code"] == "WHITELIST_CONFIG_ERROR"


def test_config_error_names_the_offending_command_and_reason(whitelist_dir, client):
    """The operator has to be able to fix it from the response alone.

    A 500 that only says "invalid whitelist" sends them reading source; the
    message must carry the command name and which flag conflicts.
    """
    whitelist_dir([_REBOOT, _JSON_LOGGED])

    message = _info(client).json()["error"]["message"]

    assert "report" in message, "must name the offending command"
    assert "logged" in message, "must name the conflicting flag"
    assert "output_format" in message, "must name the setting that conflicts"


def test_one_bad_entry_takes_down_every_command_for_that_user(whitelist_dir, client):
    """The blast radius, pinned deliberately.

    The whitelist is validated as a whole on every request, so a single
    incompatible entry disables commands that have nothing to do with JSON
    output — `reboot` here. That is the accepted cost of failing at load rather
    than at poll time (ADR 0001); this test exists so the cost is a recorded
    decision rather than a surprise during an upgrade.
    """
    whitelist_dir([_REBOOT, _JSON_LOGGED])

    # `reboot` is text-output, fire-and-forget, entirely unrelated to format=json.
    r = client.get(
        "/api/v1/command/reboot/info",
        headers={"Authorization": f"Bearer {_token(client)}"},
    )

    assert r.status_code == 500, "an unrelated command is collateral damage"
    assert r.json()["error"]["code"] == "WHITELIST_CONFIG_ERROR"


def test_removing_the_bad_entry_restores_the_other_commands(whitelist_dir, client):
    """The failure is not sticky: fixing the file fixes the user immediately.

    The whitelist is re-read per request rather than cached at startup, so an
    operator's edit takes effect without a redeploy. That is what makes the
    blast radius above tolerable.
    """
    whitelist_dir([_REBOOT, _JSON_LOGGED])
    assert _info(client).status_code == 500

    whitelist_dir([_REBOOT])  # operator removes the incompatible entry

    r = _info(client)
    assert r.status_code == 200, r.text
    names = [c["command_name"] for c in r.json()["data"]["allow_commands"]]
    assert names == ["reboot"]


def test_disconnects_ssh_with_json_is_rejected_the_same_way(whitelist_dir, client):
    """The other incompatible pairing reaches the operator identically."""
    whitelist_dir(
        [
            {
                "command_name": "reboot_json",
                "disconnects_ssh": True,
                "output_format": "json",
                "pipeline": [{"command": ["reboot"]}],
            }
        ]
    )

    r = _info(client)

    assert r.status_code == 500
    assert "disconnects_ssh" in r.json()["error"]["message"]


def test_a_valid_json_command_still_loads(whitelist_dir, client):
    """The guard must not reject the configuration it exists to enable."""
    whitelist_dir(
        [
            _REBOOT,
            {
                "command_name": "report",
                "output_format": "json",
                "pipeline": [{"command": ["python3", "report.py"]}],
            },
        ]
    )

    r = _info(client)

    assert r.status_code == 200, r.text
    by_name = {c["command_name"]: c for c in r.json()["data"]["allow_commands"]}
    assert by_name["report"]["output_format"] == "json"
    assert by_name["reboot"]["output_format"] == "text"
