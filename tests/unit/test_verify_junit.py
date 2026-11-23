"""P2-14a (SPEC FR-11, D9, §11.2): JUnit XML parsing into normalized test cases, and the
`TestResult` built from them.

The XML samples are real output, not hand-written: `scripts/record-test-results.sh` ran
jest 29.7.0 + jest-junit 16.0.0, vitest 3.2.7, mocha 11.8.0 + mocha-junit-reporter 2.2.1 and
pytest 8.4.2 in the mpilot/node:22 and mpilot/python:3.12 images, offline, on small sample
projects with passing, failing, erroring, skipped, parametrized, nested, unicode and
XML-special-character tests, and suites that fail to load. Hostile inputs are hand-written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mpilot.core.types import AttemptId, RunId
from mpilot.models.domain import TestStage
from mpilot.verify.junit import (
    MAX_JUNIT_BYTES,
    JunitParseError,
    parse_junit,
)
from mpilot.verify.results import (
    MAX_FAILURES,
    MAX_MESSAGE_CHARS,
    MAX_TRACE_CHARS,
    CaseOutcome,
    ParsedCase,
    ParsedTests,
    ResultSource,
    Runner,
    to_test_result,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "test_results"
RUN = RunId("run-1")
P, F, E, S = CaseOutcome.PASSED, CaseOutcome.FAILED, CaseOutcome.ERROR, CaseOutcome.SKIPPED


def parse(name: str, runner: Runner) -> ParsedTests:
    return parse_junit((FIXTURES / name).read_bytes(), runner)


def outcomes(parsed: ParsedTests) -> dict[str, CaseOutcome]:
    seen: dict[str, CaseOutcome] = {}
    for case in parsed.cases:
        assert case.test_id not in seen, f"unexpected duplicate {case.test_id}"
        seen[case.test_id] = case.outcome
    return seen


# ---------------------------------------------------------------- pytest

PYTEST_SAMPLE = {
    "tests.pkg.test_mod::test_in_package": F,
    "tests.test_sample::test_pass": P,
    "tests.test_sample::test_fail": F,
    "tests.test_sample::test_setup_error": E,
    "tests.test_sample::test_teardown_error": E,
    "tests.test_sample::test_skipped": S,
    "tests.test_sample::test_xfail": S,  # pytest writes an expected failure as skipped
    "tests.test_sample::test_xpass": P,
    "tests.test_sample::test_xpass_strict": F,
    "tests.test_sample::test_param[1]": P,
    "tests.test_sample::test_param[a/b]": P,
    "tests.test_sample::test_param[x::y]": F,
    "tests.test_sample::test_param[with space]": P,
    "tests.test_sample::test_param[\\xfcn\\xef]": P,  # pytest escapes non-ASCII parameters
    "tests.test_sample::test_param[br[ack]et]": P,
    "tests.test_sample.TestKlass::test_method": P,
    "tests.test_sample.TestKlass::test_method_param[1]": P,
    "tests.test_sample.TestKlass::test_method_param[2]": F,
    "tests.test_sample.TestKlass.TestInner::test_deep": P,
    "tests.test_sample.LegacyCase::test_legacy": P,
    "tests.test_sample.LegacyCase::test_legacy_skip": S,
}


def test_pytest_xml_yields_every_case_with_its_outcome() -> None:
    parsed = parse("pytest-junit.xml", Runner.PYTEST)
    assert (parsed.runner, parsed.source) == (Runner.PYTEST, ResultSource.JUNIT)
    assert outcomes(parsed) == PYTEST_SAMPLE
    assert parsed.warnings == ()


def test_pytest_counts_come_from_the_cases_not_the_suite_attributes() -> None:
    """The recorded suite says tests="22" for 21 cases: pytest also counts the teardown
    error of a passing test. The cases are what the run actually reports per test."""
    parsed = parse("pytest-junit.xml", Runner.PYTEST)
    assert parsed.counts.as_tuple() == (11, 5, 3, 2)  # passed, failed, skipped, errors
    assert parsed.counts.executed == 18


def test_the_xunit1_family_gives_the_same_ids_plus_the_file() -> None:
    xunit2 = parse("pytest-junit.xml", Runner.PYTEST)
    xunit1 = parse("pytest-xunit1.xml", Runner.PYTEST)
    assert outcomes(xunit1) == outcomes(xunit2)
    files = {c.test_id: c.file for c in xunit1.cases}
    assert files["tests.pkg.test_mod::test_in_package"] == "tests/pkg/test_mod.py"
    assert files["tests.test_sample.TestKlass.TestInner::test_deep"] == "tests/test_sample.py"
    assert {c.file for c in xunit2.cases} == {None}  # xunit2 (the default) has no file


def test_a_pytest_collection_error_is_an_errored_module() -> None:
    parsed = parse("pytest-collection-error.xml", Runner.PYTEST)
    assert outcomes(parsed) == {"tests.test_broken": E}
    (case,) = parsed.cases
    assert case.message == "collection failure"
    assert "ModuleNotFoundError: No module named 'does_not_exist'" in case.trace_excerpt


def test_failure_messages_and_traces_are_kept_unescaped() -> None:
    by_id = {c.test_id: c for c in parse("pytest-junit.xml", Runner.PYTEST).cases}
    case = by_id["tests.pkg.test_mod::test_in_package"]
    assert case.message.startswith("assert '<&>' == '<&>\"'")
    assert "tests/pkg/test_mod.py:2: AssertionError" in case.trace_excerpt
    assert by_id["tests.test_sample::test_setup_error"].message == (
        'failed on setup with "RuntimeError: fixture boom"'
    )
    assert by_id["tests.test_sample::test_skipped"].message == "not today"
    assert by_id["tests.test_sample::test_pass"].message == ""


# ---------------------------------------------------------------- vitest

VITEST_SAMPLE = {
    "tests/broken.test.js": F,  # the suite that failed to load is one failed case
    "tests/green.test.js > green > one": P,
    "tests/green.test.js > green > two": P,
    "tests/math.test.js > math > adds": P,
    'tests/math.test.js > math > nested > subtracts & <compares> "quotes"': F,
    "tests/math.test.js > math > skipped one": S,
    "tests/math.test.js > math > todo one": S,
    "tests/math.test.js > math > inc 1 -> 2": P,
    "tests/math.test.js > math > inc 2 -> 4": F,
    "tests/math.test.js > top level": P,
    "tests/sub/unicode.test.js > ünïcödé ✓": P,
    "tests/sub/unicode.test.js > throws error": F,
}


def test_vitest_xml_yields_file_qualified_ids() -> None:
    parsed = parse("vitest-junit.xml", Runner.VITEST)
    assert outcomes(parsed) == VITEST_SAMPLE
    files = {c.test_id: c.file for c in parsed.cases}
    assert files["tests/math.test.js > math > adds"] == "tests/math.test.js"
    assert files["tests/broken.test.js"] == "tests/broken.test.js"
    by_id = {c.test_id: c for c in parsed.cases}
    assert by_id["tests/sub/unicode.test.js > throws error"].message == "boom"
    assert parsed.warnings == ()


# ---------------------------------------------------------------- jest

JEST_CONFIGURED = {
    "tests/broken.test.js": F,  # suite error: jest-junit writes it twice (error, failure)
    "tests/green.test.js › green › one": P,
    "tests/green.test.js › green › two": P,
    "tests/math.test.js › math › adds": P,
    'tests/math.test.js › math › nested › subtracts & <compares> "quotes"': F,
    "tests/math.test.js › math › skipped one": S,
    "tests/math.test.js › math › todo one": P,  # jest-junit has no marker for test.todo
    "tests/math.test.js › math › inc 1 -> 2": P,
    "tests/math.test.js › math › inc 2 -> 4": F,
    "tests/math.test.js › top level": P,
    "tests/sub/unicode.test.js › ünïcödé ✓": P,
    "tests/sub/unicode.test.js › throws error": F,
}


def merged(parsed: ParsedTests) -> dict[str, CaseOutcome]:
    """Per ID, the outcome `to_test_result` judges by (FAILED wins)."""
    result = to_test_result(parsed, run_id=RUN, stage=TestStage.BASELINE, duration_s=1.0)
    ids = {c.test_id for c in parsed.cases}
    out: dict[str, CaseOutcome] = {}
    for test_id in ids:
        if test_id in result.failed_ids:
            out[test_id] = F
        elif test_id in result.passed_ids:
            out[test_id] = P
        else:
            out[test_id] = S
    return out


@pytest.mark.parametrize("name", ["jest-junit-configured.xml", "jest-junit-repo-config.xml"])
def test_jest_xml_written_with_the_mpilot_settings_yields_file_qualified_ids(name: str) -> None:
    """The second sample was recorded with a repo `jest-junit` config in package.json that
    asks for other templates, another file and no suite errors: the environment wins."""
    parsed = parse(name, Runner.JEST)
    assert merged(parsed) == JEST_CONFIGURED
    suite = [c for c in parsed.cases if c.test_id == "tests/broken.test.js"]
    assert sorted(c.outcome for c in suite) == sorted([E, F])
    assert {c.file for c in suite} == {"tests/broken.test.js"}
    by_id = {c.test_id: c for c in parsed.cases}
    assert by_id["tests/sub/unicode.test.js › throws error"].message == "TypeError: boom"
    assert parsed.warnings == ()


def test_jest_xml_with_jest_junit_defaults_is_parsed_but_flagged() -> None:
    """Without the settings jest-junit writes no file path (IDs can collide across files and
    do not match the log) and drops suites that fail to load."""
    parsed = parse("jest-junit-default.xml", Runner.JEST)
    ids = outcomes(parsed)
    assert ids["math adds"] is P  # jest-junit's own "{classname} {title}", spaces between
    assert ids["top level"] is P
    assert ids['math nested subtracts & <compares> "quotes"'] is F
    assert "tests/broken.test.js" not in ids
    assert len(ids) == 11
    assert any("file" in w for w in parsed.warnings)


# ---------------------------------------------------------------- mocha


def test_mocha_xml_yields_full_titles_and_suite_files() -> None:
    parsed = parse("mocha-junit.xml", Runner.MOCHA)
    assert outcomes(parsed) == {
        "top level": P,
        "ünïcödé ✓": P,
        "math adds": P,
        "math inc 1 -> 2": P,
        "math inc 2 -> 4": F,
        'math nested subtracts & <compares> "quotes"': F,
        'math hooks "before all" hook for "after failing hook"': F,
    }
    files = {c.test_id: c.file for c in parsed.cases}
    assert files["math adds"] == "test/math.spec.js"  # /workspace/... made relative
    assert files["top level"] is None  # mocha's root suite has no file


# ---------------------------------------------------------------- shapes and outcomes


def xml(body: str, root: str = "testsuites") -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><{root}>{body}</{root}>'.encode()


def test_a_bare_testsuite_root_and_nested_suites_are_accepted() -> None:
    nested = xml(
        '<testsuite name="outer"><testsuite name="inner">'
        '<testcase classname="m" name="t1"/></testsuite>'
        '<testcase classname="m" name="t2"/></testsuite>'
    )
    assert {c.test_id for c in parse_junit(nested, Runner.PYTEST).cases} == {"m::t1", "m::t2"}
    bare = b'<testsuite name="s"><testcase classname="m" name="t"/></testsuite>'
    assert [c.test_id for c in parse_junit(bare, Runner.PYTEST).cases] == ["m::t"]


@pytest.mark.parametrize(
    ("children", "expected"),
    [
        ("", P),
        ("<system-out>noise</system-out><system-err>x</system-err>", P),
        ("<skipped/>", S),
        ('<failure message="m"/>', F),
        ('<error message="m"/>', E),
        ("<failure/><error/>", F),  # pytest: a failing test whose teardown also errors
        ("<error/><failure/>", F),
        ("<skipped/><failure/>", F),
        ("<flakyFailure/>", P),  # a rerun that passed in the end (surefire-style)
        ("<rerunFailure/>", P),
    ],
)
def test_the_outcome_is_the_worst_result_element(children: str, expected: CaseOutcome) -> None:
    data = xml(f'<testsuite><testcase classname="m" name="t">{children}</testcase></testsuite>')
    (case,) = parse_junit(data, Runner.PYTEST).cases
    assert case.outcome is expected


def test_suite_level_extras_are_ignored() -> None:
    """pytest writes `<properties>` (record_testsuite_property); vitest and mocha may add
    other elements. Only cases count."""
    data = xml(
        '<testsuite><properties><property name="k" value="v"/></properties>'
        '<testcase classname="m" name="t"><properties><property name="p"/></properties>'
        "<system-out>out</system-out></testcase><system-err>err</system-err></testsuite>"
    )
    parsed = parse_junit(data, Runner.PYTEST)
    assert [(c.test_id, c.outcome) for c in parsed.cases] == [("m::t", P)]


def test_a_vitest_case_without_a_classname_keeps_its_name() -> None:
    data = xml('<testsuite><testcase classname="" name="loose"/></testsuite>')
    (one,) = parse_junit(data, Runner.VITEST).cases
    assert (one.test_id, one.file) == ("loose", None)


def test_a_message_falls_back_to_the_first_line_of_the_text() -> None:
    data = xml(
        '<testsuite><testcase classname="m" name="t"><failure>\n\n  Error: first\nsecond'
        "</failure></testcase></testsuite>"
    )
    (case,) = parse_junit(data, Runner.PYTEST).cases
    assert case.message == "Error: first"
    assert case.trace_excerpt == "Error: first\nsecond"


def test_long_texts_are_capped() -> None:
    long = "x" * (MAX_TRACE_CHARS * 3)
    data = xml(
        f'<testsuite><testcase classname="m" name="t"><failure message="{long}">{long}'
        "</failure></testcase></testsuite>"
    )
    (case,) = parse_junit(data, Runner.PYTEST).cases
    assert len(case.message) <= MAX_MESSAGE_CHARS
    assert len(case.trace_excerpt) <= MAX_TRACE_CHARS


def test_the_trace_cap_holds_across_text_chunks() -> None:
    """CDATA and plain text arrive as separate chunks; the cap applies to their sum."""
    head = "\n" * 20 + "a" * (MAX_TRACE_CHARS - 10)
    data = xml(
        f'<testsuite><testcase classname="m" name="t"><failure>{head}<![CDATA[{"b" * 50}]]>'
        f"{'c' * 50}<![CDATA[{'d' * 50}]]></failure></testcase></testsuite>"
    )
    (one,) = parse_junit(data, Runner.PYTEST).cases
    assert one.trace_excerpt == head.strip() + "b" * 10  # leading blank lines do not count


def test_durations_are_read_and_bad_ones_are_zero() -> None:
    data = xml(
        '<testsuite><testcase classname="m" name="a" time="1.5"/>'
        '<testcase classname="m" name="b" time="nan"/>'
        '<testcase classname="m" name="c" time="-3"/>'
        '<testcase classname="m" name="d" time="1,5"/></testsuite>'
    )
    times = [c.duration_s for c in parse_junit(data, Runner.PYTEST).cases]
    assert times == [1.5, 0.0, 0.0, 0.0]


def test_an_empty_report_is_valid_and_has_no_cases() -> None:
    parsed = parse_junit(xml('<testsuite name="pytest" tests="0"/>'), Runner.PYTEST)
    assert parsed.cases == ()
    assert parsed.counts.executed == 0


def test_unittest_has_no_junit_format() -> None:
    with pytest.raises(ValueError, match="unittest"):
        parse_junit(xml("<testsuite/>"), Runner.UNITTEST)


# ---------------------------------------------------------------- hostile and broken input


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"", "empty"),
        (b"not xml at all", "syntax"),
        (b"<testsuites><testsuite>", "syntax"),
        (xml("<testsuite/>", root="html"), "root"),
        (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuites/>', "DOCTYPE"),
        (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "a"><!ENTITY b "&a;&a;&a;">]>'
            b'<testsuites><testsuite><testcase classname="m" name="&b;"/></testsuite></testsuites>',
            "DOCTYPE",
        ),
        (
            b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "file:///etc/passwd"><testsuites/>',
            "DOCTYPE",
        ),
        (
            b'<testsuites><testsuite><testcase classname="m" name="&undefined;"/>'
            b"</testsuite></testsuites>",
            "syntax",
        ),
        (xml('<testsuite><testcase classname="m"/></testsuite>'), "name"),
        (xml('<testsuite><testcase classname="m" name="  "/></testsuite>'), "name"),
        (xml('<testcase classname="m" name="t"/>'), "outside"),
        (
            xml(
                '<properties><testsuite><testcase classname="m" name="t"/></testsuite></properties>'
            ),
            "testsuite inside",
        ),
        (
            xml('<testsuite><testcase classname="m" name="t"><testcase/></testcase></testsuite>'),
            "nested",
        ),
        (xml("<testsuite>" + "<testsuite>" * 80 + "</testsuite>" * 80 + "</testsuite>"), "deep"),
        (b"\xff\xfe<\x00t\x00", "syntax"),
    ],
)
def test_bad_or_hostile_xml_is_refused(data: bytes, why: str) -> None:
    with pytest.raises(JunitParseError, match=why):
        parse_junit(data, Runner.PYTEST)


def test_an_oversized_report_is_refused_before_parsing() -> None:
    with pytest.raises(JunitParseError, match="bytes"):
        parse_junit(b" " * (MAX_JUNIT_BYTES + 1), Runner.PYTEST)


def test_too_many_cases_are_refused() -> None:
    body = '<testcase classname="m" name="t"/>' * 11
    with pytest.raises(JunitParseError, match="cases"):
        parse_junit(xml(f"<testsuite>{body}</testsuite>"), Runner.PYTEST, max_cases=10)


def test_a_latin1_report_is_decoded_by_its_declaration() -> None:
    data = (
        '<?xml version="1.0" encoding="ISO-8859-1"?><testsuite>'
        '<testcase classname="m" name="caf\xe9"/></testsuite>'
    ).encode("latin-1")
    assert [c.test_id for c in parse_junit(data, Runner.PYTEST).cases] == ["m::café"]


def test_newlines_and_tabs_in_names_are_folded_so_an_id_is_one_line() -> None:
    """XML 1.0 cannot carry most control characters at all; a tab or newline (as a character
    reference) can, and is folded to a space. Outer whitespace is dropped."""
    data = xml('<testsuite><testcase classname="m" name=" a&#10;b&#9;c "/></testsuite>')
    assert [c.test_id for c in parse_junit(data, Runner.PYTEST).cases] == ["m::a b c"]


# ---------------------------------------------------------------- TestResult


def test_the_test_result_carries_counts_and_id_sets() -> None:
    parsed = parse("pytest-junit.xml", Runner.PYTEST)
    result = to_test_result(
        parsed,
        run_id=RUN,
        stage=TestStage.FINAL,
        attempt_id=AttemptId("att-1"),
        duration_s=2.5,
        junit_path=".mpilot/junit.xml",
    )
    assert (result.passed, result.failed, result.skipped, result.errors) == (11, 5, 3, 2)
    assert result.executed_count == 18
    assert result.passed_ids == {k for k, v in PYTEST_SAMPLE.items() if v is P}
    assert result.failed_ids == {k for k, v in PYTEST_SAMPLE.items() if v in (F, E)}
    assert (result.stage, result.attempt_id, result.junit_path) == (
        TestStage.FINAL,
        "att-1",
        ".mpilot/junit.xml",
    )
    assert {f.test_id for f in result.failures} == result.failed_ids
    fail = next(f for f in result.failures if f.test_id == "tests.test_sample::test_fail")
    assert fail.message == "AssertionError: one is not two\nassert 1 == 2"
    assert "tests/test_sample.py:11: AssertionError" in fail.trace_excerpt


def case(test_id: str, outcome: CaseOutcome, message: str = "") -> ParsedCase:
    return ParsedCase(test_id=test_id, outcome=outcome, message=message)


def parsed_of(*cases: ParsedCase) -> ParsedTests:
    return ParsedTests(runner=Runner.JEST, source=ResultSource.JUNIT, cases=tuple(cases))


def test_a_duplicated_id_fails_if_any_copy_fails_and_passes_only_if_all_pass() -> None:
    """Two tests with one ID cannot be told apart: the ID is failed if any copy failed or
    errored, passed only if every copy passed, and neither if a copy was skipped. Every
    copy is still counted, so the executed count is exact."""
    parsed = parsed_of(
        case("a", P),
        case("a", F, "first"),
        case("b", P),
        case("b", P),
        case("c", P),
        case("c", S),
        case("d", E, "err"),
        case("d", E, "err again"),
    )
    result = to_test_result(parsed, run_id=RUN, stage=TestStage.BASELINE, duration_s=0.0)
    assert result.failed_ids == {"a", "d"}
    assert result.passed_ids == {"b"}
    assert (result.passed, result.failed, result.skipped, result.errors) == (4, 1, 1, 2)
    assert [f.test_id for f in result.failures] == ["a", "d"]  # one entry per ID, first copy
    assert [f.message for f in result.failures] == ["first", "err"]
    assert parsed.duplicate_ids == ("a", "b", "c", "d")


def test_failures_are_capped_but_failed_ids_are_complete() -> None:
    many = [case(f"t{i:04}", F, "m") for i in range(MAX_FAILURES + 50)]
    result = to_test_result(parsed_of(*many), run_id=RUN, stage=TestStage.FULL, duration_s=0.0)
    assert len(result.failed_ids) == MAX_FAILURES + 50
    assert len(result.failures) == MAX_FAILURES


def test_parsed_tests_refuse_inconsistent_shapes() -> None:
    with pytest.raises(ValueError, match="test_id"):
        ParsedCase(test_id="", outcome=P)
    with pytest.raises(ValueError, match="duration"):
        ParsedCase(test_id="x", outcome=P, duration_s=-1.0)
