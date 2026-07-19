# Logged Command Failure Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make pre-ansible stage failures (clone / secret / inventory) of `logged` SSH commands observable — capture them in the control_node log, write the `.exit` marker on any exit, and backfill the API `output` field with the log tail on both the fast-path and heal-path.

**Architecture:** Three coordinated changes. (1) `run-ansible.sh` tees the whole run (not just the docker step) to `{id}.log` and writes the `=== EXIT N ===` marker + `.exit` sidecar via the EXIT trap so any early exit (e.g. clone failure) is recorded. (2) A shared `SshSupport._read_log_tail` helper reads the last N lines of `{id}.log` over SSH. (3) The fast-path and heal-path both call it to fill the API `output` on a failed logged command whose SSH channel was empty (by design — logged commands redirect their output to `/dev/null`).

**Tech Stack:** Bash (run-ansible.sh), Python 3.11 + FastAPI + asyncssh, pytest (`asyncio_mode=auto`).

## Global Constraints

- Working directory for all commands: `deploy-service/`.
- Run tests with: `APP_ENV=test uv run pytest <path> -v`.
- The bash script tests are Python integration tests in `tests/integration/test_run_ansible_script.py`; they invoke the script via `subprocess` with fake `git`/`docker` on `PATH`.
- Anti-injection: any remote path passed to an SSH `conn.run(...)` MUST be `shlex.quote`-d. Never interpolate user/state values into a shell string unquoted.
- `_read_log_tail` MUST swallow SSH failures and return `None` — a control_node outage must never turn a poll into a 5xx.
- Tail size comes from `settings.COMMAND_LOG_FAILURE_TAIL_LINES` (currently `50`). Do not hardcode 50.
- `run-ansible.sh` has exactly ONE `trap ... EXIT` handler (`cleanup`). Bash replaces (does not stack) EXIT traps — extend `cleanup`, do NOT add a second `trap ... EXIT`.
- The script is version `2.0.0` (`SCRIPT_VERSION` at `ansible/run-ansible.sh:22`). This change alters observable log/marker behaviour on the early-exit path; bump `SCRIPT_VERSION` to `2.1.0` (minor: additive/behavioural, backward compatible with existing callers).

---

## File Structure

**Modified files:**
- `ansible/run-ansible.sh` — tee whole run; arm trap right after `resolve_log_file`; move `=== EXIT ===` + `.exit` writing into `cleanup`; bump version.
- `app/services/command_ssh.py` — add `_read_log_tail(state, n)` to `SshSupport`.
- `app/services/command_executor.py` — fast-path: backfill empty `output` from log tail on failed logged command.
- `app/services/command_state_helpers.py` — heal-path: pass log tail into `mark_failed`.

**Test files:**
- `tests/integration/test_run_ansible_script.py` — add: clone-failure writes marker + log; tee covers whole run; version bump reflected. (existing marker-on-success/failure tests must still pass)
- `tests/unit/test_command_ssh_log_tail.py` — NEW: `_read_log_tail` behaviour.
- `tests/unit/test_command_orphan_heal.py` — add: heal fills `output` from log tail.
- `tests/unit/test_command_executor_log_tail.py` — NEW: fast-path backfill behaviour.

---

## Task 1: `run-ansible.sh` — tee whole run, arm trap early, centralise marker writing

**Files:**
- Modify: `ansible/run-ansible.sh` (`SCRIPT_VERSION` line 22; `cleanup` ~323-325; `clone_inventory` trap-arm line 341; `run_normal` lines ~573-585; `main` lines 588-603)
- Test: `tests/integration/test_run_ansible_script.py`

**Interfaces:**
- Produces (for deploy-service side, unchanged contract): control_node file `{LOG_DIR}/{RUN_ID}.log` containing the full run output ending in `=== EXIT N ===`; sidecar `{LOG_DIR}/{RUN_ID}.exit` containing `N`. These already exist for the docker step; this task extends them to cover clone/pre-ansible failures.

