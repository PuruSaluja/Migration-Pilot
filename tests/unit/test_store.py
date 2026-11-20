"""P1-10: the SQLite `RunStore`.

The first half is one contract, run against the in-memory double and the real store, so the
double stays honest. The second half is what only the real store has: WAL, transactions,
constraints, persistence across reopen, and concurrent writers.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.util.exc import CommandError
from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.orm import Session
from test_domain_models import NOW, RUN, SAMPLES

from mpilot.agents.router import RoutingCounters, StickyState
from mpilot.core.clock import FakeClock
from mpilot.core.errors import StoreError
from mpilot.core.types import (
    AttemptId,
    FailureCategory,
    LLMCallId,
    RunId,
    RunStatus,
    StepId,
    ToolCallId,
)
from mpilot.models import artifacts as art
from mpilot.models import domain as d
from mpilot.models.coordination import Lease, run_following
from mpilot.models.migrate import head_revision
from mpilot.models.state import BudgetState, Checkpoint, FailureHistoryEntry, InFlightCall
from mpilot.models.store import SqliteRunStore, sqlite_engine
from mpilot.models.tables import (
    DbAttempt,
    DbCheckpoint,
    DbCriticVerdict,
    DbEscalationReport,
    DbFinalVerification,
    DbIntegrityReport,
    DbRun,
    DbToolCall,
)
from mpilot.orchestrator.base import RunStore
from mpilot.testing.infra_doubles import MemoryStore

# ---------------------------------------------------------------- builders


def sample[M: d.DomainModel](model: type[M], **update: Any) -> M:
    made = SAMPLES[model]()
    assert isinstance(made, model)
    return made.model_copy(update=update) if update else made


def lease_for(store: Any, run_id: RunId = RUN) -> Lease:
    """The test's one driver lease on `run_id`: taken on first use, then reused (a store
    never hands out a live lease twice)."""
    current: Lease | None = store.current_lease(run_id)
    if current is not None and current.holder == "test-driver" and current.released_at is None:
        return current
    lease: Lease = store.acquire_lease(run_id, "test-driver", ttl_s=3600)
    return lease


def run(**update: Any) -> d.Run:
    base: dict[str, Any] = {"status": RunStatus.REPAIR_LOOP, "paused_at_state": None}
    return sample(d.Run, **{**base, **update})


def checkpoint(seq: int, **update: Any) -> Checkpoint:
    base: dict[str, Any] = {
        "run_id": RUN,
        "seq": seq,
        "at": NOW + timedelta(seconds=seq),
        "status": RunStatus.REPAIR_LOOP,
        "step_id": StepId("s1"),
        "attempt_n": 2,
        "attempt_id": AttemptId("att-2"),
        "sticky": {"step/s1": StickyState(tier_escalated=True, trigger_fired=True)},
        "routing": {
            "step/s1": RoutingCounters(
                policy_denials=1,
                last_confidence=0.6,
                last_confidence_call=LLMCallId("llm-2"),
                confidence_calls=frozenset({"llm-2"}),
                explorer_questions=frozenset({"b" * 64}),
            )
        },
        "completed_calls": (LLMCallId("llm-2"),),
        "budget": BudgetState(
            spent=d.Spent(turns=10, input_tokens=1000),
            active_seconds=50.0,
            attempts_by_step={StepId("s1"): 2},
            attempts_total=2,
        ),
        "failure_history": (
            FailureHistoryEntry(signature="sig-a", attempts=2, hypotheses_tried=("h1",)),
        ),
        "last_tool_call_id": ToolCallId("tc-9"),
        "in_flight": InFlightCall(
            role_call_id=LLMCallId("llm-3"), role="editor", tool_calls_done=(ToolCallId("tc-9"),)
        ),
        "integrity_failures": 1,
        "critic_revisions": 1,
    }
    return Checkpoint(**{**base, **update})


def final_verification(commit: str = "b" * 40, **update: Any) -> art.FinalVerification:
    base: dict[str, Any] = {
        "mirror_commit": commit,
        "verify": d.VerifyResult(
            stages=[
                d.VerifyStageResult(stage=stage, passed=True)
                for stage in (d.VerifyStage.INSTALL, d.VerifyStage.TESTS)
            ],
            tests=d.TestResult(
                run_id=RUN,
                stage=d.TestStage.FINAL,
                passed=3,
                failed=0,
                skipped=1,
                errors=0,
                duration_s=1.0,
                passed_ids=frozenset({"t::a", "t::b", "t::c"}),
            ),
        ),
        "runner_exit_code": 0,
        "runner_summary": art.TestCounts(passed=3, failed=0, skipped=1, errors=0),
        "resolved_versions": {"react": "18.3.1"},
        "coverage_pct": 80.0,
        "cache": "fresh",
    }
    return art.FinalVerification(**{**base, **update})


def escalation(**update: Any) -> art.EscalationReport:
    base: dict[str, Any] = {
        "run_id": RUN,
        "category": FailureCategory.NO_PROGRESS,
        "report_md": "# Escalation\n\nthree identical failure sets",
        "data": {"failing_tests": ["t::a"], "hypotheses": ["h1", "h2"], "budget": {"turns": 12}},
        "created_at": NOW,
    }
    return art.EscalationReport(**{**base, **update})


def note(key: str = "convention", text: str = "tabs") -> art.MigrationNote:
    return art.MigrationNote(run_id=RUN, key=key, text=text)


def tool_call(id: str) -> d.ToolCall:
    return sample(d.ToolCall, id=ToolCallId(id))


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def sqlite_store(tmp_path: Path, clock: FakeClock) -> Iterator[SqliteRunStore]:
    store = SqliteRunStore.open(tmp_path / "state" / "mpilot.db", clock)
    yield store
    store.close()


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path, clock: FakeClock) -> Iterator[RunStore]:
    """The same contract, on the double and on the real thing."""
    if request.param == "memory":
        yield MemoryStore()
        return
    real = SqliteRunStore.open(tmp_path / "mpilot.db", clock)
    yield real
    real.close()


# ================================================================= the contract


def test_both_implement_the_protocol(sqlite_store: SqliteRunStore) -> None:
    assert isinstance(sqlite_store, RunStore)
    assert isinstance(MemoryStore(), RunStore)


def test_run_lifecycle(store: RunStore) -> None:
    with pytest.raises(KeyError):
        store.get_run(RUN)
    with pytest.raises(KeyError):
        store.save_run(run(), lease=lease_for(store))
    store.create_run(run())
    assert store.get_run(RUN) == run()
    with pytest.raises(StoreError, match=r"exist|UNIQUE|PRIMARY"):
        store.create_run(run())
    updated = run(branch="mpilot/react-18-1", base_sha="c" * 40)
    store.save_run(updated, lease=lease_for(store))
    assert store.get_run(RUN) == updated


def test_runs_are_listed_newest_first(store: RunStore) -> None:
    ids = [RunId("run-a"), RunId("run-b"), RunId("run-c")]
    for i, run_id in enumerate(ids):
        store.create_run(run(id=run_id, created_at=NOW + timedelta(hours=i)))
    assert [r.id for r in store.list_runs()] == list(reversed(ids))


def test_every_appended_record_type_comes_back_equal(store: RunStore) -> None:
    store.create_run(run())
    decision = sample(d.RoutingDecision)
    records: list[Any] = [
        tool_call("tc-1"),
        decision,
        sample(d.LLMCall),  # links to the decision above
        sample(d.TestResult),
        sample(d.Attempt),
        sample(d.IntegrityReport),
        sample(d.CriticVerdict),
        note(),
    ]
    for record in records:
        store.append(record, lease=lease_for(store))
    for record in records:
        assert store.rows(RUN, type(record)) == [record], type(record).__name__


def test_appended_rows_keep_insertion_order(store: RunStore) -> None:
    store.create_run(run())
    results = [sample(d.TestResult, passed=n) for n in (5, 1, 3)]
    for result in results:
        store.append(result, lease=lease_for(store))
    assert store.rows(RUN, d.TestResult) == results
    calls = [tool_call(f"tc-{n}") for n in (3, 1, 2)]
    for call in calls:
        store.append(call, lease=lease_for(store))
    assert store.rows(RUN, d.ToolCall) == calls


def test_single_records_are_replaced_not_duplicated(store: RunStore) -> None:
    store.create_run(run())
    first: list[Any] = [
        sample(d.Baseline),
        sample(d.Plan, approved=None),
        sample(d.PullRequest),
        escalation(),
    ]
    for record in first:
        store.put(record, lease=lease_for(store))
        assert store.rows(RUN, type(record)) == [record]
    with pytest.raises(StoreError, match="already has its baseline"):  # P0 is fixed for the run
        store.put(sample(d.Baseline, test_count=99), lease=lease_for(store))
    assert store.rows(RUN, d.Baseline) == [first[0]]
    second: list[Any] = [
        sample(d.Plan, approved=None, risks=["peer deps"]),  # approved needs CP-1 (P1-12c)
        sample(d.PullRequest, number=2, url="https://github.com/o/r/pull/2"),
        escalation(category=FailureCategory.BUDGET_EXHAUSTED, report_md="# again"),
    ]
    for record in second:
        store.put(record, lease=lease_for(store))
        assert store.rows(RUN, type(record)) == [record]


def test_a_run_with_nothing_recorded_has_no_rows(store: RunStore) -> None:
    store.create_run(run())
    assert store.rows(RUN, d.ToolCall) == []
    assert store.rows(RUN, d.Plan) == []
    assert store.rows(RunId("no-such-run"), d.TestResult) == []
    assert store.latest_checkpoint(RUN) is None
    assert store.final_verifications(RUN) == []


def test_rows_are_per_run(store: RunStore) -> None:
    other = RunId("run-other")
    store.create_run(run())
    store.create_run(run(id=other))
    store.append(sample(d.TestResult), lease=lease_for(store))
    store.append(sample(d.TestResult, run_id=other, passed=7), lease=lease_for(store, other))
    assert [r.passed for r in store.rows(other, d.TestResult)] == [7]
    assert len(store.rows(RUN, d.TestResult)) == 1


def test_every_write_to_a_note_key_is_kept(store: RunStore) -> None:
    store.create_run(run())
    notes = [
        note("convention", "tabs"),
        note("flaky", "t::x is slow"),
        note("convention", "spaces"),
    ]
    for n in notes:
        store.append(n, lease=lease_for(store))
    assert store.rows(RUN, art.MigrationNote) == notes


def test_final_verifications_are_kept_oldest_first(store: RunStore) -> None:
    store.create_run(run())
    first, second = final_verification("a" * 40), final_verification("b" * 40, coverage_pct=None)
    store.append_final_verification(RUN, first, lease=lease_for(store))
    store.append_final_verification(RUN, second, lease=lease_for(store))
    assert store.final_verifications(RUN) == [first, second]


def test_checkpoints_move_forward_only(store: RunStore) -> None:
    store.create_run(run())
    store.checkpoint(checkpoint(5), lease=lease_for(store))
    assert store.latest_checkpoint(RUN) == checkpoint(5)
    forward = checkpoint(6)
    store.checkpoint(forward, lease=lease_for(store))
    assert store.latest_checkpoint(RUN) == forward


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"seq": 6}, "does not follow"),
        ({"sticky": {}}, "sticky state"),
        ({"sticky": {"step/s1": StickyState(tier_escalated=True)}}, "sticky state"),
        ({"budget": BudgetState()}, "lowered"),
        ({"failure_history": ()}, "failure history"),
        ({"integrity_failures": 0}, "integrity_failures"),
        ({"critic_revisions": 0}, "critic_revisions"),
        ({"routing": {"step/s1": RoutingCounters()}}, "routing counters"),
        ({"attempt_id": AttemptId("att-other")}, "changed its id"),
        ({"completed_calls": ()}, "completed role call"),
    ],
)
def test_a_checkpoint_that_undoes_anything_is_refused_and_changes_nothing(
    store: RunStore, change: dict[str, Any], fragment: str
) -> None:
    store.create_run(run())
    store.checkpoint(checkpoint(6), lease=lease_for(store))
    with pytest.raises(StoreError, match=fragment):
        store.checkpoint(checkpoint(7).model_copy(update=change), lease=lease_for(store))
    assert store.latest_checkpoint(RUN) == checkpoint(6)
    store.checkpoint(checkpoint(7), lease=lease_for(store))  # the refusal did not use up seq 7


def test_evals_are_listed_by_suite_in_order(store: RunStore) -> None:
    rows = [
        sample(d.EvalResult, suite="v1", task_id="t1"),
        sample(d.EvalResult, suite="other", task_id="t9"),
        sample(d.EvalResult, suite="v1", task_id="t2"),
    ]
    for r in rows:
        store.add_eval(r)
    assert [r.task_id for r in store.evals("v1")] == ["t1", "t2"]
    assert store.evals("nothing") == []


# ================================================================= the real store only


def _pragma(engine: Engine, name: str) -> Any:
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"PRAGMA {name}").scalar_one()


def test_the_database_is_in_wal_mode_with_the_expected_pragmas(
    sqlite_store: SqliteRunStore,
) -> None:
    engine = sqlite_store.engine
    assert _pragma(engine, "journal_mode") == "wal"
    assert _pragma(engine, "foreign_keys") == 1
    assert _pragma(engine, "synchronous") == 1  # NORMAL
    assert _pragma(engine, "busy_timeout") >= 5_000


def test_wal_is_recorded_in_the_file_itself(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    SqliteRunStore.open(path).close()
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        raw.close()


def test_open_creates_the_folder_and_migrates_to_head(tmp_path: Path) -> None:
    path = tmp_path / "does" / "not" / "exist" / "mpilot.db"
    store = SqliteRunStore.open(path)
    try:
        assert path.is_file()
        with store.engine.connect() as conn:
            assert conn.execute(text("select version_num from alembic_version")).scalar_one() == (
                head_revision()
            )
    finally:
        store.close()


def test_opening_twice_is_harmless(tmp_path: Path) -> None:
    path = tmp_path / "mpilot.db"
    first = SqliteRunStore.open(path)
    first.create_run(run())
    second = SqliteRunStore.open(path)
    try:
        assert second.get_run(RUN) == run()
    finally:
        first.close()
        second.close()


def test_a_database_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mpilot.db"
    SqliteRunStore.open(path).close()
    raw = sqlite3.connect(path)
    raw.execute("UPDATE alembic_version SET version_num = '9999'")
    raw.commit()
    raw.close()
    with pytest.raises(CommandError, match="9999"):
        SqliteRunStore.open(path)


def test_everything_survives_closing_and_reopening(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "mpilot.db"
    first = SqliteRunStore.open(path, clock)
    first.create_run(run())
    first.append(sample(d.RoutingDecision), lease=lease_for(first))
    first.append(sample(d.LLMCall), lease=lease_for(first))
    first.put(sample(d.Plan, approved=None), lease=lease_for(first))
    first.checkpoint(checkpoint(1), lease=lease_for(first))
    first.checkpoint(checkpoint(2), lease=lease_for(first))
    first.append_final_verification(RUN, final_verification(), lease=lease_for(first))
    first.put(escalation(), lease=lease_for(first))
    first.append(note(), lease=lease_for(first))
    first.close()

    again = SqliteRunStore.open(path, clock)
    try:
        assert again.get_run(RUN) == run_following(run(), checkpoint(2))
        assert again.rows(RUN, d.LLMCall) == [sample(d.LLMCall)]
        assert again.rows(RUN, d.Plan) == [sample(d.Plan, approved=None)]
        assert again.latest_checkpoint(RUN) == checkpoint(2)
        assert again.final_verifications(RUN) == [final_verification()]
        assert again.rows(RUN, art.EscalationReport) == [escalation()]
        assert again.rows(RUN, art.MigrationNote) == [note()]
        # A resume must not be able to undo what was checkpointed before the restart (B5).
        with pytest.raises(StoreError, match="sticky"):
            again.checkpoint(checkpoint(3, sticky={}), lease=lease_for(again))
    finally:
        again.close()


# -- constraints and transactions


def test_rows_for_a_run_that_does_not_exist_are_refused(sqlite_store: SqliteRunStore) -> None:
    made_up = Lease(
        run_id=RUN, holder="x", token=1, acquired_at=NOW, expires_at=NOW + timedelta(hours=1)
    )
    with pytest.raises(KeyError):
        lease_for(sqlite_store)
    with pytest.raises(StoreError):
        sqlite_store.append(tool_call("tc-1"), lease=made_up)
    with pytest.raises(StoreError):
        sqlite_store.put(sample(d.Plan), lease=made_up)
    with pytest.raises(StoreError):
        sqlite_store.checkpoint(checkpoint(1), lease=made_up)
    with pytest.raises(StoreError):
        sqlite_store.append_final_verification(RUN, final_verification(), lease=made_up)


def test_an_llm_call_needs_its_routing_decision_stored_first(
    sqlite_store: SqliteRunStore,
) -> None:
    sqlite_store.create_run(run())
    with pytest.raises(StoreError):
        sqlite_store.append(sample(d.LLMCall), lease=lease_for(sqlite_store))
    assert sqlite_store.rows(RUN, d.LLMCall) == []
    sqlite_store.append(sample(d.RoutingDecision), lease=lease_for(sqlite_store))
    sqlite_store.append(sample(d.LLMCall), lease=lease_for(sqlite_store))


def test_duplicate_ids_are_refused_and_leave_no_partial_row(sqlite_store: SqliteRunStore) -> None:
    sqlite_store.create_run(run())
    sqlite_store.append(tool_call("tc-1"), lease=lease_for(sqlite_store))
    with pytest.raises(StoreError):
        sqlite_store.append(tool_call("tc-1"), lease=lease_for(sqlite_store))
    assert len(sqlite_store.rows(RUN, d.ToolCall)) == 1


def test_a_checkpoint_for_a_used_sequence_number_is_refused(sqlite_store: SqliteRunStore) -> None:
    sqlite_store.create_run(run())
    sqlite_store.checkpoint(checkpoint(1), lease=lease_for(sqlite_store))
    with pytest.raises(StoreError, match="does not follow"):
        sqlite_store.checkpoint(checkpoint(1), lease=lease_for(sqlite_store))


def test_asking_for_a_type_that_is_not_stored_is_a_programming_error(
    sqlite_store: SqliteRunStore,
) -> None:
    with pytest.raises(TypeError, match="Checkpoint"):
        sqlite_store.rows(RUN, Checkpoint)


def test_deleting_a_run_is_not_part_of_the_api_but_cascades_if_done(
    sqlite_store: SqliteRunStore,
) -> None:
    sqlite_store.create_run(run())
    sqlite_store.append(tool_call("tc-1"), lease=lease_for(sqlite_store))
    sqlite_store.checkpoint(checkpoint(1), lease=lease_for(sqlite_store))
    with Session(sqlite_store.engine) as session, session.begin():
        session.connection().exec_driver_sql("DELETE FROM runs")
    assert sqlite_store.rows(RUN, d.ToolCall) == []
    assert sqlite_store.latest_checkpoint(RUN) is None


# -- columns stay in step with the stored model


def test_save_run_updates_the_queryable_columns_as_well_as_the_document(
    sqlite_store: SqliteRunStore,
) -> None:
    sqlite_store.create_run(run())
    sqlite_store.save_run(run(branch="mpilot/x-1"), lease=lease_for(sqlite_store))
    sqlite_store.checkpoint(
        checkpoint(
            1,
            status=RunStatus.END_ERROR,
            terminal_reason="boom",
            attempt_n=None,
            attempt_id=None,
        ),
        lease=lease_for(sqlite_store),
    )
    with Session(sqlite_store.engine) as session:
        row = session.get_one(DbRun, RUN)
        assert (row.status, row.terminal_reason, row.branch) == ("END_ERROR", "boom", "mpilot/x-1")
        assert row.doc["status"] == "END_ERROR"


def test_a_paused_run_keeps_its_paused_state_in_the_column(sqlite_store: SqliteRunStore) -> None:
    paused = sample(d.Run)  # the sample is a paused run
    assert paused.status is RunStatus.PAUSED_LIMIT
    sqlite_store.create_run(paused)
    with Session(sqlite_store.engine) as session:
        assert session.get_one(DbRun, RUN).paused_at_state == paused.paused_at_state


def test_an_attempts_signature_and_category_are_copied_from_its_diagnosis(
    sqlite_store: SqliteRunStore,
) -> None:
    attempt = sample(d.Attempt)
    assert attempt.diagnosis is not None
    sqlite_store.create_run(run())
    sqlite_store.append(attempt, lease=lease_for(sqlite_store))
    sqlite_store.append(
        attempt.model_copy(
            update={
                "id": "attempt-2",
                "n": attempt.n + 1,
                "n_in_step": attempt.n_in_step + 1,
                "diagnosis": None,
            }
        ),
        lease=lease_for(sqlite_store),
    )
    with Session(sqlite_store.engine) as session:
        rows = list(session.scalars(select(DbAttempt).order_by(DbAttempt.seq)))
    assert (rows[0].signature, rows[0].category) == (
        attempt.diagnosis.signature,
        attempt.diagnosis.category.value,
    )
    assert (rows[1].signature, rows[1].category) == (None, None)


def test_checkpoint_columns_match_the_checkpoint(sqlite_store: SqliteRunStore) -> None:
    sqlite_store.create_run(run())
    sqlite_store.checkpoint(checkpoint(4), lease=lease_for(sqlite_store))
    sqlite_store.checkpoint(
        checkpoint(
            5,
            status=RunStatus.PAUSED_LIMIT,
            paused_at_state=RunStatus.REPAIR_LOOP,
            export_dir="/runs/x/export",
            export_digest="d" * 64,
        ),
        lease=lease_for(sqlite_store),
    )
    with Session(sqlite_store.engine) as session:
        row = session.get_one(DbCheckpoint, (RUN, 5))
    assert (row.status, row.paused_at_state, row.step_id, row.attempt_n) == (
        "PAUSED_LIMIT",
        "REPAIR_LOOP",
        "s1",
        2,
    )
    assert row.at == NOW + timedelta(seconds=5)


def test_escalation_report_columns_hold_the_whole_report(sqlite_store: SqliteRunStore) -> None:
    sqlite_store.create_run(run())
    sqlite_store.put(escalation(), lease=lease_for(sqlite_store))
    with Session(sqlite_store.engine) as session:
        row = session.get_one(DbEscalationReport, RUN)
    assert row.category == "NO_PROGRESS"
    assert row.data["hypotheses"] == ["h1", "h2"]
    assert row.report_md.startswith("# Escalation")


def test_final_verification_columns(sqlite_store: SqliteRunStore) -> None:
    sqlite_store.create_run(run())
    sqlite_store.append_final_verification(
        RUN, final_verification("c" * 40, coverage_pct=71.5), lease=lease_for(sqlite_store)
    )
    with Session(sqlite_store.engine) as session:
        row = session.scalars(select(DbFinalVerification)).one()
    assert (row.mirror_commit, row.cache, row.runner_exit_code, row.coverage_pct) == (
        "c" * 40,
        "fresh",
        0,
        71.5,
    )


def test_recorded_at_comes_from_the_injected_clock(
    sqlite_store: SqliteRunStore, clock: FakeClock
) -> None:
    sqlite_store.create_run(run())
    sqlite_store.append(tool_call("tc-1"), lease=lease_for(sqlite_store))
    clock.advance(300)
    sqlite_store.append(tool_call("tc-2"), lease=lease_for(sqlite_store))
    with Session(sqlite_store.engine) as session:
        stamps = list(session.scalars(select(DbToolCall.recorded_at).order_by(DbToolCall.seq)))
    assert stamps == [NOW, NOW + timedelta(minutes=5)]


# -- transactions and concurrency


def _statements(engine: Engine) -> list[str]:
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    return seen


def test_writes_begin_immediate_and_reads_begin_deferred(sqlite_store: SqliteRunStore) -> None:
    seen = _statements(sqlite_store.engine)
    sqlite_store.create_run(run())
    assert seen[0] == "BEGIN IMMEDIATE"
    seen.clear()
    sqlite_store.get_run(RUN)
    sqlite_store.list_runs()
    assert seen[0] == "BEGIN"
    assert "BEGIN IMMEDIATE" not in seen


def test_a_checkpoint_holds_the_write_lock_from_its_first_read(
    sqlite_store: SqliteRunStore,
) -> None:
    sqlite_store.create_run(run())
    lease = lease_for(sqlite_store)
    seen = _statements(sqlite_store.engine)
    sqlite_store.checkpoint(checkpoint(1), lease=lease)
    assert seen[0] == "BEGIN IMMEDIATE"
    assert seen[1].lstrip().upper().startswith("SELECT")  # the fence and covers() reads, locked


def test_a_second_writer_waits_for_the_first_instead_of_failing(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "mpilot.db"
    a = SqliteRunStore.open(path, clock)
    b = SqliteRunStore(sqlite_engine(path), clock)
    a.create_run(run())
    holding, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def hold_the_write_lock() -> None:
        with a._write() as session:
            session.add(DbToolCall(**_tool_row("tc-held")))
            session.flush()  # the write lock is taken here, at the first statement
            holding.set()
            release.wait(10)

    def write_from_the_other_store() -> None:
        try:
            b.append(tool_call("tc-waiting"), lease=lease_for(b))
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    holder = threading.Thread(target=hold_the_write_lock)
    holder.start()
    assert holding.wait(10)
    writer = threading.Thread(target=write_from_the_other_store)
    writer.start()
    assert not finished.wait(0.5), "the second writer should be waiting on the lock"
    release.set()
    holder.join(10)
    writer.join(10)
    try:
        assert errors == []
        assert [c.id for c in a.rows(RUN, d.ToolCall)] == ["tc-held", "tc-waiting"]
    finally:
        a.close()
        b.close()


def _tool_row(id: str) -> dict[str, Any]:
    m = tool_call(id)
    return {
        "run_id": m.run_id,
        "recorded_at": NOW,
        "id": m.id,
        "role_call_id": m.role_call_id,
        "attempt_id": m.attempt_id,
        "role": m.role.value,
        "tool": m.tool,
        "duration_ms": m.duration_ms,
        "policy_decision": m.policy_decision.value,
        "deny_reason": m.deny_reason,
        "doc": m.model_dump(mode="json"),
    }


def test_readers_are_not_blocked_by_a_writer_in_wal_mode(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "mpilot.db"
    writer = SqliteRunStore.open(path, clock)
    reader = SqliteRunStore(sqlite_engine(path), clock)
    writer.create_run(run())
    holding, release = threading.Event(), threading.Event()
    seen: list[d.Run] = []

    def hold() -> None:
        with writer._write() as session:
            session.add(DbToolCall(**_tool_row("tc-held")))
            session.flush()  # the write lock is taken here, at the first statement
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(10)
    try:
        t = threading.Thread(target=lambda: seen.append(reader.get_run(RUN)))
        t.start()
        t.join(5)
        assert not t.is_alive(), "a read must not wait for the writer"
        assert seen == [run()]
        assert reader.rows(RUN, d.ToolCall) == []  # the uncommitted row is invisible
    finally:
        release.set()
        holder.join(10)
        writer.close()
        reader.close()


def test_concurrent_checkpoints_from_two_stores_never_lose_or_reorder(
    tmp_path: Path, clock: FakeClock
) -> None:
    """Two writers hammer one run with strictly increasing checkpoints; every checkpoint is
    either stored or refused as stale, and the latest is always the highest one accepted."""
    path = tmp_path / "mpilot.db"
    a = SqliteRunStore.open(path, clock)
    b = SqliteRunStore(sqlite_engine(path), clock)
    a.create_run(run())
    accepted: list[int] = []
    crashed: list[BaseException] = []
    lock = threading.Lock()

    def writer(store: SqliteRunStore, seqs: range) -> None:
        for seq in seqs:
            try:
                store.checkpoint(checkpoint(seq), lease=lease_for(store))
            except StoreError:
                continue  # a stale checkpoint: refused, as it should be
            except BaseException as exc:
                with lock:
                    crashed.append(exc)
                return
            with lock:
                accepted.append(seq)

    threads = [
        threading.Thread(target=writer, args=(a, range(1, 41))),
        threading.Thread(target=writer, args=(b, range(1, 41))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    try:
        assert crashed == []
        latest = a.latest_checkpoint(RUN)
        assert latest is not None
        assert latest.seq == max(accepted) == 40
        with Session(a.engine) as session:
            stored = list(session.scalars(select(DbCheckpoint.seq).order_by(DbCheckpoint.seq)))
        assert stored == sorted(set(accepted))  # each accepted seq stored exactly once
        assert len(accepted) == len(set(accepted))
    finally:
        a.close()
        b.close()


def test_the_store_works_on_any_engine_given_to_the_constructor(tmp_path: Path) -> None:
    from mpilot.models.migrate import upgrade

    url = f"sqlite:///{tmp_path / 'plain.db'}"
    upgrade(url)
    engine = create_engine(url)  # no WAL pragma, no explicit transactions: the plain default
    store = SqliteRunStore(engine)
    try:
        store.create_run(run())
        assert store.get_run(RUN) == run()
    finally:
        store.close()


def test_a_second_attempt_with_the_same_run_wide_number_is_refused(
    sqlite_store: SqliteRunStore,
) -> None:
    """`Attempt.n` names `refs/mpilot/attempt/<n>`: two attempts with one number would make
    revert-to-best restore the wrong tree (P1-12 review, B2)."""
    sqlite_store.create_run(run())
    first = sample(d.Attempt)
    sqlite_store.append(first, lease=lease_for(sqlite_store))
    with pytest.raises(StoreError, match="UNIQUE"):
        sqlite_store.append(
            first.model_copy(update={"id": "attempt-other", "step_id": "s2"}),
            lease=lease_for(sqlite_store),
        )
    assert sqlite_store.rows(RUN, d.Attempt) == [first]


def test_reports_and_verdicts_keep_the_commit_they_judged(sqlite_store: SqliteRunStore) -> None:
    sqlite_store.create_run(run())
    report, verdict = sample(d.IntegrityReport), sample(d.CriticVerdict)
    sqlite_store.append(report, lease=lease_for(sqlite_store))
    sqlite_store.append(verdict, lease=lease_for(sqlite_store))
    assert sqlite_store.rows(RUN, d.IntegrityReport)[0].mirror_commit == report.mirror_commit
    assert sqlite_store.rows(RUN, d.CriticVerdict)[0].mirror_commit == verdict.mirror_commit
    with Session(sqlite_store.engine) as session:
        assert session.scalar(select(DbIntegrityReport.mirror_commit)) == report.mirror_commit
        assert session.scalar(select(DbCriticVerdict.mirror_commit)) == verdict.mirror_commit
