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