**Context — current behaviour:**
- `main()` (lines 588-603) runs stages sequentially; only `run_normal` pipes `docker run ... 2>&1 | tee "$LOG_FILE"` and writes `=== EXIT ===`/`.exit` at lines 578-583.
- The single EXIT trap is `trap cleanup EXIT`, armed inside `clone_inventory` (line 341); `cleanup` (323-325) only `rm -rf "$CLONE_DIR"`. `run_debug` disarms it with `trap - EXIT` (line 508).
- If `clone_inventory` fails, no tee, no marker, no sidecar → invisible.

- [ ] **Step 1: Write the failing test — clone failure writes marker, sidecar, and log**

Add to `tests/integration/test_run_ansible_script.py`:

```python
def _run_with_failing_git(tmp_path, *extra):
    """Fake git that FAILS the clone (like a private repo with no token),
    printing a git-style error to stderr and exiting non-zero. Fake docker
    exists but must never be reached."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'echo "fatal: could not read Username for '
        "'https://gitlab.com': terminal prompts disabled\" >&2\n"
        "exit 128\n"
    )
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'touch "{tmp_path}/docker_was_called"\n'
        "exit 0\n"
    )
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "SKIP_SSH_KEY_CHECK": "1"}
    env.pop("INVENTORY_REPO", None)
    return subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path), *extra],
        capture_output=True, text=True, env=env,
    )


def test_clone_failure_writes_marker_sidecar_and_log(tmp_path):
    res = _run_with_failing_git(tmp_path, "--run-id", "clone-fail")
    # The script must EXIT with git's real code so the API sees failure.
    assert res.returncode == 128, res.stderr
    # docker must never have been reached.
    assert not (tmp_path / "docker_was_called").exists()
    # The clone error is now captured in the per-run log (was going to /dev/null).
    log = (tmp_path / "clone-fail.log").read_text()
    assert "could not read Username" in log
    assert log.rstrip().endswith("=== EXIT 128 ===")
    # And the sidecar marker is written so heal can recover cross-pod.
    assert (tmp_path / "clone-fail.exit").read_text().strip() == "128"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py::test_clone_failure_writes_marker_sidecar_and_log -v`
Expected: FAIL — `clone-fail.log` either does not exist or lacks the clone error / `=== EXIT 128 ===`, and no `.exit` sidecar is written.

- [ ] **Step 3: Extend `cleanup` to write the marker + sidecar**

Replace the `cleanup` function (`ansible/run-ansible.sh:323-325`) with:

```sh
# ── EXIT trap: clone cleanup + terminal marker/sidecar ───────────────────────
# Runs on ANY exit once armed (right after resolve_log_file). Records the run's
# real exit code to the per-run log (=== EXIT N ===) and, when a RUN_ID is set,
# to the <run_id>.exit sidecar — so a failure at ANY stage (clone, secret,
# inventory, ansible) is visible to /view and recoverable by deploy-service's
# heal path. Guarded by _MARKER_WRITTEN so it fires exactly once even though
# the trap re-runs on the final exit.
_MARKER_WRITTEN=0
cleanup() {
  local code="$?"
  rm -rf "$CLONE_DIR" || true
  if [[ "$_MARKER_WRITTEN" -eq 0 && -n "$LOG_FILE" ]]; then
    _MARKER_WRITTEN=1
    echo "=== EXIT $code ===" >> "$LOG_FILE" || true
    if [[ -n "$RUN_ID" ]]; then
      local exit_file="$LOG_DIR/$RUN_ID.exit"
      printf '%s\n' "$code" > "$exit_file.tmp" && mv -f "$exit_file.tmp" "$exit_file"
    fi
  fi
}
```

- [ ] **Step 4: Arm the trap right after `resolve_log_file`, and wrap the stages in a tee**

Replace the body of `main()` (`ansible/run-ansible.sh:588-603`) with:

```sh
run_stages() {
  clone_inventory
  build_cmd_args
  build_docker_base_args
  case "$MODE" in
    debug)   run_debug ;;
    dry-run) run_dry_run ;;
    *)       run_normal ;;
  esac
}

main() {
  parse_args "$@"
  load_secrets
  ensure_vault_client
  resolve_inventory_repo
  resolve_token
  resolve_log_file      # sets LOG_FILE + mkdir LOG_DIR; only now can we tee
  # DRYRUN exits inside resolve_log_file before reaching here.
  trap cleanup EXIT     # single EXIT trap for the whole run (clone cleanup + marker)
  # Tee the entire run (clone/secret/inventory/ansible) to the per-run log, and
  # re-exit run_stages' real code (PIPESTATUS[0], NOT tee's).
  run_stages 2>&1 | tee "$LOG_FILE"
  exit "${PIPESTATUS[0]}"
}
```

