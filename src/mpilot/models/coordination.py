"""Process coordination: one driver per run, and requests other processes make (P1-12c,
SPEC §10.1, §13.1, FR-21, NFR-4, C6).

- **Driver lease.** Only the process holding a run's lease may write the run's state
  (`save_run`, rows, checkpoints). A lease expires unless renewed (a crashed driver frees the
  run), and every takeover raises its fencing `token`. Writes carry the lease and the store
  refuses any whose token is not the current one, so a stalled driver that lost its lease can
  never write again, not even after the new holder has finished.
- **Requests.** `mpilot cancel` and `mpilot approve` run in other processes. They record a
  request in the run's `RunControl` (durable, no lease needed). A live driver meets a cancel
  at its next checkpoint (the store refuses anything but END_CANCELLED from then on) and
  polls for a plan decision while it waits in AWAITING_APPROVAL. With no live driver, the
  requesting process takes the lease and applies the request itself.

The rules are pure functions here, so the SQLite store and the in-memory double apply exactly
the same ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from mpilot.core.errors import LeaseHeldError, LeaseLostError
from mpilot.core.types import RunId, RunStatus
from mpilot.models.domain import PositiveInt, Run
from mpilot.models.state import Checkpoint


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanDecision(StrEnum):
    """The human's answer at CP-1 (`mpilot approve [--reject]`, §11.5)."""

    APPROVE = "approve"
    REJECT = "reject"


class Lease(_Frozen):
    """The right to drive one run. `token` is the fencing token: it rises with every new
    holder, so writes made under an older lease are refused."""

    run_id: RunId
    holder: str = Field(min_length=1)  # who drives, for `mpilot status` ("mpilot run, pid 42")
    token: PositiveInt
    acquired_at: AwareDatetime
    expires_at: AwareDatetime
    released_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.expires_at <= self.acquired_at:
            raise ValueError("a lease must expire after it was acquired")
        return self

    def live(self, now: datetime) -> bool:
        """Held: not released and not expired. Only a live lease keeps others out."""
        return self.released_at is None and now < self.expires_at


class RunControl(_Frozen):
    """Requests other processes have recorded for a run. Each is made once and never undone."""

    run_id: RunId
    cancel_requested_at: AwareDatetime | None = None
    cancel_reason: str | None = None
    plan_decision: PlanDecision | None = None
    plan_reason: str | None = None
    plan_decided_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.cancel_reason is not None and self.cancel_requested_at is None:
            raise ValueError("cancel_reason needs a cancel request")
        if (self.plan_decision is None) != (self.plan_decided_at is None):
            raise ValueError("plan_decision and plan_decided_at are set together")
        if self.plan_reason is not None and self.plan_decision is None:
            raise ValueError("plan_reason needs a plan decision")
        return self

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_requested_at is not None


def take_lease(
    current: Lease | None, run_id: RunId, holder: str, *, now: datetime, ttl_s: float
) -> Lease:
    """The lease `holder` gets, given the run's current one: a new lease with the next token.
    Any live lease raises `LeaseHeldError`, whoever holds it: holder names are labels, not
    identities, so the same name asking twice is two drivers (extend a lease with `renew`)."""
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    expires = now + timedelta(seconds=ttl_s)
    if current is not None and current.live(now):
        raise LeaseHeldError(
            f"run {run_id} is being driven by {current.holder} (lease until "
            f"{current.expires_at.isoformat()})"
        )
    token = current.token + 1 if current is not None else 1
    return Lease(run_id=run_id, holder=holder, token=token, acquired_at=now, expires_at=expires)


def check_fence(current: Lease | None, lease: Lease, run_id: RunId) -> Lease:
    """The run's current lease, if it is `lease` and not released; else `LeaseLostError`. Expiry
    alone does not refuse a write: until someone else takes over, nobody else can write
    either, and the takeover raises the token."""
    if lease.run_id != run_id:
        raise LeaseLostError(f"a lease for {lease.run_id} cannot write to {run_id}")
    if current is None or (current.token, current.holder) != (lease.token, lease.holder):
        holder = "nobody" if current is None else f"{current.holder} (token {current.token})"
        raise LeaseLostError(f"lease {lease.token} on {run_id} is no longer current: {holder}")
    if current.released_at is not None:
        raise LeaseLostError(f"lease {lease.token} on {run_id} was released")
    return current


