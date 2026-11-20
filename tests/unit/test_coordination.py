"""P1-12c: process coordination (SPEC §10.1, §13.1, FR-21, NFR-4, C6).

- Pure rules (`models/coordination.py`): lease taking and fencing, requests, transitions.
- The store contract, against the in-memory double and the SQLite store: fenced writes, the
  checkpoint updating the run, cancel and plan-decision requests.
- Acceptance on SQLite with two store instances on one file (two processes): a cancel from
  another process reaches a live driver; no END_CANCELLED is lost, not even to a stalled
  driver coming back; two resumes can never both drive a run.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_store import final_verification, run, sample, tool_call

from mpilot.core.clock import FakeClock, SystemClock
from mpilot.core.errors import (
    CancelRequestedError,
    LeaseHeldError,
    LeaseLostError,
    RunEndedError,
    StoreError,
)
from mpilot.core.types import RunId, RunStatus
from mpilot.models import domain as d
from mpilot.models.coordination import (
    Lease,
    PlanDecision,
    RunControl,
    check_fence,
    renew,
    take_lease,
    transition_problems,
)
from mpilot.models.state import BudgetState, Checkpoint
from mpilot.models.store import SqliteRunStore
from mpilot.orchestrator.base import RunStore
from mpilot.testing.infra_doubles import MemoryStore

RUN = RunId("run-000000000001")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
TTL = 30.0


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def lease(token: int = 1, holder: str = "a", **over: Any) -> Lease:
    base: dict[str, Any] = {
        "run_id": RUN,
        "holder": holder,
        "token": token,
        "acquired_at": T0,
        "expires_at": at(TTL),
    }
    return Lease(**{**base, **over})


def state(seq: int, status: RunStatus = RunStatus.REPAIR_LOOP, **over: Any) -> Checkpoint:
    base: dict[str, Any] = {
        "run_id": RUN,
        "seq": seq,
        "at": at(seq),
        "status": status,
        "budget": BudgetState(spent=d.Spent(turns=seq, input_tokens=10 * seq)),
    }
    if status is RunStatus.PAUSED_LIMIT:
        base |= {
            "paused_at_state": RunStatus.REPAIR_LOOP,
            "export_dir": "/runs/x/export",
            "export_digest": "e" * 64,
        }
    return Checkpoint(**{**base, **over})


# ---------------------------------------------------------------- pure rules


def test_the_first_lease_has_token_1_and_expires_after_its_ttl() -> None:
    first = take_lease(None, RUN, "a", now=T0, ttl_s=TTL)
    assert (first.token, first.holder, first.acquired_at, first.expires_at) == (1, "a", T0, at(TTL))
    assert first.live(at(TTL - 1))
    assert not first.live(at(TTL))


def test_a_live_lease_keeps_everyone_out_even_its_own_holder_name() -> None:
    """Review B3: holder names are labels, so the same name asking twice is two drivers."""
    held = lease()
    for holder in ("b", "a"):
        with pytest.raises(LeaseHeldError, match="being driven by a"):
            take_lease(held, RUN, holder, now=at(1), ttl_s=TTL)
    assert renew(held, held, now=at(10), ttl_s=TTL).expires_at == at(10 + TTL)


@pytest.mark.parametrize(
    "current", [lease(), lease(released_at=at(5))], ids=["expired", "released"]
)
def test_a_dead_lease_is_taken_over_with_a_higher_token(current: Lease) -> None:
    now = at(TTL + 1) if current.released_at is None else at(6)
    taken = take_lease(current, RUN, "b", now=now, ttl_s=TTL)
    assert (taken.token, taken.holder, taken.released_at) == (2, "b", None)


def test_a_ttl_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        take_lease(None, RUN, "a", now=T0, ttl_s=0)
    with pytest.raises(ValueError, match="positive"):
        renew(lease(), lease(), now=T0, ttl_s=-1)


def test_lease_invariants() -> None:
    with pytest.raises(ValidationError, match="expire after"):
        lease(expires_at=T0)
    with pytest.raises(ValidationError):
        lease(holder="")
    with pytest.raises(ValidationError):
        lease(token=0)


@pytest.mark.parametrize(
    ("current", "mine", "run_id", "fragment"),
    [
        (None, lease(), RUN, "no longer current: nobody"),
        (lease(token=2, holder="b"), lease(), RUN, "no longer current: b"),
        (lease(holder="b"), lease(), RUN, "no longer current"),
        (lease(released_at=at(1)), lease(), RUN, "was released"),
        (lease(), lease(), RunId("run-other"), "cannot write"),
    ],
)
def test_only_the_current_lease_passes_the_fence(
    current: Lease | None, mine: Lease, run_id: RunId, fragment: str
) -> None:
    with pytest.raises(LeaseLostError, match=fragment):
        check_fence(current, mine, run_id)


def test_an_expired_lease_still_writes_until_someone_takes_over() -> None:
    """Expiry only lets others in; the takeover's higher token is what refuses the old one."""
    old = lease()
    assert check_fence(old, old, RUN) == old  # long expired, nobody else came
    assert renew(old, old, now=at(100), ttl_s=TTL).expires_at == at(100 + TTL)
    taken = take_lease(old, RUN, "b", now=at(100), ttl_s=TTL)
    with pytest.raises(LeaseLostError):
        check_fence(taken, old, RUN)


