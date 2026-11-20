"""Model Router interface (SPEC §8.4, FR-22, NFR-12; B8, B9, B10).

`route(ctx, policies)` is pure and deterministic. It picks one of exactly three
combinations: opus/high, sonnet/high, sonnet/medium. It returns a `RouteOutcome` with no
IDs, and the orchestrator wraps it into a §12 `RoutingDecision` (`RouteOutcome.to_decision`).

Stickiness without state: the orchestrator persists `StickyState` per step (per run phase
for step-less calls) with each checkpoint and passes it back in `RoutingContext.sticky`. The
router returns `new_sticky`, which never has fewer flags than the input (B5: a resume can
never de-escalate).

Implementations: v0 in P4-02 (B10: Opus/high for routed roles, never medium) and v1 in
P5-11/P5-12 (escalation triggers, strict effort rules).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

from mpilot.core.types import (
    Effort,
    EffortPolicy,
    FailureCategory,
    LLMCallId,
    Outcome,
    Role,
    RoutingDecisionId,
    RoutingPolicy,
    RunId,
    StepId,
    StepType,
    StopReason,
    Tier,
)
from mpilot.model_policy import (
    HIGH_ONLY_ROLES,
    OPUS_FIXED_ROLES,
    UNPINNABLE_ROLES,
    RolePin,
    pin_problems,
)
from mpilot.models.domain import (
    Confidence,
    CriticDecision,
    LLMCall,
    MigrationKind,
    NonNegInt,
    PolicyDecision,
    RoutingDecision,
    ToolCall,
)
from mpilot.tools.base import EXPLORE_CALLER_ROLES

#: Roles whose tier is routed under `routed` (§8.4 default tier table).
ROUTED_ROLES = frozenset({Role.EDITOR, Role.DIAGNOSER, Role.ANALYZER})
#: Roles that stay on Sonnet under `routed`, even after escalation; only effort moves (B8).
SONNET_FIXED_ROLES = frozenset({Role.EXPLORER, Role.PR_WRITER})
#: The only roles that may ever run at medium (§8.4 effort routing).
MEDIUM_ELIGIBLE_ROLES = frozenset({Role.EXPLORER, Role.PR_WRITER, Role.EDITOR})
#: Roles that always run inside a plan step, and roles that never do (§8.4 Purity).
IN_STEP_ROLES = frozenset({Role.EDITOR, Role.DIAGNOSER})
STEPLESS_ROLES = frozenset({Role.ANALYZER, Role.PLANNER, Role.PR_WRITER})


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StickyState(_Frozen):
    """Escalations that hold until the step (or run phase) ends (§8.4 Sticky escalation)."""

    tier_escalated: bool = False  # sonnet -> opus happened in this scope
    effort_escalated: bool = False  # medium -> high happened in this scope
    #: Any of triggers 1-8 fired in this scope, whatever tier and effort that call already
    #: had (an Opus/high call can fire a trigger too). Medium is then off for the rest of the
    #: scope (§8.4: "no escalation trigger (1-8) has fired in this step").
    trigger_fired: bool = False

    def covers(self, other: StickyState) -> bool:
        """True if `self` keeps every flag `other` has (no de-escalation)."""
        mine = (self.tier_escalated, self.effort_escalated, self.trigger_fired)
        theirs = (other.tier_escalated, other.effort_escalated, other.trigger_fired)
        return all(m or not t for m, t in zip(mine, theirs, strict=True))

    @property
    def any(self) -> bool:
        return self.tier_escalated or self.effort_escalated or self.trigger_fired


class ExplorerFlag(StrEnum):
    """Explorer answer problems that move the next Explorer call to high (§8.4)."""

    NO_CITATIONS = "no_citations"
    OVER_CAP = "over_cap"
    REPEATED_QUESTION = "repeated_question"


_FlagSet = Annotated[frozenset[ExplorerFlag], PlainSerializer(sorted, return_type=list[str])]


_StrSet = Annotated[frozenset[str], PlainSerializer(sorted, return_type=list[str])]


def explorer_question_hash(caller: Role, question: str, scope: str | None = None) -> str:
    """Key for "the caller repeating the same Explorer question" (§8.4 effort escalation):
    SHA-256 over the caller role, the question (NFKC, case-folded, whitespace collapsed) and
    the `explore` scope argument, normalized the same way."""

    def norm(text: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", text).casefold().split())

    canon = json.dumps([caller.value, norm(question), norm(scope or "")], separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


class RoutingCounters(_Frozen):
    """Router inputs that come from what already happened in a scope (a plan step, or a run
    phase for step-less calls), not from the plan or the call itself (§8.4 `ctx`).

    The orchestrator keeps one per `StickyScope` in every checkpoint (`Checkpoint.routing`),
    updated after every tool call and role call, so a crash or pause loses none: one denial
    before a crash and one after still fire trigger 3 (FR-21: a resume never de-escalates).
    Everything only grows within the scope, except `last_confidence`, which each newer
    completed call replaces (`Checkpoint.covers` checks both).
    """

    policy_denials: NonNegInt = 0  # denied tool calls in scope; 2 fire trigger 3
    schema_invalid: NonNegInt = 0  # calls in scope that ended SCHEMA_INVALID; 1 fires trigger 3
    #: Self-reported confidence of the latest completed call in scope (trigger 5), and which
    #: call reported it. Nested Explorer calls count: a low value only pushes up.
    last_confidence: Confidence | None = None
    last_confidence_call: LLMCallId | None = None
    #: Every call whose confidence was recorded in scope, so `covers` can tell a newer call's
    #: value from a switch back to an older one (which could reset trigger 5).
    confidence_calls: _StrSet = frozenset()
    explorer_questions: _StrSet = frozenset()  # `explorer_question_hash` of every question
    #: Problems seen on Explorer answers or questions in scope. Kept for the rest of the scope:
    #: the effort escalation they cause is sticky anyway.
    explorer_flags: _FlagSet = frozenset()
    pr_template_rejected: bool = False  # a PR Writer draft failed template validation

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.last_confidence is None) != (self.last_confidence_call is None):
            raise ValueError("last_confidence and last_confidence_call are set together")
        if self.last_confidence_call is not None and (
            self.last_confidence_call not in self.confidence_calls
        ):
            raise ValueError("last_confidence_call must be among confidence_calls")
        for h in self.explorer_questions:
            if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
                raise ValueError(f"not an explorer_question_hash: {h!r}")
        return self

    def covers(self, other: RoutingCounters) -> bool:
        """True if `self` keeps everything `other` recorded: no count lowered, no question
        hash, flag or confidence call dropped, no rejection forgotten, a confidence never
        cleared, the same call's confidence never changed, and a change of call only to one
        never recorded before (a newer call, not a switch back)."""
        if self.last_confidence_call == other.last_confidence_call:
            conf_kept = self.last_confidence == other.last_confidence
        else:
            conf_kept = (
                self.last_confidence_call is not None
                and self.last_confidence_call not in other.confidence_calls
            )
        return (
            self.policy_denials >= other.policy_denials
            and self.schema_invalid >= other.schema_invalid
            and conf_kept
            and self.confidence_calls >= other.confidence_calls
            and self.explorer_questions >= other.explorer_questions
            and self.explorer_flags >= other.explorer_flags
            and (self.pr_template_rejected or not other.pr_template_rejected)
        )

    def after_tool_call(self, call: ToolCall) -> RoutingCounters:
        """Count a denied tool call (any denial: host, role, arguments or policy)."""
        if call.policy_decision is not PolicyDecision.DENY:
            return self
        return self.model_copy(update={"policy_denials": self.policy_denials + 1})

    def after_call(self, call: LLMCall) -> RoutingCounters:
        """Fold in a recorded role call: a SCHEMA_INVALID ending, or a completed call's
        `confidence` (required in every output schema, §8.4). A limit-interrupted call
        changes nothing."""
        if call.stop is StopReason.SCHEMA_INVALID:
            return self.model_copy(update={"schema_invalid": self.schema_invalid + 1})
        if call.output is None:
            return self
        conf = call.output.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, int | float):
            raise ValueError(f"call {call.id} output has no numeric confidence")
        if call.id in self.confidence_calls:
            return self  # this call's confidence is already in (a SCHEMA_INVALID fold is not
            # idempotent: folding it twice over-counts, the safe direction)
        return RoutingCounters.model_validate(
            {
                **self.model_dump(),
                "last_confidence": float(conf),
                "last_confidence_call": call.id,
                "confidence_calls": self.confidence_calls | {call.id},
            }
        )

    def asked(self, question_hash: str) -> RoutingCounters:
        """Record an Explorer question before routing its call. A hash already seen in scope
        adds `REPEATED_QUESTION`, which `apply` passes on, so that call runs at high."""
        flags = self.explorer_flags
        if question_hash in self.explorer_questions:
            flags = flags | {ExplorerFlag.REPEATED_QUESTION}
        return RoutingCounters.model_validate(
            {
                **self.model_dump(),
                "explorer_questions": self.explorer_questions | {question_hash},
                "explorer_flags": flags,
            }
        )

    def flagged(self, *flags: ExplorerFlag) -> RoutingCounters:
        """Record problems with an Explorer answer (no citations, over the token cap)."""
        return self.model_copy(update={"explorer_flags": self.explorer_flags | set(flags)})

    def pr_rejected(self) -> RoutingCounters:
        return self.model_copy(update={"pr_template_rejected": True})

    def apply(self, ctx: RoutingContext) -> RoutingContext:
        """`ctx` with the counter-derived inputs filled in. Every field only moves towards
        Opus/high: more denials, schema-invalid, the lower of the two confidences, more
        flags. The Explorer flags go only to Explorer calls, the rejection only to PR Writer
        calls."""
        update: dict[str, object] = {
            "policy_denials_in_step": max(ctx.policy_denials_in_step, self.policy_denials),
            "schema_invalid_in_step": ctx.schema_invalid_in_step or self.schema_invalid > 0,
        }
        if self.last_confidence is not None:
            mine, theirs = self.last_confidence, ctx.last_confidence
            update["last_confidence"] = mine if theirs is None else min(mine, theirs)
        if ctx.role is Role.EXPLORER:
            update["explorer_flags"] = ctx.explorer_flags | self.explorer_flags
        if ctx.role is Role.PR_WRITER:
            update["pr_template_rejected"] = ctx.pr_template_rejected or self.pr_template_rejected
        return RoutingContext.model_validate({**ctx.model_dump(), **update})


class RoutingContext(_Frozen):
    """Routing inputs (§8.4 `ctx`). Built by the orchestrator from state. Some inputs come
    from model output: the self-reported `last_confidence`, which only ever pushes towards
    Opus/high, and the Diagnoser's `diagnosis_confidence`, `failure_category` and expected
    edit size, which can also enable Sonnet or medium. The latter are validated against the
    output schema and the medium/Sonnet conditions need several of them at once (§8.4);
    `has_changelog_mapping`/`error_maps_to_changelog` come from the host's changelog index,
    never from a model's claim (P1-12d decision; P5-11 builds them that way).

    Counters are 1-based for the current call; 0 means "not in a step" / "no signature yet".
    """

    role: Role
    caller: Role | None = None  # set exactly for Explorer calls (Critic caller => high)
    step_type: StepType | None = None
    touches_test_config: bool = False  # a config step touching §11.2 (e) files => high
    migration_kind: MigrationKind
    usage_sites: NonNegInt | None = None  # Analyzer: Sonnet only if <= 50
    attempt_in_step: NonNegInt = 0
    attempts_on_signature: NonNegInt = 0  # >= 2 fires trigger 1
    prev_outcome: Outcome | None = None
    failure_category: FailureCategory | None = None
    expected_files: NonNegInt | None = None  # None = unknown, never "small"
    expected_lines: NonNegInt | None = None
    has_changelog_mapping: bool = False  # Editor: explicit before/after mapping (B9)
    error_maps_to_changelog: bool = False  # Diagnoser: error maps to a changelog entry (B9)
    is_codemod_followup: bool = False
    last_confidence: Confidence | None = None
    diagnosis_confidence: Confidence | None = None
    policy_denials_in_step: NonNegInt = 0
    schema_invalid_in_step: bool = False
    critic_verdict: CriticDecision | None = None
    explorer_flags: _FlagSet = frozenset()
    pr_template_rejected: bool = False
    sticky: StickyState = Field(default_factory=StickyState)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.caller is None) != (self.role is not Role.EXPLORER):
            raise ValueError("caller is set exactly for Explorer calls")
        if self.caller is not None and self.caller not in EXPLORE_CALLER_ROLES:
            raise ValueError(f"{self.caller} has no `explore` tool (§8.2, §8.5)")
        if self.role in IN_STEP_ROLES and self.step_type is None:
            raise ValueError(f"{self.role} calls always run inside a plan step")
        if self.role in STEPLESS_ROLES and self.step_type is not None:
            raise ValueError(f"{self.role} calls never run inside a plan step")
        if (self.step_type is None) != (self.attempt_in_step == 0):
            raise ValueError("attempt_in_step is >= 1 exactly inside a step")
        if self.explorer_flags and self.role is not Role.EXPLORER:
            raise ValueError("explorer_flags apply only to Explorer calls")
        if self.pr_template_rejected and self.role is not Role.PR_WRITER:
            raise ValueError("pr_template_rejected applies only to PR Writer calls")
        return self

    def inputs_hash(self, policies: RoutingPolicies) -> str:
        """SHA-256 over the canonical JSON of the context and the policies (§12)."""
        payload = {
            "ctx": self.model_dump(mode="json"),
            "policies": policies.model_dump(mode="json"),
        }
        canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()


class RoutingPolicies(_Frozen):
    """Config `routing.policy`, `routing.effort_policy` and `routing.pins` (§8.4, P1-06).

    `all_opus` implies `all_high` (the config loader normalizes it; this re-checks). Pins are
    re-validated with the same NFR-12 rules as the config.

    Pin semantics (SPEC §8.4 "Pins"): a pin replaces the role's *default* tier and/or effort
    and nothing else. Escalation triggers 1-8, the effort-escalation rules, stickiness and
    the medium preconditions always apply on top, so a pin can raise a choice but never
    lower one those rules force.
    """

    policy: RoutingPolicy = RoutingPolicy.ROUTED
    effort_policy: EffortPolicy = EffortPolicy.ROUTED
    pins: Mapping[Role, RolePin] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (
            self.policy is RoutingPolicy.ALL_OPUS
            and self.effort_policy is not EffortPolicy.ALL_HIGH
        ):
            raise ValueError("routing.policy = all_opus implies effort_policy = all_high")
        problems = pin_problems(self.pins, self.policy)
        if problems:
            raise ValueError("; ".join(problems))
        return self


class RouteOutcome(_Frozen):
    """Pure routing result (B9): no IDs. Reason codes explain both tier and effort."""

    tier: Tier
    effort: Effort
    reason_codes: tuple[str, ...] = Field(min_length=1)
    escalated_from: Literal[Tier.SONNET] | None = None
    effort_escalated_from: Literal[Effort.MEDIUM] | None = None
    sticky: bool = False  # this choice was forced by an earlier escalation in scope
    new_sticky: StickyState
    inputs_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.tier is Tier.OPUS and self.effort is Effort.MEDIUM:
            raise ValueError("Opus never runs at medium effort (§8.4)")
        if self.escalated_from is not None and self.tier is not Tier.OPUS:
            raise ValueError("escalated_from is set, so the tier must be opus")
        if self.effort_escalated_from is not None and self.effort is not Effort.HIGH:
            raise ValueError("effort_escalated_from is set, so the effort must be high")
        return self

    def to_decision(
        self,
        *,
        decision_id: RoutingDecisionId,
        run_id: RunId,
        step_id: StepId | None,
        ctx: RoutingContext,
        policies: RoutingPolicies,
    ) -> RoutingDecision:
        """Wrap into the persisted §12 `RoutingDecision`. Refuses an outcome that was not
        computed for exactly this `ctx` and `policies` (hash mismatch)."""
        if self.inputs_hash != ctx.inputs_hash(policies):
            raise ValueError("outcome was routed for a different context or policy")
        if (step_id is None) != (ctx.step_type is None):
            raise ValueError("step_id must be given exactly for in-step calls")
        return RoutingDecision(
            id=decision_id,
            run_id=run_id,
            step_id=step_id,
            role=ctx.role,
            policy=policies.policy,
            effort_policy=policies.effort_policy,
            tier=self.tier,
            effort=self.effort,
            reason_codes=list(self.reason_codes),
            escalated_from=self.escalated_from,
            effort_escalated_from=self.effort_escalated_from,
            sticky=self.sticky,
            inputs_hash=self.inputs_hash,
        )


@runtime_checkable
class ModelRouter(Protocol):
    """§8.4 Model Router."""

    def route(self, ctx: RoutingContext, policies: RoutingPolicies) -> RouteOutcome:
        """Pure and deterministic: same inputs, same outcome. Never reads the clock, the
        budget or the usage state ("never trade quality for usage", §8.4).

        Must satisfy `route_invariant_violations(ctx, policies, outcome) == []` for every
        input (P4-02 and P5-12 property-test this). The oracle fixes what is forbidden and
        what is forced; the implementation chooses within that and writes the reason codes.
        """
        ...


# ---------------------------------------------------------------- §8.4 rules over ctx

#: §8.4 thresholds, named once.
LOW_CONFIDENCE = 0.7  # trigger 5
LARGE_EDIT_FILES, LARGE_EDIT_LINES = 3, 150  # trigger 8 and the Editor's Sonnet condition
EDITOR_SONNET_CONFIDENCE = 0.8
TRIVIAL_EDIT_FILES, TRIVIAL_EDIT_LINES, TRIVIAL_EDIT_CONFIDENCE = 1, 40, 0.9
ANALYZER_SONNET_MAX_USAGE_SITES = 50
DIAGNOSER_SONNET_CATEGORIES = frozenset(
    {FailureCategory.COMPILE_TYPE, FailureCategory.API_REMOVED, FailureCategory.CONFIG_FORMAT}
)
HARD_CATEGORIES = frozenset(  # trigger 4
    {
        FailureCategory.API_BEHAVIOR_CHANGED,
        FailureCategory.TRANSITIVE_INCOMPAT,
        FailureCategory.TEST_INFRA,
        FailureCategory.NO_PROGRESS,
    }
)


class EscalationTrigger(StrEnum):
    """The eight §8.4 escalation triggers (sonnet -> opus for routed roles; -> high always)."""

    SECOND_ATTEMPT_ON_SIGNATURE = "t1_second_attempt_on_signature"
    NO_IMPROVEMENT = "t2_prev_same_or_regressed"
    SCHEMA_INVALID_OR_DENIALS = "t3_schema_invalid_or_two_denials"
    HARD_CATEGORY = "t4_hard_category"
    LOW_CONFIDENCE = "t5_low_confidence"
    CRITIC_REVISE = "t6_critic_revise"
    TEST_TOUCH = "t7_test_touch"
    LARGE_EDIT = "t8_large_edit"


def fired_triggers(ctx: RoutingContext) -> frozenset[EscalationTrigger]:
    """Which of triggers 1-8 hold for `ctx`. Pure; unknown sizes (None) never fire 8."""
    t = EscalationTrigger
    fired = {
        t.SECOND_ATTEMPT_ON_SIGNATURE: ctx.attempts_on_signature >= 2,
        t.NO_IMPROVEMENT: ctx.prev_outcome in (Outcome.SAME, Outcome.REGRESSED),
        t.SCHEMA_INVALID_OR_DENIALS: ctx.schema_invalid_in_step or ctx.policy_denials_in_step >= 2,
        t.HARD_CATEGORY: ctx.failure_category in HARD_CATEGORIES,
        t.LOW_CONFIDENCE: ctx.last_confidence is not None and ctx.last_confidence < LOW_CONFIDENCE,
        t.CRITIC_REVISE: ctx.critic_verdict is CriticDecision.REVISE,
        t.TEST_TOUCH: ctx.step_type is StepType.TEST_TOUCH,
        t.LARGE_EDIT: (ctx.expected_files or 0) > LARGE_EDIT_FILES
        or (ctx.expected_lines or 0) > LARGE_EDIT_LINES,
    }
    return frozenset(k for k, hit in fired.items() if hit)


def _small_edit(ctx: RoutingContext, files: int, lines: int) -> bool:
    return (
        ctx.expected_files is not None
        and ctx.expected_lines is not None
        and ctx.expected_files <= files
        and ctx.expected_lines <= lines
    )


def sonnet_allowed_by_default(ctx: RoutingContext) -> bool:
    """§8.4 default-tier table, "Sonnet allowed only when", for an unpinned role under
    `routed`. Triggers are separate (`fired_triggers`)."""
    mapped = ctx.has_changelog_mapping or ctx.is_codemod_followup
    match ctx.role:
        case Role.EDITOR:
            conf = ctx.diagnosis_confidence
            small = _small_edit(ctx, LARGE_EDIT_FILES, LARGE_EDIT_LINES)
            return small and mapped and conf is not None and conf >= EDITOR_SONNET_CONFIDENCE
        case Role.DIAGNOSER:
            return (
                ctx.attempts_on_signature <= 1
                and ctx.failure_category in DIAGNOSER_SONNET_CATEGORIES
                and ctx.error_maps_to_changelog
            )
        case Role.ANALYZER:
            return (
                ctx.usage_sites is not None
                and ctx.usage_sites <= ANALYZER_SONNET_MAX_USAGE_SITES
                and ctx.migration_kind is not MigrationKind.REPLACE
            )
        case Role.EXPLORER | Role.PR_WRITER:
            return True
        case _:
            return False


def is_trivial_edit(ctx: RoutingContext) -> bool:
    """§8.4 *trivial edit*: the only Editor calls that may run at medium."""
    conf = ctx.diagnosis_confidence
    return (
        ctx.role is Role.EDITOR
        and sonnet_allowed_by_default(ctx)
        and ctx.attempt_in_step == 1
        and _small_edit(ctx, TRIVIAL_EDIT_FILES, TRIVIAL_EDIT_LINES)
        and (ctx.has_changelog_mapping or ctx.is_codemod_followup)
        and conf is not None
        and conf >= TRIVIAL_EDIT_CONFIDENCE
    )


def minimal_sticky_after(ctx: RoutingContext, policies: RoutingPolicies) -> StickyState:
    """The least `new_sticky` any outcome for `ctx` must carry: the input flags, plus
    `trigger_fired` if a trigger fires, plus `tier_escalated` if it fires for a routed role
    under `routed`, plus `effort_escalated` if a flagged Explorer answer or a rejected PR
    draft forces high. A router may add more (e.g. its own `escalated_from`)."""
    fired = bool(fired_triggers(ctx))
    routed_role = policies.policy is RoutingPolicy.ROUTED and ctx.role in ROUTED_ROLES
    effort_forced = bool(ctx.explorer_flags) or ctx.pr_template_rejected
    return StickyState(
        tier_escalated=ctx.sticky.tier_escalated or (fired and routed_role),
        effort_escalated=ctx.sticky.effort_escalated or effort_forced,
        trigger_fired=ctx.sticky.trigger_fired or fired,
    )


def route_invariant_violations(
    ctx: RoutingContext, policies: RoutingPolicies, out: RouteOutcome
) -> list[str]:
    """Every §8.2/§8.4/B8 rule an outcome must meet, whatever the router version.

    It fixes what is forbidden and what is forced; within that the router chooses (v0, B10,
    always picks opus/high for routed roles). Used as the property-test oracle (P4-02,
    P5-11/12) and as an orchestrator-side assertion before any call runs. An empty list
    means the outcome is allowed.
    """
    v: list[str] = []
    role, policy, eff_policy = ctx.role, policies.policy, policies.effort_policy
    pin = policies.pins.get(role)
    triggers = fired_triggers(ctx)
    routed = policy is RoutingPolicy.ROUTED
    routed_role = routed and role in ROUTED_ROLES

    def need(cond: bool, msg: str) -> None:
        if not cond:
            v.append(msg)

    opus_high = out.tier is Tier.OPUS and out.effort is Effort.HIGH
    need(out.inputs_hash == ctx.inputs_hash(policies), "inputs_hash does not match ctx/policies")

    # Fixed roles and policies (§8.2, B8).
    if role in UNPINNABLE_ROLES:
        need(opus_high, f"{role} always runs opus/high (B8)")
    if role in OPUS_FIXED_ROLES:
        need(opus_high, f"{role} is never routed down from opus/high (§8.4)")
    if role in HIGH_ONLY_ROLES:
        need(out.effort is Effort.HIGH, f"{role} always runs at high effort (§8.2)")
    if policy is RoutingPolicy.ALL_OPUS:
        need(opus_high, "all_opus puts every role on opus/high (B8)")
    if policy is RoutingPolicy.ALL_SONNET and role not in OPUS_FIXED_ROLES | UNPINNABLE_ROLES:
        need(out.tier is Tier.SONNET, "all_sonnet puts this role on sonnet (B8)")
    if eff_policy is EffortPolicy.ALL_HIGH:
        need(out.effort is Effort.HIGH, "effort_policy = all_high")

    # Pins raise defaults; nothing below lets them lower a forced choice.
    if pin is not None and pin.tier is Tier.OPUS:
        need(out.tier is Tier.OPUS, f"{role} is pinned to opus")
    if pin is not None and pin.effort is Effort.HIGH:
        need(out.effort is Effort.HIGH, f"{role} is pinned to high")
    if routed and role in SONNET_FIXED_ROLES and not (pin and pin.tier is Tier.OPUS):
        need(out.tier is Tier.SONNET, f"{role} stays on sonnet under routed (B8)")
    tier_pinned_sonnet = pin is not None and pin.tier is Tier.SONNET
    if routed_role and out.tier is Tier.SONNET and not tier_pinned_sonnet:
        need(sonnet_allowed_by_default(ctx), f"{role} on sonnet needs the §8.4 Sonnet conditions")

    # Escalation triggers 1-8 (never overridden by a pin).
    if triggers:
        names = ", ".join(sorted(triggers))
        need(out.effort is Effort.HIGH, f"escalation trigger fired ({names}): effort must be high")
        need(out.new_sticky.trigger_fired, "a fired trigger must be recorded in new_sticky")
        if routed_role:
            need(out.tier is Tier.OPUS, f"escalation trigger fired ({names}): {role} goes to opus")
            need(out.new_sticky.tier_escalated, "a tier escalation must be recorded in new_sticky")

    # Effort escalation (§8.4): forced to high now, and recorded so it sticks for the scope.
    # The scope is shared: an Explorer call inside a step escalates that step's state, which
    # also rules out Editor medium for the rest of the step (stricter than required, on purpose).
    effort_forced = bool(ctx.explorer_flags) or ctx.pr_template_rejected
    if effort_forced:
        need(
            out.effort is Effort.HIGH, "a flagged answer or rejected draft moves this call to high"
        )
    if effort_forced or out.effort_escalated_from is not None:
        need(out.new_sticky.effort_escalated, "an effort escalation must be recorded in new_sticky")
    if out.effort_escalated_from is not None:
        need(
            role in MEDIUM_ELIGIBLE_ROLES,
            f"{role} never runs at medium, so cannot escalate from it",
        )

    # Stickiness (never de-escalates).
    need(out.new_sticky.covers(ctx.sticky), "the router never de-escalates (sticky)")
    if out.new_sticky.tier_escalated and routed_role:
        need(out.tier is Tier.OPUS, "a tier escalation in scope keeps the role on opus")
    if out.escalated_from is not None:
        need(routed_role, "only routed roles under `routed` escalate their tier")
        need(
            not (pin is not None and pin.tier is Tier.OPUS),
            f"{role} is pinned to opus, so it never ran on sonnet to escalate from",
        )
        need(out.new_sticky.tier_escalated, "escalated_from is set but not recorded in new_sticky")
    if out.sticky:
        need(ctx.sticky.any, "`sticky` means an earlier escalation in scope forced this choice")

    # Medium: only when every §8.4 condition holds.
    if out.effort is Effort.MEDIUM:
        need(role in MEDIUM_ELIGIBLE_ROLES, f"{role} never runs at medium (§8.4)")
        need(ctx.step_type is not StepType.TEST_TOUCH, "test_touch never runs at medium")
        need(ctx.caller is not Role.CRITIC, "an Explorer call for the Critic runs at high")
        need(
            not (ctx.step_type is StepType.CONFIG and ctx.touches_test_config),
            "a config step touching test configs runs at high",
        )
        need(not ctx.sticky.any, "an escalation or trigger earlier in scope rules out medium")
        need(
            not (out.new_sticky.effort_escalated or out.new_sticky.trigger_fired),
            "escalated effort never returns to medium",
        )
        if role is Role.EDITOR:
            need(is_trivial_edit(ctx), "editor medium needs a §8.4 trivial edit")
        if role is Role.EXPLORER:
            need(not ctx.explorer_flags, "a flagged Explorer answer moves the next call to high")
        if role is Role.PR_WRITER:
            need(not ctx.pr_template_rejected, "a rejected PR draft moves the next call to high")
    return v
