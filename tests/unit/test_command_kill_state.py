"""kill_command must never strand a command in KILLING.

Bug found in manual testing: a `killable: false` run, when the service shuts
down (shutdown_gracefully → kill_command), was flipped to KILLING and then
returned early *without* transitioning to KILLED — leaving it stuck at KILLING
forever, even after docker finished on the control_node and wrote its marker.

Two guarantees:
  1. A non-killable command is never moved to KILLING (nothing to kill).
  2. Any command that does enter KILLING is reconciled by the marker heal
     (see test_command_orphan_heal for the heal of KILLING).
"""

from unittest.mock import AsyncMock

from app.domain.command import CommandState, CommandStatus, RunningCommandEntry
from app.services.command_service import CommandService
import app.services.command_service as cs


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
        killable=False,
        run_log_path="/var/log/ansible-runs/c1.log",
    )
    base.update(over)
    return CommandState(**base)


class _Repo:
    def __init__(self, state):
        self.state = state

    async def get(self, cid):
        return self.state

    async def update_if(self, cid, condition, updater, ttl_seconds):
        if not condition(self.state):
            return False
        r = updater(self.state)
        if hasattr(r, "__await__"):
            await r
        return True

    async def update(self, cid, updater, ttl_seconds):
        r = updater(self.state)
        if hasattr(r, "__await__"):
            await r


async def test_kill_non_killable_local_does_not_strand_in_killing():
    state = _state(killable=False)
    svc = CommandService(repo=_Repo(state), inventory_repo=None)
    # Present in the local running pool but flagged non-killable.
    cs.pool_add(
        "c1",
        RunningCommandEntry(
            host_ip="1.2.3.4",
            killable=False,
        ),
    )
    try:
        await svc.kill_command("c1")
    finally:
        cs.pool_remove("c1")
    # Must NOT be left in KILLING. Either untouched (RUNNING) so the marker can
    # later heal it, or already terminal — but never the transient KILLING.
    assert state.status != CommandStatus.KILLING


async def test_kill_non_killable_cross_pod_does_not_strand_in_killing():
    # No local entry → cross-pod path; same guarantee.
    state = _state(killable=False)
    svc = CommandService(repo=_Repo(state), inventory_repo=None)
    await svc.kill_command("c1")
    assert state.status != CommandStatus.KILLING


async def test_kill_killable_still_reaches_killed(monkeypatch):
    # Regression guard: the killable path must still end at KILLED.
    state = _state(killable=True, pgids=[906])
    svc = CommandService(repo=_Repo(state), inventory_repo=None)
    entry = RunningCommandEntry(host_ip="1.2.3.4", killable=True, pgids=[906])
    monkeypatch.setattr(svc._lifecycle, "_do_kill_via_connection", AsyncMock())
    cs.pool_add("c1", entry)
    try:
        await svc.kill_command("c1")
    finally:
        cs.pool_remove("c1")
    assert state.status == CommandStatus.KILLED


async def test_force_kill_overrides_non_killable(monkeypatch):
    # Human override: force=True bypasses the killable guard and actually kills,
    # reaching KILLED. (timeout/shutdown never pass force, so they still respect
    # killable — see _timeout_wrapper/shutdown_gracefully, which call without it.)
    state = _state(killable=False, pgids=[906])
    svc = CommandService(repo=_Repo(state), inventory_repo=None)
    entry = RunningCommandEntry(host_ip="1.2.3.4", killable=False, pgids=[906])
    killed = AsyncMock()
    monkeypatch.setattr(svc._lifecycle, "_do_kill_via_connection", killed)
    cs.pool_add("c1", entry)
    try:
        await svc.kill_command("c1", force=True)
    finally:
        cs.pool_remove("c1")
    killed.assert_awaited_once()
    assert state.status == CommandStatus.KILLED


async def test_non_force_kill_still_respects_non_killable(monkeypatch):
    # Without force, a non-killable command is left untouched (no kill, no
    # KILLING) — this is what timeout/shutdown rely on.
    state = _state(killable=False)
    svc = CommandService(repo=_Repo(state), inventory_repo=None)
    killed = AsyncMock()
    monkeypatch.setattr(svc._lifecycle, "_do_kill_via_connection", killed)
    await svc.kill_command("c1")  # force defaults False
    killed.assert_not_awaited()
    assert state.status != CommandStatus.KILLING
    assert state.status != CommandStatus.KILLED