def test_run_control_invariants() -> None:
    with pytest.raises(ValidationError, match="needs a cancel request"):
        RunControl(run_id=RUN, cancel_reason="x")
    with pytest.raises(ValidationError, match="set together"):
        RunControl(run_id=RUN, plan_decision=PlanDecision.APPROVE)
    with pytest.raises(ValidationError, match="needs a plan decision"):
        RunControl(run_id=RUN, plan_reason="x")
    assert not RunControl(run_id=RUN).cancel_requested
    assert RunControl(run_id=RUN, cancel_requested_at=T0).cancel_requested


NONE = RunControl(run_id=RUN)
CANCEL = RunControl(run_id=RUN, cancel_requested_at=T0)
APPROVED = RunControl(run_id=RUN, plan_decision=PlanDecision.APPROVE, plan_decided_at=T0)
REJECTED = RunControl(run_id=RUN, plan_decision=PlanDecision.REJECT, plan_decided_at=T0)
S = RunStatus


@pytest.mark.parametrize(
    ("new", "was", "control", "ok"),
    [
        (S.END_CANCELLED, S.REPAIR_LOOP, NONE, False),  # a driver cannot invent a cancel
        (S.END_CANCELLED, S.REPAIR_LOOP, CANCEL, True),
        (S.END_REJECTED, S.AWAITING_APPROVAL, NONE, False),
        (S.END_REJECTED, S.AWAITING_APPROVAL, APPROVED, False),
        (S.END_REJECTED, S.AWAITING_APPROVAL, REJECTED, True),
        (S.BUMPED, S.AWAITING_APPROVAL, NONE, False),  # CP-1 is a human decision
        (S.BUMPED, S.AWAITING_APPROVAL, REJECTED, False),
        (S.BUMPED, S.AWAITING_APPROVAL, APPROVED, True),
        (S.AWAITING_APPROVAL, S.AWAITING_APPROVAL, NONE, True),  # still (or paused) waiting
        (S.END_ERROR, S.AWAITING_APPROVAL, NONE, True),  # §7.4: any -> END_ERROR
        (S.END_BUDGET_EXCEEDED, S.AWAITING_APPROVAL, NONE, True),
        (S.END_SUCCESS, S.AWAITING_APPROVAL, NONE, False),  # review B2: no terminal bypass
        (S.END_ESCALATED, S.AWAITING_APPROVAL, NONE, False),
        (S.END_BASELINE_RED, S.AWAITING_APPROVAL, NONE, False),
        (S.CODEMODS_APPLIED, S.AWAITING_APPROVAL, NONE, False),
        (S.END_SUCCESS, S.AWAITING_APPROVAL, APPROVED, True),
        (S.BUMPED, S.PLANNED, NONE, True),  # no --approve-plan: no decision needed
    ],
)
def test_transition_rules(new: RunStatus, was: RunStatus, control: RunControl, ok: bool) -> None:
    assert (transition_problems(new, was, control) == []) is ok


