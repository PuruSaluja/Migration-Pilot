"""The log-parsing fallback (SPEC FR-11, D9): test results from a runner's own output.

Used when there is no JUnit XML: a test command that is wrapped, chained, overridden or taken
from CI gets no reporter flags (`EnvDetection.junit` is None), and an XML report that cannot
be read falls back here too. Recorded samples for every runner are in
`tests/fixtures/test_results/`, made by `scripts/record-test-results.sh`.

What a log can and cannot say:
- **Counts** come from the runner's summary line (`5 failed, 11 passed in 0.04s`, `Ran 8 tests`,
  `Tests: ...`, `Tests  ... (11)`, `4 passing`), mapped to the JUnit meanings: an expected
  failure and a todo are skipped, an expected pass counts as passed, an unexpected success is
  a failure, and a test file that fails to load is one error. They are summed over every
  summary in the output, so a chained command (`... && ...`) is counted whole.
- **Failed IDs** come from each runner's failure listing (pytest's short summary, unittest's
  `FAIL:` blocks, jest's `●` headers, vitest's `FAIL` lines, mocha's failure list), so they
  are usually complete even from the default reporter.
- **Passed IDs** exist only when the reporter lists passing tests (pytest `-v`/`-rA`,
  unittest `-v`, jest `--verbose` or a single test file, vitest `--reporter=verbose`, mocha's
  spec reporter). `ParsedTests.passed_ids_known` is true only when the number of passed tests
  named equals the summary's count, so a cut, forged or partial listing is never mistaken for
  the full set, and never when the output was truncated (the part cut off is unknown, and a
  chained command's first run may be the part that was cut). The baseline must not build P0
  (§11.2) from a result whose passed IDs are unknown.

The output is repo-controlled text (a test can print anything), so a log never overrides a
JUnit report and is checked only against itself: lines are matched whole and with bounded
patterns, line length, size and case count are capped, and a summary is never taken from a
partial match. Mixed output from several runners is refused rather than guessed.

Not recovered from a log: stack traces (the trimmed log, P3-12, carries them), durations, and
the test file of a pytest or unittest case.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from mpilot.verify.results import (
    CaseOutcome,
    Counts,
    ParsedCase,
    ParsedTests,
    ResultSource,
    Runner,
)
from mpilot.verify.test_ids import (
    clean,
    jest_id,
    mocha_id,
    pytest_id_from_nodeid,
    unittest_id,
    vitest_id,
    workspace_relative,
)

#: Output longer than this is refused (callers keep only the tail of a command's output).
MAX_LOG_CHARS: Final = 16 * 1024 * 1024
#: A longer line is cut before matching: no test line is that long, and it bounds the work.
MAX_LINE_CHARS: Final = 4000
#: More cases than this in one output is refused.
MAX_CASES: Final = 200_000

P, F, E, S = CaseOutcome.PASSED, CaseOutcome.FAILED, CaseOutcome.ERROR, CaseOutcome.SKIPPED
_FAILING: Final = (F, E)


class LogParseError(ValueError):
    """The output has no summary this parser can read, or is too large or ambiguous."""


# ---------------------------------------------------------------- cleaning

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_output(text: str) -> list[str]:
    """The output as lines, without ANSI sequences or control characters. A line a progress
    bar rewrote with carriage returns keeps what the last rewrite left."""
    lines: list[str] = []
    for raw in text.split("\n"):
        line = _ANSI.sub("", raw)
        if "\r" in line:
            line = next((s for s in reversed(line.split("\r")) if s), "")
        lines.append(_CONTROL.sub("", line)[:MAX_LINE_CHARS])
    return lines


# ---------------------------------------------------------------- shared state


@dataclass
class _Raw:
    """What one runner's parser found, before the counts are checked against the listing."""

    max_cases: int
    listed: list[ParsedCase] = field(default_factory=list)  # one per test the output lists
    failures: list[ParsedCase] = field(default_factory=list)  # named by a failure listing
    totals: list[Counts] = field(default_factory=list)  # one per summary line
    warnings: list[str] = field(default_factory=list)

    def _check(self) -> None:
        if len(self.listed) + len(self.failures) > self.max_cases:
            raise LogParseError(f"more than {self.max_cases} cases in the output")

    def list(self, case: ParsedCase | None) -> None:
        if case is not None:
            self.listed.append(case)
            self._check()

    def fail(self, case: ParsedCase | None) -> None:
        if case is not None:
            self.failures.append(case)
            self._check()


