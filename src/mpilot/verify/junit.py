"""JUnit XML from the sandbox, parsed into normalized test cases (SPEC FR-11, D9, §9.5).

The XML is written by repo code running in the sandbox, so it is untrusted input:
- it is parsed with expat directly, and any DOCTYPE is refused, so no entity is ever
  declared (no expansion bombs, no external entities); an undeclared entity is a syntax error;
- size, element depth, case count and kept text are capped; the caller also reads the file
  through the frozen, size-capped workspace (`Workspace.read_blob`, §9.5);
- anything that does not look like a report (wrong root, a case outside a suite, a case with
  no name) is refused with `JunitParseError`, and the caller falls back to the log (P2-14b).
  Refusing is never silent: a report that cannot be read yields no result, never a partial
  one.

Formats (recorded samples in `tests/fixtures/test_results/`, made by
`scripts/record-test-results.sh`):
- pytest: `<testsuites><testsuite name="pytest">`, one case per test, `classname` the dotted
  module and classes. An expected failure is `<skipped>`, a strict XPASS a `<failure>`; a
  collection error is an `<error>` case with an empty classname. The xunit1 family adds `file`.
- vitest: one suite per file; `classname` is the file and `name` the `>`-joined titles; a file
  that fails to load is one failed case named after the file.
- jest (jest-junit): with the settings `detect/node.py` passes, `classname` and `file` are the
  file, `name` is `<describe › ...> › <title>` (a leading ` › ` when there is no describe),
  and a suite that fails to load is reported twice (an `<error>` and a `<failure>` case whose
  classname is "Test suite failed to run" and whose name is the file). `test.todo` has no
  marker and reads as passed. With jest-junit's defaults there is no file and no suite error.
- mocha (mocha-junit-reporter): `name` is the full title, the suite may carry an absolute
  `file`; pending tests are left out of the XML altogether, and a failed hook is one failed
  case named after the hook.
Result elements other than failure/error/skipped (`system-out`, surefire's `flakyFailure`,
`rerunFailure`, ...) are ignored: the case's final outcome is what counts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final
from xml.parsers import expat

from mpilot.verify.results import (
    MAX_MESSAGE_CHARS,
    MAX_TRACE_CHARS,
    CaseOutcome,
    ParsedCase,
    ParsedTests,
    ResultSource,
    Runner,
)
from mpilot.verify.test_ids import (
    JEST_SEPARATOR,
    clean,
    jest_id,
    mocha_id,
    pytest_id,
    vitest_id,
    workspace_relative,
)

#: A report larger than this is refused before parsing.
MAX_JUNIT_BYTES: Final = 32 * 1024 * 1024
#: More cases than this in one report is refused (no real suite comes close).
MAX_CASES: Final = 200_000
#: Element nesting deeper than this is refused.
MAX_DEPTH: Final = 32

_ROOTS: Final = frozenset({"testsuites", "testsuite"})
#: Result elements, ranked: the worst one present decides the outcome.
_RANK: Final = {"failure": 3, "error": 2, "skipped": 1}
_OUTCOME: Final = {
    "failure": CaseOutcome.FAILED,
    "error": CaseOutcome.ERROR,
    "skipped": CaseOutcome.SKIPPED,
}
#: jest-junit's fixed classname for a test file that failed to load.
JEST_SUITE_ERROR: Final = "Test suite failed to run"
_JEST_NO_FILE: Final = (
    "jest-junit wrote no file attribute (its defaults, not the mpilot settings): test IDs are "
    "not file-qualified, can collide across files and will not match the log; suites that "
    "fail to load are left out"
)


class JunitParseError(ValueError):
    """The bytes are not a JUnit report this parser accepts; fall back to the log."""


@dataclass
class _Result:
    tag: str
    message: str
    text: list[str] = field(default_factory=list)
    size: int = 0


@dataclass
class _Case:
    classname: str
    name: str
    file: str | None
    suite_file: str | None
    time: str
    results: list[_Result] = field(default_factory=list)


class _Handler:
    def __init__(self, runner: Runner, max_cases: int) -> None:
        self.runner = runner
        self.max_cases = max_cases
        self.stack: list[str] = []
        self.suite_files: list[str | None] = []
        self.case: _Case | None = None
        self.collecting: _Result | None = None
        self.cases: list[ParsedCase] = []
        self.warnings: list[str] = []
        self.count = 0

    # -- expat callbacks

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if len(self.stack) >= MAX_DEPTH:
            raise JunitParseError(f"element nesting deeper than {MAX_DEPTH}")
        parent = self.stack[-1] if self.stack else None
        self.stack.append(tag)
        if parent is None:
            if tag not in _ROOTS:
                raise JunitParseError(f"root element must be testsuites or testsuite, not {tag}")
            if tag == "testsuite":
                self._open_suite(attrs)
            return
        if self.case is not None:
            if tag == "testcase":
                raise JunitParseError("nested testcase")
            if parent == "testcase" and tag in _RANK:
                self.collecting = _Result(tag, attrs.get("message", ""))
                self.case.results.append(self.collecting)
            return
        if tag == "testsuite":
            if parent not in _ROOTS:
                raise JunitParseError(f"testsuite inside {parent}")
            self._open_suite(attrs)
        elif tag == "testcase":
            if parent != "testsuite":
                raise JunitParseError("testcase outside a testsuite")
            self.count += 1
            if self.count > self.max_cases:
                raise JunitParseError(f"more than {self.max_cases} cases")
            self.case = _Case(
                classname=attrs.get("classname", ""),
                name=attrs.get("name", ""),
                file=attrs.get("file"),
                suite_file=self.suite_files[-1] if self.suite_files else None,
                time=attrs.get("time", ""),
            )

    def end(self, tag: str) -> None:
        self.stack.pop()
        if self.case is not None:
            if tag == "testcase" and "testcase" not in self.stack:
                self.cases.append(self._finish(self.case))
                self.case = None
            elif (
                self.collecting is not None
                and tag == self.collecting.tag
                and self.stack[-1] == "testcase"
            ):
                self.collecting = None
            return
        if tag == "testsuite":
            self.suite_files.pop()

    def text(self, data: str) -> None:
        result = self.collecting
        if result is None or self.stack[-1] != result.tag:
            return
        # Twice the cap, raw: leading whitespace is stripped before the cap applies.
        kept = data[: max(0, 2 * MAX_TRACE_CHARS - result.size)]
        result.text.append(kept)
        result.size += len(kept)

    # -- helpers

    def _open_suite(self, attrs: dict[str, str]) -> None:
        own = attrs.get("file")
        inherited = self.suite_files[-1] if self.suite_files else None
        self.suite_files.append(workspace_relative(own) if own else inherited)

    def _finish(self, case: _Case) -> ParsedCase:
        if not clean(case.name):
            raise JunitParseError("a testcase without a name")
        test_id, file = self._identify(case)
        outcome, message, trace = CaseOutcome.PASSED, "", ""
        if case.results:
            worst = max(case.results, key=lambda r: _RANK[r.tag])  # first of the worst
            outcome = _OUTCOME[worst.tag]
            trace = "".join(worst.text).strip()[:MAX_TRACE_CHARS]
            message = worst.message.strip() or next(
                (line.strip() for line in trace.splitlines() if line.strip()), ""
            )
        return ParsedCase(
            test_id=test_id,
            outcome=outcome,
            file=file,
            message=message[:MAX_MESSAGE_CHARS],
            trace_excerpt=trace,
            duration_s=_duration(case.time),
        )

    def _identify(self, case: _Case) -> tuple[str, str | None]:
        own_file = workspace_relative(case.file) if case.file else None
        if self.runner is Runner.PYTEST:
            return pytest_id(case.classname, case.name), own_file
        if self.runner is Runner.VITEST:
            file = clean(case.classname)
            if not file:
                return clean(case.name), None
            if clean(case.name) == file:  # the file itself failed (did not load)
                return vitest_id(file, []), workspace_relative(file)
            return vitest_id(file, [case.name]), workspace_relative(file)
        if self.runner is Runner.JEST:
            if case.file is None:
                if _JEST_NO_FILE not in self.warnings:
                    self.warnings.append(_JEST_NO_FILE)
                return clean(case.name), None
            path = own_file or clean(case.file)
            if clean(case.classname) == JEST_SUITE_ERROR:
                return jest_id(path, []), own_file
            name = case.name.removeprefix(JEST_SEPARATOR)
            return jest_id(path, [name]), own_file
        # mocha: the suite carries the file, the case does not
        return mocha_id([case.name]), own_file or case.suite_file


def _duration(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value >= 0 else 0.0


def _refuse_doctype(
    name: str, sysid: str | None, pubid: str | None, has_internal_subset: bool
) -> None:
    raise JunitParseError("a DOCTYPE is not allowed in a JUnit report")


def _refuse_entity(*_: object) -> None:  # pragma: no cover - only inside a DOCTYPE
    raise JunitParseError("an entity declaration is not allowed in a JUnit report")


def _refuse_external(  # pragma: no cover - only via a DOCTYPE
    context: str, base: str | None, sysid: str | None, pubid: str | None
) -> int:
    raise JunitParseError("an external entity is not allowed in a JUnit report")


def parse_junit(data: bytes, runner: Runner, *, max_cases: int = MAX_CASES) -> ParsedTests:
    """Every case in a JUnit report, with normalized IDs for `runner`. Raises
    `JunitParseError` for anything that is not a well-formed report within the caps, and
    ValueError for unittest (which has no JUnit output)."""
    if runner is Runner.UNITTEST:
        raise ValueError("unittest writes no JUnit XML: its results come from the log")
    if len(data) > MAX_JUNIT_BYTES:
        raise JunitParseError(f"report is {len(data)} bytes, over the {MAX_JUNIT_BYTES} cap")
    if not data.strip():
        raise JunitParseError("empty report")
    handler = _Handler(runner, max_cases)
    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse_doctype
    parser.EntityDeclHandler = _refuse_entity
    parser.ExternalEntityRefHandler = _refuse_external
    parser.StartElementHandler = handler.start
    parser.EndElementHandler = handler.end
    parser.CharacterDataHandler = handler.text
    try:
        parser.Parse(data, True)
    except expat.ExpatError as e:
        raise JunitParseError(f"XML syntax error: {expat.ErrorString(e.code)}") from e
    return ParsedTests(
        runner=runner,
        source=ResultSource.JUNIT,
        cases=tuple(handler.cases),
        warnings=tuple(handler.warnings),
    )
