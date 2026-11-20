"""P1-08: orchestrator, state machine, budgets, checkpoints, store and events (SPEC §7.4,
§10.1, NFR-1..4, FR-21; B2, B5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from mpilot.agents.router import StickyState
from mpilot.agents.runtime import BudgetGuard
from mpilot.core.errors import AttemptCapError, BudgetExceededError, IllegalTransition
from mpilot.core.types import LLMCallId, RunId, RunStatus, StepId, ToolCallId, Usage
from mpilot.models.domain import (
    Budgets,
    Ecosystem,
    MigrationKind,
    MigrationSpec,
    PackageChange,
    Run,
    Spent,
    TestResult,
    TestStage,
)
from mpilot.models.state import (
    BudgetState,
    Checkpoint,
    FailureHistoryEntry,
    InFlightCall,
    sticky_scope,
)
from mpilot.orchestrator import base as ob
from mpilot.testing.infra_doubles import (
    DictTable,
    MemoryBus,
    MemoryStore,
    ScriptedOrchestrator,
    SimpleBudget,
)

RUN = RunId("run-1")
NOW = datetime(2026, 11, 20, tzinfo=UTC)
BUDGETS = Budgets(
    max_turns=300, max_tokens=8_000_000, minutes_cap=120, max_attempts_run=40, max_attempts_step=6
)


def test_conformance() -> None:
    assert isinstance(DictTable({}), ob.TransitionTable)
    assert isinstance(ScriptedOrchestrator(), ob.Orchestrator)
    assert isinstance(SimpleBudget(BUDGETS), ob.BudgetManager)
    budget = SimpleBudget(BUDGETS)
    budget.precheck(LLMCallId("c"), _est())
    assert isinstance(budget.guard(LLMCallId("c")), BudgetGuard)
    assert isinstance(MemoryStore(), ob.RunStore)
    assert isinstance(MemoryBus(), ob.EventBus)


# ---------------------------------------------------------------- RunRequest


def test_run_request_needs_a_goal_package_or_bot_pr() -> None:
    ob.RunRequest(repo="https://github.com/o/r", goal="react 18", budgets=BUDGETS)
    ob.RunRequest(repo=".", package="react", budgets=BUDGETS)
    ob.RunRequest(
        repo="https://github.com/o/r", bot_pr_url="https://github.com/o/r/pull/1", budgets=BUDGETS
    )
    pinned = ob.RunRequest(repo="https://github.com/o/r", goal="x", ref="a" * 40, budgets=BUDGETS)
    assert pinned.ref == "a" * 40  # bench tasks clone at pinned SHAs (FR-2, US-7)
    with pytest.raises(ValidationError, match="needs a goal"):
        ob.RunRequest(repo=".", budgets=BUDGETS)
    with pytest.raises(ValidationError, match="contradicts --no-pr"):
        ob.RunRequest(repo=".", goal="x", open_pr=False, pr_on_failure=True, budgets=BUDGETS)


# ---------------------------------------------------------------- state machine contract


def test_run_event_vocabulary_reaches_every_terminal_state() -> None:
    """Every §7.4 ending has an event that names it (P3-04 maps them)."""
    names = {e.name for e in ob.RunEvent}
    for needed in (
        "BASELINE_RED",
        "PLAN_REJECTED",
        "BUDGET_EXCEEDED",
        "FAILED",
        "CANCELLED",
        "REPORT_WRITTEN",
        "FINISHED",
        "USAGE_LIMIT",
        "RESUMED",
        "ESCALATE",
    ):
        assert needed in names


def test_table_contract_with_the_double() -> None:
    table = DictTable({(RunStatus.INIT, ob.RunEvent.CLONED): RunStatus.CLONED})
    assert table.next_state(RunStatus.INIT, ob.RunEvent.CLONED) is RunStatus.CLONED
    assert table.events_from(RunStatus.INIT) == {ob.RunEvent.CLONED}
    with pytest.raises(IllegalTransition):
        table.next_state(RunStatus.INIT, ob.RunEvent.BUMPED)
    back = table.next_state(
        RunStatus.PAUSED_LIMIT, ob.RunEvent.RESUMED, paused_at=RunStatus.REPAIR_LOOP
    )
    assert back is RunStatus.REPAIR_LOOP
    with pytest.raises(IllegalTransition, match="paused in"):
        table.next_state(RunStatus.PAUSED_LIMIT, ob.RunEvent.RESUMED)
    with pytest.raises(IllegalTransition, match="only for RESUMED"):
        table.next_state(RunStatus.INIT, ob.RunEvent.CLONED, paused_at=RunStatus.REVIEW)


# ---------------------------------------------------------------- B2 estimate


def _est(prompt: int = 1000, tool: int = 500, out: int = 2000, turns: int = 10) -> ob.CallEstimate:
    return ob.CallEstimate(
        prompt_tokens=prompt, tool_output_per_turn=tool, output_ceiling=out, requested_turns=turns
    )


@given(st.integers(0, 50_000), st.integers(0, 20_000), st.integers(1, 64_000), st.integers(0, 60))
def test_tokens_for_is_the_sum_over_turns(prompt: int, tool: int, out: int, n: int) -> None:
    est = _est(prompt, tool, out, max(n, 1))
    assert est.tokens_for(n) == sum(prompt + (k - 1) * tool + out for k in range(1, n + 1))


@given(
    st.integers(0, 50_000),
    st.integers(0, 20_000),
    st.integers(1, 64_000),
    st.integers(1, 60),
    st.integers(0, 3_000_000),
    st.integers(0, 80),
)
def test_max_turns_within_is_the_largest_that_fits(
    prompt: int, tool: int, out: int, turns: int, room: int, turn_room: int
) -> None:
    est = _est(prompt, tool, out, turns)
    n = est.max_turns_within(room, turn_room)
    assert 0 <= n <= min(turns, turn_room)
    assert est.tokens_for(n) <= room
    if n < min(turns, turn_room):
        assert est.tokens_for(n + 1) > room


def test_refused_only_if_one_turn_does_not_fit() -> None:
    est = _est(prompt=1000, tool=0, out=1000, turns=5)
    assert est.max_turns_within(1999, 10) == 0
    assert est.max_turns_within(2000, 10) == 1
    assert est.max_turns_within(10**9, 3) == 3  # turn room clamps too
    with pytest.raises(ValueError, match=">= 0"):
        est.tokens_for(-1)


# ---------------------------------------------------------------- budget manager contract (double)


def test_attempt_caps_end_as_escalation_not_budget() -> None:
    b = SimpleBudget(BUDGETS)
    for _ in range(6):
        b.start_attempt(StepId("s1"))
    with pytest.raises(AttemptCapError):
        b.start_attempt(StepId("s1"))
    numbers = [b.start_attempt(StepId(f"s{i}")) for i in range(2, 36)]
    assert numbers[-1] == 40
    with pytest.raises(AttemptCapError, match="in the run"):
        b.start_attempt(StepId("s99"))


def test_paused_time_does_not_count() -> None:
    b = SimpleBudget(BUDGETS)
    b.advance(60 * 100)
    b.pause()
    b.advance(60 * 1000)
    b.resume()
    b.check_time()
    b.advance(60 * 21)
    with pytest.raises(BudgetExceededError, match="time"):
        b.check_time()


def test_guard_stops_mid_call_and_precheck_clamps() -> None:
    b = SimpleBudget(BUDGETS.model_copy(update={"max_turns": 3}))
    assert b.precheck(LLMCallId("c1"), _est(turns=10)) == 3
    g = b.guard(LLMCallId("c1"))
    for _ in range(3):
        g.after_turn(None)
    with pytest.raises(BudgetExceededError):
        g.after_turn(None)
    b.record_call(LLMCallId("c1"), Usage(input_tokens=10), turns=4, api_equiv_usd=0.0)
    with pytest.raises(BudgetExceededError, match="one turn"):
        b.precheck(LLMCallId("c2"), _est())


def test_guard_needs_a_precheck_and_nested_calls_stay_apart() -> None:
    """Review #3: an `explore` call runs inside an Editor call; each keeps its own estimate."""
    b = SimpleBudget(BUDGETS.model_copy(update={"max_turns": 10}))
    with pytest.raises(KeyError, match="never prechecked"):
        b.guard(LLMCallId("nope"))
    assert b.precheck(LLMCallId("editor"), _est(turns=8)) == 8
    outer = b.guard(LLMCallId("editor"))
    for _ in range(4):
        outer.after_turn(None)
    # The nested call's room already counts the outer call's 4 in-flight turns.
    assert b.precheck(LLMCallId("explorer"), _est(turns=20)) == 6
    assert b.calls[LLMCallId("editor")][1] == 8
    assert b.calls[LLMCallId("explorer")][1] == 6


