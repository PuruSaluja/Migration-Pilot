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
from mpilot.models.coordination import Lease, Renewal, RunControl
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
        if v is None or _FULL_SHA.fullmatch(v):
            return v
        if not _SAFE_REF.fullmatch(v) or ".." in v or "//" in v or "@{" in v or v.endswith(".lock"):
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
    latest checkpoint.

    Coordination (P1-12c, `models/coordination.py`): whatever drives a run first takes its
    lease (`RunStore.acquire_lease`; `LeaseHeldError` = another live process is driving it)
    and keeps that one lease for as long as it drives. It renews it at least once every
    ttl/3 from a heartbeat that runs also during long tool calls and while waiting for
    approval; every `Renewal` carries the run's `RunControl`, and a recorded cancel seen
    there (or at a checkpoint, `CancelRequestedError`) means: stop the running tool call,
    tear the sandbox down, checkpoint END_CANCELLED. It checks `RunStore.control` right
    after taking the lease, and releases it on PAUSED_LIMIT, on leaving a wait in
    AWAITING_APPROVAL, or on any other stop short of a terminal state (a terminal
    checkpoint releases it itself). Every write carries the lease, so a driver that lost it
    can write nothing. The CLI, the REST API and the UI are all such processes."""

    async def start(self, req: RunRequest) -> RunId:
        """Validate and record a new run in INIT. Input errors raise InputError (exit 2)
        before any run exists."""
        ...

    async def drive(self, run_id: RunId) -> RunStatus:
        """Take the lease and advance until a terminal state, PAUSED_LIMIT or (without a
        waiting `--approve-plan` caller) AWAITING_APPROVAL, and return it. A
        `CancelRequestedError` from any checkpoint, or a cancel in a `Renewal` from the
        heartbeat, means: stop the running tool call, tear the sandbox down and checkpoint
        END_CANCELLED. The CLI maps the result with `exit_code_for` (C6)."""
        ...

    async def resume(self, run_id: RunId) -> RunStatus:
        """Continue a paused or crashed run from its latest checkpoint (B5): first
        `SandboxDriver.reap(run_id)` so no old container still mounts the workspace, then
        restore it (from the export after a pause, checked against the checkpoint's
        `export_digest`; from the latest host snapshot after a crash), never repeat a
        completed role call, restart an interrupted one, keep every sticky flag and counter.
        Takes the lease first, so two resumes can never both drive a run; a recorded cancel
        ends the run END_CANCELLED instead of continuing. `resume_point(...).checkpoints`
        are written under the lease. Then `drive`."""
        ...

    async def decide_plan(self, run_id: RunId, *, approve: bool, reason: str | None) -> None:
        """`mpilot approve` (CP-1). Records the decision (`RunStore.request_plan_decision`:
        only in AWAITING_APPROVAL, only once). A live driver waiting in AWAITING_APPROVAL
        reads it from its heartbeat's `Renewal.control` and applies it; with no live driver
        this process takes the lease and applies it itself: approve -> continue driving,
        reject -> END_REJECTED. The store refuses leaving AWAITING_APPROVAL without the
        matching recorded decision."""
        ...

    async def cancel(self, run_id: RunId) -> None:
        """Stop the run and tear down its sandbox -> END_CANCELLED (C6). Idempotent; a no-op
        for a run that already ended. Records the request first (`request_cancel`, durable),
        so it is never lost: a live driver meets it at its next heartbeat or checkpoint, a
        later resume before doing anything. If no live driver holds the lease, this process
        takes it, reaps the run's containers and checkpoints END_CANCELLED itself; if one
        does, the CLI waits until the run ends or the lease expires (then takes over), and
        the REST API returns 202 at once. A cancel recorded before the final checkpoint wins,
        even after the PR was opened: the run ends END_CANCELLED and the PR stays a draft."""
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


class AttemptNumbers(_Frozen):
    """Both numbers of a new attempt, from one counter update (`Attempt.n`, `n_in_step`)."""

    n: PositiveInt  # run-wide; the n of `refs/mpilot/attempt/<n>`
    n_in_step: PositiveInt

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.n_in_step > self.n:
            raise ValueError("an attempt's number in its step cannot exceed its run-wide number")
        return self


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
        """Reconcile the guard's in-call charges with the runtime's reported usage: `spent`
        gains the reported usage, and the in-call charges (a bound used for stopping, not a
        measure) are dropped. Never below what the runtime itself reported turn by turn: if
        the final `usage` is lower than the per-turn reports it sums, the sum counts (H7).
        Also for an interrupted call: its tokens still count (B5)."""
        ...

    def start_attempt(self, step_id: StepId) -> AttemptNumbers:
        """Count one repair attempt and return its run-wide number (for `attempt_ref`) and its
        number within the step, so callers never derive them separately. Raises
        AttemptCapError at 6 per step or 40 per run (NFR-3) -> ESCALATED."""
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

    def save_run(self, run: Run, *, lease: Lease) -> None:
        """Update an existing run (env, base_sha, branch, ...); `KeyError` if it was never
        created. Status, paused state, spent and terminal reason are the checkpoint's
        (`checkpoint` updates them): changing them here is a `StoreError`."""
        ...

    def get_run(self, run_id: RunId) -> Run:
        """`KeyError` for an unknown run."""
        ...

    def list_runs(self) -> Sequence[Run]:
        """Newest first (`GET /runs`, the UI runs table)."""
        ...

    # Coordination (P1-12c). Every method that writes a run's state takes the driver `lease`
    # and raises `LeaseLostError` unless it is the run's current, unreleased lease; the check
    # is in the write's own transaction.

    def acquire_lease(self, run_id: RunId, holder: str, *, ttl_s: float) -> Lease:
        """Take the run's driver lease, with a higher fencing token than any before it.
        Raises `LeaseHeldError` while any lease is live (whatever its holder: holder names are
        labels, so extending your own lease is `renew_lease`), `RunEndedError` for a run in a
        terminal state, `KeyError` for an unknown run. Atomic: of two racing processes,
        exactly one gets it."""
        ...

    def renew_lease(self, lease: Lease, *, ttl_s: float) -> Renewal:
        """Extend a lease that is still current (`LeaseLostError` if not), and return the
        run's requests with it: the driver's heartbeat is how a cancel reaches a driver that
        is not checkpointing."""
        ...

    def release_lease(self, lease: Lease) -> None:
        """Give the run up (paused, waiting for approval, interrupted). A no-op for a lease
        that is no longer current. A terminal checkpoint releases the lease by itself."""
        ...

    def current_lease(self, run_id: RunId) -> Lease | None:
        """Who drives the run, for `mpilot status` (check `Lease.live`)."""
        ...

    def control(self, run_id: RunId) -> RunControl:
        """The requests recorded for the run; `KeyError` for an unknown run."""
        ...

    def request_cancel(self, run_id: RunId, *, reason: str | None = None) -> RunControl:
        """Record a cancel request (no lease needed; durable once this returns). Idempotent:
        the first request is kept; a no-op for a run that already ended. From then on the
        store refuses every checkpoint but END_CANCELLED with `CancelRequestedError`."""
        ...

    def request_plan_decision(
        self, run_id: RunId, *, approve: bool, reason: str | None = None
    ) -> RunControl:
        """Record the CP-1 decision (no lease needed). `StoreError` unless the run is in
        AWAITING_APPROVAL (or paused there) and no decision was recorded yet."""
        ...

    def append(self, row: AppendRow, *, lease: Lease) -> None:
        """Append one row; rows are never updated or deleted. A row for a run that does not
        exist, or one the schema's constraints refuse (for example an `LLMCall` whose routing
        decision was not stored first), is a `StoreError`."""
        ...

    def put(self, row: SingleRow, *, lease: Lease) -> None:
        """Insert or replace the run's single record of this type (plan approval, PR). A
        `Plan` whose `approved` disagrees with the recorded decision is a `StoreError`. A
        `Baseline` is written once per run and never replaced (P0 and the flaky set are
        fixed for the run, §11.2): a second one is a `StoreError`."""
        ...

    def rows(self, run_id: RunId, kind: type[RowT]) -> Sequence[RowT]:
        """Every appended row of one type for a run, in insertion order, or the single
        record as a one-item sequence."""
        ...

    def append_final_verification(
        self, run_id: RunId, result: FinalVerification, *, lease: Lease
    ) -> None:
        """Record a final verification (§9.5). A `FinalVerification` carries no run id, so the
        caller gives it. It can recur (a failed integrity gate sends the run back once), so
        every one is kept; the current one is the last."""
        ...

    def final_verifications(self, run_id: RunId) -> Sequence[FinalVerification]:
        """Every final verification of the run, oldest first."""
        ...

    def checkpoint(self, cp: Checkpoint, *, lease: Lease, rows: Sequence[AppendRow] = ()) -> None:
        """Write a checkpoint (after every tool call and state change, NFR-4), with `rows`
        appended in the same transaction (a role call's row with the checkpoint that folds it
        in). In one atomic step, in this order:
        - the lease must be current (`LeaseLostError`)
        - a recorded cancel refuses anything but END_CANCELLED (`CancelRequestedError`;
          nothing is written, rows included)
        - `Checkpoint.covers` against the previous one, and `transition_problems` against the
          recorded requests, on effective states (a paused run's `paused_at_state`):
          END_CANCELLED needs a cancel request, END_REJECTED a reject, and leaving
          AWAITING_APPROVAL for anything but waiting, END_ERROR or END_BUDGET_EXCEEDED an
          approval -> `StoreError`
        - write the rows and the checkpoint, and update the run's status, paused state, spent
          and terminal reason from it (the checkpoint is authoritative)
        - a terminal checkpoint also releases the lease: nothing can be written after it."""
        ...

    def latest_checkpoint(self, run_id: RunId) -> Checkpoint | None: ...

    def add_eval(self, result: EvalResult) -> None:
        """Benchmark result rows (FR-20). DRAFT until P9-01: `EvalResult` has no `run_id` or
        `bench_id` yet, so a row can't be traced to its run or batch, and how a resumed
        batch replaces a row is open. P9-01 adds both (a new migration) and may change this
        method and `evals`."""
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
    """Run events for `mpilot logs --follow`, SSE and the UI (P3-05, P8-02).

    Durable first: `publish` writes the record to its own table (`run_events`, added with the
    bus in P3-05 as a new migration) and only then hands it to subscribers, so a replay with
    `after_seq` never misses an event a live subscriber saw, across processes and restarts.
    Events are a view: run state is the checkpoint, never the event stream."""

    def publish(
        self, run_id: RunId, kind: EventKind, data: dict[str, Any], *, lease: Lease
    ) -> RunEventRecord:
        """Persist, then fan out one event; the bus assigns `seq` and `at`. Fenced like every
        write of run state: only the process driving the run publishes, so a stalled driver
        that was taken over cannot add events (`LeaseLostError`; the store-backed bus checks
        the lease in the insert's transaction, P3-05)."""
        ...

    def subscribe(self, run_id: RunId, *, after_seq: int = 0) -> AsyncIterator[RunEventRecord]:
        """Every event with `seq > after_seq`, in order, then live ones until the run ends.
        `after_seq` is how an SSE client reconnects (Last-Event-ID, P8-02)."""
        ...
