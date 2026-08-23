"""A malformed regex in allow-commands-{user}.json must fail loudly at load.

Before this was validated, a typo such as ``"*"`` (instead of ``".*"``) let the
file parse fine and then blew up with a bare ``re.error`` in the middle of
request validation, surfacing as an opaque unhandled 500.
"""

import json

import pytest
from pydantic import ValidationError

from app.core.exceptions import WhitelistConfigurationException
from app.domain.command import UserCommandWhitelist
import app.services.command_executor as ce_mod
from app.services.command_executor import CommandExecutor


def _whitelist(**overrides) -> dict:
    body = {
        "name": "test_admin",
        "allow_hosts": [".*"],
        "deny_hosts": [],
        "allow_commands": [
            {
                "command_name": "sleep",
                "pipeline": [{"command": ["sleep", "{time}"]}],
                "arguments": [
                    {"name": "time", "type": "int", "validation_regex": "^[0-9]+$"},
                ],
            }
        ],
    }
    body.update(overrides)
    return body


def test_valid_whitelist_still_parses():
    wl = UserCommandWhitelist(**_whitelist())
    assert wl.allow_commands[0].arguments[0].validation_regex == "^[0-9]+$"


def test_empty_validation_regex_is_allowed():
    """An omitted regex means "no regex check" — it must not be compiled."""
    body = _whitelist()
    body["allow_commands"][0]["arguments"][0]["validation_regex"] = ""
    assert UserCommandWhitelist(**body).allow_commands[0].arguments[0].validation_regex == ""


def test_invalid_argument_regex_rejected():
    body = _whitelist()
    body["allow_commands"][0]["arguments"][0]["validation_regex"] = "*"

    with pytest.raises(ValidationError) as exc_info:
        UserCommandWhitelist(**body)

    msg = str(exc_info.value)
    assert "validation_regex" in msg
    assert "'time'" in msg  # names the offending argument
    assert "'*'" in msg     # and the offending pattern


@pytest.mark.parametrize("field", ["allow_hosts", "deny_hosts"])
def test_invalid_host_regex_rejected(field):
    with pytest.raises(ValidationError) as exc_info:
        UserCommandWhitelist(**_whitelist(**{field: ["10.0.0.1", "["]}))

    assert field in str(exc_info.value)


def test_loader_raises_app_exception_not_re_error(tmp_path, monkeypatch):
    """The bad file must surface as a typed 500, not an unhandled re.error."""
    body = _whitelist()
    body["allow_commands"][0]["arguments"][0]["validation_regex"] = "*"
    (tmp_path / "allow-commands-bob.json").write_text(json.dumps(body))
    monkeypatch.setattr(ce_mod.settings, "COMMAND_CONFIG_DIR", str(tmp_path))

    executor = CommandExecutor.__new__(CommandExecutor)
    with pytest.raises(WhitelistConfigurationException) as exc_info:
        CommandExecutor._load_user_whitelist(executor, "bob")

    exc = exc_info.value
    assert exc.http_status == 500
    assert exc.error_code == "WHITELIST_CONFIG_ERROR"
    assert "bob" in exc.message
    assert any("validation_regex" in e for e in exc.detail["errors"])


def test_shipped_whitelists_have_valid_regexes():
    """Guard the committed whitelist files against the same typo.

    Covers the active config dir (tests/fixtures under APP_ENV=test) plus the
    data/*.example.json templates, which are what a new deployment copies —
    the real data/allow-commands-*.json are untracked and machine-specific.
    """
    from pathlib import Path

    from app.core.config import get_settings

    config_dir = Path(get_settings().COMMAND_CONFIG_DIR)
    files = sorted(config_dir.glob("allow-commands-*.json"))
    assert files, f"no whitelist files found in {config_dir}"

    examples = sorted(Path("data").glob("allow-commands-*.example.json"))
    assert examples, "no data/allow-commands-*.example.json templates found"

    for path in files + examples:
        UserCommandWhitelist(**json.loads(path.read_text()))