# ---------------------------------------------------------------- checkpoints


def _cp(**kw: Any) -> Checkpoint:
    base: dict[str, Any] = {"run_id": RUN, "seq": 1, "at": NOW, "status": RunStatus.REPAIR_LOOP}
    base.update(kw)
    return Checkpoint(**base)


def test_sticky_scope_keys() -> None:
    assert sticky_scope(StepId("step-1"), RunStatus.REPAIR_LOOP) == "step/step-1"
    assert sticky_scope(None, RunStatus.PLANNED) == "phase/PLANNED"
    _cp(sticky={"step/step-1": StickyState(), "phase/PLANNED": StickyState()})
    with pytest.raises(ValidationError):
        _cp(sticky={"whatever": StickyState()})


def test_checkpoint_shape() -> None:
    _cp(
        status=RunStatus.PAUSED_LIMIT,
        paused_at_state=RunStatus.REPAIR_LOOP,
        export_dir="/runs/x/export",
        export_digest="d" * 64,
    )
    for bad, match in [
        ({"status": RunStatus.PAUSED_LIMIT}, "paused_at_state"),
        ({"paused_at_state": RunStatus.REPAIR_LOOP}, "paused_at_state"),
        (
            {
                "status": RunStatus.PAUSED_LIMIT,
                "paused_at_state": RunStatus.END_ERROR,
                "export_dir": "/x",
                "export_digest": "d" * 64,
            },
            "resume into",
        ),
        (
            {"status": RunStatus.PAUSED_LIMIT, "paused_at_state": RunStatus.REVIEW},
            "workspace export",
        ),
        (
            {
                "status": RunStatus.PAUSED_LIMIT,
                "paused_at_state": RunStatus.REVIEW,
                "export_dir": "/x",
            },
            "workspace export and digest",
        ),
        ({"export_digest": "d" * 64}, "set together"),
        ({"attempt_n": 2}, "needs a step_id"),
        ({"critic_revisions": 2}, "one Critic revise"),
        ({"latest_snapshot_ref": "refs/heads/main"}, "snapshot ref"),
    ]:
        with pytest.raises(ValidationError, match=match):
            _cp(**bad)
    _cp(latest_snapshot_ref="refs/mpilot/attempt/12")
    _cp(latest_snapshot_ref="refs/mpilot/phase/bumped")


