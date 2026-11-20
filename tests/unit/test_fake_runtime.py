"""P1-11: `FakeRuntime`, the scripted runtime. It goes through a real `ToolHost` and
`BudgetGuard`, writes `ToolCall` and `LLMCall` rows, records denials, and raises on demand."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from runtime_helpers import ANSWER, OPUS, RUN, SONNET, ctx, guard, host, no_redact, req
from test_domain_models import SAMPLES

from mpilot.agents.fake_runtime import (
    FakeRuntime,
    RaiseKind,
    ScriptedCall,
    ScriptedToolCall,
    ScriptedTurn,
    play,
    tool,
)
from mpilot.agents.runtime import (
    AgentRuntime,
    RoleCallRequest,
    RoleCallResult,
    StopReason,
    check_result,
    llm_call_from,
    schema_errors,
)
from mpilot.core.errors import (
    ForbiddenModelError,
    LockdownViolation,
    RuntimeCallError,
    ScriptDivergedError,
    UsageLimitError,
)
from mpilot.core.types import Effort, LLMCallId, Role, RoutingDecisionId, Usage
from mpilot.models import domain as d
from mpilot.models.store import SqliteRunStore
from mpilot.testing.doubles import InMemoryToolHost, TurnLimitGuard
from mpilot.tools.base import HostDenyCode


def run[T](coro: Any) -> T:
    result: T = asyncio.run(coro)
    return result


def call_once(
    script: ScriptedCall,
    *,
    host_: InMemoryToolHost | None = None,
    guard_: TurnLimitGuard | None = None,
    **req_kw: Any,
) -> tuple[RoleCallResult, FakeRuntime, InMemoryToolHost]:
    runtime, h = FakeRuntime([script]), host_ or host()
    request = req(**req_kw)
    result: RoleCallResult = run(runtime.call(request, h, guard_ or guard()))
    check_result(request, result)  # every result a runtime returns must pass the acceptance check
    return result, runtime, h


# ---------------------------------------------------------------- the basics


def test_it_is_an_agent_runtime() -> None:
    runtime = FakeRuntime()
    assert isinstance(runtime, AgentRuntime)
    assert runtime.name == "fake"
    assert runtime.capabilities.per_call_effort
    runtime.check_lockdown_static()
    assert run(runtime.selftest()).passed  # type: ignore[attr-defined]


def test_one_scripted_answer() -> None:
    result, runtime, h = call_once(ScriptedCall.answer(ANSWER))
    assert result.stop is StopReason.COMPLETED
    assert result.output == ANSWER
    assert (result.turns, result.usage) == (1, Usage(input_tokens=1, output_tokens=1))
    assert result.model_id == OPUS
    assert result.effort_applied is Effort.HIGH
    assert result.models_seen == {OPUS}
    assert result.thinking_enabled
    assert result.tool_call_ids == ()
    assert h.calls == []
    assert runtime.calls[0].result == result


def test_the_result_reports_the_effort_and_model_it_was_routed_to() -> None:
    from mpilot.agents.runtime import ModelChoice
    from mpilot.core.types import Tier

    choice = ModelChoice(tier=Tier.SONNET, effort=Effort.MEDIUM, model_id=SONNET)
    result, _, _ = call_once(ScriptedCall.answer(ANSWER), choice=choice)
    assert (result.model_id, result.effort_applied) == (SONNET, Effort.MEDIUM)


def test_usage_is_summed_over_turns_and_each_turn_is_charged_to_the_guard() -> None:
    u1, u2 = Usage(10, 5, 100, 20), Usage(3, 2)
    script = ScriptedCall(
        turns=(
            ScriptedTurn(tool_calls=(tool("view_file", path="a.py"),), usage=u1),
            ScriptedTurn(final=ANSWER, usage=u2),
        )
    )
    g = guard()
    result, _, _ = call_once(script, guard_=g)
    assert result.usage == u1 + u2
    assert result.turns == 2
    assert g.charged == [u1, u2]
    assert g.tool_checks == 1


# ---------------------------------------------------------------- tools go through the host


def test_tool_calls_run_through_the_host_and_are_recorded() -> None:
    script = ScriptedCall.using(
        tool("view_file", path="a.py"), tool("view_file", path="b.py"), then=ANSWER
    )
    result, runtime, h = call_once(script)
    assert [c.tool for c in h.calls] == ["view_file", "view_file"]
    assert [c.input for c in h.calls] == [{"path": "a.py"}, {"path": "b.py"}]
    assert all(c.policy_decision is d.PolicyDecision.ALLOW for c in h.calls)
    assert result.tool_call_ids == tuple(c.id for c in h.calls)
    played = runtime.calls[0]
    assert [i.output for i in played.invocations] == [
        {"content": "contents of a.py", "total_lines": 1},
        {"content": "contents of b.py", "total_lines": 1},
    ]


@pytest.mark.parametrize(
    ("name", "args", "code"),
    [
        ("Bash", {"command": "ls"}, HostDenyCode.UNKNOWN_TOOL),  # a Claude Code built-in (H9)
        ("mcp__other__view_file", {"path": "a"}, HostDenyCode.UNKNOWN_TOOL),
        ("mcp__mpilot__str_replace", {"path": "a"}, HostDenyCode.UNKNOWN_TOOL),  # not on this host
        ("mcp__mpilot__view_file", {}, HostDenyCode.INVALID_ARGS),
    ],
)
def test_a_scripted_denial_is_recorded_and_the_call_carries_on(
    name: str, args: dict[str, Any], code: str
) -> None:
    script = ScriptedCall.using(ScriptedToolCall(name=name, args=args), then=ANSWER)
    result, runtime, h = call_once(script)
    assert result.stop is StopReason.COMPLETED  # the model sees the denial and wraps up
    (row,) = h.calls
    assert row.policy_decision is d.PolicyDecision.DENY
    assert row.deny_reason
    (inv,) = runtime.calls[0].invocations
    assert inv.deny_code == code
    assert inv.for_model()["denied"] is True


def test_a_role_without_the_tool_is_denied() -> None:
    h = host(allowlists={Role.EDITOR: frozenset()})
    result, _, _ = call_once(ScriptedCall.using(tool("view_file", path="a"), then=ANSWER), host_=h)
    assert h.calls[0].deny_reason is not None
    assert result.stop is StopReason.COMPLETED


# ---------------------------------------------------------------- the per-call tool cap


def test_the_call_over_the_cap_is_refused_and_the_next_one_ends_the_call() -> None:
    script = ScriptedCall.using(*(tool("view_file", path=f"{i}.py") for i in range(5)), then=ANSWER)
    c = ctx(max_tool_calls=2)
    result, _, h = call_once(script, context=c)
    assert result.stop is StopReason.TOOL_CALL_CAP
    assert result.output is None
    assert len(h.calls) == 3  # two allowed, the third refused, the fourth never forwarded
    assert [x.policy_decision.value for x in h.calls] == ["allow", "allow", "deny"]
    assert len(result.tool_call_ids) == 3 == c.max_tool_calls + 1


def test_a_refusal_followed_by_an_answer_still_completes() -> None:
    script = ScriptedCall.using(*(tool("view_file", path=f"{i}.py") for i in range(3)), then=ANSWER)
    result, _, h = call_once(script, context=ctx(max_tool_calls=2))
    assert result.stop is StopReason.COMPLETED  # the model wrapped up after the refusal
    assert len(h.calls) == 3


def test_a_zero_allowance_refuses_the_first_call() -> None:
    script = ScriptedCall.using(
        tool("view_file", path="a"), tool("view_file", path="b"), then=ANSWER
    )
    result, _, h = call_once(script, context=ctx(max_tool_calls=0))
    assert result.stop is StopReason.TOOL_CALL_CAP
    assert len(h.calls) == 1


@settings(max_examples=60, deadline=None)
@given(
    names=st.lists(
        st.sampled_from(
            ["mcp__mpilot__view_file", "Bash", "mcp__other__x", "mcp__mpilot__list_dir"]
        ),
        max_size=8,
    ),
    cap=st.integers(min_value=0, max_value=5),
)
def test_a_call_never_makes_more_than_cap_plus_one_invocations(names: list[str], cap: int) -> None:
    calls = tuple(ScriptedToolCall(name=n, args={"path": "a"}) for n in names)
    tool_turn = (ScriptedTurn(tool_calls=calls),) if calls else ()
    script = ScriptedCall(turns=(*tool_turn, ScriptedTurn(final=ANSWER)))
    result, _, h = call_once(script, context=ctx(max_tool_calls=cap))
    assert len(result.tool_call_ids) <= cap + 1
    assert len(h.calls) == len(result.tool_call_ids)
    expected = StopReason.COMPLETED if len(names) <= cap + 1 else StopReason.TOOL_CALL_CAP
    assert result.stop is expected


# ---------------------------------------------------------------- budget


def test_crossing_the_budget_after_a_turn_stops_the_call_before_its_tools_run() -> None:
    script = ScriptedCall(
        turns=(
            ScriptedTurn(tool_calls=(tool("view_file", path="a"),)),
            ScriptedTurn(tool_calls=(tool("view_file", path="b"),)),
            ScriptedTurn(final=ANSWER),
        )
    )
    g = guard(max_turns=1)  # the 2nd charged turn crosses the cap
    result, _, h = call_once(script, guard_=g)
    assert result.stop is StopReason.BUDGET_STOP
    assert result.output is None
    assert [c.input for c in h.calls] == [{"path": "a"}]  # the crossing turn's tool never ran
    assert result.turns == 2  # the turn that crossed still happened and counts


def test_crossing_the_budget_before_a_tool_call_stops_the_call() -> None:
    class StopBeforeTools(TurnLimitGuard):
        def before_tool_call(self) -> None:
            from mpilot.core.errors import BudgetExceededError

            raise BudgetExceededError("time cap")

    result, _, h = call_once(
        ScriptedCall.using(tool("view_file", path="a"), then=ANSWER), guard_=StopBeforeTools(10)
    )
    assert result.stop is StopReason.BUDGET_STOP
    assert h.calls == []


def test_max_turns_ends_a_call_that_never_answers() -> None:
    turns = tuple(ScriptedTurn(tool_calls=(tool("view_file", path=f"{i}"),)) for i in range(5))
    result, _, _ = call_once(ScriptedCall(turns=turns), max_turns=3)
    assert result.stop is StopReason.MAX_TURNS
    assert result.turns == 3


def test_an_answer_on_the_last_allowed_turn_counts() -> None:
    result, _, _ = call_once(ScriptedCall.answer(ANSWER), max_turns=1)
    assert result.stop is StopReason.COMPLETED


# ---------------------------------------------------------------- raising


def test_a_usage_limit_after_some_tool_calls_keeps_what_was_done() -> None:
    script = ScriptedCall(
        turns=(
            ScriptedTurn(tool_calls=(tool("view_file", path="a"), tool("view_file", path="b"))),
        ),
        raises=RaiseKind.USAGE_LIMIT,
        raise_message="5-hour limit reached",
    )
    runtime, h, g = FakeRuntime([script]), host(), guard()
    with pytest.raises(UsageLimitError, match="5-hour"):
        run(runtime.call(req(), h, g))
    assert len(h.calls) == 2  # the interrupted call's tool calls happened and are on record
    assert g.charged == [Usage(1, 1)]  # and its completed turn is charged (B5)
    played = runtime.calls[0]
    assert played.result is None
    assert isinstance(played.error, UsageLimitError)
    assert len(played.invocations) == 2


def test_a_usage_limit_on_the_very_first_request() -> None:
    runtime = FakeRuntime([ScriptedCall(raises=RaiseKind.USAGE_LIMIT)])
    with pytest.raises(UsageLimitError):
        run(runtime.call(req(), host(), guard()))


def test_a_scripted_runtime_error() -> None:
    runtime = FakeRuntime([ScriptedCall(raises=RaiseKind.RUNTIME_ERROR, raise_message="boom")])
    with pytest.raises(RuntimeCallError, match="boom"):
        run(runtime.call(req(), host(), guard()))


def test_the_limit_fires_only_on_the_call_that_scripts_it() -> None:
    runtime = FakeRuntime(
        [
            ScriptedCall.answer(ANSWER),
            ScriptedCall(raises=RaiseKind.USAGE_LIMIT),
            ScriptedCall.answer(ANSWER),  # what the resumed call does
        ]
    )
    h = host()
    run(runtime.call(req(), h, guard()))
    with pytest.raises(UsageLimitError):
        run(runtime.call(req(), h, guard()))
    assert run(runtime.call(req(), h, guard())).stop is StopReason.COMPLETED  # type: ignore[attr-defined]
    runtime.assert_exhausted()


# ---------------------------------------------------------------- answers are validated


@pytest.mark.parametrize(
    "bad",
    [
        {"answer": "x"},  # no confidence
        {"answer": "x", "confidence": 1.5},
        {"answer": 3, "confidence": 0.5},
        {},
    ],
)
def test_an_answer_that_breaks_the_schema_is_schema_invalid(bad: dict[str, Any]) -> None:
    result, _, _ = call_once(ScriptedCall.answer(bad))
    assert result.stop is StopReason.SCHEMA_INVALID
    assert result.output is None


def test_schema_errors_name_the_problem() -> None:
    schema = req().output_schema
    assert schema_errors(schema, ANSWER) == []
    errors = schema_errors(schema, {"answer": 3, "confidence": 2})
    assert any(e.startswith("answer:") for e in errors)
    assert any(e.startswith("confidence:") for e in errors)
    assert schema_errors(schema, {})[0].startswith("<root>:")


# ---------------------------------------------------------------- loud failures


def test_running_out_of_script_fails_loudly() -> None:
    runtime = FakeRuntime([ScriptedCall.answer(ANSWER)])
    run(runtime.call(req(), host(), guard()))
    with pytest.raises(ScriptDivergedError, match="no scripted call left for editor"):
        run(runtime.call(req(), host(), guard()))


def test_unused_script_steps_are_caught() -> None:
    runtime = FakeRuntime([ScriptedCall.answer(ANSWER), ScriptedCall.answer(ANSWER)])
    run(runtime.call(req(), host(), guard()))
    assert runtime.remaining == 1
    with pytest.raises(ScriptDivergedError, match="never used"):
        runtime.assert_exhausted()


def test_a_script_that_just_stops_is_an_error_not_a_guess() -> None:
    script = ScriptedCall(turns=(ScriptedTurn(tool_calls=(tool("view_file", path="a"),)),))
    runtime = FakeRuntime([script])
    with pytest.raises(ScriptDivergedError, match="without a final answer"):
        run(runtime.call(req(), host(), guard()))
    assert runtime.calls[0].result is None


def test_a_script_for_another_role_is_refused() -> None:
    runtime = FakeRuntime([ScriptedCall.answer(ANSWER, expect_role=Role.PLANNER)])
    with pytest.raises(ScriptDivergedError, match="script is for planner, got editor"):
        run(runtime.call(req(), host(), guard()))


def test_expectations_about_the_host_are_checked() -> None:
    allowed = ScriptedToolCall(
        name="mcp__mpilot__view_file", args={"path": "a"}, expect_decision=d.PolicyDecision.DENY
    )
    runtime = FakeRuntime([ScriptedCall.using(allowed, then=ANSWER)])
    with pytest.raises(ScriptDivergedError, match="expected deny, the host said allow"):
        run(runtime.call(req(), host(), guard()))

    denied = ScriptedToolCall(name="Bash", expect_deny_code="role_not_allowed")
    runtime = FakeRuntime([ScriptedCall.using(denied, then=ANSWER)])
    with pytest.raises(ScriptDivergedError, match="expected denial 'role_not_allowed'"):
        run(runtime.call(req(), host(), guard()))


def test_expected_outputs_are_compared_only_when_strict() -> None:
    wrong = ScriptedToolCall(
        name="mcp__mpilot__view_file", args={"path": "a"}, expect_output={"content": "other"}
    )
    script = ScriptedCall.using(wrong, then=ANSWER)
    with pytest.raises(ScriptDivergedError, match="output differs"):
        run(FakeRuntime([script]).call(req(), host(), guard()))
    lenient = FakeRuntime([script], strict_outputs=False)
    assert run(lenient.call(req(), host(), guard())).stop is StopReason.COMPLETED  # type: ignore[attr-defined]


def test_a_step_can_depend_on_the_request() -> None:
    seen: list[RoleCallRequest] = []

    def step(r: RoleCallRequest) -> ScriptedCall:
        seen.append(r)
        return ScriptedCall.answer({"answer": r.role.value, "confidence": 1})

    result, _, _ = call_once(step)  # type: ignore[arg-type]
    assert result.output == {"answer": "editor", "confidence": 1}
    assert len(seen) == 1


def test_a_call_cannot_have_an_answer_and_tools_in_one_turn_or_answer_early() -> None:
    with pytest.raises(ValueError, match="either calls tools or gives"):
        ScriptedTurn(tool_calls=(tool("view_file", path="a"),), final=ANSWER)
    with pytest.raises(ValueError, match="exactly one"):
        ScriptedTurn()  # a turn that does nothing is not a model response
    with pytest.raises(ValueError, match="only the last turn"):
        ScriptedCall(turns=(ScriptedTurn(final=ANSWER), ScriptedTurn(final=ANSWER)))
    with pytest.raises(ValueError, match="no final answer"):
        ScriptedCall(turns=(ScriptedTurn(final=ANSWER),), raises=RaiseKind.USAGE_LIMIT)


def test_lockdown_failure_can_be_scripted() -> None:
    with pytest.raises(LockdownViolation, match="built-in"):
        FakeRuntime(lockdown_error="built-in tools enabled").check_lockdown_static()


# ---------------------------------------------------------------- results that must be rejected


@pytest.mark.parametrize(
    ("override", "error", "match"),
    [
        ({"model_id": SONNET}, ForbiddenModelError, SONNET),
        ({"models_seen_extra": (SONNET,)}, ForbiddenModelError, "only claude-opus"),
        ({"thinking_enabled": False}, LockdownViolation, "thinking"),
        ({"effort_applied": Effort.MEDIUM}, RuntimeCallError, "ran at"),
    ],
)
def test_scripts_can_misreport_so_the_orchestrator_check_can_be_tested(
    override: dict[str, Any], error: type[Exception], match: str
) -> None:
    script = ScriptedCall.answer(ANSWER, **override)
    request = req()
    result: RoleCallResult = run(FakeRuntime([script]).call(request, host(), guard()))
    with pytest.raises(error, match=match):
        check_result(request, result)


def test_play_is_usable_without_the_runtime_class() -> None:
    invocations: list[Any] = []
    result: RoleCallResult = run(
        play(ScriptedCall.answer(ANSWER), req(), host(), guard(), invocations, strict_outputs=True)
    )
    assert result.stop is StopReason.COMPLETED
    assert invocations == []


# ---------------------------------------------------------------- rows: ToolCall and LLMCall


def test_llm_call_rows_record_what_actually_ran() -> None:
    request = req()
    result, _, _ = call_once(
        ScriptedCall(turns=(ScriptedTurn(final=ANSWER, usage=Usage(7, 3, 11, 5)),), latency_ms=42)
    )
    row = llm_call_from(request, result, runtime="fake", redact=no_redact, api_equiv_usd=0.25)
    assert (row.id, row.run_id, row.role) == (request.call_id, RUN, Role.EDITOR)
    assert (row.step_id, row.attempt_id, row.parent_call_id) == (
        request.context.step_id,
        request.context.attempt_id,
        None,
    )
    assert (row.stop, row.turns, row.output) == (StopReason.COMPLETED, 1, ANSWER)
    assert (row.runtime, row.model, row.tier, row.effort) == ("fake", OPUS, "opus", "high")
    assert (row.input_tokens, row.output_tokens) == (7, 3)
    assert (row.cache_read_tokens, row.cache_write_tokens) == (11, 5)
    assert row.thinking
    assert row.latency_ms == 42
    assert row.api_equiv_usd == 0.25
    assert row.routing_decision_id == request.routing_decision_id
    assert row.limit_error is None


def test_a_scripted_run_writes_tool_call_and_llm_call_rows_to_the_store(tmp_path: Path) -> None:
    """The P1-11 acceptance: scripted run -> ToolCall and LLMCall rows, a denial among them."""
    request = req()
    script = ScriptedCall.using(
        tool("view_file", path="a.py"),
        ScriptedToolCall(name="Bash", args={"command": "cat ~/.ssh/id_rsa"}),  # scripted denial
        then=ANSWER,
    )
    runtime, h = FakeRuntime([script]), host()
    result: RoleCallResult = run(runtime.call(request, h, guard()))
    check_result(request, result)

    store = SqliteRunStore.open(tmp_path / "mpilot.db")
    try:
        run_row = SAMPLES[d.Run]()
        assert isinstance(run_row, d.Run)
        store.create_run(
            run_row.model_copy(
                update={"id": RUN, "status": d.RunStatus.REPAIR_LOOP, "paused_at_state": None}
            )
        )
        decision = SAMPLES[d.RoutingDecision]()
        assert isinstance(decision, d.RoutingDecision)
        store.append(decision.model_copy(update={"id": RoutingDecisionId("rd-1"), "run_id": RUN}))
        store.append(llm_call_from(request, result, runtime=runtime.name, redact=no_redact))
        for row in h.calls:  # what the real ToolHost (P4-07) persists
            store.append(row)

        (llm,) = store.rows(RUN, d.LLMCall)
        tool_rows = store.rows(RUN, d.ToolCall)
        assert llm.id == request.call_id
        assert llm.runtime == "fake"
        assert [r.id for r in tool_rows] == list(result.tool_call_ids)
        assert [(r.tool, r.policy_decision.value) for r in tool_rows] == [
            ("view_file", "allow"),
            ("Bash", "deny"),
        ]
        assert tool_rows[1].deny_reason
        assert {r.role_call_id for r in tool_rows} == {llm.id}  # joinable to their role call
    finally:
        store.close()


def test_each_call_gets_its_own_ids() -> None:
    runtime, h = FakeRuntime([ScriptedCall.answer(ANSWER)] * 2), host()
    first = run(runtime.call(req(context=ctx(role_call_id=LLMCallId("llm-1"))), h, guard()))
    second = run(runtime.call(req(context=ctx(role_call_id=LLMCallId("llm-2"))), h, guard()))
    assert (first.call_id, second.call_id) == ("llm-1", "llm-2")  # type: ignore[attr-defined]
