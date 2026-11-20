"""Shared builders for the FakeRuntime and cassette tests: a request, a tool host with one
read tool, deterministic IDs, and a guard that never trips."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from mpilot.agents.runtime import ModelChoice, RoleCallRequest
from mpilot.core.types import (
    Effort,
    LLMCallId,
    Role,
    RoutingDecisionId,
    RunId,
    StepId,
    StepType,
    Tier,
    ToolCallId,
)
from mpilot.testing.doubles import InMemoryToolHost, StaticTool, TurnLimitGuard
from mpilot.tools.base import ToolContext, ToolEffect, ToolSpec

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
RUN = RunId("run-1")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["answer", "confidence"],
}
ANSWER: dict[str, Any] = {"answer": "done", "confidence": 0.9}


class ViewArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class ViewOut(BaseModel):
    content: str
    total_lines: int


def view_tool() -> StaticTool:
    spec = ToolSpec("view_file", "Read a file.", ViewArgs, ViewOut, ToolEffect.READ)

    def run(args: BaseModel) -> BaseModel:
        assert isinstance(args, ViewArgs)
        return ViewOut(content=f"contents of {args.path}", total_lines=1)

    return StaticTool(spec, run)


def ids() -> Any:
    n = iter(range(1, 10_000))
    return lambda: ToolCallId(f"tc-{next(n)}")


def host(**kw: Any) -> InMemoryToolHost:
    kw.setdefault("tools", [view_tool()])
    kw.setdefault("allowlists", {Role.EDITOR: frozenset({"view_file"})})
    return InMemoryToolHost(new_id=ids(), **kw)


def ctx(**kw: Any) -> ToolContext:
    base: dict[str, Any] = {
        "run_id": RUN,
        "role_call_id": LLMCallId("llm-1"),
        "role": Role.EDITOR,
        "step_id": StepId("step-1"),
        "step_type": StepType.EDIT_CLUSTER,
        "max_tool_calls": 15,
    }
    base.update(kw)
    return ToolContext(**base)


def req(**kw: Any) -> RoleCallRequest:
    base: dict[str, Any] = {
        "context": ctx(),
        "choice": ModelChoice(tier=Tier.OPUS, effort=Effort.HIGH, model_id=OPUS),
        "routing_decision_id": RoutingDecisionId("rd-1"),
        "system_prompt": "rules",
        "user_prompt": "<untrusted_repo_content>x</untrusted_repo_content>",
        "tool_allowlist": frozenset({"view_file"}),
        "output_schema": SCHEMA,
        "max_turns": 10,
    }
    base.update(kw)
    return RoleCallRequest(**base)


def guard(max_turns: int = 1000) -> TurnLimitGuard:
    return TurnLimitGuard(max_turns)


def no_redact(line: str) -> str:
    """Identity redactor for tests whose answers hold no secrets (A3 is P4-14)."""
    return line
