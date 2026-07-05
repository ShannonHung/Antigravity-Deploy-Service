# Script Contract & Version Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a general, per-command script version-check capability to deploy-service and document a written script contract, with `run-ansible.sh` as the first conforming script / reference implementation.

**Architecture:** A pure semver util (`app/core/version.py`) is shared in spirit by two implementations — Python (caller-side pre-check) and bash (`run-ansible.sh` self-guard). The whitelist config gains a `checks_script_version` opt-in flag plus a `min_script_version` baseline, validated at load. Before running a version-checked command's pipeline, `CommandExecutor` SSHes `--version` on the target over the already-open connection, parses the semver, and raises a new `ScriptVersionException` (412) if the actual version is below `max(whitelist_default, api_value)`. The live `/view` + `logged` output policy already exists and is only documented.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, asyncssh, pytest (`asyncio_mode=auto`), bash, uv.

## Global Constraints

- Strict semver `X.Y.Z` only (three numeric segments); per-segment **numeric** comparison so `1.10.0 > 1.9.0`. No pre-release / build metadata. Malformed → error, never silently accepted.
- Version requirement lives **per-command in the whitelist**; there is **no global env default and no fallback**.
- Effective minimum = `max(whitelist_default, api_value)` — the API may only **raise** the bar, never lower the whitelist floor.
- Detection is via the explicit `checks_script_version: bool` flag, never filename heuristics.
- Version query is convention-based: always `--version`, script prints `<name> X.Y.Z`.
- Anti-injection guarantee is untouched: discrete args, no `eval`, `shlex.join` / `shlex.quote` on both sides.
- All commands run from `deploy-service/`; tests run with `APP_ENV=test uv run pytest`.
- `run-ansible.sh` dedicated exit codes: `2` = usage error (existing), `4` = version guard failure (new).
- All new/changed exceptions extend `BaseAppException` so the global handler renders them.

---

### Task 1: Semver comparison utility (`app/core/version.py`)

**Files:**
- Create: `app/core/version.py`
- Test: `tests/unit/test_version_util.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_semver(s: str) -> tuple[int, int, int]` — raises `ValueError` on malformed input.
  - `version_ge(a: str, b: str) -> bool` — `True` iff `a >= b` by numeric per-segment comparison. Raises `ValueError` if either is malformed.
  - `version_max(a: str, b: str) -> str` — returns whichever of `a`/`b` is greater (the string). Raises `ValueError` if either is malformed.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_version_util.py
import pytest

from app.core.version import parse_semver, version_ge, version_max


def test_parse_semver_valid():
    assert parse_semver("1.10.0") == (1, 10, 0)


@pytest.mark.parametrize("bad", ["1.2", "v1.0.0", "1.2.3.4", "1.2.x", "", "1.02.0"])
def test_parse_semver_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_semver(bad)


def test_version_ge_numeric_not_lexicographic():
    assert version_ge("1.10.0", "1.9.0") is True   # numeric: 10 > 9
    assert version_ge("1.9.0", "1.10.0") is False


def test_version_ge_equal():
    assert version_ge("1.2.3", "1.2.3") is True


def test_version_ge_strict_less():
    assert version_ge("1.0.0", "1.2.0") is False


def test_version_max_returns_greater_string():
    assert version_max("1.2.0", "1.5.0") == "1.5.0"
    assert version_max("1.5.0", "1.2.0") == "1.5.0"
    assert version_max("1.2.0", "1.2.0") == "1.2.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_version_util.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.version'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/core/version.py
"""Strict semver X.Y.Z parsing and comparison (numeric, per-segment).

The Python twin of the bash `version_ge` in run-ansible.sh — same semantics,
two implementations. No pre-release / build metadata; malformed input raises.
"""

import re