def test_a_terminal_checkpoint_is_final() -> None:
    ended = state(3, RunStatus.END_CANCELLED, terminal_reason="cancelled by user")
    assert "the run already ended in END_CANCELLED" in state(4).covers(ended)
    assert any("already ended" in p for p in state(4, RunStatus.END_ERROR).covers(ended))


def test_terminal_reason_only_in_a_terminal_state() -> None:
    with pytest.raises(ValidationError, match="only in a terminal state"):
        state(1, terminal_reason="x")


# ---------------------------------------------------------------- the store contract


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path, clock: FakeClock) -> Iterator[RunStore]:
    if request.param == "memory":
        yield MemoryStore(clock=clock)
        return
    sqlite = SqliteRunStore.open(tmp_path / "mpilot.db", clock)
    yield sqlite
    sqlite.close()


@pytest.fixture
def driven(store: RunStore) -> Lease:
    store.create_run(run())
    return store.acquire_lease(RUN, "driver-a", ttl_s=TTL)


def test_both_stores_implement_the_extended_protocol(store: RunStore) -> None:
    assert isinstance(store, RunStore)


def test_acquiring_needs_a_known_run_that_has_not_ended(store: RunStore) -> None:
    with pytest.raises(KeyError):
        store.acquire_lease(RUN, "a", ttl_s=TTL)
    store.create_run(run(status=RunStatus.END_SUCCESS, paused_at_state=None))
    with pytest.raises(RunEndedError):
        store.acquire_lease(RUN, "a", ttl_s=TTL)


def test_the_same_holder_name_cannot_take_a_live_lease_twice(
    store: RunStore, driven: Lease
) -> None:
    """Review B3: two `mpilot resume` processes with the same label still can't both drive."""
    with pytest.raises(LeaseHeldError):
        store.acquire_lease(RUN, "driver-a", ttl_s=TTL)
    assert store.current_lease(RUN) == driven


def test_save_run_on_an_unknown_run_is_a_key_error(store: RunStore) -> None:
    with pytest.raises(KeyError):
        store.save_run(run(), lease=lease())


def test_a_second_driver_waits_for_expiry_then_takes_over(
    store: RunStore, driven: Lease, clock: FakeClock
) -> None:
    with pytest.raises(LeaseHeldError, match="driver-a"):
        store.acquire_lease(RUN, "driver-b", ttl_s=TTL)
    assert store.current_lease(RUN) == driven
    clock.advance(TTL)
    taken = store.acquire_lease(RUN, "driver-b", ttl_s=TTL)
    assert (taken.token, taken.holder) == (2, "driver-b")
    assert store.current_lease(RUN) == taken


def _every_write(store: RunStore, held: Lease) -> dict[str, Any]:
    return {
        "append": lambda: store.append(tool_call("tc-x"), lease=held),
        "put": lambda: store.put(sample(d.Plan), lease=held),
        "save_run": lambda: store.save_run(run(branch="mpilot/x-9"), lease=held),
        "final": lambda: store.append_final_verification(RUN, final_verification(), lease=held),
        "checkpoint": lambda: store.checkpoint(state(50), lease=held),
        "renew": lambda: store.renew_lease(held, ttl_s=TTL),
    }


@pytest.mark.parametrize("write", ["append", "put", "save_run", "final", "checkpoint", "renew"])
def test_a_driver_that_lost_its_lease_can_write_nothing(
    store: RunStore, driven: Lease, clock: FakeClock, write: str
) -> None:
    clock.advance(TTL + 1)
    store.acquire_lease(RUN, "driver-b", ttl_s=TTL)  # takeover
    with pytest.raises(LeaseLostError):
        _every_write(store, driven)[write]()
    assert store.rows(RUN, d.ToolCall) == []
    assert store.latest_checkpoint(RUN) is None


@pytest.mark.parametrize("write", ["append", "put", "save_run", "final", "checkpoint", "renew"])
def test_a_released_lease_can_write_nothing(store: RunStore, driven: Lease, write: str) -> None:
    store.release_lease(driven)
    with pytest.raises(LeaseLostError, match="released"):
        _every_write(store, driven)[write]()


