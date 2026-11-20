"""Tool layer interfaces (SPEC §9.1, §8.1, §11.1, NFR-4).

The only path from a model to a tool is:

    runtime -> in-process MCP server -> ToolHost.invoke -> ToolPolicy.pre -> Tool.run
            -> ToolPolicy.post (redaction) -> ToolCall row + checkpoint -> back to the model

Models only propose; the tool host decides. The registry is exactly the §9.1 tools plus
`explore` (§8.5): no GitHub, commit, push or PR tools ever (§9.1, §11.3 control 2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, Self, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mpilot.core.types import AttemptId, LLMCallId, Role, RunId, StepId, StepType, ToolCallId
from mpilot.models.domain import NonNegInt, PolicyDecision

#: Name of the in-process MCP server. Tools reach the model as `mcp__mpilot__<name>` (§8.1).
MCP_SERVER_NAME = "mpilot"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

#: The complete tool registry: §9.1 plus `explore` (§8.5). A test parses SPEC §9.1 against it.
REGISTRY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "view_file",
        "list_dir",
        "search_code",
        "find_symbol",
        "str_replace",
        "create_file",
        "delete_file",
        "run_command",
        "install_deps",
        "run_build",
        "run_typecheck",
        "run_tests",
        "git_status_diff",
        "changelog_search",
        "registry_lookup",
        "run_codemod",
        "write_note",
        "explore",
    }
)

_TOOL_NAME = re.compile(r"^[a-z][a-z_]*$")

#: Roles that have the `explore` tool (§8.2 tools column, §8.5), i.e. the only valid
#: `caller`s of an Explorer call. The PR Writer and the benchmark baselines have none.
EXPLORE_CALLER_ROLES = frozenset(
    {Role.ANALYZER, Role.PLANNER, Role.EDITOR, Role.DIAGNOSER, Role.CRITIC}
)


def mcp_name(tool: str) -> str:
    """The name the model sees for a registry tool."""
    if tool not in REGISTRY_TOOL_NAMES:
        raise ValueError(f"{tool!r} is not a registry tool")
    return MCP_TOOL_PREFIX + tool


def bare_name(exposed: str) -> str | None:
    """Inverse of `mcp_name`: the registry name, or None for anything that is not one of ours
    (a built-in tool, another server's tool, a typo). None must always be denied."""
    if not exposed.startswith(MCP_TOOL_PREFIX):
        return None
    name = exposed.removeprefix(MCP_TOOL_PREFIX)
    return name if name in REGISTRY_TOOL_NAMES else None


class ToolEffect(StrEnum):
    """What a tool can touch. Policies key off this, never off the tool's own description."""

    READ = "read"  # host-side workspace reads, sandbox searches, knowledge lookups
    WRITE = "write"  # host-side workspace edits (str_replace, create_file, delete_file)
    EXEC = "exec"  # runs in the no-network work container
    NETWORK = "network"  # install_deps only: the install container behind the proxy (§9.3)
    DELEGATE = "delegate"  # explore: asks the orchestrator for an Explorer call (§8.5)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static description of one tool. `input_model` is the JSON schema the model sees and the
    validator its arguments must pass before any policy or the tool itself sees them."""

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    effect: ToolEffect

    def __post_init__(self) -> None:
        if not _TOOL_NAME.match(self.name):
            raise ValueError(f"tool name must be snake_case: {self.name!r}")
        if self.name not in REGISTRY_TOOL_NAMES:
            raise ValueError(f"{self.name!r} is not in the §9.1 registry")
        if not self.description.strip():
            raise ValueError("a tool needs a description")

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()


class ToolContext(BaseModel):
    """Who is calling, and inside what. Built by the orchestrator for each role call; never
    from model output. Policies decide on this plus the validated arguments.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    role_call_id: LLMCallId  # the role call these tool calls belong to; ToolHost counts per id
    role: Role
    caller: Role | None = None  # set exactly for Explorer calls: the role that asked (§8.5)
    #: Set exactly for Explorer calls: the role call whose `explore` started this one, so the
    #: `LLMCall` row nests under it (FR-18 traces, §8.5 Critic citations).
    parent_call_id: LLMCallId | None = None
    step_id: StepId | None = None
    step_type: StepType | None = None
    attempt_id: AttemptId | None = None
    #: Files the current plan step names. For a `test_touch` step these are the only test
    #: files that may be written (H3, P4-12).
    step_target_files: frozenset[str] = frozenset()
    #: H8 (A11): flag -> plan-level justification, e.g. {"--legacy-peer-deps": "..."}.
    flag_justifications: Mapping[str, str] = Field(default_factory=dict)
    #: §8.3 Editor <= 15 per attempt, §8.5 Explorer <= 20. The host counts per
    #: `role_call_id`, so the orchestrator passes the *remaining* per-attempt allowance: a
    #: role call restarted after a pause (B5) does not get a fresh 15. Every invocation counts,
    #: denied ones included, so a model cannot probe past the cap.
    max_tool_calls: NonNegInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.caller is None) != (self.role is not Role.EXPLORER):
            raise ValueError("caller is set exactly for Explorer calls")
        if self.caller is not None and self.caller not in EXPLORE_CALLER_ROLES:
            raise ValueError(f"{self.caller} has no `explore` tool (§8.2, §8.5)")
        if (self.parent_call_id is None) != (self.caller is None):
            raise ValueError("parent_call_id is set exactly for Explorer calls")
        if self.parent_call_id == self.role_call_id:
            raise ValueError("a role call cannot be its own parent")
        if (self.step_id is None) != (self.step_type is None):
            raise ValueError("step_id and step_type are set together")
        if self.step_id is None and (self.attempt_id is not None or self.step_target_files):
            raise ValueError("attempt_id and step_target_files need a step")
        return self


