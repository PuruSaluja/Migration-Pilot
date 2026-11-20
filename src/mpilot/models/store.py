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
from mpilot.core.errors import StoreError
from mpilot.core.types import RunId
from mpilot.models.artifacts import EscalationReport, FinalVerification, MigrationNote
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
    DbLLMCall,
    DbMigrationNote,
    DbPlan,
    DbPullRequest,
    DbRoutingDecision,
    DbRun,
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

    # -- runs

    def create_run(self, run: Run) -> None:
        with self._write() as session:
            session.add(DbRun(id=run.id, **_run_columns(run)))

    def save_run(self, run: Run) -> None:
        with self._write() as session:
            row = session.get(DbRun, run.id)
            if row is None:
                raise KeyError(run.id)
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

    # -- rows

    def append(self, row: AppendRow) -> None:
        with self._write() as session:
            session.add(_append_row(row, self._clock.now()))

    def put(self, row: SingleRow) -> None:
        with self._write() as session:
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

    def append_final_verification(self, run_id: RunId, result: FinalVerification) -> None:
        with self._write() as session:
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

    def checkpoint(self, cp: Checkpoint) -> None:
        with self._write() as session:
            # BEGIN IMMEDIATE is already held, so nobody can write a checkpoint between this
            # read and the insert below.
            previous = session.scalar(_latest_checkpoint_doc(cp.run_id))
            if previous is not None:
                problems = cp.covers(Checkpoint.model_validate(previous))
                if problems:
                    raise StoreError("; ".join(problems))
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


def _latest_checkpoint_doc(run_id: RunId) -> Any:
    return (
        select(DbCheckpoint.doc)
        .where(DbCheckpoint.run_id == run_id)
        .order_by(DbCheckpoint.seq.desc())
        .limit(1)
    )