Then remove the now-duplicated `trap cleanup EXIT` line inside `clone_inventory` (line 341) — the trap is armed earlier in `main` now. Leave `run_debug`'s `trap - EXIT` (line 508) as-is: debug mode intentionally keeps the clone dir AND should not write a `.exit` sidecar (existing `test_debug_*` tests assert `not (tmp_path / "...exit").exists()`), and disarming the trap preserves both.

- [ ] **Step 5: Drop the duplicate tee + marker writing from `run_normal`**

In `run_normal` (`ansible/run-ansible.sh:571-585`), the outer tee now owns log capture and `cleanup` owns the marker. Replace lines 571-585:

```sh
  # set -e would abort before we record a non-zero exit, so capture via
  # ${PIPESTATUS[0]} (the docker side of the pipe, NOT tee's) and re-exit it.
  # Output is captured by the outer tee in main(); do not tee again here.
  set +e
  docker run --rm "${DOCKER_BASE_ARGS[@]}" "$IMAGE" "${CMD_ARGS[@]}" 2>&1
  RUN_EXIT="${PIPESTATUS[0]}"
  set -e

  # The EXIT-marker log line and <run_id>.exit sidecar are now written by the
  # cleanup EXIT trap using this exit code.
  exit "$RUN_EXIT"
```

- [ ] **Step 6: Bump `SCRIPT_VERSION`**

Change `ansible/run-ansible.sh:22` from `SCRIPT_VERSION="2.0.0"` to:

```sh
SCRIPT_VERSION="2.1.0"
```

- [ ] **Step 7: Run the new test + the full script suite to verify no regression**

Run: `APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py -v`
Expected: PASS — the new `test_clone_failure_writes_marker_sidecar_and_log` passes, AND the existing `test_marker_written_on_success`, `test_marker_written_on_failure_preserves_exit_code`, `test_no_sidecar_without_run_id`, all `test_debug_*`, `test_dry_run_*` still pass (marker now comes from the trap; success/failure codes unchanged; debug/dry-run still write no sidecar).

- [ ] **Step 8: Verify the success log still ends with the marker exactly once**

The success-path test (`test_marker_written_on_success`) asserts `log.rstrip().endswith("=== EXIT 0 ===")` and the sidecar is `"0"`. Confirm it passes — this proves the trap writes the marker exactly once (the `_MARKER_WRITTEN` guard) and that moving the write from `run_normal` to `cleanup` did not drop or double it.

Run: `APP_ENV=test uv run pytest tests/integration/test_run_ansible_script.py::test_marker_written_on_success tests/integration/test_run_ansible_script.py::test_no_sidecar_without_run_id -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add ansible/run-ansible.sh tests/integration/test_run_ansible_script.py
git commit -m "feat(run-ansible): capture pre-ansible failures in log + exit marker

Tee the whole run (not just the docker step) to {id}.log and write the
=== EXIT N === marker plus {id}.exit sidecar from the EXIT trap, so clone/
secret/inventory failures are visible in /view and recoverable by heal.
Bump SCRIPT_VERSION to 2.1.0.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01G8D5Eq7UYX6SGix9ZttWvG"
```

---

## Task 2: `SshSupport._read_log_tail` — read the last N lines of the run log over SSH

**Files:**
- Modify: `app/services/command_ssh.py` (add method to `SshSupport`, after `_connect_to_control_node`)
- Test: `tests/unit/test_command_ssh_log_tail.py` (NEW)

**Interfaces:**
- Consumes: `SshSupport._connect_to_control_node(state) -> asyncssh.SSHClientConnection` (existing); `CommandState.run_log_path: Optional[str]`.
- Produces: `async def _read_log_tail(self, state: CommandState, n: int) -> Optional[str]` — returns the last `n` lines of `state.run_log_path` on the control_node, or `None` if there's no log path, the file is absent/empty, or SSH fails.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_ssh_log_tail.py`:

