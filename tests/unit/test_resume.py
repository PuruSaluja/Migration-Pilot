"""P1-12b: resume completeness (FR-21, FR-18, §8.4, §10.1, §12).

- `LLMCall` keeps how a call ended, its turns and its redacted output, plus where it ran
  (step, attempt, parent); `ToolCall` keeps its role call.
- `RoutingCounters` keep the router inputs that come from a scope's history; `Checkpoint`
  carries them per scope with the attempt id and the unit's completed calls, and `covers`
  refuses lowering any of them.
- `resume_point` says what a resume keeps, restarts and re-runs.
- Acceptance, on the real SQLite store with a reopen in between: a pause between two role
  calls (Diagnoser -> Editor, Analyzer -> Planner) re-runs neither; one denial before a crash
  and one after still fire trigger 3.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from runtime_helpers import RUN, ctx, guard, host, no_redact, req
from test_domain_models import SAMPLES

from mpilot.agents.fake_runtime import FakeRuntime, RaiseKind, ScriptedCall, tool
from mpilot.agents.router import (
    EscalationTrigger,
    ExplorerFlag,
    RoutingContext,
    RoutingCounters,
    explorer_question_hash,
    fired_triggers,
)
from mpilot.agents.runtime import RoleCallResult, check_result, llm_call_from
from mpilot.core.clock import FakeClock
from mpilot.core.errors import StoreError, UsageLimitError
from mpilot.core.types import (
    AttemptId,
    Effort,
    LLMCallId,
    Role,
    RoutingDecisionId,
    RunStatus,
    StepId,
    StepType,
    StopReason,
    Tier,
    ToolCallId,
    Usage,
)
from mpilot.models import domain as d
from mpilot.models.coordination import Lease
from mpilot.models.state import (
    BudgetState,
    Checkpoint,
    InFlightCall,
    ResumePoint,
    latest_output,
    resume_point,
    sticky_scope,
)
from mpilot.models.store import SqliteRunStore
from mpilot.testing.doubles import DenyToolsPolicy

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
STEP = StepId("step-1")
ATT = AttemptId("att-1")
SCOPE = sticky_scope(STEP, RunStatus.REPAIR_LOOP)
OPUS_ID = "claude-opus-5-5"


def arun[T](coro: Any) -> T:
    return asyncio.run(coro)  # type: ignore[no-any-return]


def llm(**over: Any) -> d.LLMCall:
    base: dict[str, Any] = {
        "id": LLMCallId("llm-1"),
        "run_id": RUN,
        "role": Role.EDITOR,
        "step_id": STEP,
        "attempt_id": ATT,
        "runtime": "fake",
        "model": OPUS_ID,
        "tier": Tier.OPUS,
        "effort": Effort.HIGH,
        "routing_decision_id": RoutingDecisionId("rd-1"),
        "stop": StopReason.COMPLETED,
        "turns": 2,
        "output": {"answer": "x", "confidence": 0.9},
        "input_tokens": 1,
        "output_tokens": 1,
        "thinking": True,
        "latency_ms": 1,
    }
    return d.LLMCall(**{**base, **over})


def cp(seq: int, **over: Any) -> Checkpoint:
    base: dict[str, Any] = {
        "run_id": RUN,
        "seq": seq,
        "at": NOW + timedelta(seconds=seq),
        "status": RunStatus.REPAIR_LOOP,
        "step_id": STEP,
        "attempt_n": 1,
        "attempt_id": ATT,
        "budget": budget(1),
    }
    return Checkpoint(**{**base, **over})


def budget(attempts: int) -> BudgetState:
    return BudgetState(
        attempts_by_step={STEP: attempts} if attempts else {}, attempts_total=attempts
    )


def paused(seq: int, **over: Any) -> Checkpoint:
    base: dict[str, Any] = {
        "status": RunStatus.PAUSED_LIMIT,
        "paused_at_state": RunStatus.REPAIR_LOOP,
        "export_dir": "/runs/run-1/export",
        "export_digest": "0" * 64,
    }
    return cp(seq, **{**base, **over})


def denied_tool_call(n: int = 1) -> d.ToolCall:
    return d.ToolCall(
        id=ToolCallId(f"tc-{n}"),
        run_id=RUN,
        role_call_id=LLMCallId("llm-1"),
        role=Role.EDITOR,
        tool="Bash",
        input={},
        output_summary="",
        duration_ms=0,
        policy_decision=d.PolicyDecision.DENY,
        deny_reason="not an mpilot tool",
    )


def editor_ctx(**over: Any) -> RoutingContext:
    base: dict[str, Any] = {
        "role": Role.EDITOR,
        "step_type": StepType.EDIT_CLUSTER,
        "migration_kind": d.MigrationKind.BUMP,
        "attempt_in_step": 1,
    }
    return RoutingContext(**{**base, **over})


# ---------------------------------------------------------------- LLMCall and ToolCall


def test_a_finished_call_records_how_it_ended_and_its_output() -> None:
    row = llm()
    assert row.finished
    assert d.LLMCall.model_validate_json(row.model_dump_json()) == row
    no_answer = llm(stop=StopReason.MAX_TURNS, output=None)
    assert no_answer.finished
    assert no_answer.output is None


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"stop": None}, "exactly one of stop and limit_error"),
        ({"limit_error": "usage limit"}, "exactly one of stop and limit_error"),
        ({"output": None}, "output is present exactly"),
        ({"stop": StopReason.SCHEMA_INVALID}, "output is present exactly"),
        ({"role": Role.EXPLORER}, "parent_call_id is set exactly"),
        ({"parent_call_id": LLMCallId("llm-0")}, "parent_call_id is set exactly"),
        (
            {"role": Role.EXPLORER, "parent_call_id": LLMCallId("llm-1")},
            "its own parent",
        ),
        ({"step_id": None}, "attempt_id needs a step_id"),
        ({"turns": -1}, "greater than or equal"),
    ],
)
def test_llm_call_invariants(over: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        llm(**over)


def test_a_limit_interrupted_call_has_no_stop_and_is_not_finished() -> None:
    row = llm(stop=None, output=None, limit_error="usage limit", turns=1)
    assert not row.finished


def test_an_explorer_call_nests_under_its_parent() -> None:
    row = llm(id=LLMCallId("llm-x"), role=Role.EXPLORER, parent_call_id=LLMCallId("llm-1"))
    assert row.parent_call_id == "llm-1"


def test_a_tool_call_needs_its_role_call() -> None:
    fields = denied_tool_call().model_dump()
    del fields["role_call_id"]
    with pytest.raises(ValidationError, match="role_call_id"):
        d.ToolCall(**fields)


# ---------------------------------------------------------------- llm_call_from


def _result(**over: Any) -> RoleCallResult:
    base: dict[str, Any] = {
        "call_id": LLMCallId("llm-1"),
        "stop": StopReason.COMPLETED,
        "output": {"answer": "token ghp_SECRET here", "confidence": 0.5},
        "model_id": OPUS_ID,
        "effort_applied": Effort.HIGH,
        "models_seen": frozenset({OPUS_ID}),
        "thinking_enabled": True,
        "usage": Usage(input_tokens=3, output_tokens=2),
        "turns": 4,
        "latency_ms": 9,
    }
    return RoleCallResult(**{**base, **over})


def test_llm_call_from_records_scope_stop_turns_and_the_redacted_output() -> None:
    request = req(context=ctx(attempt_id=ATT))
    row = llm_call_from(
        request, _result(), runtime="fake", redact=lambda s: s.replace("ghp_SECRET", "[REDACTED]")
    )
    assert (row.step_id, row.attempt_id, row.parent_call_id) == (STEP, ATT, None)
    assert (row.stop, row.turns) == (StopReason.COMPLETED, 4)
    assert row.output == {"answer": "token [REDACTED] here", "confidence": 0.5}


def test_llm_call_from_nests_an_explorer_call() -> None:
    context = ctx(
        role=Role.EXPLORER,
        caller=Role.EDITOR,
        parent_call_id=LLMCallId("llm-editor"),
        role_call_id=LLMCallId("llm-1"),
    )
    row = llm_call_from(req(context=context), _result(), runtime="fake", redact=no_redact)
    assert (row.role, row.parent_call_id, row.step_id) == (Role.EXPLORER, "llm-editor", STEP)


def test_a_call_without_an_answer_records_no_output() -> None:
    res = _result(stop=StopReason.BUDGET_STOP, output=None)
    row = llm_call_from(req(), res, runtime="fake", redact=no_redact)
    assert (row.stop, row.output) == (StopReason.BUDGET_STOP, None)


@pytest.mark.parametrize(
    ("redact", "fragment"),
    [
        (lambda s: s[:-1], "broke the answer's JSON"),
        (lambda s: "[1]", "other than an object"),
    ],
)
def test_a_redactor_that_breaks_the_answer_records_nothing(
    redact: Callable[[str], str], fragment: str
) -> None:
    with pytest.raises(ValueError, match=fragment):
        llm_call_from(req(), _result(), runtime="fake", redact=redact)


def test_an_answer_the_redaction_makes_schema_invalid_is_recorded_as_such() -> None:
    """The call ran and was paid for: keep its row (trigger 3), with no unusable output."""
    row = llm_call_from(
        req(),
        _result(),
        runtime="fake",
        redact=lambda s: s.replace('"confidence":0.5', '"confidence":"[REDACTED]"'),
    )
    assert (row.stop, row.output) == (StopReason.SCHEMA_INVALID, None)
    assert RoutingCounters().after_call(row).schema_invalid == 1


def test_the_redactor_sees_non_ascii_unescaped() -> None:
    seen: list[str] = []

    def spy(line: str) -> str:
        seen.append(line)
        return line

    llm_call_from(
        req(), _result(output={"answer": "clé ghp_x", "confidence": 1}), runtime="f", redact=spy
    )
    assert "clé" in seen[0]


# ---------------------------------------------------------------- RoutingCounters


def test_denials_and_schema_invalid_calls_are_counted() -> None:
    c = RoutingCounters().after_tool_call(denied_tool_call())
    allowed = denied_tool_call().model_copy(
        update={"policy_decision": d.PolicyDecision.ALLOW, "deny_reason": None}
    )
    assert c.after_tool_call(allowed) == c
    c = c.after_call(llm(stop=StopReason.SCHEMA_INVALID, output=None))
    assert (c.policy_denials, c.schema_invalid) == (1, 1)


def test_the_latest_completed_call_sets_last_confidence() -> None:
    c = RoutingCounters().after_call(llm(output={"confidence": 0.4}))
    assert (c.last_confidence, c.last_confidence_call) == (0.4, "llm-1")
    c = c.after_call(llm(id=LLMCallId("llm-2"), output={"confidence": 1}))
    assert (c.last_confidence, c.last_confidence_call) == (1.0, "llm-2")
    limit = llm(id=LLMCallId("llm-3"), stop=None, output=None, limit_error="limit")
    assert c.after_call(limit) == c


@pytest.mark.parametrize("output", [{"answer": "x"}, {"confidence": True}, {"confidence": "0.9"}])
def test_an_output_without_numeric_confidence_is_refused(output: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="numeric confidence"):
        RoutingCounters().after_call(llm(output=output))


def test_explorer_question_hash_normalizes_and_keeps_caller_and_scope_apart() -> None:
    h = explorer_question_hash(Role.EDITOR, "Where is  `render` called?", "src/")
    assert h == explorer_question_hash(Role.EDITOR, " where is `RENDER`\ncalled? ", "src/")
    fullwidth = "Where is `\uff52\uff45\uff4e\uff44\uff45\uff52` called?"  # NFKC folds it
    assert h == explorer_question_hash(Role.EDITOR, fullwidth, "src/")
    assert h != explorer_question_hash(Role.DIAGNOSER, "Where is `render` called?", "src/")
    assert h != explorer_question_hash(Role.EDITOR, "Where is `render` called?", "lib/")
    assert explorer_question_hash(Role.EDITOR, "q") == explorer_question_hash(Role.EDITOR, "q", "")


def test_a_repeated_question_flags_the_call() -> None:
    h = explorer_question_hash(Role.EDITOR, "q")
    once = RoutingCounters().asked(h)
    assert once.explorer_flags == frozenset()
    twice = once.asked(h)
    assert twice.explorer_flags == {ExplorerFlag.REPEATED_QUESTION}
    assert twice.explorer_questions == {h}


def test_counters_refuse_malformed_values() -> None:
    with pytest.raises(ValidationError, match="set together"):
        RoutingCounters(last_confidence=0.5)
    with pytest.raises(ValidationError, match="explorer_question_hash"):
        RoutingCounters(explorer_questions=frozenset({"not-a-hash"}))
    with pytest.raises(ValidationError, match="among confidence_calls"):
        RoutingCounters(last_confidence=0.5, last_confidence_call=LLMCallId("llm-1"))


FULL = RoutingCounters(
    policy_denials=2,
    schema_invalid=1,
    last_confidence=0.6,
    last_confidence_call=LLMCallId("llm-1"),
    confidence_calls=frozenset({"llm-0", "llm-1"}),
    explorer_questions=frozenset({"a" * 64}),
    explorer_flags=frozenset({ExplorerFlag.OVER_CAP}),
    pr_template_rejected=True,
)


@pytest.mark.parametrize(
    "lowered",
    [
        {"policy_denials": 1},
        {"schema_invalid": 0},
        {"last_confidence": 0.9},  # the same call's value rewritten
        {"last_confidence": None, "last_confidence_call": None},
        {
            "last_confidence": 0.95,
            "last_confidence_call": "llm-0",
        },  # switched back to an older call
        {"confidence_calls": frozenset({"llm-1"})},
        {"explorer_questions": frozenset()},
        {"explorer_flags": frozenset()},
        {"pr_template_rejected": False},
    ],
)
def test_counters_cover_only_what_keeps_everything(lowered: dict[str, Any]) -> None:
    assert FULL.covers(FULL)
    other = RoutingCounters.model_validate({**FULL.model_dump(), **lowered})
    assert not other.covers(FULL)


def test_a_newer_calls_confidence_may_be_anything() -> None:
    newer = FULL.after_call(llm(id=LLMCallId("llm-2"), output={"confidence": 0.99}))
    assert newer.covers(FULL)
    assert newer.confidence_calls == {"llm-0", "llm-1", "llm-2"}
    assert newer.after_call(llm(id=LLMCallId("llm-1"), output={"confidence": 1})) == newer
    assert RoutingCounters().covers(RoutingCounters())


def test_apply_fills_the_routing_inputs() -> None:
    ctx_ = FULL.apply(editor_ctx())
    assert (ctx_.policy_denials_in_step, ctx_.schema_invalid_in_step) == (2, True)
    assert ctx_.last_confidence == 0.6
    assert ctx_.explorer_flags == frozenset()
    assert not ctx_.pr_template_rejected
    triggers = fired_triggers(ctx_)
    assert {
        EscalationTrigger.SCHEMA_INVALID_OR_DENIALS,
        EscalationTrigger.LOW_CONFIDENCE,
    } <= triggers


def test_apply_passes_explorer_flags_and_rejections_only_to_their_roles() -> None:
    explorer = RoutingContext(
        role=Role.EXPLORER, caller=Role.PLANNER, migration_kind=d.MigrationKind.BUMP
    )
    assert FULL.apply(explorer).explorer_flags == {ExplorerFlag.OVER_CAP}
    pr = RoutingContext(role=Role.PR_WRITER, migration_kind=d.MigrationKind.BUMP)
    assert FULL.apply(pr).pr_template_rejected


def test_apply_never_lowers_what_the_context_already_says() -> None:
    ctx_ = RoutingCounters().apply(
        editor_ctx(policy_denials_in_step=3, schema_invalid_in_step=True)
    )
    assert (ctx_.policy_denials_in_step, ctx_.schema_invalid_in_step) == (3, True)
    assert RoutingCounters().apply(editor_ctx(last_confidence=0.3)).last_confidence == 0.3
    assert FULL.apply(editor_ctx(last_confidence=0.3)).last_confidence == 0.3  # the lower one
    assert FULL.apply(editor_ctx(last_confidence=0.9)).last_confidence == 0.6


# ---------------------------------------------------------------- Checkpoint


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"attempt_id": None}, "set together"),
        ({"attempt_n": None, "step_id": STEP}, "set together"),
        ({"attempt_n": 2, "attempt_id": AttemptId("att-2")}, "count of started attempts"),
        ({"latest_snapshot_ref": "refs/mpilot/attempt/1"}, "own snapshot ends it"),
        ({"status": RunStatus.REVIEW}, "only in REPAIR_LOOP"),  # review B2-suggestion 2
        ({"completed_calls": (LLMCallId("a"), LLMCallId("a"))}, "completed twice"),
        (
            {
                "completed_calls": (LLMCallId("a"),),
                "in_flight": InFlightCall(role_call_id=LLMCallId("a"), role=Role.EDITOR),
            },
            "cannot also be completed",
        ),
    ],
)
def test_checkpoint_invariants(over: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        cp(1, **over)


def test_an_earlier_attempts_snapshot_is_fine() -> None:
    later = cp(1, attempt_n=2, budget=budget(2), latest_snapshot_ref="refs/mpilot/attempt/1")
    assert later.attempt_n == 2


def test_the_unit_is_state_step_attempt_and_epoch_and_a_pause_keeps_it() -> None:
    assert cp(1).unit_key == (RunStatus.REPAIR_LOOP, STEP, 1, 0)
    assert paused(2).unit_key == cp(1).unit_key
    assert cp(1, restore_epoch=2).unit_key[3] == 2
    assert paused(2).scope == cp(1).scope == SCOPE  # never phase/PAUSED_LIMIT


def test_completed_calls_only_grow_within_a_unit() -> None:
    one = cp(1, completed_calls=(LLMCallId("a"),))
    assert cp(2, completed_calls=(LLMCallId("a"), LLMCallId("b"))).covers(one) == []
    assert paused(2, completed_calls=(LLMCallId("a"),)).covers(one) == []
    for bad in [(), (LLMCallId("b"),), (LLMCallId("b"), LLMCallId("a"))]:
        assert "a completed role call of the current unit was dropped" in cp(
            2, completed_calls=bad
        ).covers(one)
        assert any("dropped" in p for p in paused(2, completed_calls=bad).covers(one))


def test_a_crash_restore_may_only_cut_completed_calls_back() -> None:
    two = cp(1, completed_calls=(LLMCallId("a"), LLMCallId("b")))
    assert cp(2, restore_epoch=1, completed_calls=(LLMCallId("a"),)).covers(two) == []
    assert cp(2, restore_epoch=1, completed_calls=()).covers(two) == []
    for bad in [(LLMCallId("b"),), (LLMCallId("a"), LLMCallId("c"))]:
        assert any(
            "cut completed calls back" in p
            for p in cp(2, restore_epoch=1, completed_calls=bad).covers(two)
        )


def test_a_new_unit_starts_with_no_completed_calls() -> None:
    one = cp(1, completed_calls=(LLMCallId("a"),))
    next_attempt = cp(2, attempt_n=2, attempt_id=AttemptId("att-2"), budget=budget(2))
    assert next_attempt.covers(one) == []
    between = cp(2, attempt_n=None, attempt_id=None)
    assert between.covers(one) == []
    assert cp(3, attempt_n=2, attempt_id=AttemptId("att-2"), budget=budget(2)).covers(between) == []


def test_an_attempt_cannot_be_reentered_through_a_gap() -> None:
    """Review B3: attempt 2 -> no attempt -> attempt 2 again (completed calls reset) is refused."""
    two = cp(1, attempt_n=2, attempt_id=AttemptId("att-2"), budget=budget(2))
    gap = cp(2, attempt_n=None, attempt_id=None, budget=budget(2))
    assert gap.covers(two) == []
    again = cp(3, attempt_n=2, attempt_id=AttemptId("att-2"), budget=budget(2))
    assert any("attempt 2 is not new" in p for p in again.covers(gap))
    reused = cp(3, attempt_n=3, attempt_id=AttemptId("att-2"), budget=budget(3))
    assert any("reuses the previous attempt's id" in p for p in reused.covers(two))


def test_routing_counters_of_every_scope_are_kept() -> None:
    first = cp(1, routing={SCOPE: FULL, "phase/ANALYZED": RoutingCounters(policy_denials=1)})
    assert cp(2, routing={**first.routing}).covers(first) == []
    problems = cp(2, routing={SCOPE: FULL}).covers(first)
    assert problems == ["routing counters for phase/ANALYZED were lowered"]


# ---------------------------------------------------------------- resume_point

DIAG = llm(id=LLMCallId("llm-diag"), role=Role.DIAGNOSER)
EDIT = llm(id=LLMCallId("llm-edit"), role=Role.EDITOR)
AT = NOW + timedelta(hours=1)


def tc(n: int, role_call: str, *, deny: bool = False) -> d.ToolCall:
    row = denied_tool_call(n).model_copy(
        update={"role_call_id": LLMCallId(role_call), "attempt_id": ATT}
    )
    if deny:
        return row
    return row.model_copy(update={"policy_decision": d.PolicyDecision.ALLOW, "deny_reason": None})


def limited(row: d.LLMCall) -> d.LLMCall:
    return row.model_copy(update={"stop": None, "output": None, "limit_error": "limit"})


def test_after_a_pause_finished_calls_are_kept_and_the_in_flight_one_restarts() -> None:
    flight = InFlightCall(
        role_call_id=EDIT.id,
        role=Role.EDITOR,
        step_id=STEP,
        tool_calls_done=(ToolCallId("tc-1"),),
        summary="viewed a.py",
    )
    state = paused(
        3,
        completed_calls=(DIAG.id,),
        in_flight=flight,
        last_tool_call_id=ToolCallId("tc-1"),
        last_llm_call_id=EDIT.id,
    )
    point = resume_point(state, [DIAG, limited(EDIT)], [tc(1, EDIT.id)], at=AT)
    assert (point.completed, point.restart, point.undone) == ((DIAG,), flight, ())
    (written,) = point.checkpoints
    assert written.covers(state) == []
    assert (written.seq, written.at, written.status) == (4, AT, RunStatus.PAUSED_LIMIT)
    assert written.completed_calls == (DIAG.id,)


def test_tool_calls_the_checkpoint_missed_join_the_in_flight_call() -> None:
    flight = InFlightCall(role_call_id=EDIT.id, role=Role.EDITOR, step_id=STEP)
    state = paused(3, in_flight=flight, last_llm_call_id=EDIT.id)
    rows = [tc(1, EDIT.id), tc(2, "llm-nested", deny=True), tc(3, EDIT.id, deny=True)]
    point = resume_point(state, [limited(EDIT)], rows, at=AT)
    assert point.restart is not None
    assert point.restart.tool_calls_done == ("tc-1", "tc-3")
    (written,) = point.checkpoints
    assert written.last_tool_call_id == "tc-3"
    assert written.routing[SCOPE].policy_denials == 2  # nested Explorer denials count too


def test_a_finished_call_the_checkpoint_missed_is_completed_and_folded_in() -> None:
    """Review B1: a crash between the row and its checkpoint loses neither the call nor its
    router inputs (here a low confidence: trigger 5)."""
    low = DIAG.model_copy(update={"output": {"hypotheses": ["h"], "confidence": 0.4}})
    flight = InFlightCall(role_call_id=DIAG.id, role=Role.DIAGNOSER, step_id=STEP)
    for state in (cp(3, in_flight=flight), paused(3, in_flight=flight)):
        point = resume_point(state, [low], [], at=AT)
        assert point.completed == (low,)
        assert point.restart is None
        final = point.checkpoints[-1]
        assert final.completed_calls == (DIAG.id,)
        assert final.in_flight is None
        assert final.last_llm_call_id == DIAG.id
        assert EscalationTrigger.LOW_CONFIDENCE in fired_triggers(
            final.routing[SCOPE].apply(editor_ctx())
        )


def test_a_call_that_was_never_in_flight_is_still_found() -> None:
    """Review B5: a call with no tool calls whose in-flight checkpoint was skipped."""
    point = resume_point(cp(3), [DIAG], [], at=AT)
    assert point.completed == (DIAG,)


def test_after_a_crash_edits_since_the_snapshot_run_again() -> None:
    """§10.1: the workspace goes back to the latest snapshot, which predates the unit."""
    after_editor = cp(4, completed_calls=(DIAG.id, EDIT.id), last_llm_call_id=EDIT.id)
    point = resume_point(after_editor, [DIAG, EDIT], [], at=AT)
    assert (point.completed, point.undone, point.restart) == ((DIAG,), (EDIT,), None)
    reconciled, restored = point.checkpoints
    assert reconciled.covers(after_editor) == []
    assert restored.covers(reconciled) == []
    assert (restored.restore_epoch, restored.completed_calls, restored.in_flight) == (
        1,
        (DIAG.id,),
        None,
    )
    flight = InFlightCall(role_call_id=EDIT.id, role=Role.EDITOR, step_id=STEP, summary="s")
    during_editor = cp(3, completed_calls=(DIAG.id,), in_flight=flight, last_llm_call_id=DIAG.id)
    point = resume_point(during_editor, [DIAG], [tc(1, EDIT.id)], at=AT)
    assert (point.completed, point.restart) == ((DIAG,), None)  # re-issued, no summary
    assert point.checkpoints[-1].in_flight is None


def test_read_only_calls_before_the_first_edit_survive_a_crash() -> None:
    critic = llm(id=LLMCallId("llm-c"), role=Role.CRITIC, step_id=None, attempt_id=None)
    review = cp(
        2,
        status=RunStatus.REVIEW,
        step_id=None,
        attempt_n=None,
        attempt_id=None,
        completed_calls=(critic.id,),
        last_llm_call_id=critic.id,
    )
    assert resume_point(review, [critic], [], at=AT).completed == (critic,)


@pytest.mark.parametrize(
    ("rows", "fragment"),
    [
        ([], "has no row"),
        ([limited(DIAG)], "a limit"),
        ([DIAG.model_copy(update={"attempt_id": AttemptId("att-9")})], "another step or attempt"),
        ([DIAG.model_copy(update={"step_id": StepId("s9")})], "another step or attempt"),
        (
            [DIAG.model_copy(update={"role": Role.EXPLORER, "parent_call_id": EDIT.id})],
            "nested",
        ),
    ],
)
def test_a_checkpoint_the_store_contradicts_is_refused(
    rows: list[d.LLMCall], fragment: str
) -> None:
    with pytest.raises(ValueError, match=fragment):
        resume_point(paused(3, completed_calls=(DIAG.id,)), rows, [], at=AT)


def test_newer_rows_from_another_attempt_are_refused() -> None:
    with pytest.raises(ValueError, match="tool call tc-1 is from another attempt"):
        resume_point(cp(3), [], [denied_tool_call(1)], at=AT)  # attempt_id None
    nested = llm(id=LLMCallId("x"), role=Role.EXPLORER, parent_call_id=EDIT.id, attempt_id=None)
    with pytest.raises(ValueError, match="another step or attempt"):
        resume_point(cp(3), [nested], [], at=AT)


def test_a_newer_finished_call_from_another_step_is_refused() -> None:
    stray = DIAG.model_copy(update={"step_id": StepId("s9"), "attempt_id": None})
    with pytest.raises(ValueError, match="another step or attempt"):
        resume_point(cp(3), [stray], [], at=AT)


@pytest.mark.parametrize(
    "marks",
    [{"last_llm_call_id": LLMCallId("gone")}, {"last_tool_call_id": ToolCallId("gone")}],
)
def test_a_high_water_mark_without_a_row_is_refused(marks: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="has no row"):
        resume_point(cp(3, **marks), [DIAG], [tc(1, DIAG.id)], at=AT)


def test_rows_of_other_runs_are_ignored() -> None:
    other = DIAG.model_copy(update={"run_id": "run-other"})
    with pytest.raises(ValueError, match="has no row"):
        resume_point(paused(3, completed_calls=(DIAG.id,)), [other], [], at=AT)


def test_latest_output_is_the_last_completed_top_level_call_of_the_role() -> None:
    first = llm(id=LLMCallId("a1"), role=Role.ANALYZER, step_id=None, attempt_id=None)
    second = first.model_copy(update={"id": "a2", "output": {"usage_sites": 3, "confidence": 1}})
    invalid = first.model_copy(
        update={"id": "a3", "stop": StopReason.SCHEMA_INVALID, "output": None}
    )
    assert latest_output([first, second, invalid], Role.ANALYZER) == second.output
    assert latest_output([first], Role.PLANNER) is None


# ---------------------------------------------------------------- acceptance (real store)

DIAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hypotheses": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["hypotheses", "confidence"],
}
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["answer", "confidence"],
}
HYPOTHESES = {"hypotheses": ["render() moved to react-dom/client"], "confidence": 0.85}
REPORT = {"answer": "3 usage sites of ReactDOM.render", "confidence": 0.9}
EDITOR_DONE = {"answer": "switched to createRoot", "confidence": 0.95}

#: How a role's prompt is built from the unit's completed calls and the run's rows.
Prompt = Callable[[Sequence[d.LLMCall], Sequence[d.LLMCall]], str]
Unit = Sequence[tuple[Role, dict[str, Any], Prompt]]


LEASE_TTL = 60.0


class MiniDriver:
    """The smallest driver that follows the resume protocol (see `resume_point`): in-flight
    checkpoint before each call, rows, then a checkpoint folding them in; on resume, the
    `ResumePoint` checkpoints first; the next role gets the stored output."""

    def __init__(self, path: Path, **host_kw: Any) -> None:
        self.path = path
        self.clock = FakeClock(start=NOW)
        self.store = SqliteRunStore.open(path, self.clock)
        self._lease: Lease | None = None
        self.host = host(allowlists={r: frozenset({"view_file"}) for r in Role}, **host_kw)
        self.persisted_tools = 0
        self.n = 0

    @property
    def lease(self) -> Lease:
        """This process's one driver lease, taken on first use and kept."""
        if self._lease is None:
            self._lease = self.store.acquire_lease(RUN, "mini-driver", ttl_s=LEASE_TTL)
        return self._lease

    def reopen(self) -> None:
        """A new process after a crash or pause: the old lease is gone with its process
        (released on pause, expired after a crash), and the new one takes the run over."""
        self.store.close()
        self.clock.advance(LEASE_TTL + 1)
        self.store = SqliteRunStore.open(self.path, self.clock)
        self._lease = None

    def latest(self) -> Checkpoint:
        found = self.store.latest_checkpoint(RUN)
        assert found is not None
        return found

    def write(self, base: Checkpoint, **update: Any) -> Checkpoint:
        nxt = Checkpoint.model_validate(
            {**base.model_dump(), "seq": base.seq + 1, "at": base.at + timedelta(seconds=1)}
            | update
        )
        self.store.checkpoint(nxt, lease=self.lease)
        return nxt

    def llm_rows(self) -> Sequence[d.LLMCall]:
        return self.store.rows(RUN, d.LLMCall)

    def resume(self) -> tuple[Checkpoint, list[d.LLMCall], ResumePoint]:
        cp_ = self.latest()
        point = resume_point(
            cp_,
            self.llm_rows(),
            self.store.rows(RUN, d.ToolCall),
            at=cp_.at + timedelta(seconds=1),
        )
        for nxt in point.checkpoints:
            self.store.checkpoint(nxt, lease=self.lease)
        cp_ = point.checkpoints[-1]
        if cp_.status is RunStatus.PAUSED_LIMIT:
            cp_ = self.write(
                cp_,
                status=cp_.paused_at_state,
                paused_at_state=None,
                export_dir=None,
                export_digest=None,
                in_flight=None,
            )
        return cp_, list(point.completed), point

    def _persist_tool_rows(self) -> list[d.ToolCall]:
        new = self.host.calls[self.persisted_tools :]
        self.persisted_tools = len(self.host.calls)
        for row in new:
            self.store.append(row, lease=self.lease)
        return new

    def _fold(self, cp_: Checkpoint, tools: Sequence[d.ToolCall], row: d.LLMCall) -> dict[str, Any]:
        counters = cp_.routing.get(cp_.scope, RoutingCounters())
        for t in tools:
            counters = counters.after_tool_call(t)
        return {
            "routing": {**cp_.routing, cp_.scope: counters.after_call(row)},
            "last_llm_call_id": row.id,
            "last_tool_call_id": tools[-1].id if tools else cp_.last_tool_call_id,
        }

    def run_unit(
        self,
        runtime: FakeRuntime,
        roles: Unit,
        *,
        step: StepId | None,
        resume: bool = False,
        crash_after_row: bool = False,
    ) -> Checkpoint:
        if resume:
            cp_, done, _ = self.resume()
        else:
            cp_, done = self.latest(), []
        for role, schema, prompt in roles[len(done) :]:
            self.n += 1
            call_id = LLMCallId(f"llm-{role.value}-{self.n}")
            cp_ = self.write(
                cp_, in_flight=InFlightCall(role_call_id=call_id, role=role, step_id=step)
            )
            request = self._request(role, call_id, schema, step, cp_, prompt(done, self.llm_rows()))
            try:
                result = arun(runtime.call(request, self.host, guard()))
            except UsageLimitError as exc:
                tools = self._persist_tool_rows()
                row = self._limit_row(request, str(exc))
                self.store.append(row, lease=self.lease)
                return self.write(
                    cp_,
                    status=RunStatus.PAUSED_LIMIT,
                    paused_at_state=cp_.status,
                    export_dir="/runs/run-1/export",
                    export_digest="0" * 64,
                    **self._fold(cp_, tools, row),
                )
            check_result(request, result)
            tools = self._persist_tool_rows()
            row = llm_call_from(request, result, runtime=runtime.name, redact=no_redact)
            self.store.append(row, lease=self.lease)
            if crash_after_row:  # the process dies before the checkpoint that folds it in
                return cp_
            done.append(row)
            cp_ = self.write(
                cp_,
                in_flight=None,
                completed_calls=(*cp_.completed_calls, row.id),
                **self._fold(cp_, tools, row),
            )
        return cp_

    def _request(
        self,
        role: Role,
        call_id: LLMCallId,
        schema: dict[str, Any],
        step: StepId | None,
        cp_: Checkpoint,
        prompt: str,
    ) -> Any:
        decision_id = RoutingDecisionId(f"rd-{call_id}")
        decision = SAMPLES[d.RoutingDecision]()
        assert isinstance(decision, d.RoutingDecision)
        self.store.append(
            decision.model_copy(
                update={"id": decision_id, "run_id": RUN, "role": role, "step_id": step}
            ),
            lease=self.lease,
        )
        return req(
            context=ctx(
                role=role,
                role_call_id=call_id,
                step_id=step,
                step_type=StepType.EDIT_CLUSTER if step is not None else None,
                attempt_id=cp_.attempt_id,
            ),
            routing_decision_id=decision_id,
            output_schema=schema,
            user_prompt=prompt,
        )

    def _limit_row(self, request: Any, message: str) -> d.LLMCall:
        c = request.context
        return d.LLMCall(
            id=c.role_call_id,
            run_id=RUN,
            role=c.role,
            step_id=c.step_id,
            attempt_id=c.attempt_id,
            runtime="fake",
            model=request.choice.model_id,
            tier=request.choice.tier,
            effort=request.choice.effort,
            routing_decision_id=request.routing_decision_id,
            stop=None,
            turns=0,
            input_tokens=0,
            output_tokens=0,
            thinking=True,
            latency_ms=0,
            limit_error=message,
        )


