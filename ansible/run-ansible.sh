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
SCRIPT_VERSION="1.0.0"

# ── Defaults ─────────────────────────────────────────────────────────────────
PLAYBOOK=""
INVENTORY=""               # path RELATIVE to the inventory repo root
INVENTORY_REF="main"       # branch/tag of the inventory repo to clone
TOKEN_FILE=""              # --token-file <path>: read clone token from a file
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

usage() {
  cat <<'EOF'
Usage: run-ansible.sh --playbook <file> --inventory <repo-relative-path> [options]

Required:
  --playbook <file>       Playbook filename under playbooks/ (e.g. ping.yml)
  --inventory <path>      Inventory path RELATIVE to the inventory repo root
                          (e.g. taipei/multinode.ini)

Options:
  --inventory-ref <ref>   Branch/tag of the inventory repo to clone (default: main)
  --inventory-repo-name <name>  Inventory repo under the fixed namespace to clone
                          (default: my-ansible-inventory; e.g. ansible-inventory-v2)
  --token-file <path>     Read the clone auth token from this file. Token may also
                          come from the INVENTORY_TOKEN env var. The token is NEVER
                          accepted as a CLI value, never logged, and never placed in
                          the clone URL (passed to git via GIT_ASKPASS only).
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
                          manual `docker exec` debugging; do NOT run ansible
  -h, --help              Show this help

The inventory repo is cloned fresh each run (under the fixed namespace
https://gitlab.com/ShannonHung) and removed afterward. Select it with
--inventory-repo-name; private repos authenticate via INVENTORY_TOKEN or
--token-file.

Example:
  ./run-ansible.sh --playbook ping.yml --inventory taipei/multinode.ini --limit node1
  ./run-ansible.sh -d --playbook ping.yml --inventory taipei/multinode.ini --limit node1
  INVENTORY_TOKEN=glpat-xxx ./run-ansible.sh --inventory-repo-name ansible-inventory-v2 \
      --playbook ping.yml --inventory taipei/multinode.ini
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
    if (( A[i] > B[i] )); then return 0; fi
    if (( A[i] < B[i] )); then return 1; fi
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
      --token-file)          TOKEN_FILE="$2"; shift 2 ;;
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

  if [[ -z "$PLAYBOOK" || -z "$INVENTORY" ]]; then
    echo "Error: --playbook and --inventory are required." >&2
    usage
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

  # --token-file must point at a readable file if given (the token VALUE is read
  # later, in resolve_token, so failures here don't depend on docker/git).
  if [[ -n "$TOKEN_FILE" && ! -f "$TOKEN_FILE" ]]; then
    echo "Error: --token-file not found: $TOKEN_FILE" >&2
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

# Resolve the clone token (optional). Precedence: --token-file > INVENTORY_TOKEN
# env > none (anonymous). Sets CLONE_TOKEN and AUTH_SOURCE (file|env|anonymous).
# The token VALUE is never echoed; only its source is reported.
resolve_token() {
  CLONE_TOKEN=""
  AUTH_SOURCE="anonymous"
  if [[ -n "$TOKEN_FILE" ]]; then
    # Strip a single trailing newline; keep the rest verbatim.
    CLONE_TOKEN="$(cat "$TOKEN_FILE")"
    AUTH_SOURCE="file"
  elif [[ -n "${INVENTORY_TOKEN:-}" ]]; then
    CLONE_TOKEN="$INVENTORY_TOKEN"
    AUTH_SOURCE="env"
  fi
}

# Human-readable auth label for the summary (never the token value).
auth_label() {
  case "$AUTH_SOURCE" in
    file) echo "token (file)" ;;
    env)  echo "token (env)" ;;
    *)    echo "anonymous" ;;
  esac
}

# ── Fresh inventory clone (deleted on exit, always latest) ───────────────────
# cleanup removes the clone dir AND the askpass helper (if one was created).
cleanup() {
  rm -rf "$CLONE_DIR"
  # Guard with `|| true`: a false [[ -n ... ]] would otherwise become the trap's
  # exit status and clobber the script's real exit code (the EXIT trap runs last).
  [[ -n "${ASKPASS_HELPER:-}" ]] && rm -f "$ASKPASS_HELPER" || true
}

