"""Parsed test results and the `TestResult` built from them (SPEC FR-11, D9, §11.2).

`junit.py` (and the P2-14b log fallback) turn a runner's output into `ParsedTests`: one
`ParsedCase` per reported test, with a normalized ID (`test_ids.py`). `to_test_result` folds
them into the persisted `TestResult`.

Counting and ID rules:
- Counts are per reported case, never from a report's own totals (pytest's `tests=`
  attribute, for one, also counts a passing test's teardown error).
- passed + failed + errors is the executed count (D9); skipped cases are not executed.
- An ID reported more than once cannot be told apart: it is failed if any copy failed or
  errored, passed only if every copy passed, and in neither set otherwise. Every copy still
  counts. `ParsedTests.duplicate_ids` lists them so the baseline can say so.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from mpilot.core.types import AttemptId, RunId
from mpilot.models.domain import FailedTest, TestResult, TestStage

#: Per-case text kept: the message, and the head of the trace the runner wrote.
MAX_MESSAGE_CHARS: Final = 500
MAX_TRACE_CHARS: Final = 4000
#: `TestResult.failures` entries kept (the ID sets are always complete).
MAX_FAILURES: Final = 200


class Runner(StrEnum):
    """The test runners the results can come from (values as detection names them)."""

    PYTEST = "pytest"
    UNITTEST = "unittest"
    JEST = "jest"
    VITEST = "vitest"
    MOCHA = "mocha"


class CaseOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class ResultSource(StrEnum):
    JUNIT = "junit"
    LOG = "log"


@dataclass(frozen=True)
class Counts:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0

    @property
    def executed(self) -> int:
        """D9: passed + failed + errors."""
        return self.passed + self.failed + self.errors

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.passed, self.failed, self.skipped, self.errors)


@dataclass(frozen=True)
class ParsedCase:
    test_id: str
    outcome: CaseOutcome
    file: str | None = None  # workspace-relative, when the report says
    message: str = ""
    trace_excerpt: str = ""
    duration_s: float = 0.0

    def __post_init__(self) -> None:
        if not self.test_id.strip():
            raise ValueError("a parsed case needs a test_id")
        if not math.isfinite(self.duration_s) or self.duration_s < 0:
            raise ValueError("duration_s must be a finite number >= 0")


@dataclass(frozen=True)
class ParsedTests:
    """What one test run reported. For a log (`ResultSource.LOG`) the cases are only the tests
    the output named, so the counts come from the runner's own summary (`totals`), and
    `passed_ids_known` says whether every passed test in those counts was named: a verbose
    reporter lists them, the default one of most runners does not. A JUnit report always
    names every case (`totals` is None, `passed_ids_known` True)."""

    runner: Runner
    source: ResultSource
    cases: tuple[ParsedCase, ...]
    warnings: tuple[str, ...] = ()
    totals: Counts | None = None
    passed_ids_known: bool = True

    @property
    def counts(self) -> Counts:
        if self.totals is not None:
            return self.totals
        tally = Counter(c.outcome for c in self.cases)
        return Counts(
            passed=tally[CaseOutcome.PASSED],
            failed=tally[CaseOutcome.FAILED],
            skipped=tally[CaseOutcome.SKIPPED],
            errors=tally[CaseOutcome.ERROR],
        )

    @property
    def duplicate_ids(self) -> tuple[str, ...]:
        """IDs reported more than once, sorted."""
        tally = Counter(c.test_id for c in self.cases)
        return tuple(sorted(i for i, n in tally.items() if n > 1))


def _failing(outcome: CaseOutcome) -> bool:
    return outcome in (CaseOutcome.FAILED, CaseOutcome.ERROR)


def id_sets(cases: Sequence[ParsedCase]) -> tuple[frozenset[str], frozenset[str]]:
    """(passed_ids, failed_ids) under the duplicate rule in the module docstring."""
    failed = frozenset(c.test_id for c in cases if _failing(c.outcome))
    not_passed = {c.test_id for c in cases if c.outcome is not CaseOutcome.PASSED}
    passed = frozenset(c.test_id for c in cases if c.test_id not in not_passed)
    return passed, failed


def to_test_result(
    parsed: ParsedTests,
    *,
    run_id: RunId,
    stage: TestStage,
    duration_s: float,
    attempt_id: AttemptId | None = None,
    junit_path: str | None = None,
) -> TestResult:
    """The persisted result of one test run. `duration_s` is the command's wall time (the
    cases' own times leave out collection and start-up). `passed_ids` is empty when the
    source did not name every passed test (`ParsedTests.passed_ids_known`): the caller must
    not build P0 from it (§11.2). `failures` holds the first failing
    copy of each failed ID, in report order, at most `MAX_FAILURES` of them."""
    passed_ids, failed_ids = id_sets(parsed.cases)
    if not parsed.passed_ids_known:
        passed_ids = frozenset()  # a partial listing must not read as the full set
    failures: list[FailedTest] = []
    seen: set[str] = set()
    for case in parsed.cases:
        if len(failures) >= MAX_FAILURES:
            break
        if _failing(case.outcome) and case.test_id not in seen:
            seen.add(case.test_id)
            failures.append(
                FailedTest(
                    test_id=case.test_id,
                    message=case.message[:MAX_MESSAGE_CHARS],
                    trace_excerpt=case.trace_excerpt[:MAX_TRACE_CHARS],
                )
            )
    counts = parsed.counts
    return TestResult(
        run_id=run_id,
        attempt_id=attempt_id,
        stage=stage,
        passed=counts.passed,
        failed=counts.failed,
        skipped=counts.skipped,
        errors=counts.errors,
        passed_ids=passed_ids,
        failed_ids=failed_ids,
        duration_s=duration_s,
        failures=failures,
        junit_path=junit_path,
    )
