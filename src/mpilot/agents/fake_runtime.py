"""`FakeRuntime`: a scripted `AgentRuntime` for tests (P1-11, SPEC §18 rule 7).

Tests never call real Claude. A `ScriptedCall` says what the "model" does in one role call:
which tools it asks for, turn by turn, and its final answer. The runtime plays it through the
real `ToolHost` and `BudgetGuard`, so everything the orchestrator relies on is exercised for
real: policy denials and `ToolCall` rows, the per-call tool cap, budget stops, schema
validation, and the exact `RoleCallResult` shape `check_result` judges.

`play` is the engine. `CassetteRuntime` (agents/cassette.py) uses the same one, so a replayed
recording behaves exactly like a scripted run.

What a script is, precisely, per model turn:

1. the turn completes: its usage is charged (`guard.after_turn`); if a cap is crossed the
   call ends BUDGET_STOP and none of the turn's tool calls run
2. each of its tool calls: `guard.before_tool_call()`, then `tools.invoke`; once the host has
   refused a call with TOOL_CALL_CAP, the next tool call is not forwarded and the call ends
   TOOL_CALL_CAP
3. a turn with `final` ends the call: COMPLETED, or SCHEMA_INVALID if the answer fails the
   request's schema

After the last turn, `raises` (a usage limit, say) fires as the "next request" to the model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mpilot.agents.runtime import (
    BudgetGuard,
    RoleCallRequest,
    RoleCallResult,
    RuntimeCapabilities,
    SelftestCheck,
    SelftestReport,
    schema_errors,
)
from mpilot.core.errors import (
    BudgetExceededError,
    LockdownViolation,
    RuntimeCallError,
    ScriptDivergedError,
    UsageLimitError,
)
from mpilot.core.types import Effort, Role, StopReason, Usage
from mpilot.models.domain import NonNegInt, PolicyDecision
from mpilot.tools.base import HostDenyCode, ToolHost, ToolInvocation, mcp_name


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RaiseKind(StrEnum):
    """What a script can make the runtime raise after its last turn."""

    USAGE_LIMIT = "usage_limit"  # UsageLimitError: the run pauses (FR-21)
    RUNTIME_ERROR = "runtime_error"  # RuntimeCallError: any other failure to get a result


class ScriptedToolCall(_Frozen):
    """One tool call the scripted model makes. `name` is exactly what the model sends, so a
    script can ask for `Bash` or `mcp__other__x` to prove the host denies it (H9)."""

    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    #: Optional expectations about what the host does. A mismatch means the code under test
    #: or a policy changed since the script (or cassette) was written: `ScriptDivergedError`.
    expect_decision: PolicyDecision | None = None
    expect_deny_code: str | None = None
    expect_output: dict[str, Any] | None = None  # compared only when `strict_outputs`


class ScriptedTurn(_Frozen):
    """One model response: it asks for tools, or gives the final answer."""

    tool_calls: tuple[ScriptedToolCall, ...] = ()
    final: dict[str, Any] | None = None
    usage: Usage = Field(default_factory=lambda: Usage(input_tokens=1, output_tokens=1))

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.final is not None) == bool(self.tool_calls):
            raise ValueError("a turn either calls tools or gives the final answer, exactly one")
        return self


class ScriptedCall(_Frozen):
    """What the model does in one role call, plus the facts the runtime reports about it.

    The report fields default to "as routed"; override them to build calls that must be
    rejected (`check_result`): another model, thinking off, a different effort.
    """

    turns: tuple[ScriptedTurn, ...] = ()
    raises: RaiseKind | None = None
    raise_message: str = "scripted"
    expect_role: Role | None = None  # fail loudly if the script is consumed by another role
    model_id: str | None = None
    models_seen_extra: tuple[str, ...] = ()
    thinking_enabled: bool = True
    effort_applied: Effort | None = None
    latency_ms: NonNegInt = 0

    @model_validator(mode="after")
    def _check(self) -> Self:
        for turn in self.turns[:-1]:
            if turn.final is not None:
                raise ValueError("only the last turn can give the final answer")
        if self.raises is not None and any(t.final is not None for t in self.turns):
            raise ValueError("a call that raises has no final answer")
        return self

    @classmethod
    def answer(cls, output: dict[str, Any], **kw: Any) -> Self:
        """One turn, no tools, this final answer."""
        return cls(turns=(ScriptedTurn(final=output),), **kw)

    @classmethod
    def using(cls, *tool_calls: ScriptedToolCall, then: dict[str, Any], **kw: Any) -> Self:
        """One turn asking for these tools, then a turn with the final answer."""
        return cls(turns=(ScriptedTurn(tool_calls=tool_calls), ScriptedTurn(final=then)), **kw)


def tool(name: str, **args: Any) -> ScriptedToolCall:
    """`tool("view_file", path="a.py")` asks for `mcp__mpilot__view_file`."""
    return ScriptedToolCall(name=mcp_name(name), args=args)


@dataclass(frozen=True)
class PlayedCall:
    """What one `call` did, for assertions: the request, its result (None if it raised), every
    tool invocation made, and the error it raised, if any."""

    request: RoleCallRequest
    result: RoleCallResult | None
    invocations: tuple[ToolInvocation, ...]
    error: BaseException | None = None


# ---------------------------------------------------------------- the engine


def _check_expectations(
    call: ScriptedToolCall, inv: ToolInvocation, *, strict_outputs: bool
) -> None:
    where = f"tool call {call.name} {call.args}"
    if call.expect_decision is not None and inv.decision is not call.expect_decision:
        raise ScriptDivergedError(
            f"{where}: expected {call.expect_decision.value}, the host said {inv.decision.value}"
            + (f" ({inv.deny_code}: {inv.deny_reason})" if inv.deny_code else "")
        )
    if call.expect_deny_code is not None and inv.deny_code != call.expect_deny_code:
        raise ScriptDivergedError(
            f"{where}: expected denial {call.expect_deny_code!r}, got {inv.deny_code!r}"
        )
    if strict_outputs and call.expect_output is not None and inv.output != call.expect_output:
        raise ScriptDivergedError(
            f"{where}: output differs from the expected one:\n  expected {call.expect_output}\n"
            f"  got      {inv.output}"
        )


async def play(
    script: ScriptedCall,
    req: RoleCallRequest,
    tools: ToolHost,
    guard: BudgetGuard,
    invocations: list[ToolInvocation],
    *,
    strict_outputs: bool,
) -> RoleCallResult:
    """Run `script` as the model's side of one role call (see the module docstring).

    Every tool invocation is appended to `invocations` as it happens, so the caller still has
    them if this raises (a usage limit, a diverged script).
    """
    if script.expect_role is not None and script.expect_role is not req.role:
        raise ScriptDivergedError(f"script is for {script.expect_role.value}, got {req.role.value}")

    usage, turns = Usage(), 0
    stop: StopReason | None = None
    output: dict[str, Any] | None = None
    cap_refused = False

    for turn in script.turns:
        if turns >= req.max_turns:
            stop = StopReason.MAX_TURNS
            break
        turns += 1
        usage += turn.usage
        try:
            guard.after_turn(turn.usage)
        except BudgetExceededError:
            stop = StopReason.BUDGET_STOP
            break
        for scripted in turn.tool_calls:
            if cap_refused:
                stop = StopReason.TOOL_CALL_CAP
                break
            try:
                guard.before_tool_call()
            except BudgetExceededError:
                stop = StopReason.BUDGET_STOP
                break
            inv = await tools.invoke(scripted.name, scripted.args, req.context)
            invocations.append(inv)
            _check_expectations(scripted, inv, strict_outputs=strict_outputs)
            cap_refused = inv.deny_code == HostDenyCode.TOOL_CALL_CAP
        if stop is not None:
            break
        if turn.final is not None:
            if schema_errors(req.output_schema, turn.final):
                stop = StopReason.SCHEMA_INVALID
            else:
                stop, output = StopReason.COMPLETED, dict(turn.final)
            break

    if stop is None:
        if script.raises is RaiseKind.USAGE_LIMIT:
            raise UsageLimitError(script.raise_message)
        if script.raises is RaiseKind.RUNTIME_ERROR:
            raise RuntimeCallError(script.raise_message)
        if turns >= req.max_turns:
            stop = StopReason.MAX_TURNS
        else:
            raise ScriptDivergedError(
                f"the script for {req.role.value} ended after {turns} turn(s) without a final "
                "answer, a limit error, or reaching max_turns"
            )

    model_id = script.model_id or req.choice.model_id
    return RoleCallResult(
        call_id=req.call_id,
        stop=stop,
        output=output,
        model_id=model_id,
        effort_applied=script.effort_applied or req.choice.effort,
        models_seen=frozenset({model_id, *script.models_seen_extra}),
        thinking_enabled=script.thinking_enabled,
        usage=usage,
        turns=turns,
        latency_ms=script.latency_ms,
        tool_call_ids=tuple(i.tool_call_id for i in invocations),
    )


# ---------------------------------------------------------------- the runtime

ScriptStep = ScriptedCall | Callable[[RoleCallRequest], ScriptedCall]


@dataclass
class FakeRuntime:
    """Plays one `ScriptedCall` per `call`, in order. A step can be a function of the request
    when the script depends on it. Running out of steps is a `ScriptDivergedError`; call
    `assert_exhausted()` at the end of a test to catch steps that were never used.

    `calls` records every call for assertions, including ones that raised.
    """

    script: Sequence[ScriptStep] = ()
    strict_outputs: bool = True
    #: Makes `check_lockdown_static` fail, to test how the orchestrator reacts (H9).
    lockdown_error: str | None = None
    name: str = "fake"
    capabilities: RuntimeCapabilities = field(
        default_factory=lambda: RuntimeCapabilities(
            per_call_effort=True,
            default_effort_verified_high=True,
            prompt_caching=False,
            context_editing=False,
        )
    )
    calls: list[PlayedCall] = field(default_factory=list)
    _next: int = 0

    @property
    def remaining(self) -> int:
        return len(self.script) - self._next

    def assert_exhausted(self) -> None:
        if self.remaining:
            raise ScriptDivergedError(f"{self.remaining} scripted call(s) were never used")

    def check_lockdown_static(self) -> None:
        if self.lockdown_error is not None:
            raise LockdownViolation(self.lockdown_error)

    async def call(
        self, req: RoleCallRequest, tools: ToolHost, guard: BudgetGuard
    ) -> RoleCallResult:
        if self._next >= len(self.script):
            raise ScriptDivergedError(
                f"no scripted call left for {req.role.value} (call {self._next + 1}; "
                f"the script has {len(self.script)})"
            )
        step = self.script[self._next]
        self._next += 1
        script = step(req) if callable(step) else step
        invocations: list[ToolInvocation] = []
        try:
            result = await play(
                script, req, tools, guard, invocations, strict_outputs=self.strict_outputs
            )
        except BaseException as exc:
            self.calls.append(PlayedCall(req, None, tuple(invocations), exc))
            raise
        self.calls.append(PlayedCall(req, result, tuple(invocations)))
        return result

    async def selftest(self) -> SelftestReport:
        return SelftestReport(
            runtime=self.name, checks=(SelftestCheck(name="fake", passed=True, detail="scripted"),)
        )
