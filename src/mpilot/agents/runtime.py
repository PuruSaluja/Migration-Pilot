"""Agent runtime interface (SPEC §8.1, §8.3, FR-21, NFR-12, H9).

One role call = one `AgentRuntime.call`. The orchestrator builds the request from state
(§10.3); the runtime runs the model loop and forwards every tool call to the `ToolHost`.
Implementations: `AgentSDKRuntime` (default, P4-08/09), `MessagesRuntime` (optional, not in
v1), `FakeRuntime` and `CassetteRuntime` (P1-11; tests never call real Claude).
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Protocol, Self, runtime_checkable

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

from mpilot.core.errors import ForbiddenModelError, LockdownViolation, RuntimeCallError
from mpilot.core.types import Effort, LLMCallId, Role, RoutingDecisionId, Tier, ToolCallId, Usage
from mpilot.model_policy import mentions_haiku
from mpilot.models.domain import LLMCall, NonNegInt, PositiveInt
from mpilot.tools.base import REGISTRY_TOOL_NAMES, ToolContext, ToolHost

_NameSet = Annotated[frozenset[str], PlainSerializer(sorted, return_type=list[str])]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelChoice(_Frozen):
    """Tier, effort and the concrete model ID, from the router plus validated config."""

    tier: Tier
    effort: Effort
    model_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if mentions_haiku(self.model_id):
            raise ValueError("Haiku is never used (NFR-12)")
        if self.tier.value not in self.model_id.lower():
            raise ValueError(f"{self.model_id!r} is not a {self.tier.value} model ID")
        if self.tier is Tier.OPUS and self.effort is Effort.MEDIUM:
            raise ValueError("Opus never runs at medium effort (§8.4)")
        return self


def _require_confidence(schema: dict[str, Any]) -> None:
    """§8.4: every role's output schema has a required `confidence` number in [0, 1]."""
    props = schema.get("properties")
    conf = props.get("confidence") if isinstance(props, dict) else None
    if not isinstance(conf, dict):
        raise ValueError("output_schema must define a `confidence` property (§8.4)")
    if (conf.get("type"), conf.get("minimum"), conf.get("maximum")) != ("number", 0, 1):
        raise ValueError("`confidence` must be {type: number, minimum: 0, maximum: 1}")
    if "confidence" not in schema.get("required", []):
        raise ValueError("`confidence` must be required")


