import pytest

from app.core.exceptions import ScriptVersionException
from app.services.command_executor import CommandExecutor


class _FakeResult:
    def __init__(self, exit_status, stdout="", stderr=""):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


class _FakeConn:
    def __init__(self, result):
        self._result = result
        self.ran = []

    async def run(self, cmd, check=False):
        self.ran.append(cmd)
        return self._result


def _ctx(checks, whitelist_min, api_min, conn, script="run-ansible.sh"):
    from types import SimpleNamespace

    cmd_config = SimpleNamespace(
        checks_script_version=checks,
        min_script_version=whitelist_min,
    )
    raw_request = SimpleNamespace(min_script_version=api_min)
    return SimpleNamespace(
        cmd_config=cmd_config,
        raw_request=raw_request,
        conn=conn,
        pipeline_cmds=[[script, "--playbook", "ping.yml"]],
    )


def test_parse_version_output_extracts_trailing_semver():
    assert CommandExecutor._parse_version_output("run-ansible.sh 1.4.0") == "1.4.0"
    assert CommandExecutor._parse_version_output("1.4.0\n") == "1.4.0"


def test_parse_version_output_rejects_missing():
    with pytest.raises(ValueError):
        CommandExecutor._parse_version_output("no version here")


async def test_precheck_noop_when_flag_off():
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 1.0.0"))
    ctx = _ctx(checks=False, whitelist_min=None, api_min=None, conn=conn)
    await CommandExecutor._precheck_script_version(None, ctx)
    assert conn.ran == []  # never asked for --version


async def test_precheck_passes_when_actual_ge_min():
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 1.2.0"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min=None, conn=conn)
    await CommandExecutor._precheck_script_version(None, ctx)  # no raise


async def test_precheck_raises_when_actual_below_min():
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 1.0.0"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min=None, conn=conn)
    with pytest.raises(ScriptVersionException):
        await CommandExecutor._precheck_script_version(None, ctx)


async def test_precheck_api_raises_bar_but_cannot_lower():
    # whitelist 1.2.0, API asks 1.5.0 -> effective 1.5.0 -> 1.3.0 fails
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 1.3.0"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min="1.5.0", conn=conn)
    with pytest.raises(ScriptVersionException):
        await CommandExecutor._precheck_script_version(None, ctx)

    # API asks 1.0.0 (lower) -> effective stays 1.2.0 -> 1.2.0 passes
    conn2 = _FakeConn(_FakeResult(0, "run-ansible.sh 1.2.0"))
    ctx2 = _ctx(checks=True, whitelist_min="1.2.0", api_min="1.0.0", conn=conn2)
    await CommandExecutor._precheck_script_version(None, ctx2)  # no raise


async def test_precheck_raises_when_version_call_fails():
    conn = _FakeConn(_FakeResult(127, "", "not found"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min=None, conn=conn)
    with pytest.raises(ScriptVersionException):
        await CommandExecutor._precheck_script_version(None, ctx)


async def test_precheck_raises_when_output_unparseable():
    conn = _FakeConn(_FakeResult(0, "starting up, no version here"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min=None, conn=conn)
    with pytest.raises(ScriptVersionException):
        await CommandExecutor._precheck_script_version(None, ctx)


async def test_precheck_raises_when_actual_malformed():
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 01.2.0"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min=None, conn=conn)
    with pytest.raises(ScriptVersionException):
        await CommandExecutor._precheck_script_version(None, ctx)
