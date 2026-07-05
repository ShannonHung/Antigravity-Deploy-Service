# Script Contract — Version Checking & Log Viewer Integration

---

## Purpose

Any `.sh` script that deploy-service executes over its SSH command API can opt into two
optional but related features, purely by following the conventions in this document:

- **Version checking** — deploy-service can refuse to run a script that is older than a
  required minimum, *before* the pipeline runs.
- **Live log viewing** — deploy-service can tee the script's output to a per-run log file
  and expose it through the `/execution/{command_id}/view` auto-refreshing viewer.

Neither feature requires changes to deploy-service itself. A script earns them by
implementing the small CLI surface described below, and a whitelist entry opts the
command into the corresponding behaviour. `ansible/run-ansible.sh` is the reference
implementation and is cross-checked against this contract on every change.

See `docs/arch/ssh-command.md` for the full design of the SSH command API (whitelist
model, anti-injection architecture, process-group tracking, Redis state machine); this
document only covers the script-side contract layered on top of it.

---

## Version contract

A script that wants to be version-checked MUST implement:

- **`--version`** — prints `<name> X.Y.Z` to stdout, exits `0`, and has no side effects
  (no docker pull, no cloning, no mutation of any kind). deploy-service runs exactly
  this over the existing SSH connection as a pre-check.
- **`--min-version X.Y.Z`** — self-guard. On startup, before doing any real work, the
  script compares its own `SCRIPT_VERSION` against the given minimum. If
  `SCRIPT_VERSION < min` (or either version string is malformed), it prints the reason
  to stderr and exits with code **`4`**.

Version numbers use strict semver `X.Y.Z` (three numeric segments, no pre-release or
build metadata). Comparison is numeric per segment, not lexicographic —
`1.10.0 > 1.9.0`. `run-ansible.sh`'s `version_ge()` is the reference implementation:

```bash
SCRIPT_VERSION="1.0.0"

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

`--version` and `--min-version` are handled as ordinary flags in argument parsing and
must not require any of the script's other required arguments (e.g. `--playbook`,
`--inventory`) to be present — deploy-service needs to be able to call `--version` alone.

### deploy-service side of the contract

When a whitelist command has `checks_script_version: true`, deploy-service runs
`<script> --version` over the already-open SSH connection **before** executing the
pipeline. It parses the last `X.Y.Z`-shaped token out of combined stdout/stderr and
compares it against the effective minimum:

```
effective_minimum = max(whitelist_min_script_version, api_min_script_version)
```

- The whitelist's `min_script_version` is the floor — it can never be lowered by a
  request.
- A request-supplied `min_script_version` (see [API override](#api-override)) can only
  raise that floor, never relax it.

If the target script is missing, unreadable, exits non-zero on `--version`, prints an
unparsable version, or reports a version below the effective minimum, deploy-service
raises a **412 Precondition Failed** (`ScriptVersionException`) and does **not** run the
pipeline. This is a fail-closed check: any inability to determine the version is treated
as a rejection, not as "assume it's fine."

---

## Log/view contract

A script that wants to support the live `/view` log viewer MUST implement:

- **`--run-id <id>`** — accept a run identifier. deploy-service generates this
  server-side (a UUID, reused as the `command_id`) and injects it into the pipeline via
  the `{run_id}` placeholder — the script never has to invent its own id.
- **Tee execution output** to `<log-dir>/<run_id>.log`. This file is what
  `GET /execution/{command_id}/view` and its polling endpoint
  (`/execution/{command_id}/trace/ui`) read from, so it must contain the full,
  human-readable output of the run as it happens (not buffered until exit).
- **On completion, write `<log-dir>/<run_id>.exit`** containing a single integer exit
  code and nothing else. deploy-service reads this file to heal its state machine if the
  owning pod crashes or loses track of the run mid-flight.
- **Append an `=== EXIT <code> ===` marker** to the tail of the log file once the run
  finishes, so a viewer tailing the log can detect completion without depending solely
  on the `.exit` file.

`run-ansible.sh`'s `run_normal()` is the reference implementation:

```bash
set +e
docker run --rm ... "$IMAGE" "${CMD_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
RUN_EXIT="${PIPESTATUS[0]}"
set -e

echo "=== EXIT $RUN_EXIT ===" >> "$LOG_FILE"

if [[ -n "$RUN_ID" ]]; then
  EXIT_FILE="$LOG_DIR/$RUN_ID.exit"
  printf '%s\n' "$RUN_EXIT" > "$EXIT_FILE.tmp" && mv -f "$EXIT_FILE.tmp" "$EXIT_FILE"
