"""Pydantic v2 domain models for every SPEC §12 class.

Conventions:
- Every model forbids unknown fields and validates on assignment.
- IDs are the `NewType` strings from `core.types`; enums are the closed ones from there too,
  so a Haiku tier or a `low` effort cannot be represented (NFR-12).
- Sets of test IDs serialize as sorted lists, so JSON is deterministic.
- Fields beyond the §12 sketch, each justified in PROGRESS.md: `run_id` on every top-level
  record ("everything is keyed by run_id", §7.3), `confidence` on `Diagnosis` (§8.2, §8.4),
  `changelog_refs` on `Step` (FR-7), and the nested value types the sketch leaves implicit.
- Numeric limits that are configuration (budget caps, attempt caps) carry no defaults here:
  the §6 defaults live only in the config loader (P1-05).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, model_validator

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
    Tier,
    ToolCallId,
)

NonNegInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegFloat = Annotated[float, Field(ge=0)]
Share = Annotated[float, Field(ge=0, le=1)]
Percent = Annotated[float, Field(ge=0, le=100)]
Confidence = Annotated[float, Field(ge=0, le=1)]
#: A set of test IDs, serialized as a sorted list.
IdSet = Annotated[frozenset[str], PlainSerializer(sorted, return_type=list[str])]

_GIT_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------- enums used only by models


class Ecosystem(StrEnum):
    NODE = "node"
    PYTHON = "python"
    JAVA = "java"


class MigrationKind(StrEnum):
    """G3 migration kinds."""

    BUMP = "bump"
    FAMILY_BUMP = "family_bump"
    REPLACE = "replace"
    DEPRECATION = "deprecation"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class TestStage(StrEnum):
    """Which test run a `TestResult` belongs to (§11.2, §9.5)."""

    __test__ = False  # not a pytest class

    BASELINE = "baseline"
    TARGETED = "targeted"
    FULL = "full"
    FINAL = "final"


class VerifyStage(StrEnum):
    """Verification stages in order (§7.4 attempt scoring: install < build < typecheck < tests)."""

    INSTALL = "install"
    BUILD = "build"
    TYPECHECK = "typecheck"
    TESTS = "tests"


class CriticDecision(StrEnum):
    APPROVE = "approve"
    REVISE = "revise"
    ESCALATE = "escalate"


class SuccessLevel(StrEnum):
    """Benchmark success levels (§14.3); `NONE` means not even L0."""

    NONE = "none"
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


# ---------------------------------------------------------------- spec and run


class PackageChange(DomainModel):
    name: str = Field(min_length=1)
    from_range: str | None = None
    to_version: str = Field(min_length=1)
    replaces: str | None = None  # library replacement X -> Y: the package being replaced


class MigrationSpec(DomainModel):
    kind: MigrationKind
    ecosystem: Ecosystem
    packages: list[PackageChange] = Field(min_length=1)
    goal_text: str
    assumptions: list[str] = Field(default_factory=list)


class EnvProfile(DomainModel):
    language: Ecosystem
    runtime_version: str
    package_manager: str
    install_cmd: str
    build_cmd: str | None = None
    typecheck_cmd: str | None = None
    test_cmd: str
    image_digest: str
    lockfile_hashes: dict[str, str] = Field(default_factory=dict)  # path -> sha256
    detection_sources: dict[str, str] = Field(default_factory=dict)  # field -> where it came from


class Budgets(DomainModel):
    max_turns: PositiveInt
    max_tokens: PositiveInt
    minutes_cap: PositiveInt
    max_attempts_run: PositiveInt
    max_attempts_step: PositiveInt


class Spent(DomainModel):
    turns: NonNegInt = 0
    input_tokens: NonNegInt = 0
    output_tokens: NonNegInt = 0
    cache_read_tokens: NonNegInt = 0
    cache_write_tokens: NonNegInt = 0
    api_equiv_usd: NonNegFloat = 0.0
    sandbox_seconds: NonNegFloat = 0.0


class Run(DomainModel):
    id: RunId
    created_at: AwareDatetime
    status: RunStatus
    repo_url: str
    base_sha: str | None = None  # known once the repo is cloned
    branch: str | None = None
    spec: MigrationSpec
    env: EnvProfile | None = None
    budgets: Budgets
    spent: Spent = Field(default_factory=Spent)
    terminal_reason: str | None = None
    paused_at_state: RunStatus | None = None
    config_hash: str  # prompts + models + settings (NFR-5)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.base_sha is not None and not _GIT_SHA.match(self.base_sha):
            raise ValueError("base_sha must be a full lowercase hex git object id")
        if self.status is RunStatus.PAUSED_LIMIT and self.paused_at_state is None:
            raise ValueError("a run in PAUSED_LIMIT must record paused_at_state")
        if self.paused_at_state is not None and (
            self.paused_at_state is RunStatus.PAUSED_LIMIT or self.paused_at_state.is_terminal
        ):
            raise ValueError("paused_at_state must be a state the run can resume into")
        return self


# ---------------------------------------------------------------- tests and verification


class FailedTest(DomainModel):
    test_id: str
    message: str
    trace_excerpt: str = ""


class TestResult(DomainModel):
    """Result of one test run. D9: carries the passed and failed test IDs, not just counts."""

    __test__: ClassVar[bool] = False  # not a pytest class

    run_id: RunId
    attempt_id: AttemptId | None = None
    stage: TestStage
    passed: NonNegInt
    failed: NonNegInt
    skipped: NonNegInt
    errors: NonNegInt
    passed_ids: IdSet = frozenset()
    failed_ids: IdSet = frozenset()
    duration_s: NonNegFloat
    failures: list[FailedTest] = Field(default_factory=list)
    junit_path: str | None = None

    @property
    def executed_count(self) -> int:
        """D9: passed + failed + errors; skipped tests are not executed."""
        return self.passed + self.failed + self.errors

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.passed_ids & self.failed_ids:
            raise ValueError("a test cannot be both passed and failed")
        return self


class VerifyStageResult(DomainModel):
    stage: VerifyStage
    passed: bool
    exit_code: int | None = None
    duration_s: NonNegFloat = 0.0
    log_tail: str = ""
    failure_signature: str | None = None  # FR-12, set when the stage failed


class VerifyResult(DomainModel):
    stages: list[VerifyStageResult] = Field(default_factory=list)  # in pipeline order
    tests: TestResult | None = None


class Baseline(DomainModel):
    run_id: RunId
    runs: list[TestResult] = Field(min_length=1)  # the baseline test runs (FR-4: 3)
    passing_ids: IdSet  # P0: passing in every run, not flaky
    flaky_ids: IdSet
    test_file_hashes: dict[str, str]  # path -> sha256, from the host walk (§11.2)
    test_count: NonNegInt  # executed count, highest of the runs (§11.2 b)
    coverage_pct: Percent | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.passing_ids & self.flaky_ids:
            raise ValueError("a flaky test cannot be in the passing set P0")
        return self


# ---------------------------------------------------------------- plan, attempts, diagnosis


class Step(DomainModel):
    id: StepId
    order: NonNegInt
    type: StepType
    description: str
    target_files: list[str] = Field(default_factory=list)
    rationale: str
    changelog_refs: list[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    attempts: NonNegInt = 0


class Plan(DomainModel):
    id: PlanId
    run_id: RunId
    steps: list[Step]
    risks: list[str] = Field(default_factory=list)
    changelog_refs: list[str] = Field(default_factory=list)
    approved: bool | None = None  # None until a human decides (CP-1)


class Diagnosis(DomainModel):
    signature: str
    category: FailureCategory
    evidence: str  # the exact error line, plus a log excerpt of at most 60 lines (FR-12)
    hypotheses: list[str] = Field(default_factory=list, max_length=3)  # ranked, best first (§8.2)
    files: list[str] = Field(default_factory=list)
    changelog_refs: list[str] = Field(default_factory=list)
    confidence: Confidence  # required in every role's output schema (§8.4)


class Attempt(DomainModel):
    id: AttemptId
    run_id: RunId
    step_id: StepId
    n: PositiveInt  # 1-based attempt number within the step
    diagnosis: Diagnosis | None = None
    diff_sha: str  # hash of the host-computed diff (oscillation detection, §7.4)
    verify: VerifyResult
    outcome: Outcome


# ---------------------------------------------------------------- tool and model calls


class ToolCall(DomainModel):
    id: ToolCallId
    run_id: RunId
    attempt_id: AttemptId | None = None
    role: Role
    tool: str
    input: dict[str, Any]
    output_summary: str
    duration_ms: NonNegInt
    policy_decision: PolicyDecision
    deny_reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.policy_decision is PolicyDecision.DENY and not self.deny_reason:
            raise ValueError("a denied tool call must record deny_reason")
        if self.policy_decision is PolicyDecision.ALLOW and self.deny_reason is not None:
            raise ValueError("an allowed tool call cannot have deny_reason")
        return self


def _reject_opus_medium(tier: Tier, effort: Effort) -> None:
    """NFR-12 / §8.4: Opus always runs at high effort."""
    if tier is Tier.OPUS and effort is Effort.MEDIUM:
        raise ValueError("Opus never runs at medium effort")


class LLMCall(DomainModel):
    id: LLMCallId
    run_id: RunId
    role: Role
    runtime: str  # e.g. "agent_sdk", "fake", "cassette"
    model: str  # the model ID that actually ran
    tier: Tier
    effort: Effort  # the effort the call actually ran at, never just the requested one
    routing_decision_id: RoutingDecisionId
    input_tokens: NonNegInt
    output_tokens: NonNegInt
    cache_read_tokens: NonNegInt = 0
    cache_write_tokens: NonNegInt = 0
    thinking: bool  # extended thinking was on (§8.1 item 10)
    api_equiv_usd: NonNegFloat = 0.0
    latency_ms: NonNegInt
    limit_error: str | None = None  # set when the call ended on a usage or rate limit (FR-21)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if "haiku" in self.model.lower():
            raise ValueError("Haiku is never used (NFR-12)")
        _reject_opus_medium(self.tier, self.effort)
        return self


class RoutingDecision(DomainModel):
    id: RoutingDecisionId
    run_id: RunId
    step_id: StepId | None = None
    role: Role
    policy: RoutingPolicy
    effort_policy: EffortPolicy
    tier: Tier
    effort: Effort
    reason_codes: list[str] = Field(default_factory=list)
    escalated_from: Literal[Tier.SONNET] | None = None
    effort_escalated_from: Literal[Effort.MEDIUM] | None = None
    sticky: bool = False
    inputs_hash: str

    @model_validator(mode="after")
    def _check(self) -> Self:
        _reject_opus_medium(self.tier, self.effort)
        if self.escalated_from is not None and self.tier is not Tier.OPUS:
            raise ValueError("escalated_from is set, so the tier must be opus")
        if self.effort_escalated_from is not None and self.effort is not Effort.HIGH:
            raise ValueError("effort_escalated_from is set, so the effort must be high")
        return self


# ---------------------------------------------------------------- integrity, review, PR


class Violation(DomainModel):
    check_id: str  # "integrity.a" ... "integrity.i", "diff_limits", ...
    code: str
    message: str
    path: str | None = None
    hunk: str | None = None


class IntegrityReport(DomainModel):
    run_id: RunId
    passed: bool
    violations: list[Violation] = Field(default_factory=list)
    coverage_delta_pp: float | None = None
    test_count_before: NonNegInt
    test_count_after: NonNegInt
    resolved_versions: dict[str, str] = Field(default_factory=dict)
    removed_api_usages_remaining: list[str] = Field(default_factory=list)  # "path:line api"

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.passed == bool(self.violations):
            raise ValueError("passed must be true exactly when there are no violations")
        return self


class CriticVerdict(DomainModel):
    run_id: RunId
    decision: CriticDecision
    findings: list[str] = Field(default_factory=list)
    hunk_mapping: dict[str, str] = Field(default_factory=dict)  # hunk -> plan step or diagnosis


class PullRequest(DomainModel):
    run_id: RunId
    number: PositiveInt
    url: str
    branch: str
    draft: bool
    commits: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


# ---------------------------------------------------------------- benchmark


class RubricScores(DomainModel):
    """Blinded L4 review rubric (§14.3); each criterion scored 1 to 5."""

    correctness: Annotated[int, Field(ge=1, le=5)]
    minimality: Annotated[int, Field(ge=1, le=5)]
    idiomatic_use: Annotated[int, Field(ge=1, le=5)]
    explanation: Annotated[int, Field(ge=1, le=5)]


class HumanReview(DomainModel):
    merge_ready: bool
    rubric: RubricScores
    reviewer: str


class EvalResult(DomainModel):
    suite: str
    task_id: str
    system: str  # full, b0, b1, b2, b3 or an ablation name
    seed: NonNegInt  # a repeat index when thinking is on (NFR-5)
    status: RunStatus
    success_level: SuccessLevel
    turns: NonNegInt
    tokens: NonNegInt
    api_equiv_usd: NonNegFloat
    wall_s: NonNegFloat
    attempts: NonNegInt
    llm_calls: NonNegInt
    routing_policy: RoutingPolicy
    effort_policy: EffortPolicy
    opus_token_share: Share
    medium_effort_share: Share
    failure_category: FailureCategory | None = None
    integrity_violations_blocked: NonNegInt = 0
    human_review: HumanReview | None = None
