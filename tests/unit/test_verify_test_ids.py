"""P2-14a (SPEC FR-11, D9): normalized test IDs, one canonical form per runner.

A test's ID must be the same whether it was read from the JUnit XML or from the runner's own
log (P2-14b), because the baseline's P0 set and the final run's `passed_ids` are compared as
sets (§11.2 a). The log lines used here are real output, recorded by
`scripts/record-test-results.sh` into `tests/fixtures/test_results/`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mpilot.verify.test_ids import (
    JEST_SEPARATOR,
    VITEST_SEPARATOR,
    jest_id,
    mocha_id,
    pytest_id,
    pytest_id_from_nodeid,
    vitest_id,
    workspace_relative,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "test_results"


# ---------------------------------------------------------------- pytest


@pytest.mark.parametrize(
    ("classname", "name", "expected"),
    [
        ("tests.test_sample", "test_pass", "tests.test_sample::test_pass"),
        ("tests.test_sample.TestKlass", "test_method", "tests.test_sample.TestKlass::test_method"),
        ("tests.test_sample", "test_param[x::y]", "tests.test_sample::test_param[x::y]"),
        ("", "tests.test_broken", "tests.test_broken"),  # a collection error has no classname
    ],
)
def test_the_pytest_id_is_classname_and_name(classname: str, name: str, expected: str) -> None:
    assert pytest_id(classname, name) == expected


@pytest.mark.parametrize(
    ("nodeid", "expected"),
    [
        ("tests/test_sample.py::test_pass", "tests.test_sample::test_pass"),
        ("tests/pkg/test_mod.py::test_in_package", "tests.pkg.test_mod::test_in_package"),
        (
            "tests/test_sample.py::TestKlass::TestInner::test_deep",
            "tests.test_sample.TestKlass.TestInner::test_deep",
        ),
        # Parameters are kept verbatim, even when they hold the separators or brackets.
        ("tests/test_sample.py::test_param[a/b]", "tests.test_sample::test_param[a/b]"),
        ("tests/test_sample.py::test_param[x::y]", "tests.test_sample::test_param[x::y]"),
        ("tests/test_sample.py::test_param[br[ack]et]", "tests.test_sample::test_param[br[ack]et]"),
        (
            "tests/test_sample.py::test_param[with space]",
            "tests.test_sample::test_param[with space]",
        ),
        # A module-level node (collection error) and a file outside a package.
        ("tests/test_broken.py", "tests.test_broken"),
        ("test_top.py::test_x", "test_top::test_x"),
    ],
)
def test_a_pytest_node_id_maps_to_the_junit_identity(nodeid: str, expected: str) -> None:
    assert pytest_id_from_nodeid(nodeid) == expected


def test_every_node_id_in_the_recorded_log_matches_the_recorded_xml() -> None:
    """`pytest -rA` prints every node ID; each one must map to an ID the XML of the same suite
    yields (pytest's own `mangle_test_address`)."""
    from mpilot.verify.junit import parse_junit
    from mpilot.verify.results import Runner

    log = (FIXTURES / "pytest-ra.stdout").read_text(encoding="utf-8")
    nodeids = set(re.findall(r"^(?:PASSED|FAILED|ERROR|XFAIL|XPASS) (.+?)(?: - .*)?$", log, re.M))
    assert "tests/test_sample.py::test_param[with space]" in nodeids
    assert len(nodeids) >= 15
    xml = parse_junit((FIXTURES / "pytest-junit.xml").read_bytes(), Runner.PYTEST)
    xml_ids = {c.test_id for c in xml.cases}
    assert {pytest_id_from_nodeid(n) for n in nodeids} <= xml_ids


# ---------------------------------------------------------------- vitest, jest, mocha


def test_the_vitest_id_is_the_file_and_titles_joined_as_vitest_prints_them() -> None:
    assert VITEST_SEPARATOR == " > "
    assert vitest_id("tests/math.test.js", ["math", "nested", "x"]) == (
        "tests/math.test.js > math > nested > x"
    )
    assert vitest_id("tests/math.test.js", []) == "tests/math.test.js"  # a suite that failed


def test_every_vitest_verbose_line_is_an_id_the_xml_yields() -> None:
    from mpilot.verify.junit import parse_junit
    from mpilot.verify.results import Runner

    log = (FIXTURES / "vitest-verbose.stdout").read_text(encoding="utf-8")
    log = re.sub(r"\x1b\[[0-9;]*m", "", log)
    lines = re.findall(r"^ [✓×↓] (.+?)(?: \d+ms)?$", log, re.M)
    assert len(lines) == 11
    xml = parse_junit((FIXTURES / "vitest-junit.xml").read_bytes(), Runner.VITEST)
    assert set(lines) <= {c.test_id for c in xml.cases}


def test_the_jest_id_is_the_file_and_titles_joined_as_jest_prints_them() -> None:
    assert JEST_SEPARATOR == " › "
    assert jest_id("tests/math.test.js", ["math", "nested", "x"]) == (
        "tests/math.test.js › math › nested › x"
    )
    assert jest_id("tests/a.test.js", ["top level"]) == "tests/a.test.js › top level"
    assert jest_id(None, ["math", "adds"]) == "math › adds"


def test_jest_failure_headers_in_the_recorded_log_are_ids_the_configured_xml_yields() -> None:
    """jest prints `FAIL <file>` and then `● a › b › title` for each failure."""
    from mpilot.verify.junit import parse_junit
    from mpilot.verify.results import CaseOutcome, Runner

    log = re.sub(r"\x1b\[[0-9;]*m", "", (FIXTURES / "jest-default.stderr").read_text("utf-8"))
    ids: set[str] = set()
    current = ""
    for line in log.splitlines():
        if m := re.match(r"^(?:PASS|FAIL) (\S+)", line):
            current = m[1]
        elif (m := re.match(r"^  ● (.+)$", line)) and m[1] != "Test suite failed to run":
            ids.add(jest_id(current, m[1].split(JEST_SEPARATOR)))
    assert len(ids) == 3
    xml = parse_junit((FIXTURES / "jest-junit-configured.xml").read_bytes(), Runner.JEST)
    assert ids <= {c.test_id for c in xml.cases if c.outcome is CaseOutcome.FAILED}


def test_the_mocha_id_is_the_full_title() -> None:
    assert mocha_id(["math", "nested", "x"]) == "math nested x"
    assert mocha_id(["top level"]) == "top level"


# ---------------------------------------------------------------- file paths


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/a.test.js", "tests/a.test.js"),
        ("./tests/a.test.js", "tests/a.test.js"),
        ("/workspace/test/math.spec.js", "test/math.spec.js"),
        ("/elsewhere/test.js", None),  # outside the workspace: not a repo path
        ("tests/../../etc/passwd", None),
        ("", None),
        ("/workspace", None),
        ("/workspace/", None),
        ("tests\\win.js", None),
        ("a\x00b", None),
    ],
)
def test_file_paths_are_made_workspace_relative_or_dropped(path: str, expected: str | None) -> None:
    assert workspace_relative(path) == expected
