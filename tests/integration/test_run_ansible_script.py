import base64
import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ansible" / "run-ansible.sh"


def _run(tmp_path, *extra):
    return subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path), *extra],
        capture_output=True, text=True,
        env={**os.environ, "DRYRUN": "1"},
    )


def test_bad_run_id_rejected(tmp_path):
    res = _run(tmp_path, "--run-id", "../evil")
    assert res.returncode == 2
    assert "run-id" in (res.stderr + res.stdout).lower()


def test_run_id_sets_log_filename(tmp_path):
    res = _run(tmp_path, "--run-id", "abc-123")
    assert res.returncode == 0, res.stderr
    assert str(tmp_path / "abc-123.log") in res.stdout


def test_bad_retention_rejected(tmp_path):
    res = _run(tmp_path, "--run-id", "ok", "--log-retention-days", "abc")
    assert res.returncode == 2
    assert "retention" in (res.stderr + res.stdout).lower()


def test_self_cleaning_prunes_old_logs(tmp_path):
    old = tmp_path / "old.log"
    fresh = tmp_path / "fresh.log"
    old.write_text("x")
    fresh.write_text("y")
    # Backdate old.log to 5 days ago (default retention is 3 → it must go).
    five_days_ago = time.time() - 5 * 86400
    os.utime(old, (five_days_ago, five_days_ago))

    res = _run(tmp_path, "--run-id", "run9")
    assert res.returncode == 0, res.stderr
    assert not old.exists(), "5-day-old log should be pruned at default retention 3"
    assert fresh.exists(), "fresh log must be kept"


def test_retention_zero_disables_cleanup(tmp_path):
    old = tmp_path / "old.log"
    old.write_text("x")
    five_days_ago = time.time() - 5 * 86400
    os.utime(old, (five_days_ago, five_days_ago))

    res = _run(tmp_path, "--run-id", "run9", "--log-retention-days", "0")
    assert res.returncode == 0, res.stderr
    assert old.exists(), "retention 0 must disable cleanup"


# ── Terminal marker (orphan-run recovery: log file as source of truth) ────────
#
# DRYRUN exits before docker, so the marker logic is exercised with a fake
# `docker` on PATH (and `git`, since the script clones before running). The
# script must, after the run, write the real ansible/docker exit code to:
#   * a sidecar  <log-dir>/<run-id>.exit   (machine-parsed by deploy-service)
#   * a final log line  "=== EXIT <code> ===" (human-visible in /view)