def _new_driver(path: Path, **host_kw: Any) -> MiniDriver:
    drv = MiniDriver(path, **host_kw)
    run_row = SAMPLES[d.Run]()
    assert isinstance(run_row, d.Run)
    drv.store.create_run(
        run_row.model_copy(
            update={"id": RUN, "status": RunStatus.REPAIR_LOOP, "paused_at_state": None}
        )
    )
    return drv


@pytest.fixture
def driver(tmp_path: Path) -> Any:
    drv = _new_driver(tmp_path / "mpilot.db")
    yield drv
    drv.store.close()


def _diag_prompt(done: Sequence[d.LLMCall], rows: Sequence[d.LLMCall]) -> str:
    return "Failing stage: typecheck"


def _edit_prompt(done: Sequence[d.LLMCall], rows: Sequence[d.LLMCall]) -> str:
    (diagnosis,) = [c for c in done if c.role is Role.DIAGNOSER]
    assert diagnosis.output is not None
    return f"Top hypothesis: {diagnosis.output['hypotheses'][0]}"


REPAIR: Unit = [
    (Role.DIAGNOSER, DIAG_SCHEMA, _diag_prompt),
    (Role.EDITOR, ANSWER_SCHEMA, _edit_prompt),
]


def _start_attempt(drv: MiniDriver) -> None:
    drv.store.checkpoint(cp(0), lease=drv.lease)