fi

exit "$RUN_EXIT"
```

Two details worth preserving if you adapt this pattern:

- Capture the exit code from `${PIPESTATUS[0]}` (the producer side of the pipe), not
  `tee`'s own exit code — otherwise a failing run looks like a success because `tee`
  itself exits `0`.
- Write the `.exit` file via a temp file + atomic `mv`, so a concurrent reader never
  observes a partially-written exit code.

`{run_id}` is a **server-injected pipeline placeholder** — it is not a user-controllable
argument. deploy-service only assigns and substitutes it for commands whose whitelist
entry has `logged: true`.

---

## Whitelist configuration

A command opts into these behaviours entirely through its whitelist entry (see
`docs/arch/ssh-command.md` §3 for the full whitelist schema). Worked example:

```json
{
  "command_name": "run_my_tool",
  "logged": true,
  "checks_script_version": true,
  "min_script_version": "1.2.0",
  "pipeline": [
    {
      "command": [
        "/opt/tools/my-tool.sh",
        "--run-id", "{run_id}",
        "--log-dir", "/var/log/deploy-service"
      ]
    }
  ],
  "arguments": []
}
```

- **`logged: true`** enables the tee-to-file + `.exit` marker convention above, and
  switches the command over to the [output policy](#output-policy-that-comes-with-logged-true)
  described below. The `/view` endpoint is meaningful for this command once it's set.
- **`checks_script_version: true`** enables the version pre-check described above. It
  **requires** `min_script_version` to also be set — the two fields are validated
  together at whitelist load time, so a `checks_script_version: true` entry with no
  baseline version fails loudly at startup rather than silently skipping the check at
  request time.
- **`min_script_version: "1.2.0"`** is the whitelist floor for this command; see
  [API override](#api-override) for how a request can raise it further.
- **`{run_id}`** in the pipeline's `command` array is the server-injected placeholder —
  do not define it under `arguments`; deploy-service substitutes it automatically for
  `logged: true` commands.
- **`--log-dir /var/log/deploy-service`** should match the deployment's `COMMAND_LOG_DIR`
  setting (default: `/var/log/deploy-service`) so the log/exit files land where
  deploy-service expects to find them for `/view` and state-healing.

---

## Output policy that comes with `logged: true`

For commands with `logged: true`, the API response's `output` field is intentionally
reduced — the full transcript belongs in `/view`, not in the polling response. This is
implemented in `CommandExecutor._apply_output_policy`:

- **On success**: the response carries **no output** at all (`output` is empty/absent).
  Go to `/view` (or `/execution/{command_id}/trace/ui`) for the full log.
- **On failure**: the response includes only the **last `COMMAND_LOG_FAILURE_TAIL_LINES`
  lines** of output (default **50**), so a caller polling the result endpoint gets enough
  context to see what went wrong without pulling the entire log inline.
- **Regardless of outcome**, the complete log is always available at `/view` for the
  lifetime of the log file — the tail-truncation only affects what's embedded in the
  JSON response.

Non-logged commands are unaffected: they keep their full output in the response, as
before.

---

## API override

A request may pass `min_script_version` to *raise* the required minimum for that one
call. The effective minimum used by the pre-check is:

```
effective_minimum = max(whitelist_min_script_version, request_min_script_version)
```

The API can only tighten the requirement, never lower the whitelist's floor — a caller
cannot use `min_script_version` to bypass a stricter baseline set in the whitelist. This
is useful when a caller knows it depends on a fix that landed after the whitelist's
baseline (e.g. it wants at least `1.3.0` even though the whitelist only guarantees
`1.2.0`), without needing deploy-service's whitelist to be edited and redeployed.

---

## Reference implementation

- **`ansible/run-ansible.sh`** — implements the full contract: `SCRIPT_VERSION`,
  `--version`, `--min-version` self-guard, `--run-id`/`--log-dir`, tee to
  `<log-dir>/<run_id>.log`, the `=== EXIT <code> ===` marker, and the
  `<log-dir>/<run_id>.exit` file written atomically after the run.
- **`docs/arch/ssh-command.md`** — the full SSH command API design: whitelist schema,
  anti-injection architecture, process-group tracking and kill, the Redis-backed state
  machine, and the `/view` log viewer's polling mechanism. Read this first if you are
  unfamiliar with the whitelist/pipeline model this contract builds on.