def _case(
    test_id: str, outcome: CaseOutcome, *, file: str | None = None, message: str = ""
) -> ParsedCase | None:
    if not test_id.strip():
        return None
    return ParsedCase(test_id=test_id, outcome=outcome, file=file, message=message[:500])


def _int_words(text: str, names: Sequence[str]) -> dict[str, int]:
    """`{"failed": 2, "passed": 11}` from `2 failed, 11 passed`: only the words in `names`."""
    return {word: int(n) for n, word in re.findall(r"(\d+) ([A-Za-z]+)", text) if word in names}


def _merge(listed: list[ParsedCase], failures: list[ParsedCase]) -> list[ParsedCase]:
    """The listing plus the failures a failure listing names that it lacks; a failure the
    listing already has only lends its message."""
    out = list(listed)
    index: dict[str, int] = {}
    for i, case in enumerate(out):
        if case.outcome in _FAILING:
            index.setdefault(case.test_id, i)
    for failure in failures:
        at = index.get(failure.test_id)
        if at is None:
            index[failure.test_id] = len(out)
            out.append(failure)
        elif failure.message and not out[at].message:
            out[at] = replace(out[at], message=failure.message)
    return out


def _first_text(lines: Sequence[str], start: int, stop: re.Pattern[str], limit: int = 12) -> str:
    """The first non-blank line from `start` on, stripped: the failure's message. It is empty
    when the block ends first (a line matching `stop` is the next block or the summary, never
    this failure's text) or none is found within `limit` lines."""
    for line in lines[start : start + limit]:
        if stop.match(line):
            return ""
        if line.strip():
            return line.strip()
    return ""


# ---------------------------------------------------------------- pytest

_PY_WORDS: Final = r"(?:passed|failed|skipped|deselected|xfailed|xpassed|warnings?|errors?|rerun)"
_PY_SUMMARY = re.compile(
    rf"^(?:=+ )?(\d+ {_PY_WORDS}(?:, \d+ {_PY_WORDS})*|no tests ran)"
    r" in \d+(?:\.\d+)?s(?: \(\d+:\d\d:\d\d\))?(?: =+)?$"
)
_PY_STATUS = "PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
_PY_LIST = re.compile(rf"^(\S.*?::\S.*?) ({_PY_STATUS})(?: \(.*\))?(?:\s+\[\s*\d+%\])?$")
_PY_XDIST = re.compile(rf"^\[gw\d+\] (?:\[\s*\d+%\] )?({_PY_STATUS}) (\S.*)$")
_PY_SHORT_HEADER = re.compile(r"^=+ short test summary info =+$")
_PY_SHORT = re.compile(r"^(FAILED|ERROR|PASSED|XFAIL|XPASS) (\S.*)$")
_PY_BANNER = re.compile(r"^=+ .* =+$")
_PY_OUTCOME: Final = {
    "PASSED": P,
    "XPASS": P,
    "FAILED": F,
    "ERROR": E,
    "SKIPPED": S,
    "XFAIL": S,
}


def _split_node(rest: str) -> tuple[str, str]:
    """`nodeid - message`, splitting at the first ` - ` outside brackets (a parameter may
    contain one)."""
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and rest.startswith(" - ", i):
            return rest[:i], rest[i + 3 :]
    return rest, ""


