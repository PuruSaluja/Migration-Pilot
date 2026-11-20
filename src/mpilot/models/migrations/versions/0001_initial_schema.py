"""Initial schema: a table for every SPEC §12 model, plus checkpoints, final verification,
migration notes and the escalation report (P1-09; amended at the P1-12 gate, before any
database existed outside tests: attempt numbering, commit-bound integrity reports and
Critic verdicts, and the resume fields of tool and role calls).

Generated with Alembic autogenerate from `mpilot.models.tables`, then frozen: it imports no
application code, so later changes to `tables.py` never alter what this revision does. From
the Phase 1 gate on, changes are new revisions. A test compares the migrated schema with
`tables.py`.

Revision ID: 0001
Revises:
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_results",
        sa.Column("suite", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("system", sa.String(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("success_level", sa.String(), nullable=False),
        sa.Column("failure_category", sa.String(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "success_level IN ('none', 'L0', 'L1', 'L2', 'L3', 'L4')",
            name=op.f("ck_eval_results_success_level_known"),
        ),
        sa.CheckConstraint("seed >= 0", name=op.f("ck_eval_results_seed_nonneg")),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_eval_results")),
    )
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.create_index(
            "ix_eval_results_suite_task_id_system_seed",
            ["suite", "task_id", "system", "seed"],
            unique=False,
        )

    op.create_table(
        "runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("repo_url", sa.String(), nullable=False),
        sa.Column("base_sha", sa.String(), nullable=True),
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("paused_at_state", sa.String(), nullable=True),
        sa.Column("config_hash", sa.String(), nullable=False),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.CheckConstraint(
            "(status = 'PAUSED_LIMIT') = (paused_at_state IS NOT NULL)",
            name=op.f("ck_runs_paused_state_iff_paused"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index("ix_runs_created_at", ["created_at"], unique=False)
        batch_op.create_index("ix_runs_status", ["status"], unique=False)

    op.create_table(
        "attempts",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("n_in_step", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("diff_sha", sa.String(), nullable=False),
        sa.Column("signature", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('improved', 'same', 'regressed', 'green')",
            name=op.f("ck_attempts_outcome_known"),
        ),
        sa.CheckConstraint(
            "n > 0 AND n_in_step > 0 AND n_in_step <= n", name=op.f("ck_attempts_numbers_valid")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_attempts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_attempts")),
        sa.UniqueConstraint("id", name=op.f("uq_attempts_id")),
        sa.UniqueConstraint("run_id", "n", name=op.f("uq_attempts_run_id_n")),
        sa.UniqueConstraint(
            "run_id", "step_id", "n_in_step", name=op.f("uq_attempts_run_id_step_id_n_in_step")
        ),
    )
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.create_index("ix_attempts_run_id_seq", ["run_id", "seq"], unique=False)
        batch_op.create_index("ix_attempts_step_id", ["step_id"], unique=False)

    op.create_table(
        "baselines",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("test_count", sa.Integer(), nullable=False),
        sa.Column("coverage_pct", sa.Float(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.CheckConstraint("test_count >= 0", name=op.f("ck_baselines_test_count_nonneg")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_baselines_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_baselines")),
    )
    op.create_table(
        "checkpoints",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("paused_at_state", sa.String(), nullable=True),
        sa.Column("step_id", sa.String(), nullable=True),
        sa.Column("attempt_n", sa.Integer(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.CheckConstraint(
            "(status = 'PAUSED_LIMIT') = (paused_at_state IS NOT NULL)",
            name=op.f("ck_checkpoints_paused_state_iff_paused"),
        ),
        sa.CheckConstraint("seq >= 0", name=op.f("ck_checkpoints_seq_nonneg")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_checkpoints_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_checkpoints")),
    )
    op.create_table(
        "critic_verdicts",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("mirror_commit", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approve', 'revise', 'escalate')",
            name=op.f("ck_critic_verdicts_decision_known"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_critic_verdicts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_critic_verdicts")),
    )
    with op.batch_alter_table("critic_verdicts", schema=None) as batch_op:
        batch_op.create_index("ix_critic_verdicts_run_id_seq", ["run_id", "seq"], unique=False)

    op.create_table(
        "escalation_reports",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_md", sa.Text(), nullable=False),
        sa.Column("data", JSON_DOC, nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_escalation_reports_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_escalation_reports")),
    )
    op.create_table(
        "final_verifications",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("mirror_commit", sa.String(), nullable=False),
        sa.Column("cache", sa.String(), nullable=False),
        sa.Column("runner_exit_code", sa.Integer(), nullable=True),
        sa.Column("coverage_pct", sa.Float(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "cache IN ('fresh', 'hash_verified')", name=op.f("ck_final_verifications_cache_known")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_final_verifications_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_final_verifications")),
    )
    with op.batch_alter_table("final_verifications", schema=None) as batch_op:
        batch_op.create_index(
            "ix_final_verifications_mirror_commit", ["mirror_commit"], unique=False
        )
        batch_op.create_index("ix_final_verifications_run_id_seq", ["run_id", "seq"], unique=False)

    op.create_table(
        "integrity_reports",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("mirror_commit", sa.String(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("test_count_before", sa.Integer(), nullable=False),
        sa.Column("test_count_after", sa.Integer(), nullable=False),
        sa.Column("coverage_delta_pp", sa.Float(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "test_count_before >= 0 AND test_count_after >= 0",
            name=op.f("ck_integrity_reports_counts_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_integrity_reports_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_integrity_reports")),
    )
    with op.batch_alter_table("integrity_reports", schema=None) as batch_op:
        batch_op.create_index("ix_integrity_reports_run_id_seq", ["run_id", "seq"], unique=False)

    op.create_table(
        "migration_notes",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(key) > 0", name=op.f("ck_migration_notes_key_nonempty")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_migration_notes_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_migration_notes")),
    )
    with op.batch_alter_table("migration_notes", schema=None) as batch_op:
        batch_op.create_index(
            "ix_migration_notes_run_id_key_seq", ["run_id", "key", "seq"], unique=False
        )

    op.create_table(
        "plans",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_plans_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_plans")),
        sa.UniqueConstraint("id", name=op.f("uq_plans_id")),
    )
    op.create_table(
        "pull_requests",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("draft", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.CheckConstraint("number > 0", name=op.f("ck_pull_requests_number_positive")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_pull_requests_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_pull_requests")),
    )
    op.create_table(
        "routing_decisions",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("policy", sa.String(), nullable=False),
        sa.Column("effort_policy", sa.String(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("effort", sa.String(), nullable=False),
        sa.Column("sticky", sa.Boolean(), nullable=False),
        sa.Column("inputs_hash", sa.String(), nullable=False),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "NOT (tier = 'opus' AND effort = 'medium')",
            name=op.f("ck_routing_decisions_opus_never_medium"),
        ),
        sa.CheckConstraint(
            "effort IN ('medium', 'high')", name=op.f("ck_routing_decisions_effort_known")
        ),
        sa.CheckConstraint(
            "effort_policy IN ('routed', 'all_high')",
            name=op.f("ck_routing_decisions_effort_policy_known"),
        ),
        sa.CheckConstraint(
            "policy IN ('routed', 'all_opus', 'all_sonnet')",
            name=op.f("ck_routing_decisions_policy_known"),
        ),
        sa.CheckConstraint(
            "tier IN ('opus', 'sonnet')", name=op.f("ck_routing_decisions_tier_known")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_routing_decisions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_routing_decisions")),
        sa.UniqueConstraint("id", name=op.f("uq_routing_decisions_id")),
    )
    with op.batch_alter_table("routing_decisions", schema=None) as batch_op:
        batch_op.create_index("ix_routing_decisions_run_id_seq", ["run_id", "seq"], unique=False)

    op.create_table(
        "test_results",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("attempt_id", sa.String(), nullable=True),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage IN ('baseline', 'targeted', 'full', 'final')",
            name=op.f("ck_test_results_stage_known"),
        ),
        sa.CheckConstraint(
            "passed >= 0 AND failed >= 0 AND skipped >= 0 AND errors >= 0 AND duration_s >= 0",
            name=op.f("ck_test_results_counts_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_test_results_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_test_results")),
    )
    with op.batch_alter_table("test_results", schema=None) as batch_op:
        batch_op.create_index(
            "ix_test_results_run_id_stage_seq", ["run_id", "stage", "seq"], unique=False
        )

    op.create_table(
        "tool_calls",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("role_call_id", sa.String(), nullable=False),
        sa.Column("attempt_id", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("policy_decision", sa.String(), nullable=False),
        sa.Column("deny_reason", sa.Text(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(policy_decision = 'deny') = (deny_reason IS NOT NULL)",
            name=op.f("ck_tool_calls_deny_reason_iff_deny"),
        ),
        sa.CheckConstraint(
            "policy_decision IN ('allow', 'deny')", name=op.f("ck_tool_calls_decision_known")
        ),
        sa.CheckConstraint("duration_ms >= 0", name=op.f("ck_tool_calls_duration_nonneg")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_tool_calls_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_tool_calls")),
        sa.UniqueConstraint("id", name=op.f("uq_tool_calls_id")),
    )
    with op.batch_alter_table("tool_calls", schema=None) as batch_op:
        batch_op.create_index("ix_tool_calls_attempt_id", ["attempt_id"], unique=False)
        batch_op.create_index("ix_tool_calls_role_call_id", ["role_call_id"], unique=False)
        batch_op.create_index("ix_tool_calls_run_id_seq", ["run_id", "seq"], unique=False)

    op.create_table(
        "llm_calls",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("step_id", sa.String(), nullable=True),
        sa.Column("attempt_id", sa.String(), nullable=True),
        sa.Column("parent_call_id", sa.String(), nullable=True),
        sa.Column("runtime", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("effort", sa.String(), nullable=False),
        sa.Column("routing_decision_id", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("thinking", sa.Boolean(), nullable=False),
        sa.Column("api_equiv_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("stop", sa.String(), nullable=True),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("limit_error", sa.Text(), nullable=True),
        sa.Column("doc", JSON_DOC, nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "NOT (tier = 'opus' AND effort = 'medium')", name=op.f("ck_llm_calls_opus_never_medium")
        ),
        sa.CheckConstraint("effort IN ('medium', 'high')", name=op.f("ck_llm_calls_effort_known")),
        sa.CheckConstraint("lower(model) NOT LIKE '%haiku%'", name=op.f("ck_llm_calls_no_haiku")),
        sa.CheckConstraint("tier IN ('opus', 'sonnet')", name=op.f("ck_llm_calls_tier_known")),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 AND cache_write_tokens >= 0 AND api_equiv_usd >= 0 AND latency_ms >= 0",
            name=op.f("ck_llm_calls_usage_nonneg"),
        ),
        sa.CheckConstraint("turns >= 0", name=op.f("ck_llm_calls_turns_nonneg")),
        sa.CheckConstraint(
            "stop IS NULL OR stop IN ('completed', 'schema_invalid', 'max_turns', 'budget_stop', 'tool_call_cap')",
            name=op.f("ck_llm_calls_stop_known"),
        ),
        sa.CheckConstraint(
            "(stop IS NULL) = (limit_error IS NOT NULL)", name=op.f("ck_llm_calls_stop_xor_limit")
        ),
        sa.CheckConstraint(
            "(parent_call_id IS NOT NULL) = (role = 'explorer')",
            name=op.f("ck_llm_calls_parent_iff_explorer"),
        ),
        sa.CheckConstraint(
            "attempt_id IS NULL OR step_id IS NOT NULL",
            name=op.f("ck_llm_calls_attempt_needs_step"),
        ),
        sa.ForeignKeyConstraint(
            ["routing_decision_id"],
            ["routing_decisions.id"],
            name=op.f("fk_llm_calls_routing_decision_id_routing_decisions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_llm_calls_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_llm_calls")),
        sa.UniqueConstraint("id", name=op.f("uq_llm_calls_id")),
    )
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.create_index("ix_llm_calls_attempt_id", ["attempt_id"], unique=False)
        batch_op.create_index(
            "ix_llm_calls_routing_decision_id", ["routing_decision_id"], unique=False
        )
        batch_op.create_index("ix_llm_calls_run_id_seq", ["run_id", "seq"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_calls_run_id_seq")
        batch_op.drop_index("ix_llm_calls_routing_decision_id")
        batch_op.drop_index("ix_llm_calls_attempt_id")

    op.drop_table("llm_calls")
    with op.batch_alter_table("tool_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_tool_calls_run_id_seq")
        batch_op.drop_index("ix_tool_calls_role_call_id")
        batch_op.drop_index("ix_tool_calls_attempt_id")

    op.drop_table("tool_calls")
    with op.batch_alter_table("test_results", schema=None) as batch_op:
        batch_op.drop_index("ix_test_results_run_id_stage_seq")

    op.drop_table("test_results")
    with op.batch_alter_table("routing_decisions", schema=None) as batch_op:
        batch_op.drop_index("ix_routing_decisions_run_id_seq")

    op.drop_table("routing_decisions")
    op.drop_table("pull_requests")
    op.drop_table("plans")
    with op.batch_alter_table("migration_notes", schema=None) as batch_op:
        batch_op.drop_index("ix_migration_notes_run_id_key_seq")

    op.drop_table("migration_notes")
    with op.batch_alter_table("integrity_reports", schema=None) as batch_op:
        batch_op.drop_index("ix_integrity_reports_run_id_seq")

    op.drop_table("integrity_reports")
    with op.batch_alter_table("final_verifications", schema=None) as batch_op:
        batch_op.drop_index("ix_final_verifications_run_id_seq")
        batch_op.drop_index("ix_final_verifications_mirror_commit")

    op.drop_table("final_verifications")
    op.drop_table("escalation_reports")
    with op.batch_alter_table("critic_verdicts", schema=None) as batch_op:
        batch_op.drop_index("ix_critic_verdicts_run_id_seq")

    op.drop_table("critic_verdicts")
    op.drop_table("checkpoints")
    op.drop_table("baselines")
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_attempts_step_id")
        batch_op.drop_index("ix_attempts_run_id_seq")

    op.drop_table("attempts")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index("ix_runs_status")
        batch_op.drop_index("ix_runs_created_at")

    op.drop_table("runs")
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.drop_index("ix_eval_results_suite_task_id_system_seed")

    op.drop_table("eval_results")