def renew(current: Lease | None, lease: Lease, *, now: datetime, ttl_s: float) -> Lease:
    """`lease` with a new expiry, if it is still current (`check_fence`)."""
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    held = check_fence(current, lease, lease.run_id)
    return held.model_copy(update={"expires_at": now + timedelta(seconds=ttl_s)})


#: States a run may reach from AWAITING_APPROVAL without a recorded approval (CP-1, §7.4):
#: still waiting (or paused while waiting), the endings §7.4 allows from any state, and the
#: two endings that have their own recorded request.
_WITHOUT_APPROVAL = frozenset(
    {
        RunStatus.AWAITING_APPROVAL,
        RunStatus.END_ERROR,
        RunStatus.END_BUDGET_EXCEEDED,
        RunStatus.END_CANCELLED,
        RunStatus.END_REJECTED,
    }
)


def transition_problems(
    new_state: RunStatus, previous: RunStatus, control: RunControl
) -> list[str]:
    """What a checkpoint moving the run from `previous` to `new_state` needs from the
    recorded requests. Both are effective states: the state a paused run paused in
    (`Checkpoint.unit_key[0]`), so a pause can never stand in for leaving a state. A pending
    cancel is not listed here: the store raises `CancelRequestedError` for it first.

    - END_CANCELLED only after a cancel request (C6): a driver cannot invent one.
    - END_REJECTED only after a recorded reject.
    - From AWAITING_APPROVAL, anything outside `_WITHOUT_APPROVAL` only after a recorded
      approve: CP-1 is a human decision (§11.5), never the driver's.
    """
    problems: list[str] = []
    if new_state is RunStatus.END_CANCELLED and not control.cancel_requested:
        problems.append("END_CANCELLED needs a recorded cancel request")
    if new_state is RunStatus.END_REJECTED and control.plan_decision is not PlanDecision.REJECT:
        problems.append("END_REJECTED needs a recorded plan rejection (CP-1)")
    if (
        previous is RunStatus.AWAITING_APPROVAL
        and new_state not in _WITHOUT_APPROVAL
        and control.plan_decision is not PlanDecision.APPROVE
    ):
        problems.append(f"leaving AWAITING_APPROVAL for {new_state} needs a recorded approval")
    return problems


def plan_problem(plan_approved: bool | None, control: RunControl) -> str | None:
    """A stored `Plan.approved` must agree with the recorded decision: the driver copies the
    human's answer, it never makes one up (CP-1)."""
    if plan_approved is None:
        return None
    expected = PlanDecision.APPROVE if plan_approved else PlanDecision.REJECT
    if control.plan_decision is not expected:
        return f"Plan.approved={plan_approved} without a recorded {expected} decision (CP-1)"
    return None


class Renewal(_Frozen):
    """What `RunStore.renew_lease` returns: the extended lease and the run's requests, so a
    driver's heartbeat sees a cancel even while it is not checkpointing (a long tool call,
    waiting for approval) and cannot forget to look."""

    lease: Lease
    control: RunControl


#: `Run` fields only a checkpoint may change.
CHECKPOINT_OWNED = ("status", "paused_at_state", "spent", "terminal_reason")


def run_following(run: Run, cp: Checkpoint) -> Run:
    """`run` with the fields the checkpoint owns taken from `cp`: status, paused state, spent
    and terminal reason (the checkpoint is authoritative)."""
    return Run.model_validate(
        {
            **run.model_dump(),
            "status": cp.status,
            "paused_at_state": cp.paused_at_state,
            "spent": cp.budget.spent.model_dump(),
            "terminal_reason": cp.terminal_reason,
        }
    )
