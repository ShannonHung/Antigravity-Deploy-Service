#!/usr/bin/env bash
#
# run-ansible.sh — launch the ansible runner image to execute a playbook.
#
# clone inventory -> docker run ansible image -> ansible-playbook -> SSH -> nodes
#
# Always-latest: the inventory repo is cloned FRESH every run and deleted on exit
# (trap); the runner image is `docker pull`-ed unless --no-pull. All user values
# are passed as DISCRETE ARGS (never interpolated into a shell string) — this is
# the load-bearing anti-injection guarantee. Do NOT add `eval` here.

set -euo pipefail

# ── Fixed config (not user-overridable by design) ────────────────────────────
# Inventory repos all live under one GitLab namespace; --inventory-repo-name
# selects which repo to clone (default my-ansible-inventory). The full
# INVENTORY_REPO env var still wins when set (e.g. a file:// path for local
# testing) — see resolve_inventory_repo().
INVENTORY_NAMESPACE="https://gitlab.com/ShannonHung"
INVENTORY_REPO_NAME="my-ansible-inventory"   # --inventory-repo-name <name>
IMAGE="shannonhung/ansible-runner:latest"
SCRIPT_VERSION="2.1.0"
# Where the auto-generated vault password file is mounted INSIDE the container;
# ANSIBLE_VAULT_PASSWORD_FILE is pointed here so ansible finds it automatically.
VAULT_PASS_CONTAINER="/ansible_vault"

# ── Defaults ─────────────────────────────────────────────────────────────────
PLAYBOOK=""
INVENTORY=""               # path RELATIVE to the inventory repo root
INVENTORY_REF="main"       # branch/tag of the inventory repo to clone
SECRET_PATH="${SECRET_PATH:-}"  # --secret-path <path>: a KEY=VALUE env file
                           # sourced before the run (e.g. INVENTORY_TOKEN for the
                           # inventory clone, ANSIBLE_VAULT_PASSWORD for vault).
                           # Also honoured from the SECRET_PATH env var.
TAGS=""
LIMIT=""
EXTRA_VARS=""
IMAGE_TAG=""               # --image-tag <tag>: shannonhung/ansible-runner:<tag>
IMAGE_SET=0                # 1 if --image was given (for mutual-exclusion check)
PULL=1                     # docker pull before run; --no-pull disables
MODE="normal"              # normal | debug | dry-run
WANT_DEBUG=0
WANT_DRY_RUN=0
LOG_DIR="$(pwd)/logs"
RUN_ID=""                  # per-run id from deploy-service; log is <run_id>.log
LOG_RETENTION_DAYS=3       # prune <log-dir>/*.log older than this many days
SSH_KEY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/ssh_keys/client_key"
MIN_VERSION=""             # --min-version <X.Y.Z>: self-guard minimum
# Host path of the auto-generated vault password file. Lives beside this script
# (host-consistent for the DooD bind mount, same reasoning as the clone dir).
# Overridable via the VAULT_PASS_FILE env var (used by tests).
VAULT_PASS_FILE="${VAULT_PASS_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.ansible_vault}"

