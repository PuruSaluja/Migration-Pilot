"""P1-02: enums, IDs, exit-code matrix, Usage.cap_tokens."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mpilot.core.types import (
    EXIT_INTERRUPTED,
    Effort,
    ExitCode,
    FailureCategory,
    Outcome,
    Role,
    RunStatus,
    StepType,
    Tier,
    Usage,
    exit_code_for,
    new_attempt_id,
    new_bench_id,
    new_llm_call_id,
    new_plan_id,
    new_routing_decision_id,
    new_run_id,
    new_step_id,
    new_tool_call_id,
)

SPEC = Path(__file__).resolve().parents[2] / "SPEC.md"


def test_tier_has_exactly_opus_and_sonnet() -> None:
    assert {t.value for t in Tier} == {"opus", "sonnet"}
    assert len(Tier) == 2


def test_effort_has_exactly_high_and_medium() -> None:
    assert {e.value for e in Effort} == {"high", "medium"}
    assert len(Effort) == 2


@pytest.mark.parametrize("bad", ["haiku", "low", "max", "xhigh"])
def test_invalid_tier_or_effort_unrepresentable(bad: str) -> None:
    with pytest.raises(ValueError, match="is not a valid"):
        Tier(bad)
    with pytest.raises(ValueError, match="is not a valid"):
        Effort(bad)


def test_no_member_mentions_haiku() -> None:
    for enum in (Tier, Effort, Role, StepType, Outcome, RunStatus, FailureCategory):
        assert not any("haiku" in m.value.lower() for m in enum)


def test_step_types_and_outcomes_match_section_12() -> None:
    assert {s.value for s in StepType} == {
        "bump",
        "codemod",
        "edit_cluster",
        "config",
        "test_touch",
        "verify",
    }
    assert {o.value for o in Outcome} == {"improved", "same", "regressed", "green"}


def _spec_failure_categories() -> list[str]:
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### 14.5 Failure taxonomy", 1)[1].split("###", 1)[0]
    return re.findall(r"`([A-Z_/]+)`", section)


def test_failure_category_matches_spec_14_5_exactly() -> None:
    entries = _spec_failure_categories()
    assert len(entries) == 17
    # A slash entry such as BASELINE_RED/FLAKY is one category named by its first token.
    expected = [entry.split("/")[0] for entry in entries]
    assert [c.value for c in FailureCategory] == expected
    assert all(c.name == c.value for c in FailureCategory)


def test_run_status_matches_the_state_machine_diagram() -> None:
    section = SPEC.read_text(encoding="utf-8").split("### 7.4 State machine", 1)[1]
    diagram = section.split("```", 2)[1]
    end_states = {f"END_{name}" for name in re.findall(r"END:([A-Z_]+)", diagram)}
    assert end_states == {s.value for s in RunStatus if s.is_terminal}
    working_states = {s.value for s in RunStatus if not s.is_terminal}
    for name in working_states:
        assert name in diagram, name


# ---------------------------------------------------------------- exit codes (C6)

EXIT_MATRIX = {
    RunStatus.END_SUCCESS: 0,
    RunStatus.END_ESCALATED: 1,
    RunStatus.END_ERROR: 1,
    RunStatus.END_REJECTED: 1,
    RunStatus.END_CANCELLED: 1,
    RunStatus.END_BASELINE_RED: 3,
    RunStatus.END_BUDGET_EXCEEDED: 4,
    RunStatus.PAUSED_LIMIT: 5,
}


@pytest.mark.parametrize(("status", "code"), EXIT_MATRIX.items())
def test_exit_code_matrix(status: RunStatus, code: int) -> None:
    assert exit_code_for(status) == code


def test_matrix_covers_every_terminal_state_plus_paused() -> None:
    terminal = {s for s in RunStatus if s.is_terminal}
    assert set(EXIT_MATRIX) == terminal | {RunStatus.PAUSED_LIMIT}


def test_budget_exceeded_is_4_and_paused_is_5() -> None:
    assert exit_code_for(RunStatus.END_BUDGET_EXCEEDED) == ExitCode.BUDGET_EXCEEDED == 4
    assert exit_code_for(RunStatus.PAUSED_LIMIT) == ExitCode.PAUSED == 5


def test_exit_codes_match_section_13_1() -> None:
    assert {c.name: c.value for c in ExitCode} == {
        "SUCCESS": 0,
        "FAILED": 1,
        "INPUT_ERROR": 2,
        "BASELINE_RED": 3,
        "BUDGET_EXCEEDED": 4,
        "PAUSED": 5,
    }
    assert EXIT_INTERRUPTED == 130


@pytest.mark.parametrize(
    "status", [s for s in RunStatus if not s.is_terminal and s is not RunStatus.PAUSED_LIMIT]
)
def test_running_states_have_no_exit_code(status: RunStatus) -> None:
    with pytest.raises(ValueError, match="no exit code"):
        exit_code_for(status)


# ---------------------------------------------------------------- Usage.cap_tokens (B2)


def test_cap_tokens_formula() -> None:
    usage = Usage(
        input_tokens=1000, output_tokens=500, cache_read_tokens=2000, cache_write_tokens=300
    )
    assert usage.cap_tokens() == 1000 + 500 + 300 + 200


@pytest.mark.parametrize(("cache_read", "counted"), [(0, 0), (1, 1), (10, 1), (11, 2), (25, 3)])
def test_cache_reads_count_ten_percent_rounded_up(cache_read: int, counted: int) -> None:
    assert Usage(cache_read_tokens=cache_read).cap_tokens() == counted


@given(
    st.integers(0, 10**9),
    st.integers(0, 10**9),
    st.integers(0, 10**9),
    st.integers(0, 10**9),
)
def test_cap_tokens_never_below_exact_formula(i: int, o: int, r: int, w: int) -> None:
    usage = Usage(i, o, r, w)
    assert usage.cap_tokens() >= i + o + w + r / 10
    assert usage.cap_tokens() < i + o + w + r / 10 + 1


def test_usage_add_and_validation() -> None:
    total = Usage(1, 2, 3, 4) + Usage(10, 20, 30, 40)
    assert total == Usage(11, 22, 33, 44)
    with pytest.raises(ValueError, match="must be >= 0"):
        Usage(input_tokens=-1)


# ---------------------------------------------------------------- IDs


def test_ids_are_unique_prefixed_and_branch_safe() -> None:
    makers = [
        (new_run_id, "run-"),
        (new_step_id, "step-"),
        (new_plan_id, "plan-"),
        (new_attempt_id, "att-"),
        (new_tool_call_id, "tc-"),
        (new_llm_call_id, "llm-"),
        (new_routing_decision_id, "rd-"),
        (new_bench_id, "bench-"),
    ]
    for make, prefix in makers:
        a, b = make(), make()
        assert a != b
        assert a.startswith(prefix)
        assert re.fullmatch(r"[a-z]+-[0-9a-f]{12}", a)