clone_inventory() {
  # DooD: the clone dir is bind-mounted into the ansible container, and -v
  # resolves on the HOST daemon. So clone beside this script (host-consistent),
  # NOT control_node's private /tmp. Override with CLONE_PARENT if needed.
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  CLONE_PARENT="${CLONE_PARENT:-$SCRIPT_DIR/.run-tmp}"
  mkdir -p "$CLONE_PARENT"
  CLONE_DIR="$(mktemp -d "$CLONE_PARENT/ansible-inventory.XXXXXX")"
  trap cleanup EXIT

  echo ">> Cloning inventory (ref: $INVENTORY_REF) from $INVENTORY_REPO into $CLONE_DIR"
  echo ">> Auth: $AUTH_SOURCE"

  if [[ -n "$CLONE_TOKEN" ]]; then
    # Authenticate WITHOUT leaking the token: a GIT_ASKPASS helper supplies it on
    # demand. The token reaches git only via the helper's stdout — never in argv
    # (ps), the URL, git config, or any log. The helper reads the token from its
    # own env (exported solely for this git call), so the token is not baked into
    # the helper file's contents either. The URL carries only the "oauth2"
    # username; for a GitLab PAT the username is ignored and the token (as
    # password) is what authenticates.
    ASKPASS_HELPER="$(mktemp "$CLONE_PARENT/askpass.XXXXXX")"
    chmod 600 "$ASKPASS_HELPER"
    printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" "$INVENTORY_GIT_TOKEN"' \
      > "$ASKPASS_HELPER"
    chmod 700 "$ASKPASS_HELPER"
    local auth_url="${INVENTORY_REPO/https:\/\//https://oauth2@}"
    GIT_ASKPASS="$ASKPASS_HELPER" GIT_TERMINAL_PROMPT=0 \
      INVENTORY_GIT_TOKEN="$CLONE_TOKEN" \
      git clone --depth 1 --branch "$INVENTORY_REF" "$auth_url" "$CLONE_DIR"
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
build_cmd_args() {
  CMD_ARGS=(ansible-playbook -i "/inventory/$INVENTORY" "/playbooks/$PLAYBOOK")
  [[ -n "$TAGS"       ]] && CMD_ARGS+=(--tags "$TAGS")
  [[ -n "$LIMIT"      ]] && CMD_ARGS+=(--limit "$LIMIT")
  [[ -n "$EXTRA_VARS" ]] && CMD_ARGS+=(--extra-vars "$EXTRA_VARS")
  return 0
}

# ── Logging: human-readable run summary + the exact docker command ───────────
print_summary() {
  cat <<EOF
══════════════════ RUN SUMMARY ══════════════════
  Script version : $SCRIPT_VERSION
  Inventory repo : $INVENTORY_REPO
  Inventory ref  : $INVENTORY_REF
  Auth           : $(auth_label)
  Clone dir      : $CLONE_DIR
  Inventory file : /inventory/$INVENTORY
  Playbook       : /playbooks/$PLAYBOOK
  Image          : $IMAGE
  SSH key        : $SSH_KEY
  Ansible cmd    : ${CMD_ARGS[*]}
  Log file       : $LOG_FILE
══════════════════════════════════════════════════
EOF
}

print_docker_run() {
  cat <<EOF
>> docker run command:
   docker run --rm \\
     --add-host host.docker.internal:host-gateway \\
     -v $CLONE_DIR:/inventory:ro \\
     -v $SSH_KEY:/root/.ssh/id_key:ro \\
     -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \\
     -e ANSIBLE_COLLECTIONS_PATH=/collections \\
     $IMAGE \\
     ${CMD_ARGS[*]}
EOF
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
  else
    DEBUG_CONTAINER="ansible-debug-$(basename "$CLONE_DIR" | sed 's/^ansible-inventory\.//')"
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

  # Keep the clone dir alive for the running container.
  trap - EXIT

  docker run -d --name "$DEBUG_CONTAINER" \
    --add-host host.docker.internal:host-gateway \
    -v "$CLONE_DIR":/inventory:ro \
    -v "$SSH_KEY":/root/.ssh/id_key:ro \
    -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \
    -e ANSIBLE_COLLECTIONS_PATH=/collections \
    "$IMAGE" \
    sleep infinity

  local manual="ansible-playbook -i /inventory/$INVENTORY /playbooks/$PLAYBOOK"
  [[ -n "$TAGS"  ]] && manual="$manual --tags $TAGS"
  [[ -n "$LIMIT" ]] && manual="$manual --limit $LIMIT"

  cat <<EOF
══════════════ DEBUG MODE ══════════════
Container '$DEBUG_CONTAINER' is running (sleep infinity).

Enter it:
  docker exec -it $DEBUG_CONTAINER bash

Run the playbook manually inside:
  $manual

When done, clean up:
  docker rm -f $DEBUG_CONTAINER
  rm -rf $CLONE_DIR
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
  set +e
  docker run --rm \
    --add-host host.docker.internal:host-gateway \
    -v "$CLONE_DIR":/inventory:ro \
    -v "$SSH_KEY":/root/.ssh/id_key:ro \
    -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \
    -e ANSIBLE_COLLECTIONS_PATH=/collections \
    "$IMAGE" \
    "${CMD_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
  RUN_EXIT="${PIPESTATUS[0]}"
  set -e

  echo "=== EXIT $RUN_EXIT ===" >> "$LOG_FILE"

  if [[ -n "$RUN_ID" ]]; then
    EXIT_FILE="$LOG_DIR/$RUN_ID.exit"
    printf '%s\n' "$RUN_EXIT" > "$EXIT_FILE.tmp" && mv -f "$EXIT_FILE.tmp" "$EXIT_FILE"
  fi

  exit "$RUN_EXIT"
}

main() {
  parse_args "$@"
  resolve_inventory_repo
  resolve_token
  resolve_log_file
  clone_inventory
  build_cmd_args
  case "$MODE" in
    debug)   run_debug ;;
    dry-run) run_dry_run ;;
    *)       run_normal ;;
  esac
}

main "$@"