def test_releasing_a_lease_that_was_taken_over_frees_nothing(
    store: RunStore, driven: Lease, clock: FakeClock
) -> None:
    clock.advance(TTL + 1)
    taken = store.acquire_lease(RUN, "driver-b", ttl_s=TTL)
    store.release_lease(driven)  # the stalled driver gives up: must not free b's lease
    assert store.current_lease(RUN) == taken
    store.append(tool_call("tc-b"), lease=taken)
    store.release_lease(taken)
    store.release_lease(taken)  # idempotent
    after = store.acquire_lease(RUN, "driver-c", ttl_s=TTL)
    assert after.token == 3


def test_renewing_extends_the_lease_and_reports_the_requests(
    store: RunStore, driven: Lease, clock: FakeClock
) -> None:
    clock.advance(20)
    renewal = store.renew_lease(driven, ttl_s=TTL)
    renewed = renewal.lease
    assert (renewed.token, renewed.expires_at) == (1, at(20 + TTL))
    assert not renewal.control.cancel_requested
    store.request_cancel(RUN)
    assert store.renew_lease(driven, ttl_s=TTL).control.cancel_requested
    clock.advance(TTL - 1)
    with pytest.raises(LeaseHeldError):
        store.acquire_lease(RUN, "driver-b", ttl_s=TTL)


def test_the_checkpoint_updates_the_run_in_the_same_step(store: RunStore, driven: Lease) -> None:
    store.checkpoint(state(1), lease=driven)
    store.checkpoint(state(2, RunStatus.PAUSED_LIMIT), lease=driven)
    paused = store.get_run(RUN)
    assert (paused.status, paused.paused_at_state) == (
        RunStatus.PAUSED_LIMIT,
        RunStatus.REPAIR_LOOP,
    )
    assert paused.spent == state(2).budget.spent
    store.checkpoint(state(3), lease=driven)
    store.checkpoint(state(4, RunStatus.END_ERROR, terminal_reason="sandbox died"), lease=driven)
    ended = store.get_run(RUN)
    assert (ended.status, ended.paused_at_state, ended.terminal_reason) == (
        RunStatus.END_ERROR,
        None,
        "sandbox died",
    )
    assert ended.spec == run().spec  # everything else kept


@pytest.mark.parametrize(
    "change",
    [
        {"status": RunStatus.INTEGRITY_CHECK},
        {"status": RunStatus.PAUSED_LIMIT, "paused_at_state": RunStatus.REPAIR_LOOP},
        {"spent": d.Spent(turns=99)},
        {"terminal_reason": "made up"},
    ],
)
def test_save_run_cannot_change_what_the_checkpoint_owns(
    store: RunStore, driven: Lease, change: dict[str, Any]
) -> None:
    with pytest.raises(StoreError, match="only through a checkpoint"):
        store.save_run(run(**change), lease=driven)
    assert store.get_run(RUN) == run()


def test_rows_given_with_a_checkpoint_are_written_with_it_or_not_at_all(
    store: RunStore, driven: Lease
) -> None:
    store.checkpoint(state(1), lease=driven, rows=[tool_call("tc-1"), tool_call("tc-2")])
    assert [r.id for r in store.rows(RUN, d.ToolCall)] == ["tc-1", "tc-2"]
    with pytest.raises(StoreError):
        store.checkpoint(state(1), lease=driven, rows=[tool_call("tc-3")])  # seq reused
    other = tool_call("tc-4").model_copy(update={"run_id": "run-other"})
    with pytest.raises(StoreError, match="row for"):
        store.checkpoint(state(2), lease=driven, rows=[other])
    assert [r.id for r in store.rows(RUN, d.ToolCall)] == ["tc-1", "tc-2"]
    assert store.latest_checkpoint(RUN) == state(1)


def test_a_refused_row_refuses_its_checkpoint(sqlite_store_driven: SqliteRunStore) -> None:
    """SQLite only: the schema refuses the row (an LLMCall whose routing decision was never
    stored), and the checkpoint is rolled back with it."""
    llm = sample(d.LLMCall)
    with pytest.raises(StoreError):
        sqlite_store_driven.checkpoint(state(1), lease=_lease_of(sqlite_store_driven), rows=[llm])
    assert sqlite_store_driven.latest_checkpoint(RUN) is None
    assert sqlite_store_driven.get_run(RUN) == run()


