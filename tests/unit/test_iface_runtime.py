"""P1-07: AgentRuntime interface (SPEC §8.1, §8.3, NFR-12) and its value types."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from mpilot.agents.runtime import (
    AgentRuntime,
    BudgetGuard,
    ModelChoice,
    RoleCallRequest,
    RoleCallResult,
    SelftestCheck,
    SelftestReport,
    StopReason,
    check_result,
)
from mpilot.core.errors import ForbiddenModelError, LockdownViolation, RuntimeCallError
from mpilot.core.types import (
    AttemptId,
    Effort,
    LLMCallId,
    Role,
    RoutingDecisionId,
    RunId,
    StepId,
    StepType,
    Tier,
    ToolCallId,
    Usage,
)
from mpilot.testing.doubles import EchoRuntime, InMemoryToolHost, TurnLimitGuard
from mpilot.tools.base import ToolContext

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["answer", "confidence"],
}


def _ctx(**kw: Any) -> ToolContext:
    base: dict[str, Any] = {
        "run_id": RunId("run-1"),
        "role_call_id": LLMCallId("llm-1"),
        "role": Role.EDITOR,
        "step_id": StepId("step-1"),
        "step_type": StepType.EDIT_CLUSTER,
        "max_tool_calls": 15,
    }
    base.update(kw)
    return ToolContext(**base)


def _req(**kw: Any) -> RoleCallRequest:
    base: dict[str, Any] = {
        "context": _ctx(),
        "choice": ModelChoice(tier=Tier.OPUS, effort=Effort.HIGH, model_id=OPUS),
        "routing_decision_id": RoutingDecisionId("rd-1"),
        "system_prompt": "rules",
        "user_prompt": "<untrusted_repo_content>x</untrusted_repo_content>",
        "tool_allowlist": frozenset({"view_file", "str_replace"}),
        "output_schema": SCHEMA,
        "max_turns": 10,
    }
    base.update(kw)
    return RoleCallRequest(**base)


def _res(**kw: Any) -> RoleCallResult:
    base: dict[str, Any] = {
        "call_id": LLMCallId("llm-1"),
        "stop": StopReason.COMPLETED,
        "output": {"answer": "a", "confidence": 0.9},
        "model_id": OPUS,
        "effort_applied": Effort.HIGH,
        "models_seen": frozenset({OPUS}),
        "thinking_enabled": True,
        "usage": Usage(input_tokens=10, output_tokens=5),
        "turns": 2,
        "latency_ms": 100,
    }
    base.update(kw)
    return RoleCallResult(**base)


# ---------------------------------------------------------------- ModelChoice


@pytest.mark.parametrize("model_id", ["claude-haiku-4-5", "claude-sonnet-HAIKU", "x-Haiku"])
def test_model_choice_rejects_haiku(model_id: str) -> None:
    with pytest.raises(ValidationError, match="Haiku"):
        ModelChoice(tier=Tier.SONNET, effort=Effort.HIGH, model_id=model_id)


def test_model_choice_rejects_opus_medium_and_tier_mismatch() -> None:
    with pytest.raises(ValidationError, match="medium"):
        ModelChoice(tier=Tier.OPUS, effort=Effort.MEDIUM, model_id=OPUS)
    with pytest.raises(ValidationError, match="not a opus model"):
        ModelChoice(tier=Tier.OPUS, effort=Effort.HIGH, model_id=SONNET)
    with pytest.raises(ValidationError):
        ModelChoice(tier="haiku", effort=Effort.HIGH, model_id=SONNET)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ModelChoice(tier=Tier.SONNET, effort="low", model_id=SONNET)  # type: ignore[arg-type]
    assert (
        ModelChoice(tier=Tier.SONNET, effort=Effort.MEDIUM, model_id=SONNET).effort is Effort.MEDIUM
    )


# ---------------------------------------------------------------- RoleCallRequest


def test_request_exposes_context() -> None:
    r = _req()
    assert r.call_id == "llm-1"
    assert r.role is Role.EDITOR


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}},
        {
            "type": "object",
            "properties": {"confidence": {"type": "number"}},
            "required": ["confidence"],
        },
        {
            "type": "object",
            "properties": {"confidence": {"type": "number", "minimum": 0, "maximum": 1}},
        },
        {
            "type": "object",
            "properties": {"confidence": {"type": "integer", "minimum": 0, "maximum": 1}},
            "required": ["confidence"],
        },
    ],
)
def test_output_schema_must_require_a_confidence_in_0_1(schema: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="confidence"):
        _req(output_schema=schema)


@pytest.mark.parametrize("name", ["Bash", "mcp__mpilot__view_file", "git_push", "View_File"])
def test_allowlist_takes_bare_registry_names_only(name: str) -> None:
    with pytest.raises(ValidationError, match="bare registry tool name"):
        _req(tool_allowlist=frozenset({name}))


def test_request_rejects_unknown_fields_and_bad_turns() -> None:
    with pytest.raises(ValidationError):
        _req(max_turns=0)
    with pytest.raises(ValidationError):
        RoleCallRequest(**{**_req().model_dump(), "permission_mode": "bypassPermissions"})


def test_request_hash_is_stable_and_sensitive() -> None:
    a = _req(tool_allowlist=frozenset({"view_file", "str_replace", "search_code"}))
    b = _req(tool_allowlist=frozenset({"search_code", "view_file", "str_replace"}))
    assert a.request_hash() == b.request_hash()
    assert a.model_dump(mode="json")["tool_allowlist"] == [
        "search_code",
        "str_replace",
        "view_file",
    ]
    assert _req(user_prompt="other").request_hash() != _req().request_hash()
    sonnet = ModelChoice(tier=Tier.SONNET, effort=Effort.HIGH, model_id=SONNET)
    assert _req(choice=sonnet).request_hash() != _req().request_hash()
    assert len(a.request_hash()) == 64


def test_request_round_trips_through_json() -> None:
    r = _req()
    assert RoleCallRequest.model_validate_json(r.model_dump_json()) == r


# ---------------------------------------------------------------- RoleCallResult / check_result


def test_result_output_present_exactly_when_completed() -> None:
    with pytest.raises(ValidationError, match="output is present"):
        _res(output=None)
    with pytest.raises(ValidationError, match="output is present"):
        _res(stop=StopReason.SCHEMA_INVALID)
    _res(stop=StopReason.BUDGET_STOP, output=None)


def test_result_cannot_even_represent_haiku() -> None:
    with pytest.raises(ValidationError, match="Haiku"):
        _res(models_seen=frozenset({OPUS, "claude-haiku-4-5"}))


def test_check_result_accepts_a_matching_result() -> None:
    check_result(_req(), _res())


@pytest.mark.parametrize(
    ("override", "error", "match"),
    [
        ({"model_id": SONNET, "models_seen": frozenset({SONNET})}, ForbiddenModelError, SONNET),
        ({"models_seen": frozenset({OPUS, SONNET})}, ForbiddenModelError, "only claude-opus"),
        ({"thinking_enabled": False}, LockdownViolation, "thinking"),
        ({"call_id": LLMCallId("llm-other")}, RuntimeCallError, "answers request"),
        ({"turns": 11}, RuntimeCallError, "turns"),
        (
            {"tool_call_ids": tuple(ToolCallId(f"t{i}") for i in range(17))},
            RuntimeCallError,
            "must stop",
        ),
    ],
)
def test_check_result_rejects(override: dict[str, Any], error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        check_result(_req(), _res(**override))


def test_check_result_rejects_an_effort_the_call_did_not_run_at() -> None:
    req = _req(choice=ModelChoice(tier=Tier.SONNET, effort=Effort.MEDIUM, model_id=SONNET))
    res = _res(model_id=SONNET, models_seen=frozenset({SONNET}), effort_applied=Effort.HIGH)
    with pytest.raises(RuntimeCallError, match="ran at high"):
        check_result(req, res)


def test_selftest_report_needs_at_least_one_check() -> None:
    assert not SelftestReport(runtime="x", checks=()).passed
    ok = SelftestCheck(name="a", passed=True)
    assert SelftestReport(runtime="x", checks=(ok,)).passed
    assert not SelftestReport(
        runtime="x", checks=(ok, SelftestCheck(name="b", passed=False))
    ).passed


# ---------------------------------------------------------------- conformance via the doubles


def test_doubles_conform() -> None:
    assert isinstance(EchoRuntime(output={}), AgentRuntime)
    assert isinstance(TurnLimitGuard(max_turns=1), BudgetGuard)


def test_echo_runtime_obeys_the_contract() -> None:
    rt = EchoRuntime(output={"answer": "a", "confidence": 1.0})
    rt.check_lockdown_static()
    guard = TurnLimitGuard(max_turns=5)
    req = _req()
    res = asyncio.run(rt.call(req, InMemoryToolHost(tools=[]), guard))
    check_result(req, res)
    assert res.stop is StopReason.COMPLETED
    assert guard.charged == [res.usage]
    assert asyncio.run(rt.selftest()).passed


def test_budget_guard_ends_the_call() -> None:
    rt = EchoRuntime(output={"answer": "a", "confidence": 1.0})
    res = asyncio.run(rt.call(_req(), InMemoryToolHost(tools=[]), TurnLimitGuard(max_turns=0)))
    assert res.stop is StopReason.BUDGET_STOP
    assert res.output is None


def test_schema_invalid_answer_has_no_output() -> None:
    rt = EchoRuntime(output={"answer": "no confidence"})
    res = asyncio.run(rt.call(_req(), InMemoryToolHost(tools=[]), TurnLimitGuard(max_turns=5)))
    assert res.stop is StopReason.SCHEMA_INVALID
    assert res.output is None


# ---------------------------------------------------------------- review fixes


def test_request_hash_ignores_random_ids() -> None:
    """Cassettes (B6) must match on replay, where every ID is freshly generated."""
    a = _req()
    b = _req(
        context=_ctx(
            run_id=RunId("run-2"),
            role_call_id=LLMCallId("llm-9"),
            step_id=StepId("step-7"),
            attempt_id=AttemptId("att-3"),
        ),
        routing_decision_id=RoutingDecisionId("rd-9"),
    )
    assert a.request_hash() == b.request_hash()
    assert _req(context=_ctx(max_tool_calls=3)).request_hash() != a.request_hash()
    assert _req(context=_ctx(step_type=StepType.TEST_TOUCH)).request_hash() != a.request_hash()
    assert _req(max_turns=3).request_hash() != a.request_hash()


def test_cap_refusal_is_normal_but_a_call_past_it_is_not() -> None:
    """§9.1: the host refuses call 16; that refusal is a normal result. The runtime must
    then stop (TOOL_CALL_CAP), so a 17th invocation is a runtime bug."""
    from pydantic import BaseModel

    from mpilot.testing.doubles import StaticTool
    from mpilot.tools.base import ToolEffect, ToolSpec, mcp_name

    class Args(BaseModel):
        path: str

    class Out(BaseModel):
        ok: bool = True

    spec = ToolSpec("view_file", "Read a file.", Args, Out, ToolEffect.READ)
    host = InMemoryToolHost(
        tools=[StaticTool(spec, lambda _a: Out())],
        allowlists={Role.EDITOR: frozenset({"view_file"})},
    )
    req = _req()
    invs = [
        asyncio.run(host.invoke(mcp_name("view_file"), {"path": "a"}, req.context))
        for _ in range(16)
    ]
    assert invs[-1].deny_code == "tool_call_cap"
    ids = tuple(i.tool_call_id for i in invs)
    check_result(req, _res(tool_call_ids=ids, stop=StopReason.TOOL_CALL_CAP, output=None))
    with pytest.raises(RuntimeCallError, match="must stop"):
        check_result(req, _res(tool_call_ids=(*ids, ToolCallId("tc-extra"))))
