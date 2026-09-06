from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import asyncio
import re
from enum import Enum
import asyncssh
from pydantic import BaseModel, Field, model_validator


def _compile_config_regex(pattern: str, field_label: str) -> "re.Pattern[str]":
    """Compile a regex that came from a whitelist JSON file.

    A typo in the config (e.g. ``"*"`` instead of ``".*"``) must fail loudly at
    load time with a message naming the offending field, rather than raising a
    bare ``re.error`` from deep inside request handling.
    """
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(
            f"{field_label}: invalid regex {pattern!r} ({exc})"
        ) from exc


# ── State Machine Domain Models ──────────────────────────────────────────────

class CommandStatus(str, Enum):
    RUNNING = "running"
    KILLING = "killing"
    KILLED = "killed"
    SUCCESS = "success"
    FAILED = "failed"

class HostType(str, Enum):
    IP = "ip"
    BASTION = "bastion"
    HOSTNAME = "hostname"
    CLUSTER = "cluster"


class OutputFormat(str, Enum):
    """How the caller wants a command's `output` returned.

    `raw` (the default) is byte-for-byte the historical response: `output`
    stays a string and the JSON fields stay null. `json` additionally parses
    `output` into `output_json` — permitted only for commands whose whitelist
    entry declares `output_format: "json"`.
    """
    RAW = "raw"
    JSON = "json"


class CommandOutputFormat(str, Enum):
    """The operator's declaration of what a command emits on stdout.

    Part of the command's contract, not the caller's preference: declaring
    `json` PERMITS `?format=json`, it never triggers parsing on its own.
    """
    TEXT = "text"
    JSON = "json"


class OutputJsonError(str, Enum):
    """Why `output_json` is null even though `?format=json` was requested.

    Each value maps to a different operational response, which is why they are
    distinct rather than one generic failure:

    - `parse_failed` — the command declared `output_format: "json"` but its
      stdout is not valid JSON. The remote script broke its own contract; this
      is logged server-side at ERROR.
    - `output_unavailable` — the command succeeded, but its stdout was lost
      during cross-pod orphan-run recovery, which reconstructs the exit code
      from the control_node marker without the output. Logged at WARNING.
    - `not_applicable` — the command is not in a success state, so there was
      never stdout to parse. Not logged.
    """
    PARSE_FAILED = "parse_failed"
    OUTPUT_UNAVAILABLE = "output_unavailable"
    NOT_APPLICABLE = "not_applicable"

class CommandState(BaseModel):
    command_id: str
    status: CommandStatus
    output: Optional[str] = None
    exit_code: Optional[int] = None
    message: Optional[str] = None
    run_log_path: Optional[str] = None  # control_node path of the tee'd run log

    # execution metadata
    host: str
    host_type: HostType = HostType.IP
    resolved_ip: str
    port: int
    username: str
    ssh_config: str
    request_id: str
    exec_command: str
    # Snapshot of the whitelist's stdout contract, captured at launch. Stored on
    # the state (rather than re-read from the whitelist at poll time) so an
    # operator editing allow-commands mid-flight cannot retroactively change how
    # a already-finished command's output is interpreted. Defaulted for states
    # written by an older pod during a rolling upgrade.
    output_format: CommandOutputFormat = CommandOutputFormat.TEXT

    # control
    killable: bool
    pgids: List[int] = Field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.status == CommandStatus.RUNNING

    @property
    def is_killable_state(self) -> bool:
        return self.status == CommandStatus.RUNNING

    def mark_success(self, exit_code: int, output: str):
        self.status = CommandStatus.SUCCESS
        self.exit_code = exit_code
        self.output = output

    def mark_failed(self, message: str, exit_code: Optional[int] = None, output: Optional[str] = None):
        self.status = CommandStatus.FAILED
        self.message = message
        # Keep exit_code/output when the failure came from a finished process
        # (e.g. a non-zero ansible exit), so the poll endpoint can show WHY it
        # failed instead of null/null. They stay None for failures with no
        # process result (capacity rejection, SSH error, etc.).
        if exit_code is not None:
            self.exit_code = exit_code
        if output is not None:
            self.output = output

    def mark_killing(self, message: str):
        self.status = CommandStatus.KILLING
        self.message = message

    def mark_killed(self, message: str):
        self.status = CommandStatus.KILLED
        self.message = message