@pytest.fixture
def sqlite_store_driven(tmp_path: Path, clock: FakeClock) -> Iterator[SqliteRunStore]:
    s = SqliteRunStore.open(tmp_path / "one.db", clock)
    s.create_run(run())
    s.acquire_lease(RUN, "driver-a", ttl_s=TTL)
    yield s
    s.close()


def _lease_of(store: RunStore) -> Lease:
    found = store.current_lease(RUN)
    assert found is not None
    return found


# -- cancel


def test_a_cancel_request_is_recorded_once_and_needs_no_lease(
    store: RunStore, driven: Lease, clock: FakeClock
) -> None:
    with pytest.raises(KeyError):
        store.control(RunId("run-unknown"))
    assert store.control(RUN) == RunControl(run_id=RUN)
    first = store.request_cancel(RUN, reason="wrong branch")
    clock.advance(5)
    assert store.request_cancel(RUN, reason="again") == first  # the first request is kept
    assert (first.cancel_requested_at, first.cancel_reason) == (T0, "wrong branch")
    assert store.control(RUN) == first


def test_after_a_cancel_request_only_end_cancelled_can_be_checkpointed(
    store: RunStore, driven: Lease
) -> None:
    store.checkpoint(state(1), lease=driven)
    store.request_cancel(RUN)
    for refused in (state(2), state(2, RunStatus.PAUSED_LIMIT), state(2, RunStatus.END_SUCCESS)):
        with pytest.raises(CancelRequestedError):
            store.checkpoint(refused, lease=driven, rows=[tool_call("tc-1")])
    assert store.rows(RUN, d.ToolCall) == []  # nothing was written, rows included
    assert store.latest_checkpoint(RUN) == state(1)
    store.checkpoint(
        state(2, RunStatus.END_CANCELLED, terminal_reason="cancelled"),
        lease=driven,
        rows=[tool_call("tc-1")],
    )
    assert store.get_run(RUN).status is RunStatus.END_CANCELLED
    assert [r.id for r in store.rows(RUN, d.ToolCall)] == ["tc-1"]


def test_end_cancelled_needs_a_request(store: RunStore, driven: Lease) -> None:
    with pytest.raises(StoreError, match="needs a recorded cancel request"):
        store.checkpoint(state(1, RunStatus.END_CANCELLED), lease=driven)


def test_nothing_can_follow_end_cancelled(store: RunStore, driven: Lease) -> None:
    store.request_cancel(RUN)
    store.checkpoint(state(1, RunStatus.END_CANCELLED), lease=driven)
    current = store.current_lease(RUN)
    assert current is not None
    assert current.released_at is not None  # the terminal checkpoint freed the run
    with pytest.raises(LeaseLostError):
        store.checkpoint(state(2, RunStatus.END_CANCELLED), lease=driven)
    with pytest.raises(LeaseLostError):
        store.append(tool_call("tc-late"), lease=driven)
    with pytest.raises(RunEndedError):
        store.acquire_lease(RUN, "driver-b", ttl_s=TTL)
    before = store.control(RUN)
    assert store.request_cancel(RUN) == before  # a no-op on an ended run
    assert store.get_run(RUN).status is RunStatus.END_CANCELLED


def test_cancelling_a_run_that_already_ended_records_nothing(store: RunStore) -> None:
    store.create_run(run(status=RunStatus.END_SUCCESS, paused_at_state=None))
    assert not store.request_cancel(RUN).cancel_requested


def test_with_no_live_driver_the_canceller_ends_the_run_itself(
    store: RunStore, driven: Lease
) -> None:
    """`mpilot cancel` on a paused run: the driver released the lease when it paused."""
    store.checkpoint(state(1, RunStatus.PAUSED_LIMIT), lease=driven)
    store.release_lease(driven)
    store.request_cancel(RUN)
    canceller = store.acquire_lease(RUN, "mpilot cancel", ttl_s=TTL)
    store.checkpoint(state(2, RunStatus.END_CANCELLED), lease=canceller)
    assert store.get_run(RUN).status is RunStatus.END_CANCELLED


