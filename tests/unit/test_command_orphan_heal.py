"""Orphan-run recovery: the control_node log is the source of truth.

If the pod that launched a `logged` run dies, run-ansible.sh keeps running and
finishes, writing a `<run_id>.exit` sidecar + an `=== EXIT <code> ===` marker.
The poll endpoint (`get_command_execution_result`) must SSH back when Redis is
still RUNNING, read the marker, and lazily heal the state — so any pod, any
time, recovers the true outcome. This must be race-safe with the fast path
(`_store_result`), which also gates on status==RUNNING.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.command import CommandState, CommandStatus
from app.services.command_service import CommandService
from app.core.exceptions import NotFoundException, UpstreamUnavailableException


def _state(**over):
    base = dict(
        command_id="c1", status=CommandStatus.RUNNING, host="h",
        resolved_ip="1.2.3.4", port=2224, username="root",
        ssh_config="control_node", request_id="r1", exec_command="x",
        killable=True, run_log_path="/var/log/ansible-runs/c1.log",
    )
    base.update(over)
    return CommandState(**base)


class _FakeRepo:
    """In-memory repo that honours update_if's RUNNING condition, so the
    heal/fast-path race is actually exercised rather than mocked away."""
    def __init__(self, state):
        self._state = state

    async def get(self, command_id):
        return self._state

    async def update_if(self, command_id, condition, updater, ttl_seconds):
        if not condition(self._state):
            return False
        result = updater(self._state)
        if hasattr(result, "__await__"):
            await result
        return True


def _svc(state):
    return CommandService(repo=_FakeRepo(state), inventory_repo=None)


# ── sidecar path derivation ───────────────────────────────────────────────────

def test_exit_marker_path_derived_from_log_path():
    svc = CommandService(repo=None, inventory_repo=None)
    assert svc._state._exit_marker_path("/var/log/ansible-runs/abc.log") == \
        "/var/log/ansible-runs/abc.exit"


# ── heal on poll ──────────────────────────────────────────────────────────────

async def test_poll_heals_to_success_when_marker_says_exit_0(monkeypatch):
    state = _state()
    svc = _svc(state)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", AsyncMock(return_value=0))
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.SUCCESS.value
    assert resp.exit_status == 0
    assert state.status == CommandStatus.SUCCESS  # Redis lazily healed


async def test_poll_heals_to_failed_when_marker_says_nonzero(monkeypatch):
    state = _state()
    svc = _svc(state)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", AsyncMock(return_value=2))
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.FAILED.value
    assert resp.exit_status == 2
    assert state.status == CommandStatus.FAILED


async def test_poll_stays_running_when_no_marker_yet(monkeypatch):
    state = _state()
    svc = _svc(state)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", AsyncMock(return_value=None))
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.RUNNING.value
    assert state.status == CommandStatus.RUNNING  # untouched


async def test_poll_does_not_heal_non_logged_commands(monkeypatch):
    # No run_log_path → nothing to SSH back to; never attempt a marker read.
    state = _state(run_log_path=None)
    svc = _svc(state)
    marker = AsyncMock(return_value=0)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", marker)
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.RUNNING.value
    marker.assert_not_awaited()


async def test_poll_heals_stuck_killing_from_marker(monkeypatch):
    # The exact bug from manual testing: a non-killable run got flipped to
    # KILLING on shutdown and stranded there, even though docker finished
    # (EXIT 0). A stuck KILLING is a transient orphan and MUST be healed.
    state = _state(status=CommandStatus.KILLING, message="Killed")
    svc = _svc(state)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", AsyncMock(return_value=0))
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.SUCCESS.value
    assert resp.exit_status == 0
    assert state.status == CommandStatus.SUCCESS


async def test_poll_killing_stays_killing_without_marker(monkeypatch):
    # KILLING but no marker yet → a genuine kill may still be in progress;
    # leave it as KILLING (don't invent an outcome).
    state = _state(status=CommandStatus.KILLING, message="Killed")
    svc = _svc(state)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", AsyncMock(return_value=None))
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.KILLING.value
    assert state.status == CommandStatus.KILLING


async def test_poll_does_not_heal_terminal_states(monkeypatch):
    # Already SUCCESS (fast path won the race) → don't SSH back at all.
    state = _state(status=CommandStatus.SUCCESS, exit_code=0)
    svc = _svc(state)
    marker = AsyncMock(return_value=2)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", marker)
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.SUCCESS.value
    marker.assert_not_awaited()


async def test_poll_does_not_resurrect_killed_run(monkeypatch):
    # KILLED must never be overwritten by a marker — the condition gate
    # (status==RUNNING) in update_if protects this even if a marker exists.
    state = _state(status=CommandStatus.KILLED, message="killed")
    svc = _svc(state)
    marker = AsyncMock(return_value=0)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", marker)
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.KILLED.value
    marker.assert_not_awaited()


async def test_poll_survives_ssh_failure_when_healing(monkeypatch):
    # If the control_node is briefly unreachable during a heal read, the poll
    # must not 5xx — it falls back to reporting the last known state (RUNNING).
    state = _state()
    svc = _svc(state)
    monkeypatch.setattr(
        svc._state, "_read_run_exit_marker",
        AsyncMock(side_effect=UpstreamUnavailableException("ssh down")),
    )
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.RUNNING.value
    assert state.status == CommandStatus.RUNNING


# ── the SSH marker read itself ────────────────────────────────────────────────

async def test_read_run_exit_marker_returns_int_when_sidecar_present(monkeypatch):
    import shlex
    state = _state(run_log_path="/var/log/ansible-runs/c1.log")
    svc = _svc(state)

    calls = []

    class _R:
        def __init__(self, stdout, exit_status=0):
            self.stdout = stdout
            self.exit_status = exit_status

    fake_conn = MagicMock()

    async def fake_run(command, *args, **kwargs):
        calls.append((command, args))
        # `cat <sidecar>` returns the code; exit 0 means the file exists.
        return _R("2\n", 0)

    fake_conn.run = AsyncMock(side_effect=fake_run)
    fake_conn.close = MagicMock()
    monkeypatch.setattr(
        "app.services.command_ssh.create_authenticator",
        lambda cfg: MagicMock(get_connect_kwargs=lambda: {}),
    )
    monkeypatch.setattr(svc._ssh, "_load_ssh_config", lambda t: MagicMock())
    monkeypatch.setattr(
        "app.services.command_ssh.asyncssh.connect",
        AsyncMock(return_value=fake_conn),
    )

    code = await svc._state._read_run_exit_marker(state)
    assert code == 2
    # One command string, no stray argv (the asyncssh create_session trap).
    assert all(extra == () for _c, extra in calls), calls
    quoted = shlex.quote("/var/log/ansible-runs/c1.exit")
    assert any(quoted in c for c, _ in calls)


async def test_read_run_exit_marker_returns_none_when_absent(monkeypatch):
    state = _state()
    svc = _svc(state)

    class _R:
        def __init__(self, stdout, exit_status):
            self.stdout = stdout
            self.exit_status = exit_status

    fake_conn = MagicMock()
    fake_conn.run = AsyncMock(return_value=_R("", 1))  # cat: no such file
    fake_conn.close = MagicMock()
    monkeypatch.setattr(
        "app.services.command_ssh.create_authenticator",
        lambda cfg: MagicMock(get_connect_kwargs=lambda: {}),
    )
    monkeypatch.setattr(svc._ssh, "_load_ssh_config", lambda t: MagicMock())
    monkeypatch.setattr(
        "app.services.command_ssh.asyncssh.connect",
        AsyncMock(return_value=fake_conn),
    )
    assert await svc._state._read_run_exit_marker(state) is None


async def test_heal_failed_backfills_output_from_log_tail(monkeypatch):
    state = _state()
    svc = _svc(state)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", AsyncMock(return_value=128))
    monkeypatch.setattr(
        svc._state._ssh, "_read_log_tail",
        AsyncMock(return_value="fatal: could not read Username\n"),
    )
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.FAILED.value
    assert resp.exit_status == 128
    assert "could not read Username" in (resp.output or "")


async def test_heal_success_does_not_read_log_tail(monkeypatch):
    state = _state()
    svc = _svc(state)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", AsyncMock(return_value=0))
    tail = AsyncMock(return_value="SHOULD-NOT-BE-USED")
    monkeypatch.setattr(svc._state._ssh, "_read_log_tail", tail)
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.SUCCESS.value
    tail.assert_not_awaited()


async def test_heal_failed_survives_log_tail_none(monkeypatch):
    state = _state()
    svc = _svc(state)
    monkeypatch.setattr(svc._state, "_read_run_exit_marker", AsyncMock(return_value=2))
    monkeypatch.setattr(
        svc._state._ssh, "_read_log_tail", AsyncMock(return_value=None))
    resp = await svc.get_command_execution_result("c1")
    assert resp.status == CommandStatus.FAILED.value
    assert resp.exit_status == 2
    assert resp.output is None


async def test_unknown_command_still_raises_notfound():
    from app.core.exceptions import CommandExecutionException
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=CommandExecutionException("nope"))
    svc = CommandService(repo=repo, inventory_repo=None)
    with pytest.raises(NotFoundException):
        await svc.get_command_execution_result("missing")