def _diag(output: dict[str, Any] = HYPOTHESES) -> ScriptedCall:
    return ScriptedCall.using(
        tool("view_file", path="src/index.tsx"), then=output, expect_role=Role.DIAGNOSER
    )


def _edit(output: dict[str, Any] = EDITOR_DONE) -> ScriptedCall:
    return ScriptedCall.answer(output, expect_role=Role.EDITOR)


LIMIT_EDIT = ScriptedCall(raises=RaiseKind.USAGE_LIMIT, expect_role=Role.EDITOR)


def test_a_pause_between_diagnoser_and_editor_reruns_neither(driver: MiniDriver) -> None:
    _start_attempt(driver)
    first = FakeRuntime([_diag(), LIMIT_EDIT])
    assert driver.run_unit(first, REPAIR, step=STEP).status is RunStatus.PAUSED_LIMIT
    first.assert_exhausted()
    edit_prompt_before = first.calls[1].request.user_prompt

    driver.reopen()  # `mpilot resume` in a new process
    second = FakeRuntime([_edit()])
    final = driver.run_unit(second, REPAIR, step=STEP, resume=True)
    second.assert_exhausted()  # only the Editor ran: the Diagnoser was not repeated

    assert second.calls[0].request.user_prompt == edit_prompt_before
    assert "react-dom/client" in edit_prompt_before  # its hypotheses were not lost
    rows = driver.llm_rows()
    assert [(r.role, r.stop) for r in rows] == [
        (Role.DIAGNOSER, StopReason.COMPLETED),
        (Role.EDITOR, None),  # the interrupted call: its tokens still count
        (Role.EDITOR, StopReason.COMPLETED),
    ]
    assert all((r.step_id, r.attempt_id) == (STEP, ATT) for r in rows)
    assert final.completed_calls == (rows[0].id, rows[2].id)
    assert final.routing[SCOPE].last_confidence_call == rows[2].id


