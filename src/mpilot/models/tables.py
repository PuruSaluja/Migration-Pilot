"""SQLAlchemy 2 tables mirroring the SPEC §12 models (P1-09). The store is P1-10.

Layout rule: every table keeps the **whole Pydantic model** as JSON in `doc` (that is what a
read returns, so a round trip is exact), plus plain columns copied from that model for the
things queried, joined, ordered or constrained. The columns are derived, never authoritative:
the store writes both from the same model object in one statement. Nested §12 classes live
inside their parent's `doc`, not in tables of their own (see `NESTED_IN`).

Conventions:
- Append-only tables (`seq` autoincrement) are never updated or deleted; `seq` is insertion
  order. `recorded_at` is the store's `Clock` time (UTC), so traces are deterministic in tests.
- Single-record-per-run tables are keyed by `run_id` and upserted.
- Every table is dialect-neutral (SQLite in WAL mode now, Postgres later): portable types,
  named constraints, no server-side functions. A test compiles the DDL for both.
- NFR-12 is repeated as CHECK constraints on `llm_calls` and `routing_decisions`: the tier
  is opus or sonnet, the effort medium or high, Opus never runs at medium, and no model
  name mentions Haiku. The database itself refuses what the models already refuse.
- Foreign keys need `PRAGMA foreign_keys=ON` on SQLite; `mpilot.models.migrate.enable_sqlite_fks`
  hooks that onto an engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

#: Stable constraint names: Alembic needs them to drop constraints on every dialect.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSON on SQLite and every other dialect, JSONB on Postgres.
Json = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every dialect.

    SQLite has no timezone type and hands back naive values, so this stores UTC and returns an
    aware datetime. A naive value is refused: the models use `AwareDatetime` (NFR-5).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime: store timezone-aware values only")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: Json, datetime: UtcDateTime}


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# ---------------------------------------------------------------- runs and single records


class DbRun(Base):
    """`Run`. `doc` holds the nested spec, env, budgets and spent."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String)
    repo_url: Mapped[str] = mapped_column(String)
    base_sha: Mapped[str | None] = mapped_column(String)
    branch: Mapped[str | None] = mapped_column(String)
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    paused_at_state: Mapped[str | None] = mapped_column(String)
    config_hash: Mapped[str] = mapped_column(String)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_runs_created_at", "created_at"),  # newest first (`GET /runs`)
        Index("ix_runs_status", "status"),
        CheckConstraint(
            "(status = 'PAUSED_LIMIT') = (paused_at_state IS NOT NULL)",
            name="paused_state_iff_paused",
        ),
    )


class DbBaseline(Base):
    """`Baseline`, one per run. P0, flaky set and test-file hashes are inside `doc`."""

    __tablename__ = "baselines"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    test_count: Mapped[int] = mapped_column(Integer)
    coverage_pct: Mapped[float | None] = mapped_column(Float)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (CheckConstraint("test_count >= 0", name="test_count_nonneg"),)


class DbPlan(Base):
    """`Plan`, one per run, upserted as step statuses change. `Step` rows live in `doc`."""

    __tablename__ = "plans"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String, unique=True)
    approved: Mapped[bool | None] = mapped_column(Boolean)  # NULL until a human decides (CP-1)
    doc: Mapped[dict[str, Any]]


class DbPullRequest(Base):
    """`PullRequest`, one per run; a rerun updates it (FR-16)."""

    __tablename__ = "pull_requests"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String)
    branch: Mapped[str] = mapped_column(String)
    draft: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime]
    doc: Mapped[dict[str, Any]]

    __table_args__ = (CheckConstraint("number > 0", name="number_positive"),)


class DbEscalationReport(Base):
    """`EscalationReport` (FR-17), one per run. Every field of the model is a column, so there
    is no `doc`: `report_md` is `report.md` as written, `data` the structured parts (failing
    tests, the top 3 hypotheses, attempts, budget used; P5-14 fixes its shape), `category` a
    `FailureCategory` value."""

    __tablename__ = "escalation_reports"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    category: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime]
    report_md: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]]


# ---------------------------------------------------------------- checkpoints


class DbCheckpoint(Base):
    """`Checkpoint`: written after every tool call and state change (NFR-4); never updated.

    The latest one is the row with the highest `seq` for the run. `doc` holds the sticky
    routing state, budget state, failure history and in-flight call.
    """

    __tablename__ = "checkpoints"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String)
    paused_at_state: Mapped[str | None] = mapped_column(String)
    step_id: Mapped[str | None] = mapped_column(String)
    attempt_n: Mapped[int | None] = mapped_column(Integer)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        CheckConstraint("seq >= 0", name="seq_nonneg"),
        CheckConstraint(
            "(status = 'PAUSED_LIMIT') = (paused_at_state IS NOT NULL)",
            name="paused_state_iff_paused",
        ),
    )