# -- plan decision (CP-1)


@pytest.fixture
def awaiting(store: RunStore) -> Lease:
    store.create_run(run(status=RunStatus.PLANNED, paused_at_state=None))
    held = store.acquire_lease(RUN, "mpilot run --approve-plan", ttl_s=TTL)
    store.checkpoint(state(1, RunStatus.AWAITING_APPROVAL), lease=held)
    return held


def test_a_plan_decision_is_only_taken_while_awaiting_it(store: RunStore, driven: Lease) -> None:
    with pytest.raises(StoreError, match="not awaiting plan approval"):
        store.request_plan_decision(RUN, approve=True)


def test_a_plan_is_decided_once(store: RunStore, awaiting: Lease) -> None:
    decided = store.request_plan_decision(RUN, approve=False, reason="too broad")
    assert (decided.plan_decision, decided.plan_reason, decided.plan_decided_at) == (
        PlanDecision.REJECT,
        "too broad",
        T0,
    )
    with pytest.raises(StoreError, match="already decided"):
        store.request_plan_decision(RUN, approve=True)
    assert store.control(RUN) == decided


def test_the_driver_cannot_leave_awaiting_approval_without_a_decision(
    store: RunStore, awaiting: Lease
) -> None:
    with pytest.raises(StoreError, match="needs a recorded approval"):
        store.checkpoint(state(2, RunStatus.BUMPED), lease=awaiting)
    with pytest.raises(StoreError, match="needs a recorded plan rejection"):
        store.checkpoint(state(2, RunStatus.END_REJECTED), lease=awaiting)
    store.request_plan_decision(RUN, approve=True)
    with pytest.raises(StoreError, match="plan rejection"):
        store.checkpoint(state(2, RunStatus.END_REJECTED), lease=awaiting)
    store.checkpoint(state(2, RunStatus.BUMPED), lease=awaiting)
    assert store.get_run(RUN).status is RunStatus.BUMPED


def test_a_rejection_ends_the_run_rejected(store: RunStore, awaiting: Lease) -> None:
    store.request_plan_decision(RUN, approve=False)
    with pytest.raises(StoreError, match="needs a recorded approval"):
        store.checkpoint(state(2, RunStatus.BUMPED), lease=awaiting)
    store.checkpoint(
        state(2, RunStatus.END_REJECTED, terminal_reason="plan rejected"), lease=awaiting
    )
    assert store.get_run(RUN).status is RunStatus.END_REJECTED


def test_with_no_live_driver_the_approver_continues_the_run(
    store: RunStore, awaiting: Lease
) -> None:
    """`mpilot run --approve-plan` was interrupted (exit 130) and released the lease."""
    store.release_lease(awaiting)
    store.request_plan_decision(RUN, approve=True)
    approver = store.acquire_lease(RUN, "mpilot approve", ttl_s=TTL)
    store.checkpoint(state(2, RunStatus.BUMPED), lease=approver)
    assert store.get_run(RUN).status is RunStatus.BUMPED


def test_a_pause_cannot_stand_in_for_leaving_awaiting_approval(
    store: RunStore, awaiting: Lease
) -> None:
    """Review B1: AWAITING_APPROVAL -> PAUSED_LIMIT(paused_at_state=BUMPED) would let the next
    checkpoint continue "from BUMPED" with no decision."""
    with pytest.raises(StoreError, match="needs a recorded approval"):
        store.checkpoint(
            state(2, RunStatus.PAUSED_LIMIT, paused_at_state=RunStatus.BUMPED), lease=awaiting
        )
    store.checkpoint(
        state(2, RunStatus.PAUSED_LIMIT, paused_at_state=RunStatus.AWAITING_APPROVAL),
        lease=awaiting,
    )
    with pytest.raises(StoreError, match="needs a recorded approval"):
        store.checkpoint(state(3, RunStatus.BUMPED), lease=awaiting)


@pytest.mark.parametrize("ending", [RunStatus.END_SUCCESS, RunStatus.END_ESCALATED])
def test_no_ending_skips_the_decision(store: RunStore, awaiting: Lease, ending: RunStatus) -> None:
    with pytest.raises(StoreError, match="needs a recorded approval"):
        store.checkpoint(state(2, ending), lease=awaiting)


