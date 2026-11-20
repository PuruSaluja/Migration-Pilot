"""Core enums, IDs, exit codes and token usage (SPEC §7.4, §13.1, §14.5, NFR-1).

These are the vocabulary every other layer shares, so the enums are closed on purpose:
`Tier` and `Effort` have exactly two members each (NFR-12), so Haiku or any other effort
cannot even be represented.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import NewType

# ---------------------------------------------------------------- IDs

RunId = NewType("RunId", str)
StepId = NewType("StepId", str)
PlanId = NewType("PlanId", str)
AttemptId = NewType("AttemptId", str)
ToolCallId = NewType("ToolCallId", str)
LLMCallId = NewType("LLMCallId", str)
RoutingDecisionId = NewType("RoutingDecisionId", str)
BenchId = NewType("BenchId", str)

_ID_HEX_BYTES = 6  # 12 hex chars: short enough for branch names (`mpilot/<slug>-<id>`)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(_ID_HEX_BYTES)}"


def new_run_id() -> RunId:
    return RunId(_new_id("run"))


def new_step_id() -> StepId:
    return StepId(_new_id("step"))


def new_plan_id() -> PlanId:
    return PlanId(_new_id("plan"))


def new_attempt_id() -> AttemptId:
    return AttemptId(_new_id("att"))


def new_tool_call_id() -> ToolCallId:
    return ToolCallId(_new_id("tc"))


def new_llm_call_id() -> LLMCallId:
    return LLMCallId(_new_id("llm"))


def new_routing_decision_id() -> RoutingDecisionId:
    return RoutingDecisionId(_new_id("rd"))


def new_bench_id() -> BenchId:
    return BenchId(_new_id("bench"))


# ---------------------------------------------------------------- model routing


class Tier(StrEnum):
    """Model tier (§8.4). No other members, ever: Haiku is never a tier (NFR-12)."""

    OPUS = "opus"
    SONNET = "sonnet"


class Effort(StrEnum):
    """Reasoning effort (§8.4). No other members, ever (NFR-12)."""

    HIGH = "high"
    MEDIUM = "medium"


class RoutingPolicy(StrEnum):
    """Model policy, config `routing.policy` (§8.4 Policies)."""

    ROUTED = "routed"
    ALL_OPUS = "all_opus"
    ALL_SONNET = "all_sonnet"


class EffortPolicy(StrEnum):
    """Effort policy, config `routing.effort_policy` (§8.4 Policies)."""

    ROUTED = "routed"
    ALL_HIGH = "all_high"


class Role(StrEnum):
    """Agent roles (§8.2) plus the two benchmark baselines that run as one role (§14.6)."""

    ANALYZER = "analyzer"
    PLANNER = "planner"
    EDITOR = "editor"
    DIAGNOSER = "diagnoser"
    CRITIC = "critic"
    EXPLORER = "explorer"
    PR_WRITER = "pr_writer"
    B2_SINGLE_SHOT = "b2_single_shot"
    B3_GENERIC = "b3_generic"


class StepType(StrEnum):
    """Plan step types (FR-7)."""

    BUMP = "bump"
    CODEMOD = "codemod"
    EDIT_CLUSTER = "edit_cluster"
    CONFIG = "config"
    TEST_TOUCH = "test_touch"
    VERIFY = "verify"


class Outcome(StrEnum):
    """Attempt outcome (§7.4 attempt scoring, C1)."""

    IMPROVED = "improved"
    SAME = "same"
    REGRESSED = "regressed"
    GREEN = "green"


class StopReason(StrEnum):
    """Why a role call ended normally (§12 `LLMCall.stop`). A usage or rate limit is not a
    stop reason: it raises `UsageLimitError` and the row records `limit_error` (FR-21).
    Cancellation is `asyncio.CancelledError`."""

    COMPLETED = "completed"  # final answer produced and schema-valid
    SCHEMA_INVALID = "schema_invalid"  # final answer failed `output_schema` (router trigger 3)
    MAX_TURNS = "max_turns"  # hit req.max_turns without a final answer
    BUDGET_STOP = "budget_stop"  # the BudgetGuard ended the call (B2 mid-call stop)
    TOOL_CALL_CAP = "tool_call_cap"  # a tool call after the cap refusal (§8.3, §9.1)


class NetworkMode(StrEnum):
    """Sandbox container network mode (§9.3)."""

    NONE = "none"
    REGISTRY = "registry"


# ---------------------------------------------------------------- run state


class RunStatus(StrEnum):
    """One member per §7.4 state, plus the terminal `END_*` states (C6 adds END_CANCELLED)."""

    INIT = "INIT"
    CLONED = "CLONED"
    ENV_DETECTED = "ENV_DETECTED"
    BASELINE_OK = "BASELINE_OK"
    ANALYZED = "ANALYZED"
    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    BUMPED = "BUMPED"
    CODEMODS_APPLIED = "CODEMODS_APPLIED"
    REPAIR_LOOP = "REPAIR_LOOP"
    INTEGRITY_CHECK = "INTEGRITY_CHECK"
    REVIEW = "REVIEW"
    PR_OPENED = "PR_OPENED"
    ESCALATED = "ESCALATED"
    PAUSED_LIMIT = "PAUSED_LIMIT"
    END_SUCCESS = "END_SUCCESS"
    END_BASELINE_RED = "END_BASELINE_RED"
    END_REJECTED = "END_REJECTED"
    END_BUDGET_EXCEEDED = "END_BUDGET_EXCEEDED"
    END_ERROR = "END_ERROR"
    END_ESCALATED = "END_ESCALATED"
    END_CANCELLED = "END_CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self.value.startswith("END_")


class FailureCategory(StrEnum):
    """Failure taxonomy (§14.5), shared by the Diagnoser and the eval.

    §14.5 writes two entries with a slash (`BASELINE_RED/FLAKY`, `NO_PROGRESS/OSCILLATION`).
    Each is ONE category, named by its first token, which is also how §8.4 refers to it
    (`NO_PROGRESS`).
    """

    ENV_INSTALL = "ENV_INSTALL"
    ENV_TOOLCHAIN = "ENV_TOOLCHAIN"
    ENV_NETWORK = "ENV_NETWORK"
    BASELINE_RED = "BASELINE_RED"
    COMPILE_TYPE = "COMPILE_TYPE"
    API_REMOVED = "API_REMOVED"
    API_BEHAVIOR_CHANGED = "API_BEHAVIOR_CHANGED"
    TEST_INFRA = "TEST_INFRA"
    CONFIG_FORMAT = "CONFIG_FORMAT"
    TRANSITIVE_INCOMPAT = "TRANSITIVE_INCOMPAT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_PROGRESS = "NO_PROGRESS"
    INTEGRITY_BLOCKED = "INTEGRITY_BLOCKED"
    CRITIC_REJECTED = "CRITIC_REJECTED"
    USAGE_LIMIT = "USAGE_LIMIT"
    AGENT_ERROR = "AGENT_ERROR"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------- exit codes (C6, §13.1)


class ExitCode(IntEnum):
    """`mpilot` process exit codes (§13.1)."""

    SUCCESS = 0
    FAILED = 1  # escalated, error, rejected or cancelled
    INPUT_ERROR = 2  # no run state: raised before a run exists
    BASELINE_RED = 3
    BUDGET_EXCEEDED = 4
    PAUSED = 5


#: Exit code returned when `mpilot run --approve-plan` is interrupted in AWAITING_APPROVAL
#: (§13.1). It belongs to the CLI, not to any run state, so it is not an `ExitCode` member.
EXIT_INTERRUPTED = 130

_EXIT_CODES: dict[RunStatus, ExitCode] = {
    RunStatus.END_SUCCESS: ExitCode.SUCCESS,
    RunStatus.END_ESCALATED: ExitCode.FAILED,
    RunStatus.END_ERROR: ExitCode.FAILED,
    RunStatus.END_REJECTED: ExitCode.FAILED,
    RunStatus.END_CANCELLED: ExitCode.FAILED,
    RunStatus.END_BASELINE_RED: ExitCode.BASELINE_RED,
    RunStatus.END_BUDGET_EXCEEDED: ExitCode.BUDGET_EXCEEDED,
    RunStatus.PAUSED_LIMIT: ExitCode.PAUSED,
}


def exit_code_for(status: RunStatus) -> ExitCode:
    """Exit code for a run that stopped in `status` (C6 matrix).

    Defined for every terminal state and for PAUSED_LIMIT. Any other state means the run
    has not stopped, so asking for its exit code is a programming error.
    """
    try:
        return _EXIT_CODES[status]
    except KeyError:
        raise ValueError(f"{status} is neither terminal nor PAUSED_LIMIT: no exit code") from None


# ---------------------------------------------------------------- token usage (B2)


@dataclass(frozen=True)
class Usage:
    """Token usage of one or more model turns, as reported by the runtime."""

    input_tokens: int = 0
    output_tokens: int = 0  # thinking included
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")

    def cap_tokens(self) -> int:
        """Tokens counted against the run cap (NFR-1, B2).

        `input + output + cache_write + ceil(0.1 * cache_read)`, in integer arithmetic.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_write_tokens
            + -(-self.cache_read_tokens // 10)
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )
