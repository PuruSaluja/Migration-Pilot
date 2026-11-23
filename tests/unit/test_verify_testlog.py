"""P2-14b (SPEC FR-11, D9): the log-parsing fallback.

When the test command is wrapped, chained, overridden or taken from CI there is no JUnit XML
(`EnvDetection.junit` is None), so the runner's own output is the only result. The real logs
here were recorded by `scripts/record-test-results.sh` (same runs as the P2-14a XML samples,
so the two sources can be compared test by test): pytest 8.4.2 (`-q`, `-v`, `-rA`, collection
error, green), unittest on Python 3.10 and 3.12 (default and `-v`), jest 29.7.0 (default,
`--verbose`, mixed green/red, console output on a single file), vitest 3.2.7 (default and
verbose) and mocha 11.8.0 (spec and dot reporters).
Hand-written logs are marked SYNTHETIC. They cover only reporter variants that were not
recorded (pytest-xdist lines, unittest docstring headers, jest's older `● Console` layout) and
hostile output; they come from the documented formats and have not been checked against a run.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest

from mpilot.models.domain import TestStage
from mpilot.verify.junit import parse_junit
from mpilot.verify.results import (
    CaseOutcome,
    Counts,
    ParsedCase,
    ParsedTests,
    ResultSource,
    Runner,
    to_test_result,
)
from mpilot.verify.testlog import (
    MAX_LOG_CHARS,
    LogParseError,
    clean_output,
    detect_runner,
    parse_log,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "test_results"
P, F, E, S = CaseOutcome.PASSED, CaseOutcome.FAILED, CaseOutcome.ERROR, CaseOutcome.SKIPPED


def log(*names: str) -> str:
    """The recorded streams, stdout then stderr, as the caller would join an `ExecResult`."""
    parts = []
    for name in names:
        for suffix in (".stdout", ".stderr"):
            path = FIXTURES / f"{name}{suffix}"
            if path.exists():
                parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def result(parsed: ParsedTests):  # type: ignore[no-untyped-def]
    return to_test_result(parsed, run_id="r", stage=TestStage.BASELINE, duration_s=0.0)  # type: ignore[arg-type]


def xml_result(name: str, runner: Runner):  # type: ignore[no-untyped-def]
    return result(parse_junit((FIXTURES / name).read_bytes(), runner))


# ---------------------------------------------------------------- cleaning


def test_ansi_sequences_are_removed() -> None:
    text = "\x1b[1m\x1b[46m RUN \x1b[49m\x1b[22m \x1b[32m✓\x1b[39m ok\x1b]0;title\x07 end\x1b[2K"
    assert clean_output(text) == [" RUN  ✓ ok end"]


def test_a_progress_line_keeps_only_what_the_last_carriage_return_left() -> None:
    assert clean_output("10%\r50%\r100% done\nnext\r\n") == ["100% done", "next", ""]


def test_control_characters_are_dropped_but_tabs_stay() -> None:
    assert clean_output("a\x00b\x07c\x1fd\te") == ["abcd\te"]


# ---------------------------------------------------------------- runner detection


@pytest.mark.parametrize(
    ("names", "runner"),
    [
        (["pytest-junit"], Runner.PYTEST),
        (["pytest-v"], Runner.PYTEST),
        (["pytest-ra"], Runner.PYTEST),
        (["pytest-collection-error"], Runner.PYTEST),
        (["pytest-green"], Runner.PYTEST),
        (["unittest-py3.10"], Runner.UNITTEST),
        (["unittest-v-py3.12"], Runner.UNITTEST),
        (["jest-default"], Runner.JEST),
        (["jest-verbose"], Runner.JEST),
        (["vitest-default"], Runner.VITEST),
        (["vitest-verbose"], Runner.VITEST),
        (["mocha-spec"], Runner.MOCHA),
        (["mocha-dot"], Runner.MOCHA),
        (["jest-console"], Runner.JEST),
    ],
)
def test_the_runner_is_recognised_from_its_summary(names: list[str], runner: Runner) -> None:
    assert detect_runner(log(*names)) is runner


def test_no_summary_means_no_runner() -> None:
    assert detect_runner("hello\nworld\n") is None
    assert detect_runner("") is None


def test_two_runners_in_one_output_are_ambiguous_unless_the_command_names_one() -> None:
    both = log("pytest-junit") + "\n" + log("jest-default")
    assert detect_runner(both) is None
    assert detect_runner(both, "npm test") is None
    assert detect_runner(both, "python -m pytest && npx jest") is None  # names both
    assert detect_runner(both, "python -m pytest") is Runner.PYTEST
    assert detect_runner(both, "npx jest --ci") is Runner.JEST


def test_an_explicit_runner_skips_detection() -> None:
    parsed = parse_log(log("pytest-junit"), runner=Runner.PYTEST)
    assert parsed.runner is Runner.PYTEST
    assert parse_log(log("pytest-junit")).runner is Runner.PYTEST


def test_a_log_without_a_known_summary_is_refused() -> None:
    with pytest.raises(LogParseError, match="no test runner summary"):
        parse_log("collected 3 items\n...\n")
    with pytest.raises(LogParseError, match="mixed"):
        parse_log(log("pytest-junit") + "\n" + log("jest-default"))
    with pytest.raises(LogParseError, match="pytest summary"):
        parse_log(log("jest-default"), runner=Runner.PYTEST)


def test_a_tail_that_lost_the_summary_is_refused() -> None:
    head = "\n".join(log("pytest-v").splitlines()[:15])
    with pytest.raises(LogParseError, match="summary"):
        parse_log(head, runner=Runner.PYTEST)


# ---------------------------------------------------------------- pytest


def test_pytest_verbose_log_agrees_with_the_junit_of_the_same_run() -> None:
    parsed = parse_log(log("pytest-v"))
    assert parsed.source is ResultSource.LOG
    assert parsed.passed_ids_known
    got, want = result(parsed), xml_result("pytest-junit.xml", Runner.PYTEST)
    assert got.passed_ids == want.passed_ids
    assert got.failed_ids == want.failed_ids
    assert len(got.failed_ids) == 7  # five failures and two errors
    assert (got.failed, got.errors, got.skipped) == (want.failed, want.errors, want.skipped)
    # The log counts a passing test whose teardown errors as passed *and* errored; the XML
    # reports that test once, as an error. The ID is failed in both (fail-closed).
    assert (got.passed, want.passed) == (12, 11)
    assert "tests.test_sample::test_teardown_error" in got.failed_ids
    assert "tests.test_sample::test_teardown_error" not in got.passed_ids
    assert got.executed_count == 12 + 5 + 2


def test_pytest_quiet_log_gives_failed_ids_and_counts_but_not_passed_ids() -> None:
    parsed = parse_log(log("pytest-junit"))  # `pytest -q --junitxml=...`: the log is the same
    assert not parsed.passed_ids_known
    got = result(parsed)
    assert got.passed_ids == frozenset()
    assert got.failed_ids == xml_result("pytest-junit.xml", Runner.PYTEST).failed_ids
    assert (got.passed, got.failed, got.skipped, got.errors) == (12, 5, 3, 2)
    assert any("passed" in w and "verbose" in w for w in parsed.warnings)


def test_pytest_dash_r_a_log_lists_every_passed_test() -> None:
    parsed = parse_log(log("pytest-ra"))
    assert parsed.passed_ids_known
    got = result(parsed)
    want = xml_result("pytest-junit.xml", Runner.PYTEST)
    assert (got.passed_ids, got.failed_ids) == (want.passed_ids, want.failed_ids)
    assert "tests.test_sample::test_param[x::y]" in got.failed_ids
    assert "tests.test_sample::test_param[br[ack]et]" in got.passed_ids


def test_a_pytest_collection_error_is_an_errored_module() -> None:
    parsed = parse_log(log("pytest-collection-error"))
    got = result(parsed)
    assert got.failed_ids == {"tests.test_broken"}
    assert (got.passed, got.failed, got.skipped, got.errors) == (0, 0, 0, 1)
    assert parsed.passed_ids_known  # nothing passed, so nothing is missing


def test_a_green_quiet_pytest_run_has_a_count_but_no_ids() -> None:
    parsed = parse_log(log("pytest-green"))
    got = result(parsed)
    assert (got.passed, got.executed_count) == (1, 1)
    assert not parsed.passed_ids_known


def test_failure_messages_come_from_the_short_summary() -> None:
    by_id = {f.test_id: f.message for f in result(parse_log(log("pytest-v"))).failures}
    assert by_id["tests.test_sample::test_fail"] == "AssertionError: one is not two"
    assert by_id["tests.test_sample::test_setup_error"] == "RuntimeError: fixture boom"
    assert by_id["tests.test_sample::test_xpass_strict"] == ""


PYTEST_SYNTHETIC_XDIST = """\
============================= test session starts ==============================
[gw0] [ 50%] PASSED tests/test_a.py::test_one
[gw1] [100%] FAILED tests/test_a.py::TestK::test_two[a - b]
=========================== short test summary info ============================
FAILED tests/test_a.py::TestK::test_two[a - b] - assert 1 == 2
========================= 1 failed, 1 passed in 0.50s ==========================
"""


def test_pytest_xdist_lines_and_a_dash_inside_a_parameter() -> None:
    parsed = parse_log(PYTEST_SYNTHETIC_XDIST)
    got = result(parsed)
    assert got.passed_ids == {"tests.test_a::test_one"}
    assert got.failed_ids == {"tests.test_a.TestK::test_two[a - b]"}
    assert got.failures[0].message == "assert 1 == 2"
    assert parsed.passed_ids_known


def test_pytest_verbose_without_percentages_and_with_skip_reasons() -> None:
    text = (
        "tests/test_a.py::test_x PASSED\n"
        "tests/test_a.py::test_y SKIPPED (needs a gpu)\n"
        "tests/test_a.py::test_z XFAIL (known)\n"
        "==== 1 passed, 1 skipped, 1 xfailed in 0.01s ====\n"
    )
    parsed = parse_log(text)
    assert [(c.test_id, c.outcome) for c in parsed.cases] == [
        ("tests.test_a::test_x", P),
        ("tests.test_a::test_y", S),
        ("tests.test_a::test_z", S),
    ]
    assert parsed.counts == Counts(passed=1, failed=0, skipped=2, errors=0)


@pytest.mark.parametrize(
    ("line", "counts"),
    [
        ("5 passed in 0.12s", Counts(passed=5)),
        ("1 failed, 2 passed, 3 warnings in 1.50s", Counts(passed=2, failed=1)),
        ("2 passed, 1 deselected in 0.01s", Counts(passed=2)),
        ("1 xpassed, 1 xfailed, 1 error in 0.10s (0:00:00)", Counts(passed=1, skipped=1, errors=1)),
        ("no tests ran in 0.01s", Counts()),
        ("=== 3 passed, 1 rerun in 12.00s (0:00:12) ===", Counts(passed=3)),
    ],
)
def test_pytest_summary_words_map_to_junit_meanings(line: str, counts: Counts) -> None:
    assert parse_log(line, runner=Runner.PYTEST).counts == counts


# ---------------------------------------------------------------- unittest


UNITTEST_FAILED_IDS = {
    "tests.test_things.Things.test_fail",
    "tests.test_things.Things.test_error",
    "tests.test_things.Things.test_subtests (i=1)",
    "unittest.loader._FailedTest.tests.test_import_error",
}


@pytest.mark.parametrize("py", ["3.10", "3.12"])
def test_unittest_default_log_gives_failed_ids_and_counts(py: str) -> None:
    """Python 3.10 prints `test_x (mod.Class)`, 3.12 `test_x (mod.Class.test_x)`: one ID."""
    parsed = parse_log(log(f"unittest-py{py}"))
    got = result(parsed)
    assert got.failed_ids == UNITTEST_FAILED_IDS
    # 8 tests ran; 2 failures (one is a subtest), 2 errors, 1 skipped, 1 expected failure.
    assert (got.passed, got.failed, got.errors, got.skipped) == (2, 2, 2, 2)
    assert not parsed.passed_ids_known


@pytest.mark.parametrize("py", ["3.10", "3.12"])
def test_unittest_verbose_log_lists_every_passed_test(py: str) -> None:
    parsed = parse_log(log(f"unittest-v-py{py}"))
    got = result(parsed)
    assert parsed.passed_ids_known
    assert got.passed_ids == {
        "tests.test_things.Other.test_other",
        "tests.test_things.Things.test_pass",
    }
    assert got.failed_ids == UNITTEST_FAILED_IDS
    outcomes = {c.test_id: c.outcome for c in parsed.cases}
    assert outcomes["tests.test_things.Things.test_skip"] is S
    assert outcomes["tests.test_things.Things.test_expected_failure"] is S


def test_unittest_ids_are_the_same_on_both_python_versions() -> None:
    old, new = (result(parse_log(log(f"unittest-v-py{v}"))) for v in ("3.10", "3.12"))
    assert (old.passed_ids, old.failed_ids) == (new.passed_ids, new.failed_ids)


UNITTEST_SYNTHETIC_DOCSTRING = """\
test_a (pkg.test_a.Case.test_a)
Adds things. ... ok
test_b (pkg.test_a.Case.test_b) ... unexpected success
test_c (pkg.test_a.Case.test_c) ... skipped 'why not'

