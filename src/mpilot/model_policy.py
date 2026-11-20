"""NFR-12 rules as pure functions (SPEC §8.2 "The config MUST reject", §8.4, §8.1 items 8-9).

- Only Opus 5.5 and Sonnet 5.5. A model ID containing "haiku" (any case) is never valid,
  in a model field or in the value of any `ANTHROPIC_*` setting.
- Effort is `medium` or `high` (the `Effort` enum makes anything else unrepresentable).
- Opus always runs at `high`. `medium` is only for the clearly mechanical Sonnet cases:
  a §8.4 trivial Sonnet edit by the Editor, the Explorer and the PR Writer. Never for the Planner,
  Critic, Diagnoser or Analyzer, never with Opus, never under `all_high`/`all_opus`.
- The Planner and Critic are never routed down; B2 and B3 are always Opus/high whatever the
  config (B8), so they cannot be pinned.
- No API key in v1 (Q9, NFR-11).

`anthropic_env_problems` is shared with the runtime lockdown (P4-08), which must apply the
same rules to the environment it actually hands to the SDK.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from pydantic import BaseModel, ConfigDict

from mpilot.core.types import Effort, EffortPolicy, Role, RoutingPolicy, Tier

ANTHROPIC_PREFIX = "ANTHROPIC_"

#: Roles that must never run at medium effort (§8.2).
HIGH_ONLY_ROLES = frozenset({Role.PLANNER, Role.CRITIC, Role.DIAGNOSER, Role.ANALYZER})
#: Roles that are always Opus (§8.4 default tier table: never routed down).
OPUS_FIXED_ROLES = frozenset({Role.PLANNER, Role.CRITIC})
#: Benchmark baselines: always Opus/high whatever the config (B8).
UNPINNABLE_ROLES = frozenset({Role.B2_SINGLE_SHOT, Role.B3_GENERIC})


class RolePin(BaseModel):
    """Pin a role's default tier and/or effort (SPEC §8.4 "Pins"). A pin replaces only the
    default: escalation triggers, effort escalation, stickiness and the medium conditions
    always apply on top, so a pin never lowers a choice those rules force."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: Tier | None = None
    effort: Effort | None = None


def mentions_haiku(value: str) -> bool:
    return "haiku" in value.lower()


def model_id_problems(opus: str, sonnet: str) -> list[str]:
    problems: list[str] = []
    for tier, value in (("opus", opus), ("sonnet", sonnet)):
        if mentions_haiku(value):
            problems.append(f"models.{tier}: Haiku is never allowed (NFR-12)")
        elif tier not in value.lower():
            problems.append(f"models.{tier}: {value!r} is not a {tier} model ID")
    return problems


def anthropic_env_problems(env: Mapping[str, str], model_ids: Collection[str]) -> list[str]:
    """Problems with a set of `ANTHROPIC_*` variables destined for the Agent SDK.

    A variable *named* after Haiku is fine as long as it is pinned to a configured model:
    that is exactly how the background model is pinned to Sonnet (§8.1 item 9).
    """
    problems: list[str] = []
    for name, value in env.items():
        if not name.startswith(ANTHROPIC_PREFIX):
            problems.append(f"{name}: only {ANTHROPIC_PREFIX}* variables are allowed here")
            continue
        upper = name.upper()
        if "API_KEY" in upper or "AUTH_TOKEN" in upper:
            problems.append(f"{name}: no API key or token is configured here (Q9, NFR-11)")
        if mentions_haiku(value):
            problems.append(f"{name}: value mentions Haiku, which is never allowed (NFR-12)")
        elif name.endswith("_MODEL") and value not in model_ids:
            problems.append(f"{name}: {value!r} is not one of the configured models")
    return problems


def pin_problems(
    pins: Mapping[Role, RolePin],
    policy: RoutingPolicy,
    effort_policy: EffortPolicy,
) -> list[str]:
    """Problems with per-role pins. `effort_policy` is the one after `all_opus` normalization."""
    problems: list[str] = []
    if pins and policy is not RoutingPolicy.ROUTED:
        problems.append(f"routing.pins: pins need routing.policy = routed, not {policy.value}")
    for role, pin in pins.items():
        where = f"routing.pins.{role.value}"
        if pin.tier is None and pin.effort is None:
            problems.append(f"{where}: a pin must set a tier or an effort")
        if role in UNPINNABLE_ROLES:
            problems.append(f"{where}: B2 and B3 always run Opus at high effort (B8)")
        if role in OPUS_FIXED_ROLES and pin.tier is Tier.SONNET:
            problems.append(f"{where}: the {role.value} is never routed down from Opus (§8.4)")
        if pin.effort is not Effort.MEDIUM:
            continue
        if pin.tier is Tier.OPUS:
            problems.append(f"{where}: medium effort can never be pinned onto Opus (NFR-12)")
        if role in HIGH_ONLY_ROLES:
            problems.append(f"{where}: the {role.value} always runs at high effort (NFR-12)")
        if role is Role.EDITOR and pin.tier is not Tier.SONNET:
            problems.append(f"{where}: medium for the editor also needs tier = sonnet")
        if effort_policy is EffortPolicy.ALL_HIGH:
            problems.append(
                f"{where}: medium conflicts with effort_policy = all_high (or all_opus)"
            )
    return problems
