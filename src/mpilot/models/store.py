"""`RunStore` on SQLite in WAL mode (P1-10, SPEC §10.1, NFR-4).

State lives here and nowhere else: every checkpoint, tool call, model call and verification
result is written as it happens, so a crash or a usage-limit pause loses nothing.

How it stores things is fixed by `models/tables.py`: the whole Pydantic model as JSON in `doc`,
plus columns copied from it. A read returns the model rebuilt from `doc` (or from the columns,
for the two records that have no `doc`). One model object is the source of both, in one
statement, so they cannot disagree.

SQLite specifics, all in `sqlite_engine`:
- WAL, `synchronous=NORMAL` (WAL's safe setting: a crash cannot corrupt the file), foreign keys
  on, and a busy timeout so a second writer waits instead of failing.
- Python's `sqlite3` guesses when to begin transactions; here every transaction is begun
  explicitly instead. Writes use `BEGIN IMMEDIATE`, so a read-check-write sequence (the
  checkpoint's "does it undo anything?" test) holds the write lock from its first read and two
  writers cannot both pass the check against the same predecessor. Reads use a plain `BEGIN`
  and never block on WAL.

Coordination (P1-12c, `models/coordination.py`): every write of a run's state carries the
driver `Lease` and is checked against the run's current lease inside the same transaction,
so the check and the write cannot be separated. `checkpoint` also updates the `runs` row
(status, paused state, spent, terminal reason) in that transaction: the checkpoint is
authoritative, and `save_run` may not change those fields.

Nothing else here is SQLite-specific, so a Postgres engine can be given to `SqliteRunStore`'s
constructor unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Self, cast

from pydantic import BaseModel
from sqlalchemy import URL, Engine, create_engine, event, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mpilot.core.clock import Clock, SystemClock
from mpilot.core.errors import CancelRequestedError, RunEndedError, StoreError
from mpilot.core.types import RunId, RunStatus
from mpilot.models.artifacts import EscalationReport, FinalVerification, MigrationNote
from mpilot.models.coordination import (
    CHECKPOINT_OWNED,
    Lease,
    PlanDecision,
    Renewal,
    RunControl,
    check_fence,
    plan_problem,
    renew,
    run_following,
    take_lease,
    transition_problems,
)
from mpilot.models.domain import (
    Attempt,
    Baseline,
    CriticVerdict,
    EvalResult,
    IntegrityReport,
    LLMCall,
    Plan,
    PullRequest,
    RoutingDecision,
    Run,
    TestResult,
    ToolCall,
)
from mpilot.models.migrate import current_revision, head_revision, upgrade_connection
from mpilot.models.state import Checkpoint
from mpilot.models.tables import (
    Base,
    DbAttempt,
    DbBaseline,
    DbCheckpoint,
    DbCriticVerdict,
    DbEscalationReport,
    DbEvalResult,
    DbFinalVerification,
    DbIntegrityReport,
    DbLease,
    DbLLMCall,
    DbMigrationNote,
    DbPlan,
    DbPullRequest,
    DbRoutingDecision,
    DbRun,
    DbRunControl,
    DbTestResult,
    DbToolCall,
)
from mpilot.orchestrator.base import AppendRow, RowT, SingleRow

_WRITE = "mpilot_write"

#: Wait this long for another writer before giving up (SQLite `busy_timeout`).
BUSY_TIMEOUT_MS = 30_000


# ---------------------------------------------------------------- engine


def sqlite_engine(path: Path | str) -> Engine:
    """An engine for a SQLite file with the pragmas and transaction handling described above.

    Use `.execution_options(mpilot_write=True)` on it for a connection that begins its
    transactions with `BEGIN IMMEDIATE`; `SqliteRunStore` does so for every write.
    """
    engine = create_engine(URL.create("sqlite", database=str(path)))

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None  # we begin transactions ourselves
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        immediate = conn.get_execution_options().get(_WRITE, False)
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


# ---------------------------------------------------------------- row mapping
#
# One function per record type turns a model into the table row it is stored as. The columns
# are copied from the model here and nowhere else.


def _doc(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _value(member: Any) -> Any:
    return None if member is None else member.value


def _run_columns(run: Run) -> dict[str, Any]:
    return {
        "created_at": run.created_at,
        "status": run.status.value,
        "repo_url": run.repo_url,
        "base_sha": run.base_sha,
        "branch": run.branch,
        "terminal_reason": run.terminal_reason,
        "paused_at_state": _value(run.paused_at_state),
        "config_hash": run.config_hash,
        "doc": _doc(run),
    }


def _append_row(row: AppendRow, at: datetime) -> Base:
    base: dict[str, Any] = {"run_id": row.run_id, "recorded_at": at}
    match row:
        case ToolCall():
            return DbToolCall(
                **base,
                id=row.id,
                role_call_id=row.role_call_id,
                attempt_id=row.attempt_id,
                role=row.role.value,
                tool=row.tool,
                duration_ms=row.duration_ms,
                policy_decision=row.policy_decision.value,
                deny_reason=row.deny_reason,
                doc=_doc(row),
            )
        case LLMCall():
            return DbLLMCall(
                **base,
                id=row.id,
                role=row.role.value,
                step_id=row.step_id,
                attempt_id=row.attempt_id,
                parent_call_id=row.parent_call_id,
                runtime=row.runtime,
                model=row.model,
                tier=row.tier.value,
                effort=row.effort.value,
                routing_decision_id=row.routing_decision_id,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_read_tokens=row.cache_read_tokens,
                cache_write_tokens=row.cache_write_tokens,
                thinking=row.thinking,
                api_equiv_usd=row.api_equiv_usd,
                latency_ms=row.latency_ms,
                stop=_value(row.stop),
                turns=row.turns,
                limit_error=row.limit_error,
                doc=_doc(row),
            )
        case RoutingDecision():
            return DbRoutingDecision(
                **base,
                id=row.id,
                step_id=row.step_id,
                role=row.role.value,
                policy=row.policy.value,
                effort_policy=row.effort_policy.value,
                tier=row.tier.value,
                effort=row.effort.value,
                sticky=row.sticky,
                inputs_hash=row.inputs_hash,
                doc=_doc(row),
            )
        case TestResult():
            return DbTestResult(
                **base,
                attempt_id=row.attempt_id,
                stage=row.stage.value,
                passed=row.passed,
                failed=row.failed,
                skipped=row.skipped,
                errors=row.errors,
                duration_s=row.duration_s,
                doc=_doc(row),
            )
        case Attempt():
            return DbAttempt(
                **base,
                id=row.id,
                step_id=row.step_id,
                n=row.n,
                n_in_step=row.n_in_step,
                outcome=row.outcome.value,
                diff_sha=row.diff_sha,
                signature=row.diagnosis.signature if row.diagnosis else None,
                category=row.diagnosis.category.value if row.diagnosis else None,
                doc=_doc(row),
            )
        case IntegrityReport():
            return DbIntegrityReport(
                **base,
                mirror_commit=row.mirror_commit,
                passed=row.passed,
                test_count_before=row.test_count_before,
                test_count_after=row.test_count_after,
                coverage_delta_pp=row.coverage_delta_pp,
                doc=_doc(row),
            )
        case CriticVerdict():
            return DbCriticVerdict(
                **base, mirror_commit=row.mirror_commit, decision=row.decision.value, doc=_doc(row)
            )
        case MigrationNote():
            return DbMigrationNote(**base, key=row.key, text=row.text)
    raise TypeError(f"not an appendable record: {type(row).__name__}")


def _single_row(row: SingleRow) -> Base:
    match row:
        case Baseline():
            return DbBaseline(
                run_id=row.run_id,
                test_count=row.test_count,
                coverage_pct=row.coverage_pct,
                doc=_doc(row),
            )
        case Plan():
            return DbPlan(run_id=row.run_id, id=row.id, approved=row.approved, doc=_doc(row))
        case PullRequest():
            return DbPullRequest(
                run_id=row.run_id,
                number=row.number,
                url=row.url,
                branch=row.branch,
                draft=row.draft,
                created_at=row.created_at,
                doc=_doc(row),
            )
        case EscalationReport():
            return DbEscalationReport(
                run_id=row.run_id,
                category=row.category.value,
                created_at=row.created_at,
                report_md=row.report_md,
                data=row.data,
            )
    raise TypeError(f"not a single-record type: {type(row).__name__}")


def _from_doc[M: BaseModel](model: type[M]) -> Callable[[Any], M]:
    return lambda row: model.model_validate(row.doc)


#: Record type -> (table, how a row becomes the model again). The two records with no `doc`
#: are rebuilt from their columns.
_READERS: dict[type[BaseModel], tuple[type[Base], Callable[[Any], BaseModel]]] = {
    ToolCall: (DbToolCall, _from_doc(ToolCall)),
    LLMCall: (DbLLMCall, _from_doc(LLMCall)),
    RoutingDecision: (DbRoutingDecision, _from_doc(RoutingDecision)),
    TestResult: (DbTestResult, _from_doc(TestResult)),
    Attempt: (DbAttempt, _from_doc(Attempt)),
    IntegrityReport: (DbIntegrityReport, _from_doc(IntegrityReport)),
    CriticVerdict: (DbCriticVerdict, _from_doc(CriticVerdict)),
    MigrationNote: (
        DbMigrationNote,
        lambda r: MigrationNote(run_id=RunId(r.run_id), key=r.key, text=r.text),
    ),
    Baseline: (DbBaseline, _from_doc(Baseline)),
    Plan: (DbPlan, _from_doc(Plan)),
    PullRequest: (DbPullRequest, _from_doc(PullRequest)),
    EscalationReport: (
        DbEscalationReport,
        lambda r: EscalationReport(
            run_id=RunId(r.run_id),
            category=r.category,
            report_md=r.report_md,
            data=r.data,
            created_at=r.created_at,
        ),
    ),
}
_SINGLE_TYPES = (Baseline, Plan, PullRequest, EscalationReport)


# ---------------------------------------------------------------- the store


class SqliteRunStore:
    """The `RunStore` (orchestrator/base.py) over a SQLAlchemy engine.

    `SqliteRunStore.open(path)` creates the file and brings its schema to the latest Alembic
    revision. The constructor takes any engine whose schema is already migrated.
    """

    def __init__(self, engine: Engine, clock: Clock | None = None) -> None:
        self._engine = engine
        self._write_engine = engine.execution_options(**{_WRITE: True})
        self._clock: Clock = clock if clock is not None else SystemClock()

    @classmethod
    def open(cls, path: Path | str, clock: Clock | None = None) -> Self:
        """Open (creating if needed) the database at `path` and migrate it to head."""
        if str(path) != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        store = cls(sqlite_engine(path), clock)
        store.migrate()
        return store

    def migrate(self) -> None:
        """Upgrade the schema to the latest revision. Runs under the write lock, so two
        processes opening a new file at once cannot both create the tables."""
        with self._write_engine.begin() as conn:
            if current_revision(conn) != head_revision():
                upgrade_connection(conn)

    def close(self) -> None:
        self._engine.dispose()

    @property
    def engine(self) -> Engine:
        return self._engine

    # -- transactions

    @contextmanager
    def _write(self) -> Iterator[Session]:
        """One write transaction; a refused write is a `StoreError`, and nothing is kept."""
        try:
            with Session(self._write_engine) as session, session.begin():
                yield session
        except IntegrityError as exc:
            raise StoreError(f"the store refused the write: {exc.orig}") from exc

    def _read(self) -> Session:
        return Session(self._engine)

    # -- coordination helpers (inside a write transaction)

    @staticmethod
    def _lease_of(session: Session, run_id: RunId) -> Lease | None:
        row = session.get(DbLease, run_id)
        if row is None:
            return None
        return Lease(
            run_id=RunId(row.run_id),
            holder=row.holder,
            token=row.token,
            acquired_at=row.acquired_at,
            expires_at=row.expires_at,
            released_at=row.released_at,
        )

    def _fence(self, session: Session, lease: Lease, run_id: RunId) -> Lease:
        return check_fence(self._lease_of(session, run_id), lease, run_id)

    @staticmethod
    def _control_of(session: Session, run_id: RunId) -> RunControl:
        row = session.get(DbRunControl, run_id)
        if row is None:
            return RunControl(run_id=run_id)
        return RunControl(
            run_id=run_id,
            cancel_requested_at=row.cancel_requested_at,
            cancel_reason=row.cancel_reason,
            plan_decision=None if row.plan_decision is None else PlanDecision(row.plan_decision),
            plan_reason=row.plan_reason,
            plan_decided_at=row.plan_decided_at,
        )

    @staticmethod
    def _run_row(session: Session, run_id: RunId) -> DbRun:
        row = session.get(DbRun, run_id)
        if row is None:
            raise KeyError(run_id)
        return row

    # -- runs

    def create_run(self, run: Run) -> None:
        with self._write() as session:
            session.add(DbRun(id=run.id, **_run_columns(run)))

    def save_run(self, run: Run, *, lease: Lease) -> None:
        with self._write() as session:
            row = self._run_row(session, run.id)  # KeyError for an unknown run, before fencing
            self._fence(session, lease, run.id)
            problem = _checkpoint_owned_change(Run.model_validate(row.doc), run)
            if problem:
                raise StoreError(problem)
            for column, value in _run_columns(run).items():
                setattr(row, column, value)

    def get_run(self, run_id: RunId) -> Run:
        with self._read() as session:
            row = session.get(DbRun, run_id)
            if row is None:
                raise KeyError(run_id)
            return Run.model_validate(row.doc)

    def list_runs(self) -> Sequence[Run]:
        with self._read() as session:
            stmt = select(DbRun.doc).order_by(DbRun.created_at.desc(), DbRun.id.desc())
            return [Run.model_validate(doc) for doc in session.scalars(stmt)]

    # -- leases and requests

    def acquire_lease(self, run_id: RunId, holder: str, *, ttl_s: float) -> Lease:
        with self._write() as session:
            run = Run.model_validate(self._run_row(session, run_id).doc)
            if run.status.is_terminal:
                raise RunEndedError(f"run {run_id} has ended ({run.status})")
            lease = take_lease(
                self._lease_of(session, run_id), run_id, holder, now=self._clock.now(), ttl_s=ttl_s
            )
            session.merge(_lease_row(lease))
            return lease

    def renew_lease(self, lease: Lease, *, ttl_s: float) -> Renewal:
        with self._write() as session:
            renewed = renew(
                self._lease_of(session, lease.run_id), lease, now=self._clock.now(), ttl_s=ttl_s
            )
            session.merge(_lease_row(renewed))
            return Renewal(lease=renewed, control=self._control_of(session, lease.run_id))

    def release_lease(self, lease: Lease) -> None:
        with self._write() as session:
            current = self._lease_of(session, lease.run_id)
            if current is None or (current.token, current.holder) != (lease.token, lease.holder):
                return  # someone else holds it now: nothing of ours to release
            if current.released_at is None:
                session.merge(
                    _lease_row(current.model_copy(update={"released_at": self._clock.now()}))
                )

    def current_lease(self, run_id: RunId) -> Lease | None:
        with self._read() as session:
            return self._lease_of(session, run_id)

    def control(self, run_id: RunId) -> RunControl:
        with self._read() as session:
            self._run_row(session, run_id)
            return self._control_of(session, run_id)

    def request_cancel(self, run_id: RunId, *, reason: str | None = None) -> RunControl:
        with self._write() as session:
            run = Run.model_validate(self._run_row(session, run_id).doc)
            control = self._control_of(session, run_id)
            if run.status.is_terminal or control.cancel_requested:
                return control  # nothing to cancel, or already asked: idempotent
            updated = control.model_copy(
                update={"cancel_requested_at": self._clock.now(), "cancel_reason": reason}
            )
            session.merge(_control_row(updated))
            return updated

    def request_plan_decision(
        self, run_id: RunId, *, approve: bool, reason: str | None = None
    ) -> RunControl:
        with self._write() as session:
            run = Run.model_validate(self._run_row(session, run_id).doc)
            control = self._control_of(session, run_id)
            problem = _plan_decision_problem(run, control)
            if problem:
                raise StoreError(problem)
            updated = RunControl.model_validate(
                {
                    **control.model_dump(),
                    "plan_decision": PlanDecision.APPROVE if approve else PlanDecision.REJECT,
                    "plan_reason": reason,
                    "plan_decided_at": self._clock.now(),
                }
            )
            session.merge(_control_row(updated))
            return updated

    # -- rows

    def append(self, row: AppendRow, *, lease: Lease) -> None:
        with self._write() as session:
            self._fence(session, lease, row.run_id)
            session.add(_append_row(row, self._clock.now()))

    def put(self, row: SingleRow, *, lease: Lease) -> None:
        with self._write() as session:
            self._fence(session, lease, row.run_id)
            if isinstance(row, Baseline) and session.get(DbBaseline, row.run_id) is not None:
                raise StoreError(f"run {row.run_id} already has its baseline")
            if isinstance(row, Plan):
                problem = plan_problem(row.approved, self._control_of(session, row.run_id))
                if problem:
                    raise StoreError(problem)
            session.merge(_single_row(row))

    def rows(self, run_id: RunId, kind: type[RowT]) -> Sequence[RowT]:
        reader = _READERS.get(kind)
        if reader is None:
            raise TypeError(f"{kind.__name__} is not a stored record type")
        table, build = reader
        with self._read() as session:
            if kind in _SINGLE_TYPES:
                one = session.get(table, run_id)
                return [] if one is None else [cast(RowT, build(one))]
            stmt = select(table).where(table.run_id == run_id).order_by(table.seq)  # type: ignore[attr-defined]
            return [cast(RowT, build(r)) for r in session.scalars(stmt)]

    def append_final_verification(
        self, run_id: RunId, result: FinalVerification, *, lease: Lease
    ) -> None:
        with self._write() as session:
            self._fence(session, lease, run_id)
            session.add(
                DbFinalVerification(
                    run_id=run_id,
                    recorded_at=self._clock.now(),
                    mirror_commit=result.mirror_commit,
                    cache=result.cache,
                    runner_exit_code=result.runner_exit_code,
                    coverage_pct=result.coverage_pct,
                    doc=_doc(result),
                )
            )

    def final_verifications(self, run_id: RunId) -> Sequence[FinalVerification]:
        with self._read() as session:
            stmt = (
                select(DbFinalVerification.doc)
                .where(DbFinalVerification.run_id == run_id)
                .order_by(DbFinalVerification.seq)
            )
            return [FinalVerification.model_validate(doc) for doc in session.scalars(stmt)]

    # -- checkpoints

    def checkpoint(self, cp: Checkpoint, *, lease: Lease, rows: Sequence[AppendRow] = ()) -> None:
        with self._write() as session:
            # BEGIN IMMEDIATE is already held, so nobody can write between these reads and
            # the inserts below: lease, requests, predecessor and run row are one snapshot.
            held = self._fence(session, lease, cp.run_id)
            control = self._control_of(session, cp.run_id)
            if control.cancel_requested and cp.status is not RunStatus.END_CANCELLED:
                raise CancelRequestedError(f"run {cp.run_id}: cancel requested")
            run_row = self._run_row(session, cp.run_id)
            run = Run.model_validate(run_row.doc)
            doc = session.scalar(_latest_checkpoint_doc(cp.run_id))
            previous = None if doc is None else Checkpoint.model_validate(doc)
            problems = [] if previous is None else cp.covers(previous)
            was = (
                previous.unit_key[0] if previous is not None else run.paused_at_state or run.status
            )
            problems += transition_problems(cp.unit_key[0], was, control)
            problems += [
                f"row for {r.run_id} in a checkpoint of {cp.run_id}"
                for r in rows
                if r.run_id != cp.run_id
            ]
            if problems:
                raise StoreError("; ".join(problems))
            now = self._clock.now()
            for row in rows:
                session.add(_append_row(row, now))
            session.add(
                DbCheckpoint(
                    run_id=cp.run_id,
                    seq=cp.seq,
                    at=cp.at,
                    status=cp.status.value,
                    paused_at_state=_value(cp.paused_at_state),
                    step_id=cp.step_id,
                    attempt_n=cp.attempt_n,
                    doc=_doc(cp),
                )
            )
            for column, value in _run_columns(run_following(run, cp)).items():
                setattr(run_row, column, value)
            if cp.status.is_terminal:  # nothing is left to drive: free the run in the same step
                session.merge(_lease_row(held.model_copy(update={"released_at": now})))

    def latest_checkpoint(self, run_id: RunId) -> Checkpoint | None:
        with self._read() as session:
            doc = session.scalar(_latest_checkpoint_doc(run_id))
            return None if doc is None else Checkpoint.model_validate(doc)

    # -- benchmark

    def add_eval(self, result: EvalResult) -> None:
        with self._write() as session:
            session.add(
                DbEvalResult(
                    recorded_at=self._clock.now(),
                    suite=result.suite,
                    task_id=result.task_id,
                    system=result.system,
                    seed=result.seed,
                    status=result.status.value,
                    success_level=result.success_level.value,
                    failure_category=_value(result.failure_category),
                    doc=_doc(result),
                )
            )

    def evals(self, suite: str) -> Sequence[EvalResult]:
        with self._read() as session:
            stmt = select(DbEvalResult.doc).where(DbEvalResult.suite == suite)
            return [
                EvalResult.model_validate(d)
                for d in session.scalars(stmt.order_by(DbEvalResult.seq))
            ]


def _checkpoint_owned_change(stored: Run, new: Run) -> str | None:
    changed = [f for f in CHECKPOINT_OWNED if getattr(stored, f) != getattr(new, f)]
    if not changed:
        return None
    return f"{', '.join(changed)} change only through a checkpoint (it is authoritative)"


def _plan_decision_problem(run: Run, control: RunControl) -> str | None:
    """A plan decision is recorded once, and only while the run waits for it (CP-1)."""
    if (run.paused_at_state or run.status) is not RunStatus.AWAITING_APPROVAL:
        return f"run {run.id} is not awaiting plan approval ({run.status})"
    if control.plan_decision is not None:
        return f"run {run.id}'s plan was already decided: {control.plan_decision}"
    return None


def _lease_row(lease: Lease) -> DbLease:
    return DbLease(
        run_id=lease.run_id,
        holder=lease.holder,
        token=lease.token,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        released_at=lease.released_at,
    )


def _control_row(control: RunControl) -> DbRunControl:
    return DbRunControl(
        run_id=control.run_id,
        cancel_requested_at=control.cancel_requested_at,
        cancel_reason=control.cancel_reason,
        plan_decision=_value(control.plan_decision),
        plan_reason=control.plan_reason,
        plan_decided_at=control.plan_decided_at,
    )


def _latest_checkpoint_doc(run_id: RunId) -> Any:
    return (
        select(DbCheckpoint.doc)
        .where(DbCheckpoint.run_id == run_id)
        .order_by(DbCheckpoint.seq.desc())
        .limit(1)
    )