def test_budget_state_counts_add_up() -> None:
    BudgetState(attempts_by_step={StepId("a"): 2, StepId("b"): 1}, attempts_total=3)
    with pytest.raises(ValidationError, match="sum over steps"):
        BudgetState(attempts_by_step={StepId("a"): 2}, attempts_total=3)


def _rich(seq: int) -> Checkpoint:
    return _cp(
        seq=seq,
        step_id=StepId("s1"),
        attempt_n=2,
        sticky={"step/s1": StickyState(tier_escalated=True, trigger_fired=True)},
        budget=BudgetState(
            spent=Spent(turns=10, input_tokens=1000),
            active_seconds=50.0,
            attempts_by_step={StepId("s1"): 2},
            attempts_total=2,
        ),
        failure_history=(
            FailureHistoryEntry(signature="sig-a", attempts=2, hypotheses_tried=("h1",)),
        ),
        last_tool_call_id=ToolCallId("tc-9"),
        in_flight=InFlightCall(
            role_call_id=LLMCallId("llm-3"), role="editor", tool_calls_done=(ToolCallId("tc-9"),)
        ),
        integrity_failures=1,
        critic_revisions=1,
    )


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"seq": 5}, "does not follow"),
        ({"sticky": {"step/s1": StickyState(tier_escalated=True)}}, "sticky state"),
        ({"sticky": {}}, "sticky state"),
        (
            {
                "budget": BudgetState(
                    spent=Spent(turns=10, input_tokens=1000),
                    active_seconds=50.0,
                    attempts_by_step={StepId("s1"): 1},
                    attempts_total=1,
                )
            },
            "attempt counter",
        ),
        (
            {
                "budget": BudgetState(
                    spent=Spent(turns=9, input_tokens=1000),
                    active_seconds=50.0,
                    attempts_by_step={StepId("s1"): 2},
                    attempts_total=2,
                )
            },
            "spent.turns",
        ),
        (
            {
                "budget": BudgetState(
                    spent=Spent(turns=10, input_tokens=1000),
                    active_seconds=1.0,
                    attempts_by_step={StepId("s1"): 2},
                    attempts_total=2,
                )
            },
            "active_seconds",
        ),
        ({"failure_history": ()}, "failure history lost"),
        (
            {
                "failure_history": (
                    FailureHistoryEntry(signature="sig-a", attempts=1, hypotheses_tried=("h1",)),
                )
            },
            "attempts on sig-a",
        ),
        (
            {"failure_history": (FailureHistoryEntry(signature="sig-a", attempts=2),)},
            "tried hypotheses",
        ),
        ({"integrity_failures": 0}, "integrity_failures"),
        ({"critic_revisions": 0}, "critic_revisions"),
        ({"run_id": RunId("other")}, "different runs"),
    ],
)
def test_a_checkpoint_can_never_undo_the_previous_one(
    change: dict[str, Any], fragment: str
) -> None:
    """B5 / FR-21: a resume never de-escalates and never gets fresh attempts or budget."""
    prev = _rich(5)
    nxt = _rich(6).model_copy(update=change)
    assert any(fragment in p for p in nxt.covers(prev)), nxt.covers(prev)


