"""P1-09: SQLAlchemy tables mirror the §12 models and enforce what the models enforce.

Every test runs on a real SQLite file created by the Alembic migration, with foreign keys on,
so the constraints under test are the ones the shipped schema has.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable
from test_domain_models import NOW, RUN, SAMPLES, SPEC

from mpilot.models import artifacts, state, tables
from mpilot.models import domain as d
from mpilot.models.migrate import enable_sqlite_fks, upgrade
from mpilot.models.tables import (
    NESTED_IN,
    TABLE_OF,
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

# ---------------------------------------------------------------- fixtures and row builders


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'mpilot.db'}"
    upgrade(url)
    eng = create_engine(url)
    enable_sqlite_fks(eng)
    yield eng
    eng.dispose()


def _doc(model: d.DomainModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def run_row(**over: Any) -> DbRun:
    run = SAMPLES[d.Run]()
    assert isinstance(run, d.Run)
    values: dict[str, Any] = {
        "id": run.id,
        "created_at": run.created_at,
        "status": run.status.value,
        "repo_url": run.repo_url,
        "base_sha": run.base_sha,
        "branch": run.branch,
        "terminal_reason": run.terminal_reason,
        "paused_at_state": run.paused_at_state.value if run.paused_at_state else None,
        "config_hash": run.config_hash,
        "doc": _doc(run),
    }
    return DbRun(**{**values, **over})


def routing_row(**over: Any) -> DbRoutingDecision:
    m = SAMPLES[d.RoutingDecision]()
    assert isinstance(m, d.RoutingDecision)
    values: dict[str, Any] = {
        "run_id": m.run_id,
        "id": m.id,
        "step_id": m.step_id,
        "role": m.role.value,
        "policy": m.policy.value,
        "effort_policy": m.effort_policy.value,
        "tier": m.tier.value,
        "effort": m.effort.value,
        "sticky": m.sticky,
        "inputs_hash": m.inputs_hash,
        "doc": _doc(m),
        "recorded_at": NOW,
    }
    return DbRoutingDecision(**{**values, **over})


def llm_row(**over: Any) -> DbLLMCall:
    m = SAMPLES[d.LLMCall]()
    assert isinstance(m, d.LLMCall)
    values: dict[str, Any] = {
        "run_id": m.run_id,
        "id": m.id,
        "role": m.role.value,
        "runtime": m.runtime,
        "model": m.model,
        "tier": m.tier.value,
        "effort": m.effort.value,
        "routing_decision_id": m.routing_decision_id,
        "input_tokens": m.input_tokens,
        "output_tokens": m.output_tokens,
        "cache_read_tokens": m.cache_read_tokens,
        "cache_write_tokens": m.cache_write_tokens,
        "thinking": m.thinking,
        "api_equiv_usd": m.api_equiv_usd,
        "latency_ms": m.latency_ms,
        "limit_error": m.limit_error,
        "doc": _doc(m),
        "recorded_at": NOW,
    }
    return DbLLMCall(**{**values, **over})


def tool_row(**over: Any) -> DbToolCall:
    m = SAMPLES[d.ToolCall]()
    assert isinstance(m, d.ToolCall)
    values: dict[str, Any] = {
        "run_id": m.run_id,
        "id": m.id,
        "attempt_id": m.attempt_id,
        "role": m.role.value,
        "tool": m.tool,
        "duration_ms": m.duration_ms,
        "policy_decision": m.policy_decision.value,
        "deny_reason": m.deny_reason,
        "doc": _doc(m),
        "recorded_at": NOW,
    }
    return DbToolCall(**{**values, **over})


def checkpoint_row(seq: int = 0, **over: Any) -> DbCheckpoint:
    values: dict[str, Any] = {
        "run_id": RUN,
        "seq": seq,
        "at": NOW,
        "status": "REPAIR_LOOP",
        "paused_at_state": None,
        "step_id": None,
        "attempt_n": None,
        "doc": {"seq": seq},
    }
    return DbCheckpoint(**{**values, **over})


def commit(engine: Engine, *rows: Any) -> None:
    with Session(engine) as session, session.begin():
        session.add_all(rows)


def count(engine: Engine, table: type[Base]) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(table)) or 0


def with_run(engine: Engine) -> None:
    commit(engine, run_row())


def with_decision(engine: Engine) -> None:
    with_run(engine)
    commit(engine, routing_row())


# ---------------------------------------------------------------- §12 coverage


def _section_12_classes() -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    block = text.split("## 12. Data models", 1)[1].split("```", 2)[1]
    return set(re.findall(r"^class (\w+)", block, re.MULTILINE))


def test_every_section_12_class_is_stored_in_a_table_or_inside_one() -> None:
    stored = set(TABLE_OF) | set(NESTED_IN)
    missing = _section_12_classes() - stored
    # `Step`, `Diagnosis`, `MigrationSpec` and `EnvProfile` are nested; the rest have tables.
    assert not missing, f"§12 classes with nowhere to live: {sorted(missing)}"


def test_the_mapping_names_real_tables_and_real_parents() -> None:
    names = set(Base.metadata.tables)
    assert set(TABLE_OF.values()) <= names
    assert set(NESTED_IN.values()) <= set(TABLE_OF)


def test_every_table_belongs_to_a_model_or_is_an_extra_record() -> None:
    extras = {"escalation_reports", "migration_notes"}  # no Pydantic model yet (P5-14, P1-10)
    assert set(Base.metadata.tables) == set(TABLE_OF.values()) | extras


MODELS: dict[str, type] = {
    "Run": d.Run,
    "Baseline": d.Baseline,
    "Plan": d.Plan,
    "PullRequest": d.PullRequest,
    "Checkpoint": state.Checkpoint,
    "Attempt": d.Attempt,
    "ToolCall": d.ToolCall,
    "RoutingDecision": d.RoutingDecision,
    "LLMCall": d.LLMCall,
    "TestResult": d.TestResult,
    "FinalVerification": artifacts.FinalVerification,
    "IntegrityReport": d.IntegrityReport,
    "CriticVerdict": d.CriticVerdict,
    "EvalResult": d.EvalResult,
}
#: Columns that are not copied from a field of the model.
BOOKKEEPING = {"seq", "recorded_at", "doc"}
#: Columns derived from a nested model (not top-level fields).
DERIVED = {
    "attempts": {"signature", "category"},
    "final_verifications": {"run_id"},  # the artifact has none; the store supplies it
}


def test_every_modeled_table_is_listed() -> None:
    assert set(MODELS) == set(TABLE_OF)


@pytest.mark.parametrize(("cls_name", "table_name"), TABLE_OF.items())
def test_columns_are_copies_of_model_fields(cls_name: str, table_name: str) -> None:
    fields = set(MODELS[cls_name].model_fields)  # type: ignore[attr-defined]
    columns = {c.name for c in Base.metadata.tables[table_name].columns}
    extra = columns - BOOKKEEPING - fields - DERIVED.get(table_name, set())
    assert not extra, f"{table_name} has columns the model does not: {sorted(extra)}"


def test_the_checkpoint_table_has_the_scalars_the_resume_needs() -> None:
    cols = {c.name for c in Base.metadata.tables["checkpoints"].columns}
    assert {"run_id", "seq", "at", "status", "paused_at_state", "step_id", "attempt_n"} <= cols


# ---------------------------------------------------------------- JSON docs round-trip


def _single_row(model: d.DomainModel) -> Base:
    """The table row a single-record model is stored as."""
    if isinstance(model, d.Run):
        return run_row(doc=_doc(model))
    if isinstance(model, d.Baseline):
        return DbBaseline(
            run_id=model.run_id,
            test_count=model.test_count,
            coverage_pct=model.coverage_pct,
            doc=_doc(model),
        )
    if isinstance(model, d.Plan):
        return DbPlan(run_id=model.run_id, id=model.id, approved=model.approved, doc=_doc(model))
    assert isinstance(model, d.PullRequest)
    return DbPullRequest(
        run_id=model.run_id,
        number=model.number,
        url=model.url,
        branch=model.branch,
        draft=model.draft,
        created_at=model.created_at,
        doc=_doc(model),
    )


@pytest.mark.parametrize(
    ("model_cls", "row_type"),
    [(d.Run, DbRun), (d.Baseline, DbBaseline), (d.Plan, DbPlan), (d.PullRequest, DbPullRequest)],
)
def test_a_model_survives_the_json_column_unchanged(
    engine: Engine, model_cls: type[d.DomainModel], row_type: type[Base]
) -> None:
    model = SAMPLES[model_cls]()
    if model_cls is not d.Run:
        with_run(engine)
    commit(engine, _single_row(model))
    with Session(engine) as session:
        back = session.scalars(select(row_type)).one()
        assert model_cls.model_validate(back.doc) == model  # type: ignore[attr-defined]


def test_test_id_sets_come_back_sorted_and_equal(engine: Engine) -> None:
    with_run(engine)
    model = SAMPLES[d.TestResult]()
    assert isinstance(model, d.TestResult)
    commit(
        engine,
        DbTestResult(
            run_id=model.run_id,
            attempt_id=model.attempt_id,
            stage=model.stage.value,
            passed=model.passed,
            failed=model.failed,
            skipped=model.skipped,
            errors=model.errors,
            duration_s=model.duration_s,
            doc=_doc(model),
            recorded_at=NOW,
        ),
    )
    with Session(engine) as session:
        doc = session.scalars(select(DbTestResult)).one().doc
    assert d.TestResult.model_validate(doc) == model
    assert doc["passed_ids"] == sorted(doc["passed_ids"])


# ---------------------------------------------------------------- times


def test_aware_times_come_back_as_utc(engine: Engine) -> None:
    plus_two = timezone(timedelta(hours=2))
    stamp = datetime(2026, 11, 20, 14, 0, tzinfo=plus_two)
    commit(engine, run_row(created_at=stamp))
    with Session(engine) as session:
        back = session.scalars(select(DbRun)).one().created_at
    assert back == stamp
    assert back.utcoffset() == timedelta(0)
    assert back.tzinfo is not None


def test_naive_times_are_refused(engine: Engine) -> None:
    with pytest.raises(StatementError, match="naive"):
        commit(engine, run_row(created_at=datetime(2026, 11, 20, 12, 0)))


# ---------------------------------------------------------------- NFR-12 in the database


@pytest.mark.parametrize(
    "bad",
    [
        {"tier": "haiku"},
        {"tier": "OPUS"},
        {"effort": "low"},
        {"effort": "max"},
        {"tier": "opus", "effort": "medium"},
    ],
)
def test_routing_decisions_refuse_what_nfr_12_forbids(engine: Engine, bad: dict[str, str]) -> None:
    with_run(engine)
    with pytest.raises(IntegrityError):
        commit(engine, routing_row(**bad))
    assert count(engine, DbRoutingDecision) == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"tier": "haiku"},
        {"effort": "low"},
        {"tier": "opus", "effort": "medium"},
        {"model": "claude-haiku-5-5"},
        {"model": "Claude-HAIKU-5-5"},
    ],
)
def test_llm_calls_refuse_what_nfr_12_forbids(engine: Engine, bad: dict[str, str]) -> None:
    with_decision(engine)
    with pytest.raises(IntegrityError):
        commit(engine, llm_row(**bad))
    assert count(engine, DbLLMCall) == 0


@pytest.mark.parametrize(
    ("tier", "effort"), [("opus", "high"), ("sonnet", "high"), ("sonnet", "medium")]
)
def test_the_three_legal_combinations_are_accepted(engine: Engine, tier: str, effort: str) -> None:
    with_run(engine)
    commit(engine, routing_row(tier=tier, effort=effort))
    assert count(engine, DbRoutingDecision) == 1


def test_an_llm_call_must_link_to_an_existing_routing_decision(engine: Engine) -> None:
    with_run(engine)  # no routing decision stored
    with pytest.raises(IntegrityError):
        commit(engine, llm_row())
    with_decision_after = routing_row()
    commit(engine, with_decision_after)
    commit(engine, llm_row())
    assert count(engine, DbLLMCall) == 1


def test_negative_usage_is_refused(engine: Engine) -> None:
    with_decision(engine)
    with pytest.raises(IntegrityError):
        commit(engine, llm_row(input_tokens=-1))


# ---------------------------------------------------------------- other constraints


def test_a_denied_tool_call_needs_a_reason_and_an_allowed_one_cannot_have_one(
    engine: Engine,
) -> None:
    with_run(engine)
    with pytest.raises(IntegrityError):
        commit(engine, tool_row(id="t-1", policy_decision="deny", deny_reason=None))
    with pytest.raises(IntegrityError):
        commit(engine, tool_row(id="t-2", policy_decision="allow", deny_reason="why"))
    with pytest.raises(IntegrityError):
        commit(engine, tool_row(id="t-3", policy_decision="maybe", deny_reason=None))
    commit(engine, tool_row(id="t-4", policy_decision="deny", deny_reason="not on the allowlist"))
    commit(engine, tool_row(id="t-5", policy_decision="allow", deny_reason=None))
    assert count(engine, DbToolCall) == 2


def test_paused_state_is_set_exactly_in_paused_limit(engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        commit(engine, run_row(status="PAUSED_LIMIT", paused_at_state=None))
    with pytest.raises(IntegrityError):
        commit(engine, run_row(status="REPAIR_LOOP", paused_at_state="REPAIR_LOOP"))
    commit(engine, run_row(status="PAUSED_LIMIT", paused_at_state="REPAIR_LOOP"))
    with pytest.raises(IntegrityError):
        commit(engine, checkpoint_row(0, status="PAUSED_LIMIT", paused_at_state=None))
    commit(engine, checkpoint_row(1, status="PAUSED_LIMIT", paused_at_state="REPAIR_LOOP"))


def test_ids_are_unique(engine: Engine) -> None:
    with_run(engine)
    commit(engine, tool_row(id="t-1"))
    with pytest.raises(IntegrityError):
        commit(engine, tool_row(id="t-1"))
    with pytest.raises(IntegrityError):
        commit(engine, run_row())


def test_rows_need_an_existing_run(engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        commit(engine, tool_row(id="orphan"))
    with pytest.raises(IntegrityError):
        commit(engine, checkpoint_row(0))
    with pytest.raises(IntegrityError):
        commit(engine, DbMigrationNote(run_id=RUN, key="k", text="t", recorded_at=NOW))


def test_a_checkpoint_sequence_number_is_used_once_per_run(engine: Engine) -> None:
    with_run(engine)
    commit(engine, checkpoint_row(0), checkpoint_row(1))
    with pytest.raises(IntegrityError):
        commit(engine, checkpoint_row(1))
    with pytest.raises(IntegrityError):
        commit(engine, checkpoint_row(-1))


def test_single_record_tables_hold_one_row_per_run(engine: Engine) -> None:
    with_run(engine)
    plan = SAMPLES[d.Plan]()
    assert isinstance(plan, d.Plan)
    commit(engine, DbPlan(run_id=RUN, id=plan.id, approved=None, doc=_doc(plan)))
    with pytest.raises(IntegrityError):
        commit(engine, DbPlan(run_id=RUN, id="plan-other", approved=None, doc=_doc(plan)))


def test_deleting_a_run_deletes_everything_recorded_for_it(engine: Engine) -> None:
    with_decision(engine)
    commit(
        engine,
        llm_row(),
        tool_row(id="t-1"),
        checkpoint_row(0),
        DbMigrationNote(run_id=RUN, key="k", text="t", recorded_at=NOW),
        DbEscalationReport(
            run_id=RUN, category="NO_PROGRESS", created_at=NOW, report_md="# report", data={}
        ),
    )
    with Session(engine) as session, session.begin():
        session.delete(session.get_one(DbRun, RUN))
    for table in (
        DbRoutingDecision,
        DbLLMCall,
        DbToolCall,
        DbCheckpoint,
        DbMigrationNote,
        DbEscalationReport,
    ):
        assert count(engine, table) == 0


def test_append_only_tables_keep_insertion_order(engine: Engine) -> None:
    with_run(engine)
    commit(engine, *(tool_row(id=f"t-{i}") for i in (3, 1, 2)))
    with Session(engine) as session:
        ids = list(session.scalars(select(DbToolCall.id).order_by(DbToolCall.seq)))
    assert ids == ["t-3", "t-1", "t-2"]


def test_migration_notes_keep_every_write_and_the_latest_is_the_highest_seq(
    engine: Engine,
) -> None:
    with_run(engine)
    commit(
        engine,
        DbMigrationNote(run_id=RUN, key="convention", text="tabs", recorded_at=NOW),
        DbMigrationNote(run_id=RUN, key="convention", text="spaces", recorded_at=NOW),
    )
    with pytest.raises(IntegrityError):
        commit(engine, DbMigrationNote(run_id=RUN, key="", text="t", recorded_at=NOW))
    with Session(engine) as session:
        texts = list(
            session.scalars(
                select(DbMigrationNote.text)
                .where(DbMigrationNote.key == "convention")
                .order_by(DbMigrationNote.seq)
            )
        )
    assert texts == ["tabs", "spaces"]


def test_closed_value_sets(engine: Engine) -> None:
    with_run(engine)
    sample = SAMPLES[d.CriticVerdict]()
    assert isinstance(sample, d.CriticVerdict)
    with pytest.raises(IntegrityError):
        commit(engine, DbCriticVerdict(run_id=RUN, decision="shrug", doc={}, recorded_at=NOW))
    commit(engine, DbCriticVerdict(run_id=RUN, decision="approve", doc={}, recorded_at=NOW))
    with pytest.raises(IntegrityError):
        commit(
            engine,
            DbTestResult(
                run_id=RUN,
                attempt_id=None,
                stage="nightly",
                passed=0,
                failed=0,
                skipped=0,
                errors=0,
                duration_s=0.0,
                doc={},
                recorded_at=NOW,
            ),
        )
    with pytest.raises(IntegrityError):
        commit(
            engine,
            DbFinalVerification(
                run_id=RUN,
                mirror_commit="a" * 40,
                cache="stale",
                runner_exit_code=0,
                coverage_pct=None,
                doc={},
                recorded_at=NOW,
            ),
        )
    with pytest.raises(IntegrityError):
        commit(
            engine,
            DbIntegrityReport(
                run_id=RUN,
                passed=True,
                test_count_before=-1,
                test_count_after=0,
                coverage_delta_pp=None,
                doc={},
                recorded_at=NOW,
            ),
        )
    with pytest.raises(IntegrityError):
        commit(
            engine,
            DbEvalResult(
                suite="s",
                task_id="t",
                system="full",
                seed=0,
                status="END_SUCCESS",
                success_level="L9",
                failure_category=None,
                doc={},
                recorded_at=NOW,
            ),
        )
    with pytest.raises(IntegrityError):
        commit(
            engine,
            DbAttempt(
                run_id=RUN,
                id="a-1",
                step_id="s-1",
                n=0,
                outcome="improved",
                diff_sha="x",
                signature=None,
                category=None,
                doc={},
                recorded_at=NOW,
            ),
        )
    with pytest.raises(IntegrityError):
        commit(
            engine,
            DbBaseline(run_id=RUN, test_count=-1, coverage_pct=None, doc={}),
        )
    with pytest.raises(IntegrityError):
        commit(
            engine,
            DbPullRequest(
                run_id=RUN,
                number=0,
                url="u",
                branch="b",
                draft=True,
                created_at=NOW,
                doc={},
            ),
        )


# ---------------------------------------------------------------- dialects


def test_every_table_compiles_for_postgres_and_sqlite() -> None:
    from sqlalchemy.dialects import postgresql, sqlite

    for dialect in (postgresql.dialect(), sqlite.dialect()):
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=dialect))
            assert f"CREATE TABLE {table.name}" in ddl
            for index in table.indexes:
                assert "CREATE INDEX" in str(CreateIndex(index).compile(dialect=dialect))


def test_postgres_uses_jsonb_and_timestamptz() -> None:
    from sqlalchemy.dialects import postgresql

    ddl = str(CreateTable(Base.metadata.tables["runs"]).compile(dialect=postgresql.dialect()))
    assert "doc JSONB NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl


def test_constraints_have_stable_names() -> None:
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            assert constraint.name, f"unnamed constraint on {table.name}: {constraint}"


def test_module_exports_every_table_class() -> None:
    classes = {
        name
        for name in dir(tables)
        if name.startswith("Db") and isinstance(getattr(tables, name), type)
    }
    assert classes <= set(tables.__all__)
    assert {t.__tablename__ for t in map(tables.__dict__.get, classes)} == set(Base.metadata.tables)
