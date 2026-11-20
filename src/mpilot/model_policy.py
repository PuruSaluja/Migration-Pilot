"""NFR-12 rules as pure functions (SPEC §8.2 "The config MUST reject", §8.4, §8.1 items 8-9).

- Only Opus 5.5 and Sonnet 5.5. A model ID containing "haiku" (any case) is never valid,
  in a model field or in the value of any `ANTHROPIC_*` variable.
- Effort is `medium` or `high` (the `Effort` enum makes anything else unrepresentable).
- Opus always runs at `high`. `medium` is only for the clearly mechanical Sonnet cases:
  a §8.4 trivial Sonnet edit by the Editor, the Explorer and the PR Writer. Never for the Planner,
  Critic, Diagnoser or Analyzer, never with Opus, never under `all_high`/`all_opus`.
  Medium is chosen only by the router's §8.4 rules and can never be pinned.
- The Planner and Critic are never routed down; B2 and B3 are always Opus/high whatever the
  config (B8), so they cannot be pinned.
- No API key in v1 (Q9, NFR-11).

`anthropic_env_problems` is shared with the runtime lockdown (P4-08), which must apply the
same rules to the environment it actually hands to the SDK. There is no user setting for
`ANTHROPIC_*` variables at all: the pins are fixed in code (§8.1 items 8-9).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from mpilot.core.types import Effort, Role, RoutingPolicy, Tier

ANTHROPIC_PREFIX = "ANTHROPIC_"

#: Roles that must never run at medium effort (§8.2).
HIGH_ONLY_ROLES = frozenset({Role.PLANNER, Role.CRITIC, Role.DIAGNOSER, Role.ANALYZER})
#: Roles that are always Opus (§8.4 default tier table: never routed down).
OPUS_FIXED_ROLES = frozenset({Role.PLANNER, Role.CRITIC})
#: Benchmark baselines: always Opus/high whatever the config (B8).
UNPINNABLE_ROLES = frozenset({Role.B2_SINGLE_SHOT, Role.B3_GENERIC})


class RolePin(BaseModel):
    """Pin a role's default tier (opus or sonnet) and/or effort (high only; medium is never
    pinned, SPEC §8.4 "Pins"). A pin replaces only the default: escalation triggers, effort
    escalation and stickiness always apply on top, so a pin never lowers a choice they force."""

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


#: The only `ANTHROPIC_*` variables the Agent SDK subprocess may receive (§8.1 items 8-9):
#: model pins, each set to exactly the configured model of the tier named here. The
#: background ("haiku" / small-fast) model is pinned to Sonnet, so Haiku can never run.
#: P4-01 re-verifies the variable names against the SDK; a new one is added here, in code.
SDK_MODEL_PIN_TIERS: Mapping[str, Tier] = MappingProxyType(
    {
        "ANTHROPIC_DEFAULT_OPUS_MODEL": Tier.OPUS,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": Tier.SONNET,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": Tier.SONNET,
        "ANTHROPIC_SMALL_FAST_MODEL": Tier.SONNET,
    }
)


def sdk_model_pins(opus: str, sonnet: str) -> dict[str, str]:
    """The model-pin variables for the SDK subprocess, for the configured model IDs."""
    by_tier = {Tier.OPUS: opus, Tier.SONNET: sonnet}
    return {name: by_tier[tier] for name, tier in SDK_MODEL_PIN_TIERS.items()}


def anthropic_env_problems(env: Mapping[str, str], *, opus: str, sonnet: str) -> list[str]:
    """Problems with the `ANTHROPIC_*` part of an environment destined for the Agent SDK.

    Shared with the runtime lockdown (P4-08), which runs it on the env it actually hands to
    the SDK. Every pin in `SDK_MODEL_PIN_TIERS` must be present with exactly its configured
    value, and no other `ANTHROPIC_*` variable may be there at all: not an API key or token
    (Q9), not `ANTHROPIC_BASE_URL` (it would send the OAuth token elsewhere), not
    `ANTHROPIC_MODEL` (the router picks the model per call). Other names are not judged here:
    the rest of the item-8 allowlist is the env builder's job.
    """
    required = sdk_model_pins(opus, sonnet)
    problems: list[str] = []
    for name, value in sorted(env.items()):
        if not name.upper().startswith(ANTHROPIC_PREFIX):
            continue
        upper = name.upper()
        if "API_KEY" in upper or "AUTH_TOKEN" in upper:
            problems.append(f"{name}: no API key or token is ever passed to the SDK (Q9, NFR-11)")
        elif name not in required:
            problems.append(
                f"{name}: not allowed; the SDK gets only the model pins (§8.1 items 8-9)"
            )
        elif mentions_haiku(value):
            problems.append(f"{name}: value mentions Haiku, which is never allowed (NFR-12)")
        elif value != required[name]:
            problems.append(f"{name}: must be {required[name]!r}, not {value!r} (§8.1 item 9)")
    problems.extend(f"{name}: missing model pin" for name in sorted(set(required) - set(env)))
    return problems


def pin_problems(
    pins: Mapping[Role, RolePin],
    policy: RoutingPolicy,
) -> list[str]:
    """Problems with per-role pins (SPEC §8.4 "Pins")."""
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
        if pin.effort is Effort.MEDIUM:
            # Medium is never pinned: it comes only from the §8.4 effort rules, so a medium pin
            # could never change a result. This also covers every §8.2 rejection (medium on
            # Opus or on the Planner, Critic, Diagnoser or Analyzer).
            problems.append(
                f"{where}: medium effort cannot be pinned; it is chosen only by the §8.4 "
                "effort rules (NFR-12)"
            )
    return problems
