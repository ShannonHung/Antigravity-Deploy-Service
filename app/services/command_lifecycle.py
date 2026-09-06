import asyncio
import logging
from typing import List, Optional

import asyncssh

from app.domain.command import CommandState, CommandStatus
from app.core.config import get_settings
from app.repositories.command_state_repository import CommandStateRepository
from app.repositories.ssh_auth_repository import create_authenticator
from app.services.command_ssh import SshSupport
from app.services.command_pool import pool_get, pool_size, pool_command_ids
from app.core.exceptions import CommandExecutionException

logger = logging.getLogger(__name__)
settings = get_settings()


class CommandLifecycle:
    """Kill / list / graceful-shutdown for running commands (local + cross-pod)."""

    def __init__(self, repo: CommandStateRepository, ssh: SshSupport):
        self.repo = repo
        self._ssh = ssh

    async def kill_command(
        self, command_id: str, message: str = "Killed", force: bool = False
    ):
        """Terminate a running command using two-phase PGID-based kill.

        Phase 1: ``kill -TERM -{pgid}`` (soft kill).
        Phase 2: After a grace period, ``kill -KILL -{pgid}``.

        Transitions state: RUNNING -> KILLING -> KILLED.

        ``force`` is a HUMAN override: a user who explicitly asks to kill a
        ``killable: false`` command (via ``POST /kill?force=true``) bypasses the
        killable guard. Automatic callers — the timeout wrapper and
        ``shutdown_gracefully`` — never pass ``force``, so they keep respecting
        ``killable`` (the flag's purpose is "the system must not kill this on its
        own", not "no one may ever kill this").
        """
        ttl = settings.COMMAND_RESULT_TTL_SECONDS

        # 0. Refuse to kill a non-killable command UNLESS a human forced it. This
        # MUST happen before any state transition: flipping to KILLING and then
        # bailing strands the command in KILLING forever (it has no kill path to
        # reach KILLED). We leave it RUNNING so the marker heal can later resolve
        # its true outcome.
        if not force:
            entry = pool_get(command_id)
            if entry is not None:
                is_killable = entry.killable
            else:
                try:
                    is_killable = (await self.repo.get(command_id)).killable
                except CommandExecutionException:
                    logger.info(
                        f"Kill request for unknown command {command_id}; nothing to do.",
                        extra={"command_id": command_id},
                    )
                    return
            if not is_killable:
                logger.warning(
                    f"Command {command_id} is not killable; leaving state untouched.",
                    extra={"command_id": command_id},
                )
                return

        # 1. Atomic State Transition to KILLING (only from RUNNING).
        success = await self.repo.update_if(
            command_id,
            condition=lambda s: s.status == CommandStatus.RUNNING,
            updater=lambda s: s.mark_killing(message),
            ttl_seconds=ttl,
        )

        if not success:
            logger.info(
                f"Kill request aborted for {command_id}: Command is not in RUNNING state."
            )
            return

        # 2. Perform SSH Kill Logic
        # Try Local Kill First
        entry = pool_get(command_id)
        if entry:
            await self._do_kill_via_connection(entry.conn, entry.pgids, command_id)
            await self.repo.update(command_id, lambda s: s.mark_killed(message), ttl)
            return

        # Try Cross-Pod Kill via Repository
        try:
            state = await self.repo.get(command_id)
        except CommandExecutionException:
            logger.info(
                f"Cross-pod kill for {command_id} aborted; state vanished from Redis.",
                extra={"command_id": command_id},
            )
            return

        if not state.pgids:
            # If no PGIDs yet, we've already marked it as KILLING,
            # so the async task will eventually hit _store_result and be blocked.
            await self.repo.update(command_id, lambda s: s.mark_killed(message), ttl)
            return

        logger.info(
            f"Initiating cross-pod kill for {command_id} on {state.resolved_ip}:{state.port} "
            f"(host_type={state.host_type.value}, raw={state.host})"
        )

        ssh_config = self._ssh._load_ssh_config(state.ssh_config)
        authenticator = create_authenticator(ssh_config)
        conn_kwargs = authenticator.get_connect_kwargs()

        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    host=state.resolved_ip,
                    port=state.port,
                    username=state.username,
                    **conn_kwargs,
                ),
                timeout=10,
            )
            try:
                await self._do_kill_via_connection(conn, state.pgids, command_id)
                await self.repo.update(
                    command_id, lambda s: s.mark_killed(message), ttl
                )
            finally:
                conn.close()
        # Cross-pod kill is best-effort over SSH; any failure is logged and the state left for the heal path.
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(
                f"Failed cross-pod kill for {command_id}: {e}",
                extra={"command_id": command_id},
            )

    async def _do_kill_via_connection(
        self, conn: asyncssh.SSHClientConnection, pgids: List[int], command_id: str
    ):
        for pgid in pgids:
            try:
                logger.info(
                    f"Soft killing PGID {pgid}", extra={"command_id": command_id}
                )
                await conn.run(f"kill -TERM -{pgid}", check=False)
                await asyncio.sleep(settings.COMMAND_KILL_GRACE_SECONDS)
                res = await conn.run(f"kill -0 -{pgid}", check=False)
                if res.exit_status == 0:
                    logger.info(
                        f"Process {pgid} still running, hard killing it.",
                        extra={"command_id": command_id},
                    )
                    await conn.run(f"kill -KILL -{pgid}", check=False)
            # Same best-effort kill guard as above.
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(
                    f"Error killing PGID {pgid}: {e}", extra={"command_id": command_id}
                )

    async def list_running_commands(
        self, statuses: Optional[set[CommandStatus]] = None
    ) -> List[CommandState]:
        """Return command states currently in-flight across all pods.

        Defaults to non-terminal states (RUNNING + KILLING) when no explicit
        status set is given. Reads from Redis so it sees commands started on
        other pods.
        """
        if statuses is None:
            statuses = {CommandStatus.RUNNING, CommandStatus.KILLING}
        return await self.repo.list_states(statuses)

    async def shutdown_gracefully(self):
        """Kill all active commands during application shutdown.

        Called by the FastAPI lifespan handler to ensure no orphan processes
        remain on remote hosts after the API server stops.
        """
        logger.info(f"Shutting down {pool_size()} running commands gracefully.")
        tasks = [self.kill_command(cmd_id) for cmd_id in pool_command_ids()]
        if tasks:
            await asyncio.gather(*tasks)