# ---------------------------------------------------------------- append-only run records


class _Append:
    """Columns every append-only table has: insertion order, the run, and when it was stored."""

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recorded_at: Mapped[datetime]


class DbAttempt(_Append, Base):
    """`Attempt`. `signature` and `category` are copied out of its `Diagnosis`."""

    __tablename__ = "attempts"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    id: Mapped[str] = mapped_column(String, unique=True)
    step_id: Mapped[str] = mapped_column(String)
    n: Mapped[int] = mapped_column(Integer)  # run-wide, = refs/mpilot/attempt/<n>
    n_in_step: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String)
    diff_sha: Mapped[str] = mapped_column(String)
    signature: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_attempts_run_id_seq", "run_id", "seq"),
        Index("ix_attempts_step_id", "step_id"),
        UniqueConstraint("run_id", "n"),  # one attempt ref per number (§9.5)
        UniqueConstraint("run_id", "step_id", "n_in_step"),
        CheckConstraint("n > 0 AND n_in_step > 0 AND n_in_step <= n", name="numbers_valid"),
        CheckConstraint(
            _in("outcome", ("improved", "same", "regressed", "green")), name="outcome_known"
        ),
    )


class DbToolCall(_Append, Base):
    """`ToolCall`: every tool call, allowed or denied."""

    __tablename__ = "tool_calls"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    id: Mapped[str] = mapped_column(String, unique=True)
    role_call_id: Mapped[str] = mapped_column(String)  # no FK: the LLMCall row comes later
    attempt_id: Mapped[str | None] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    tool: Mapped[str] = mapped_column(String)
    duration_ms: Mapped[int] = mapped_column(Integer)
    policy_decision: Mapped[str] = mapped_column(String)
    deny_reason: Mapped[str | None] = mapped_column(Text)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_tool_calls_run_id_seq", "run_id", "seq"),
        Index("ix_tool_calls_attempt_id", "attempt_id"),
        Index("ix_tool_calls_role_call_id", "role_call_id"),
        CheckConstraint("duration_ms >= 0", name="duration_nonneg"),
        CheckConstraint(_in("policy_decision", ("allow", "deny")), name="decision_known"),
        CheckConstraint(
            "(policy_decision = 'deny') = (deny_reason IS NOT NULL)", name="deny_reason_iff_deny"
        ),
    )


class DbRoutingDecision(_Append, Base):
    """`RoutingDecision`: one per model call, written before the call (FR-22)."""

    __tablename__ = "routing_decisions"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    id: Mapped[str] = mapped_column(String, unique=True)
    step_id: Mapped[str | None] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    policy: Mapped[str] = mapped_column(String)
    effort_policy: Mapped[str] = mapped_column(String)
    tier: Mapped[str] = mapped_column(String)
    effort: Mapped[str] = mapped_column(String)
    sticky: Mapped[bool] = mapped_column(Boolean)
    inputs_hash: Mapped[str] = mapped_column(String)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_routing_decisions_run_id_seq", "run_id", "seq"),
        CheckConstraint(_in("tier", ("opus", "sonnet")), name="tier_known"),
        CheckConstraint(_in("effort", ("medium", "high")), name="effort_known"),
        CheckConstraint("NOT (tier = 'opus' AND effort = 'medium')", name="opus_never_medium"),
        CheckConstraint(_in("policy", ("routed", "all_opus", "all_sonnet")), name="policy_known"),
        CheckConstraint(_in("effort_policy", ("routed", "all_high")), name="effort_policy_known"),
    )