def _parse_pytest(lines: list[str], raw: _Raw) -> None:
    in_short = False
    short: list[ParsedCase] = []
    for line in lines:
        if m := _PY_SUMMARY.match(line):
            in_short = False
            words = _int_words(
                m[1], ("passed", "failed", "skipped", "xfailed", "xpassed", "error", "errors")
            )
            raw.totals.append(
                Counts(
                    passed=words.get("passed", 0) + words.get("xpassed", 0),
                    failed=words.get("failed", 0),
                    skipped=words.get("skipped", 0) + words.get("xfailed", 0),
                    errors=words.get("error", 0) + words.get("errors", 0),
                )
            )
        elif _PY_SHORT_HEADER.match(line):
            in_short = True
        elif _PY_BANNER.match(line):
            in_short = False
        elif in_short:
            if m := _PY_SHORT.match(line):
                node, message = _split_node(m[2])
                case = _case(pytest_id_from_nodeid(node), _PY_OUTCOME[m[1]], message=message)
                if case is not None:  # pragma: no branch - a matched line has an ID
                    short.append(case)
                    if len(short) > raw.max_cases:
                        raise LogParseError(f"more than {raw.max_cases} cases in the output")
        elif m := _PY_XDIST.match(line):
            raw.list(_case(pytest_id_from_nodeid(m[2]), _PY_OUTCOME[m[1]]))
        elif m := _PY_LIST.match(line):
            raw.list(_case(pytest_id_from_nodeid(m[1]), _PY_OUTCOME[m[2]]))
    if raw.listed:
        raw.failures = [c for c in short if c.outcome in _FAILING]
    else:
        raw.listed = short  # `-rA` without `-v`: the short summary names every test


# ---------------------------------------------------------------- unittest

_UT_STATUS = r"(ok|FAIL|ERROR|skipped\b.*|expected failure|unexpected success)"
_UT_RAN = re.compile(r"^Ran (\d+) tests? in \d+(?:\.\d+)?s$")
_UT_RESULT = re.compile(r"^(OK|FAILED|NO TESTS RAN)\b(?: \((.*)\))?$")
_UT_VERBOSE = re.compile(rf"^\s*(\S+) \(([^()\s]+)\)(?: \((.+?)\))? \.\.\. {_UT_STATUS}$")
_UT_HEADER = re.compile(r"^\s*(\S+) \(([^()\s]+)\)(?: \((.+?)\))?$")
_UT_DOC = re.compile(rf"^.*\S \.\.\. {_UT_STATUS}$")
_UT_BLOCK = re.compile(r"^(ERROR|FAIL): (\S+) \(([^()\s]+)\)(?: \((.+)\))?$")
_UT_OUTCOME: Final = {
    "ok": P,
    "FAIL": F,
    "ERROR": E,
    "expected failure": S,
    "unexpected success": F,
}


def _unittest_outcome(status: str) -> CaseOutcome:
    return S if status.startswith("skipped") else _UT_OUTCOME[status]


_UT_COUNT = re.compile(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)")


def _parse_unittest(lines: list[str], raw: _Raw) -> None:
    pending: re.Match[str] | None = None
    for i, line in enumerate(lines):
        if m := _UT_VERBOSE.match(line):
            raw.list(_case(unittest_id(m[1], m[2], m[3]), _unittest_outcome(m[4])))
            pending = None
            continue
        if pending is not None and _UT_DOC.match(line):  # a docstring line carries the status
            status = line.rsplit(" ... ", 1)[1]
            raw.list(
                _case(unittest_id(pending[1], pending[2], pending[3]), _unittest_outcome(status))
            )
            pending = None
            continue
        pending = _UT_HEADER.match(line)
        if (m := _UT_BLOCK.match(line)) and i > 0 and lines[i - 1].startswith("===="):
            raw.fail(_case(unittest_id(m[2], m[3], m[4]), F if m[1] == "FAIL" else E))
        elif ran := _UT_RAN.match(line):
            result = next((_UT_RESULT.match(x) for x in lines[i + 1 : i + 6] if x.strip()), None)
            if result is None:
                raise LogParseError("no result line (OK / FAILED) after 'Ran N tests'")
            found = {k: int(n) for k, n in _UT_COUNT.findall(result[2] or "")}
            bad = found.get("failures", 0) + found.get("errors", 0)
            skipped = found.get("skipped", 0) + found.get("expected failures", 0)
            unexpected = found.get("unexpected successes", 0)
            passed = int(ran[1]) - bad - skipped - unexpected
            if passed < 0:
                raw.warnings.append(
                    "unittest counted more failures than tests (several failing subtests): "
                    "the passed count is a lower bound"
                )
            raw.totals.append(
                Counts(
                    passed=max(0, passed),
                    failed=found.get("failures", 0) + unexpected,
                    skipped=skipped,
                    errors=found.get("errors", 0),
                )
            )


# ---------------------------------------------------------------- shared: indented trees

_Stack = list[tuple[int, str]]


