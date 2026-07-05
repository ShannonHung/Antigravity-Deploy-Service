import pytest
from unittest.mock import AsyncMock, MagicMock
from app.domain.command import CommandState, CommandStatus
from app.services.command_service import CommandService
from app.core.exceptions import NotFoundException
from app.core.config import get_settings


def _state(**over):
    base = dict(
        command_id="c1", status=CommandStatus.RUNNING, host="h",
        resolved_ip="1.2.3.4", port=2224, username="root",
        ssh_config="control_node", request_id="r1", exec_command="x",
        killable=True, run_log_path="/var/log/ansible-runs/c1.log",
    )
    base.update(over)
    return CommandState(**base)


def _svc_with_state(state):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=state)
    return CommandService(repo=repo, inventory_repo=None)


async def test_trace_no_log_path_returns_empty_with_status():
    svc = _svc_with_state(_state(run_log_path=None, status=CommandStatus.SUCCESS))
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.lines == []
    assert resp.status == "success"
    assert resp.total_size == 0
    assert resp.next_byte_offset == 0
    # A command without a run log (logged:false) is flagged so the viewer can
    # show a "logging not enabled" notice instead of an empty, polling page.
    assert resp.not_logged is True


async def test_trace_logged_command_is_not_flagged_not_logged(monkeypatch):
    # A logged command (run_log_path set) must NOT carry the not_logged flag.
    svc = _svc_with_state(_state(run_log_path="/var/log/deploy-service/c1.log"))
    monkeypatch.setattr(
        svc._trace, "_read_remote_log",
        AsyncMock(return_value=(0, "")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.not_logged is False


async def test_trace_unknown_command_raises_notfound():
    repo = MagicMock()
    from app.core.exceptions import CommandExecutionException
    repo.get = AsyncMock(side_effect=CommandExecutionException("nope"))
    svc = CommandService(repo=repo, inventory_repo=None)
    with pytest.raises(NotFoundException):
        await svc.get_command_trace("missing")


async def test_trace_happy_path_renders_new_lines(monkeypatch):
    svc = _svc_with_state(_state())
    monkeypatch.setattr(
        svc._trace, "_read_remote_log",
        AsyncMock(return_value=(18, "line one\nline two\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.total_size == 18
    assert resp.next_byte_offset == 18
    assert [l.num for l in resp.lines] == [1, 2]
    assert resp.next_line_num == 3


async def test_trace_hard_cap_stops_serving_lines(monkeypatch):
    get_settings.cache_clear()
    svc = _svc_with_state(_state())
    big = get_settings().COMMAND_LOG_HARD_CAP_BYTES + 1
    monkeypatch.setattr(
        svc._trace, "_read_remote_log", AsyncMock(return_value=(big, "x\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.too_large is True
    assert resp.lines == []


async def test_trace_hard_cap_reports_where_to_read_the_log(monkeypatch):
    # When the viewer gives up (too_large), it must tell the user WHERE the full
    # log lives so they can read it directly on the control_node: the host/ip,
    # port, SSH account, and the file path.
    get_settings.cache_clear()
    svc = _svc_with_state(_state(
        resolved_ip="10.0.0.7", port=2224, username="root",
        run_log_path="/var/log/ansible-runs/c1.log",
    ))
    big = get_settings().COMMAND_LOG_HARD_CAP_BYTES + 1
    monkeypatch.setattr(
        svc._trace, "_read_remote_log", AsyncMock(return_value=(big, "x\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.too_large is True
    assert resp.log_host == "10.0.0.7"
    assert resp.log_port == 2224
    assert resp.log_user == "root"
    assert resp.log_file_path == "/var/log/ansible-runs/c1.log"


async def test_trace_normal_response_omits_location_fields(monkeypatch):
    # The location is only meaningful on the too_large bail-out; normal slices
    # leave them None to keep the response lean.
    svc = _svc_with_state(_state())
    monkeypatch.setattr(
        svc._trace, "_read_remote_log",
        AsyncMock(return_value=(18, "line one\nline two\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.log_host is None
    assert resp.log_file_path is None


async def test_read_remote_log_calls_conn_run_with_single_command_string(monkeypatch):
    """asyncssh's conn.run takes ONE command string, not argv. Passing argv
    blew up with 'create_session() takes 2-3 positional arguments but 6 given'.
    The server-generated path must be shlex-quoted into that string."""
    import shlex
    svc = _svc_with_state(_state(run_log_path="/var/log/ansible-runs/c 1.log"))

    calls = []

    class _FakeResult:
        def __init__(self, stdout, exit_status=0):
            self.stdout = stdout
            self.exit_status = exit_status

    fake_conn = MagicMock()

    async def fake_run(command, *args, **kwargs):
        # Record the full positional shape so argv-misuse is detectable:
        # correct code calls run("stat -c %s <path>"); buggy code calls
        # run("stat", "-c", "%s", <path>) which leaves args non-empty.
        calls.append((command, args))
        if command.startswith("stat"):
            return _FakeResult("42\n", 0)
        return _FakeResult("line\n", 0)

    fake_conn.run = AsyncMock(side_effect=fake_run)
    fake_conn.close = MagicMock()

    monkeypatch.setattr(
        "app.services.command_ssh.create_authenticator",
        lambda cfg: MagicMock(get_connect_kwargs=lambda: {}),
    )
    monkeypatch.setattr(svc._ssh, "_load_ssh_config", lambda target: MagicMock())
    monkeypatch.setattr(
        "app.services.command_ssh.asyncssh.connect",
        AsyncMock(return_value=fake_conn),
    )

    total, text = await svc._trace._read_remote_log(svc.repo.get.return_value, 0)

    # Each call must be ONE command string with no extra positional argv
    # (extra argv is exactly what crashed asyncssh's create_session).
    assert all(extra == () for _cmd, extra in calls), calls
    cmds = [cmd for cmd, _ in calls]
    quoted = shlex.quote("/var/log/ansible-runs/c 1.log")
    assert any(c.startswith("stat ") and quoted in c for c in cmds)
    assert any(c.startswith("tail ") and quoted in c for c in cmds)
    assert total == 42


async def test_read_remote_log_skips_tail_when_over_hard_cap(monkeypatch):
    """Over the hard cap the caller discards new_text anyway (too_large bail-out),
    so _read_remote_log must NOT pull the body back over SSH — stat, then stop.
    Otherwise a huge log gets transferred once just to be thrown away."""
    get_settings.cache_clear()
    svc = _svc_with_state(_state())
    big = get_settings().COMMAND_LOG_HARD_CAP_BYTES + 1

    cmds = []

    class _FakeResult:
        def __init__(self, stdout, exit_status=0):
            self.stdout = stdout
            self.exit_status = exit_status

    async def fake_run(command, *args, **kwargs):
        cmds.append(command)
        if command.startswith("stat"):
            return _FakeResult(f"{big}\n", 0)
        return _FakeResult("should-not-be-read\n", 0)

    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(side_effect=fake_run)
    fake_conn.close = MagicMock()

    monkeypatch.setattr(
        "app.services.command_ssh.create_authenticator",
        lambda cfg: MagicMock(get_connect_kwargs=lambda: {}),
    )
    monkeypatch.setattr(svc._ssh, "_load_ssh_config", lambda target: MagicMock())
    monkeypatch.setattr(
        "app.services.command_ssh.asyncssh.connect",
        AsyncMock(return_value=fake_conn),
    )

    total, text = await svc._trace._read_remote_log(svc.repo.get.return_value, 0)

    assert total == big
    assert text == ""
    assert not any(c.startswith("tail") for c in cmds), cmds


async def test_trace_soft_cap_sets_warning_but_serves(monkeypatch):
    get_settings.cache_clear()
    svc = _svc_with_state(_state())
    mid = get_settings().COMMAND_LOG_SOFT_CAP_BYTES + 1
    monkeypatch.setattr(
        svc._trace, "_read_remote_log", AsyncMock(return_value=(mid, "hello\n")),
    )
    resp = await svc.get_command_trace("c1", byte_offset=0, line_num=1)
    assert resp.size_warning is True
    assert resp.too_large is False
    assert len(resp.lines) == 1