InT_contra = TypeVar("InT_contra", bound=BaseModel, contravariant=True)
OutT_co = TypeVar("OutT_co", bound=BaseModel, covariant=True)


@runtime_checkable
class Tool(Protocol[InT_contra, OutT_co]):
    """One registry tool. Constructed per run with exactly the dependencies it may touch
    (a `Workspace` for host-side file tools, a `Sandbox` for exec tools, ...), so a tool has
    no way to reach anything else.

    `run` receives arguments already validated against `spec.input_model` and already
    allowed by every pre-policy. Expected failures (non-unique `old_str`, a failing command)
    are part of the output model. An unexpected exception is a bug: the ToolHost records it
    and re-raises, and the run ends in END_ERROR.
    """

    @property
    def spec(self) -> ToolSpec: ...

    async def run(self, args: InT_contra, ctx: ToolContext) -> OutT_co: ...


class HostDenyCode(StrEnum):
    """Denials the ToolHost itself issues, before any `ToolPolicy` runs."""

    UNKNOWN_TOOL = "unknown_tool"
    ROLE_NOT_ALLOWED = "role_not_allowed"
    INVALID_ARGS = "invalid_args"
    TOOL_CALL_CAP = "tool_call_cap"


class ToolInvocation(BaseModel):
    """What one `ToolHost.invoke` produced. Mirrors the persisted §12 `ToolCall` row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: ToolCallId
    tool: str  # as the model asked for it, bare name if it was one of ours
    decision: PolicyDecision
    output: dict[str, Any] | None = None  # post-policy (redacted) output, JSON-ready
    deny_code: str | None = None
    deny_reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.decision is PolicyDecision.ALLOW:
            if self.output is None or self.deny_code is not None or self.deny_reason is not None:
                raise ValueError("an allowed invocation has an output and no denial")
        elif self.output is not None or not self.deny_code or not self.deny_reason:
            raise ValueError("a denied invocation has a code and reason and no output")
        return self

    def for_model(self) -> dict[str, Any]:
        """The tool result the model sees. A denial is a normal result, not an error the
        runtime has to interpret, so the model can adapt (and the router can count it)."""
        if self.output is not None:
            return self.output
        return {"denied": True, "code": self.deny_code, "reason": self.deny_reason}


@runtime_checkable
class ToolHost(Protocol):
    """The only path from a model to a tool (§9.1, §11.1).

    `invoke` does, in this order, and every step that refuses returns a denied
    `ToolInvocation` (never raises) and still writes a ToolCall row:
    1. count the call against `ctx.max_tool_calls` for `ctx.role_call_id`; over the cap ->
       `TOOL_CALL_CAP` (the 16th Editor call is refused)
    2. resolve the name with `bare_name`; anything that is not `mcp__mpilot__<registry
       tool>` (a built-in such as `Bash`, another server's tool, a typo) -> `UNKNOWN_TOOL`
    3. the role's allowlist (§11.1, P4-11) -> `ROLE_NOT_ALLOWED`
    4. validate `raw_args` against `spec.input_model` -> `INVALID_ARGS`
    5. every `ToolPolicy.pre` in order; the first deny wins
    6. `Tool.run`
    7. every `ToolPolicy.post` in order (A3 redaction runs last)
    8. persist the ToolCall row and checkpoint (NFR-4)

    The budget guard is not checked here: the runtime checks it before forwarding each
    tool call (`BudgetGuard.before_tool_call`), because only the runtime can end the call.
    """

    @property
    def registry(self) -> Mapping[str, ToolSpec]:
        """Every tool this host can run, by name. Its key set equals `REGISTRY_TOOL_NAMES`
        once all tools exist (P4-07 asserts it); a subset before that."""
        ...

    def specs_for(self, role: Role) -> Sequence[ToolSpec]:
        """The tools `role` may call: what the runtime exposes. Sorted by name."""
        ...

    async def invoke(
        self, name: str, raw_args: Mapping[str, Any], ctx: ToolContext
    ) -> ToolInvocation:
        """Run one tool call as described on the class. `name` is exactly what the model
        asked for (`mcp__mpilot__view_file`), so a denied foreign name is recorded too."""
        ...