def _run_with_fake_docker(tmp_path, exit_code, *extra):
    """Run the script for real (no DRYRUN) but with fake git+docker on PATH so
    no network/daemon is touched. The fake docker exits with `exit_code` and
    prints a recognisable line first."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Fake git: make `git clone <repo> <dir>` create the inventory file the
    # script validates, so it proceeds to the docker step.
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'dest="${@: -1}"\n'
        'mkdir -p "$dest/taipei"\n'
        'printf "[all]\\nnode1\\n" > "$dest/taipei/multinode.ini"\n'
    )
    # Fake docker: print a marker line then exit with the requested code.
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'echo "FAKE ANSIBLE OUTPUT"\n'
        f"exit {exit_code}\n"
    )
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)
    # The fake docker never reads the SSH key, so skip the script's key-existence
    # guard — this test only exercises the log-marker / exit-code path.
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "SKIP_SSH_KEY_CHECK": "1",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path), *extra],
        capture_output=True, text=True, env=env,
    )


def test_marker_written_on_success(tmp_path):
    res = _run_with_fake_docker(tmp_path, 0, "--run-id", "ok-run")
    assert res.returncode == 0, res.stderr
    log = (tmp_path / "ok-run.log").read_text()
    assert "FAKE ANSIBLE OUTPUT" in log
    assert log.rstrip().endswith("=== EXIT 0 ===")
    assert (tmp_path / "ok-run.exit").read_text().strip() == "0"


def test_marker_written_on_failure_preserves_exit_code(tmp_path):
    # The script must EXIT with the real ansible code (so callers waiting on it
    # still see failure) AND record it in the marker/sidecar.
    res = _run_with_fake_docker(tmp_path, 2, "--run-id", "bad-run")
    assert res.returncode == 2, res.stderr
    log = (tmp_path / "bad-run.log").read_text()
    assert log.rstrip().endswith("=== EXIT 2 ===")
    assert (tmp_path / "bad-run.exit").read_text().strip() == "2"


def test_no_sidecar_without_run_id(tmp_path):
    # Standalone use (no --run-id) keeps run.log; the log marker is still added,
    # but no UUID sidecar is written (deploy-service is the only sidecar reader).
    res = _run_with_fake_docker(tmp_path, 0)
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "run.log").read_text().rstrip().endswith("=== EXIT 0 ===")
    assert not list(tmp_path.glob("*.exit"))


def test_summary_and_docker_run_logged(tmp_path):
    res = _run_with_fake_docker(tmp_path, 0, "--run-id", "sum1", "--limit", "node1")
    out = res.stdout
    assert res.returncode == 0, res.stderr
    assert "RUN SUMMARY" in out
    assert "Inventory repo" in out
    assert "Inventory resolved: /inventory/taipei/multinode.ini" in out
    # Full docker run command (mounts/env/add-host), not just the ansible part.
    assert "docker run" in out
    assert "host.docker.internal:host-gateway" in out
    assert "/inventory:ro" in out
    assert "ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key" in out


def test_image_tag_sets_image(tmp_path):
    res = _run_with_fake_docker(tmp_path, 0, "--run-id", "it1", "--image-tag", "v1.2")
    assert res.returncode == 0, res.stderr
    assert "shannonhung/ansible-runner:v1.2" in res.stdout

def test_image_and_image_tag_mutually_exclusive(tmp_path):
    res = _run(tmp_path, "--image", "foo/bar:1", "--image-tag", "v1.2")
    assert res.returncode == 2
    assert "mutually exclusive" in (res.stderr + res.stdout).lower()


def _run_with_failing_docker(tmp_path, *extra):
    """Fake git that creates the inventory, plus a fake docker that exits 99 and
    writes a sentinel file — so any docker invocation is detectable."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'dest="${@: -1}"\n'
        'mkdir -p "$dest/taipei"\n'
        'printf "[all]\\nnode1\\n" > "$dest/taipei/multinode.ini"\n'
    )
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'touch "{tmp_path}/docker_was_called"\n'
        "exit 99\n"
    )
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "SKIP_SSH_KEY_CHECK": "1"}
    return subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path), *extra],
        capture_output=True, text=True, env=env,
    )


def test_dry_run_prints_but_does_not_run_docker(tmp_path):
    res = _run_with_failing_docker(tmp_path, "--dry-run", "--run-id", "dr1")
    assert res.returncode == 0, res.stderr
    assert "RUN SUMMARY" in res.stdout
    assert "docker run" in res.stdout            # printed as text
    assert not (tmp_path / "docker_was_called").exists()  # never executed
    assert not (tmp_path / "dr1.exit").exists()   # nothing ran → no sidecar


def test_debug_starts_idle_container_and_keeps_clone(tmp_path):
    # Fake docker records its argv so we can assert `run -d ... sleep infinity`.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'dest="${@: -1}"\n'
        'mkdir -p "$dest/taipei"\n'
        'printf "[all]\\nnode1\\n" > "$dest/taipei/multinode.ini"\n'
    )
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{tmp_path}/docker_argv"\n'
        "exit 0\n"
    )
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "SKIP_SSH_KEY_CHECK": "1",
           # Keep the clone dir beside a known, inspectable parent.
           "CLONE_PARENT": str(tmp_path / "clones")}
    res = subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path),
         "--debug", "--run-id", "dbg1"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    argv = (tmp_path / "docker_argv").read_text()
    assert "run -d" in argv
    assert "sleep infinity" in argv
    assert "ansible-debug-dbg1" in argv
    # Guidance printed
    assert "docker exec -it ansible-debug-dbg1 bash" in res.stdout
    assert "ansible-playbook -i /inventory/taipei/multinode.ini" in res.stdout
    assert "docker rm -f ansible-debug-dbg1" in res.stdout
    # No sidecar; clone dir kept (not removed by trap)
    assert not (tmp_path / "dbg1.exit").exists()
    clones = list((tmp_path / "clones").glob("ansible-inventory.*"))
    assert clones, "debug mode must keep the clone dir"


