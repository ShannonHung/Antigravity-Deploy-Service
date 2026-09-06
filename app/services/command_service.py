import logging
from typing import List, Optional

from app.domain.command import (
    CommandExecutionRequest,
    CommandExecutionResponse,
    UserCommandWhitelist,
    CommandWhitelistConfig,
    CommandState,
    CommandStatus,
    CommandTraceResponse,
    OutputFormat,
)
from app.services.command_output_format import ensure_format_allowed, parse_output
from app.core.config import get_settings
from app.repositories.command_state_repository import CommandStateRepository
from app.repositories.inventory_repository import InventoryRepository
from app.services.command_ssh import SshSupport
from app.services.command_state_helpers import StateHelpers
from app.services.command_trace import CommandTrace
from app.services.command_lifecycle import CommandLifecycle

# _decode is re-exported here for tests that patch it via this facade.
from app.services.command_executor import (  # noqa: F401  # pylint: disable=unused-import
    CommandExecutor,
    _decode,
)
from app.core.exceptions import (
    CommandExecutionException,
)

logger = logging.getLogger(__name__)
# `logger` and `settings` are retained for test fixtures that patch
# `command_service.settings` to redirect config; the thin facade itself no longer
# reads either (execution logic moved to command_executor / command_pool).
settings = get_settings()

from app.services.command_pool import (  # noqa: F401  # pylint: disable=unused-import
    # Re-exported so callers and tests can reach the pool via this facade.
    pool_add,
    pool_get,
    pool_remove,
    pool_size,
    pool_command_ids,
    _get_semaphore,
)


class CommandService:
    def __init__(
        self,
        repo: CommandStateRepository,
        inventory_repo: Optional[InventoryRepository] = None,
    ):
        self.repo = repo
        self.inventory_repo = inventory_repo
        self._ssh = SshSupport()
        self._state = StateHelpers(repo=self.repo, ssh=self._ssh)
        self._trace = CommandTrace(state=self._state, ssh=self._ssh)
        self._lifecycle = CommandLifecycle(repo=self.repo, ssh=self._ssh)
        self._executor = CommandExecutor(
            repo=self.repo,
            inventory_repo=self.inventory_repo,
            ssh=self._ssh,
            lifecycle=self._lifecycle,
        )

    async def get_command_execution_result(
        self,
        command_id: str,
        fmt: OutputFormat = OutputFormat.RAW,
    ) -> CommandExecutionResponse:
        """Poll the current status / result for a previously submitted command from Redis.

        Orphan-run recovery: if Redis still says RUNNING for a ``logged``
        command, the originating pod may have died mid-run while
        ``run-ansible.sh`` kept going on the control_node. We SSH back, read the
        ``<run_id>.exit`` marker (the log is the source of truth), and lazily
        heal the state. This makes the asyncio.Task (fast path) an optimisation,
        not the only writer of the final result.

        Args:
            fmt: ``OutputFormat.JSON`` additionally parses ``output`` into
                ``output_json``. Only permitted for commands whose whitelist
                entry declared ``output_format: "json"``; ``RAW`` (the default)
                returns exactly the historical response.

        Raises:
            NotFoundException: If the command_id does not exist in Redis.
            CommandExecutionException: If JSON output was requested for a
                command that does not declare it.
        """
        state = await self._state._get_state_or_404(command_id)
        # Validate the caller's request against the command's declared contract
        # BEFORE the heal round-trip: a 400 here is not worth an SSH connection.
        ensure_format_allowed(state, fmt)

        # Heal stuck transient states (RUNNING, or KILLING that never resolved —
        # e.g. a non-killable run flipped to KILLING on shutdown, or the killing
        # pod died) from the control_node marker. Terminal states are left alone.
        if (
            state.status in (CommandStatus.RUNNING, CommandStatus.KILLING)
            and state.run_log_path
        ):
            state = await self._state._heal_from_marker(state)

        output_json, output_json_error = (
            parse_output(state) if fmt is OutputFormat.JSON else (None, None)
        )

        return CommandExecutionResponse(
            status=state.status,
            command_id=state.command_id,
            exit_status=state.exit_code,
            output=state.output,
            message=state.message or "",
            exec_command=state.exec_command,
            host_type=state.host_type,
            resolved_ip=state.resolved_ip,
            pgids=state.pgids,
            output_json=output_json,
            output_json_error=output_json_error,
        )

    async def get_command_trace(
        self, command_id: str, byte_offset: int = 0, line_num: int = 1
    ) -> CommandTraceResponse:
        return await self._trace.get_command_trace(command_id, byte_offset, line_num)

    def get_user_commands(self, username: str) -> UserCommandWhitelist:
        """Return the full command whitelist available to the given user."""
        return self._executor._load_user_whitelist(username)

    def get_command_info(
        self, username: str, command_name: str
    ) -> CommandWhitelistConfig:
        """Return the whitelist definition for a single command.

        Raises:
            CommandExecutionException: If command_name is not in the user's whitelist.
        """
        whitelist = self._executor._load_user_whitelist(username)
        cmd_config = next(
            (c for c in whitelist.allow_commands if c.command_name == command_name),
            None,
        )
        if not cmd_config:
            raise CommandExecutionException(f"Command '{command_name}' not found.")
        return cmd_config

    async def execute_command(
        self,
        username: str,
        request_id: str,
        req: CommandExecutionRequest,
    ) -> CommandExecutionResponse:
        return await self._executor.execute_command(username, request_id, req)

    async def kill_command(
        self, command_id: str, message: str = "Killed", force: bool = False
    ):
        return await self._lifecycle.kill_command(command_id, message, force)

    async def list_running_commands(
        self, statuses: Optional[set[CommandStatus]] = None
    ) -> List[CommandState]:
        return await self._lifecycle.list_running_commands(statuses)

    async def shutdown_gracefully(self):
        return await self._lifecycle.shutdown_gracefully()
