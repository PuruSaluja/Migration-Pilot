"""P1-04: §12 domain models: fields, JSON round trip, invariants, schema snapshot."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from syrupy.assertion import SnapshotAssertion

from mpilot.core.types import (
    AttemptId,
    Effort,
    EffortPolicy,
    FailureCategory,
    LLMCallId,
    Outcome,
    PlanId,
    Role,
    RoutingDecisionId,
    RoutingPolicy,
    RunId,
    RunStatus,
    StepId,
    StepType,
    StopReason,
    Tier,
    ToolCallId,
)
from mpilot.models import domain as d

SPEC = Path(__file__).resolve().parents[2] / "SPEC.md"
RUN = RunId("run-000000000001")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
SHA = "a" * 40


# ---------------------------------------------------------------- sample instances


def _spec() -> d.MigrationSpec:
    return d.MigrationSpec(
        kind=d.MigrationKind.FAMILY_BUMP,
        ecosystem=d.Ecosystem.NODE,
        packages=[
            d.PackageChange(name="react", from_range="^17.0.2", to_version="18.3.1"),
            d.PackageChange(name="moment", to_version="2.30.1", replaces="date-fns"),
        ],
        goal_text="upgrade react to 18",
        assumptions=["target resolved to latest stable"],
    )


def _env() -> d.EnvProfile:
    return d.EnvProfile(
        language=d.Ecosystem.NODE,
        runtime_version="20.11.0",
        package_manager="npm",
        install_cmd="npm ci",
        build_cmd="npm run build",
        test_cmd="npm test",
        image_digest="sha256:" + "b" * 64,
        lockfile_hashes={"package-lock.json": "c" * 64},
        detection_sources={"test_cmd": "heuristic"},
    )


def _tests(stage: d.TestStage = d.TestStage.BASELINE) -> d.TestResult:
    return d.TestResult(
        run_id=RUN,
        attempt_id=AttemptId("att-1"),
        stage=stage,
        passed=2,
        failed=1,
        skipped=1,
        errors=0,
        passed_ids=frozenset({"b::t2", "a::t1"}),
        failed_ids=frozenset({"c::t3"}),
        duration_s=1.5,
        failures=[d.FailedTest(test_id="c::t3", message="boom", trace_excerpt="line 1")],
        junit_path="runs/x/junit.xml",
    )


def _verify() -> d.VerifyResult:
    return d.VerifyResult(
        stages=[
            d.VerifyStageResult(stage=d.VerifyStage.INSTALL, passed=True, exit_code=0),
            d.VerifyStageResult(
                stage=d.VerifyStage.BUILD, passed=False, exit_code=1, failure_signature="sig"
            ),
        ],
        tests=_tests(d.TestStage.TARGETED),
    )


def _diagnosis() -> d.Diagnosis:
    return d.Diagnosis(
        signature="sig",
        category=FailureCategory.API_REMOVED,
        evidence="TypeError: x is not a function",
        hypotheses=["h1", "h2"],
        files=["src/a.ts"],
        changelog_refs=["https://example.test/guide#removed"],
        confidence=0.8,
    )


def _llm_call(**over: Any) -> d.LLMCall:
    fields: dict[str, Any] = {
        "id": LLMCallId("llm-1"),
        "run_id": RUN,
        "role": Role.EDITOR,
        "step_id": StepId("step-1"),
        "attempt_id": AttemptId("att-1"),
        "runtime": "fake",
        "model": "claude-sonnet-5-5",
        "tier": Tier.SONNET,
        "effort": Effort.MEDIUM,
        "routing_decision_id": RoutingDecisionId("rd-1"),
        "stop": StopReason.COMPLETED,
        "turns": 3,
        "output": {"edits": [{"path": "a.ts"}], "rationale": "rename", "confidence": 0.9},
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 100,
        "cache_write_tokens": 20,
        "thinking": True,
        "api_equiv_usd": 0.0,
        "latency_ms": 1200,
    }
    return d.LLMCall(**(fields | over))


def _routing(**over: Any) -> d.RoutingDecision:
    fields: dict[str, Any] = {
        "id": RoutingDecisionId("rd-1"),
        "run_id": RUN,
        "step_id": StepId("step-1"),
        "role": Role.EDITOR,
        "policy": RoutingPolicy.ROUTED,
        "effort_policy": EffortPolicy.ROUTED,
        "tier": Tier.OPUS,
        "effort": Effort.HIGH,
        "reason_codes": ["second_attempt_same_signature"],
        "escalated_from": Tier.SONNET,
        "effort_escalated_from": Effort.MEDIUM,
        "sticky": True,
        "inputs_hash": "h" * 8,
    }
    return d.RoutingDecision(**(fields | over))


def _integrity(**over: Any) -> d.IntegrityReport:
    fields: dict[str, Any] = {
        "run_id": RUN,
        "mirror_commit": SHA,
        "checks": ["integrity.a", "integrity.c"],
        "passed": False,
        "violations": [
            d.Violation(check_id="integrity.c", code="test_deleted", message="x", path="t.py")
        ],
        "coverage_delta_pp": -0.5,
        "test_count_before": 10,
        "test_count_after": 9,
        "resolved_versions": {"react": "18.3.1"},
        "removed_api_usages_remaining": ["src/a.ts:3 ReactDOM.render"],
    }
    return d.IntegrityReport(**(fields | over))


def _run(**over: Any) -> d.Run:
    fields: dict[str, Any] = {
        "id": RUN,
        "created_at": NOW,
        "status": RunStatus.PAUSED_LIMIT,
        "repo_url": "https://github.com/PuruSaluja/x",
        "base_sha": SHA,
        "branch": "mpilot/react-1",
        "spec": _spec(),
        "env": _env(),
        "budgets": d.Budgets(
            max_turns=300,
            max_tokens=8_000_000,
            minutes_cap=120,
            max_attempts_run=40,
            max_attempts_step=6,
        ),
        "spent": d.Spent(turns=3, input_tokens=10, api_equiv_usd=0.5, sandbox_seconds=12.5),
        "terminal_reason": None,
        "paused_at_state": RunStatus.REPAIR_LOOP,
        "config_hash": "cfg",
    }
    return d.Run(**(fields | over))


SAMPLES: dict[type[d.DomainModel], Callable[[], d.DomainModel]] = {
    d.PackageChange: lambda: _spec().packages[0],
    d.MigrationSpec: _spec,
    d.EnvProfile: _env,
    d.Budgets: lambda: _run().budgets,
    d.Spent: lambda: _run().spent,
    d.Run: _run,
    d.FailedTest: lambda: _tests().failures[0],
    d.TestResult: _tests,
    d.VerifyStageResult: lambda: _verify().stages[1],
    d.VerifyResult: _verify,
    d.Baseline: lambda: d.Baseline(
        run_id=RUN,
        runs=[_tests(), _tests()],
        passing_ids=frozenset({"a::t1", "b::t2"}),
        flaky_ids=frozenset({"f::t9"}),
        test_file_hashes={"tests/a.test.ts": "d" * 64},
        test_count=4,
        coverage_pct=81.5,
    ),
    d.Step: lambda: d.Step(
        id=StepId("step-1"),
        order=1,
        type=StepType.EDIT_CLUSTER,
        description="fix render",
        target_files=["src/a.ts"],
        rationale="removed API",
        changelog_refs=["u"],
        status=d.StepStatus.RUNNING,
        attempts=2,
    ),
    d.Plan: lambda: d.Plan(
        id=PlanId("plan-1"),
        run_id=RUN,
        steps=[
            d.Step(
                id=StepId("step-1"), order=1, type=StepType.BUMP, description="bump", rationale="r"
            )
        ],
        risks=["peer deps"],
        changelog_refs=["u"],
        approved=True,
    ),
    d.Diagnosis: _diagnosis,
    d.Attempt: lambda: d.Attempt(
        id=AttemptId("att-1"),
        run_id=RUN,
        step_id=StepId("step-1"),
        n=3,
        n_in_step=1,
        diagnosis=_diagnosis(),
        diff_sha="e" * 16,
        verify=_verify(),
        outcome=Outcome.IMPROVED,
    ),
    d.ToolCall: lambda: d.ToolCall(
        id=ToolCallId("tc-1"),
        run_id=RUN,
        role_call_id=LLMCallId("llm-1"),
        attempt_id=AttemptId("att-1"),
        role=Role.EDITOR,
        tool="str_replace",
        input={"path": "a.ts", "old_str": "x", "new_str": "y", "n": [1, {"k": None}]},
        output_summary="ok",
        duration_ms=4,
        policy_decision=d.PolicyDecision.DENY,
        deny_reason="test file",
    ),
    d.LLMCall: _llm_call,
    d.RoutingDecision: _routing,
    d.Violation: lambda: _integrity().violations[0],
    d.IntegrityReport: _integrity,
    d.CriticVerdict: lambda: d.CriticVerdict(
        run_id=RUN,
        mirror_commit=SHA,
        decision=d.CriticDecision.REVISE,
        findings=["swallowed exception"],
        hunk_mapping={"src/a.ts@10": "step-1"},
    ),
    d.PullRequest: lambda: d.PullRequest(
        run_id=RUN,
        number=7,
        url="https://github.com/o/r/pull/7",
        branch="mpilot/react-1",
        draft=True,
        commits=[SHA, "b" * 40],
        created_at=NOW,
    ),
    d.RubricScores: lambda: d.RubricScores(
        correctness=5, minimality=4, idiomatic_use=4, explanation=5
    ),
    d.HumanReview: lambda: d.HumanReview(
        merge_ready=True,
        rubric=d.RubricScores(correctness=5, minimality=4, idiomatic_use=4, explanation=5),
        reviewer="owner",
    ),
    d.EvalResult: lambda: d.EvalResult(
        suite="v1",
        task_id="react-17-18",
        system="full",
        seed=0,
        status=RunStatus.END_SUCCESS,
        success_level=d.SuccessLevel.L3,
        turns=40,
        tokens=1_000_000,
        api_equiv_usd=12.3,
        wall_s=900.0,
        attempts=5,
        llm_calls=30,
        routing_policy=RoutingPolicy.ROUTED,
        effort_policy=EffortPolicy.ROUTED,
        opus_token_share=0.7,
        medium_effort_share=0.1,
        failure_category=None,
        integrity_violations_blocked=1,
        human_review=d.HumanReview(
            merge_ready=False,
            rubric=d.RubricScores(correctness=3, minimality=3, idiomatic_use=3, explanation=3),
            reviewer="peer",
        ),
    ),
}


def _all_models() -> list[type[d.DomainModel]]:
    return [
        obj
        for obj in vars(d).values()
        if isinstance(obj, type) and issubclass(obj, d.DomainModel) and obj is not d.DomainModel
    ]


def test_every_model_has_a_sample() -> None:
    assert set(_all_models()) == set(SAMPLES)


@pytest.mark.parametrize("model", list(SAMPLES), ids=lambda m: m.__name__)
def test_json_round_trip(model: type[d.DomainModel]) -> None:
    original = SAMPLES[model]()
    text = original.model_dump_json()
    assert model.model_validate_json(text) == original
    # dump(mode="json") -> validate -> dump gives the same JSON again: deterministic
    again = model.model_validate(original.model_dump(mode="json"))
    assert again.model_dump_json() == text


@pytest.mark.parametrize("model", list(SAMPLES), ids=lambda m: m.__name__)
def test_unknown_fields_are_rejected(model: type[d.DomainModel]) -> None:
    payload = SAMPLES[model]().model_dump(mode="json") | {"surprise": 1}
    with pytest.raises(ValidationError, match="surprise"):
        model.model_validate(payload)


# ---------------------------------------------------------------- §12 fields are all present

SECTION_12_FIELDS: dict[str, set[str]] = {
    "MigrationSpec": {"kind", "ecosystem", "packages", "goal_text", "assumptions"},
    "Run": {
        "id",
        "created_at",
        "status",
        "repo_url",
        "base_sha",
        "branch",
        "spec",
        "env",
        "budgets",
        "spent",
        "terminal_reason",
        "paused_at_state",
        "config_hash",
    },
    "EnvProfile": {
        "language",
        "runtime_version",
        "package_manager",
        "install_cmd",
        "build_cmd",
        "typecheck_cmd",
        "test_cmd",
        "image_digest",
        "lockfile_hashes",
        "detection_sources",
    },
    "Baseline": {
        "runs",
        "passing_ids",
        "flaky_ids",
        "test_file_hashes",
        "test_count",
        "coverage_pct",
    },
    "Plan": {"id", "run_id", "steps", "risks", "changelog_refs", "approved"},
    "Step": {
        "id",
        "order",
        "type",
        "description",
        "target_files",
        "rationale",
        "status",
        "attempts",
    },
    "Attempt": {"id", "step_id", "n", "n_in_step", "diagnosis", "diff_sha", "verify", "outcome"},
    "Diagnosis": {"signature", "category", "evidence", "hypotheses", "files", "changelog_refs"},
    "ToolCall": {
        "id",
        "role_call_id",
        "attempt_id",
        "role",
        "tool",
        "input",
        "output_summary",
        "duration_ms",
        "policy_decision",
        "deny_reason",
    },
    "LLMCall": {
        "id",
        "role",
        "step_id",
        "attempt_id",
        "parent_call_id",
        "runtime",
        "model",
        "tier",
        "effort",
        "routing_decision_id",
        "stop",
        "turns",
        "output",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "thinking",
        "api_equiv_usd",
        "latency_ms",
        "limit_error",
    },
    "RoutingDecision": {
        "id",
        "run_id",
        "step_id",
        "role",
        "policy",
        "effort_policy",
        "tier",
        "effort",
        "reason_codes",
        "escalated_from",
        "effort_escalated_from",
        "sticky",
        "inputs_hash",
    },
    "TestResult": {
        "run_id",
        "attempt_id",
        "stage",
        "passed",
        "failed",
        "skipped",
        "errors",
        "passed_ids",
        "failed_ids",
        "duration_s",
        "failures",
        "junit_path",
    },
    "IntegrityReport": {
        "mirror_commit",
        "checks",
        "passed",
        "violations",
        "coverage_delta_pp",
        "test_count_before",
        "test_count_after",
        "resolved_versions",
        "removed_api_usages_remaining",
    },
    "CriticVerdict": {"mirror_commit", "decision", "findings", "hunk_mapping"},
    "PullRequest": {"run_id", "number", "url", "branch", "draft", "commits", "created_at"},
    "EvalResult": {
        "suite",
        "task_id",
        "system",
        "seed",
        "status",
        "success_level",
        "turns",
        "tokens",
        "api_equiv_usd",
        "wall_s",
        "attempts",
        "llm_calls",
        "routing_policy",
        "effort_policy",
        "opus_token_share",
        "medium_effort_share",
        "failure_category",
        "integrity_violations_blocked",
        "human_review",
    },
}


def test_table_lists_exactly_the_classes_in_spec_section_12() -> None:
    text = SPEC.read_text(encoding="utf-8")
    block = text.split("## 12. Data models", 1)[1].split("```", 2)[1]
    assert set(re.findall(r"^class (\w+)", block, re.MULTILINE)) == set(SECTION_12_FIELDS)


@pytest.mark.parametrize(("name", "fields"), SECTION_12_FIELDS.items())
def test_every_section_12_field_is_present(name: str, fields: set[str]) -> None:
    model = getattr(d, name)
    assert fields <= set(model.model_fields), fields - set(model.model_fields)


def test_nested_section_12_value_types() -> None:
    assert {"name", "from_range", "to_version", "replaces"} == set(d.PackageChange.model_fields)
    assert {
        "max_turns",
        "max_tokens",
        "minutes_cap",
        "max_attempts_run",
        "max_attempts_step",
    } == set(d.Budgets.model_fields)
    assert {
        "turns",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "api_equiv_usd",
        "sandbox_seconds",
    } == set(d.Spent.model_fields)
    assert {"merge_ready", "rubric", "reviewer"} == set(d.HumanReview.model_fields)
    assert {"test_id", "message", "trace_excerpt"} == set(d.FailedTest.model_fields)


# ---------------------------------------------------------------- D9: ids on TestResult


def test_test_result_carries_ids_and_executed_count() -> None:
    result = _tests()
    assert result.passed_ids == {"a::t1", "b::t2"}
    assert result.failed_ids == {"c::t3"}
    assert result.executed_count == 3  # passed + failed + errors, skipped excluded
    assert (
        d.TestResult.model_validate_json(result.model_dump_json()).passed_ids == result.passed_ids
    )


def test_ids_serialize_sorted() -> None:
    dumped = _tests().model_dump(mode="json")
    assert dumped["passed_ids"] == ["a::t1", "b::t2"]


def test_a_test_cannot_pass_and_fail() -> None:
    with pytest.raises(ValidationError, match="both passed and failed"):
        d.TestResult(
            run_id=RUN,
            stage=d.TestStage.FULL,
            passed=1,
            failed=1,
            skipped=0,
            errors=0,
            passed_ids=frozenset({"x"}),
            failed_ids=frozenset({"x"}),
            duration_s=0,
        )


@given(
    passed=st.frozensets(st.text(min_size=1, max_size=8), max_size=20),
    failed=st.frozensets(st.text(min_size=1, max_size=8), max_size=20),
)
def test_test_result_round_trips_for_any_disjoint_ids(
    passed: frozenset[str], failed: frozenset[str]
) -> None:
    failed = failed - passed
    result = d.TestResult(
        run_id=RUN,
        stage=d.TestStage.FINAL,
        passed=len(passed),
        failed=len(failed),
        skipped=0,
        errors=0,
        passed_ids=passed,
        failed_ids=failed,
        duration_s=0.1,
    )
    assert d.TestResult.model_validate_json(result.model_dump_json()) == result


# ---------------------------------------------------------------- NFR-12: tier and effort


@pytest.mark.parametrize("model", [d.LLMCall, d.RoutingDecision])
@pytest.mark.parametrize(
    ("field", "bad"), [("tier", "haiku"), ("effort", "low"), ("effort", "max")]
)
def test_invalid_tier_or_effort_is_rejected(
    model: type[d.DomainModel], field: str, bad: str
) -> None:
    payload = SAMPLES[model]().model_dump(mode="json") | {field: bad}
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_opus_at_medium_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Opus never runs at medium"):
        _llm_call(tier=Tier.OPUS, effort=Effort.MEDIUM, model="claude-opus-5-5")
    with pytest.raises(ValidationError, match="Opus never runs at medium"):
        _routing(tier=Tier.OPUS, effort=Effort.MEDIUM, effort_escalated_from=None)


def test_sonnet_at_medium_and_high_are_fine() -> None:
    _llm_call(effort=Effort.MEDIUM)
    _llm_call(effort=Effort.HIGH)


@pytest.mark.parametrize("name", ["claude-haiku-4-5", "HAIKU", "x-Haiku-y"])
def test_haiku_model_id_is_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="Haiku"):
        _llm_call(model=name)


def test_escalation_fields_must_match_the_chosen_tier_and_effort() -> None:
    with pytest.raises(ValidationError, match="tier must be opus"):
        _routing(tier=Tier.SONNET, effort=Effort.MEDIUM, effort_escalated_from=None)
    with pytest.raises(ValidationError, match="effort must be high"):
        _routing(tier=Tier.SONNET, effort=Effort.MEDIUM, escalated_from=None)
    with pytest.raises(ValidationError):
        _routing(escalated_from=Tier.OPUS)  # you can only escalate from sonnet


# ---------------------------------------------------------------- other invariants


def test_tool_call_denial_needs_a_reason() -> None:
    sample = SAMPLES[d.ToolCall]().model_dump(mode="json")
    with pytest.raises(ValidationError, match="deny_reason"):
        d.ToolCall.model_validate(sample | {"deny_reason": None})
    with pytest.raises(ValidationError, match="cannot have deny_reason"):
        d.ToolCall.model_validate(sample | {"policy_decision": "allow"})


def test_integrity_report_passed_matches_violations() -> None:
    with pytest.raises(ValidationError, match="exactly when"):
        _integrity(passed=True)
    with pytest.raises(ValidationError, match="exactly when"):
        _integrity(passed=False, violations=[])
    assert _integrity(passed=True, violations=[]).passed


def test_run_pause_state_rules() -> None:
    with pytest.raises(ValidationError, match="must record paused_at_state"):
        _run(paused_at_state=None)
    with pytest.raises(ValidationError, match="resume into"):
        _run(paused_at_state=RunStatus.END_ERROR)
    with pytest.raises(ValidationError, match="resume into"):
        _run(paused_at_state=RunStatus.PAUSED_LIMIT)
    assert _run(status=RunStatus.INIT, paused_at_state=None, base_sha=None).base_sha is None


@pytest.mark.parametrize("sha", ["abc", "A" * 40, "g" * 40, "a" * 41])
def test_base_sha_must_be_a_full_hex_id(sha: str) -> None:
    with pytest.raises(ValidationError, match="base_sha"):
        _run(base_sha=sha)


def test_baseline_flaky_and_passing_are_disjoint() -> None:
    sample = SAMPLES[d.Baseline]().model_dump(mode="json")
    with pytest.raises(ValidationError, match="flaky"):
        d.Baseline.model_validate(sample | {"flaky_ids": ["a::t1"]})


def test_diagnosis_limits() -> None:
    sample = _diagnosis().model_dump(mode="json")
    with pytest.raises(ValidationError):
        d.Diagnosis.model_validate(sample | {"hypotheses": ["1", "2", "3", "4"]})
    with pytest.raises(ValidationError):
        d.Diagnosis.model_validate(sample | {"confidence": 1.5})
    with pytest.raises(ValidationError):
        d.Diagnosis.model_validate(sample | {"category": "MADE_UP"})


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        _run(created_at=datetime(2026, 1, 1))


def test_negative_counts_and_bad_shares_are_rejected() -> None:
    with pytest.raises(ValidationError):
        d.Spent(turns=-1)
    sample = SAMPLES[d.EvalResult]().model_dump(mode="json")
    with pytest.raises(ValidationError):
        d.EvalResult.model_validate(sample | {"opus_token_share": 1.2})
    with pytest.raises(ValidationError):
        d.RubricScores(correctness=0, minimality=1, idiomatic_use=1, explanation=1)


def test_assignment_is_validated() -> None:
    run = _run()
    with pytest.raises(ValidationError):
        run.status = "NOT_A_STATE"  # type: ignore[assignment]


def test_migration_spec_needs_a_package() -> None:
    with pytest.raises(ValidationError):
        d.MigrationSpec(
            kind=d.MigrationKind.BUMP, ecosystem=d.Ecosystem.PYTHON, packages=[], goal_text="x"
        )


# ---------------------------------------------------------------- schema snapshot


def test_json_schemas_snapshot(snapshot: SnapshotAssertion) -> None:
    schemas = {m.__name__: m.model_json_schema() for m in sorted(SAMPLES, key=lambda m: m.__name__)}
    assert schemas == snapshot


# ---------------------------------------------------------------- P1-12 gate: numbering, binding


def test_an_attempt_is_numbered_run_wide_and_within_its_step() -> None:
    base = SAMPLES[d.Attempt]()
    assert isinstance(base, d.Attempt)
    d.Attempt.model_validate(base.model_dump() | {"n": 7, "n_in_step": 7})
    with pytest.raises(ValidationError, match="cannot exceed"):
        d.Attempt.model_validate(base.model_dump() | {"n": 2, "n_in_step": 3})
    with pytest.raises(ValidationError):
        d.Attempt.model_validate(base.model_dump() | {"n_in_step": 0})


@pytest.mark.parametrize("bad", ["", "abc", "A" * 40, "a" * 39, "a" * 41])
def test_reports_and_verdicts_name_a_full_commit(bad: str) -> None:
    with pytest.raises(ValidationError):
        _integrity(mirror_commit=bad)
    verdict = SAMPLES[d.CriticVerdict]()
    with pytest.raises(ValidationError):
        d.CriticVerdict.model_validate(verdict.model_dump() | {"mirror_commit": bad})


def test_an_integrity_report_lists_the_checks_that_ran() -> None:
    with pytest.raises(ValidationError):
        _integrity(checks=[])
    with pytest.raises(ValidationError, match="sorted and unique"):
        _integrity(checks=["integrity.c", "integrity.a"])
    with pytest.raises(ValidationError, match="sorted and unique"):
        _integrity(checks=["integrity.a", "integrity.a"])


def test_a_sha_with_a_trailing_newline_is_not_a_sha() -> None:
    """`re.match` with `$` accepts "...\\n"; every SHA check uses fullmatch (P1-12 review)."""
    from mpilot.models import artifacts as art

    with pytest.raises(ValidationError, match="full lowercase hex"):
        _run(base_sha=SHA + "\n")
    with pytest.raises(ValueError, match="full lowercase hex"):
        art._check_git_sha(SHA + "\n")
    with pytest.raises(ValidationError):
        _integrity(mirror_commit=SHA + "\n")