# ── Whitelist Configuration ──────────────────────────────────────────────────

class CommandArgumentConfig(BaseModel):
    name: str
    type: str  # e.g., "int", "string"
    validation_regex: str = ""
    required: bool = True  # when False, the arg may be omitted from the request
                           # and any pipeline tokens referencing it are dropped.

    @model_validator(mode="after")
    def _validate_regex(self) -> "CommandArgumentConfig":
        if self.validation_regex:
            _compile_config_regex(
                self.validation_regex,
                f"argument '{self.name}' validation_regex",
            )
        return self

class PipelineStep(BaseModel):
    command: List[str]

class CommandWhitelistConfig(BaseModel):
    command_name: str
    description: str = ""
    disconnects_ssh: bool = False
    killable: bool = False
    logged: bool = False  # opt-in: tee output to a per-run file + expose viewer
    checks_script_version: bool = False  # opt-in: pre-check the target script version
    min_script_version: Optional[str] = None  # required when checks_script_version is True
    # Operator's declaration of the command's stdout contract. "json" PERMITS a
    # caller to ask for ?format=json on the poll endpoint; it never changes the
    # response on its own. Surfaced by the /command/info endpoints so callers can
    # discover which commands accept it.
    output_format: CommandOutputFormat = CommandOutputFormat.TEXT
    pipeline: List[PipelineStep]
    arguments: List[CommandArgumentConfig] = []

    @model_validator(mode="after")
    def _validate_version_config(self) -> "CommandWhitelistConfig":
        # A forgotten baseline must fail loudly at load, not silently no-op.
        if self.checks_script_version:
            from app.core.version import parse_semver
            if not self.min_script_version:
                raise ValueError(
                    f"command '{self.command_name}': checks_script_version=true "
                    "requires min_script_version"
                )
            parse_semver(self.min_script_version)  # raises ValueError if malformed
        return self

class UserCommandWhitelist(BaseModel):
    name: str = "admin"
    allow_hosts: List[str] = [".*"]
    deny_hosts: List[str] = []
    allow_commands: List[CommandWhitelistConfig]

    @model_validator(mode="after")
    def _validate_host_patterns(self) -> "UserCommandWhitelist":
        for field_name in ("allow_hosts", "deny_hosts"):
            for pattern in getattr(self, field_name):
                _compile_config_regex(pattern, field_name)
        return self


# ── SSH Configuration ────────────────────────────────────────────────────────

class SSHConnectionConfig(BaseModel):
    auth_method: str
    key_base64: str
    cert_base64: Optional[str] = None


# ── Request / Response ───────────────────────────────────────────────────────

class CommandOption(BaseModel):
    timeout_seconds: int = 30
    bastion_type: Optional[str] = None  # None → fall back to settings.BASTION_DEFAULT_TYPE
    ip_label: Optional[str] = None  # None → use settings.INVENTORY_IP_LABEL

class CommandExecutionRequest(BaseModel):
    command_name: str
    host: str
    host_type: HostType = HostType.IP
    port: int = 22
    username: str
    ssh_config: str = "default"
    option: Optional[CommandOption] = Field(default_factory=CommandOption)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    # Optional caller-supplied floor; may only RAISE the whitelist minimum.
    min_script_version: Optional[str] = None

    @model_validator(mode="after")
    def _validate_min_script_version(self) -> "CommandExecutionRequest":
        if self.min_script_version is not None:
            from app.core.version import parse_semver
            parse_semver(self.min_script_version)  # raises ValueError if malformed
        return self

