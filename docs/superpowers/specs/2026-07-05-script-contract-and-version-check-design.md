# Script Contract & Version Check — Design

**Date:** 2026-07-05
**Status:** Approved, ready for implementation planning
**Scope:** `deploy-service/`

## Problem

`run-ansible.sh` is copied onto a production control-node and maintained
long-term. Two gaps follow from that:

1. **No self-identifying version.** Once the script leaves the repo you cannot
   tell, from the file on the node, which version it is or whether it matches
   what `deploy-service` expects. It is also a subprocess *contract* of
   `deploy-service` (`--run-id`, `.exit` sidecar, EXIT marker) — drift between
   the node's copy and the caller silently breaks things.

2. **The script is no longer going to be the only one.** Other operators will
   run their own scripts (`my-tool.sh`, …) through `deploy-service`'s SSH
   command API. They want the same capabilities: version checking and the live
   `/view` log viewer. Today those behaviours exist but only as an *implicit*
   contract embodied by `run-ansible.sh`.

This design turns the implicit contract into (a) an explicit, **general**
per-command capability in `deploy-service`, and (b) a written contract document
any script can follow. `run-ansible.sh` becomes the first conforming script and
the reference implementation.

## Key decisions (all approved)

1. **Version check runs on both sides.** `deploy-service` pre-checks before
   running the real pipeline (fast failure, no wasted clone/run), AND the script
   self-guards via `--min-version` (a manually-run stale script is still
   blocked).
2. **Strict semver `X.Y.Z`, numeric per-segment comparison.** `1.10.0 > 1.9.0`.
   No pre-release / build metadata. A malformed version is rejected with an
   error, not silently accepted.
3. **Version requirement lives in the whitelist per command; no global env
   fallback.** Different scripts have unrelated version lines, so a single global
   default is meaningless. Each command that opts in declares its own baseline.
4. **Two-layer minimum: whitelist default, API may raise it.** The whitelist
   command's `min_script_version` is the default baseline (set once, everyone
   uses it). An API request may pass `min_script_version` to raise the bar.
   **Effective minimum = `max(whitelist_default, api_value)`** — the API can only
   tighten, never lower the whitelist's floor (the whitelist is authoritative).
5. **Detection via an explicit `checks_script_version: true` flag**, not
   filename heuristics — consistent with the codebase's existing per-command
   boolean flags (`killable`, `logged`, `disconnects_ssh`) and its
   "everything is explicitly declared in the whitelist" philosophy.
6. **Version query is convention-based: always `--version`.** A script that
   wants checking MUST implement `--version` printing `<name> X.Y.Z`.
   `deploy-service` always asks with `--version` — it does not need to know the
   script's name or purpose.
7. **`/view` + `logged` + `<run_id>.log` / `<run_id>.exit` already exist and are
   already generic.** No code change needed for the log/view capability — it is
   only documented. Two small adjustments accompany it (log dir default, doc).

## Architecture

Three independent pieces:

### A. Script contract (documented in `docs/arch/script-contract.md`)

Any `.sh` that wants to integrate with `deploy-service`.

**Version contract** (MUST, if the script wants version checking):
- `--version` → prints `<name> X.Y.Z` to stdout, `exit 0`, no side effects.
- `--min-version X.Y.Z` → self-guard; if `SCRIPT_VERSION < min`, print the
  reason to stderr and exit with a dedicated exit code (distinct from usage
  errors). If the version format is invalid, error out.
- Version numbers are strict semver `X.Y.Z`.

**Log/view contract** (MUST, if the script wants `/view` support):
- Accept `--run-id <id>`.
- `tee` execution output to `<log-dir>/<run_id>.log`.
- On completion, write `<log-dir>/<run_id>.exit` containing a single integer
  exit code (the platform reads this to heal its state machine).
- Append an `=== EXIT <code> ===` marker to the log tail.

The document also shows **how to configure the whitelist** for such a script
(see section D) and uses `run-ansible.sh` as the worked reference example.

### B. `run-ansible.sh` changes (script side)

Two independent additions:

**(a) Self-identifying version**
- Top-level constant `SCRIPT_VERSION="1.0.0"`.
- `--version` flag: print `run-ansible.sh 1.0.0`, `exit 0` at the arg-parse
  stage (before any clone / docker work).
- `print_summary` prints the version on its first line, so every
  `<run_id>.log` records which script version produced it.

**(b) Self-guard `--min-version <X.Y.Z>`**
- Given a minimum, compare `SCRIPT_VERSION >= min`; if not satisfied, echo the
  reason to stderr and exit with a dedicated code (e.g. `4`, distinct from the
  existing `2` = usage error).
- Validate both `SCRIPT_VERSION` and the passed min against
  `^[0-9]+\.[0-9]+\.[0-9]+$`; error out otherwise.
- A pure `version_ge(a, b)` helper function performs the per-segment numeric
  comparison, defined in the script.

### C. `deploy-service` changes (caller side)

**Env / config**
- `COMMAND_LOG_DIR` default changes from `/var/log/ansible-runs` to
  **`/var/log/deploy-service`** in `app/core/config.py` (ansible-specific →
  service-generic).
- **No** new env for version defaults; **no** global fallback (per decision 3).

**Whitelist schema** (the command config model in `app/domain/command.py`)
adds two optional fields:
- `checks_script_version: bool = False` — the opt-in switch.
- `min_script_version: Optional[str] = None` — this command/script's default
  minimum.