def test_a_stored_plan_approval_must_match_the_recorded_decision(
    store: RunStore, awaiting: Lease
) -> None:
    with pytest.raises(StoreError, match="without a recorded approve"):
        store.put(sample(d.Plan, run_id=RUN, approved=True), lease=awaiting)
    store.put(sample(d.Plan, run_id=RUN, approved=None), lease=awaiting)
    store.request_plan_decision(RUN, approve=True)
    with pytest.raises(StoreError, match="without a recorded reject"):
        store.put(sample(d.Plan, run_id=RUN, approved=False), lease=awaiting)
    store.put(sample(d.Plan, run_id=RUN, approved=True), lease=awaiting)
    assert store.rows(RUN, d.Plan)[0].approved is True


def test_a_cancel_beats_a_pending_approval(store: RunStore, awaiting: Lease) -> None:
    store.request_plan_decision(RUN, approve=True)
    store.request_cancel(RUN)
    with pytest.raises(CancelRequestedError):
        store.checkpoint(state(2, RunStatus.BUMPED), lease=awaiting)
    store.checkpoint(state(2, RunStatus.END_CANCELLED), lease=awaiting)


# ---------------------------------------------------------------- two processes (SQLite)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "shared.db"
    s = SqliteRunStore.open(path)
    s.create_run(run())
    s.close()
    return path


def test_a_cancel_from_another_process_reaches_a_live_driver(db: Path) -> None:
    """The driver checkpoints in a loop (after every tool call); `mpilot cancel` in another
    process records the request; the driver's next checkpoint refuses, it tears down and
    checkpoints END_CANCELLED, which the canceller sees."""
    driver, canceller = SqliteRunStore.open(db), SqliteRunStore.open(db)
    started, done = threading.Event(), threading.Event()
    outcome: list[str] = []

    def drive() -> None:
        held = driver.acquire_lease(RUN, "driver", ttl_s=60)
        seq = 0
        try:
            while seq < 10_000:
                seq += 1
                try:
                    driver.checkpoint(state(seq), lease=held)
                except CancelRequestedError:
                    driver.checkpoint(
                        state(seq, RunStatus.END_CANCELLED, terminal_reason="cancelled"),
                        lease=held,
                    )
                    outcome.append("cancelled")
                    return
                started.set()
                SystemClock().sleep(0.001)
            outcome.append("never saw the cancel")
        finally:
            done.set()

    worker = threading.Thread(target=drive)
    try:
        worker.start()
        assert started.wait(10)
        live = canceller.current_lease(RUN)
        assert live is not None
        assert live.live(datetime.now(UTC))  # a live driver: the canceller must not take over
        canceller.request_cancel(RUN, reason="user")
        assert done.wait(30)
        worker.join(5)
        assert outcome == ["cancelled"]
        assert canceller.get_run(RUN).status is RunStatus.END_CANCELLED
        assert canceller.control(RUN).cancel_reason == "user"
    finally:
        driver.close()
        canceller.close()


def test_a_cancel_reaches_a_driver_waiting_for_approval_through_its_heartbeat(db: Path) -> None:
    """Review B4: `mpilot run --approve-plan` blocks in AWAITING_APPROVAL and checkpoints
    nothing; its heartbeat (`renew_lease`) carries the cancel to it, so `mpilot cancel` does
    not hang until someone approves."""
    driver, canceller = SqliteRunStore.open(db), SqliteRunStore.open(db)
    waiting, done = threading.Event(), threading.Event()
    outcome: list[str] = []

    def wait_for_approval() -> None:
        held = driver.acquire_lease(RUN, "mpilot run --approve-plan", ttl_s=60)
        try:
            driver.checkpoint(state(1, RunStatus.AWAITING_APPROVAL), lease=held)
            waiting.set()
            for _ in range(10_000):
                control = driver.renew_lease(held, ttl_s=60).control
                if control.cancel_requested:
                    driver.checkpoint(state(2, RunStatus.END_CANCELLED), lease=held)
                    outcome.append("cancelled")
                    return
                if control.plan_decision is not None:
                    outcome.append("decided")
                    return
                SystemClock().sleep(0.001)
            outcome.append("never saw the cancel")
        finally:
            done.set()

    worker = threading.Thread(target=wait_for_approval)
    try:
        worker.start()
        assert waiting.wait(10)
        canceller.request_cancel(RUN)
        assert done.wait(30)
        worker.join(5)
        assert outcome == ["cancelled"]
        assert canceller.get_run(RUN).status is RunStatus.END_CANCELLED
    finally:
        driver.close()
        canceller.close()


