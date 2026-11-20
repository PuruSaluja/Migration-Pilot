"""P1-07: Model Router interface (SPEC §8.4, B8, B9) and the invariant oracle."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from mpilot.agents.router import (
    IN_STEP_ROLES,
    MEDIUM_ELIGIBLE_ROLES,
    ROUTED_ROLES,
    STEPLESS_ROLES,
    EscalationTrigger,
    ExplorerFlag,
    ModelRouter,
    RouteOutcome,
    RoutingContext,
    RoutingPolicies,
    StickyState,
    fired_triggers,
    is_trivial_edit,
    minimal_sticky_after,
    route_invariant_violations,
    sonnet_allowed_by_default,
)
from mpilot.core.types import (
    Effort,
    EffortPolicy,
    FailureCategory,
    Outcome,
    Role,
    RoutingDecisionId,
    RoutingPolicy,
    RunId,
    StepId,
    StepType,
    Tier,
)
from mpilot.model_policy import RolePin, pin_problems
from mpilot.models.domain import CriticDecision, MigrationKind
from mpilot.testing.doubles import FixedRouter
from mpilot.tools.base import EXPLORE_CALLER_ROLES

T = EscalationTrigger


def _ctx(**kw: Any) -> RoutingContext:
    base: dict[str, Any] = {
        "role": Role.EDITOR,
        "step_type": StepType.EDIT_CLUSTER,
        "attempt_in_step": 1,
        "migration_kind": MigrationKind.BUMP,
    }
    base.update(kw)
    return RoutingContext(**base)


def _stepless(role: Role, **kw: Any) -> RoutingContext:
    return RoutingContext(role=role, migration_kind=MigrationKind.BUMP, **kw)


#: An Editor call that meets every §8.4 trivial-edit condition.
TRIVIAL: dict[str, Any] = {
    "expected_files": 1,
    "expected_lines": 20,
    "has_changelog_mapping": True,
    "diagnosis_confidence": 0.95,
}

ROUTED = RoutingPolicies()
ALL_SONNET = RoutingPolicies(policy=RoutingPolicy.ALL_SONNET)
ALL_OPUS = RoutingPolicies(policy=RoutingPolicy.ALL_OPUS, effort_policy=EffortPolicy.ALL_HIGH)
ALL_HIGH = RoutingPolicies(effort_policy=EffortPolicy.ALL_HIGH)


def _out(
    ctx: RoutingContext, pol: RoutingPolicies, tier: Tier, effort: Effort, **kw: Any
) -> RouteOutcome:
    kw.setdefault("new_sticky", minimal_sticky_after(ctx, pol))
    return RouteOutcome(
        tier=tier, effort=effort, reason_codes=("t",), inputs_hash=ctx.inputs_hash(pol), **kw
    )


def _problems(ctx: RoutingContext, pol: RoutingPolicies, tier: Tier, effort: Effort) -> list[str]:
    return route_invariant_violations(ctx, pol, _out(ctx, pol, tier, effort))


# ---------------------------------------------------------------- strategies


@st.composite
def contexts(draw: st.DrawFn) -> RoutingContext:
    role = draw(st.sampled_from(list(Role)))
    caller = draw(st.sampled_from(sorted(EXPLORE_CALLER_ROLES))) if role is Role.EXPLORER else None
    if role in IN_STEP_ROLES:
        step: StepType | None = draw(st.sampled_from(list(StepType)))
    elif role in STEPLESS_ROLES:
        step = None
    else:
        step = draw(st.none() | st.sampled_from(list(StepType)))
    opt_conf = st.none() | st.floats(0, 1)
    return RoutingContext(
        role=role,
        caller=caller,
        step_type=step,
        touches_test_config=draw(st.booleans()),
        migration_kind=draw(st.sampled_from(list(MigrationKind))),
        usage_sites=draw(st.none() | st.integers(0, 500)),
        attempt_in_step=0 if step is None else draw(st.integers(1, 6)),
        attempts_on_signature=draw(st.integers(0, 6)),
        prev_outcome=draw(st.none() | st.sampled_from(list(Outcome))),
        failure_category=draw(st.none() | st.sampled_from(list(FailureCategory))),
        expected_files=draw(st.none() | st.integers(0, 20)),
        expected_lines=draw(st.none() | st.integers(0, 1000)),
        has_changelog_mapping=draw(st.booleans()),
        error_maps_to_changelog=draw(st.booleans()),
        is_codemod_followup=draw(st.booleans()),
        last_confidence=draw(opt_conf),
        diagnosis_confidence=draw(opt_conf),
        policy_denials_in_step=draw(st.integers(0, 4)),
        schema_invalid_in_step=draw(st.booleans()),
        critic_verdict=draw(st.none() | st.sampled_from(list(CriticDecision))),
        explorer_flags=draw(st.frozensets(st.sampled_from(list(ExplorerFlag))))
        if role is Role.EXPLORER
        else frozenset(),
        pr_template_rejected=draw(st.booleans()) if role is Role.PR_WRITER else False,
        sticky=StickyState(
            tier_escalated=draw(st.booleans()),
            effort_escalated=draw(st.booleans()),
            trigger_fired=draw(st.booleans()),
        ),
    )


@st.composite
def policies(draw: st.DrawFn) -> RoutingPolicies:
    policy = draw(st.sampled_from(list(RoutingPolicy)))
    if policy is RoutingPolicy.ALL_OPUS:
        effort = EffortPolicy.ALL_HIGH
    else:
        effort = draw(st.sampled_from(list(EffortPolicy)))
    pins: dict[Role, RolePin] = {}
    if policy is RoutingPolicy.ROUTED:
        for role in draw(st.frozensets(st.sampled_from(list(Role)), max_size=3)):
            pin = RolePin(
                tier=draw(st.none() | st.sampled_from(list(Tier))),
                effort=draw(st.none() | st.sampled_from(list(Effort))),
            )
            if not pin_problems({role: pin}, policy):
                pins[role] = pin
    return RoutingPolicies(policy=policy, effort_policy=effort, pins=pins)


# ---------------------------------------------------------------- value types


def test_conformance() -> None:
    assert isinstance(FixedRouter(), ModelRouter)


@pytest.mark.parametrize("flag", ["tier_escalated", "effort_escalated", "trigger_fired"])
def test_sticky_covers_each_flag(flag: str) -> None:
    on = StickyState(**{flag: True})
    assert on.covers(StickyState())
    assert on.covers(on)
    assert on.any
    assert not StickyState().covers(on)
    assert not StickyState().any
    others = StickyState(**{f: True for f in StickyState.model_fields if f != flag})
    assert not others.covers(on)


def test_context_invariants() -> None:
    _ctx()
    _stepless(Role.PLANNER)
    _stepless(Role.EXPLORER, caller=Role.CRITIC)
    for bad, match in [
        ({"role": Role.EXPLORER}, "caller is set exactly"),
        ({"caller": Role.CRITIC}, "caller is set exactly"),
        ({"role": Role.EXPLORER, "caller": Role.EXPLORER}, "has no `explore` tool"),
        ({"role": Role.EXPLORER, "caller": Role.PR_WRITER}, "has no `explore` tool"),
        ({"role": Role.EXPLORER, "caller": Role.B3_GENERIC}, "has no `explore` tool"),
        ({"step_type": None, "attempt_in_step": 0}, "inside a plan step"),
        ({"attempt_in_step": 0}, "attempt_in_step"),
        ({"explorer_flags": frozenset({ExplorerFlag.OVER_CAP})}, "explorer_flags"),
        ({"pr_template_rejected": True}, "pr_template_rejected"),
        ({"last_confidence": 1.5}, "less than or equal"),
    ]:
        with pytest.raises(ValidationError, match=match):
            _ctx(**bad)
    with pytest.raises(ValidationError, match="never run inside a plan step"):
        _ctx(role=Role.PLANNER)


def test_inputs_hash_is_deterministic_and_sensitive() -> None:
    a = _stepless(Role.EXPLORER, caller=Role.EDITOR, explorer_flags=frozenset(ExplorerFlag))
    b = RoutingContext.model_validate_json(a.model_dump_json())
    assert a.inputs_hash(ROUTED) == b.inputs_hash(ROUTED)
    assert a.model_dump(mode="json")["explorer_flags"] == sorted(f.value for f in ExplorerFlag)
    assert a.inputs_hash(ROUTED) != a.inputs_hash(ALL_HIGH)
    assert _ctx().inputs_hash(ROUTED) != _ctx(attempts_on_signature=2).inputs_hash(ROUTED)
    pinned = RoutingPolicies(pins={Role.EDITOR: RolePin(tier=Tier.OPUS)})
    assert _ctx().inputs_hash(ROUTED) != _ctx().inputs_hash(pinned)


def test_policies_all_opus_implies_all_high() -> None:
    RoutingPolicies(policy=RoutingPolicy.ALL_OPUS, effort_policy=EffortPolicy.ALL_HIGH)
    with pytest.raises(ValidationError, match="all_opus implies"):
        RoutingPolicies(policy=RoutingPolicy.ALL_OPUS)


@pytest.mark.parametrize(
    "pin",
    [
        {Role.PLANNER: RolePin(effort=Effort.MEDIUM)},
        {Role.EDITOR: RolePin(tier=Tier.OPUS, effort=Effort.MEDIUM)},
        {Role.CRITIC: RolePin(tier=Tier.SONNET)},
        {Role.B2_SINGLE_SHOT: RolePin(tier=Tier.OPUS)},
    ],
)
def test_policies_revalidate_pins(pin: dict[Role, RolePin]) -> None:
    with pytest.raises(ValidationError):
        RoutingPolicies(pins=pin)


def test_outcome_invariants() -> None:
    ctx = _ctx()
    h = ctx.inputs_hash(ROUTED)
    none = StickyState()
    with pytest.raises(ValidationError, match="Opus never"):
        RouteOutcome(
            tier=Tier.OPUS,
            effort=Effort.MEDIUM,
            reason_codes=("x",),
            new_sticky=none,
            inputs_hash=h,
        )
    with pytest.raises(ValidationError, match="must be opus"):
        _out(ctx, ROUTED, Tier.SONNET, Effort.HIGH, escalated_from=Tier.SONNET)
    with pytest.raises(ValidationError, match="must be high"):
        _out(ctx, ROUTED, Tier.SONNET, Effort.MEDIUM, effort_escalated_from=Effort.MEDIUM)
    with pytest.raises(ValidationError):
        RouteOutcome(
            tier=Tier.OPUS, effort=Effort.HIGH, reason_codes=(), new_sticky=none, inputs_hash=h
        )
    with pytest.raises(ValidationError):
        RouteOutcome(
            tier=Tier.OPUS,
            effort=Effort.HIGH,
            reason_codes=("x",),
            new_sticky=none,
            inputs_hash="x",
        )


def test_to_decision_wraps_only_its_own_context() -> None:
    ctx = _ctx()
    out = _out(ctx, ROUTED, Tier.OPUS, Effort.HIGH, escalated_from=Tier.SONNET)
    ids: dict[str, Any] = {"decision_id": RoutingDecisionId("rd-1"), "run_id": RunId("run-1")}
    d = out.to_decision(**ids, step_id=StepId("s"), ctx=ctx, policies=ROUTED)
    assert (d.role, d.tier, d.effort, d.escalated_from, d.inputs_hash) == (
        Role.EDITOR,
        Tier.OPUS,
        Effort.HIGH,
        Tier.SONNET,
        out.inputs_hash,
    )
    with pytest.raises(ValueError, match="different context"):
        out.to_decision(
            **ids, step_id=StepId("s"), ctx=_ctx(attempts_on_signature=3), policies=ROUTED
        )
    with pytest.raises(ValueError, match="step_id"):
        out.to_decision(**ids, step_id=None, ctx=ctx, policies=ROUTED)


# ---------------------------------------------------------------- triggers and conditions


@pytest.mark.parametrize(
    ("kw", "trigger"),
    [
        ({"attempts_on_signature": 2}, T.SECOND_ATTEMPT_ON_SIGNATURE),
        ({"prev_outcome": Outcome.SAME}, T.NO_IMPROVEMENT),
        ({"prev_outcome": Outcome.REGRESSED}, T.NO_IMPROVEMENT),
        ({"schema_invalid_in_step": True}, T.SCHEMA_INVALID_OR_DENIALS),
        ({"policy_denials_in_step": 2}, T.SCHEMA_INVALID_OR_DENIALS),
        ({"failure_category": FailureCategory.API_BEHAVIOR_CHANGED}, T.HARD_CATEGORY),
        ({"failure_category": FailureCategory.TRANSITIVE_INCOMPAT}, T.HARD_CATEGORY),
        ({"failure_category": FailureCategory.TEST_INFRA}, T.HARD_CATEGORY),
        ({"failure_category": FailureCategory.NO_PROGRESS}, T.HARD_CATEGORY),
        ({"last_confidence": 0.69}, T.LOW_CONFIDENCE),
        ({"critic_verdict": CriticDecision.REVISE}, T.CRITIC_REVISE),
        ({"step_type": StepType.TEST_TOUCH}, T.TEST_TOUCH),
        ({"expected_files": 4}, T.LARGE_EDIT),
        ({"expected_lines": 151}, T.LARGE_EDIT),
    ],
)
def test_each_trigger_fires_alone(kw: dict[str, Any], trigger: EscalationTrigger) -> None:
    assert fired_triggers(_ctx(**kw)) == {trigger}


@pytest.mark.parametrize(
    "kw",
    [
        {"attempts_on_signature": 1},
        {"prev_outcome": Outcome.IMPROVED},
        {"policy_denials_in_step": 1},
        {"failure_category": FailureCategory.COMPILE_TYPE},
        {"last_confidence": 0.7},
        {"critic_verdict": CriticDecision.APPROVE},
        {"expected_files": 3, "expected_lines": 150},
        {"expected_files": None, "expected_lines": None},
    ],
)
def test_triggers_stay_quiet_at_the_boundary(kw: dict[str, Any]) -> None:
    assert fired_triggers(_ctx(**kw)) == frozenset()


def test_sonnet_conditions_per_role() -> None:
    editor_ok = {"expected_files": 3, "expected_lines": 150, "is_codemod_followup": True}
    assert sonnet_allowed_by_default(_ctx(**editor_ok, diagnosis_confidence=0.8))
    assert not sonnet_allowed_by_default(_ctx(**editor_ok, diagnosis_confidence=0.79))
    assert not sonnet_allowed_by_default(_ctx(**editor_ok))  # unknown confidence
    assert not sonnet_allowed_by_default(
        _ctx(**{**TRIVIAL, "expected_files": None})
    )  # unknown size
    assert not sonnet_allowed_by_default(_ctx(**{**TRIVIAL, "has_changelog_mapping": False}))
    diag = {
        "role": Role.DIAGNOSER,
        "failure_category": FailureCategory.API_REMOVED,
        "error_maps_to_changelog": True,
        "attempts_on_signature": 1,
    }
    assert sonnet_allowed_by_default(_ctx(**diag))
    assert not sonnet_allowed_by_default(_ctx(**{**diag, "attempts_on_signature": 2}))
    assert not sonnet_allowed_by_default(_ctx(**{**diag, "error_maps_to_changelog": False}))
    cat = FailureCategory.ENV_INSTALL
    assert not sonnet_allowed_by_default(_ctx(**{**diag, "failure_category": cat}))
    assert sonnet_allowed_by_default(_stepless(Role.ANALYZER, usage_sites=50))
    assert not sonnet_allowed_by_default(_stepless(Role.ANALYZER, usage_sites=51))
    assert not sonnet_allowed_by_default(_stepless(Role.ANALYZER))
    replace = MigrationKind.REPLACE
    assert not sonnet_allowed_by_default(
        RoutingContext(role=Role.ANALYZER, migration_kind=replace, usage_sites=1)
    )
    assert not sonnet_allowed_by_default(_stepless(Role.PLANNER))


def test_trivial_edit() -> None:
    assert is_trivial_edit(_ctx(**TRIVIAL))
    for bad in [
        {"attempt_in_step": 2},
        {"expected_files": 2},
        {"expected_lines": 41},
        {"diagnosis_confidence": 0.89},
        {"has_changelog_mapping": False},
    ]:
        assert not is_trivial_edit(_ctx(**{**TRIVIAL, **bad})), bad
    assert is_trivial_edit(
        _ctx(**{**TRIVIAL, "has_changelog_mapping": False}, is_codemod_followup=True)
    )
    assert not is_trivial_edit(_ctx(role=Role.DIAGNOSER, **TRIVIAL))


# ---------------------------------------------------------------- the oracle, case by case


@pytest.mark.parametrize(
    ("ctx", "pol", "tier", "effort", "fragment"),
    [
        (_stepless(Role.B2_SINGLE_SHOT), ALL_SONNET, Tier.SONNET, Effort.HIGH, "B8"),
        (_stepless(Role.PLANNER), ROUTED, Tier.SONNET, Effort.HIGH, "never routed down"),
        (_stepless(Role.CRITIC), ALL_SONNET, Tier.SONNET, Effort.HIGH, "never routed down"),
        (_ctx(role=Role.DIAGNOSER), ROUTED, Tier.SONNET, Effort.MEDIUM, "high effort"),
        (_stepless(Role.ANALYZER), ROUTED, Tier.SONNET, Effort.MEDIUM, "high effort"),
        (
            _stepless(Role.EXPLORER, caller=Role.EDITOR),
            ALL_OPUS,
            Tier.SONNET,
            Effort.HIGH,
            "all_opus",
        ),
        (_ctx(), ALL_SONNET, Tier.OPUS, Effort.HIGH, "all_sonnet"),
        (_stepless(Role.PR_WRITER), ALL_HIGH, Tier.SONNET, Effort.MEDIUM, "all_high"),
        (
            _stepless(Role.EXPLORER, caller=Role.PLANNER),
            ROUTED,
            Tier.OPUS,
            Effort.HIGH,
            "stays on sonnet",
        ),
        (_stepless(Role.PR_WRITER), ROUTED, Tier.OPUS, Effort.HIGH, "stays on sonnet"),
        (
            _ctx(step_type=StepType.TEST_TOUCH, **TRIVIAL),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "test_touch never",
        ),
        (
            _ctx(step_type=StepType.TEST_TOUCH, **TRIVIAL),
            ROUTED,
            Tier.SONNET,
            Effort.HIGH,
            "t7_test_touch",
        ),
        (
            _stepless(Role.EXPLORER, caller=Role.CRITIC),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "for the Critic",
        ),
        (
            _ctx(step_type=StepType.CONFIG, touches_test_config=True, **TRIVIAL),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "test configs",
        ),
        (
            _ctx(sticky=StickyState(effort_escalated=True), **TRIVIAL),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "never returns",
        ),
        (
            _ctx(sticky=StickyState(tier_escalated=True), **TRIVIAL),
            ROUTED,
            Tier.SONNET,
            Effort.HIGH,
            "keeps the role on opus",
        ),
        (_ctx(), ROUTED, Tier.SONNET, Effort.HIGH, "Sonnet conditions"),
        (_ctx(**TRIVIAL, attempt_in_step=2), ROUTED, Tier.SONNET, Effort.MEDIUM, "trivial edit"),
        (
            _ctx(**TRIVIAL, attempts_on_signature=2),
            ROUTED,
            Tier.SONNET,
            Effort.HIGH,
            "goes to opus",
        ),
        (
            _ctx(**TRIVIAL, last_confidence=0.5),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "effort must be high",
        ),
        # A trigger earlier in the step rules out medium for a later Explorer call (review #2).
        (
            _stepless(Role.EXPLORER, caller=Role.EDITOR, sticky=StickyState(trigger_fired=True)),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "earlier in scope",
        ),
        (
            _stepless(Role.EXPLORER, caller=Role.EDITOR, sticky=StickyState(tier_escalated=True)),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "earlier in scope",
        ),
        (
            _stepless(
                Role.EXPLORER,
                caller=Role.EDITOR,
                explorer_flags=frozenset({ExplorerFlag.NO_CITATIONS}),
            ),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "flagged Explorer",
        ),
        (
            _stepless(Role.PR_WRITER, pr_template_rejected=True),
            ROUTED,
            Tier.SONNET,
            Effort.MEDIUM,
            "rejected PR",
        ),
    ],
)
def test_oracle_flags(
    ctx: RoutingContext, pol: RoutingPolicies, tier: Tier, effort: Effort, fragment: str
) -> None:
    problems = _problems(ctx, pol, tier, effort)
    assert any(fragment in p for p in problems), problems


def test_oracle_requires_fired_triggers_to_be_recorded() -> None:
    ctx = _ctx(attempts_on_signature=2)
    out = _out(ctx, ROUTED, Tier.OPUS, Effort.HIGH, new_sticky=StickyState())
    assert any("recorded in new_sticky" in p for p in route_invariant_violations(ctx, ROUTED, out))


def test_oracle_flags_de_escalation_hash_and_bad_escalated_from() -> None:
    ctx = _ctx(sticky=StickyState(tier_escalated=True))
    dropped = _out(ctx, ROUTED, Tier.OPUS, Effort.HIGH, new_sticky=StickyState())
    assert any("de-escalates" in p for p in route_invariant_violations(ctx, ROUTED, dropped))
    other = _out(_ctx(attempts_on_signature=2), ROUTED, Tier.OPUS, Effort.HIGH)
    assert any("inputs_hash" in p for p in route_invariant_violations(_ctx(), ROUTED, other))
    explorer = _stepless(Role.EXPLORER, caller=Role.EDITOR)
    escalated = _out(explorer, ALL_HIGH, Tier.OPUS, Effort.HIGH, escalated_from=Tier.SONNET)
    problems = route_invariant_violations(explorer, ALL_HIGH, escalated)
    assert any("only routed roles" in p for p in problems)


@pytest.mark.parametrize(
    ("ctx", "pol", "tier", "effort"),
    [
        (_ctx(), ROUTED, Tier.OPUS, Effort.HIGH),
        (_ctx(**TRIVIAL), ROUTED, Tier.SONNET, Effort.MEDIUM),  # a trivial Sonnet edit (§8.4)
        (_ctx(**TRIVIAL, attempt_in_step=2), ROUTED, Tier.SONNET, Effort.HIGH),
        (_stepless(Role.EXPLORER, caller=Role.EDITOR), ROUTED, Tier.SONNET, Effort.MEDIUM),
        (_stepless(Role.EXPLORER, caller=Role.CRITIC), ROUTED, Tier.SONNET, Effort.HIGH),
        (_stepless(Role.PR_WRITER), ROUTED, Tier.SONNET, Effort.MEDIUM),
        (_stepless(Role.PLANNER), ALL_SONNET, Tier.OPUS, Effort.HIGH),
        (_stepless(Role.B3_GENERIC), ALL_SONNET, Tier.OPUS, Effort.HIGH),
        (_ctx(role=Role.DIAGNOSER, attempts_on_signature=3), ALL_SONNET, Tier.SONNET, Effort.HIGH),
        (_stepless(Role.EXPLORER, caller=Role.EDITOR), ALL_OPUS, Tier.OPUS, Effort.HIGH),
        (_stepless(Role.ANALYZER, usage_sites=10), ROUTED, Tier.SONNET, Effort.HIGH),
    ],
)
def test_oracle_accepts_legal_outcomes(
    ctx: RoutingContext, pol: RoutingPolicies, tier: Tier, effort: Effort
) -> None:
    assert _problems(ctx, pol, tier, effort) == []


# ---------------------------------------------------------------- pins (SPEC §8.4 "Pins")


def test_pin_to_opus_lifts_the_sonnet_fixed_rule_and_is_honoured() -> None:
    pol = RoutingPolicies(pins={Role.PR_WRITER: RolePin(tier=Tier.OPUS)})
    ctx = _stepless(Role.PR_WRITER)
    assert _problems(ctx, pol, Tier.OPUS, Effort.HIGH) == []
    assert any("pinned to opus" in p for p in _problems(ctx, pol, Tier.SONNET, Effort.HIGH))


def test_effort_only_pin_keeps_a_sonnet_fixed_role_on_sonnet() -> None:
    pol = RoutingPolicies(pins={Role.PR_WRITER: RolePin(effort=Effort.HIGH)})
    ctx = _stepless(Role.PR_WRITER)
    assert any("stays on sonnet" in p for p in _problems(ctx, pol, Tier.OPUS, Effort.HIGH))
    assert any("pinned to high" in p for p in _problems(ctx, pol, Tier.SONNET, Effort.MEDIUM))


def test_sonnet_pin_never_overrides_trigger_7() -> None:
    pol = RoutingPolicies(pins={Role.EDITOR: RolePin(tier=Tier.SONNET)})
    ctx = _ctx(step_type=StepType.TEST_TOUCH)
    assert any("t7_test_touch" in p for p in _problems(ctx, pol, Tier.SONNET, Effort.HIGH))
    assert _problems(ctx, pol, Tier.OPUS, Effort.HIGH) == []


def test_sonnet_pin_replaces_only_the_default_conditions() -> None:
    pol = RoutingPolicies(pins={Role.EDITOR: RolePin(tier=Tier.SONNET)})
    assert _problems(_ctx(), pol, Tier.SONNET, Effort.HIGH) == []  # unknown size is fine pinned
    assert _problems(_ctx(), ROUTED, Tier.SONNET, Effort.HIGH) != []  # but not unpinned


@pytest.mark.parametrize("role", [Role.EDITOR, Role.EXPLORER, Role.PR_WRITER])
def test_medium_can_never_be_pinned(role: Role) -> None:
    """Owner decision (2026-11-20): medium comes only from the §8.4 rules, never from a pin."""
    for tier in (None, Tier.SONNET):
        with pytest.raises(ValidationError, match="cannot be pinned"):
            RoutingPolicies(pins={role: RolePin(tier=tier, effort=Effort.MEDIUM)})
    # An unpinned trivial Editor edit still gets medium, so nothing is lost.
    assert _problems(_ctx(**TRIVIAL), ROUTED, Tier.SONNET, Effort.MEDIUM) == []


# ---------------------------------------------------------------- the oracle, properties


SONNET_RULES = ("stays on sonnet", "all_sonnet")


@given(contexts(), policies())
def test_opus_high_is_safe_except_where_sonnet_is_required(
    ctx: RoutingContext, pol: RoutingPolicies
) -> None:
    """Opus/high that keeps the sticky state (plus any trigger it fired) never breaks a
    quality rule. The only rules it can break are the ones that require Sonnet: the B8
    ablation and the Sonnet-fixed roles."""
    problems = _problems(ctx, pol, Tier.OPUS, Effort.HIGH)
    assert all(any(rule in p for rule in SONNET_RULES) for p in problems), problems


@given(contexts(), policies())
def test_medium_passes_only_where_the_spec_allows_it(
    ctx: RoutingContext, pol: RoutingPolicies
) -> None:
    if _problems(ctx, pol, Tier.SONNET, Effort.MEDIUM) == []:
        assert ctx.role in MEDIUM_ELIGIBLE_ROLES
        assert pol.effort_policy is EffortPolicy.ROUTED
        assert pol.policy is not RoutingPolicy.ALL_OPUS
        assert ctx.step_type is not StepType.TEST_TOUCH
        assert ctx.caller is not Role.CRITIC
        assert not ctx.sticky.any
        assert not fired_triggers(ctx)
        assert not (ctx.step_type is StepType.CONFIG and ctx.touches_test_config)
        assert not ctx.explorer_flags
        assert not ctx.pr_template_rejected
        if ctx.role is Role.EDITOR:
            assert is_trivial_edit(ctx)
        pin = pol.pins.get(ctx.role)
        assert pin is None or (pin.effort is not Effort.HIGH and pin.tier is not Tier.OPUS)


@given(contexts(), policies())
def test_any_fired_trigger_forces_high_and_opus_for_routed_roles(
    ctx: RoutingContext, pol: RoutingPolicies
) -> None:
    if not fired_triggers(ctx):
        return
    assert _problems(ctx, pol, Tier.SONNET, Effort.MEDIUM) != []
    if pol.policy is RoutingPolicy.ROUTED and ctx.role in ROUTED_ROLES:
        assert _problems(ctx, pol, Tier.SONNET, Effort.HIGH) != []


@given(contexts(), policies(), st.sampled_from(list(Tier)), st.sampled_from(list(Effort)))
def test_dropping_a_sticky_flag_is_always_flagged(
    ctx: RoutingContext, pol: RoutingPolicies, tier: Tier, effort: Effort
) -> None:
    if not ctx.sticky.any or (tier is Tier.OPUS and effort is Effort.MEDIUM):
        return
    out = _out(ctx, pol, tier, effort, new_sticky=StickyState())
    assert any("de-escalates" in p for p in route_invariant_violations(ctx, pol, out))


@given(contexts(), policies())
def test_inputs_hash_survives_a_json_round_trip(ctx: RoutingContext, pol: RoutingPolicies) -> None:
    again = RoutingContext.model_validate_json(ctx.model_dump_json())
    assert again.inputs_hash(pol) == ctx.inputs_hash(pol)


@given(contexts(), policies())
def test_fixed_router_outcomes_wrap_into_decisions(
    ctx: RoutingContext, pol: RoutingPolicies
) -> None:
    out = FixedRouter().route(ctx, pol)
    step = StepId("s") if ctx.step_type is not None else None
    d = out.to_decision(
        decision_id=RoutingDecisionId("rd"), run_id=RunId("r"), step_id=step, ctx=ctx, policies=pol
    )
    assert d.inputs_hash == ctx.inputs_hash(pol)
    assert out.new_sticky.trigger_fired == (ctx.sticky.trigger_fired or bool(fired_triggers(ctx)))


# ---------------------------------------------------------------- second review: stickiness


def _recorded(problems: list[str], fragment: str) -> bool:
    return any(fragment in p for p in problems)


def test_trigger_on_routed_role_must_record_the_tier_escalation() -> None:
    ctx = _ctx(**TRIVIAL, last_confidence=0.5)
    lost = StickyState(trigger_fired=True)  # tier_escalated missing
    out = _out(ctx, ROUTED, Tier.OPUS, Effort.HIGH, escalated_from=Tier.SONNET, new_sticky=lost)
    assert _recorded(
        route_invariant_violations(ctx, ROUTED, out), "tier escalation must be recorded"
    )


def test_no_return_to_sonnet_later_in_the_step() -> None:
    """Reviewer probe: call 1 escalates on low confidence; call 2 in the same step has no
    trigger of its own but must stay on Opus."""
    call1 = _ctx(**TRIVIAL, last_confidence=0.5)
    out1 = _out(call1, ROUTED, Tier.OPUS, Effort.HIGH, escalated_from=Tier.SONNET)
    assert route_invariant_violations(call1, ROUTED, out1) == []
    call2 = _ctx(**TRIVIAL, sticky=out1.new_sticky)
    assert _recorded(_problems(call2, ROUTED, Tier.SONNET, Effort.HIGH), "keeps the role on opus")
    assert _problems(call2, ROUTED, Tier.OPUS, Effort.HIGH) == []


def test_escalated_from_without_sticky_record_is_flagged() -> None:
    ctx = _ctx()
    out = _out(
        ctx, ROUTED, Tier.OPUS, Effort.HIGH, escalated_from=Tier.SONNET, new_sticky=StickyState()
    )
    assert _recorded(
        route_invariant_violations(ctx, ROUTED, out), "escalated_from is set but not recorded"
    )


@pytest.mark.parametrize(
    "ctx",
    [
        _stepless(
            Role.EXPLORER, caller=Role.EDITOR, explorer_flags=frozenset({ExplorerFlag.NO_CITATIONS})
        ),
        _stepless(Role.PR_WRITER, pr_template_rejected=True),
    ],
)
def test_forced_effort_escalation_sticks_to_the_next_call(ctx: RoutingContext) -> None:
    lost = _out(ctx, ROUTED, Tier.SONNET, Effort.HIGH, new_sticky=StickyState())
    assert _recorded(
        route_invariant_violations(ctx, ROUTED, lost), "effort escalation must be recorded"
    )
    out = _out(ctx, ROUTED, Tier.SONNET, Effort.HIGH, effort_escalated_from=Effort.MEDIUM)
    assert route_invariant_violations(ctx, ROUTED, out) == []
    nxt = ctx.model_copy(
        update={
            "explorer_flags": frozenset(),
            "pr_template_rejected": False,
            "sticky": out.new_sticky,
        }
    )
    assert _recorded(_problems(nxt, ROUTED, Tier.SONNET, Effort.MEDIUM), "earlier in scope")


def test_effort_escalated_from_needs_a_medium_eligible_role() -> None:
    ctx = _ctx(role=Role.DIAGNOSER)
    out = _out(
        ctx,
        ROUTED,
        Tier.OPUS,
        Effort.HIGH,
        effort_escalated_from=Effort.MEDIUM,
        new_sticky=StickyState(effort_escalated=True),
    )
    assert _recorded(route_invariant_violations(ctx, ROUTED, out), "cannot escalate from it")


def test_sticky_flag_needs_an_earlier_escalation() -> None:
    ctx = _ctx()
    out = _out(ctx, ROUTED, Tier.OPUS, Effort.HIGH, sticky=True)
    assert _recorded(route_invariant_violations(ctx, ROUTED, out), "earlier escalation in scope")
    later = _ctx(sticky=StickyState(tier_escalated=True))
    assert (
        route_invariant_violations(
            later, ROUTED, _out(later, ROUTED, Tier.OPUS, Effort.HIGH, sticky=True)
        )
        == []
    )


#: Inputs that fire triggers or force effort on their own; cleared for the "next call" so
#: only the recorded sticky state can forbid de-escalation.
_QUIET = {
    "attempts_on_signature": 1,
    "prev_outcome": None,
    "schema_invalid_in_step": False,
    "policy_denials_in_step": 0,
    "failure_category": None,
    "last_confidence": None,
    "critic_verdict": None,
    "expected_files": None,
    "expected_lines": None,
    "explorer_flags": frozenset(),
    "pr_template_rejected": False,
}

_stickies = st.builds(
    StickyState,
    tier_escalated=st.booleans(),
    effort_escalated=st.booleans(),
    trigger_fired=st.booleans(),
)


@settings(max_examples=600)
@given(
    contexts(),
    policies(),
    st.sampled_from(list(Tier)),
    st.sampled_from(list(Effort)),
    _stickies,
    st.booleans(),
    st.booleans(),
)
def test_every_legal_outcome_leaves_a_state_that_forbids_de_escalation(
    ctx: RoutingContext,
    pol: RoutingPolicies,
    tier: Tier,
    effort: Effort,
    new_sticky: StickyState,
    from_sonnet: bool,
    from_medium: bool,
) -> None:
    """Whatever a legal outcome records, the next call in the same scope (with every trigger
    and effort-forcing input cleared) cannot undo it."""
    if tier is Tier.OPUS and effort is Effort.MEDIUM:
        return
    if (from_sonnet and tier is not Tier.OPUS) or (from_medium and effort is not Effort.HIGH):
        return
    floor = minimal_sticky_after(ctx, pol)  # every legal state covers it; add extra flags on top
    new_sticky = StickyState(
        tier_escalated=floor.tier_escalated or new_sticky.tier_escalated,
        effort_escalated=floor.effort_escalated or new_sticky.effort_escalated,
        trigger_fired=floor.trigger_fired or new_sticky.trigger_fired,
    )
    out = _out(
        ctx,
        pol,
        tier,
        effort,
        new_sticky=new_sticky,
        escalated_from=Tier.SONNET if from_sonnet else None,
        effort_escalated_from=Effort.MEDIUM if from_medium else None,
    )
    if route_invariant_violations(ctx, pol, out):
        return
    quiet = {k: v for k, v in _QUIET.items() if k not in ("explorer_flags", "pr_template_rejected")}
    if ctx.role is Role.EXPLORER:
        quiet["explorer_flags"] = frozenset()
    if ctx.role is Role.PR_WRITER:
        quiet["pr_template_rejected"] = False
    if ctx.step_type is StepType.TEST_TOUCH:
        return  # trigger 7 is the step itself; it can't be cleared within the scope
    nxt = RoutingContext.model_validate({**ctx.model_dump(), **quiet, "sticky": out.new_sticky})
    assert not fired_triggers(nxt)
    if (
        fired_triggers(ctx)
        or from_sonnet
        or from_medium
        or ctx.explorer_flags
        or ctx.pr_template_rejected
    ):
        assert _problems(nxt, pol, Tier.SONNET, Effort.MEDIUM) != []
    if out.new_sticky.any:
        assert _problems(nxt, pol, Tier.SONNET, Effort.MEDIUM) != []
    tier_escalated_here = from_sonnet or (
        fired_triggers(ctx) and pol.policy is RoutingPolicy.ROUTED
    )
    if tier_escalated_here and ctx.role in ROUTED_ROLES:
        assert _problems(nxt, pol, Tier.SONNET, Effort.HIGH) != []
