"""Optional JSON parsing of a finished command's stdout.

Isolated from the poll endpoint because the *policy* — when parsing is even
attempted — is the subtle part, and it is driven by a fact that is easy to miss:
``CommandState.output`` is not always stdout. Four different producers write
it, and only the first is the command's actual standard output:

1. Fast-path success (``command_executor._store_result``) — real stdout.
2. Any failure — backfilled from ``SshSupport._read_log_tail``, i.e. the last N
   lines of a text run log. Essentially never valid JSON.
3. Cross-pod healed success (``StateHelpers._heal_from_marker``) — the empty
   string, because the heal path reconstructs the exit code from the
   control_node marker and never had the output.
4. Healthy success of a ``logged`` command (``_apply_output_policy``) — also the
   empty string, and on purpose: a logged run's output lives in the
   control_node run log served by ``/trace`` and ``/view``, so none is persisted
   on the state. This one is NOT a data loss, which is why
   ``CommandWhitelistConfig`` rejects ``logged`` + ``output_format: "json"`` at
   load time rather than letting it be misreported here as
   ``output_unavailable``.

Only (1) is parseable, so attempting to parse (2) and (3) would emit a
``parse_failed`` on every failed run — turning the signal that a script broke
its contract into constant noise. Each non-parse reason therefore gets its own
enum value, so every value of ``output_json_error`` corresponds to a distinct
operator response.
"""

import json
import logging
from typing import Any, Optional, Tuple

from app.core.exceptions import CommandExecutionException
from app.domain.command import (
    CommandOutputFormat,
    CommandState,
    CommandStatus,
    OutputFormat,
    OutputJsonError,
)

logger = logging.getLogger(__name__)


def ensure_format_allowed(state: CommandState, fmt: OutputFormat) -> None:
    """Reject ``?format=json`` on a command that never promised JSON.

    A command whose whitelist entry says ``output_format: "text"`` will never
    produce parseable stdout, so asking for JSON is a caller mistake that can be
    answered immediately rather than via a ``parse_failed`` the caller can do
    nothing about.

    Raises:
        CommandExecutionException: 400, when the caller asked for JSON but the
            command's contract declares text output.
    """
    if fmt is not OutputFormat.JSON:
        return
    if state.output_format is CommandOutputFormat.JSON:
        return
    raise CommandExecutionException(
        f"Command does not emit JSON (output_format="
        f"{state.output_format.value}); ?format=json is not available for it.",
        detail={
            "command_id": state.command_id,
            "output_format": state.output_format.value,
            "requested_format": fmt.value,
        },
    )


def parse_output(
    state: CommandState,
) -> Tuple[Optional[Any], Optional[OutputJsonError]]:
    """Parse ``state.output`` as JSON, following the attempt policy above.

    The caller is expected to have already passed the state through
    ``ensure_format_allowed``. Returns ``(parsed, None)`` on success, otherwise
    ``(None, reason)``.

    Raw ``json.JSONDecodeError`` text is logged but never returned: it quotes the
    offending fragment of remote output, which may carry sensitive data.
    """
    if state.status != CommandStatus.SUCCESS:
        # Not a failure of the script — there is simply no stdout to parse.
        return None, OutputJsonError.NOT_APPLICABLE

    if not state.output:
        # Succeeded, but there is no stdout to parse. Distinct from a broken
        # script: nothing was emitted that could have failed to parse.
        #
        # Whitelist validation rules out the two configurations that would make
        # this routine (logged / disconnects_ssh), so reaching here means the
        # output was genuinely lost — in practice, cross-pod orphan-run recovery,
        # which reconstructs the exit code from the control_node marker and never
        # recovers stdout. The log states what we can actually observe rather
        # than asserting a cause we cannot verify from the state alone.
        logger.warning(
            "output_json unavailable for %s: command succeeded but output is "
            "%s (no stdout was persisted for this run)",
            state.command_id,
            "unset" if state.output is None else "empty",
            extra={"command_id": state.command_id, "request_id": state.request_id},
        )
        return None, OutputJsonError.OUTPUT_UNAVAILABLE

    try:
        return json.loads(state.output), None
    except (json.JSONDecodeError, ValueError) as exc:
        # The command declared output_format: "json" and still emitted
        # something else. That is a broken contract on the remote side, so it is
        # ERROR — the same level this codebase gives a malformed whitelist,
        # which is the same class of problem (operator-owned config is wrong).
        logger.error(
            "output_json parse failed for %s: command declares output_format="
            "json but stdout is not valid JSON (%s)",
            state.command_id,
            exc,
            extra={"command_id": state.command_id, "request_id": state.request_id},
        )
        return None, OutputJsonError.PARSE_FAILED