class RoleCallRequest(_Frozen):
    """One role call, built by the orchestrator from state (fresh context, §10.3).

    Serializable on purpose (no live objects): cassettes key on `request_hash()` (B6).
    """

    context: ToolContext  # run, step, role, caller, role_call_id, max_tool_calls
    choice: ModelChoice
    routing_decision_id: RoutingDecisionId
    system_prompt: str = Field(min_length=1)  # stable prefix: rules, H6, tool docs, repo map
    user_prompt: str = Field(min_length=1)  # repo content already in <untrusted_repo_content>
    #: Bare registry names to expose (as `mcp__mpilot__<name>`). The ToolHost enforces the
    #: role's own allowlist regardless, so a wrong list here can only narrow, never widen.
    tool_allowlist: _NameSet = frozenset()
    output_schema: dict[str, Any]
    max_turns: PositiveInt  # already clamped by the BudgetManager (B2)

    @field_validator("tool_allowlist")
    @classmethod
    def _bare_registry_names(cls, v: frozenset[str]) -> frozenset[str]:
        for name in v:
            if name not in REGISTRY_TOOL_NAMES:
                raise ValueError(f"not a bare registry tool name: {name!r}")
        return v

    @field_validator("output_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        _require_confidence(v)
        return v

    @property
    def call_id(self) -> LLMCallId:
        return self.context.role_call_id

    @property
    def role(self) -> Role:
        return self.context.role

    def content(self) -> dict[str, Any]:
        """Everything that can change what the model does, as JSON-ready data, keyed by part.

        Run, call, step, attempt and routing-decision IDs are random per run
        (`core.types.new_*_id`), so they are excluded: a replay must match its recording.
        """
        ctx = self.context
        return {
            "role": ctx.role,
            "caller": ctx.caller,
            "step_type": ctx.step_type,
            "step_target_files": sorted(ctx.step_target_files),
            "flag_justifications": dict(ctx.flag_justifications),
            "max_tool_calls": ctx.max_tool_calls,
            "choice": self.choice.model_dump(mode="json"),
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "tool_allowlist": sorted(self.tool_allowlist),
            "output_schema": self.output_schema,
            "max_turns": self.max_turns,
        }

    def request_hash(self) -> str:
        """SHA-256 over the request's *content* (cassette key, B6); see `content`."""
        return _sha256_json(self.content())

    def part_hashes(self) -> dict[str, str]:
        """One hash per part of `content`, so a cassette mismatch can say what changed."""
        return {name: _sha256_json(value) for name, value in self.content().items()}


def _sha256_json(value: Any) -> str:
    canon = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


class StopReason(StrEnum):
    """Why a call ended normally. A usage or rate limit is not a stop reason: it raises
    `UsageLimitError` (FR-21). Cancellation is `asyncio.CancelledError`."""

    COMPLETED = "completed"  # final answer produced and schema-valid
    SCHEMA_INVALID = "schema_invalid"  # final answer failed `output_schema` (router trigger 3)
    MAX_TURNS = "max_turns"  # hit req.max_turns without a final answer
    BUDGET_STOP = "budget_stop"  # the BudgetGuard ended the call (B2 mid-call stop)
    TOOL_CALL_CAP = "tool_call_cap"  # a tool call after the cap refusal (§8.3, §9.1)


class RoleCallResult(_Frozen):
    """Outcome of one role call, recorded as an `LLMCall` row (§8.1 item 6)."""

    call_id: LLMCallId
    stop: StopReason
    output: dict[str, Any] | None = None  # schema-validated final answer; None unless COMPLETED
    model_id: str  # the model the call ran on
    effort_applied: Effort  # the effort it actually ran at, never just the requested one
    models_seen: _NameSet  # every model the runtime reports using in this call (A4)
    thinking_enabled: bool  # §8.1 item 10: must be True
    usage: Usage  # summed over turns, cache read/write included
    turns: NonNegInt
    latency_ms: NonNegInt
    #: Every `ToolHost.invoke` of this call, allowed and denied, in order. At most
    #: `max_tool_calls + 1`: the one over the cap is refused by the host (§9.1), and the
    #: runtime ends the call with TOOL_CALL_CAP instead of forwarding any further one.
    tool_call_ids: tuple[ToolCallId, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.output is not None) != (self.stop is StopReason.COMPLETED):
            raise ValueError("output is present exactly when stop == completed")
        if any(mentions_haiku(m) for m in (self.model_id, *self.models_seen)):
            raise ValueError("Haiku is never used (NFR-12)")
        return self


def check_result(req: RoleCallRequest, res: RoleCallResult) -> None:
    """The orchestrator's acceptance check on every result, whatever the runtime.

    A runtime is expected to raise these itself; this is the second line (§8.1 items 9-10,
    "a call is never logged at an effort it didn't run at").
    - ForbiddenModelError: `model_id` is not the routed model, or any other model shows up
      in `models_seen`
    - LockdownViolation: thinking was off
    - RuntimeCallError: a different call id, the wrong effort, more turns than allowed, or
      more than `max_tool_calls + 1` invocations (the cap refusal itself is normal)
    """
    routed = req.choice.model_id
    if res.model_id != routed or not res.models_seen <= {routed}:
        others = sorted(({res.model_id} | res.models_seen) - {routed})
        raise ForbiddenModelError(f"call {res.call_id} used {others}; only {routed} is routed")
    if not res.thinking_enabled:
        raise LockdownViolation(f"call {res.call_id} ran without extended thinking (§8.1 10)")
    if res.call_id != req.call_id:
        raise RuntimeCallError(f"result {res.call_id} answers request {req.call_id}")
    if res.effort_applied is not req.choice.effort:
        raise RuntimeCallError(
            f"call {res.call_id} ran at {res.effort_applied}, routed {req.choice.effort}"
        )
    if res.turns > req.max_turns:
        raise RuntimeCallError(f"call {res.call_id} ran {res.turns} > {req.max_turns} turns")
    if len(res.tool_call_ids) > req.context.max_tool_calls + 1:
        raise RuntimeCallError(
            f"call {res.call_id} made {len(res.tool_call_ids)} tool calls; the runtime must stop "
            f"after the refusal of call {req.context.max_tool_calls + 1}"
        )


def schema_errors(schema: dict[str, Any], answer: dict[str, Any]) -> list[str]:
    """Problems with a final answer under the role's JSON schema (draft 2020-12); empty means
    valid. A runtime ends the call with SCHEMA_INVALID when this is not empty (§8.4 trigger 3)."""
    validator = Draft202012Validator(schema)
    return sorted(
        f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(answer)
    )


def llm_call_from(
    req: RoleCallRequest, res: RoleCallResult, *, runtime: str, api_equiv_usd: float = 0.0
) -> LLMCall:
    """The `LLMCall` row for a finished call (§8.1 item 6, FR-18). Run `check_result` first:
    this records the model and effort the call *actually* ran at, and the tier the router
    chose. It carries `limit_error=None`; a call that ended on a limit has no result."""
    return LLMCall(
        id=res.call_id,
        run_id=req.context.run_id,
        role=req.role,
        runtime=runtime,
        model=res.model_id,
        tier=req.choice.tier,
        effort=res.effort_applied,
        routing_decision_id=req.routing_decision_id,
        input_tokens=res.usage.input_tokens,
        output_tokens=res.usage.output_tokens,
        cache_read_tokens=res.usage.cache_read_tokens,
        cache_write_tokens=res.usage.cache_write_tokens,
        thinking=res.thinking_enabled,
        api_equiv_usd=api_equiv_usd,
        latency_ms=res.latency_ms,
    )


class RuntimeCapabilities(_Frozen):
    """What a runtime can do. Verified, not assumed (P4-01, B1)."""

    per_call_effort: bool
    #: §8.1 / B1 fallback: without per-call effort the orchestrator forces `all_high`, but only
    #: if the runtime's default effort is verifiably high; otherwise it stops and asks the owner.
    default_effort_verified_high: bool
    prompt_caching: bool
    context_editing: bool


class SelftestCheck(_Frozen):
    name: str = Field(min_length=1)  # e.g. "builtin_tool_refused", "no_user_settings"
    passed: bool
    detail: str = ""


class SelftestReport(_Frozen):
    runtime: str
    checks: tuple[SelftestCheck, ...]

    @property
    def passed(self) -> bool:
        """True only if there is at least one check and every check passed."""
        return bool(self.checks) and all(c.passed for c in self.checks)


@runtime_checkable
class BudgetGuard(Protocol):
    """In-call budget enforcement (B2, NFR-1, H7), handed to each `AgentRuntime.call` by the
    BudgetManager (P1-08, P4-16). Both methods raise `BudgetExceededError` once any run cap
    (turns, tokens, time) is crossed; the runtime then ends the call with BUDGET_STOP and
    runs none of the pending tool calls, so the overshoot is at most one turn.
    """

    def after_turn(self, usage: Usage | None) -> None:
        """Charge one completed model turn. `None` means the runtime cannot report per-turn
        usage, and the guard charges its pessimistic per-turn estimate instead. The call's
        final `RoleCallResult.usage` supersedes these in-call charges. An interrupted call's
        charges still count (B5)."""
        ...

    def before_tool_call(self) -> None:
        """Check every cap, the wall clock included, before a tool call is forwarded."""
        ...


@runtime_checkable
class AgentRuntime(Protocol):
    """Runs role calls. The same contract for every implementation."""

    @property
    def name(self) -> str:
        """Recorded as `LLMCall.runtime`: "agent_sdk", "fake", "cassette", ..."""
        ...

    @property
    def capabilities(self) -> RuntimeCapabilities: ...

    def check_lockdown_static(self) -> None:
        """Validate §8.1 items 1-10 statically: options, hooks, env, model pins, thinking.
        Called at every runtime start, before the first call. Raises LockdownViolation."""
        ...

    async def call(
        self, req: RoleCallRequest, tools: ToolHost, guard: BudgetGuard
    ) -> RoleCallResult:
        """Run one role call.

        - model, effort and `max_turns` come from `req`; extended thinking is always on
        - expose exactly `mcp__mpilot__<name>` for `req.tool_allowlist`, nothing else
        - when a model turn's response arrives, first `guard.after_turn(usage_or_None)`,
          before forwarding any of that turn's tool calls. On BudgetExceededError the call
          ends BUDGET_STOP and none of them run: that is what bounds overshoot to one turn
          (US-5, H7). The crossing turn still counts in `turns` and `usage`; if it was the
          final answer, the answer is discarded (BUDGET_STOP, `output` None)
        - then, for each tool call of that turn, in order: `guard.before_tool_call()`
          (BudgetExceededError -> BUDGET_STOP, the rest do not run), then
          `tools.invoke(name_as_sent, args, req.context)`, then return
          `ToolInvocation.for_model()` to the model. Once the host has refused a call with
          `TOOL_CALL_CAP`, the next tool call is not forwarded: the call ends TOOL_CALL_CAP
        - a turn with no tool calls is the final answer: validate it against
          `req.output_schema` (COMPLETED, or SCHEMA_INVALID if not); a call that reaches
          `req.max_turns` without one ends MAX_TURNS

        Raises:
            UsageLimitError: subscription usage or rate limit (FR-21); never retried on a
                different model or effort (§8.4).
            ForbiddenModelError: any model other than the routed one reported (A4, NFR-12).
            LockdownViolation: a lockdown item failed at call time (H9).
            RuntimeCallError: anything else that prevents a result.
        """
        ...

    async def selftest(self) -> SelftestReport:
        """Live proof of the lockdown (`mpilot selftest`, P4-10): a built-in tool call is
        refused, no user settings are loaded, the env is the allowlist, no Haiku is used."""
        ...