def _titles(stack: _Stack, indent: int) -> list[str]:
    """Pop the describe blocks a line at `indent` is not inside, and return the rest."""
    while stack and stack[-1][0] >= indent:
        stack.pop()
    return [title for _, title in stack]


# ---------------------------------------------------------------- jest

_JEST_FILE = re.compile(r"^(PASS|FAIL)\s+(\S.*?)(?: \(\d+(?:\.\d+)? m?s\))?$")
_JEST_TEST = re.compile(r"^(\s*)([✓√✕×]|○ skipped|✎ todo) (\S.*?)(?: \(\d+(?:\.\d+)? ms\))?$")
_JEST_FAILURE = re.compile(r"^  ● (\S.*)$")
_JEST_TESTS = re.compile(r"^Tests:\s+(.*)$")
_JEST_OUTCOME: Final = {"✓": P, "√": P, "✕": F, "×": F, "○ skipped": S, "✎ todo": S}
_JEST_SUITE_ERROR: Final = "Test suite failed to run"
_JEST_BLOCK_END = re.compile(r"^(?:  ● |PASS|FAIL|Tests:|Test Suites:|Snapshots:|Time:)")
_JEST_NOT_A_TITLE = re.compile(r"^(?:Tests:|Test Suites:|Snapshots:|Time:|Ran all)")


def _parse_jest(lines: list[str], raw: _Raw) -> None:
    file = ""
    in_tree = False
    stack: _Stack = []
    suite_errors = 0
    for i, line in enumerate(lines):
        if m := _JEST_FILE.match(line):
            file, in_tree, stack = m[2], True, []
        elif m := _JEST_FAILURE.match(line):
            title = m[1]
            if title != "Console" and file:  # a console block is output, not a failure
                in_tree = False
                message = _first_text(lines, i + 1, _JEST_BLOCK_END)
                if title == _JEST_SUITE_ERROR:
                    suite_errors += 1
                    raw.fail(
                        _case(jest_id(file, []), E, file=workspace_relative(file), message=message)
                    )
                else:
                    test_id = jest_id(file, title.split(" › "))
                    raw.fail(_case(test_id, F, file=workspace_relative(file), message=message))
        elif m := _JEST_TESTS.match(line):
            words = _int_words(m[1], ("failed", "passed", "skipped", "todo"))
            raw.totals.append(
                Counts(
                    passed=words.get("passed", 0),
                    failed=words.get("failed", 0),
                    skipped=words.get("skipped", 0) + words.get("todo", 0),
                )
            )
        elif in_tree and file:
            if m := _JEST_TEST.match(line):
                titles = _titles(stack, len(m[1]))
                case = _case(
                    jest_id(file, [*titles, m[3]]),
                    _JEST_OUTCOME[m[2]],
                    file=workspace_relative(file),
                )
                raw.list(case)
            elif (m := re.match(r"^(\s{2,})(\S.*)$", line)) and not _JEST_NOT_A_TITLE.match(m[2]):
                _titles(stack, len(m[1]))
                stack.append((len(m[1]), m[2]))
    if suite_errors and raw.totals:
        last = raw.totals[-1]
        raw.totals[-1] = replace(last, errors=last.errors + suite_errors)


# ---------------------------------------------------------------- vitest

_VT_MARKS = "✓√×✗↓"
_VT_TIME = r"(?: \d+(?:\.\d+)?m?s)?"
_VT_FILE = re.compile(rf"^ [✓❯×↓] (\S.*?) \(\d+ tests?(?: \| .*)?\){_VT_TIME}$")
_VT_NESTED = re.compile(rf"^   ([{_VT_MARKS}]) (\S.*?){_VT_TIME}$")
_VT_VERBOSE = re.compile(rf"^ ([{_VT_MARKS}]) (\S.*?){_VT_TIME}$")
_VT_FAIL = re.compile(r"^ FAIL  (?:\|[^|]*\| )?(\S.*)$")
_VT_SUITE = re.compile(r"^(\S.*?) \[ .+ \]$")
_VT_TESTS = re.compile(r"^\s*Tests\s+(.*?)\s*\(\d+\)$")
_VT_BLOCK_END = re.compile(r"^(?: FAIL  |⎯|\s*Test Files\s|\s*Tests\s)")
_VT_OUTCOME: Final = {"✓": P, "√": P, "×": F, "✗": F, "↓": S}


