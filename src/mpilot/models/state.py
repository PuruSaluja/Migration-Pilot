"""Run state that survives a crash or a usage-limit pause (SPEC §10.1, NFR-4, FR-21, B5).

A `Checkpoint` is written after every tool call. A resume rebuilds the orchestrator from the
latest one, so everything that must never go backwards lives here: routing sticky state
(no de-escalation), attempt counters (no fresh attempts), failure history (no retrying what
was rejected), the budget spent (an interrupted call's tokens still count), and how far the
in-flight role call got.
"""

from __future__ import annotations

import re
from typing import Annotated, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from mpilot.agents.router import StickyState
from mpilot.core.types import LLMCallId, Outcome, Role, RunId, RunStatus, StepId, ToolCallId
from mpilot.models.domain import NonNegFloat, NonNegInt, PositiveInt, Spent

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
    attempt_n: PositiveInt | None = None  # 1-based attempt on step_id
    #: sticky_scope(...) -> state. Never loses a flag from one checkpoint to the next.
    sticky: dict[StickyScope, StickyState] = Field(default_factory=dict)
    budget: BudgetState = Field(default_factory=BudgetState)
    failure_history: tuple[FailureHistoryEntry, ...] = ()
    last_tool_call_id: ToolCallId | None = None
    in_flight: InFlightCall | None = None
    #: Latest host snapshot (`refs/mpilot/attempt/<n>`, else a phase ref); crash recovery
    #: restores the workspace from it (§10.1).
    latest_snapshot_ref: str | None = None
    export_dir: str | None = None  # set when paused: where the workspace export lives
    export_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    integrity_failures: NonNegInt = 0  # §7.4: one failure -> REPAIR_LOOP, a second -> ESCALATED
    critic_revisions: NonNegInt = 0  # FR-15: at most one revise loop

    @field_validator("latest_snapshot_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v is not None and not _SNAPSHOT_REF.match(v):
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
        if self.critic_revisions > 1:
            raise ValueError("at most one Critic revise loop (FR-15)")
        signatures = [e.signature for e in self.failure_history]
        if len(signatures) != len(set(signatures)):
            raise ValueError("failure_history has one entry per signature")
        return self

    def covers(self, previous: Checkpoint) -> list[str]:
        """Problems if `self` would undo anything `previous` recorded (resume safety, B5):
        a sticky flag dropped, an attempt counter or spent value lowered, a failure-history
        entry lost, or the sequence not increasing. Empty list = safe to write."""
        problems: list[str] = []
        if self.run_id != previous.run_id:
            problems.append("checkpoints of different runs")
        if self.seq <= previous.seq:
            problems.append(f"seq {self.seq} does not follow {previous.seq}")
        for scope, old in previous.sticky.items():
            new = self.sticky.get(scope)
            if new is None or not new.covers(old):
                problems.append(f"sticky state for {scope} was lowered")
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