def test_a_forward_checkpoint_is_accepted() -> None:
    assert _rich(6).covers(_rich(5)) == []


def test_store_refuses_a_backwards_checkpoint() -> None:
    store = MemoryStore()
    store.checkpoint(_rich(5))
    with pytest.raises(ValueError, match="sticky state"):
        store.checkpoint(_rich(6).model_copy(update={"sticky": {}}))
    store.checkpoint(_rich(6))
    assert store.latest_checkpoint(RUN) == _rich(6)


# ---------------------------------------------------------------- store contract (double)


def _run() -> Run:
    spec = MigrationSpec(
        kind=MigrationKind.BUMP,
        ecosystem=Ecosystem.NODE,
        packages=[PackageChange(name="react", to_version="18.3.1")],
        goal_text="g",
    )
    return Run(
        id=RUN,
        created_at=NOW,
        status=RunStatus.INIT,
        repo_url="u",
        spec=spec,
        budgets=BUDGETS,
        config_hash="h",
    )


def test_store_rows_round_trip() -> None:
    store = MemoryStore()
    store.create_run(_run())
    with pytest.raises(ValueError, match="exists"):
        store.create_run(_run())
    tr = TestResult(
        run_id=RUN, stage=TestStage.FULL, passed=1, failed=0, skipped=0, errors=0, duration_s=1
    )
    store.append(tr)
    store.append(tr.model_copy(update={"passed": 2}))
    assert [r.passed for r in store.rows(RUN, TestResult)] == [1, 2]
    store.save_run(_run().model_copy(update={"status": RunStatus.CLONED}))
    assert store.get_run(RUN).status is RunStatus.CLONED


# ---------------------------------------------------------------- events


def test_bus_replays_after_seq_then_streams_live() -> None:
    bus = MemoryBus()

    async def run() -> list[int]:
        bus.publish(RUN, ob.EventKind.STATE_CHANGED, {"to": "CLONED"})
        bus.publish(RUN, ob.EventKind.LOG, {"msg": "a"})
        got: list[int] = []

        async def consume() -> None:
            async for ev in bus.subscribe(RUN, after_seq=1):
                got.append(ev.seq)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        bus.publish(RUN, ob.EventKind.LOG, {"msg": "b"})
        await asyncio.sleep(0)
        bus.close(RUN)
        await asyncio.wait_for(task, 1)
        return got

    assert asyncio.run(run()) == [2, 3]
    with pytest.raises(ValueError, match="closed"):
        bus.publish(RUN, ob.EventKind.LOG, {})


def test_event_seq_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ob.RunEventRecord(run_id=RUN, seq=0, at=NOW, kind=ob.EventKind.LOG)


# ---------------------------------------------------------------- second review, non-blocking


@pytest.mark.parametrize(
    "ref",
    [
        "--upload-pack=touch /tmp/x",
        "-x",
        "a..b",
        "main.lock",
        "a b",
        "a:b",
        "x~1",
        "x^",
        "a*",
        "a[0]",
        "a\\b",
        "a@{1}",
        "/abs",
        "trailing/",
        "trailing.",
        "a//b",
        "",
        ".hidden",
        "a\x01b",
    ],
)
def test_unsafe_refs_are_refused(ref: str) -> None:
    with pytest.raises(ValidationError, match="safe git ref"):
        ob.RunRequest(repo=".", goal="x", ref=ref, budgets=BUDGETS)


@pytest.mark.parametrize("ref", ["main", "v1.2.3", "release/2024-11", "a" * 40, "feature/x+y"])
def test_safe_refs_are_accepted(ref: str) -> None:
    assert ob.RunRequest(repo=".", goal="x", ref=ref, budgets=BUDGETS).ref == ref


def test_checkpoint_rejects_a_malformed_digest_and_duplicate_signatures() -> None:
    with pytest.raises(ValidationError):
        _cp(export_dir="/x", export_digest="XYZ")
    entry = FailureHistoryEntry(signature="sig-a", attempts=1)
    with pytest.raises(ValidationError, match="one entry per signature"):
        _cp(failure_history=(entry, entry))


def test_in_flight_charges_survive_in_the_checkpointed_state() -> None:
    b = SimpleBudget(BUDGETS)
    b.precheck(LLMCallId("c1"), _est())
    g = b.guard(LLMCallId("c1"))
    g.after_turn(None)
    g.after_turn(None)
    assert b.state().in_flight_turns == 2  # a crash now does not lose them
    b.record_call(LLMCallId("c1"), Usage(input_tokens=5), turns=2, api_equiv_usd=0.0)
    assert b.state().in_flight_turns == 0
    assert b.state().spent.turns == 2
