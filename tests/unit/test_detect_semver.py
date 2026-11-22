"""P2-10 (SPEC §9.4): the node-semver subset that `engines.node` uses, to pick a runtime."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mpilot.detect import semver
from mpilot.detect.semver import RangeSyntaxError, parse_version, satisfies


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("20.11.0", (20, 11, 0)),
        ("v18.20.8", (18, 20, 8)),
        (" 22.0.0 ", (22, 0, 0)),
        ("20.0.0-rc.1", (20, 0, 0)),
    ],
)
def test_versions_parse(version: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(version) == expected


@pytest.mark.parametrize("bad", ["", "20", "20.1", "a.b.c", "20.1.x", "1.2.3.4", "-1.0.0"])
def test_bad_versions_are_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="version"):
        parse_version(bad)


@pytest.mark.parametrize(
    ("range_", "version", "expected"),
    [
        # comparators
        (">=18", "18.0.0", True),
        (">=18", "17.9.9", False),
        (">=18.17.0", "18.20.8", True),
        (">=18.17.0", "18.16.0", False),
        (">18", "18.20.8", False),  # >18 means >=19.0.0
        (">18", "19.0.0", True),
        (">18.1.0", "18.1.1", True),
        ("<20", "19.9.9", True),
        ("<20", "20.0.0", False),
        ("<=20", "20.20.2", True),  # <=20 means <21.0.0
        ("<=20.1.0", "20.1.1", False),
        ("=20.1.0", "20.1.0", True),
        ("20.1.0", "20.1.1", False),
        ("v20.1.0", "20.1.0", True),
        # x-ranges and bare majors
        ("20", "20.20.2", True),
        ("20", "21.0.0", False),
        ("20.x", "20.5.0", True),
        ("20.*", "20.5.0", True),
        ("20.X.x", "21.0.0", False),
        ("20.3", "20.3.9", True),
        ("20.3", "20.4.0", False),
        ("*", "1.0.0", True),
        ("x", "99.0.0", True),
        ("", "1.2.3", True),
        # caret and tilde
        ("^18.17.0", "18.20.8", True),
        ("^18.17.0", "19.0.0", False),
        ("^18.17.0", "18.16.9", False),
        ("^0.2.3", "0.2.9", True),
        ("^0.2.3", "0.3.0", False),
        ("^0.0.3", "0.0.4", False),
        ("^18", "18.99.0", True),
        ("^18", "19.0.0", False),
        ("~18.17.0", "18.17.9", True),
        ("~18.17.0", "18.18.0", False),
        ("~18", "18.9.0", True),
        ("~18", "19.0.0", False),
        # hyphen ranges
        ("16 - 20", "20.20.2", True),
        ("16 - 20", "21.0.0", False),
        ("16.3.0 - 18.1.0", "18.1.1", False),
        ("16.3.0 - 18.1.0", "16.3.0", True),
        # AND (space or comma) and OR
        (">=18 <21", "20.20.2", True),
        (">=18 <21", "21.0.0", False),
        (">=18, <21", "19.0.0", True),
        ("^18 || ^20", "20.20.2", True),
        ("^18 || ^20", "19.0.0", False),
        ("^18 || ^20 || ^22", "22.23.5", True),
        (">=14.0.0 || >=16.0.0 <18", "17.0.0", True),
        # whitespace after the operator
        (">= 18.17.0", "18.20.8", True),
        ("< 20", "19.1.1", True),
        ("^ 18", "18.1.1", True),
    ],
)
def test_ranges(range_: str, version: str, expected: bool) -> None:
    assert satisfies(version, range_) is expected


@pytest.mark.parametrize(
    "bad",
    [
        "lts/*",
        "latest",
        "node",
        ">=",
        "^",
        "1.2.3.4",
        ">=a",
        "20 -",
        "- 20",
        "||",
        "^18 ||",
        "~>1",
        "20.1.x.2",
        "<>20",
        ">=18 <",
        "18 - 20 - 22",
    ],
)
def test_unsupported_ranges_raise_instead_of_guessing(bad: str) -> None:
    with pytest.raises(RangeSyntaxError):
        satisfies("20.0.0", bad)


def test_prerelease_ranges_are_not_supported_and_say_so() -> None:
    with pytest.raises(RangeSyntaxError, match="pre-release"):
        satisfies("20.0.0", ">=20.0.0-rc.1")


def test_the_empty_or_star_range_matches_everything() -> None:
    for r in ("", "*", "x", "X", "  "):
        assert satisfies("0.0.1", r)


def test_the_error_is_a_value_error() -> None:
    assert issubclass(RangeSyntaxError, ValueError)


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=40))
def test_random_text_never_crashes_only_raises_range_syntax_error(text: str) -> None:
    try:
        result = satisfies("20.1.0", text)
    except RangeSyntaxError:
        return
    assert isinstance(result, bool)


@settings(max_examples=300, deadline=None)
@given(
    st.integers(0, 30),
    st.integers(0, 30),
    st.integers(0, 30),
)
def test_caret_and_tilde_always_include_their_own_version(
    major: int, minor: int, patch: int
) -> None:
    version = f"{major}.{minor}.{patch}"
    assert satisfies(version, f"^{version}")
    assert satisfies(version, f"~{version}")
    assert satisfies(version, f">={version}")
    assert satisfies(version, f"<={version}")
    assert satisfies(version, f"={version}")
    assert not satisfies(version, f">{version}")
    assert not satisfies(version, f"<{version}")


def test_the_module_exports_what_the_detector_uses() -> None:
    assert {"satisfies", "parse_version", "RangeSyntaxError"} <= set(dir(semver))