def test_debug_and_dry_run_mutually_exclusive(tmp_path):
    res = _run(tmp_path, "--debug", "--dry-run")
    assert res.returncode == 2
    assert "debug" in (res.stderr + res.stdout).lower()


def test_debug_fails_fast_on_missing_ssh_key(tmp_path):
    """Debug mode must fail early with exit 2 when the SSH key is missing.

    Verifies the guard added to run_debug() mirrors the one in run_normal():
      if [[ "${SKIP_SSH_KEY_CHECK:-0}" != "1" && ! -f "$SSH_KEY" ]]; then
        echo "Error: ssh key not found: $SSH_KEY" >&2; exit 2
      fi
    SKIP_SSH_KEY_CHECK is intentionally NOT set so the guard fires.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Fake git: creates the inventory file so clone_inventory() passes.
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'dest="${@: -1}"\n'
        'mkdir -p "$dest/taipei"\n'
        'printf "[all]\\nnode1\\n" > "$dest/taipei/multinode.ini"\n'
    )
    # Fake docker: records whether it was ever called.
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'touch "{tmp_path}/docker_was_called"\n'
        "exit 0\n"
    )
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)

    # No SKIP_SSH_KEY_CHECK; point --ssh-key at a path that definitely doesn't exist.
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    res = subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--debug",
         "--ssh-key", "/nonexistent/key",
         "--log-dir", str(tmp_path)],
        capture_output=True, text=True, env=env,
    )

    assert res.returncode == 2, (
        f"Expected exit 2 (missing key guard), got {res.returncode}.\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    combined = res.stderr + res.stdout
    assert "ssh key not found" in combined.lower(), (
        f"Expected 'ssh key not found' in output. Got:\n{combined}"
    )
    # The container must NOT have been started.
    assert not (tmp_path / "docker_was_called").exists(), (
        "docker must not be called when the SSH key is missing in debug mode"
    )


# ── Inventory repo selection + token auth ────────────────────────────────────
#
# These use a fake git that records what URL it was asked to clone, the FULL
# argv it received, whether GIT_ASKPASS was set, and the auth header injected via
# git's env-based config (GIT_CONFIG_VALUE_0) — written to files in tmp_path.
# That lets us assert the resolved URL, prove the token never appears in
# argv/URL/output, and confirm it rides in only via the env-config header. The
# fake docker exits 0 so the run completes; we only care about the clone step.

def _run_with_recording_git(tmp_path, *extra, env_extra=None):
    """Fake git that records clone URL + argv + GIT_ASKPASS + the injected
    GIT_CONFIG auth header to files, then creates the inventory file so the
    script proceeds. Fake docker exits 0."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/git_argv"\n'
        # The script only ever invokes `git clone ... <url> <dest>`; the URL and
        # dest are the last two positional args.
        'dest="${@: -1}"\n'
        'url="${@: -2:1}"\n'
        f'printf "%s\\n" "$url" >> "{tmp_path}/git_url"\n'
        f'printf "ASKPASS=[%s]\\n" "${{GIT_ASKPASS:-}}" >> "{tmp_path}/git_askpass"\n'
        # Record the credential header the script injects via env-based config
        # (GIT_CONFIG_VALUE_0). The value is base64; the Python test decodes it
        # to prove the token reaches git ONLY here, never in argv/URL.
        f'printf "HEADER=[%s]\\n" "${{GIT_CONFIG_VALUE_0:-}}" >> "{tmp_path}/git_header"\n'
        'mkdir -p "$dest/taipei"\n'
        'printf "[all]\\nnode1\\n" > "$dest/taipei/multinode.ini"\n'
    )
    (bindir / "docker").write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "SKIP_SSH_KEY_CHECK": "1"}
    # The recording git ignores --branch, so unset INVENTORY_REPO to exercise the
    # repo-name → URL builder (otherwise the inherited env could override it).
    env.pop("INVENTORY_REPO", None)
    # A stray INVENTORY_TOKEN in the ambient env would turn "anonymous" tests
    # into authenticated ones; drop it so each test controls auth explicitly.
    env.pop("INVENTORY_TOKEN", None)
    if env_extra:
        env.update(env_extra)
    res = subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path), *extra],
        capture_output=True, text=True, env=env,
    )
    return res


def test_default_inventory_repo_name(tmp_path):
    res = _run_with_recording_git(tmp_path)
    assert res.returncode == 0, res.stderr
    url = (tmp_path / "git_url").read_text()
    assert "gitlab.com/ShannonHung/my-ansible-inventory.git" in url


def test_inventory_repo_name_selects_repo(tmp_path):
    res = _run_with_recording_git(tmp_path, "--inventory-repo-name",
                                  "ansible-inventory-v2")
    assert res.returncode == 0, res.stderr
    url = (tmp_path / "git_url").read_text()
    assert "gitlab.com/ShannonHung/ansible-inventory-v2.git" in url
    assert "ansible-inventory-v2" in res.stdout  # shown in summary


def test_inventory_repo_env_still_overrides(tmp_path):
    res = _run_with_recording_git(
        tmp_path, "--inventory-repo-name", "ansible-inventory-v1",
        env_extra={"INVENTORY_REPO": "https://example.com/custom/repo.git"})
    assert res.returncode == 0, res.stderr
    url = (tmp_path / "git_url").read_text()
    assert "example.com/custom/repo.git" in url
    # The repo-name builder must NOT win over the explicit env URL.
    assert "ShannonHung" not in url


def test_inventory_repo_name_rejects_traversal(tmp_path):
    res = _run_with_recording_git(tmp_path, "--inventory-repo-name", "../evil")
    assert res.returncode == 2
    assert "inventory-repo-name" in (res.stderr + res.stdout).lower()


def test_anonymous_clone_when_no_token(tmp_path):
    res = _run_with_recording_git(tmp_path)
    assert res.returncode == 0, res.stderr
    askpass = (tmp_path / "git_askpass").read_text()
    # No token → GIT_ASKPASS must be empty (anonymous clone).
    assert "ASKPASS=[]" in askpass
    # URL stays a bare https URL with no embedded credentials.
    url = (tmp_path / "git_url").read_text()
    assert "@" not in url
    assert "Auth           : anonymous" in res.stdout


def _decode_header_token(tmp_path):
    """Decode the base64 in the recorded `Authorization: Basic ...` header."""
    raw = (tmp_path / "git_header").read_text().strip()
    # Format recorded by the fake git: HEADER=[Authorization: Basic <b64>]
    inner = raw[len("HEADER=["):-1] if raw.startswith("HEADER=[") else raw
    assert inner.startswith("Authorization: Basic "), raw
    b64 = inner[len("Authorization: Basic "):]
    return base64.b64decode(b64).decode()