----------------------------------------------------------------------
Ran 3 tests in 0.001s

FAILED (unexpected successes=1, skipped=1)
"""


def test_unittest_docstring_headers_and_unexpected_success() -> None:
    parsed = parse_log(UNITTEST_SYNTHETIC_DOCSTRING)
    assert [(c.test_id, c.outcome) for c in parsed.cases] == [
        ("pkg.test_a.Case.test_a", P),
        ("pkg.test_a.Case.test_b", F),
        ("pkg.test_a.Case.test_c", S),
    ]
    assert parsed.counts == Counts(passed=1, failed=1, skipped=1)
    assert parsed.passed_ids_known


@pytest.mark.parametrize(
    ("tail", "counts"),
    [
        ("Ran 5 tests in 0.1s\n\nOK", Counts(passed=5)),
        ("Ran 5 tests in 0.1s\n\nOK (skipped=2)", Counts(passed=3, skipped=2)),
        ("Ran 1 test in 0.1s\n\nOK", Counts(passed=1)),
        ("Ran 0 tests in 0.000s\n\nNO TESTS RAN", Counts()),
        (
            "Ran 4 tests in 0.1s\n\nFAILED (failures=1, errors=1, expected failures=1)",
            Counts(1, 1, 1, 1),
        ),
        (
            "Ran 2 tests in 0.1s\n\nFAILED (failures=5)",
            Counts(passed=0, failed=5),
        ),  # subtests: clamp
    ],
)
def test_unittest_result_line_to_counts(tail: str, counts: Counts) -> None:
    assert parse_log(tail, runner=Runner.UNITTEST).counts == counts


def test_unittest_without_a_result_line_is_refused() -> None:
    with pytest.raises(LogParseError, match="result line"):
        parse_log("Ran 3 tests in 0.1s\n", runner=Runner.UNITTEST)


# ---------------------------------------------------------------- jest


def test_jest_verbose_log_agrees_with_the_configured_junit_of_the_same_run() -> None:
    parsed = parse_log(log("jest-verbose"))
    assert parsed.passed_ids_known
    got = result(parsed)
    want = xml_result("jest-junit-configured.xml", Runner.JEST)
    todo = "tests/math.test.js › math › todo one"
    assert todo in want.passed_ids  # jest-junit has no marker for test.todo; the log has
    assert got.passed_ids == want.passed_ids - {todo}
    assert got.failed_ids == want.failed_ids
    assert "tests/broken.test.js" in got.failed_ids  # the suite that failed to load
    # Log meanings: `Tests: 3 failed, 1 skipped, 1 todo, 6 passed`; todo is not executed.
    assert (got.passed, got.failed, got.skipped, got.errors) == (6, 3, 2, 1)
    assert got.executed_count == 10


def test_jest_default_log_gives_failed_ids_but_not_passed_ones() -> None:
    parsed = parse_log(log("jest-default"))
    got = result(parsed)
    assert not parsed.passed_ids_known
    assert got.failed_ids == xml_result("jest-junit-configured.xml", Runner.JEST).failed_ids
    assert (got.passed, got.failed, got.skipped, got.errors) == (6, 3, 2, 1)


def test_jest_failure_messages_and_suite_errors() -> None:
    by_id = {f.test_id: f.message for f in result(parse_log(log("jest-default"))).failures}
    assert by_id["tests/sub/unicode.test.js › throws error"] == "TypeError: boom"
    assert by_id["tests/math.test.js › math › inc 2 -> 4"] == (
        "expect(received).toBe(expected) // Object.is equality"
    )
    assert (
        by_id["tests/broken.test.js"]
        == "Cannot find module './does-not-exist' from 'tests/broken.test.js'"
    )


def test_a_jest_run_with_a_green_file_and_a_red_one() -> None:
    parsed = parse_log(log("jest-green"))
    got = result(parsed)
    assert got.failed_ids == {"tests/sub/unicode.test.js › throws error"}
    assert (got.passed, got.failed) == (3, 1)
    assert not parsed.passed_ids_known  # PASS tests/green.test.js lists no tests


JEST_CONSOLE_IDS = [
    ("tests2/console.test.js › top", P),
    ("tests2/console.test.js › later", S),
    ("tests2/console.test.js › outer › first", P),
    ("tests2/console.test.js › outer › inner › second", P),
    ("tests2/console.test.js › outer › inner › third", S),
]


def test_jest_console_output_before_the_tree_names_no_test() -> None:
    """Recorded with jest 29.7 on a single file (so verbose): console output comes before the
    `PASS` line, and top-level tests are listed before the describe blocks."""
    parsed = parse_log(log("jest-console"))
    assert [(c.test_id, c.outcome) for c in parsed.cases] == JEST_CONSOLE_IDS
    assert parsed.passed_ids_known
    assert parsed.counts == Counts(passed=3, skipped=2)


# SYNTHETIC, from jest's older layout (a `● Console` heading inside the file's section); no
# jest of that age was run here, so this only checks the parser's own tolerance.
JEST_SYNTHETIC_OLD_CONSOLE = """\
PASS tests2/console.test.js
  ● Console

    console.log
      hello
        indented

      at Object.<anonymous> (tests2/console.test.js:2:9)

  outer
    ✓ first (3 ms)
    inner
      ✓ second
      ○ skipped third
  ✓ top
    console.log
      noise after
  ✎ todo later