_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse_semver(s: str) -> tuple[int, int, int]:
    """Parse a strict `X.Y.Z` string into an (int, int, int) tuple.

    Raises ValueError for anything that is not exactly three numeric segments
    with no leading zeros (e.g. "1.2", "v1.0.0", "1.02.0").
    """
    m = _SEMVER_RE.match(s or "")
    if not m:
        raise ValueError(f"Invalid semver (expected X.Y.Z): {s!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def version_ge(a: str, b: str) -> bool:
    """True iff version `a` >= version `b` by numeric per-segment comparison."""
    return parse_semver(a) >= parse_semver(b)


def version_max(a: str, b: str) -> str:
    """Return whichever of `a` / `b` is the greater version (as a string)."""
    return a if parse_semver(a) >= parse_semver(b) else b
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_version_util.py -v`
Expected: PASS (all cases)

- [ ] **Step 5: Commit**

```bash
git add app/core/version.py tests/unit/test_version_util.py
git commit -m "feat: add strict semver comparison util (app/core/version.py)"
```

---

### Task 2: `ScriptVersionException` (412)

**Files:**
- Modify: `app/core/exceptions.py` (add class after `CommandExecutionException`, around line 175)
- Test: `tests/unit/test_script_version_exception.py`

**Interfaces:**
- Consumes: `BaseAppException` from `app/core/exceptions.py`.
- Produces: `ScriptVersionException` with `http_status = 412`, `error_code = "SCRIPT_VERSION_MISMATCH"`, `log_level = logging.WARNING`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_script_version_exception.py
import logging

from app.core.exceptions import BaseAppException, ScriptVersionException


def test_script_version_exception_shape():
    exc = ScriptVersionException("too old", detail={"actual": "1.0.0"})
    assert isinstance(exc, BaseAppException)
    assert exc.http_status == 412
    assert exc.error_code == "SCRIPT_VERSION_MISMATCH"
    assert exc.log_level == logging.WARNING
    assert exc.message == "too old"
    assert exc.detail == {"actual": "1.0.0"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_script_version_exception.py -v`
Expected: FAIL with `ImportError: cannot import name 'ScriptVersionException'`

- [ ] **Step 3: Write minimal implementation**

Add to `app/core/exceptions.py` immediately after the `CommandExecutionException` class:

```python
class ScriptVersionException(BaseAppException):
    """Raised when a target script's version is below the required minimum.

    Surfaced as 412 Precondition Failed: the request is well-formed but the
    control_node script does not meet the version precondition, so the pipeline
    is not run.
    """

    http_status = 412
    error_code = "SCRIPT_VERSION_MISMATCH"
    log_level = logging.WARNING
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_script_version_exception.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/exceptions.py tests/unit/test_script_version_exception.py
git commit -m "feat: add ScriptVersionException (412)"
```

---

### Task 3: Whitelist schema fields + load-time validation

**Files:**
- Modify: `app/domain/command.py` (`CommandWhitelistConfig`, lines 92-99; add `model_validator` import from pydantic on line 6)
- Modify: `app/domain/command.py` (`CommandExecutionRequest`, lines 123-131 — add optional `min_script_version`)
- Test: `tests/unit/test_command_version_config.py`

**Interfaces:**
- Consumes: `parse_semver` from `app/core/version.py` (Task 1).
- Produces:
  - `CommandWhitelistConfig.checks_script_version: bool` (default `False`).
  - `CommandWhitelistConfig.min_script_version: Optional[str]` (default `None`).
  - Model validator: if `checks_script_version` is `True`, `min_script_version` MUST be present and valid semver, else `ValueError` at construction.
  - `CommandExecutionRequest.min_script_version: Optional[str]` (default `None`); validated as semver when present.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_command_version_config.py
import pytest
from pydantic import ValidationError

from app.domain.command import CommandWhitelistConfig, CommandExecutionRequest


def _cfg(**kw):
    base = dict(command_name="run_ansible", pipeline=[{"command": ["run-ansible.sh"]}])
    base.update(kw)
    return CommandWhitelistConfig(**base)


def test_defaults_off():
    cfg = _cfg()
    assert cfg.checks_script_version is False
    assert cfg.min_script_version is None


def test_checks_true_with_valid_min_ok():
    cfg = _cfg(checks_script_version=True, min_script_version="1.2.0")
    assert cfg.checks_script_version is True
    assert cfg.min_script_version == "1.2.0"


def test_checks_true_without_min_rejected():
    with pytest.raises(ValidationError):
        _cfg(checks_script_version=True)


def test_checks_true_with_malformed_min_rejected():
    with pytest.raises(ValidationError):
        _cfg(checks_script_version=True, min_script_version="1.2")


def test_request_min_script_version_optional_and_validated():
    assert CommandExecutionRequest(
        command_name="run_ansible", host="h", username="u"
    ).min_script_version is None
    assert CommandExecutionRequest(
        command_name="run_ansible", host="h", username="u",
        min_script_version="1.5.0",
    ).min_script_version == "1.5.0"
    with pytest.raises(ValidationError):
        CommandExecutionRequest(
            command_name="run_ansible", host="h", username="u",
            min_script_version="bad",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_version_config.py -v`
Expected: FAIL (`checks_script_version` / `min_script_version` not attributes; no validation)

- [ ] **Step 3: Write minimal implementation**

In `app/domain/command.py`, update the pydantic import on line 6:

```python
from pydantic import BaseModel, Field, model_validator
```

Replace `CommandWhitelistConfig` (lines 92-99) with:

```python
class CommandWhitelistConfig(BaseModel):
    command_name: str
    description: str = ""
    disconnects_ssh: bool = False
    killable: bool = False
    logged: bool = False  # opt-in: tee output to a per-run file + expose viewer
    checks_script_version: bool = False  # opt-in: pre-check the target script version
    min_script_version: Optional[str] = None  # required when checks_script_version is True
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
```

Add a `min_script_version` field + validator to `CommandExecutionRequest` (lines 123-131):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_version_config.py -v`
Expected: PASS

Then run the existing domain/model tests to confirm no regression:

Run: `APP_ENV=test uv run pytest tests/unit/test_command_domain.py tests/unit/test_command_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/domain/command.py tests/unit/test_command_version_config.py
git commit -m "feat: whitelist version fields + load-time validation; request min_script_version"
```

---

### Task 4: Version pre-check in CommandExecutor

**Files:**
- Modify: `app/services/command_executor.py` (add `_precheck_script_version`; call it in `execute_command` between `_connect` and dispatch, around lines 662-668)
- Test: `tests/unit/test_command_version_precheck.py`

**Interfaces:**
- Consumes:
  - `version_ge`, `version_max` from `app/core/version.py` (Task 1).
  - `ScriptVersionException` from `app/core/exceptions.py` (Task 2).
  - `CommandWhitelistConfig.checks_script_version` / `.min_script_version`, `CommandExecutionRequest.min_script_version` (Task 3).
  - `ExecutionContext` (`cmd_config`, `raw_request`, `conn`, `pipeline_cmds`).
- Produces:
  - `async def _precheck_script_version(self, context: ExecutionContext) -> None` — no-op unless `context.cmd_config.checks_script_version`; otherwise SSH-runs `<script> --version` over `context.conn`, parses the trailing `X.Y.Z`, and raises `ScriptVersionException` (412) if `actual < effective_min` or if the version can't be fetched/parsed.
  - `@staticmethod _parse_version_output(text: str) -> str` — extracts the last whitespace-delimited `X.Y.Z` token from `--version` output; raises `ValueError` if none.

**Key facts for the implementer:**
- The script executable is the first token of the first pipeline step: `context.pipeline_cmds[0][0]`.
- `context.conn` is an open `asyncssh.SSHClientConnection`; run a command with `await context.conn.run(cmd_str, check=False)` → result has `.exit_status`, `.stdout`, `.stderr`.
- Preserve anti-injection: build the command with `shlex.join([script, "--version"])`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_command_version_precheck.py
import pytest

from app.core.exceptions import ScriptVersionException
from app.services.command_executor import CommandExecutor


class _FakeResult:
    def __init__(self, exit_status, stdout="", stderr=""):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


class _FakeConn:
    def __init__(self, result):
        self._result = result
        self.ran = []

    async def run(self, cmd, check=False):
        self.ran.append(cmd)
        return self._result


def _ctx(checks, whitelist_min, api_min, conn, script="run-ansible.sh"):
    from types import SimpleNamespace
    cmd_config = SimpleNamespace(
        checks_script_version=checks, min_script_version=whitelist_min,
    )
    raw_request = SimpleNamespace(min_script_version=api_min)
    return SimpleNamespace(
        cmd_config=cmd_config, raw_request=raw_request, conn=conn,
        pipeline_cmds=[[script, "--playbook", "ping.yml"]],
    )


def test_parse_version_output_extracts_trailing_semver():
    assert CommandExecutor._parse_version_output("run-ansible.sh 1.4.0") == "1.4.0"
    assert CommandExecutor._parse_version_output("1.4.0\n") == "1.4.0"


def test_parse_version_output_rejects_missing():
    with pytest.raises(ValueError):
        CommandExecutor._parse_version_output("no version here")


async def test_precheck_noop_when_flag_off():
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 1.0.0"))
    ctx = _ctx(checks=False, whitelist_min=None, api_min=None, conn=conn)
    await CommandExecutor._precheck_script_version(None, ctx)
    assert conn.ran == []  # never asked for --version


async def test_precheck_passes_when_actual_ge_min():
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 1.2.0"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min=None, conn=conn)
    await CommandExecutor._precheck_script_version(None, ctx)  # no raise


async def test_precheck_raises_when_actual_below_min():
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 1.0.0"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min=None, conn=conn)
    with pytest.raises(ScriptVersionException):
        await CommandExecutor._precheck_script_version(None, ctx)


async def test_precheck_api_raises_bar_but_cannot_lower():
    # whitelist 1.2.0, API asks 1.5.0 -> effective 1.5.0 -> 1.3.0 fails
    conn = _FakeConn(_FakeResult(0, "run-ansible.sh 1.3.0"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min="1.5.0", conn=conn)
    with pytest.raises(ScriptVersionException):
        await CommandExecutor._precheck_script_version(None, ctx)

    # API asks 1.0.0 (lower) -> effective stays 1.2.0 -> 1.2.0 passes
    conn2 = _FakeConn(_FakeResult(0, "run-ansible.sh 1.2.0"))
    ctx2 = _ctx(checks=True, whitelist_min="1.2.0", api_min="1.0.0", conn=conn2)
    await CommandExecutor._precheck_script_version(None, ctx2)  # no raise


async def test_precheck_raises_when_version_call_fails():
    conn = _FakeConn(_FakeResult(127, "", "not found"))
    ctx = _ctx(checks=True, whitelist_min="1.2.0", api_min=None, conn=conn)
    with pytest.raises(ScriptVersionException):
        await CommandExecutor._precheck_script_version(None, ctx)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_version_precheck.py -v`
Expected: FAIL with `AttributeError: ... '_precheck_script_version'` / `'_parse_version_output'`

- [ ] **Step 3: Write minimal implementation**

Add these two methods to the `CommandExecutor` class in `app/services/command_executor.py` (place them just before `execute_command`, after `_compute_log_path`). Ensure `import shlex` and `from app.core.version import version_ge, version_max` and `from app.core.exceptions import ScriptVersionException` are present at the top of the file (add the two new imports if missing).

```python
    @staticmethod
    def _parse_version_output(text: str) -> str:
        """Extract the last whitespace-delimited X.Y.Z token from --version output.

        Tolerates a leading script name (e.g. "run-ansible.sh 1.4.0").
        Raises ValueError if no semver-shaped token is present.
        """
        import re
        tokens = re.findall(r"\b\d+\.\d+\.\d+\b", text or "")
        if not tokens:
            raise ValueError(f"no version found in output: {text!r}")
        return tokens[-1]

    async def _precheck_script_version(self, context: ExecutionContext) -> None:
        """Reject the request if the target script is older than required.

        No-op unless the command opts in via ``checks_script_version``. Runs
        ``<script> --version`` over the already-open SSH connection, parses the
        semver, and compares against ``max(whitelist_min, api_min)``. Any failure
        to fetch/parse the version is treated as a rejection (fail closed).
        """
        cfg = context.cmd_config
        if not cfg.checks_script_version:
            return

        effective_min = cfg.min_script_version
        api_min = context.raw_request.min_script_version
        if api_min:
            effective_min = version_max(effective_min, api_min)

        script = context.pipeline_cmds[0][0]
        cmd_str = shlex.join([script, "--version"])
        result = await context.conn.run(cmd_str, check=False)

        if result.exit_status != 0:
            raise ScriptVersionException(
                f"Could not read version of '{script}' on the target "
                f"(exit {result.exit_status}).",
                detail={"script": script, "required": effective_min},
            )

        raw = (str(result.stdout) if result.stdout else "") + \
              ("\n" + str(result.stderr) if result.stderr else "")
        try:
            actual = self._parse_version_output(raw)
        except ValueError as exc:
            raise ScriptVersionException(
                f"Could not parse version of '{script}' on the target.",
                detail={"script": script, "required": effective_min},
            ) from exc

        try:
            ok = version_ge(actual, effective_min)
        except ValueError as exc:
            raise ScriptVersionException(
                f"Invalid version data for '{script}'.",
                detail={"script": script, "actual": actual, "required": effective_min},
            ) from exc

        if not ok:
            raise ScriptVersionException(
                f"{script} on the target is version {actual}, "
                f"below the required {effective_min}.",
                detail={"script": script, "actual": actual, "required": effective_min},
            )
```

Wire it into `execute_command` — insert the call right after `context.conn = conn` and before the `disconnects_ssh` branch (currently lines 663-665):

```python
        conn = await self._connect(context, req)
        context.conn = conn

        # Version gate: reject before running anything if the target script is
        # too old (fast failure, no wasted work). Runs over the open conn.
        await self._precheck_script_version(context)

        if context.cmd_config.disconnects_ssh:
            return await self._handle_fire_and_forget(context)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_version_precheck.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/command_executor.py tests/unit/test_command_version_precheck.py
git commit -m "feat: version pre-check before running version-checked commands"
```

---

### Task 5: `run-ansible.sh` — SCRIPT_VERSION, --version, --min-version

**Files:**
- Modify: `ansible/run-ansible.sh`
- Test: `tests/integration/test_run_ansible_script.py` (append cases)

**Interfaces:**
- Consumes: nothing (self-contained bash).
- Produces: `--version` prints `run-ansible.sh <SCRIPT_VERSION>` and exits 0; `--min-version X.Y.Z` self-guards and exits 4 when unsatisfied; a `version_ge` bash function.

**Key facts:**
- `SCRIPT_VERSION` starts at `1.0.0` (matches spec).
- Existing exit codes: `2` = usage. Use `4` for version-guard failure.
- `--version` and `--min-version` must be handled in `parse_args` (the loop at lines 88-146) and evaluated before any clone/docker work.
- `print_summary` (lines 290-305) prints the version on its first content line.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_run_ansible_script.py`:

```python
def test_version_flag_prints_and_exits_zero(tmp_path):
    res = subprocess.run(
        ["bash", str(SCRIPT), "--version"],
        capture_output=True, text=True, env={**os.environ},
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().startswith("run-ansible.sh ")
    # trailing token is strict semver
    last = res.stdout.strip().split()[-1]
    parts = last.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_min_version_satisfied_proceeds(tmp_path):
    # min below SCRIPT_VERSION -> passes the guard, continues to DRYRUN exit 0
    res = _run(tmp_path, "--run-id", "ok", "--min-version", "0.0.1")
    assert res.returncode == 0, res.stderr


def test_min_version_too_high_exits_4(tmp_path):
    res = _run(tmp_path, "--run-id", "ok", "--min-version", "99.0.0")
    assert res.returncode == 4
    assert "version" in (res.stderr + res.stdout).lower()


def test_min_version_malformed_rejected(tmp_path):
    res = _run(tmp_path, "--run-id", "ok", "--min-version", "1.2")
    assert res.returncode != 0
    assert "version" in (res.stderr + res.stdout).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v -k "version"`
Expected: FAIL (`--version` unknown arg → exit 2; `--min-version` unknown)

- [ ] **Step 3: Write minimal implementation**

In `ansible/run-ansible.sh`, add the constant near the other fixed config (after line 21):

```bash
SCRIPT_VERSION="1.0.0"
```

Add a `MIN_VERSION=""` default alongside the other defaults (after line 39):

```bash
MIN_VERSION=""             # --min-version <X.Y.Z>: self-guard minimum
```

Add a semver guard helper (place it above `parse_args`, after `usage`):

```bash
# Strict semver X.Y.Z numeric per-segment comparison: version_ge A B -> A >= B.
version_ge() {
  local a="$1" b="$2"
  if [[ ! "$a" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || ! "$b" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must be X.Y.Z (got '$a' vs '$b')." >&2
    exit 4
  fi
  local IFS=.
  local -a A=($a) B=($b)
  for i in 0 1 2; do
    if (( A[i] > B[i] )); then return 0; fi
    if (( A[i] < B[i] )); then return 1; fi
  done
  return 0
}
```

In `parse_args`, add two cases to the `case "$1"` block (near line 108, beside `-h|--help`):

```bash
      --version)             echo "run-ansible.sh $SCRIPT_VERSION"; exit 0 ;;
      --min-version)         MIN_VERSION="$2"; shift 2 ;;
```

At the end of `parse_args` (after the existing validation, before its closing `}` at line 146), enforce the guard:

```bash
  if [[ -n "$MIN_VERSION" ]]; then
    if ! version_ge "$SCRIPT_VERSION" "$MIN_VERSION"; then
      echo "Error: run-ansible.sh version $SCRIPT_VERSION is below the required minimum $MIN_VERSION." >&2
      exit 4
    fi
  fi
```

In `print_summary`, add the version as the first content line (inside the heredoc, right after the top border on line 292):

```bash
  Script version : $SCRIPT_VERSION
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: PASS (new version cases + existing cases still green)

- [ ] **Step 5: Commit**

```bash
git add ansible/run-ansible.sh tests/integration/test_run_ansible_script.py
git commit -m "feat(run-ansible): SCRIPT_VERSION, --version, --min-version self-guard"
```

---

### Task 6: `COMMAND_LOG_DIR` default + whitelist `--log-dir` migration

**Files:**
- Modify: `app/core/config.py:70`
- Modify: `data/allow-commands-admin.json` (2 occurrences of `/var/log/ansible-runs`)
- Modify: `data/allow-commands-cluster_proxy.json` (2 occurrences)
- Test: `tests/unit/test_command_log_settings.py` (add assertion) OR new small test

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.COMMAND_LOG_DIR == "/var/log/deploy-service"` by default; whitelist pipelines pass `--log-dir /var/log/deploy-service`.

**Key facts:** `.env*` files may override `COMMAND_LOG_DIR`; check none pin the old value (`grep COMMAND_LOG_DIR .env*`). If any do, update them to the new path too.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_command_log_settings.py`:

```python
def test_command_log_dir_default_is_deploy_service():
    from app.core.config import get_settings
    get_settings.cache_clear()
    assert get_settings().COMMAND_LOG_DIR == "/var/log/deploy-service"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_log_settings.py -v -k deploy_service`
Expected: FAIL (`'/var/log/ansible-runs' != '/var/log/deploy-service'`)

- [ ] **Step 3: Write minimal implementation**

In `app/core/config.py`, line 70:

```python
    COMMAND_LOG_DIR: str = "/var/log/deploy-service"
```

Update the four whitelist occurrences. In `data/allow-commands-admin.json` and `data/allow-commands-cluster_proxy.json`, change each pipeline arg pair:

```json
            "--log-dir",
            "/var/log/deploy-service",
```

Confirm none remain:

```bash
grep -rn "ansible-runs" data/allow-commands-*.json && echo "STILL PRESENT" || echo "clean"
```

Also check `.env*`:

```bash
grep -n "COMMAND_LOG_DIR" .env .env.dev .env.prod .env.test 2>/dev/null || echo "no env override"
```

If any `.env*` pins `/var/log/ansible-runs`, update it to `/var/log/deploy-service`.

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_log_settings.py -v`
Expected: PASS

Also confirm whitelist JSON still parses (the load-time validator from Task 3 runs here):

Run: `APP_ENV=test uv run pytest tests/unit/test_command_version_config.py tests/integration/test_command_trace_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py data/allow-commands-admin.json data/allow-commands-cluster_proxy.json tests/unit/test_command_log_settings.py
git commit -m "feat: default COMMAND_LOG_DIR to /var/log/deploy-service; migrate whitelist --log-dir"
```

---

### Task 7: Script contract document (`docs/arch/script-contract.md`)

**Files:**
- Create: `docs/arch/script-contract.md`

**Interfaces:**
- Consumes: the behaviours implemented in Tasks 1-6 (for accurate documentation).
- Produces: the written contract. No code.

- [ ] **Step 1: Write the document**

Create `docs/arch/script-contract.md` with these sections (write real content, not placeholders):

1. **Purpose** — Any `.sh` run through deploy-service's SSH command API can opt into version checking and the live `/view` log viewer by following this contract. `run-ansible.sh` is the reference implementation.

2. **Version contract (MUST, to be version-checked):**
   - `--version` → prints `<name> X.Y.Z` to stdout, exits 0, no side effects.
   - `--min-version X.Y.Z` → self-guards; if `SCRIPT_VERSION < min`, prints the reason to stderr and exits `4`. Invalid version format also errors.
   - Version numbers are strict semver `X.Y.Z`, compared numerically per segment (`1.10.0 > 1.9.0`).

3. **Log/view contract (MUST, to support `/view`):**
   - Accept `--run-id <id>`.
   - `tee` execution output to `<log-dir>/<run_id>.log`.
   - On completion, write `<log-dir>/<run_id>.exit` containing a single integer exit code (deploy-service reads it to heal its state machine).
   - Append an `=== EXIT <code> ===` marker to the log tail.

4. **Whitelist configuration** — show a worked example entry:

   ```json
   {
     "command_name": "run_my_tool",
     "logged": true,
     "checks_script_version": true,
     "min_script_version": "1.2.0",
     "pipeline": [
       { "command": ["/opt/tools/my-tool.sh",
                      "--run-id", "{run_id}",
                      "--log-dir", "/var/log/deploy-service"] }
     ],
     "arguments": []
   }
   ```
   - `logged: true` enables the tee + `/view`.
   - `checks_script_version: true` + `min_script_version` enables version gating (both required together, validated at load).
   - `{run_id}` is the server-injected placeholder.
   - `--log-dir` should match `COMMAND_LOG_DIR` (default `/var/log/deploy-service`).

5. **Output policy that comes with `logged: true`** (already implemented in `_apply_output_policy`): on **success** the API response carries no output — go to `/view`; on **failure** the response includes the last `COMMAND_LOG_FAILURE_TAIL_LINES` lines (default **50**); the full log is always at `/view` regardless of outcome.

6. **API override** — a request may pass `min_script_version` to *raise* the required minimum; effective minimum = `max(whitelist_default, api_value)`. The API can only tighten, never lower the whitelist floor.

7. **Reference implementation** — link to `ansible/run-ansible.sh` and `docs/arch/ssh-command.md`.

- [ ] **Step 2: Commit**

```bash
git add docs/arch/script-contract.md
git commit -m "docs: add general script contract (docs/arch/script-contract.md)"
```

---

### Task 8: Full regression + verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite**

Run: `APP_ENV=test uv run pytest tests/ -v`
Expected: PASS (all green, including pre-existing tests). If any pre-existing test asserted `/var/log/ansible-runs`, update it to the new default as part of this task and re-run.

- [ ] **Step 2: Sanity-check the script end-to-end (no docker)**

Run: `DRYRUN=1 bash ansible/run-ansible.sh --version`
Expected: prints `run-ansible.sh 1.0.0`, exits 0.

Run: `DRYRUN=1 bash ansible/run-ansible.sh --playbook ping.yml --inventory taipei/multinode.ini --run-id ok --min-version 99.0.0`
Expected: exits 4 with a version error.

- [ ] **Step 3: Commit any fixups**

```bash
git add -A
git commit -m "test: green suite after script-contract & version-check"
```

---

## Self-Review

**Spec coverage:**
- Version check both sides → Task 4 (caller pre-check) + Task 5 (script `--min-version`). ✓
- Strict semver numeric → Task 1. ✓
- Whitelist per-command requirement, no env fallback → Task 3 (fields, no env added) + Task 6 (only log dir env changes). ✓
- Two-layer `max()` override, API can only raise → Task 3 (request field) + Task 4 (`version_max`). ✓
- Explicit `checks_script_version` flag (no heuristics) → Task 3 + Task 4. ✓
- Convention `--version` printing `<name> X.Y.Z` → Task 4 (parse) + Task 5 (print). ✓
- Load-time loud failure for `checks=true` without min → Task 3 validator. ✓
- `ScriptVersionException` 4xx (412) → Task 2. ✓
- `COMMAND_LOG_DIR` → `/var/log/deploy-service` + whitelist migration → Task 6. ✓
- `docs/arch/script-contract.md` incl. output policy → Task 7. ✓
- Output policy documented-only, no code change → Task 7 (no code task touches `_apply_output_policy`). ✓
- Anti-injection preserved → Task 4 (`shlex.join`) + Task 5 (no eval). ✓

**Placeholder scan:** No TBD/TODO; every code step shows concrete code. ✓

**Type consistency:** `parse_semver`/`version_ge`/`version_max` names match across Tasks 1, 3, 4. `ScriptVersionException` name/status (412) consistent Tasks 2 & 4. `checks_script_version`/`min_script_version` field names consistent Tasks 3, 4, 6, 7. Exit code `4` consistent Tasks 5 & 7. ✓
