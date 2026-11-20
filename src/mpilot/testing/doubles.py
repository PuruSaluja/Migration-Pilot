"""Minimal test doubles for the P1-07 interfaces.

They live in `src` so `mypy --strict` proves they conform to each Protocol (see
`_static_conformance` at the bottom), and later tasks reuse them: P1-11 needs "a minimal
test-double ToolHost". They are deliberately small. The scripted `FakeRuntime` and the
`CassetteRuntime` are P1-11's, the real ToolHost is P4-07's, the real router P4-02's.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from mpilot.agents.router import (
    RouteOutcome,
    RoutingContext,
    RoutingPolicies,
    minimal_sticky_after,
)
from mpilot.agents.runtime import (
    BudgetGuard,
    RoleCallRequest,
    RoleCallResult,
    RuntimeCapabilities,
    SelftestCheck,
    SelftestReport,
)
from mpilot.core.errors import BudgetExceededError
from mpilot.core.types import Effort, Role, StopReason, Tier, ToolCallId, Usage, new_tool_call_id
from mpilot.models.artifacts import TreeEntry, TreeSnapshot
from mpilot.models.domain import PolicyDecision, ToolCall, Violation
from mpilot.policy.gate import GateInput, PolicyCheck, TreeReader
from mpilot.policy.hooks import PolicyVerdict, ToolPolicy, run_post, run_pre
from mpilot.policy.test_files import TestFileClassifier
from mpilot.tools.base import (
    HostDenyCode,
    Tool,
    ToolContext,
    ToolHost,
    ToolInvocation,
    ToolSpec,
    bare_name,
)

_OUTPUT_SUMMARY_CHARS = 200


# ---------------------------------------------------------------- router


@dataclass(frozen=True)
class FixedRouter:
    """Always returns one tier/effort with `minimal_sticky_after`, so opus/high outcomes pass
    the oracle except where Sonnet is required."""

    tier: Tier = Tier.OPUS
    effort: Effort = Effort.HIGH
    reason: str = "test_double.fixed"

    def route(self, ctx: RoutingContext, policies: RoutingPolicies) -> RouteOutcome:
        return RouteOutcome(
            tier=self.tier,
            effort=self.effort,
            reason_codes=(self.reason,),
            new_sticky=minimal_sticky_after(ctx, policies),
            inputs_hash=ctx.inputs_hash(policies),
        )


# ---------------------------------------------------------------- budget guard


@dataclass
class TurnLimitGuard:
    """Raises once more than `max_turns` turns were charged; records every charge."""

    max_turns: int
    charged: list[Usage | None] = field(default_factory=list)
    tool_checks: int = 0

    def after_turn(self, usage: Usage | None) -> None:
        self.charged.append(usage)
        if len(self.charged) > self.max_turns:
            raise BudgetExceededError(f"turn cap {self.max_turns} crossed")

    def before_tool_call(self) -> None:
        self.tool_checks += 1
        if len(self.charged) > self.max_turns:
            raise BudgetExceededError(f"turn cap {self.max_turns} crossed")


# ---------------------------------------------------------------- tool host


@dataclass
class InMemoryToolHost:
    """The `ToolHost.invoke` pipeline (steps 1-7) with an in-memory ToolCall list instead of
    the DB and no checkpoints. `allowlists` maps role -> bare tool names (P4-11 owns the real
    ones); a role absent from it may call nothing."""

    tools: Sequence[Tool[Any, Any]]
    allowlists: Mapping[Role, frozenset[str]] = field(default_factory=dict)
    policies: Sequence[ToolPolicy] = ()
    new_id: Callable[[], ToolCallId] = new_tool_call_id
    calls: list[ToolCall] = field(default_factory=list)
    _counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names = [t.spec.name for t in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate tools: {sorted(names)}")
        self._by_name = {t.spec.name: t for t in self.tools}

    @property
    def registry(self) -> Mapping[str, ToolSpec]:
        return {name: tool.spec for name, tool in self._by_name.items()}

    def specs_for(self, role: Role) -> Sequence[ToolSpec]:
        allowed = self.allowlists.get(role, frozenset())
        return [self._by_name[n].spec for n in sorted(allowed) if n in self._by_name]

    async def invoke(
        self, name: str, raw_args: Mapping[str, Any], ctx: ToolContext
    ) -> ToolInvocation:
        started = time.monotonic()
        call_id = self.new_id()
        count = self._counts.get(ctx.role_call_id, 0) + 1
        self._counts[ctx.role_call_id] = count
        bare = bare_name(name)
        shown = bare or name

        def deny(code: str, reason: str) -> ToolInvocation:
            self._record(call_id, shown, raw_args, ctx, started, None, reason)
            return ToolInvocation(
                tool_call_id=call_id,
                tool=shown,
                decision=PolicyDecision.DENY,
                deny_code=code,
                deny_reason=reason,
            )

        if count > ctx.max_tool_calls:
            return deny(HostDenyCode.TOOL_CALL_CAP, f"at most {ctx.max_tool_calls} tool calls")
        if bare is None or bare not in self._by_name:
            return deny(HostDenyCode.UNKNOWN_TOOL, f"{name!r} is not an mpilot tool")
        if bare not in self.allowlists.get(ctx.role, frozenset()):
            return deny(HostDenyCode.ROLE_NOT_ALLOWED, f"{ctx.role} may not call {bare}")
        tool = self._by_name[bare]
        try:
            args = tool.spec.input_model.model_validate(dict(raw_args))
        except ValidationError as exc:
            return deny(HostDenyCode.INVALID_ARGS, f"invalid arguments: {exc.error_count()} errors")
        verdict = run_pre(self.policies, tool.spec, args, ctx)
        if not verdict.allowed:
            return deny(str(verdict.code), str(verdict.reason))  # both set on a denial
        try:
            output = run_post(self.policies, tool.spec, await tool.run(args, ctx), ctx)
        except Exception as exc:
            self._record(call_id, bare, raw_args, ctx, started, {"error": type(exc).__name__}, None)
            raise
        dumped = output.model_dump(mode="json")
        self._record(call_id, bare, raw_args, ctx, started, dumped, None)
        return ToolInvocation(
            tool_call_id=call_id, tool=bare, decision=PolicyDecision.ALLOW, output=dumped
        )

    def _record(
        self,
        call_id: ToolCallId,
        tool: str,
        raw_args: Mapping[str, Any],
        ctx: ToolContext,
        started: float,
        output: dict[str, Any] | None,
        deny_reason: str | None,
    ) -> None:
        self.calls.append(
            ToolCall(
                id=call_id,
                run_id=ctx.run_id,
                role_call_id=ctx.role_call_id,
                attempt_id=ctx.attempt_id,
                role=ctx.role,
                tool=tool,
                input=dict(raw_args),
                output_summary=str(output)[:_OUTPUT_SUMMARY_CHARS] if output is not None else "",
                duration_ms=int((time.monotonic() - started) * 1000),
                policy_decision=PolicyDecision.DENY if deny_reason else PolicyDecision.ALLOW,
                deny_reason=deny_reason,
            )
        )


@dataclass(frozen=True)
class DenyToolsPolicy:
    """Denies the named tools with one code; passes outputs through."""

    tools: frozenset[str]
    code: str = "test_double.denied"
    name: str = "deny_tools"

    def pre(self, spec: ToolSpec, args: BaseModel, ctx: ToolContext) -> PolicyVerdict:
        if spec.name in self.tools:
            return PolicyVerdict.deny(self.name, self.code, f"{spec.name} is denied")
        return PolicyVerdict.allow()

    def post(self, spec: ToolSpec, output: BaseModel, ctx: ToolContext) -> BaseModel:
        return output


# ---------------------------------------------------------------- runtime


@dataclass
class EchoRuntime:
    """Answers every call with `output` in one turn, without tool calls. Enough to show the
    contract; the scripted FakeRuntime is P1-11."""

    output: Mapping[str, Any]
    name: str = "echo"
    capabilities: RuntimeCapabilities = field(
        default_factory=lambda: RuntimeCapabilities(
            per_call_effort=True,
            default_effort_verified_high=False,
            prompt_caching=False,
            context_editing=False,
        )
    )

    def check_lockdown_static(self) -> None:
        return None

    async def call(
        self, req: RoleCallRequest, tools: ToolHost, guard: BudgetGuard
    ) -> RoleCallResult:
        usage = Usage(input_tokens=1, output_tokens=1)
        try:
            guard.after_turn(usage)
        except BudgetExceededError:
            stop, output = StopReason.BUDGET_STOP, None
        else:
            missing = set(req.output_schema.get("required", [])) - set(self.output)
            ok = not missing
            stop = StopReason.COMPLETED if ok else StopReason.SCHEMA_INVALID
            output = dict(self.output) if ok else None
        return RoleCallResult(
            call_id=req.call_id,
            stop=stop,
            output=output,
            model_id=req.choice.model_id,
            effort_applied=req.choice.effort,
            models_seen=frozenset({req.choice.model_id}),
            thinking_enabled=True,
            usage=usage,
            turns=1,
            latency_ms=0,
        )

    async def selftest(self) -> SelftestReport:
        return SelftestReport(runtime=self.name, checks=(SelftestCheck(name="echo", passed=True),))


# ---------------------------------------------------------------- policy


@dataclass(frozen=True)
class SetClassifier:
    """Classifies by exact path membership (the D2 pattern classifier is P2-15)."""

    test_files: frozenset[str] = frozenset()
    test_configs: frozenset[str] = frozenset()
    embedding: frozenset[str] = frozenset()

    def is_test_file(self, rel_path: str) -> bool:
        return rel_path in self.test_files

    def is_test_config(self, rel_path: str) -> bool:
        return rel_path in self.test_configs

    def may_embed_test_config(self, rel_path: str) -> bool:
        return rel_path in self.embedding


@dataclass(frozen=True)
class DictTree:
    """A TreeReader over an in-memory {path: text} map."""

    commit: str
    files: Mapping[str, str] = field(default_factory=dict)

    def snapshot(self) -> TreeSnapshot:
        return TreeSnapshot(
            entries={
                p: TreeEntry(mode="100644", sha256=hashlib.sha256(t.encode()).hexdigest())
                for p, t in self.files.items()
            }
        )

    def paths(self) -> frozenset[str]:
        return frozenset(self.files)

    def read_text(self, rel_path: str) -> str | None:
        return self.files.get(rel_path)


@dataclass(frozen=True)
class ConstantCheck:
    """A PolicyCheck that always returns the same violations."""

    id: str
    violations: tuple[Violation, ...] = ()

    def evaluate(self, inp: GateInput) -> list[Violation]:
        return list(self.violations)


def violation(check_id: str, code: str = "test", message: str = "test") -> Violation:
    return Violation(check_id=check_id, code=code, message=message)


# ---------------------------------------------------------------- tools


@dataclass
class StaticTool:
    """A tool that returns `make_output(args)` and records the arguments it saw."""

    spec: ToolSpec
    make_output: Callable[[BaseModel], BaseModel]
    seen: list[BaseModel] = field(default_factory=list)

    async def run(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        self.seen.append(args)
        return self.make_output(args)


# ---------------------------------------------------------------- static conformance


def _static_conformance(  # pragma: no cover - checked by mypy, never called
    router: FixedRouter,
    guard: TurnLimitGuard,
    host: InMemoryToolHost,
    policy: DenyToolsPolicy,
    runtime: EchoRuntime,
    classifier: SetClassifier,
    tree: DictTree,
    check: ConstantCheck,
    tool: StaticTool,
) -> None:
    from mpilot.agents.router import ModelRouter
    from mpilot.agents.runtime import AgentRuntime

    _r: ModelRouter = router
    _g: BudgetGuard = guard
    _h: ToolHost = host
    _p: ToolPolicy = policy
    _a: AgentRuntime = runtime
    _c: TestFileClassifier = classifier
    _t: TreeReader = tree
    _k: PolicyCheck = check
    _o: Tool[BaseModel, BaseModel] = tool