def test_two_resumes_can_never_both_drive_a_run(db: Path) -> None:
    stores = [SqliteRunStore.open(db) for _ in range(6)]
    barrier = threading.Barrier(len(stores))
    won: list[Lease] = []
    refused: list[Exception] = []

    def resume(store: SqliteRunStore, i: int) -> None:
        barrier.wait()
        try:
            won.append(store.acquire_lease(RUN, f"resume-{i}", ttl_s=60))
        except LeaseHeldError as exc:
            refused.append(exc)

    threads = [threading.Thread(target=resume, args=(s, i)) for i, s in enumerate(stores)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert len(won) == 1
        assert len(refused) == len(stores) - 1
        assert stores[0].current_lease(RUN) == won[0]
    finally:
        for s in stores:
            s.close()


def test_no_end_cancelled_is_lost_to_a_stalled_driver(db: Path) -> None:
    """Driver A stalls past its lease; `mpilot cancel` finds no live driver, takes over and
    ends the run; A wakes up and tries to carry on: every write it makes is refused, and the
    run stays END_CANCELLED."""
    clock = FakeClock(start=T0)
    a, b = SqliteRunStore.open(db, clock), SqliteRunStore.open(db, clock)
    try:
        stalled = a.acquire_lease(RUN, "driver-a", ttl_s=TTL)
        a.checkpoint(state(1), lease=stalled)
        b.request_cancel(RUN)
        with pytest.raises(LeaseHeldError):  # A is live: the canceller waits
            b.acquire_lease(RUN, "mpilot cancel", ttl_s=TTL)
        clock.advance(TTL + 1)  # A hangs (GC pause, frozen container, ...)
        canceller = b.acquire_lease(RUN, "mpilot cancel", ttl_s=TTL)
        b.checkpoint(state(2, RunStatus.END_CANCELLED), lease=canceller)

        for late in (
            lambda: a.checkpoint(state(3), lease=stalled),
            lambda: a.checkpoint(state(3, RunStatus.END_SUCCESS), lease=stalled),
            lambda: a.save_run(run(), lease=stalled),
            lambda: a.append(tool_call("tc-late"), lease=stalled),
            lambda: a.renew_lease(stalled, ttl_s=TTL),
        ):
            with pytest.raises(LeaseLostError):
                late()
        a.release_lease(stalled)  # harmless
        assert b.get_run(RUN).status is RunStatus.END_CANCELLED
        assert b.latest_checkpoint(RUN) == state(2, RunStatus.END_CANCELLED)
        assert b.rows(RUN, d.ToolCall) == []
    finally:
        a.close()
        b.close()


def test_a_crashed_driver_meets_the_cancel_when_it_resumes(db: Path) -> None:
    """The cancel lands while no process runs the run (the driver crashed holding a lease
    that has since expired); the resume sees the request before doing anything."""
    clock = FakeClock(start=T0)
    s = SqliteRunStore.open(db, clock)
    try:
        crashed = s.acquire_lease(RUN, "driver-a", ttl_s=TTL)
        s.checkpoint(state(1), lease=crashed)
        s.request_cancel(RUN)
        clock.advance(TTL)
        resumed = s.acquire_lease(RUN, "mpilot resume", ttl_s=TTL)
        assert s.control(RUN).cancel_requested  # checked right after taking the lease
        with pytest.raises(CancelRequestedError):
            s.checkpoint(state(2), lease=resumed)
        s.checkpoint(state(2, RunStatus.END_CANCELLED), lease=resumed)
        assert s.get_run(RUN).status is RunStatus.END_CANCELLED
    finally:
        s.close()