def test_a_resumed_attempt_gives_the_editor_what_an_uninterrupted_one_does(
    driver: MiniDriver, tmp_path: Path
) -> None:
    _start_attempt(driver)
    straight = FakeRuntime([_diag(), _edit()])
    driver.run_unit(straight, REPAIR, step=STEP)
    straight.assert_exhausted()
    other = _new_driver(tmp_path / "other.db")
    try:
        _start_attempt(other)
        other.run_unit(FakeRuntime([_diag(), LIMIT_EDIT]), REPAIR, step=STEP)
        other.reopen()
        resumed = FakeRuntime([_edit()])
        other.run_unit(resumed, REPAIR, step=STEP, resume=True)
        assert resumed.calls[0].request.request_hash() == straight.calls[1].request.request_hash()
    finally:
        other.store.close()


def test_a_crash_between_a_low_confidence_diagnosis_and_its_checkpoint(
    driver: MiniDriver,
) -> None:
    """Review B1: neither the Diagnoser nor its confidence (trigger 5) is lost."""
    _start_attempt(driver)
    low = {"hypotheses": ["maybe the peer dependency"], "confidence": 0.5}
    driver.run_unit(FakeRuntime([_diag(low)]), REPAIR, step=STEP, crash_after_row=True)
    crashed = driver.latest()
    assert crashed.in_flight is not None
    assert crashed.completed_calls == ()

    driver.reopen()
    point = resume_point(  # read-only look at what the resume will write
        crashed, driver.llm_rows(), driver.store.rows(RUN, d.ToolCall), at=AT
    )
    assert [c.role for c in point.completed] == [Role.DIAGNOSER]
    assert EscalationTrigger.LOW_CONFIDENCE in fired_triggers(
        point.checkpoints[-1].routing[SCOPE].apply(editor_ctx())
    )
    second = FakeRuntime([_edit()])
    final = driver.run_unit(second, REPAIR, step=STEP, resume=True)
    second.assert_exhausted()  # the Diagnoser was not repeated
    diag_row, edit_row = driver.llm_rows()
    assert final.completed_calls == (diag_row.id, edit_row.id)