def _parse_vitest(lines: list[str], raw: _Raw) -> None:
    file = ""
    suite_errors = 0
    for i, line in enumerate(lines):
        if m := _VT_FILE.match(line):
            file = m[1]
        elif m := _VT_NESTED.match(line):
            if file:
                raw.list(
                    _case(vitest_id(file, [m[2]]), _VT_OUTCOME[m[1]], file=workspace_relative(file))
                )
        elif m := _VT_VERBOSE.match(line):
            raw.list(_case(clean(m[2]), _VT_OUTCOME[m[1]]))
        elif m := _VT_FAIL.match(line):
            message = _first_text(lines, i + 1, _VT_BLOCK_END)
            if suite := _VT_SUITE.match(m[1]):
                suite_errors += 1
                raw.fail(
                    _case(clean(suite[1]), E, file=workspace_relative(suite[1]), message=message)
                )
            else:
                raw.fail(_case(clean(m[1]), F, message=message))
        elif m := _VT_TESTS.match(line):
            words = _int_words(m[1], ("failed", "passed", "skipped", "todo"))
            raw.totals.append(
                Counts(
                    passed=words.get("passed", 0),
                    failed=words.get("failed", 0),
                    skipped=words.get("skipped", 0) + words.get("todo", 0),
                )
            )
    if suite_errors and raw.totals:
        last = raw.totals[-1]
        raw.totals[-1] = replace(last, errors=last.errors + suite_errors)


# ---------------------------------------------------------------- mocha

_MO_PASS = re.compile(r"^(\s*)[✔✓√] (\S.*?)(?: \(\d+ms\))?$")
_MO_PENDING = re.compile(r"^(\s*)- (\S.*)$")
_MO_FAILED = re.compile(r"^(\s*)\d+\) (\S.*)$")
_MO_PASSING = re.compile(r"^\s+(\d+) passing(?: \(.*\))?$")
_MO_COUNT = re.compile(r"^\s+(\d+) (pending|failing)$")
_MO_DETAIL = re.compile(r"^  \d+\) (\S.*)$")


def _parse_mocha(lines: list[str], raw: _Raw) -> None:
    stack: _Stack = []
    in_details = False
    passed = pending = failing = 0
    seen = False
    for i, line in enumerate(lines):
        if m := _MO_PASSING.match(line):
            passed, seen, in_details = passed + int(m[1]), True, True
        elif m := _MO_COUNT.match(line):
            if m[2] == "pending":
                pending += int(m[1])
            else:
                failing += int(m[1])
        elif in_details:
            if m := _MO_DETAIL.match(line):
                parts, j = [m[1].strip()], i
                while (
                    not parts[-1].endswith(":")
                    and j + 1 < min(len(lines), i + 10)
                    and lines[j + 1].strip()
                ):
                    j += 1
                    parts.append(lines[j].strip())
                parts[-1] = parts[-1].removesuffix(":")
                raw.fail(_case(mocha_id(parts), F, message=_first_text(lines, j + 1, _MO_DETAIL)))
        elif m := _MO_PASS.match(line):
            raw.list(_case(mocha_id([*_titles(stack, len(m[1])), m[2]]), P))
        elif m := _MO_FAILED.match(line):
            raw.list(_case(mocha_id([*_titles(stack, len(m[1])), m[2]]), F))
        elif m := _MO_PENDING.match(line):
            raw.list(_case(mocha_id([*_titles(stack, len(m[1])), m[2]]), S))
        elif m := re.match(r"^(\s{2,})(\S.*)$", line):
            _titles(stack, len(m[1]))
            stack.append((len(m[1]), m[2]))
    if seen:
        raw.totals.append(Counts(passed=passed, failed=failing, skipped=pending))


# ---------------------------------------------------------------- runner detection

