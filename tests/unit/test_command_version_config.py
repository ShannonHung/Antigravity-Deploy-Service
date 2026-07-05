import pytest
from pydantic import ValidationError

from app.domain.command import CommandWhitelistConfig, CommandExecutionRequest


def _cfg(**kw):
    base = dict(command_name="run_ansible", pipeline=[{"command": ["run-ansible.sh"]}])
    base.update(kw)
    return CommandWhitelistConfig(**base)


def test_defaults_off():
    cfg = _cfg()
    assert cfg.checks_script_version is False
    assert cfg.min_script_version is None


def test_checks_true_with_valid_min_ok():
    cfg = _cfg(checks_script_version=True, min_script_version="1.2.0")
    assert cfg.checks_script_version is True
    assert cfg.min_script_version == "1.2.0"


def test_checks_true_without_min_rejected():
    with pytest.raises(ValidationError):
        _cfg(checks_script_version=True)


def test_checks_true_with_malformed_min_rejected():
    with pytest.raises(ValidationError):
        _cfg(checks_script_version=True, min_script_version="1.2")


def test_request_min_script_version_optional_and_validated():
    assert CommandExecutionRequest(
        command_name="run_ansible", host="h", username="u"
    ).min_script_version is None
    assert CommandExecutionRequest(
        command_name="run_ansible", host="h", username="u",
        min_script_version="1.5.0",
    ).min_script_version == "1.5.0"
    with pytest.raises(ValidationError):
        CommandExecutionRequest(
            command_name="run_ansible", host="h", username="u",
            min_script_version="bad",
        )