- **Load-time validation:** if `checks_script_version` is `true` but
  `min_script_version` is absent (or not valid semver), that is a configuration
  error and MUST fail loudly at load. A forgotten baseline fails loudly instead
  of silently doing nothing.

**API request** may optionally carry `min_script_version` (validated as semver
when present). Effective minimum = `max(whitelist_default, api_value)`.

**Version comparison utility** — new `app/core/version.py`, pure / I/O-free,
easily unit-tested:
- `parse_semver(s) -> tuple[int, int, int]` (raises `ValueError` on malformed
  input).
- `version_ge(a, b) -> bool`.

This is the Python twin of the script's bash `version_ge` — same semantics, two
implementations.

**Pre-check flow** (only for commands with `checks_script_version: true`),
inserted before the real pipeline runs:
1. `effective_min = max(whitelist min, API min)`.
2. Over the existing SSH execution path, run the script with `--version` on the
   target host; parse the trailing `X.Y.Z` from the output (tolerating a
   leading script name).
3. `actual >= effective_min` → proceed with the original pipeline. Otherwise —
   or on parse/fetch failure — **do not run the pipeline**; return a structured
   error (a new `BaseAppException` subclass, e.g. `ScriptVersionException`) via
   the global handler → `ApiResponse` (with `request_id`), carrying a clear
   reason message, e.g. *"run-ansible.sh on control_node is version 1.0.0, below
   the required 1.2.0"*.
4. A version mismatch is an expected rejection, not a 500 — map to 4xx
   (e.g. 412 Precondition Failed).

**Double guard:** `deploy-service` pre-check blocks up-front (fast failure, no
wasted clone/run); the script's own `--min-version` self-guard is the backstop
(a manually-run stale script is still blocked).

### D. Whitelist configuration (documented pattern)

To make a command version-checked and `/view`-capable:
- `logged: true`
- pipeline carries `--run-id {run_id}` (`{run_id}` is the server-injected
  placeholder), `--log-dir <COMMAND_LOG_DIR>`
- to enable version checking, add `checks_script_version: true` +
  `min_script_version: "X.Y.Z"`

**Existing `run_ansible` / `run_ansible_clock` whitelist entries** get their
hard-coded `--log-dir /var/log/ansible-runs` updated to
`/var/log/deploy-service` to match the new `COMMAND_LOG_DIR` default (four
occurrences across `data/allow-commands-admin.json` and
`data/allow-commands-cluster_proxy.json`).

**Output policy that accompanies `logged: true` (already implemented — document
only).** `_apply_output_policy` (`app/services/command_executor.py`) already
gives every `logged: true` command this behaviour, and it is bound to the
`logged` flag (not to ansible), so any conforming script inherits it:
- **Success →** the API response carries no output; the caller goes to `/view`
  for the full log.
- **Failure →** the response includes the last `COMMAND_LOG_FAILURE_TAIL_LINES`
  lines (default **50**) of output, so the error is visible inline.
- **Full content →** always available at `/view` (the control_node
  `<run_id>.log`), regardless of outcome.

The contract document MUST state this so script authors know that opting into
`logged: true` also opts into this success-silent / failure-tail policy. No code
change — this is documentation only.

## What is NOT changing

- The `/view` route, the `logged` output-detachment mechanism, the
  `<run_id>.log` / `<run_id>.exit` / EXIT-marker heal machinery, and the
  success-silent / failure-tail output policy (`_apply_output_policy`) — all
  already exist and are already generic. This design documents them and adds the
  two new whitelist fields + version pre-check; it does not rework the log/view
  path or the output policy.
- The anti-injection guarantee (discrete args, no `eval`, `shlex.join`) is
  untouched on both sides.

## Testing

- **`app/core/version.py`:** unit tests for `parse_semver` (valid, malformed,
  two-digit segments) and `version_ge` (`1.10.0 >= 1.9.0`, equality, strict
  less-than).
- **Whitelist load validation:** `checks_script_version: true` without a valid
  `min_script_version` raises at load.
- **Effective-minimum logic:** `max(whitelist, api)` — API raises, API-lower is
  ignored, API-absent uses whitelist.
- **Pre-check outcome:** actual ≥ min proceeds; actual < min raises
  `ScriptVersionException` (4xx) and the pipeline never runs; parse/fetch
  failure is treated as a rejection.
- **Script:** a unit test (no docker/network, via the existing `DRYRUN`/test
  hooks) that `--version` prints `run-ansible.sh X.Y.Z` and exits 0, and that
  `--min-version` above `SCRIPT_VERSION` exits with the dedicated code.

## Deliverables

1. `docs/arch/script-contract.md` — the general script contract (English),
   including the `logged: true` output policy (success-silent / failure-tail /
   full log at `/view`).
2. `run-ansible.sh`: `SCRIPT_VERSION`, `--version`, `--min-version`,
   `version_ge`, version in summary/log.
3. `app/core/version.py` + tests.
4. Whitelist model: `checks_script_version`, `min_script_version`, load-time
   validation.
5. API request field `min_script_version` + effective-minimum (`max`) logic.
6. `ScriptVersionException` + pre-check step wired into the command flow.
7. `COMMAND_LOG_DIR` default → `/var/log/deploy-service`; update the four
   whitelist `--log-dir` occurrences to match.
