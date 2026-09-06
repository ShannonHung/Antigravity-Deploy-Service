"""SshSupport._read_log_tail: fetch the tail of a run's control_node log.

Used to backfill the API `output` on a failed `logged` command, whose SSH
channel is empty by design (output redirected to /dev/null on the target).
"""

from unittest.mock import AsyncMock, MagicMock

from app.domain.command import CommandState, CommandStatus
from app.services.command_ssh import SshSupport
from app.core.exceptions import UpstreamUnavailableException


def _state(**over):
    base = dict(
        command_id="c1",
        status=CommandStatus.RUNNING,
        host="h",
        resolved_ip="1.2.3.4",
        port=2224,
        username="root",
        ssh_config="control_node",
        request_id="r1",
        exec_command="x",
        killable=True,
        run_log_path="/var/log/deploy-service/c1.log",
    )
    base.update(over)
    return CommandState(**base)


def _conn_returning(stdout, exit_status=0):
    """A fake asyncssh connection whose .run() returns a result with stdout."""
    result = MagicMock()
    result.stdout = stdout
    result.exit_status = exit_status
    conn = MagicMock()
    conn.run = AsyncMock(return_value=result)
    conn.close = MagicMock()
    return conn


async def test_read_log_tail_returns_tail_text(monkeypatch):
    ssh = SshSupport()
    conn = _conn_returning("line48\nline49\nline50\n")
    monkeypatch.setattr(ssh, "_connect_to_control_node", AsyncMock(return_value=conn))
    out = await ssh._read_log_tail(_state(), 50)
    assert out == "line48\nline49\nline50\n"
    # Path is shlex-quoted inside a `tail -n` command.
    cmd = conn.run.call_args.args[0]
    assert "tail -n 50" in cmd
    assert "/var/log/deploy-service/c1.log" in cmd
    conn.close.assert_called_once()


async def test_read_log_tail_none_when_file_absent(monkeypatch):
    ssh = SshSupport()
    conn = _conn_returning("", exit_status=1)  # tail: no such file
    monkeypatch.setattr(ssh, "_connect_to_control_node", AsyncMock(return_value=conn))
    assert await ssh._read_log_tail(_state(), 50) is None
    conn.close.assert_called_once()


async def test_read_log_tail_none_when_empty(monkeypatch):
    ssh = SshSupport()
    conn = _conn_returning("", exit_status=0)
    monkeypatch.setattr(ssh, "_connect_to_control_node", AsyncMock(return_value=conn))
    assert await ssh._read_log_tail(_state(), 50) is None


async def test_read_log_tail_none_when_no_log_path():
    ssh = SshSupport()
    # No SSH attempt should be made when there's no log path.
    assert await ssh._read_log_tail(_state(run_log_path=None), 50) is None


async def test_read_log_tail_swallows_ssh_failure(monkeypatch):
    ssh = SshSupport()
    monkeypatch.setattr(
        ssh,
        "_connect_to_control_node",
        AsyncMock(side_effect=UpstreamUnavailableException("down")),
    )
    assert await ssh._read_log_tail(_state(), 50) is None


async def test_read_log_tail_swallows_midread_channel_failure(monkeypatch):
    """The channel can drop after connecting (control_node reboot, network
    blip). That must be swallowed like a connect failure — a best-effort
    backfill may never turn a poll into a 5xx.
    """
    import asyncssh

    conn = MagicMock()
    conn.run = AsyncMock(side_effect=asyncssh.ChannelOpenError(1, "channel gone"))
    conn.close = MagicMock()
    ssh = SshSupport()
    monkeypatch.setattr(ssh, "_connect_to_control_node", AsyncMock(return_value=conn))

    assert await ssh._read_log_tail(_state(), 50) is None
    conn.close.assert_called_once()  # still cleaned up