usage() {
  cat <<'EOF'
Usage: run-ansible.sh --playbook <file> --inventory <repo-relative-path> [options]

Required (except with --debug, where both are optional):
  --playbook <file>       Playbook filename under playbooks/ (e.g. ping.yml)
  --inventory <path>      Inventory path RELATIVE to the inventory repo root
                          (e.g. taipei/multinode.ini)

Options:
  --inventory-ref <ref>   Branch/tag of the inventory repo to clone (default: main)
  --inventory-repo-name <name>  Inventory repo under the fixed namespace to clone
                          (default: my-ansible-inventory; e.g. ansible-inventory-v2)
  --secret-path <path>    KEY=VALUE env file sourced before the run. Recognised
                          keys: INVENTORY_TOKEN (private inventory clone),
                          INVENTORY_TOKEN_USER (Basic-auth username; default
                          oauth2 for a PAT, set to the deploy token's username
                          for a GitLab deploy token) and ANSIBLE_VAULT_PASSWORD
                          (ansible-vault decrypt). INVENTORY_TOKEN and
                          ANSIBLE_VAULT_PASSWORD are stored base64-encoded and
                          decoded at load time (INVENTORY_TOKEN_USER is not).
                          The file must be chmod 600/400
                          (not group/other-accessible).
                          Secret VALUES are never accepted as CLI args, never
                          logged, and never placed in argv/URL. Also honoured from
                          the SECRET_PATH env var.
                          When ANSIBLE_VAULT_PASSWORD is present, a vault password
                          file (.ansible_vault, chmod 600, gitignored) is generated
                          beside this script, mounted into the container, and
                          ANSIBLE_VAULT_PASSWORD_FILE is preset — so ansible (and
                          manual runs in a --debug container) decrypt with no
                          extra flags.
  --tags <tags>           Comma-separated ansible --tags
  --limit <pattern>       ansible --limit host/group pattern
  --extra-vars <k=v ...>  ansible --extra-vars string
  --image <name>          Runner image full name (default: shannonhung/ansible-runner:latest)
  --image-tag <tag>       Use shannonhung/ansible-runner:<tag> (mutually exclusive with --image)
  --no-pull               Skip `docker pull` (use a locally-built image)
  --log-dir <path>        Host dir to mount for logs (default: ./logs)
  --run-id <id>           Per-run id; log is <log-dir>/<id>.log (^[A-Za-z0-9_-]+$)
  --log-retention-days <n>  Delete <log-dir>/*.log older than n days (default: 3; 0 disables)
  --ssh-key <path>        SSH private key to mount (default: ../data/ssh_keys/client_key)
  --dry-run               Clone inventory + print summary/commands; do NOT pull or run docker
  -d, --debug             Start the runner container idle (sleep infinity) for
                          manual `docker exec` debugging; do NOT run ansible.
                          --playbook/--inventory are optional here: omit both to
                          start a bare container with no inventory mounted.
  -h, --help              Show this help

The inventory repo is cloned fresh each run (under the fixed namespace
https://gitlab.com/ShannonHung) and removed afterward. Select it with
--inventory-repo-name; private repos authenticate via INVENTORY_TOKEN, which
(together with ANSIBLE_VAULT_PASSWORD) is typically supplied by --secret-path.

Example:
  ./run-ansible.sh --playbook ping.yml --inventory taipei/multinode.ini --limit node1
  ./run-ansible.sh -d --playbook ping.yml --inventory taipei/multinode.ini --limit node1
  ./run-ansible.sh --secret-path ~/.secrets.env --inventory-repo-name ansible-inventory-v2 \
      --playbook ping.yml --inventory taipei/multinode.ini
  # ~/.secrets.env (chmod 600; INVENTORY_TOKEN and ANSIBLE_VAULT_PASSWORD
  # are stored base64-encoded and decoded at load time):
  #   INVENTORY_TOKEN=$(printf %s glpat-xxx | base64)
  #   ANSIBLE_VAULT_PASSWORD=$(printf %s s3cr3t | base64)
EOF
}

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
    if (( 10#${A[i]} > 10#${B[i]} )); then return 0; fi
    if (( 10#${A[i]} < 10#${B[i]} )); then return 1; fi
  done
  return 0
}

# ── Arg parsing ──────────────────────────────────────────────────────────────
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --playbook)            PLAYBOOK="$2"; shift 2 ;;
      --inventory)           INVENTORY="$2"; shift 2 ;;
      --inventory-ref)       INVENTORY_REF="$2"; shift 2 ;;
      --inventory-repo-name) INVENTORY_REPO_NAME="$2"; shift 2 ;;
      --secret-path)         SECRET_PATH="$2"; shift 2 ;;
      --tags)                TAGS="$2"; shift 2 ;;
      --limit)               LIMIT="$2"; shift 2 ;;
      --extra-vars)          EXTRA_VARS="$2"; shift 2 ;;
      --image)               IMAGE="$2"; IMAGE_SET=1; shift 2 ;;
      --image-tag)           IMAGE_TAG="$2"; shift 2 ;;
      --no-pull)             PULL=0; shift ;;
      --log-dir)             LOG_DIR="$2"; shift 2 ;;
      --run-id)              RUN_ID="$2"; shift 2 ;;
      --log-retention-days)  LOG_RETENTION_DAYS="$2"; shift 2 ;;
      --ssh-key)             SSH_KEY="$2"; shift 2 ;;
      --dry-run)             WANT_DRY_RUN=1; shift ;;
      -d|--debug)            WANT_DEBUG=1; shift ;;
      --version)             echo "run-ansible.sh $SCRIPT_VERSION"; exit 0 ;;
      --min-version)         MIN_VERSION="$2"; shift 2 ;;
      -h|--help)             usage; exit 0 ;;
      *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
  done

  # Debug mode starts an idle container for manual `docker exec`, so a
  # playbook/inventory are optional there; every other mode needs both. When
  # only one is supplied in debug mode, still require the pair (an inventory
  # with no playbook, or vice versa, is almost certainly a mistake).
  if [[ "$WANT_DEBUG" -ne 1 && ( -z "$PLAYBOOK" || -z "$INVENTORY" ) ]]; then
    echo "Error: --playbook and --inventory are required (except with --debug)." >&2
    usage
    exit 2
  fi
  if [[ "$WANT_DEBUG" -eq 1 ]] && \
     { [[ -n "$PLAYBOOK" && -z "$INVENTORY" ]] || [[ -z "$PLAYBOOK" && -n "$INVENTORY" ]]; }; then
    echo "Error: --playbook and --inventory must be given together (or both omitted with --debug)." >&2
    exit 2
  fi

  if [[ "$IMAGE_SET" -eq 1 && -n "$IMAGE_TAG" ]]; then
    echo "Error: --image and --image-tag are mutually exclusive." >&2
    exit 2
  fi
  if [[ -n "$IMAGE_TAG" ]]; then
    IMAGE="shannonhung/ansible-runner:$IMAGE_TAG"
  fi

  if [[ "$WANT_DEBUG" -eq 1 && "$WANT_DRY_RUN" -eq 1 ]]; then
    echo "Error: --debug and --dry-run are mutually exclusive." >&2
    exit 2
  fi
  if [[ "$WANT_DEBUG" -eq 1 ]]; then MODE="debug"; fi
  if [[ "$WANT_DRY_RUN" -eq 1 ]]; then MODE="dry-run"; fi

  # The repo name becomes part of a clone URL path segment, so constrain it.
  if [[ ! "$INVENTORY_REPO_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "Error: --inventory-repo-name must match ^[A-Za-z0-9._-]+$" >&2
    exit 2
  fi

  # --secret-path must point at a readable file if given (its VALUES are sourced
  # later, in load_secrets, which also enforces the 600/400 permission bound).
  if [[ -n "$SECRET_PATH" && ! -f "$SECRET_PATH" ]]; then
    echo "Error: --secret-path not found: $SECRET_PATH" >&2
    exit 2
  fi

  if [[ -n "$MIN_VERSION" ]]; then
    if ! version_ge "$SCRIPT_VERSION" "$MIN_VERSION"; then
      echo "Error: run-ansible.sh version $SCRIPT_VERSION is below the required minimum $MIN_VERSION." >&2
      exit 4
    fi
  fi
}

# ── Per-run log file + self-cleaning ─────────────────────────────────────────
resolve_log_file() {
  # RUN_ID becomes a filename, so validate it strictly. Empty RUN_ID keeps the
  # legacy single-file behaviour (run.log) for standalone use.
  if [[ -n "$RUN_ID" ]]; then
    if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
      echo "Error: --run-id must match ^[A-Za-z0-9_-]+$" >&2
      exit 2
    fi
    LOG_FILE="$LOG_DIR/$RUN_ID.log"
  else
    LOG_FILE="$LOG_DIR/run.log"
  fi

  if [[ ! "$LOG_RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
    echo "Error: --log-retention-days must be a non-negative integer." >&2
    exit 2
  fi

  mkdir -p "$LOG_DIR"

  # Prune old logs BEFORE work so a killed run never skips cleanup. Guarded so an
  # empty LOG_DIR can't widen the delete scope. Only files older than the window
  # go — never the in-flight <run_id>.log. 0 disables.
  if [[ "$LOG_RETENTION_DAYS" -gt 0 && -n "$LOG_DIR" && -d "$LOG_DIR" ]]; then
    find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -mtime "+$LOG_RETENTION_DAYS" -delete 2>/dev/null || true
  fi

  # Test/inspection hook: print the resolved log path and exit before any docker
  # or git work. Used by the script's unit test (no network/docker required).
  if [[ "${DRYRUN:-0}" == "1" ]]; then
    echo "DRYRUN log file: $LOG_FILE"
    exit 0
  fi
}

# ── Inventory repo URL + auth resolution ─────────────────────────────────────
# The full INVENTORY_REPO env var wins when set (e.g. a file:// path for local
# testing); otherwise the URL is built from the fixed namespace + repo name.
resolve_inventory_repo() {
  if [[ -n "${INVENTORY_REPO:-}" ]]; then
    INVENTORY_REPO="$INVENTORY_REPO"
  else
    INVENTORY_REPO="$INVENTORY_NAMESPACE/$INVENTORY_REPO_NAME.git"
  fi
}

# Load secrets from --secret-path (a KEY=VALUE env file) into the environment so
# the rest of the run can consume them (INVENTORY_TOKEN for the clone,
# ANSIBLE_VAULT_PASSWORD for vault). `set -a` exports every key the file defines
# so `docker run -e NAME` (value-less form) and git's env-config pick them up.
#
# The file is `source`d, i.e. executed as shell — so it MUST be trusted. We
# refuse to source anything group/other-accessible (a writable file would be
# arbitrary code execution) to keep the trust boundary honest.
load_secrets() {
  [[ -z "$SECRET_PATH" ]] && return 0
  if [[ ! -f "$SECRET_PATH" ]]; then
    echo "Error: --secret-path not found: $SECRET_PATH" >&2
    exit 2
  fi
  local mode=""
  if ! mode="$(stat -c '%a' "$SECRET_PATH" 2>/dev/null)"; then
    mode="$(stat -f '%Lp' "$SECRET_PATH" 2>/dev/null || echo "")"
  fi
  # Reject if group/other bits are set (last two octal digits must be "00").
  if [[ -n "$mode" && "${mode: -2}" != "00" ]]; then
    echo "Error: secret file $SECRET_PATH must be chmod 600/400 (not group/other-accessible). Current: $mode" >&2
    exit 2
  fi
  set -a
  # shellcheck disable=SC1090
  . "$SECRET_PATH"
  set +a

  # The secret file stores INVENTORY_TOKEN and ANSIBLE_VAULT_PASSWORD base64-
  # encoded; decode them here, ONCE, so every downstream consumer (resolve_token
  # for the clone, ensure_vault_client for the vault file) sees plaintext and
  # nothing has to decode again. Only decode keys that are actually present —
  # both are optional (anonymous clone / no-vault runs supply neither). A value
  # that isn't valid base64 fails the run: better to stop than to clone with a
  # mangled token or decrypt with a mangled password. The value itself is never
  # printed. `set -a` above already marked these for export, so re-assigning
  # them keeps them exported.
  local decoded
  if [[ -n "${INVENTORY_TOKEN:-}" ]]; then
    if ! decoded="$(printf '%s' "$INVENTORY_TOKEN" | base64 -d 2>/dev/null)"; then
      echo "Error: INVENTORY_TOKEN in $SECRET_PATH is not valid base64." >&2
      exit 2
    fi
    INVENTORY_TOKEN="$decoded"
  fi
  if [[ -n "${ANSIBLE_VAULT_PASSWORD:-}" ]]; then
    if ! decoded="$(printf '%s' "$ANSIBLE_VAULT_PASSWORD" | base64 -d 2>/dev/null)"; then
      echo "Error: ANSIBLE_VAULT_PASSWORD in $SECRET_PATH is not valid base64." >&2
      exit 2
    fi
    ANSIBLE_VAULT_PASSWORD="$decoded"
  fi
}

# Auto-generate the vault password file next to this script (VAULT_PASS_FILE),
# ONLY when a vault password is available (sourced from --secret-path). It holds
# the raw ANSIBLE_VAULT_PASSWORD, chmod 600 and NOT executable, so ansible reads
# it directly. It is mounted read-only into the container and pointed at by
# ANSIBLE_VAULT_PASSWORD_FILE — so both normal and debug containers decrypt with
# no extra flags. Rewritten every run so it can never drift; gitignored.
ensure_vault_client() {
  [[ -z "${ANSIBLE_VAULT_PASSWORD:-}" ]] && return 0
  ( umask 077; printf '%s' "$ANSIBLE_VAULT_PASSWORD" > "$VAULT_PASS_FILE" )
  chmod 600 "$VAULT_PASS_FILE"
  echo ">> Vault password file generated: $VAULT_PASS_FILE (mounted at $VAULT_PASS_CONTAINER)"
}

# Resolve the clone token (optional). Source: INVENTORY_TOKEN env (populated by
# load_secrets, or set by the caller) > none (anonymous). Sets CLONE_TOKEN and
# AUTH_SOURCE (env|anonymous). The token VALUE is never echoed; only its source.
resolve_token() {
  CLONE_TOKEN=""
  AUTH_SOURCE="anonymous"
  if [[ -n "${INVENTORY_TOKEN:-}" ]]; then
    CLONE_TOKEN="$INVENTORY_TOKEN"
    AUTH_SOURCE="env"
  fi
}

# Human-readable auth label for the summary (never the token value).
auth_label() {
  case "$AUTH_SOURCE" in
    env)  echo "token (env)" ;;
    *)    echo "anonymous" ;;
  esac
}

# ── EXIT traps: clone cleanup (subshell-local) + terminal marker/sidecar ─────
# main() runs `run_stages 2>&1 | tee "$LOG_FILE"` — the LEFT side of a pipe runs
# in a forked subshell. On this bash, a subshell does NOT inherit/fire a trap
# armed in its parent BEFORE the fork; it only fires a trap it explicitly arms
# ITSELF. So two separate traps are needed:
#
#   * clone_cleanup — removes $CLONE_DIR. Armed inside clone_inventory() (which
#     runs INSIDE the run_stages subshell, once CLONE_DIR is known), so it fires
#     when THAT subshell exits and actually has CLONE_DIR in scope. run_debug's
#     `trap - EXIT` disarms this (same shell) to intentionally keep the clone
#     dir for manual inspection.
#   * cleanup — the terminal marker (=== EXIT N === appended to $LOG_FILE) and,
#     when RUN_ID is set, the <run_id>.exit sidecar. Armed once in main(), right
#     after resolve_log_file, so it fires in the OUTER shell after the pipe
#     completes — the single place that should write the marker, regardless of
#     which stage (clone, secret, inventory, ansible) failed. _MARKER_WRITTEN
#     guards against this same trap firing more than once in one shell.
clone_cleanup() {
  rm -rf "${CLONE_DIR:-}" || true
}

_MARKER_WRITTEN=0
cleanup() {
  local code="$?"
  # Marker/sidecar are a "normal run" concept only (matches pre-existing
  # behaviour: only run_normal ever wrote them). debug mode intentionally keeps
  # the container + clone dir for manual inspection, and dry-run does no real
  # work — neither should report a terminal exit code via the log/sidecar.
  if [[ "$_MARKER_WRITTEN" -eq 0 && -n "$LOG_FILE" && "$MODE" == "normal" ]]; then
    _MARKER_WRITTEN=1
    echo "=== EXIT $code ===" >> "$LOG_FILE" || true
    if [[ -n "$RUN_ID" ]]; then
      local exit_file="$LOG_DIR/$RUN_ID.exit"
      printf '%s\n' "$code" > "$exit_file.tmp" && mv -f "$exit_file.tmp" "$exit_file"
    fi
  fi
}

clone_inventory() {
  # Debug mode may start an idle container with no inventory at all (nothing to
  # clone or mount). Signal that with an empty CLONE_DIR and skip the clone.
  if [[ -z "$INVENTORY" ]]; then
    CLONE_DIR=""
    return 0
  fi
  # DooD: the clone dir is bind-mounted into the ansible container, and -v
  # resolves on the HOST daemon. So clone beside this script (host-consistent),
  # NOT control_node's private /tmp. Override with CLONE_PARENT if needed.
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  CLONE_PARENT="${CLONE_PARENT:-$SCRIPT_DIR/.run-tmp}"
  mkdir -p "$CLONE_PARENT"
  CLONE_DIR="$(mktemp -d "$CLONE_PARENT/ansible-inventory.XXXXXX")"
  # Arm clone_cleanup HERE (not just in main): clone_inventory runs inside the
  # run_stages subshell forked by `| tee` in main(), and this subshell does not
  # fire a trap armed only in its parent — it must arm its own to actually
  # remove CLONE_DIR when it exits. run_debug's `trap - EXIT` disarms this same
  # trap (same shell) to intentionally keep the clone dir for inspection.
  trap clone_cleanup EXIT

  echo ">> Cloning inventory (ref: $INVENTORY_REF) from $INVENTORY_REPO into $CLONE_DIR"
  echo ">> Auth: $AUTH_SOURCE"

  if [[ -n "$CLONE_TOKEN" ]]; then
    # Authenticate WITHOUT leaking the token and WITHOUT an askpass helper: the
    # credential rides in as an HTTP Authorization header injected via git's
    # env-based config (GIT_CONFIG_* — git >= 2.31). The token therefore lives
    # only in this git process's ENVIRONMENT (base64-encoded), never in:
    #   * argv        → invisible to `ps`
    #   * the URL      → the clone uses the bare repo URL, so no credential is
    #                    written to the clone's .git/config (which is later
    #                    bind-mounted read-only into the ansible container)
    #   * any log/summary output
    # Basic auth is "<username>:<token>". The username matters by token type:
    #   * GitLab PAT          → username is ignored (convention: oauth2)
    #   * GitLab deploy token → authenticates as the token's OWN username
    # So the username is configurable via INVENTORY_TOKEN_USER (set it in the
    # secret file next to INVENTORY_TOKEN); it defaults to oauth2 for PATs.
    local auth_user="${INVENTORY_TOKEN_USER:-oauth2}"
    local auth_b64
    auth_b64="$(printf '%s:%s' "$auth_user" "$CLONE_TOKEN" | base64 | tr -d '\n')"
    GIT_CONFIG_COUNT=1 \
      GIT_CONFIG_KEY_0="http.extraHeader" \
      GIT_CONFIG_VALUE_0="Authorization: Basic $auth_b64" \
      GIT_ASKPASS="" SSH_ASKPASS="" GIT_TERMINAL_PROMPT=0 \
      git clone --depth 1 --branch "$INVENTORY_REF" "$INVENTORY_REPO" "$CLONE_DIR"
  else
    # Anonymous clone. Neutralize any inherited GIT_ASKPASS/SSH_ASKPASS from the
    # caller's environment (e.g. an editor or CI) and disable interactive prompts
    # so a private repo fails fast instead of hanging or using stray credentials.
    GIT_ASKPASS="" SSH_ASKPASS="" GIT_TERMINAL_PROMPT=0 \
      git clone --depth 1 --branch "$INVENTORY_REF" "$INVENTORY_REPO" "$CLONE_DIR"
  fi

  # Reject path traversal so the relative path can't escape the cloned repo.
  case "$INVENTORY" in
    /*|*..*) echo "Error: --inventory must be a relative path inside the repo." >&2; exit 2 ;;
  esac
  if [[ ! -f "$CLONE_DIR/$INVENTORY" ]]; then
    echo "Error: inventory file not found in repo: $INVENTORY" >&2
    echo "Available inventory files:" >&2
    find "$CLONE_DIR" -name '*.ini' -o -name '*.yml' -path '*inventor*' 2>/dev/null | sed "s#$CLONE_DIR/#  #" >&2 || true
    exit 2
  fi
  echo ">> Inventory resolved: /inventory/$INVENTORY"
}

# ── Build the ansible command (discrete args, no eval) ───────────────────────
# Produces three things:
#   ANSIBLE_ARGS     — the human-readable ansible-playbook invocation (summary).
#   CMD_ARGS         — what the container actually runs. Identical to ANSIBLE_ARGS
#                      unless a vault password is present, in which case it is
#                      wrapped so the password is echoed to ansible via a pipe.
#   SECRET_ENV_ARGS  — `docker run` env flags for secrets (value-less `-e NAME`,
#                      so the value is copied from this script's env, never argv).
#   VAULT_MOUNT_ARGS — `docker run -v` flags mounting the auto-generated vault
#                      client into the container (empty when no vault password).
build_cmd_args() {
  # Debug mode may run with no playbook/inventory (idle container); there is no
  # ansible command to build then — only the secret env/mount flags still matter.
  ANSIBLE_ARGS=()
  if [[ -n "$PLAYBOOK" && -n "$INVENTORY" ]]; then
    ANSIBLE_ARGS=(ansible-playbook -i "/inventory/$INVENTORY" "/playbooks/$PLAYBOOK")
    [[ -n "$TAGS"       ]] && ANSIBLE_ARGS+=(--tags "$TAGS")
    [[ -n "$LIMIT"      ]] && ANSIBLE_ARGS+=(--limit "$LIMIT")
    [[ -n "$EXTRA_VARS" ]] && ANSIBLE_ARGS+=(--extra-vars "$EXTRA_VARS")
  fi

  # Vault: mount the auto-generated password file and point
  # ANSIBLE_VAULT_PASSWORD_FILE at it. ansible reads the mounted file directly
  # and decrypts automatically — no per-command flags, and it works identically
  # for a manual `ansible-playbook` inside a debug container. Only the FILE PATH
  # (not the password) ever appears in argv.
  SECRET_ENV_ARGS=()
  VAULT_MOUNT_ARGS=()
  if [[ -n "${ANSIBLE_VAULT_PASSWORD:-}" ]]; then
    if [[ ! -f "$VAULT_PASS_FILE" ]]; then
      echo "Error: vault password file not found at $VAULT_PASS_FILE (should have been auto-generated)." >&2
      exit 2
    fi
    SECRET_ENV_ARGS=(-e "ANSIBLE_VAULT_PASSWORD_FILE=$VAULT_PASS_CONTAINER")
    VAULT_MOUNT_ARGS=(-v "$VAULT_PASS_FILE":"$VAULT_PASS_CONTAINER":ro)
  fi
  CMD_ARGS=(${ANSIBLE_ARGS[@]+"${ANSIBLE_ARGS[@]}"})
  return 0
}

# ── Single source of truth for `docker run` flags ────────────────────────────
# Assemble the mounts + env shared by BOTH the normal and debug runs. Only the
# mode-specific bits differ and stay in the callers: normal uses `--rm` + the
# ansible command + a `tee` pipe; debug uses `-d --name` + `sleep infinity`.
# Change a mount or env var HERE and all three call sites (normal, debug,
# print_docker_run) update together.
build_docker_base_args() {
  DOCKER_BASE_ARGS=(--add-host host.docker.internal:host-gateway)
  # /inventory only when an inventory was cloned (a bare --debug container has none).
  [[ -n "$CLONE_DIR" ]] && DOCKER_BASE_ARGS+=(-v "$CLONE_DIR":/inventory:ro)
  DOCKER_BASE_ARGS+=(-v "$SSH_KEY":/root/.ssh/id_key:ro)
  DOCKER_BASE_ARGS+=(${VAULT_MOUNT_ARGS[@]+"${VAULT_MOUNT_ARGS[@]}"})
  DOCKER_BASE_ARGS+=(-e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key
                     -e ANSIBLE_COLLECTIONS_PATH=/collections)
  DOCKER_BASE_ARGS+=(${SECRET_ENV_ARGS[@]+"${SECRET_ENV_ARGS[@]}"})
}

# ── Logging: human-readable run summary + the exact docker command ───────────
print_summary() {
  cat <<EOF
══════════════════ RUN SUMMARY ══════════════════
  Script version : $SCRIPT_VERSION
  Inventory repo : $([[ -n "$INVENTORY" ]] && echo "$INVENTORY_REPO" || echo "(none — debug)")
  Inventory ref  : $INVENTORY_REF
  Auth           : $(auth_label)
  Clone dir      : $([[ -n "$CLONE_DIR" ]] && echo "$CLONE_DIR" || echo "(none)")
  Inventory file : $([[ -n "$INVENTORY" ]] && echo "/inventory/$INVENTORY" || echo "(none)")
  Playbook       : $([[ -n "$PLAYBOOK" ]] && echo "/playbooks/$PLAYBOOK" || echo "(none)")
  Image          : $IMAGE
  SSH key        : $SSH_KEY
  Ansible cmd    : $([[ ${#ANSIBLE_ARGS[@]} -gt 0 ]] && echo "${ANSIBLE_ARGS[*]}" || echo "(none — idle container)")
  Vault          : $([[ -n "${ANSIBLE_VAULT_PASSWORD:-}" ]] && echo "ANSIBLE_VAULT_PASSWORD_FILE=$VAULT_PASS_CONTAINER (file auto-generated + mounted)" || echo "none")
  Log file       : $LOG_FILE
══════════════════════════════════════════════════
EOF
}

print_docker_run() {
  echo ">> docker run command:"
  echo "   docker run --rm ${DOCKER_BASE_ARGS[*]} $IMAGE ${CMD_ARGS[*]}"
}

# ── Dry-run: clone + print everything, but never pull or run docker ──────────
# Distinct from DRYRUN=1 (which exits before clone). The clone dir is still
# removed by the EXIT trap armed in clone_inventory.
run_dry_run() {
  print_summary
  print_docker_run
  echo ">> --dry-run: skipping docker pull and docker run."
  exit 0
}

# ── Debug: start an idle container for manual `docker exec` poking ───────────
# No --rm (container is kept), trap disarmed (clone dir is kept) — both are
# needed so the operator can exec in and inspect /inventory and networking.
run_debug() {
  if [[ -n "$RUN_ID" ]]; then
    DEBUG_CONTAINER="ansible-debug-$RUN_ID"
  elif [[ -n "$CLONE_DIR" ]]; then
    DEBUG_CONTAINER="ansible-debug-$(basename "$CLONE_DIR" | sed 's/^ansible-inventory\.//')"
  else
    # No run-id and no inventory clone — name it by PID so it stays unique.
    DEBUG_CONTAINER="ansible-debug-$$"
  fi

  print_summary

  if [[ "${SKIP_SSH_KEY_CHECK:-0}" != "1" && ! -f "$SSH_KEY" ]]; then
    echo "Error: ssh key not found: $SSH_KEY" >&2
    exit 2
  fi

  if [[ "$PULL" -eq 1 ]]; then
    echo ">> Pulling latest image: $IMAGE"
    docker pull "$IMAGE"
  fi

  # Keep the clone dir (if any) alive for the running container.
  trap - EXIT

  # Same mounts/env as a normal run (DOCKER_BASE_ARGS); debug just swaps --rm +
  # ansible command for -d/--name + `sleep infinity`.
  docker run -d --name "$DEBUG_CONTAINER" \
    "${DOCKER_BASE_ARGS[@]}" \
    "$IMAGE" \
    sleep infinity

  local manual
  if [[ -n "$INVENTORY" && -n "$PLAYBOOK" ]]; then
    manual="ansible-playbook -i /inventory/$INVENTORY /playbooks/$PLAYBOOK"
    [[ -n "$TAGS"  ]] && manual="$manual --tags $TAGS"
    [[ -n "$LIMIT" ]] && manual="$manual --limit $LIMIT"
    # No vault flag needed: ANSIBLE_VAULT_PASSWORD_FILE is already set in the
    # container env and the client is mounted, so ansible decrypts automatically.
  else
    manual="# no inventory mounted; test connectivity ad-hoc, e.g.:
  ansible all -i <your-inventory> -m ping"
  fi

  local cleanup_hint="  docker rm -f $DEBUG_CONTAINER"
  [[ -n "$CLONE_DIR" ]] && cleanup_hint="$cleanup_hint
  rm -rf $CLONE_DIR"

  cat <<EOF
══════════════ DEBUG MODE ══════════════
Container '$DEBUG_CONTAINER' is running (sleep infinity).

Enter it:
  docker exec -it $DEBUG_CONTAINER bash

Run the playbook manually inside:
  $manual

When done, clean up:
$cleanup_hint
══════════════════════════════════════════
EOF
  exit 0
}

# ── Normal run: docker run + tee + EXIT marker + sidecar + re-exit ───────────
run_normal() {
  if [[ "$PULL" -eq 1 ]]; then
    echo ">> Pulling latest image: $IMAGE"
    docker pull "$IMAGE"
  fi

  print_summary
  print_docker_run

  echo ">> Running: ${CMD_ARGS[*]}"
  echo ">> Logs:    $LOG_FILE (tee'd from stdout)"

  # SSH key is only consumed by docker run; validate here (after DRYRUN/arg/
  # inventory checks) so dry-run and unit tests don't require a real key.
  # SKIP_SSH_KEY_CHECK=1 lets fake-docker tests run without one.
  if [[ "${SKIP_SSH_KEY_CHECK:-0}" != "1" && ! -f "$SSH_KEY" ]]; then
    echo "Error: ssh key not found: $SSH_KEY" >&2
    exit 2
  fi

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
}

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
  trap cleanup EXIT     # outer-shell EXIT trap: writes the terminal marker/sidecar
  # Tee the entire run (clone/secret/inventory/ansible) to the per-run log, and
  # re-exit run_stages' real code (PIPESTATUS[0], NOT tee's).
  run_stages 2>&1 | tee "$LOG_FILE"
  exit "${PIPESTATUS[0]}"
}

main "$@"
