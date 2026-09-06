import pytest

from app.core.version import parse_semver, version_ge, version_max


def test_parse_semver_valid():
    assert parse_semver("1.10.0") == (1, 10, 0)


@pytest.mark.parametrize("bad", ["1.2", "v1.0.0", "1.2.3.4", "1.2.x", "", "1.02.0"])
def test_parse_semver_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_semver(bad)


def test_version_ge_numeric_not_lexicographic():
    assert version_ge("1.10.0", "1.9.0") is True  # numeric: 10 > 9
    assert version_ge("1.9.0", "1.10.0") is False


def test_version_ge_equal():
    assert version_ge("1.2.3", "1.2.3") is True


def test_version_ge_strict_less():
    assert version_ge("1.0.0", "1.2.0") is False


def test_version_max_returns_greater_string():
    assert version_max("1.2.0", "1.5.0") == "1.5.0"
    assert version_max("1.5.0", "1.2.0") == "1.5.0"
    assert version_max("1.2.0", "1.2.0") == "1.2.0"