class CommandExecutionResponse(BaseModel):
    command_id: Optional[str] = None
    status: str
    message: str = ""
    exit_status: Optional[int] = None
    output: Optional[str] = None
    exec_command: Optional[str] = None
    # Populated only by GET /command/execution/{id}; surfaced from CommandState.
    host_type: Optional[HostType] = None
    resolved_ip: Optional[str] = None
    pgids: List[int] = Field(default_factory=list)

    # ── Parsed output (populated only by GET /execution/{id}?format=json) ──
    # `output` above always keeps its raw string value; these are purely
    # additive so a caller that never passes ?format=json sees an unchanged
    # response. Typed `Any` because the source script may legitimately emit an
    # object, an array, or a scalar — pretending otherwise would reject valid
    # payloads.
    output_json: Optional[Any] = Field(
        default=None,
        description=(
            "The command's stdout parsed as JSON. Populated only when "
            "?format=json is requested AND the command's whitelist entry "
            "declares output_format: \"json\" AND parsing succeeded. Null "
            "otherwise — see output_json_error for why."
        ),
    )
    output_json_error: Optional[OutputJsonError] = Field(
        default=None,
        description=(
            "Why output_json is null despite ?format=json. "
            "`parse_failed`: the command declared output_format \"json\" but "
            "its stdout was not valid JSON — the remote script broke its "
            "contract (logged server-side at ERROR). "
            "`output_unavailable`: the command succeeded, but its stdout was "
            "lost during cross-pod orphan-run recovery, which reconstructs the "
            "exit code from the control_node marker without the output. "
            "`not_applicable`: the command is not in a success state, so there "
            "was never stdout to parse."
        ),
    )

    @classmethod
    def failed(cls, message: str, exit_status: Optional[int] = None, output: Optional[str] = None, command_id: Optional[str] = None) -> "CommandExecutionResponse":
        return cls(status=CommandStatus.FAILED.value, message=message, exit_status=exit_status, output=output, command_id=command_id)

    @classmethod
    def success(cls, command_id: str, exit_status: int, output: str) -> "CommandExecutionResponse":
        return cls(status=CommandStatus.SUCCESS.value, command_id=command_id, exit_status=exit_status, output=output)


class CommandLogLine(BaseModel):
    num: int
    content_html: str


class CommandTraceResponse(BaseModel):
    """Incremental slice of processed command-log lines for the UI.

    Mirrors the deploy FormattedLogResponse but keyed by command_id and
    carrying the command's lifecycle status.
    """
    command_id: str
    status: str
    next_byte_offset: int
    next_line_num: int
    lines: List[CommandLogLine]
    total_size: int = 0
    size_warning: bool = False
    too_large: bool = False
    # True when the command was not run with ``logged: true``, so no run log
    # exists on the control_node and the viewer has nothing to stream. The UI
    # shows an explanatory notice instead of an empty, forever-polling page.
    not_logged: bool = False
    # Where the full log physically lives on the control_node. Populated only on
    # the `too_large` bail-out so the user can read it directly (ssh + tail);
    # left None on normal slices to keep the response lean.
    log_host: Optional[str] = None
    log_port: Optional[int] = None
    log_user: Optional[str] = None
    log_file_path: Optional[str] = None


class RunningCommandsResponse(BaseModel):
    count: int
    commands: List[CommandState]


# ── Runtime Dataclasses ──────────────────────────────────────────────────────

@dataclass
class RunningCommandEntry:
    host_ip: str
    killable: bool
    conn: Optional[asyncssh.SSHClientConnection] = None
    task: Optional[asyncio.Task] = None
    processes: List[asyncssh.SSHClientProcess] = field(default_factory=list)
    pgids: List[int] = field(default_factory=list)

@dataclass
class ExecutionContext:
    username: str
    request_id: str
    command_name: str
    raw_request: CommandExecutionRequest
    cmd_config: CommandWhitelistConfig
    ssh_config: SSHConnectionConfig
    resolved_host: "ResolvedHost"  # forward-ref to avoid circular import
    conn: Optional[asyncssh.SSHClientConnection] = None
    pipeline_cmds: List[List[str]] = field(default_factory=list)
    run_id: Optional[str] = None
    run_log_path: Optional[str] = None
