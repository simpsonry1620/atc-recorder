import pytest

from atc_recorder.utils import parse_duration, sanitize_filename


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30m", 1800),
        ("2h", 7200),
        ("1h30m", 5400),
        ("90", 90),
        ("45s", 45),
    ],
)
def test_parse_duration_valid_values(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "minute"])
def test_parse_duration_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


def test_sanitize_filename_replaces_forbidden_characters():
    assert sanitize_filename('foo:/bar*?<>|"') == "foo__bar______"