_SIGNATURE: Final[dict[Runner, re.Pattern[str]]] = {
    Runner.PYTEST: _PY_SUMMARY,
    Runner.UNITTEST: _UT_RAN,
    Runner.JEST: re.compile(r"^Tests:\s+.*\d+ total$"),
    Runner.VITEST: _VT_TESTS,
    Runner.MOCHA: _MO_PASSING,
}
_COMMAND_NAME: Final[dict[Runner, re.Pattern[str]]] = {
    Runner.PYTEST: re.compile(r"\b(?:pytest|py\.test)\b"),
    Runner.UNITTEST: re.compile(r"\bunittest\b"),
    Runner.JEST: re.compile(r"\bjest\b"),
    Runner.VITEST: re.compile(r"\bvitest\b"),
    Runner.MOCHA: re.compile(r"\bmocha\b"),
}
_PARSERS: Final[dict[Runner, Callable[[list[str], _Raw], None]]] = {
    Runner.PYTEST: _parse_pytest,
    Runner.UNITTEST: _parse_unittest,
    Runner.JEST: _parse_jest,
    Runner.VITEST: _parse_vitest,
    Runner.MOCHA: _parse_mocha,
}


def _matching(lines: list[str], command: str | None) -> tuple[list[Runner], Runner | None]:
    """The runners whose summary is in the output, and the one to use (None if unclear)."""
    found = [r for r, pattern in _SIGNATURE.items() if any(pattern.match(x) for x in lines)]
    if len(found) == 1:
        return found, found[0]
    if command:
        named = [r for r in found if _COMMAND_NAME[r].search(command)]
        if len(named) == 1:
            return found, named[0]
    return found, None


def _lines(output: str) -> list[str]:
    if len(output) > MAX_LOG_CHARS:
        raise LogParseError(f"output is {len(output)} characters, over the {MAX_LOG_CHARS} cap")
    return clean_output(output)


def detect_runner(output: str, command: str | None = None) -> Runner | None:
    """The runner whose summary line the output holds. When the output holds several, the one
    the command names, if it names exactly one of them; otherwise None."""
    return _matching(_lines(output), command)[1]


# ---------------------------------------------------------------- entry point


def parse_log(
    output: str,
    *,
    runner: Runner | None = None,
    command: str | None = None,
    truncated: bool = False,
    max_cases: int = MAX_CASES,
) -> ParsedTests:
    """The results in a runner's output (stdout then stderr, joined by the caller).

    `runner` is the runner the caller knows (from detection); without it the output's own
    summary decides, helped by `command` when the output holds several. `truncated` is
    `ExecResult.truncated`: the output is only the end of what the command printed.
    Raises `LogParseError` when there is no summary to read (a cut tail, a crashed run), when
    several runners' output is mixed and the command does not say which, or over the caps.
    """
    lines = _lines(output)
    if runner is None:
        found, runner = _matching(lines, command)
        if runner is None:
            if not found:
                raise LogParseError("no test runner summary found in the output")
            names = ", ".join(sorted(r.value for r in found))
            raise LogParseError(
                f"mixed output from several test runners ({names}): run one runner per command"
            )
    raw = _Raw(max_cases=max_cases)
    _PARSERS[runner](lines, raw)
    if not raw.totals:
        raise LogParseError(f"no {runner.value} summary line in the output (is the end cut off?)")
    totals = Counts(
        passed=sum(c.passed for c in raw.totals),
        failed=sum(c.failed for c in raw.totals),
        skipped=sum(c.skipped for c in raw.totals),
        errors=sum(c.errors for c in raw.totals),
    )
    cases = _merge(raw.listed, raw.failures)
    warnings = list(raw.warnings)
    listed_passed = sum(1 for c in cases if c.outcome is P)
    known = not truncated and listed_passed == totals.passed
    if truncated:
        warnings.append(
            "the output was truncated (only its end was kept): passed IDs are unknown and "
            "failed IDs may be incomplete"
        )
    elif not known:
        warnings.append(
            f"the output names {listed_passed} of {totals.passed} passed tests (use a verbose "
            "reporter): passed IDs unknown"
        )
    failing_ids = len({c.test_id for c in cases if c.outcome in _FAILING})
    if failing_ids < totals.failed + totals.errors:
        warnings.append(
            f"{totals.failed + totals.errors} failed or errored tests counted but only "
            f"{failing_ids} listed"
        )
    return ParsedTests(
        runner=runner,
        source=ResultSource.LOG,
        cases=tuple(cases),
        warnings=tuple(warnings),
        totals=totals,
        passed_ids_known=known,
    )
