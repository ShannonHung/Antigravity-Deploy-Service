#!/usr/bin/env bash
# Lint a single Python file that Claude Code just edited.
#
# Wired to the PostToolUse hook in .claude/settings.local.json. Kept as a script
# rather than an inline hook command so the same gate can be run by hand
# (`scripts/lint-changed.sh app/foo.py`) and from CI, with one definition.
#
# Scope is deliberately ONE file: linting the whole tree on every edit would
# block work on unrelated pre-existing violations. mypy uses
# --follow-imports=silent for the same reason — it still type-checks against
# imported modules, but only reports diagnostics for the file being edited.
set -uo pipefail

FILE="${1:-}"

# The hook fires for every Edit/Write. Anything that is not a Python file in
# this project (docs, JSON, another sub-project) is not ours to lint.
[[ -z "$FILE" ]] && exit 0
[[ "$FILE" == *.py ]] || exit 0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$FILE" in
  /*) ABS="$FILE" ;;
  *)  ABS="$PWD/$FILE" ;;
esac
[[ "$ABS" == "$REPO_ROOT"/* ]] || exit 0   # not under deploy-service/
[[ -f "$ABS" ]] || exit 0                  # deleted or moved

cd "$REPO_ROOT" || exit 0
REL="${ABS#"$REPO_ROOT"/}"

status=0
fail() { echo "❌ $1 failed on $REL" >&2; status=1; }

# black: formatting is mechanical, so fix it in place rather than nagging.
# --quiet keeps the hook silent when there was nothing to do.
uv run black --quiet "$REL" || fail black

# pylint: project-wide rule configuration lives in pyproject.toml under
# [tool.pylint], NOT here — so a manual run and this hook agree.
uv run pylint "$REL" || fail pylint

# mypy: --follow-imports=silent keeps the report scoped to this file while
# still using imported modules for inference.
uv run mypy --follow-imports=silent "$REL" || fail mypy

exit "$status"
