"""Orchestrator interfaces: state machine, budgets, store and events (SPEC §7.4, §10.1,
§13.1, §13.2, NFR-1..4, FR-21; B2, B5, C1, C4, C6, C7).

The orchestrator owns all control flow; models only propose (§7.1). Implementations: the
pure transition table (P3-04, tests first), the driver with checkpoints and events (P3-05),
the BudgetManager (P4-16), the RunStore on SQLite WAL (P1-09, P1-10), the event bus behind
`mpilot logs --follow` and SSE (P3-05, P8-02).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from enum import StrEnum
from typing import Any, Protocol, Self, TypeVar, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from mpilot.agents.runtime import BudgetGuard
from mpilot.core.types import LLMCallId, RunId, RunStatus, StepId, Usage
from mpilot.models.artifacts import EscalationReport, FinalVerification, MigrationNote
from mpilot.models.domain import (
    Attempt,
    Baseline,
    Budgets,
    CriticVerdict,
    EvalResult,
    IntegrityReport,
    LLMCall,
    NonNegInt,
    Plan,
    PositiveInt,
    PullRequest,
    RoutingDecision,
    Run,
    TestResult,
    ToolCall,
)
from mpilot.models.state import BudgetState, Checkpoint


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_FULL_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
#: Starts with a letter or digit (never `-`), no control characters, spaces or
#: `~ ^ : ? * [ \`, no trailing `/` or `.`.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}(?<![./])$")


# ---------------------------------------------------------------- requests and the state machine


class RunRequest(_Frozen):
    """One `mpilot run` (§13.1) or `POST /runs` (§13.2), after config and CLI are merged."""

    repo: str = Field(min_length=1)  # a GitHub URL or a local path (FR-2)
    goal: str | None = None  # free text (FR-1, C3)
    package: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    replace: tuple[str, str] | None = None  # --replace X=Y
    ref: str | None = None  # branch, tag or full SHA to clone at (FR-2); None = default branch
    test_cmd: str | None = None  # --test-cmd override (FR-3: always wins)
    bot_pr_url: str | None = None  # FR-19: fix a red Renovate/Dependabot PR
    approve_plan: bool = False  # CP-1
    open_pr: bool = True  # False = --no-pr
    pr_on_failure: bool = False  # FR-17
    system: str = "full"  # full, b0, b1, b2, b3 or an ablation name (§14.6)
    budgets: Budgets

    @field_validator("ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        """It reaches host git at intake, so it must never read as an option: a full hex SHA,
        or a name `git check-ref-format` accepts. P3-03 still passes it after `--`."""
        if v is None or _FULL_SHA.match(v):
            return v
        if not _SAFE_REF.match(v) or ".." in v or "//" in v or "@{" in v or v.endswith(".lock"):
            raise ValueError(f"not a safe git ref: {v!r}")
        return v

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not (self.goal or self.package or self.bot_pr_url):
            raise ValueError("a run needs a goal, a --package or a bot PR URL")
        if self.pr_on_failure and not self.open_pr:
            raise ValueError("--pr-on-failure contradicts --no-pr")
        return self


class RunEvent(StrEnum):
    """What happened, as the driver tells the pure §7.4 table. The table (P3-04) maps
    `(state, event)` to the next state; any pair without an edge is IllegalTransition."""

    CLONED = "cloned"
    ENV_DETECTED = "env_detected"
    BASELINE_GREEN = "baseline_green"
    BASELINE_RED = "baseline_red"  # red, or more than 5% flaky (FR-4)
    ANALYZED = "analyzed"
    PLANNED = "planned"
    APPROVAL_REQUESTED = "approval_requested"  # --approve-plan (CP-1)
    PLAN_APPROVED = "plan_approved"
    PLAN_REJECTED = "plan_rejected"
    BUMPED = "bumped"
    CODEMODS_APPLIED = "codemods_applied"
    REPAIR_STARTED = "repair_started"
    VERIFY_GREEN = "verify_green"  # repair loop -> INTEGRITY_CHECK
    INTEGRITY_PASSED = "integrity_passed"
    INTEGRITY_FAILED = "integrity_failed"  # first failure only: back to REPAIR_LOOP
    CRITIC_APPROVED = "critic_approved"
    CRITIC_REVISE = "critic_revise"  # at most once (FR-15)
    PR_OPENED = "pr_opened"
    FINISHED = "finished"  # success without a PR (--no-pr), or after PR_OPENED
    ESCALATE = "escalate"  # unresolvable bump, second gate failure, C7, attempt caps, ...
    REPORT_WRITTEN = "report_written"  # ESCALATED -> END_ESCALATED (FR-17)
    USAGE_LIMIT = "usage_limit"  # any -> PAUSED_LIMIT (FR-21)
    RESUMED = "resumed"  # PAUSED_LIMIT -> the state it paused in
    BUDGET_EXCEEDED = "budget_exceeded"  # turn, token or time cap (exit 4)
    FAILED = "failed"  # END_ERROR
    CANCELLED = "cancelled"  # END_CANCELLED (C6)


@runtime_checkable
class TransitionTable(Protocol):
    """The pure §7.4 state machine."""

    def next_state(
        self, state: RunStatus, event: RunEvent, *, paused_at: RunStatus | None = None
    ) -> RunStatus:
        """The state after `event`. `paused_at` is given exactly for RESUMED out of
        PAUSED_LIMIT, which returns there. Raises IllegalTransition for any pair without an
        edge, for every event out of a terminal state, and for `paused_at` on any other
        event.

        The table is pure over (state, event). Counted rules are the driver's, which picks
        the event: a first INTEGRITY_CHECK failure sends INTEGRITY_FAILED, a second ESCALATE
        (`Checkpoint.integrity_failures`); a second Critic revise sends ESCALATE
        (`critic_revisions`, FR-15); `--no-pr` sends FINISHED from REVIEW. P3-04 decides,
        tests first, from which states ESCALATE is legal (e.g. escalate-immediately triggers
        at ENV_DETECTED or during the baseline)."""
        ...

    def events_from(self, state: RunStatus) -> frozenset[RunEvent]:
        """Every event with an edge out of `state` (empty for terminal states)."""
        ...


@runtime_checkable
class Orchestrator(Protocol):
    """Drives runs (§7.4). Every method is safe to call after a crash: it starts from the
    latest checkpoint."""

    async def start(self, req: RunRequest) -> RunId:
        """Validate and record a new run in INIT. Input errors raise InputError (exit 2)
        before any run exists."""
        ...

    async def drive(self, run_id: RunId) -> RunStatus:
        """Advance until a terminal state, PAUSED_LIMIT or AWAITING_APPROVAL, and return it.
        The CLI maps it with `exit_code_for` (C6)."""
        ...

    async def resume(self, run_id: RunId) -> RunStatus:
        """Continue a paused or crashed run from its latest checkpoint (B5): first
        `SandboxDriver.reap(run_id)` so no old container still mounts the workspace, then
        restore it (from the export after a pause, checked against the checkpoint's
        `export_digest`; from the latest host snapshot after a crash), never repeat a
        completed role call, restart an interrupted one, keep every sticky flag and counter.
        Then `drive`."""
        ...

    async def decide_plan(self, run_id: RunId, *, approve: bool, reason: str | None) -> None:
        """`mpilot approve` (CP-1). Only valid in AWAITING_APPROVAL; reject -> END_REJECTED."""
        ...

    async def cancel(self, run_id: RunId) -> None:
        """Stop the run and tear down its sandbox -> END_CANCELLED (C6). Idempotent."""
        ...


# ---------------------------------------------------------------- budgets


class CallEstimate(_Frozen):
    """The B2 pessimistic pre-call estimate for one role call.

    Each agent turn re-sends its context, so turn k (1-based) costs
    `prompt + (k - 1) * tool_output_per_turn + output_ceiling` cap tokens.
    """

    prompt_tokens: NonNegInt  # ceil(chars / 3) of the built prompt (B3)
    #: Per-turn context growth: the capped tool output plus the turn's own re-sent output
    #: (text and tool_use), so the estimate stays pessimistic (B2).
    tool_output_per_turn: NonNegInt
    output_ceiling: PositiveInt  # max output tokens per turn, thinking included
    requested_turns: PositiveInt

    def tokens_for(self, turns: int) -> int:
        """Sum of the per-turn costs over `turns` turns."""
        if turns < 0:
            raise ValueError("turns must be >= 0")
        per_turn = self.prompt_tokens + self.output_ceiling
        return turns * per_turn + self.tool_output_per_turn * turns * (turns - 1) // 2

    def max_turns_within(self, token_room: int, turn_room: int) -> int:
        """The largest n <= requested_turns with n <= turn_room and tokens_for(n) <=
        token_room. 0 means not even one turn fits: the call is refused (§7.4)."""
        n = 0
        limit = min(self.requested_turns, max(turn_room, 0))
        while n < limit and self.tokens_for(n + 1) <= token_room:
            n += 1
        return n


@runtime_checkable
class BudgetManager(Protocol):
    """NFR-1..3, H7, B2. The only place budget arithmetic happens. Never downgrades a model
    or effort to save budget (§8.4): it can only refuse, stop or end."""

    def state(self) -> BudgetState:
        """For the checkpoint; a manager rebuilt from it continues exactly (B5). `spent`
        holds reconciled calls only; the running call's guard charges are in `in_flight_*`."""
        ...

    def precheck(self, call_id: LLMCallId, est: CallEstimate) -> int:
        """The clamped `max_turns` for this call (`est.max_turns_within` the remaining token
        and turn room), bound to `call_id` with its estimate. Raises BudgetExceededError if
        not even one turn fits. Calls nest (an `explore` runs inside an Editor call): the
        remaining room already counts the outer call's in-call charges, and the two calls'
        estimates and charges are kept apart by id."""
        ...

    def guard(self, call_id: LLMCallId) -> BudgetGuard:
        """The in-call guard for a call that passed `precheck`; it charges that call's
        estimate per turn when the runtime reports no usage (B2 mid-call stop). Raises
        KeyError for an id that was never prechecked."""
        ...

    def record_call(
        self, call_id: LLMCallId, usage: Usage, turns: int, api_equiv_usd: float
    ) -> None:
        """Reconcile the guard's in-call charges with the runtime's reported usage. Also for
        an interrupted call: its tokens still count (B5)."""
        ...

    def start_attempt(self, step_id: StepId) -> int:
        """Count one repair attempt and return its run-wide number (for `attempt_ref`).
        Raises AttemptCapError at 6 per step or 40 per run (NFR-3) -> ESCALATED."""
        ...

    def record_sandbox_seconds(self, seconds: float) -> None: ...

    def pause(self) -> None:
        """Entering PAUSED_LIMIT: stop counting wall-clock time (NFR-2)."""
        ...

    def resume(self) -> None: ...

    def check_time(self) -> None:
        """Raise BudgetExceededError if active wall-clock time passed the cap."""
        ...


