"""Run state that survives a crash or a usage-limit pause (SPEC §10.1, NFR-4, FR-21, B5).

A `Checkpoint` is written after every tool call. A resume rebuilds the orchestrator from the
latest one, so everything that must never go backwards lives here: routing sticky state and
routing counters (no de-escalation), attempt counters (no fresh attempts), failure history
(no retrying what was rejected), the budget spent (an interrupted call's tokens still count),
the role calls already finished in the current unit of work (never re-run), and how far the
in-flight role call got.

Write order for a role call: a checkpoint naming it `in_flight` before the runtime is
invoked; when it ends, its `LLMCall` row, then a checkpoint that folds the row in
(`completed_calls`, routing counters, `last_llm_call_id`). Tool calls likewise. `resume_point`
covers a crash between a row and its checkpoint.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from mpilot.agents.router import RoutingCounters, StickyState
from mpilot.core.types import (
    AttemptId,
    LLMCallId,
    Outcome,
    Role,
    RunId,
    RunStatus,
    StepId,
    StopReason,
    ToolCallId,
)
from mpilot.models.domain import LLMCall, NonNegFloat, NonNegInt, PositiveInt, Spent, ToolCall

_SCOPE = re.compile(r"^(step/[A-Za-z0-9._-]+|phase/[A-Z_]+)$")
_SNAPSHOT_REF = re.compile(r"^refs/mpilot/(phase/[a-z_]+|attempt/[1-9][0-9]*)$")

StickyScope = Annotated[str, Field(pattern=_SCOPE.pattern)]


def sticky_scope(step_id: StepId | None, state: RunStatus) -> str:
    """Key of the scope routing stickiness applies to (§8.4 Purity): the plan step, or the
    run phase for calls outside any step (Analyzer, Planner, PR Writer)."""
    return f"step/{step_id}" if step_id is not None else f"phase/{state.value}"


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FailureHistoryEntry(StateModel):
    """One failure signature across attempts (§10.1): used for no-progress detection and to
    tell the Diagnoser what not to retry."""

    signature: str = Field(min_length=1)
    attempts: PositiveInt
    hypotheses_tried: tuple[str, ...] = ()
    outcome: Outcome | None = None  # of the latest attempt on this signature


class InFlightCall(StateModel):
    """The role call that was running when the checkpoint was written. On resume it restarts
    from the workspace as it was after `tool_calls_done[-1]`, given `summary` (B5). With a
    nested `explore`, this is the outermost call: the Explorer call restarts with it."""

    role_call_id: LLMCallId
    role: Role
    step_id: StepId | None = None
    tool_calls_done: tuple[ToolCallId, ...] = ()
    summary: str = ""  # what those tool calls did, for the restarted call's prompt


class BudgetState(StateModel):
    """Everything the BudgetManager needs to continue exactly where it stopped."""

    spent: Spent = Field(default_factory=Spent)
    active_seconds: NonNegFloat = 0.0  # wall clock excluding PAUSED_LIMIT time (NFR-2)
    attempts_by_step: dict[StepId, NonNegInt] = Field(default_factory=dict)
    attempts_total: NonNegInt = 0
    #: Guard charges of the role call still running (not yet reconciled into `spent`), so a
    #: crash doesn't lose them. Counted against the remaining room; cleared by `record_call`.
    #: Not covered by `Checkpoint.covers`, since reconciliation may lower them.
    in_flight_turns: NonNegInt = 0
    in_flight_tokens: NonNegInt = 0

    @model_validator(mode="after")
    def _check(self) -> Self:
        if sum(self.attempts_by_step.values()) != self.attempts_total:
            raise ValueError("attempts_total must equal the sum over steps")
        return self


class Checkpoint(StateModel):
    """Written after every tool call and every state change (NFR-4)."""

    run_id: RunId
    seq: NonNegInt  # strictly increasing per run
    at: AwareDatetime
    status: RunStatus
    paused_at_state: RunStatus | None = None  # set exactly in PAUSED_LIMIT
    step_id: StepId | None = None
    attempt_n: PositiveInt | None = None  # run-wide `Attempt.n` of the attempt in progress
    #: Id of that attempt, minted once at its start: its ToolCall and LLMCall rows carry it,
    #: and the `Attempt` row only exists once it ends. Set exactly with `attempt_n`.
    attempt_id: AttemptId | None = None
    #: sticky_scope(...) -> state. Never loses a flag from one checkpoint to the next.
    sticky: dict[StickyScope, StickyState] = Field(default_factory=dict)
    #: sticky_scope(...) -> routing inputs from the scope's history (§8.4, FR-21).
    routing: dict[StickyScope, RoutingCounters] = Field(default_factory=dict)
    budget: BudgetState = Field(default_factory=BudgetState)
    failure_history: tuple[FailureHistoryEntry, ...] = ()
    #: High-water marks: the latest `ToolCall` and `LLMCall` rows this checkpoint has folded
    #: in (routing counters, completed calls). Rows after them were written by a process that
    #: died before its next checkpoint; `resume_point` folds them in. Never cleared once set.
    last_tool_call_id: ToolCallId | None = None
    last_llm_call_id: LLMCallId | None = None
    #: Top-level role calls (no `explore` nesting) that finished in the current unit of work,
    #: in order. A unit is one `unit_key`: the state, step, attempt and restore epoch. A
    #: resume takes their rows' outputs and never re-runs them (FR-21). Cleared when the unit
    #: changes.
    completed_calls: tuple[LLMCallId, ...] = ()
    #: Bumped by every crash restore (§10.1): the calls the restore undid leave
    #: `completed_calls`, which is otherwise only ever extended within a unit.
    restore_epoch: NonNegInt = 0
    in_flight: InFlightCall | None = None
    #: Latest host snapshot (`refs/mpilot/attempt/<n>`, else a phase ref); crash recovery
    #: restores the workspace from it (§10.1).
    latest_snapshot_ref: str | None = None
    export_dir: str | None = None  # set when paused: where the workspace export lives
    export_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: Why the run ended (set only in a terminal state); copied to `Run.terminal_reason`.
    terminal_reason: str | None = None
    integrity_failures: NonNegInt = 0  # §7.4: one failure -> REPAIR_LOOP, a second -> ESCALATED
    critic_revisions: NonNegInt = 0  # FR-15: at most one revise loop

    @field_validator("latest_snapshot_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v is not None and not _SNAPSHOT_REF.fullmatch(v):
            raise ValueError(f"not a host snapshot ref: {v!r}")
        return v

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.status is RunStatus.PAUSED_LIMIT) != (self.paused_at_state is not None):
            raise ValueError("paused_at_state is set exactly in PAUSED_LIMIT")
        if self.paused_at_state is not None and (
            self.paused_at_state.is_terminal or self.paused_at_state is RunStatus.PAUSED_LIMIT
        ):
            raise ValueError("paused_at_state must be a state the run can resume into")
        if self.status is RunStatus.PAUSED_LIMIT and (
            self.export_dir is None or self.export_digest is None
        ):
            raise ValueError("a paused run must record its workspace export and digest (FR-21)")
        if (self.export_dir is None) != (self.export_digest is None):
            raise ValueError("export_dir and export_digest are set together")
        if self.attempt_n is not None and self.step_id is None:
            raise ValueError("attempt_n needs a step_id")
        if (self.attempt_n is None) != (self.attempt_id is None):
            raise ValueError("attempt_n and attempt_id are set together")
        if self.attempt_n is not None and self.attempt_n != self.budget.attempts_total:
            raise ValueError("attempt_n is the run-wide count of started attempts")
        if self.attempt_n is not None and (self.paused_at_state or self.status) is not (
            RunStatus.REPAIR_LOOP
        ):
            raise ValueError("an attempt runs only in REPAIR_LOOP (§7.4)")
        if self.attempt_n is not None and self.latest_snapshot_ref == (
            f"refs/mpilot/attempt/{self.attempt_n}"
        ):
            raise ValueError("an attempt's own snapshot ends it: attempt_n must have moved on")
        if len(set(self.completed_calls)) != len(self.completed_calls):
            raise ValueError("a role call is listed as completed twice")
        if self.in_flight is not None and self.in_flight.role_call_id in self.completed_calls:
            raise ValueError("the in-flight call cannot also be completed")
        if self.critic_revisions > 1:
            raise ValueError("at most one Critic revise loop (FR-15)")
        if self.terminal_reason is not None and not self.status.is_terminal:
            raise ValueError("terminal_reason is set only in a terminal state")
        signatures = [e.signature for e in self.failure_history]
        if len(signatures) != len(set(signatures)):
            raise ValueError("failure_history has one entry per signature")
        return self

    @property
    def unit_key(self) -> tuple[RunStatus, StepId | None, int | None, int]:
        """The unit of work `completed_calls` belongs to: the state the run is in (the one it
        paused in, while paused), the step, the attempt and the restore epoch."""
        return (
            self.paused_at_state or self.status,
            self.step_id,
            self.attempt_n,
            self.restore_epoch,
        )

    @property
    def scope(self) -> str:
        """The routing scope of the current unit (`sticky_scope`), never `phase/PAUSED_LIMIT`."""
        return sticky_scope(self.step_id, self.paused_at_state or self.status)

    def covers(self, previous: Checkpoint) -> list[str]:
        """Problems if `self` would undo anything `previous` recorded (resume safety, B5):
        a sticky flag or routing counter dropped, an attempt counter or spent value lowered,
        a failure-history entry lost, the attempt in progress renumbered or given a new id,
        a completed role call of the same unit forgotten, or the sequence not increasing.
        Empty list = safe to write."""
        problems: list[str] = []
        if self.run_id != previous.run_id:
            problems.append("checkpoints of different runs")
        if self.seq <= previous.seq:
            problems.append(f"seq {self.seq} does not follow {previous.seq}")
        if previous.status.is_terminal:
            problems.append(f"the run already ended in {previous.status}")
        for scope, old in previous.sticky.items():
            new = self.sticky.get(scope)
            if new is None or not new.covers(old):
                problems.append(f"sticky state for {scope} was lowered")
        for scope, old_counters in previous.routing.items():
            new_counters = self.routing.get(scope)
            if new_counters is None or not new_counters.covers(old_counters):
                problems.append(f"routing counters for {scope} were lowered")
        if self.attempt_n is not None:
            if self.attempt_n == previous.attempt_n:
                if self.attempt_id != previous.attempt_id:
                    problems.append(f"attempt {self.attempt_n} changed its id")
            elif self.attempt_n <= previous.budget.attempts_total:
                problems.append(
                    f"attempt {self.attempt_n} is not new: "
                    f"{previous.budget.attempts_total} attempts had started"
                )
            elif self.attempt_id == previous.attempt_id:
                problems.append(f"attempt {self.attempt_n} reuses the previous attempt's id")
        for mark in ("last_tool_call_id", "last_llm_call_id"):
            if getattr(previous, mark) is not None and getattr(self, mark) is None:
                problems.append(f"{mark} was cleared")
        if self.restore_epoch < previous.restore_epoch:
            problems.append("restore_epoch was lowered")
        if self.unit_key == previous.unit_key:
            kept = self.completed_calls[: len(previous.completed_calls)]
            if kept != previous.completed_calls:
                problems.append("a completed role call of the current unit was dropped")
        elif (  # a crash restore of the same unit
            self.unit_key[:3] == previous.unit_key[:3]
            and self.completed_calls != previous.completed_calls[: len(self.completed_calls)]
        ):
            problems.append("a crash restore may only cut completed calls back, not change them")
        b, pb = self.budget, previous.budget
        for step, n in pb.attempts_by_step.items():
            if b.attempts_by_step.get(step, 0) < n:
                problems.append(f"attempt counter for {step} was lowered")
        for field in Spent.model_fields:
            if getattr(b.spent, field) < getattr(pb.spent, field):
                problems.append(f"spent.{field} was lowered")
        if b.active_seconds < pb.active_seconds:
            problems.append("active_seconds was lowered")
        now = {e.signature: e for e in self.failure_history}
        for old_entry in previous.failure_history:
            new_entry = now.get(old_entry.signature)
            if new_entry is None:
                problems.append(f"failure history lost {old_entry.signature}")
                continue
            if new_entry.attempts < old_entry.attempts:
                problems.append(f"attempts on {old_entry.signature} were lowered")
            if not set(old_entry.hypotheses_tried) <= set(new_entry.hypotheses_tried):
                problems.append(f"tried hypotheses on {old_entry.signature} were lost")
        if self.integrity_failures < previous.integrity_failures:
            problems.append("integrity_failures was lowered")
        if self.critic_revisions < previous.critic_revisions:
            problems.append("critic_revisions was lowered")
        return problems


#: Roles whose calls change the workspace tree. A crash restore (§10.1) undoes them.
TREE_WRITER_ROLES = frozenset({Role.EDITOR, Role.B2_SINGLE_SHOT, Role.B3_GENERIC})


class ResumePoint(StateModel):
    """Where a resume picks up within the current unit (FR-21, §10.1).

    The driver first writes `checkpoints` in order, before routing or calling anything; then
    it never re-runs `completed`, feeds their recorded outputs to what comes next, and
    continues its flow after the last of them.
    """

    #: One or two checkpoints to write first: the reconciliation (rows the latest checkpoint
    #: never folded in), then, after a crash, the restore (`restore_epoch` + 1, the undone
    #: calls cut from `completed_calls`, nothing in flight). Each `covers` its predecessor.
    checkpoints: tuple[Checkpoint, ...] = Field(min_length=1, max_length=2)
    completed: tuple[LLMCall, ...] = ()
    #: After a pause only: the call to restart from the exported workspace, given a summary of
    #: `tool_calls_done`. After a crash the driver's flow simply issues the call again, with
    #: no summary: the restore undid its tool calls.
    restart: InFlightCall | None = None
    #: After a crash only: finished calls of the unit that run again, because restoring the
    #: latest host snapshot undid their edits: the first tree-writing call and every call
    #: after it. Their tokens still count (they stay in `spent`).
    undone: tuple[LLMCall, ...] = ()


def _after[R: (LLMCall, ToolCall)](rows: Sequence[R], mark: str | None, kind: str) -> list[R]:
    """Rows after the high-water mark, in insertion order; all of them if there is no mark."""
    if mark is None:
        return list(rows)
    ids: list[str] = [r.id for r in rows]
    if mark not in ids:
        raise ValueError(f"the checkpoint's last {kind} {mark} has no row")
    return list(rows[ids.index(mark) + 1 :])


def resume_point(
    cp: Checkpoint,
    calls: Sequence[LLMCall],
    tool_calls: Sequence[ToolCall],
    *,
    at: datetime,
) -> ResumePoint:
    """What a resume from `cp` writes first, keeps, restarts and re-runs, given the run's
    `LLMCall` and `ToolCall` rows in insertion order (`RunStore.rows`).

    Protocol it relies on: a checkpoint names a role call in `in_flight` before the runtime
    is invoked; its row is written when it ends, then a checkpoint that folds the row in
    (`completed_calls`, routing counters, `last_llm_call_id`). Tool calls likewise: row, then
    a checkpoint with `last_tool_call_id`. Every state change writes a checkpoint, so rows
    after the high-water marks belong to `cp`'s unit.

    1. Reconcile: fold rows after the marks into `routing[cp.scope]` (denials, schema-invalid
       answers, confidences), so a crash between a row and its checkpoint loses no router
       input; promote finished top-level calls among them to completed; add the in-flight
       call's newer tool calls to its `tool_calls_done`.
    2. After a pause (`PAUSED_LIMIT`) the export is the state after the last completed tool
       call: every finished call is kept, an unfinished one restarts with its summary.
    3. After a crash the workspace goes back to `latest_snapshot_ref`, which never holds the
       unit's own edits (an attempt's own snapshot may not be named while it runs; a phase
       snapshot is only taken as its phase ends). Finished calls before the unit's first
       tree-writing call are kept; that call and everything after it run again.

    Raises `ValueError` if the rows and `cp` disagree (a listed or newer finished call is
    missing, hit a limit, is nested, or belongs to another step or attempt; a mark has no
    row): guessing could re-run or skip a call.
    """
    llm_rows = [c for c in calls if c.run_id == cp.run_id]
    tool_rows = [t for t in tool_calls if t.run_id == cp.run_id]
    new_llm = _after(llm_rows, cp.last_llm_call_id, "LLMCall")
    new_tools = _after(tool_rows, cp.last_tool_call_id, "ToolCall")
    by_id = {c.id: c for c in llm_rows}

    def check(row: LLMCall) -> LLMCall:
        if row.parent_call_id is not None or (row.step_id, row.attempt_id) != (
            cp.step_id,
            cp.attempt_id,
        ):
            raise ValueError(f"{row.id} is nested or from another step or attempt")
        return row

    done: list[LLMCall] = []
    for call_id in cp.completed_calls:
        listed = by_id.get(call_id)
        if listed is None:
            raise ValueError(f"checkpoint lists {call_id} as completed, but it has no row")
        if not listed.finished:
            raise ValueError(f"checkpoint lists {call_id} as completed, but it hit a limit")
        done.append(check(listed))
    counters = cp.routing.get(cp.scope, RoutingCounters())
    for tool_row in new_tools:
        if tool_row.attempt_id != cp.attempt_id:
            raise ValueError(f"tool call {tool_row.id} is from another attempt")
        counters = counters.after_tool_call(tool_row)
    for row in new_llm:
        if (row.step_id, row.attempt_id) != (cp.step_id, cp.attempt_id):
            raise ValueError(f"{row.id} is from another step or attempt")
        counters = counters.after_call(row)
        if row.parent_call_id is None and row.finished and row.id not in cp.completed_calls:
            done.append(check(row))
    in_flight = cp.in_flight
    if in_flight is not None:
        if any(c.id == in_flight.role_call_id for c in done):
            in_flight = None
        else:
            newer = tuple(
                t.id
                for t in new_tools
                if t.role_call_id == in_flight.role_call_id
                and t.id not in in_flight.tool_calls_done
            )
            in_flight = in_flight.model_copy(
                update={"tool_calls_done": in_flight.tool_calls_done + newer}
            )
    update: dict[str, Any] = {
        "seq": cp.seq + 1,
        "at": at,
        "completed_calls": tuple(c.id for c in done),
        "in_flight": in_flight,
        "last_tool_call_id": new_tools[-1].id if new_tools else cp.last_tool_call_id,
        "last_llm_call_id": new_llm[-1].id if new_llm else cp.last_llm_call_id,
    }
    if new_tools or new_llm:
        update["routing"] = {**cp.routing, cp.scope: counters}
    reconciled = Checkpoint.model_validate({**cp.model_dump(), **update})
    if cp.status is RunStatus.PAUSED_LIMIT:
        return ResumePoint(checkpoints=(reconciled,), completed=tuple(done), restart=in_flight)
    first_writer = next((i for i, c in enumerate(done) if c.role in TREE_WRITER_ROLES), len(done))
    kept = tuple(done[:first_writer])
    restored = Checkpoint.model_validate(
        {
            **reconciled.model_dump(),
            "seq": reconciled.seq + 1,
            "completed_calls": tuple(c.id for c in kept),
            "in_flight": None,
            "restore_epoch": cp.restore_epoch + 1,
        }
    )
    return ResumePoint(
        checkpoints=(reconciled, restored), completed=kept, undone=tuple(done[first_writer:])
    )


def latest_output(calls: Sequence[LLMCall], role: Role) -> dict[str, Any] | None:
    """The recorded answer of the run's latest completed top-level `role` call, or None.
    This is where a step-less role's result lives between phases: the Analyzer's
    `AnalysisReport` for the Planner (§8.2), on a resume and without one alike."""
    for call in reversed(calls):
        if call.role is role and call.parent_call_id is None and call.stop is StopReason.COMPLETED:
            return call.output
    return None