class DbLLMCall(_Append, Base):
    """`LLMCall`: one row per finished or limit-interrupted role call, linked to the decision
    that chose its model. The final answer (`output`) is only in `doc`."""

    __tablename__ = "llm_calls"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    id: Mapped[str] = mapped_column(String, unique=True)
    role: Mapped[str] = mapped_column(String)
    step_id: Mapped[str | None] = mapped_column(String)
    attempt_id: Mapped[str | None] = mapped_column(String)
    parent_call_id: Mapped[str | None] = mapped_column(String)  # no FK: the parent ends later
    runtime: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    tier: Mapped[str] = mapped_column(String)
    effort: Mapped[str] = mapped_column(String)
    routing_decision_id: Mapped[str] = mapped_column(
        ForeignKey("routing_decisions.id", ondelete="CASCADE")
    )
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer)
    cache_write_tokens: Mapped[int] = mapped_column(Integer)
    thinking: Mapped[bool] = mapped_column(Boolean)
    api_equiv_usd: Mapped[float] = mapped_column(Float)
    latency_ms: Mapped[int] = mapped_column(Integer)
    stop: Mapped[str | None] = mapped_column(String)
    turns: Mapped[int] = mapped_column(Integer)
    limit_error: Mapped[str | None] = mapped_column(Text)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_llm_calls_run_id_seq", "run_id", "seq"),
        Index("ix_llm_calls_routing_decision_id", "routing_decision_id"),
        Index("ix_llm_calls_attempt_id", "attempt_id"),
        CheckConstraint(_in("tier", ("opus", "sonnet")), name="tier_known"),
        CheckConstraint(_in("effort", ("medium", "high")), name="effort_known"),
        CheckConstraint("NOT (tier = 'opus' AND effort = 'medium')", name="opus_never_medium"),
        CheckConstraint("lower(model) NOT LIKE '%haiku%'", name="no_haiku"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0"
            " AND cache_write_tokens >= 0 AND api_equiv_usd >= 0 AND latency_ms >= 0",
            name="usage_nonneg",
        ),
        CheckConstraint("turns >= 0", name="turns_nonneg"),
        CheckConstraint(
            "stop IS NULL OR "
            + _in(
                "stop", ("completed", "schema_invalid", "max_turns", "budget_stop", "tool_call_cap")
            ),
            name="stop_known",
        ),
        CheckConstraint("(stop IS NULL) = (limit_error IS NOT NULL)", name="stop_xor_limit"),
        CheckConstraint(
            "(parent_call_id IS NOT NULL) = (role = 'explorer')", name="parent_iff_explorer"
        ),
        CheckConstraint("attempt_id IS NULL OR step_id IS NOT NULL", name="attempt_needs_step"),
    )


class DbTestResult(_Append, Base):
    """`TestResult`: baseline, targeted, full and final runs. IDs and failures are in `doc`."""

    __tablename__ = "test_results"
    __test__ = False  # not a pytest class

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    attempt_id: Mapped[str | None] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String)
    passed: Mapped[int] = mapped_column(Integer)
    failed: Mapped[int] = mapped_column(Integer)
    skipped: Mapped[int] = mapped_column(Integer)
    errors: Mapped[int] = mapped_column(Integer)
    duration_s: Mapped[float] = mapped_column(Float)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_test_results_run_id_stage_seq", "run_id", "stage", "seq"),
        CheckConstraint(
            _in("stage", ("baseline", "targeted", "full", "final")), name="stage_known"
        ),
        CheckConstraint(
            "passed >= 0 AND failed >= 0 AND skipped >= 0 AND errors >= 0 AND duration_s >= 0",
            name="counts_nonneg",
        ),
    )


class DbFinalVerification(_Append, Base):
    """`FinalVerification` (§9.5): the whole pipeline in a fresh sandbox from `mirror_commit`.

    Success and every gate check judge only this record. It can recur (a failed integrity
    gate sends the run back to the repair loop once), so it is append-only and the latest
    row for the run is the current one.
    """

    __tablename__ = "final_verifications"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    mirror_commit: Mapped[str] = mapped_column(String)
    cache: Mapped[str] = mapped_column(String)
    runner_exit_code: Mapped[int | None] = mapped_column(Integer)
    coverage_pct: Mapped[float | None] = mapped_column(Float)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_final_verifications_run_id_seq", "run_id", "seq"),
        Index("ix_final_verifications_mirror_commit", "mirror_commit"),
        CheckConstraint(_in("cache", ("fresh", "hash_verified")), name="cache_known"),
    )


class DbIntegrityReport(_Append, Base):
    """`IntegrityReport`: one per gate evaluation (§7.4 allows two)."""

    __tablename__ = "integrity_reports"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    mirror_commit: Mapped[str] = mapped_column(String)  # the commit it judged (§9.5)
    passed: Mapped[bool] = mapped_column(Boolean)
    test_count_before: Mapped[int] = mapped_column(Integer)
    test_count_after: Mapped[int] = mapped_column(Integer)
    coverage_delta_pp: Mapped[float | None] = mapped_column(Float)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_integrity_reports_run_id_seq", "run_id", "seq"),
        CheckConstraint("test_count_before >= 0 AND test_count_after >= 0", name="counts_nonneg"),
    )


class DbCriticVerdict(_Append, Base):
    """`CriticVerdict`: at most two per run (one revise loop, FR-15)."""

    __tablename__ = "critic_verdicts"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    mirror_commit: Mapped[str] = mapped_column(String)  # the diff target it reviewed
    decision: Mapped[str] = mapped_column(String)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_critic_verdicts_run_id_seq", "run_id", "seq"),
        CheckConstraint(_in("decision", ("approve", "revise", "escalate")), name="decision_known"),
    )