```python
"""SshSupport._read_log_tail: fetch the tail of a run's control_node log.

Used to backfill the API `output` on a failed `logged` command, whose SSH
channel is empty by design (output redirected to /dev/null on the target).
"""
from unittest.mock import AsyncMock, MagicMock

from app.domain.command import CommandState, CommandStatus
from app.services.command_ssh import SshSupport
from app.core.exceptions import UpstreamUnavailableException


def _state(**over):
    base = dict(
        command_id="c1", status=CommandStatus.RUNNING, host="h",
        resolved_ip="1.2.3.4", port=2224, username="root",
        ssh_config="control_node", request_id="r1", exec_command="x",
        killable=True, run_log_path="/var/log/deploy-service/c1.log",
    )
    base.update(over)
    return CommandState(**base)


def _conn_returning(stdout, exit_status=0):
    """A fake asyncssh connection whose .run() returns a result with stdout."""
    result = MagicMock()
    result.stdout = stdout
    result.exit_status = exit_status
    conn = MagicMock()
    conn.run = AsyncMock(return_value=result)
    conn.close = MagicMock()
    return conn


async def test_read_log_tail_returns_tail_text(monkeypatch):
    ssh = SshSupport()
    conn = _conn_returning("line48\nline49\nline50\n")
    monkeypatch.setattr(ssh, "_connect_to_control_node", AsyncMock(return_value=conn))
    out = await ssh._read_log_tail(_state(), 50)
    assert out == "line48\nline49\nline50\n"
    # Path is shlex-quoted inside a `tail -n` command.
    cmd = conn.run.call_args.args[0]
    assert "tail -n 50" in cmd
    assert "/var/log/deploy-service/c1.log" in cmd
    conn.close.assert_called_once()


async def test_read_log_tail_none_when_file_absent(monkeypatch):
    ssh = SshSupport()
    conn = _conn_returning("", exit_status=1)  # tail: no such file
    monkeypatch.setattr(ssh, "_connect_to_control_node", AsyncMock(return_value=conn))
    assert await ssh._read_log_tail(_state(), 50) is None
    conn.close.assert_called_once()


async def test_read_log_tail_none_when_empty(monkeypatch):
    ssh = SshSupport()
    conn = _conn_returning("", exit_status=0)
    monkeypatch.setattr(ssh, "_connect_to_control_node", AsyncMock(return_value=conn))
    assert await ssh._read_log_tail(_state(), 50) is None


async def test_read_log_tail_none_when_no_log_path():
    ssh = SshSupport()
    # No SSH attempt should be made when there's no log path.
    assert await ssh._read_log_tail(_state(run_log_path=None), 50) is None


async def test_read_log_tail_swallows_ssh_failure(monkeypatch):
    ssh = SshSupport()
    monkeypatch.setattr(
        ssh, "_connect_to_control_node",
        AsyncMock(side_effect=UpstreamUnavailableException("down")),
    )
    assert await ssh._read_log_tail(_state(), 50) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_ssh_log_tail.py -v`
Expected: FAIL — `AttributeError: 'SshSupport' object has no attribute '_read_log_tail'`.

- [ ] **Step 3: Implement `_read_log_tail`**

Add `import shlex` to the top of `app/services/command_ssh.py` (alongside the existing `import os`), then add this method to `SshSupport`, immediately after `_connect_to_control_node`:

```python
    async def _read_log_tail(self, state: CommandState, n: int) -> Optional[str]:
        """Read the last ``n`` lines of a run's control_node log over SSH.

        Used to backfill the API ``output`` for a failed ``logged`` command,
        whose SSH channel carries no output (the run redirects stdout/stderr to
        ``/dev/null`` on the target so it survives the pod dying — the real
        output lives only in ``run_log_path`` on the control_node).

        Returns the tail text, or ``None`` when there is no log path, the file
        is absent/empty, or SSH fails. SSH failures are swallowed on purpose: a
        transient control_node outage must never turn a poll into a 5xx — the
        caller keeps its last-known ``output``.
        """
        if not state.run_log_path:
            return None
        try:
            conn = await self._connect_to_control_node(state)
        except BaseAppException as exc:
            logger.info(
                f"Log-tail read failed to connect for {state.command_id}: {exc}",
                extra={"command_id": state.command_id},
            )
            return None
        try:
            quoted = shlex.quote(state.run_log_path)
            res = await conn.run(f"tail -n {int(n)} {quoted}", check=False)
            if res.exit_status != 0:
                return None  # file absent / unreadable
            text = str(res.stdout) if res.stdout else ""
            return text or None
        finally:
            conn.close()
```