Tests:       3 passed, 1 skipped, 1 todo, 5 total
Test Suites: 1 passed, 1 total
"""


def test_an_older_jest_console_heading_does_not_become_a_describe_block() -> None:
    parsed = parse_log(JEST_SYNTHETIC_OLD_CONSOLE)
    assert sorted((c.test_id, c.outcome) for c in parsed.cases) == sorted(JEST_CONSOLE_IDS)
    assert parsed.passed_ids_known


def test_jest_summary_counts_across_two_runs_are_summed() -> None:
    text = "Tests:       1 failed, 2 passed, 3 total\nTests:       4 passed, 4 total\n"
    assert parse_log(text, runner=Runner.JEST).counts == Counts(passed=6, failed=1)


# ---------------------------------------------------------------- vitest


def test_vitest_verbose_log_agrees_with_the_junit_of_the_same_run() -> None:
    parsed = parse_log(log("vitest-verbose"))
    assert parsed.passed_ids_known
    got, want = result(parsed), xml_result("vitest-junit.xml", Runner.VITEST)
    assert (got.passed_ids, got.failed_ids) == (want.passed_ids, want.failed_ids)
    assert "tests/broken.test.js" in got.failed_ids
    # Log: `3 failed | 6 passed | 1 skipped | 1 todo`; the file that failed to load adds an error.
    assert (got.passed, got.failed, got.skipped, got.errors) == (6, 3, 2, 1)


def test_vitest_default_log_prefixes_nested_tests_with_their_file() -> None:
    parsed = parse_log(log("vitest-default"))
    got = result(parsed)
    want = xml_result("vitest-junit.xml", Runner.VITEST)
    assert got.failed_ids == want.failed_ids
    assert not parsed.passed_ids_known
    assert got.passed_ids == frozenset()  # not the full set, so none is offered
    listed = {c.test_id for c in parsed.cases if c.outcome is P}
    assert listed <= want.passed_ids
    assert "tests/math.test.js > math > adds" in listed
    assert "tests/green.test.js > green > one" not in listed  # a green file lists none


def test_vitest_failure_messages() -> None:
    by_id = {f.test_id: f.message for f in result(parse_log(log("vitest-verbose"))).failures}
    assert by_id["tests/sub/unicode.test.js > throws error"] == "TypeError: boom"
    assert by_id["tests/broken.test.js"].startswith(
        "Error: Cannot find module './does-not-exist.js'"
    )


# ---------------------------------------------------------------- mocha


def test_mocha_spec_log_agrees_with_the_junit_of_the_same_run() -> None:
    parsed = parse_log(log("mocha-spec"))
    assert parsed.passed_ids_known
    got, want = result(parsed), xml_result("mocha-junit.xml", Runner.MOCHA)
    assert (got.passed_ids, got.failed_ids) == (want.passed_ids, want.failed_ids)
    assert (got.passed, got.failed, got.skipped, got.errors) == (4, 3, 2, 0)
    by_id = {f.test_id: f.message for f in got.failures}
    assert by_id['math hooks "before all" hook for "after failing hook"'] == "Error: hook boom"
    assert by_id["math inc 2 -> 4"].startswith("AssertionError [ERR_ASSERTION]")


def test_mocha_dot_reporter_gives_counts_and_the_failure_from_the_details() -> None:
    """Recorded: `mocha --reporter dot` prints `.!,` and then only the failure details."""
    parsed = parse_log(log("mocha-dot"))
    got = result(parsed)
    assert (got.passed, got.failed, got.skipped) == (1, 1, 1)
    assert got.failed_ids == {"suite bad"}
    assert got.failures[0].message == "Error: bad"
    assert not parsed.passed_ids_known


# ---------------------------------------------------------------- what makes passed IDs trustworthy


def test_passed_ids_are_known_only_when_the_listing_matches_the_summary() -> None:
    """The self-check: every passed test in the count must have been seen in the listing."""
    text = log("pytest-v")
    assert parse_log(text).passed_ids_known
    dropped = text.replace("tests/test_sample.py::test_pass PASSED", "")
    assert not parse_log(dropped).passed_ids_known


def test_a_truncated_output_never_has_known_passed_ids() -> None:
    """Even when what is left still agrees with itself, the part that was cut is unknown."""
    parsed = parse_log(log("pytest-v"), truncated=True)
    assert not parsed.passed_ids_known
    assert any("truncated" in w for w in parsed.warnings)


def test_a_summary_line_printed_inside_a_failure_cannot_make_passed_ids_known() -> None:
    forged = log("pytest-v").replace(
        "short test summary info ============================\n",
        "short test summary info ============================\n3 passed in 0.10s\n",
    )
    parsed = parse_log(forged)
    assert parsed.counts.passed > 12
    assert not parsed.passed_ids_known


def test_two_runs_in_one_output_add_up_and_stay_known() -> None:
    both = log("pytest-ra") + "\n" + log("pytest-ra")
    parsed = parse_log(both)
    assert parsed.counts.passed == 24
    assert parsed.passed_ids_known
    assert result(parsed).failed_ids == xml_result("pytest-junit.xml", Runner.PYTEST).failed_ids


def test_failures_listed_fewer_than_counted_is_warned() -> None:
    text = "tests/a.py::t FAILED\n== 2 failed in 1s ==\n"
    parsed = parse_log(text, runner=Runner.PYTEST)
    assert any("failed" in w and "listed" in w for w in parsed.warnings)


# ---------------------------------------------------------------- hostile output


def test_an_oversized_output_is_refused() -> None:
    with pytest.raises(LogParseError, match="characters"):
        parse_log("x" * (MAX_LOG_CHARS + 1))


@pytest.mark.parametrize(
    "text",
    [
        "tests/a.py::" + "t" * 100_000 + " PASSED [100%]\n1 passed in 1s\n",
        "a ... " * 50_000 + "ok\nRan 1 test in 1s\n\nOK\n",
        "  " * 100_000 + "✓ deep\n  ✓ x\nTests: 1 passed, 1 total\n",
        ("(" * 5000 + "\n") * 40 + "Ran 1 test in 1s\n\nOK\n",
        "=" * 200_000 + "\n5 passed in 1s\n",
    ],
)
def test_long_or_pathological_lines_parse_quickly(text: str) -> None:
    start = time.monotonic()
    with contextlib.suppress(LogParseError):
        parse_log(text)
    assert time.monotonic() - start < 3.0


def test_a_lot_of_lines_is_not_slow_and_is_capped() -> None:
    lines = "".join(f"tests/a.py::t{i} PASSED [100%]\n" for i in range(150_000))
    start = time.monotonic()
    with pytest.raises(LogParseError, match="more than"):
        parse_log(lines + "150000 passed in 1s\n", runner=Runner.PYTEST, max_cases=100_000)
    assert time.monotonic() - start < 5.0


def test_binary_junk_does_not_crash() -> None:
    junk = bytes(range(256)).decode("latin-1") * 50
    with pytest.raises(LogParseError):
        parse_log(junk)


def test_parsed_log_cases_are_plain_values() -> None:
    parsed = parse_log(PYTEST_SYNTHETIC_XDIST)
    assert all(isinstance(c, ParsedCase) for c in parsed.cases)
    assert parsed.totals == Counts(passed=1, failed=1)


def test_a_blank_id_is_never_a_case() -> None:
    """Defence in depth: no parser can produce one from a matched line, but a case with an
    empty ID would fail `ParsedCase` validation and take the whole parse down."""
    from mpilot.verify.testlog import _case, _Raw

    assert _case("  ", P) is None
    raw = _Raw(max_cases=10)
    raw.list(None)
    raw.fail(None)
    assert raw.listed == []
    assert raw.failures == []


def test_the_case_cap_also_covers_the_pytest_short_summary() -> None:
    lines = "=== short test summary info ===\n" + "".join(
        f"PASSED tests/a.py::t{i}\n" for i in range(50)
    )
    with pytest.raises(LogParseError, match="more than 10"):
        parse_log(lines + "50 passed in 1s\n", runner=Runner.PYTEST, max_cases=10)


def test_a_failure_header_with_nothing_after_it_has_an_empty_message() -> None:
    text = "FAIL tests/a.test.js\n  ● suite › broken\nTests: 1 failed, 1 total\n"
    (case,) = parse_log(text, runner=Runner.JEST).cases
    assert (case.test_id, case.message) == ("tests/a.test.js › suite › broken", "")


def test_jest_output_before_any_file_header_names_no_test() -> None:
    text = "  ● orphan › title\n  ✓ orphan line\nTests: 1 passed, 1 total\n"
    parsed = parse_log(text, runner=Runner.JEST)
    assert parsed.cases == ()
    assert not parsed.passed_ids_known


def test_vitest_nested_lines_before_any_file_header_are_ignored() -> None:
    text = "   ✓ stray 1ms\n      Tests  1 passed (1)\n"
    parsed = parse_log(text, runner=Runner.VITEST)
    assert parsed.cases == ()
    assert parsed.counts == Counts(passed=1)


@pytest.mark.parametrize(
    ("runner", "text"),
    [
        (Runner.VITEST, " FAIL  tests/a.test.js [ tests/a.test.js ]\nError: boom\n"),
        (Runner.JEST, "FAIL tests/a.test.js\n  ● Test suite failed to run\n    boom\n"),
        (Runner.MOCHA, "  ✔ a test\n  1) another\n"),
    ],
)
def test_a_failure_without_a_summary_is_still_a_missing_summary(runner: Runner, text: str) -> None:
    with pytest.raises(LogParseError, match=f"no {runner.value} summary"):
        parse_log(text, runner=runner)


def test_a_message_never_comes_from_the_next_block() -> None:
    vitest = (
        " FAIL  a.test.js > one\n\n FAIL  a.test.js > two\nError: second\n"
        "      Tests  2 failed (2)\n"
    )
    by_id = {c.test_id: c.message for c in parse_log(vitest, runner=Runner.VITEST).cases}
    assert by_id == {"a.test.js > one": "", "a.test.js > two": "Error: second"}
    mocha = "  1 passing\n  2 failing\n\n  1) a\n       b:\n\n  2) c\n       d:\n     Error: x\n"
    by_id = {c.test_id: c.message for c in parse_log(mocha, runner=Runner.MOCHA).cases}
    assert by_id == {"a b": "", "c d": "Error: x"}


def test_a_message_is_looked_for_only_a_few_lines_down() -> None:
    text = "FAIL a.test.js\n  ● broken\n" + "\n" * 20 + "     lost\nTests: 1 failed, 1 total\n"
    (case,) = parse_log(text, runner=Runner.JEST).cases
    assert case.message == ""


def test_text_in_a_jest_failure_block_is_not_a_test_line() -> None:
    text = "FAIL a.test.js\n  ● t1\n\n    ✓ looks like a passing test\n\nTests: 1 failed, 1 total\n"
    parsed = parse_log(text, runner=Runner.JEST)
    assert [(c.test_id, c.outcome) for c in parsed.cases] == [("a.test.js › t1", F)]


def test_a_failure_line_not_under_a_separator_is_not_a_unittest_failure() -> None:
    """A test can print `FAIL: x (a.B)`; only a block under the `====` rule counts."""
    text = (
        "FAIL: ghost (pkg.Case)\n"
        + "=" * 70
        + "\nFAIL: real (pkg.Case)\n"
        + "-" * 70
        + "\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n"
    )
    parsed = parse_log(text, runner=Runner.UNITTEST)
    assert {c.test_id for c in parsed.cases} == {"pkg.Case.real"}


def test_pytest_lines_after_the_short_summary_banner_are_not_results() -> None:
    text = (
        "=== short test summary info ===\n"
        "FAILED tests/a.py::real - boom\n"
        "=== a plugin's own report ===\n"
        "FAILED tests/a.py::ghost - not a result\n"
        "=== 1 failed in 0.1s ===\n"
    )
    ids = {c.test_id for c in parse_log(text, runner=Runner.PYTEST).cases}
    assert ids == {"tests.a::real"}


def test_a_line_over_the_length_cap_is_not_a_test_line() -> None:
    name = "a" * 5000
    text = f"test_{name} (pkg.Case) ... ok\nRan 1 test in 0.1s\n\nOK\n"
    parsed = parse_log(text, runner=Runner.UNITTEST)
    assert parsed.cases == ()
    assert not parsed.passed_ids_known