# ---------------------------------------------------------------- store and events

#: Rows a run accumulates (many per run).
AppendRow = (
    ToolCall
    | LLMCall
    | RoutingDecision
    | TestResult
    | Attempt
    | IntegrityReport
    | CriticVerdict
    | MigrationNote
)
#: Records a run has at most one of (upserted).
SingleRow = Baseline | Plan | PullRequest | EscalationReport
RowT = TypeVar("RowT", bound=BaseModel)


@runtime_checkable
class RunStore(Protocol):
    """Authoritative run state (§10.1), SQLite in WAL mode (NFR-4), Postgres-compatible."""

    def create_run(self, run: Run) -> None:
        """Insert a new run; an existing id is a `StoreError`."""
        ...

    def save_run(self, run: Run) -> None:
        """Update an existing run (status, env, spent, terminal reason, ...); `KeyError` if it
        was never created."""
        ...

    def get_run(self, run_id: RunId) -> Run:
        """`KeyError` for an unknown run."""
        ...

    def list_runs(self) -> Sequence[Run]:
        """Newest first (`GET /runs`, the UI runs table)."""
        ...

    def append(self, row: AppendRow) -> None:
        """Append one row; rows are never updated or deleted. A row for a run that does not
        exist, or one the schema's constraints refuse (for example an `LLMCall` whose routing
        decision was not stored first), is a `StoreError`."""
        ...

    def put(self, row: SingleRow) -> None:
        """Insert or replace the run's single record of this type (plan approval, PR)."""
        ...

    def rows(self, run_id: RunId, kind: type[RowT]) -> Sequence[RowT]:
        """Every appended row of one type for a run, in insertion order, or the single
        record as a one-item sequence."""
        ...

    def append_final_verification(self, run_id: RunId, result: FinalVerification) -> None:
        """Record a final verification (§9.5). A `FinalVerification` carries no run id, so the
        caller gives it. It can recur (a failed integrity gate sends the run back once), so
        every one is kept; the current one is the last."""
        ...

    def final_verifications(self, run_id: RunId) -> Sequence[FinalVerification]:
        """Every final verification of the run, oldest first."""
        ...

    def checkpoint(self, cp: Checkpoint) -> None:
        """Write a checkpoint (after every tool call, NFR-4). Refuses one that would undo
        anything the previous one recorded (`Checkpoint.covers`), with `StoreError`. The check
        and the write are one atomic step, so two writers cannot both pass against the same
        predecessor."""
        ...

    def latest_checkpoint(self, run_id: RunId) -> Checkpoint | None: ...

    def add_eval(self, result: EvalResult) -> None:
        """Benchmark result rows (FR-20)."""
        ...

    def evals(self, suite: str) -> Sequence[EvalResult]: ...


class EventKind(StrEnum):
    STATE_CHANGED = "state_changed"
    ROUTING_DECISION = "routing_decision"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    VERIFY = "verify"
    CHECKPOINT = "checkpoint"
    APPROVAL_REQUESTED = "approval_requested"
    LOG = "log"


class RunEventRecord(_Frozen):
    """One streamed event (`mpilot logs --follow`, SSE). Data is redacted before publish."""

    run_id: RunId
    seq: PositiveInt  # per run, gapless, assigned by the bus
    at: AwareDatetime
    kind: EventKind
    data: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class EventBus(Protocol):
    def publish(self, run_id: RunId, kind: EventKind, data: dict[str, Any]) -> RunEventRecord:
        """Record and fan out one event; the bus assigns `seq` and `at`."""
        ...

    def subscribe(self, run_id: RunId, *, after_seq: int = 0) -> AsyncIterator[RunEventRecord]:
        """Every event with `seq > after_seq`, in order, then live ones until the run ends.
        `after_seq` is how an SSE client reconnects (Last-Event-ID, P8-02)."""
        ...
