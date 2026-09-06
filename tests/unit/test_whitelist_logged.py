import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"


def _load(name):
    """Read a tracked whitelist template.

    The real data/allow-commands-*.json are untracked (machine-specific
    absolute paths), so the committed *.example.json templates are what
    these shape assertions can rely on in a fresh clone / CI.
    """
    return json.loads((DATA / name).read_text())


def test_admin_ansible_commands_are_logged():
    cfg = _load("allow-commands-admin.example.json")
    ansible = [
        c for c in cfg["allow_commands"] if c["command_name"].startswith("run_ansible")
    ]
    assert ansible, "expected run_ansible_* commands"
    for c in ansible:
        assert c.get("logged") is True, c["command_name"]
        flat = [tok for step in c["pipeline"] for tok in step["command"]]
        assert "--run-id" in flat and "{run_id}" in flat, c["command_name"]
        assert "--log-dir" in flat, c["command_name"]