Add `Optional` to the imports from `typing` at the top of the file:

```python
from typing import Optional
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_ssh_log_tail.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/command_ssh.py tests/unit/test_command_ssh_log_tail.py
git commit -m "feat(ssh): add SshSupport._read_log_tail for run-log tail over SSH

Reads the last N lines of a run's control_node log; returns None when
there's no log path, the file is absent/empty, or SSH fails (never 5xx).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01G8D5Eq7UYX6SGix9ZttWvG"
```

---

## Task 3: Fast-path backfill — fill empty `output` from the log tail on a failed logged command

**Files:**
- Modify: `app/services/command_executor.py` (`_handle_async_execution._execution_task`, lines ~647-663)
- Test: `tests/unit/test_command_executor_log_tail.py` (NEW)

**Interfaces:**
- Consumes: `self._ssh._read_log_tail(state, n) -> Optional[str]` (Task 2); `context.cmd_config.logged: bool`; `settings.COMMAND_LOG_FAILURE_TAIL_LINES: int`; `self._apply_output_policy(logged, success, output) -> Optional[str]` (existing).
- Produces: on a failed logged command whose collected `output` is empty, `stored_output` becomes the log tail (so the persisted `CommandState.output` — and thus the API `output` — shows the real failure).

**Context — current code** (`app/services/command_executor.py:647-663`, inside `_execution_task`):

```python
            # 2. Collect Output
            returncode, output = await self._collect_output(final_process)
            ...
            success = returncode == 0
            stored_output = self._apply_output_policy(
                context.cmd_config.logged, success, output,
            )
```

`state` is not in scope inside `_execution_task`, but `_read_log_tail` needs a `CommandState`. The task already has `command_id` and all connection fields on `context.raw_request` / `context.resolved_host`. Rather than reconstruct a state, fetch the persisted one with `await self.repo.get(command_id)` (it was `save`-d earlier in `_handle_async_execution`, line 628) — it carries `run_log_path`, `resolved_ip`, `port`, `username`, `ssh_config`, everything `_read_log_tail` → `_connect_to_control_node` needs.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_command_executor_log_tail.py`:

```python
"""Fast-path output backfill: a failed `logged` command has an empty SSH
channel (output → /dev/null on the target). The executor must backfill the
persisted output from the control_node log tail so the API shows the failure.

These tests exercise the pure policy seam `_maybe_backfill_output`, which
decides whether to read the log tail, keeping the async task wiring untested
here (covered by integration).
"""
from unittest.mock import AsyncMock, MagicMock

from app.domain.command import CommandState, CommandStatus
from app.services.command_service import CommandService


def _state(**over):
    base = dict(
        command_id="c1", status=CommandStatus.RUNNING, host="h",
        resolved_ip="1.2.3.4", port=2224, username="root",
        ssh_config="control_node", request_id="r1", exec_command="x",
        killable=True, run_log_path="/var/log/deploy-service/c1.log",
    )
    base.update(over)
    return CommandState(**base)


def _executor():
    return CommandService(repo=None, inventory_repo=None)._executor