def test_a_crash_then_a_pause_never_counts_undone_edits_as_completed(driver: MiniDriver) -> None:
    """Review B2: crash after Diagnoser + Editor -> Editor re-runs -> it pauses -> resume:
    only the Diagnoser counts as completed, and the Editor restarts."""
    _start_attempt(driver)
    driver.run_unit(FakeRuntime([_diag(), _edit()]), REPAIR, step=STEP)
    diag_row, undone_edit = driver.llm_rows()

    driver.reopen()  # crash: the workspace goes back to the latest snapshot
    rerun = FakeRuntime([LIMIT_EDIT])
    pausedcp = driver.run_unit(rerun, REPAIR, step=STEP, resume=True)
    rerun.assert_exhausted()
    assert pausedcp.status is RunStatus.PAUSED_LIMIT
    assert pausedcp.completed_calls == (diag_row.id,)
    assert pausedcp.restore_epoch == 1

    driver.reopen()
    _, done, point = driver.resume()
    assert done == [diag_row]
    assert point.restart is not None
    assert point.restart.role is Role.EDITOR
    assert undone_edit.id not in driver.latest().completed_calls


def test_a_pause_between_analyzer_and_planner_keeps_the_analysis_report(
    driver: MiniDriver,
) -> None:
    driver.store.checkpoint(
        cp(
            0,
            status=RunStatus.BASELINE_OK,
            step_id=None,
            attempt_n=None,
            attempt_id=None,
            budget=budget(0),
        ),
        lease=driver.lease,
    )

    def plan_prompt(done: Sequence[d.LLMCall], rows: Sequence[d.LLMCall]) -> str:
        report = latest_output(rows, Role.ANALYZER)
        assert report is not None
        return f"Analysis: {report['answer']}"

    analyze: Unit = [(Role.ANALYZER, ANSWER_SCHEMA, _diag_prompt)]
    plan: Unit = [(Role.PLANNER, ANSWER_SCHEMA, plan_prompt)]
    first = FakeRuntime([ScriptedCall.answer(REPORT, expect_role=Role.ANALYZER)])
    after_analysis = driver.run_unit(first, analyze, step=None)
    driver.write(after_analysis, status=RunStatus.ANALYZED, completed_calls=())  # next phase
    limited_plan = FakeRuntime(
        [ScriptedCall(raises=RaiseKind.USAGE_LIMIT, expect_role=Role.PLANNER)]
    )
    assert driver.run_unit(limited_plan, plan, step=None).status is RunStatus.PAUSED_LIMIT

    driver.reopen()
    resumed = FakeRuntime(
        [ScriptedCall.answer({"answer": "plan", "confidence": 1}, expect_role=Role.PLANNER)]
    )
    driver.run_unit(resumed, plan, step=None, resume=True)
    resumed.assert_exhausted()  # the Analyzer was not repeated
    assert resumed.calls[0].request.user_prompt == "Analysis: 3 usage sites of ReactDOM.render"
    assert [r.role for r in driver.llm_rows() if r.finished] == [Role.ANALYZER, Role.PLANNER]


