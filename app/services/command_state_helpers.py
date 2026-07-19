import logging
import shlex
from typing import Optional

from app.domain.command import CommandState, CommandStatus
from app.core.config import get_settings
from app.repositories.command_state_repository import CommandStateRepository
from app.services.command_ssh import SshSupport
from app.core.exceptions import (
    CommandExecutionException, NotFoundException, BaseAppException,
)

logger = logging.getLogger(__name__)
settings = get_settings()


class StateHelpers:
    """Redis state load + lazy orphan-run heal from the control_node exit marker."""

    def __init__(self, repo: CommandStateRepository, ssh: SshSupport):
        self.repo = repo
        self._ssh = ssh

    async def _get_state_or_404(self, command_id: str) -> CommandState:
        """Load a CommandState from Redis or raise NotFoundException.

        Shared by the poll and trace endpoints — both 404 on an unknown id.
        """
        try:
            return await self.repo.get(command_id)
        except CommandExecutionException as exc:
            raise NotFoundException(
                f"Command {command_id} not found.",
                detail={"command_id": command_id},
            ) from exc

    def _exit_marker_path(self, run_log_path: str) -> str:
        """Sidecar path for a run's exit code: ``<run_id>.log`` → ``<run_id>.exit``."""
        if run_log_path.endswith(".log"):
            return run_log_path[: -len(".log")] + ".exit"
        return run_log_path + ".exit"

    async def _read_run_exit_marker(self, state: CommandState) -> Optional[int]:
        """SSH to the control_node and read the run's exit-code sidecar.

        Returns the integer exit code if ``run-ansible.sh`` has finished and
        written ``<run_id>.exit``; ``None`` if the file is absent (run still in
        flight) or its contents are unparseable. The path is server-generated;
        ``shlex.quote`` keeps the anti-injection guarantee regardless.

        Raises:
            UpstreamTimeoutException / UpstreamUnavailableException:
                SSH connect failure (propagated; the caller decides whether to
                fall back to the last-known state).
        """
        marker_path = self._exit_marker_path(state.run_log_path)
        conn = await self._ssh._connect_to_control_node(state)
        try:
            quoted = shlex.quote(marker_path)
            res = await conn.run(f"cat {quoted}", check=False)
            if res.exit_status != 0:
                return None  # sidecar not written yet → still running
            raw = str(res.stdout).strip() if res.stdout else ""
            try:
                return int(raw)
            except ValueError:
                # Half-written or corrupt marker — treat as not-yet-final.
                logger.warning(
                    f"Unparseable exit marker for {state.command_id}: {raw!r}",
                    extra={"command_id": state.command_id},
                )
                return None
        finally:
            conn.close()

    async def _heal_from_marker(self, state: CommandState) -> CommandState:
        """Lazily reconcile a stuck transient state from the control_node marker.

        Applies to RUNNING and KILLING. Reads ``<run_id>.exit``; if present,
        transitions Redis to SUCCESS / FAILED via ``update_if`` gated on
        ``status in (RUNNING, KILLING)`` — so a concurrent fast-path write or a
        completed kill (which lands on the terminal KILLED) always wins the race
        and is never overwritten. Returns the (possibly) refreshed state. SSH
        failures are swallowed — a transient control_node outage must not turn a
        poll into a 5xx; the caller keeps reporting the last-known state until
        the next poll.
        """
        try:
            code = await self._read_run_exit_marker(state)
        except BaseAppException as exc:
            logger.info(
                f"Heal read failed for {state.command_id}; reporting last-known state: {exc}",
                extra={"command_id": state.command_id},
            )
            return state
        if code is None:
            return state  # no marker yet — genuinely still running

        success = code == 0
        # On failure, backfill the API output from the control_node log tail so a
        # cross-pod recovered failure shows its real error (mirrors the fast
        # path). None (SSH failure / no log) leaves output unset — never a 5xx.
        tail = None
        if not success:
            tail = await self._ssh._read_log_tail(
                state, settings.COMMAND_LOG_FAILURE_TAIL_LINES,
            )
        async def updater(s: CommandState):
            if success:
                s.mark_success(code, "")
            else:
                s.mark_failed(
                    f"Recovered from control_node marker: exit {code}.",
                    exit_code=code,
                    output=tail,
                )

        healed = await self.repo.update_if(
            state.command_id,
            condition=lambda s: s.status in (CommandStatus.RUNNING, CommandStatus.KILLING),
            updater=updater,
            ttl_seconds=settings.COMMAND_RESULT_TTL_SECONDS,
        )
        if healed:
            logger.info(
                f"Healed orphaned run {state.command_id} from marker: exit {code} "
                f"({'success' if success else 'failed'}).",
                extra={"command_id": state.command_id},
            )
            return await self.repo.get(state.command_id)
        # Lost the race (fast path / kill already wrote a terminal state).
        return await self.repo.get(state.command_id)