async def test_backfill_reads_tail_when_logged_failed_and_empty(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(
        ex._ssh, "_read_log_tail", AsyncMock(return_value="fatal: clone failed\n"))
    out = await ex._maybe_backfill_output(
        state=_state(), logged=True, success=False, output="")
    assert out == "fatal: clone failed\n"


async def test_backfill_skipped_when_output_present(monkeypatch):
    ex = _executor()
    reader = AsyncMock(return_value="SHOULD-NOT-BE-USED")
    monkeypatch.setattr(ex._ssh, "_read_log_tail", reader)
    out = await ex._maybe_backfill_output(
        state=_state(), logged=True, success=False, output="real channel output")
    assert out == "real channel output"
    reader.assert_not_awaited()


async def test_backfill_skipped_when_success(monkeypatch):
    ex = _executor()
    reader = AsyncMock(return_value="SHOULD-NOT-BE-USED")
    monkeypatch.setattr(ex._ssh, "_read_log_tail", reader)
    out = await ex._maybe_backfill_output(
        state=_state(), logged=True, success=True, output="")
    assert out == ""
    reader.assert_not_awaited()


async def test_backfill_skipped_when_not_logged(monkeypatch):
    ex = _executor()
    reader = AsyncMock(return_value="SHOULD-NOT-BE-USED")
    monkeypatch.setattr(ex._ssh, "_read_log_tail", reader)
    out = await ex._maybe_backfill_output(
        state=_state(), logged=False, success=False, output="")
    assert out == ""
    reader.assert_not_awaited()


async def test_backfill_keeps_empty_when_tail_none(monkeypatch):
    ex = _executor()
    monkeypatch.setattr(ex._ssh, "_read_log_tail", AsyncMock(return_value=None))
    out = await ex._maybe_backfill_output(
        state=_state(), logged=True, success=False, output="")
    assert out == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_executor_log_tail.py -v`
Expected: FAIL — `AttributeError: 'CommandExecutor' object has no attribute '_maybe_backfill_output'`.

- [ ] **Step 3: Implement `_maybe_backfill_output` and wire it into the task**

Add this method to `CommandExecutor` (in `app/services/command_executor.py`), next to `_apply_output_policy`:

```python
    async def _maybe_backfill_output(
        self, state: CommandState, logged: bool, success: bool, output: str,
    ) -> str:
        """For a failed ``logged`` command with an empty SSH channel, replace the
        empty ``output`` with the control_node log tail.

        Logged commands redirect their stdout/stderr to ``/dev/null`` on the
        target (so the run survives the pod dying), so ``_collect_output``
        returns "" — the real failure text lives only in the run log. We fetch
        its tail here so the persisted output (and the API) shows why it failed.
        Only triggers when logged AND failed AND the channel output is empty;
        every other case keeps the original ``output`` unchanged.
        """
        if not (logged and not success and not output):
            return output
        tail = await self._ssh._read_log_tail(
            state, settings.COMMAND_LOG_FAILURE_TAIL_LINES,
        )
        return tail or output
```

Then wire it into `_execution_task` (`app/services/command_executor.py:647-663`). Replace:

```python
            # 2. Collect Output
            returncode, output = await self._collect_output(final_process)

            logger.info(
                f"Command '{context.command_name}' finished. Exit Status: {returncode}",
                extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
            )

            success = returncode == 0
            stored_output = self._apply_output_policy(
                context.cmd_config.logged, success, output,
            )
```

with:

```python
            # 2. Collect Output
            returncode, output = await self._collect_output(final_process)

            logger.info(
                f"Command '{context.command_name}' finished. Exit Status: {returncode}",
                extra={"request_id": context.request_id, "username": context.username, "command_id": command_id, "host": context.raw_request.host, "port": context.raw_request.port}
            )

            success = returncode == 0
            # Logged commands sever their output to /dev/null on the target, so a
            # failure leaves `output` empty — backfill it from the control_node
            # log tail so the API surfaces the real error (e.g. a clone failure).
            if context.cmd_config.logged and not success and not output:
                backfill_state = await self.repo.get(command_id)
                output = await self._maybe_backfill_output(
                    backfill_state, logged=True, success=False, output=output,
                )
            stored_output = self._apply_output_policy(
                context.cmd_config.logged, success, output,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_executor_log_tail.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Run the executor's existing tests to confirm no regression**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_service.py tests/unit/test_command_service_errors.py -v`
Expected: PASS — the added branch only triggers on `logged and not success and not output`, so existing success/non-logged paths are unchanged.

- [ ] **Step 6: Commit**

```bash
git add app/services/command_executor.py tests/unit/test_command_executor_log_tail.py
git commit -m "feat(executor): backfill failed logged-command output from log tail

A failed logged command's SSH channel is empty (output redirected to
/dev/null on the target). Fetch the control_node log tail so the persisted
output — and the API response — shows the real failure.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01G8D5Eq7UYX6SGix9ZttWvG"
```

---

## Task 4: Heal-path backfill — pass the log tail into `mark_failed`

**Files:**
- Modify: `app/services/command_state_helpers.py` (`_heal_from_marker`, lines ~99-110)
- Test: `tests/unit/test_command_orphan_heal.py` (add cases)

**Interfaces:**
- Consumes: `self._ssh._read_log_tail(state, n) -> Optional[str]` (Task 2); `settings.COMMAND_LOG_FAILURE_TAIL_LINES`; `CommandState.mark_failed(message, exit_code=..., output=...)` (existing signature at `app/domain/command.py:59`).
- Produces: on a marker-driven failure, `CommandState.output` is set to the log tail (so a cross-pod recovered failure also shows its error).

**Context — current code** (`app/services/command_state_helpers.py:99-110`):

```python
        success = code == 0
        async def updater(s: CommandState):
            if success:
                s.mark_success(code, "")
            else:
                s.mark_failed(
                    f"Recovered from control_node marker: exit {code}.",
                    exit_code=code,
                )
```

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_command_orphan_heal.py` (it already imports `AsyncMock`, `MagicMock`, `_state`, `_svc`):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_orphan_heal.py::test_heal_failed_backfills_output_from_log_tail -v`
Expected: FAIL — `resp.output` is `None` (heal currently calls `mark_failed` without `output`), so the `"could not read Username" in ...` assertion fails.

- [ ] **Step 3: Read the log tail before healing and pass it into `mark_failed`**

In `app/services/command_state_helpers.py`, replace the block at lines 99-110:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_command_orphan_heal.py -v`
Expected: PASS — the three new tests pass AND all existing heal tests still pass (success path never reads the tail; `None` tail leaves `output` unset).

- [ ] **Step 5: Commit**

```bash
git add app/services/command_state_helpers.py tests/unit/test_command_orphan_heal.py
git commit -m "feat(heal): backfill recovered-failure output from control_node log tail

When healing a failed run from the .exit marker, also read the run log
tail into output so a cross-pod recovered failure shows its real error,
matching the fast path. SSH failure leaves output unset (never 5xx).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01G8D5Eq7UYX6SGix9ZttWvG"
```

---

## Task 5: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole unit + integration suite**

Run: `APP_ENV=test uv run pytest tests/ -v`
Expected: PASS — all tests green, including the four touched files and the new `test_command_ssh_log_tail.py` / `test_command_executor_log_tail.py`.

- [ ] **Step 2: Confirm no accidental behavioural drift on non-logged commands**

Run: `APP_ENV=test uv run pytest tests/ -k "command" -v`
Expected: PASS — non-logged command tests unchanged (backfill only fires for logged+failed+empty).

- [ ] **Step 3: Final review of the diff against the spec**

Run: `git diff develop --stat`
Expected: only these files changed — `ansible/run-ansible.sh`, `app/services/command_ssh.py`, `app/services/command_executor.py`, `app/services/command_state_helpers.py`, the four test files, and the spec/plan docs (plus the carried-over data/config files already committed in the spec commit).

---

## Notes for the implementer

- **Why fetch `self.repo.get(command_id)` in Task 3 instead of building a state:** `_read_log_tail` → `_connect_to_control_node` rebuilds the SSH connection purely from `CommandState` fields (`resolved_ip`, `port`, `username`, `ssh_config`, `run_log_path`). The state was persisted at `_handle_async_execution` line 628, so it's authoritative. Reconstructing a partial state risks drift.
- **Why `_read_log_tail` lives on `SshSupport`, not `StateHelpers`:** both the executor (fast-path) and `StateHelpers` (heal-path) hold `self._ssh: SshSupport`. Putting it on the shared SSH layer lets both call it without the executor depending on `StateHelpers`.
- **`_apply_output_policy` is intentionally left unchanged:** it already tails a non-empty output to `COMMAND_LOG_FAILURE_TAIL_LINES`. Feeding it a tail that's already ≤ N lines is a harmless no-op. The backfill happens *before* the policy call.
- **Bash EXIT trap is singular:** do not add a second `trap ... EXIT`; the second would silently replace the first (`cleanup`, which does the clone `rm -rf`). Everything goes through the one extended `cleanup`.
