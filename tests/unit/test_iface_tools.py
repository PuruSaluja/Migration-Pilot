"""P1-07: tool layer interfaces (SPEC §9.1, §11.1) and the ToolHost test double."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from mpilot.core.types import LLMCallId, Role, RunId, StepId, StepType, ToolCallId
from mpilot.models.domain import PolicyDecision
from mpilot.policy import hooks
from mpilot.testing.doubles import DenyToolsPolicy, InMemoryToolHost, StaticTool
from mpilot.tools.base import (
    MCP_TOOL_PREFIX,
    REGISTRY_TOOL_NAMES,
    HostDenyCode,
    Tool,
    ToolContext,
    ToolEffect,
    ToolHost,
    ToolInvocation,
    ToolSpec,
    bare_name,
    mcp_name,
)

SPEC = Path(__file__).resolve().parents[2] / "SPEC.md"


def _spec_91_tool_names() -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### 9.1 Tool schemas", 1)[1].split("### 9.2", 1)[0]
    block = section.split("```yaml", 1)[1].split("```", 1)[0]
    return {m.group(1) for m in re.finditer(r"^([a-z_]+):", block, flags=re.MULTILINE)}


def test_registry_is_exactly_spec_91_plus_explore() -> None:
    spec_names = _spec_91_tool_names()
    assert len(spec_names) == 17
    assert spec_names | {"explore"} == REGISTRY_TOOL_NAMES


def test_no_commit_push_pr_or_github_tools() -> None:
    """§9.1: commits, pushes and PR creation are not agent tools; §11.3 control 2."""
    for name in REGISTRY_TOOL_NAMES:
        for word in ("commit", "push", "merge", "pull", "issue", "github", "branch"):
            assert word not in name, name
    git_tools = {n for n in REGISTRY_TOOL_NAMES if n.startswith("git")}
    assert git_tools == {"git_status_diff"}  # read-only, sandbox-side


def test_mcp_names_round_trip() -> None:
    for name in REGISTRY_TOOL_NAMES:
        assert mcp_name(name) == MCP_TOOL_PREFIX + name
        assert bare_name(mcp_name(name)) == name
    with pytest.raises(ValueError, match="not a registry tool"):
        mcp_name("Bash")


@pytest.mark.parametrize(
    "exposed",
    ["Bash", "view_file", "mcp__mpilot__Bash", "mcp__other__view_file", "mcp__mpilot__", "Task"],
)
def test_bare_name_refuses_anything_foreign(exposed: str) -> None:
    assert bare_name(exposed) is None


# ---------------------------------------------------------------- fixtures


class ViewArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class ViewOut(BaseModel):
    content: str
    total_lines: int


class OtherOut(BaseModel):
    x: int = 0


def _view_spec() -> ToolSpec:
    return ToolSpec("view_file", "Read a file.", ViewArgs, ViewOut, ToolEffect.READ)


def _view(args: BaseModel) -> BaseModel:
    assert isinstance(args, ViewArgs)
    return ViewOut(content=f"contents of {args.path}", total_lines=1)


def _view_tool() -> StaticTool:
    return StaticTool(_view_spec(), _view)


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


def _ids() -> Any:
    n = iter(range(1, 10_000))
    return lambda: ToolCallId(f"tc-{next(n)}")


def _host(**kw: Any) -> InMemoryToolHost:
    kw.setdefault("tools", [_view_tool()])
    kw.setdefault("allowlists", {Role.EDITOR: frozenset({"view_file"})})
    return InMemoryToolHost(new_id=_ids(), **kw)


def _invoke(
    host: InMemoryToolHost, name: str, args: dict[str, Any], ctx: ToolContext
) -> ToolInvocation:
    return asyncio.run(host.invoke(name, args, ctx))


# ---------------------------------------------------------------- ToolSpec / ToolContext


def test_tool_spec_rejects_bad_names_and_descriptions() -> None:
    with pytest.raises(ValueError, match="snake_case"):
        ToolSpec("ViewFile", "x", ViewArgs, ViewOut, ToolEffect.READ)
    with pytest.raises(ValueError, match="registry"):
        ToolSpec("git_push", "x", ViewArgs, ViewOut, ToolEffect.EXEC)
    with pytest.raises(ValueError, match="description"):
        ToolSpec("view_file", "  ", ViewArgs, ViewOut, ToolEffect.READ)
    assert _view_spec().input_schema()["required"] == ["path"]


def test_tool_context_invariants() -> None:
    _ctx()
    explorer = _ctx(role=Role.EXPLORER, caller=Role.CRITIC, step_id=None, step_type=None)
    assert explorer.caller is Role.CRITIC
    with pytest.raises(ValidationError, match="caller is set exactly"):
        _ctx(role=Role.EXPLORER)
    with pytest.raises(ValidationError, match="caller is set exactly"):
        _ctx(caller=Role.PLANNER)
    with pytest.raises(ValidationError, match="has no `explore` tool"):
        _ctx(role=Role.EXPLORER, caller=Role.EXPLORER)
    with pytest.raises(ValidationError, match="set together"):
        _ctx(step_type=None)
    with pytest.raises(ValidationError, match="need a step"):
        _ctx(step_id=None, step_type=None, step_target_files=frozenset({"a.py"}))
    with pytest.raises(ValidationError):
        _ctx(max_tool_calls=-1)


def test_tool_invocation_invariants_and_model_view() -> None:
    ok = ToolInvocation(
        tool_call_id=ToolCallId("tc-1"),
        tool="view_file",
        decision=PolicyDecision.ALLOW,
        output={"a": 1},
    )
    assert ok.for_model() == {"a": 1}
    denied = ToolInvocation(
        tool_call_id=ToolCallId("tc-2"),
        tool="Bash",
        decision=PolicyDecision.DENY,
        deny_code="unknown_tool",
        deny_reason="no",
    )
    assert denied.for_model() == {"denied": True, "code": "unknown_tool", "reason": "no"}
    with pytest.raises(ValidationError):
        ToolInvocation(tool_call_id=ToolCallId("t"), tool="x", decision=PolicyDecision.ALLOW)
    with pytest.raises(ValidationError):
        ToolInvocation(
            tool_call_id=ToolCallId("t"), tool="x", decision=PolicyDecision.DENY, deny_code="c"
        )
    with pytest.raises(ValidationError):
        ToolInvocation(
            tool_call_id=ToolCallId("t"),
            tool="x",
            decision=PolicyDecision.DENY,
            output={},
            deny_code="c",
            deny_reason="r",
        )


# ---------------------------------------------------------------- the invoke pipeline (double)


def test_conformance() -> None:
    host = _host()
    assert isinstance(host, ToolHost)
    assert isinstance(_view_tool(), Tool)
    assert isinstance(DenyToolsPolicy(frozenset()), hooks.ToolPolicy)


def test_allowed_call_runs_and_is_recorded() -> None:
    host = _host()
    inv = _invoke(host, mcp_name("view_file"), {"path": "a.py"}, _ctx())
    assert inv.decision is PolicyDecision.ALLOW
    assert inv.output is not None
    assert inv.output["total_lines"] == 1
    assert [c.tool for c in host.calls] == ["view_file"]
    assert host.calls[0].policy_decision is PolicyDecision.ALLOW


def test_sixteenth_editor_call_is_refused() -> None:
    host = _host()
    ctx = _ctx()
    results = [_invoke(host, mcp_name("view_file"), {"path": "a.py"}, ctx) for _ in range(16)]
    assert all(r.decision is PolicyDecision.ALLOW for r in results[:15])
    assert results[15].deny_code == HostDenyCode.TOOL_CALL_CAP
    assert len(host.calls) == 16
    # The cap is per role call: a new call id starts again.
    fresh = _invoke(
        host, mcp_name("view_file"), {"path": "a.py"}, _ctx(role_call_id=LLMCallId("llm-2"))
    )
    assert fresh.decision is PolicyDecision.ALLOW


def test_denied_calls_count_against_the_cap() -> None:
    host = _host()
    ctx = _ctx(max_tool_calls=2)
    _invoke(host, "Bash", {}, ctx)
    _invoke(host, "Bash", {}, ctx)
    assert (
        _invoke(host, mcp_name("view_file"), {"path": "a"}, ctx).deny_code
        == HostDenyCode.TOOL_CALL_CAP
    )


@pytest.mark.parametrize("name", ["Bash", "WebFetch", "view_file", "mcp__other__view_file"])
def test_foreign_names_are_denied_and_recorded(name: str) -> None:
    host = _host()
    inv = _invoke(host, name, {"command": "env"}, _ctx())
    assert inv.deny_code == HostDenyCode.UNKNOWN_TOOL
    assert host.calls[-1].policy_decision is PolicyDecision.DENY
    assert host.calls[-1].deny_reason


def test_explorer_callers_are_the_roles_with_explore() -> None:
    for caller in (Role.PR_WRITER, Role.B2_SINGLE_SHOT, Role.B3_GENERIC):
        with pytest.raises(ValidationError, match="has no `explore` tool"):
            _ctx(role=Role.EXPLORER, caller=caller, step_id=None, step_type=None)


def test_unexpected_tool_exception_is_recorded_then_raised() -> None:
    def boom(_a: BaseModel) -> BaseModel:
        raise RuntimeError("docker went away")

    host = _host(tools=[StaticTool(_view_spec(), boom)])
    with pytest.raises(RuntimeError, match="docker went away"):
        _invoke(host, mcp_name("view_file"), {"path": "a"}, _ctx())
    assert [c.tool for c in host.calls] == ["view_file"]
    assert "RuntimeError" in host.calls[0].output_summary


def test_registry_tool_this_host_lacks_is_unknown() -> None:
    inv = _invoke(_host(), mcp_name("run_command"), {"cmd": "ls"}, _ctx())
    assert inv.deny_code == HostDenyCode.UNKNOWN_TOOL


def test_role_allowlist_is_enforced_by_the_host() -> None:
    inv = _invoke(
        _host(),
        mcp_name("view_file"),
        {"path": "a"},
        _ctx(role=Role.PR_WRITER, step_id=None, step_type=None),
    )
    assert inv.deny_code == HostDenyCode.ROLE_NOT_ALLOWED


def test_invalid_args_never_reach_policies_or_the_tool() -> None:
    tool = _view_tool()
    host = _host(tools=[tool], policies=[DenyToolsPolicy(frozenset({"view_file"}))])
    inv = _invoke(host, mcp_name("view_file"), {"path": "a", "extra": 1}, _ctx())
    assert inv.deny_code == HostDenyCode.INVALID_ARGS
    assert tool.seen == []


def test_pre_policy_denial_stops_the_tool() -> None:
    tool = _view_tool()
    host = _host(tools=[tool], policies=[DenyToolsPolicy(frozenset({"view_file"}), code="h3")])
    inv = _invoke(host, mcp_name("view_file"), {"path": "a"}, _ctx())
    assert inv.deny_code == "h3"
    assert tool.seen == []


class _Redact:
    name = "redact"

    def pre(self, spec: ToolSpec, args: BaseModel, ctx: ToolContext) -> hooks.PolicyVerdict:
        return hooks.PolicyVerdict.allow()

    def post(self, spec: ToolSpec, output: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(output, ViewOut)
        return output.model_copy(update={"content": "[REDACTED]"})


class _BreaksSchema(_Redact):
    name = "breaks"

    def post(self, spec: ToolSpec, output: BaseModel, ctx: ToolContext) -> BaseModel:
        return OtherOut()


def test_post_policies_rewrite_output_before_the_model_sees_it() -> None:
    host = _host(policies=[_Redact()])
    inv = _invoke(host, mcp_name("view_file"), {"path": "a"}, _ctx())
    assert inv.output is not None
    assert inv.output["content"] == "[REDACTED]"
    assert "contents of" not in host.calls[0].output_summary


def test_post_policy_that_breaks_the_schema_is_a_bug() -> None:
    host = _host(policies=[_BreaksSchema()])
    with pytest.raises(TypeError, match="breaks"):
        _invoke(host, mcp_name("view_file"), {"path": "a"}, _ctx())


def test_run_pre_first_denial_wins() -> None:
    asked: list[str] = []

    class _Track(_Redact):
        def __init__(self, name: str, deny: bool) -> None:
            self.name, self._deny = name, deny

        def pre(self, spec: ToolSpec, args: BaseModel, ctx: ToolContext) -> hooks.PolicyVerdict:
            asked.append(self.name)
            if self._deny:
                return hooks.PolicyVerdict.deny(self.name, "c", "r")
            return hooks.PolicyVerdict.allow()

    v = hooks.run_pre(
        [_Track("a", False), _Track("b", True), _Track("c", True)],
        _view_spec(),
        ViewArgs(path="x"),
        _ctx(),
    )
    assert v.policy == "b"
    assert asked == ["a", "b"]


def test_specs_for_is_sorted_and_role_scoped() -> None:
    host = _host(allowlists={Role.EDITOR: frozenset({"view_file", "run_command"})})
    assert [s.name for s in host.specs_for(Role.EDITOR)] == ["view_file"]
    assert host.specs_for(Role.CRITIC) == []
    assert set(host.registry) == {"view_file"}


def test_duplicate_tools_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _host(tools=[_view_tool(), _view_tool()])


def test_policy_verdict_invariants() -> None:
    assert hooks.PolicyVerdict.allow().allowed
    d = hooks.PolicyVerdict.deny("p", "c", "r")
    assert not d.allowed
    with pytest.raises(ValidationError):
        hooks.PolicyVerdict(decision=PolicyDecision.DENY, policy="p", code="c")
    with pytest.raises(ValidationError):
        hooks.PolicyVerdict(decision=PolicyDecision.ALLOW, code="c")