def test_one_denial_before_a_crash_and_one_after_still_fire_trigger_3(tmp_path: Path) -> None:
    """The denial's ToolCall row lands, the process dies before any checkpoint folds it in;
    the resume folds it, and the re-run Editor's denial makes two."""
    drv = _new_driver(tmp_path / "mpilot.db", policies=[DenyToolsPolicy(frozenset({"view_file"}))])
    try:
        _start_attempt(drv)
        denied_edit = ScriptedCall.using(
            tool("view_file", path="a.py"), then=EDITOR_DONE, expect_role=Role.EDITOR
        )
        editor_only: Unit = [(Role.EDITOR, ANSWER_SCHEMA, _diag_prompt)]
        drv.run_unit(FakeRuntime([denied_edit]), editor_only, step=STEP, crash_after_row=True)
        assert drv.latest().routing == {}  # nothing folded before the crash

        drv.reopen()
        resumed_cp, done, point = drv.resume()
        assert done == []
        assert [c.role for c in point.undone] == [Role.EDITOR]  # its edits were rolled back
        assert resumed_cp.routing[SCOPE].policy_denials == 1
        assert not fired_triggers(resumed_cp.routing[SCOPE].apply(editor_ctx()))

        final = drv.run_unit(FakeRuntime([denied_edit]), editor_only, step=STEP)
        assert final.routing[SCOPE].policy_denials == 2
        assert EscalationTrigger.SCHEMA_INVALID_OR_DENIALS in fired_triggers(
            final.routing[SCOPE].apply(editor_ctx())
        )
        with pytest.raises(StoreError, match="routing counters"):  # a reset is refused
            drv.write(final, routing={SCOPE: RoutingCounters()})
        role_calls = [r.role_call_id for r in drv.store.rows(RUN, d.ToolCall)]
        assert role_calls == [r.id for r in drv.llm_rows()]
    finally:
        drv.store.close()