def test_token_from_env_uses_config_header_and_does_not_leak(tmp_path):
    secret = "glpat-SUPERSECRETTOKEN123"
    res = _run_with_recording_git(tmp_path, env_extra={"INVENTORY_TOKEN": secret})
    assert res.returncode == 0, res.stderr
    # No askpass helper is used anymore.
    askpass = (tmp_path / "git_askpass").read_text()
    assert "ASKPASS=[]" in askpass
    # The token never appears in argv, the clone URL, or any printed output.
    argv = (tmp_path / "git_argv").read_text()
    url = (tmp_path / "git_url").read_text()
    assert secret not in argv, "token must never appear in git argv"
    assert secret not in url, "token must never appear in the clone URL"
    assert "@" not in url, "clone URL must stay credential-free (nothing in .git/config)"
    assert secret not in res.stdout and secret not in res.stderr, \
        "token must never be printed"
    # The token reaches git ONLY via the env-injected Basic auth header.
    assert _decode_header_token(tmp_path) == f"oauth2:{secret}"
    assert "Auth           : token (env)" in res.stdout


def test_secret_path_token_drives_clone_auth(tmp_path):
    secret = "glpat-FROMSECRETFILE456"
    sec = tmp_path / "secrets.env"
    sec.write_text(f"INVENTORY_TOKEN={secret}\n")
    os.chmod(sec, 0o600)
    res = _run_with_recording_git(tmp_path, "--secret-path", str(sec))
    assert res.returncode == 0, res.stderr
    # Sourced token authenticates the clone, still without leaking anywhere.
    assert secret not in (tmp_path / "git_argv").read_text()
    assert secret not in (tmp_path / "git_url").read_text()
    assert secret not in res.stdout and secret not in res.stderr
    assert _decode_header_token(tmp_path) == f"oauth2:{secret}"
    assert "Auth           : token (env)" in res.stdout


def test_secret_path_rejects_group_readable_file(tmp_path):
    sec = tmp_path / "secrets.env"
    sec.write_text("INVENTORY_TOKEN=glpat-whatever\n")
    os.chmod(sec, 0o644)  # group/other-readable → must be refused
    res = _run_with_recording_git(tmp_path, "--secret-path", str(sec))
    assert res.returncode == 2
    combined = (res.stderr + res.stdout).lower()
    assert "600" in combined or "group" in combined


def test_secret_path_missing_is_an_error(tmp_path):
    res = _run_with_recording_git(tmp_path, "--secret-path", "/nonexistent/secrets.env")
    assert res.returncode == 2
    assert "secret-path" in (res.stderr + res.stdout).lower()


def test_vault_password_passed_by_name_and_wrapped_for_stdin(tmp_path):
    """A sourced ANSIBLE_VAULT_PASSWORD is handed to the container by NAME only
    (value-less `-e`) and echoed into ansible via /dev/stdin — never in argv."""
    vault = "s3cr3t-vault-pw"
    sec = tmp_path / "secrets.env"
    sec.write_text(f"ANSIBLE_VAULT_PASSWORD={vault}\n")
    os.chmod(sec, 0o600)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'dest="${@: -1}"\n'
        'mkdir -p "$dest/taipei"\n'
        'printf "[all]\\nnode1\\n" > "$dest/taipei/multinode.ini"\n'
    )
    # Fake docker records its full argv so we can inspect the -e flag and command.
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/docker_argv"\n'
        "exit 0\n"
    )
    for f in ("git", "docker"):
        os.chmod(bindir / f, 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "SKIP_SSH_KEY_CHECK": "1"}
    env.pop("INVENTORY_REPO", None)
    res = subprocess.run(
        ["bash", str(SCRIPT), "--playbook", "ping.yml", "--inventory",
         "taipei/multinode.ini", "--no-pull", "--log-dir", str(tmp_path),
         "--run-id", "vault1", "--secret-path", str(sec)],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    argv = (tmp_path / "docker_argv").read_text()
    # Passed by name (value comes from the runner's env), never as a value.
    assert "-e ANSIBLE_VAULT_PASSWORD" in argv
    assert f"ANSIBLE_VAULT_PASSWORD={vault}" not in argv
    assert vault not in argv, "vault password must never appear in docker argv"
    # Wrapped so the container echoes it into ansible via stdin.
    assert "--vault-password-file /dev/stdin" in argv
    # And it never leaks into printed output.
    assert vault not in res.stdout and vault not in res.stderr


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