class DbMigrationNote(_Append, Base):
    """`MigrationNote`, one per `write_note` call (§9.1, §10.1): confirmed facts, rejected
    hypotheses, conventions. Every field of the model is a column, so there is no `doc`.

    Append-only so the history stays traceable (G6); the current note for a key is its
    highest `seq`. The ≤ 2k-token injection cap is applied when notes are read, not here.
    """

    __tablename__ = "migration_notes"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index("ix_migration_notes_run_id_key_seq", "run_id", "key", "seq"),
        CheckConstraint("length(key) > 0", name="key_nonempty"),
    )


# ---------------------------------------------------------------- benchmark


class DbEvalResult(_Append, Base):
    """`EvalResult` (FR-20). Not tied to a run: bench rows outlive and span runs.

    No uniqueness is imposed on (suite, task, system, seed): how a resumed batch replaces a
    row is P9-01's decision.
    """

    __tablename__ = "eval_results"

    suite: Mapped[str] = mapped_column(String)
    task_id: Mapped[str] = mapped_column(String)
    system: Mapped[str] = mapped_column(String)
    seed: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    success_level: Mapped[str] = mapped_column(String)
    failure_category: Mapped[str | None] = mapped_column(String)
    doc: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_eval_results_suite_task_id_system_seed", "suite", "task_id", "system", "seed"),
        CheckConstraint(
            _in("success_level", ("none", "L0", "L1", "L2", "L3", "L4")), name="success_level_known"
        ),
        CheckConstraint("seed >= 0", name="seed_nonneg"),
    )


# ---------------------------------------------------------------- coordination (P1-12c)


class DbLease(Base):
    """`Lease`: the run's driver lease, one row per run, updated in place. Never deleted, so
    the fencing token only rises (a release sets `released_at`). Every field is a column."""

    __tablename__ = "run_leases"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    holder: Mapped[str] = mapped_column(String)
    token: Mapped[int] = mapped_column(Integer)
    acquired_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    released_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("token > 0", name="token_positive"),
        CheckConstraint("expires_at > acquired_at", name="expires_after_acquired"),
    )


class DbRunControl(Base):
    """`RunControl`: requests from other processes (cancel, plan decision), one row per run.
    Every field is a column."""

    __tablename__ = "run_controls"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    cancel_requested_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    plan_decision: Mapped[str | None] = mapped_column(String)
    plan_reason: Mapped[str | None] = mapped_column(Text)
    plan_decided_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "plan_decision IS NULL OR " + _in("plan_decision", ("approve", "reject")),
            name="plan_decision_known",
        ),
        CheckConstraint(
            "(plan_decision IS NULL) = (plan_decided_at IS NULL)", name="plan_decided_together"
        ),
        CheckConstraint(
            "cancel_reason IS NULL OR cancel_requested_at IS NOT NULL", name="reason_needs_request"
        ),
    )


# ---------------------------------------------------------------- how §12 maps onto tables

#: Every persisted record class (all of SPEC §12, plus the records the spec leaves implicit),
#: mapped to the table that stores it. A test checks this against the spec so no model goes
#: unpersisted.
TABLE_OF: dict[str, str] = {
    "Run": "runs",
    "Baseline": "baselines",
    "Plan": "plans",
    "PullRequest": "pull_requests",
    "Checkpoint": "checkpoints",
    "Attempt": "attempts",
    "ToolCall": "tool_calls",
    "RoutingDecision": "routing_decisions",
    "LLMCall": "llm_calls",
    "TestResult": "test_results",
    "FinalVerification": "final_verifications",
    "IntegrityReport": "integrity_reports",
    "CriticVerdict": "critic_verdicts",
    "EvalResult": "eval_results",
    "MigrationNote": "migration_notes",
    "EscalationReport": "escalation_reports",
    "Lease": "run_leases",
    "RunControl": "run_controls",
}

#: Classes stored inside a parent's `doc` (the parent is the value).
NESTED_IN: dict[str, str] = {
    "MigrationSpec": "Run",
    "EnvProfile": "Run",
    "Step": "Plan",
    "Diagnosis": "Attempt",
}

__all__ = [
    "NAMING_CONVENTION",
    "NESTED_IN",
    "TABLE_OF",
    "Base",
    "DbAttempt",
    "DbBaseline",
    "DbCheckpoint",
    "DbCriticVerdict",
    "DbEscalationReport",
    "DbEvalResult",
    "DbFinalVerification",
    "DbIntegrityReport",
    "DbLLMCall",
    "DbLease",
    "DbMigrationNote",
    "DbPlan",
    "DbPullRequest",
    "DbRoutingDecision",
    "DbRun",
    "DbRunControl",
    "DbTestResult",
    "DbToolCall",
    "Json",
    "UtcDateTime",
]
