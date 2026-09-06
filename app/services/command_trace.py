import logging
import shlex

from app.domain.command import CommandState, CommandLogLine, CommandTraceResponse
from app.core.config import get_settings
from app.core.log_renderer import LogRenderer
from app.services.command_ssh import SshSupport
from app.services.command_state_helpers import StateHelpers

logger = logging.getLogger(__name__)
settings = get_settings()


class CommandTrace:
    """Incremental remote-log tail rendered for the UI log viewer."""

    def __init__(self, state: StateHelpers, ssh: SshSupport):
        self._state = state
        self._ssh = ssh

    async def _read_remote_log(
        self, state: CommandState, byte_offset: int
    ) -> tuple[int, str]:
        """SSH to the control_node and read the run log tail.

        Returns ``(total_size, new_text)``. If the file does not exist yet
        (run just started), returns ``(0, "")``. The log path is
        server-generated and passed as a discrete argument (no shell metachars).

        Raises:
            UpstreamTimeoutException / UpstreamUnavailableException:
                SSH connect failure when reading the log (mirrors ``_connect``).
        """
        path = state.run_log_path
        conn = await self._ssh._connect_to_control_node(state)
        try:
            # asyncssh's conn.run takes ONE command string, not argv. The path
            # is server-generated, but shlex.quote keeps the anti-injection
            # guarantee (and handles spaces) all the same.
            quoted_path = shlex.quote(path)
            size_res = await conn.run(f"stat -c %s {quoted_path}", check=False)
            if size_res.exit_status != 0:
                return 0, ""  # file not created yet
            total_size = int(str(size_res.stdout).strip() or "0")
            if total_size > settings.COMMAND_LOG_HARD_CAP_BYTES:
                # Caller bails out with too_large and discards the body anyway,
                # so don't pull a multi-MB tail back over SSH just to drop it.
                return total_size, ""
            tail_res = await conn.run(
                f"tail -c +{byte_offset + 1} {quoted_path}",
                check=False,
            )
            new_text = str(tail_res.stdout) if tail_res.stdout else ""
            return total_size, new_text
        finally:
            conn.close()

    async def get_command_trace(
        self, command_id: str, byte_offset: int = 0, line_num: int = 1
    ) -> CommandTraceResponse:
        """Incremental tail of a logged command's run log for the UI viewer.

        Loads the CommandState pointer from Redis, SSHes to the control_node,
        and returns the newly-appended log lines (rendered to HTML) plus the
        new byte/line cursors. Honours soft/hard size caps.

        Raises:
            NotFoundException: command_id unknown.
        """
        state = await self._state._get_state_or_404(command_id)

        status = (
            state.status.value if hasattr(state.status, "value") else str(state.status)
        )

        if not state.run_log_path:
            # Command wasn't run with logged:true, so no run log was ever
            # tee'd to the control_node. Flag it so the viewer shows an
            # explanatory notice instead of polling an empty page forever.
            return CommandTraceResponse(
                command_id=command_id,
                status=status,
                next_byte_offset=byte_offset,
                next_line_num=line_num,
                lines=[],
                not_logged=True,
            )

        total_size, new_text = await self._read_remote_log(state, byte_offset)

        if total_size > settings.COMMAND_LOG_HARD_CAP_BYTES:
            # Give up rendering, but tell the user exactly where to read the full
            # log on the control_node (ssh + tail), since the browser can't.
            return CommandTraceResponse(
                command_id=command_id,
                status=status,
                next_byte_offset=byte_offset,
                next_line_num=line_num,
                lines=[],
                total_size=total_size,
                too_large=True,
                log_host=state.resolved_ip,
                log_port=state.port,
                log_user=state.username,
                log_file_path=state.run_log_path,
            )

        size_warning = total_size > settings.COMMAND_LOG_SOFT_CAP_BYTES

        # Hold back a trailing partial line so we never render half a line.
        next_byte_offset = total_size
        if new_text and not new_text.endswith("\n"):
            last_nl = new_text.rfind("\n")
            if last_nl == -1:
                return CommandTraceResponse(
                    command_id=command_id,
                    status=status,
                    next_byte_offset=byte_offset,
                    next_line_num=line_num,
                    lines=[],
                    total_size=total_size,
                    size_warning=size_warning,
                )
            held_back = len(new_text) - (last_nl + 1)
            new_text = new_text[: last_nl + 1]
            next_byte_offset = total_size - held_back

        if not new_text:
            return CommandTraceResponse(
                command_id=command_id,
                status=status,
                next_byte_offset=next_byte_offset,
                next_line_num=line_num,
                lines=[],
                total_size=total_size,
                size_warning=size_warning,
            )

        rendered = LogRenderer().render(0, new_text, start_line_num=line_num)
        lines = [
            CommandLogLine(num=l.num, content_html=l.content_html) for l in rendered
        ]

        return CommandTraceResponse(
            command_id=command_id,
            status=status,
            next_byte_offset=next_byte_offset,
            next_line_num=line_num + len(lines),
            lines=lines,
            total_size=total_size,
            size_warning=size_warning,
        )
