import pytest

from app.core.exceptions import CommandExecutionException
from app.repositories.host_resolver import cluster_type_from_name

SLASH_MAP = {"no_slash": "type1", "with_slash": "type2"}


def test_no_slash_selects_no_slash_type():
    assert cluster_type_from_name("taiwan-taipei-my-cluster", SLASH_MAP) == (
        "type1",
        False,
    )


def test_slash_selects_with_slash_type():
    assert cluster_type_from_name("taiwan-taipei/my-cluster", SLASH_MAP) == (
        "type2",
        True,
    )


def test_missing_key_raises():
    with pytest.raises(CommandExecutionException) as exc:
        cluster_type_from_name("a/b", {"no_slash": "type1"})
    assert "with_slash" in str(exc.value)
