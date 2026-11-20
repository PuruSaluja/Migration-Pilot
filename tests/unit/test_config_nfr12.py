"""P1-06: NFR-12 config validation (SPEC §8.2, §8.4, §8.1 items 8-9, B8, Q9)."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from mpilot.config import Config, config_hash, load_config
from mpilot.core.errors import ConfigError
from mpilot.core.types import Effort, EffortPolicy, Role, RoutingPolicy, Tier
from mpilot.model_policy import anthropic_env_problems

SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"


def _toml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.toml"
    path.write_text(text)
    return path


def _load_toml(tmp_path: Path, text: str) -> Config:
    return load_config(config_file=_toml(tmp_path, text), environ={})


# ---------------------------------------------------------------- Haiku in model fields


@pytest.mark.parametrize("bad", ["claude-haiku-4-5", "HAIKU", "Claude-HaIkU-x", "my-haiku"])
@pytest.mark.parametrize("tier", ["opus", "sonnet"])
def test_haiku_in_any_model_field_is_rejected_from_every_source(
    tmp_path: Path, tier: str, bad: str
) -> None:
    with pytest.raises(ConfigError, match="Haiku"):
        _load_toml(tmp_path, f'[models]\n{tier} = "{bad}"\n')
    with pytest.raises(ConfigError, match="Haiku"):
        load_config(environ={f"MPILOT_MODELS__{tier.upper()}": bad})
    with pytest.raises(ConfigError, match="Haiku"):
        load_config(environ={}, cli_overrides={f"models.{tier}": bad})


def test_a_model_id_must_match_its_tier() -> None:
    with pytest.raises(ConfigError, match="not a opus model ID"):
        load_config(environ={"MPILOT_MODELS__OPUS": SONNET})
    with pytest.raises(ConfigError, match="not a sonnet model ID"):
        load_config(environ={"MPILOT_MODELS__SONNET": OPUS})


def test_the_default_config_mentions_haiku_nowhere() -> None:
    config = load_config(environ={})
    assert "haiku" not in config.model_dump_json().lower()
    assert not [v for v in config.sdk_model_env().values() if "haiku" in v.lower()]


# ---------------------------------------------------------------- SDK model pins (§8.1 items 8-9)


def test_every_sdk_model_alias_is_pinned_and_the_background_model_is_sonnet() -> None:
    assert load_config(environ={}).sdk_model_env() == {
        "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": SONNET,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": SONNET,
        "ANTHROPIC_SMALL_FAST_MODEL": SONNET,
    }


def test_the_pins_follow_the_configured_models() -> None:
    other = "claude-sonnet-5-6"
    env = load_config(environ={"MPILOT_MODELS__SONNET": other}).sdk_model_env()
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["ANTHROPIC_SMALL_FAST_MODEL"] == other
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == OPUS


@pytest.mark.parametrize(
    "toml",
    [
        # the P1-12 review's case: re-pointing the background model, and a base URL that
        # would send the OAuth token to another host
        f'ANTHROPIC_DEFAULT_HAIKU_MODEL = "{OPUS}"',
        f'ANTHROPIC_SMALL_FAST_MODEL = "{OPUS}"',
        'ANTHROPIC_BASE_URL = "https://attacker.example"',
        'ANTHROPIC_LOG = "debug"',
        f'ANTHROPIC_DEFAULT_HAIKU_MODEL = "{SONNET}"',  # even a harmless restatement
    ],
)
def test_there_is_no_anthropic_setting_to_override_anything(tmp_path: Path, toml: str) -> None:
    with pytest.raises(ConfigError, match="anthropic_env"):
        _load_toml(tmp_path, f"[runtime.anthropic_env]\n{toml}\n")


def test_the_env_route_is_closed_too() -> None:
    with pytest.raises(ConfigError, match=r"(?i)anthropic_env"):
        load_config(environ={"MPILOT_RUNTIME__ANTHROPIC_ENV": '{"ANTHROPIC_BASE_URL": "x"}'})


PINS = {
    "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS,
    "ANTHROPIC_DEFAULT_SONNET_MODEL": SONNET,
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": SONNET,
    "ANTHROPIC_SMALL_FAST_MODEL": SONNET,
}


def _problems(env: dict[str, str]) -> list[str]:
    return anthropic_env_problems(env, opus=OPUS, sonnet=SONNET)


def test_the_lockdown_rule_accepts_exactly_the_pins_and_ignores_other_names() -> None:
    assert _problems(PINS) == []
    assert _problems(PINS | {"PATH": "/usr/bin", "HOME": "/home/mpilot-sdk"}) == []  # item 8's job


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ({"ANTHROPIC_BASE_URL": "https://attacker.example"}, "not allowed"),
        ({"ANTHROPIC_MODEL": OPUS}, "not allowed"),
        ({"ANTHROPIC_LOG": "debug"}, "not allowed"),
        ({"anthropic_base_url": "x"}, "not allowed"),  # any case
        ({"ANTHROPIC_API_KEY": "sk-ant-x"}, "no API key"),
        ({"ANTHROPIC_AUTH_TOKEN": "x"}, "no API key"),
        ({"ANTHROPIC_FOO_API_KEY": "x"}, "no API key"),
        ({"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"}, "Haiku"),
        ({"ANTHROPIC_SMALL_FAST_MODEL": "x-HAIKU"}, "Haiku"),
        ({"ANTHROPIC_DEFAULT_HAIKU_MODEL": OPUS}, "must be"),
        ({"ANTHROPIC_SMALL_FAST_MODEL": OPUS}, "must be"),
        ({"ANTHROPIC_DEFAULT_SONNET_MODEL": OPUS}, "must be"),
        ({"ANTHROPIC_DEFAULT_OPUS_MODEL": SONNET}, "must be"),
    ],
)
def test_the_lockdown_rule_refuses_anything_else(extra: dict[str, str], match: str) -> None:
    problems = _problems(PINS | extra)
    assert problems
    assert any(match in p for p in problems), problems


@pytest.mark.parametrize("missing", sorted(PINS))
def test_the_lockdown_rule_requires_every_pin(missing: str) -> None:
    env = {k: v for k, v in PINS.items() if k != missing}
    assert _problems(env) == [f"{missing}: missing model pin"]


# ---------------------------------------------------------------- effort values


@pytest.mark.parametrize("bad", ["low", "max", "xhigh", "none", "", "HIGH"])
def test_effort_outside_medium_and_high_is_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="effort"):
        _load_toml(tmp_path, f'[routing.pins.editor]\ntier = "sonnet"\neffort = "{bad}"\n')
    with pytest.raises(ConfigError, match="effort_policy"):
        load_config(environ={"MPILOT_ROUTING__EFFORT_POLICY": bad})


def test_tier_outside_opus_and_sonnet_is_rejected_in_a_pin(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="tier"):
        _load_toml(tmp_path, '[routing.pins.editor]\ntier = "haiku"\n')


# ---------------------------------------------------------------- pins: the exhaustive matrix

ALL_ROLES = list(Role)
HIGH_ONLY = {Role.PLANNER, Role.CRITIC, Role.DIAGNOSER, Role.ANALYZER}
B_ROLES = {Role.B2_SINGLE_SHOT, Role.B3_GENERIC}


def _oracle(
    role: Role,
    tier: Tier | None,
    effort: Effort | None,
    policy: RoutingPolicy,
    effort_policy: EffortPolicy,
) -> bool:
    """Written from SPEC §8.2/§8.4/B8, independently of the implementation."""
    if policy is not RoutingPolicy.ROUTED:
        return False  # pins only make sense under the routed policy
    if tier is None and effort is None:
        return False
    if role in B_ROLES:
        return False
    if role in {Role.PLANNER, Role.CRITIC} and tier is Tier.SONNET:
        return False
    return effort is not Effort.MEDIUM  # medium is never pinned (owner decision 2026-11-20)


def _pin_config(
    role: Role,
    tier: Tier | None,
    effort: Effort | None,
    policy: RoutingPolicy,
    effort_policy: EffortPolicy,
) -> Config:
    pin: dict[str, Any] = {}
    if tier is not None:
        pin["tier"] = tier.value
    if effort is not None:
        pin["effort"] = effort.value
    return Config.model_validate(
        {
            "routing": {
                "policy": policy.value,
                "effort_policy": effort_policy.value,
                "pins": {role.value: pin},
            }
        }
    )


def test_every_role_tier_effort_policy_combination_matches_the_oracle() -> None:
    combos = itertools.product(
        ALL_ROLES,
        [None, Tier.OPUS, Tier.SONNET],
        [None, Effort.HIGH, Effort.MEDIUM],
        list(RoutingPolicy),
        list(EffortPolicy),
    )
    checked = 0
    for role, tier, effort, policy, effort_policy in combos:
        expected = _oracle(role, tier, effort, policy, effort_policy)
        try:
            _pin_config(role, tier, effort, policy, effort_policy)
            accepted = True
        except ValueError:
            accepted = False
        assert accepted == expected, (role, tier, effort, policy, effort_policy)
        checked += 1
    assert checked == len(ALL_ROLES) * 3 * 3 * 3 * 2


def test_no_accepted_config_ever_pins_medium() -> None:
    """The invariant itself, checked over everything the loader accepts."""
    for role, tier, policy, effort_policy in itertools.product(
        ALL_ROLES, [None, Tier.OPUS, Tier.SONNET], list(RoutingPolicy), list(EffortPolicy)
    ):
        with pytest.raises(ValueError, match="cannot be pinned"):
            _pin_config(role, tier, Effort.MEDIUM, policy, effort_policy)


@pytest.mark.parametrize(
    "role", ["planner", "critic", "diagnoser", "analyzer", "editor", "explorer", "pr_writer"]
)
def test_medium_effort_cannot_be_pinned_for_any_role(tmp_path: Path, role: str) -> None:
    for tier_line in ("", 'tier = "sonnet"\n', 'tier = "opus"\n'):
        with pytest.raises(ConfigError, match="medium effort cannot be pinned"):
            _load_toml(tmp_path, f'[routing.pins.{role}]\n{tier_line}effort = "medium"\n')


def test_high_effort_and_tier_pins_are_still_accepted(tmp_path: Path) -> None:
    config = _load_toml(
        tmp_path,
        """
[routing.pins.editor]
tier = "sonnet"
[routing.pins.explorer]
effort = "high"
[routing.pins.pr_writer]
tier = "opus"
effort = "high"
""",
    )
    assert set(config.routing.pins) == {Role.EDITOR, Role.EXPLORER, Role.PR_WRITER}


def test_planner_and_critic_cannot_be_routed_down(tmp_path: Path) -> None:
    for role in ("planner", "critic"):
        with pytest.raises(ConfigError, match="never routed down"):
            _load_toml(tmp_path, f'[routing.pins.{role}]\ntier = "sonnet"\n')


@pytest.mark.parametrize("role", ["b2_single_shot", "b3_generic"])
def test_baselines_b2_and_b3_cannot_be_pinned(tmp_path: Path, role: str) -> None:
    with pytest.raises(ConfigError, match="B2 and B3"):
        _load_toml(tmp_path, f'[routing.pins.{role}]\ntier = "opus"\neffort = "high"\n')


def test_unknown_role_and_empty_pin_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _load_toml(tmp_path, '[routing.pins.janitor]\neffort = "high"\n')
    with pytest.raises(ConfigError, match="tier or an effort"):
        _load_toml(tmp_path, "[routing.pins.editor]\n")


def test_pins_from_the_environment_as_json() -> None:
    config = load_config(
        environ={"MPILOT_ROUTING__PINS": '{"explorer": {"effort": "high"}}'},
    )
    assert config.routing.pins[Role.EXPLORER].effort is Effort.HIGH
    with pytest.raises(ConfigError, match="cannot be pinned"):
        load_config(environ={"MPILOT_ROUTING__PINS": '{"explorer": {"effort": "medium"}}'})
    with pytest.raises(ConfigError, match="invalid JSON object"):
        load_config(environ={"MPILOT_ROUTING__PINS": "{oops"})


# ---------------------------------------------------------------- all_opus normalizes to all_high


def test_all_opus_normalizes_effort_policy_and_records_it(tmp_path: Path) -> None:
    config = _load_toml(tmp_path, '[routing]\npolicy = "all_opus"\n')
    assert config.routing.policy is RoutingPolicy.ALL_OPUS
    assert config.routing.effort_policy is EffortPolicy.ALL_HIGH
    assert config.routing.effort_policy_normalized_from is EffortPolicy.ROUTED


def test_all_opus_from_env_and_cli_normalizes_too() -> None:
    for config in (
        load_config(environ={"MPILOT_ROUTING__POLICY": "all_opus"}),
        load_config(environ={}, cli_overrides={"routing.policy": "all_opus"}),
    ):
        assert config.routing.effort_policy is EffortPolicy.ALL_HIGH
        assert config.routing.effort_policy_normalized_from is EffortPolicy.ROUTED


def test_all_opus_with_all_high_already_set_records_nothing() -> None:
    config = load_config(
        environ={"MPILOT_ROUTING__POLICY": "all_opus", "MPILOT_ROUTING__EFFORT_POLICY": "all_high"}
    )
    assert config.routing.effort_policy is EffortPolicy.ALL_HIGH
    assert config.routing.effort_policy_normalized_from is None


def test_other_policies_are_not_normalized() -> None:
    for policy in ("routed", "all_sonnet"):
        config = load_config(environ={"MPILOT_ROUTING__POLICY": policy})
        assert config.routing.effort_policy is EffortPolicy.ROUTED
        assert config.routing.effort_policy_normalized_from is None


def test_the_normalization_record_cannot_be_forged(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="only set by the all_opus rule"):
        _load_toml(tmp_path, '[routing]\neffort_policy_normalized_from = "routed"\n')


def test_a_normalized_config_round_trips() -> None:
    config = load_config(environ={"MPILOT_ROUTING__POLICY": "all_opus"})
    assert Config.model_validate(config.model_dump(mode="json")) == config
    assert Config.model_validate_json(config.model_dump_json()) == config


def test_pins_are_rejected_under_all_opus_and_medium_under_all_high(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"pins need routing\.policy = routed"):
        _load_toml(
            tmp_path, '[routing]\npolicy = "all_opus"\n[routing.pins.explorer]\neffort = "high"\n'
        )
    with pytest.raises(ConfigError, match="cannot be pinned"):
        _load_toml(
            tmp_path,
            '[routing]\neffort_policy = "all_high"\n[routing.pins.explorer]\neffort = "medium"\n',
        )


def test_normalization_does_not_change_the_hash_between_spellings(tmp_path: Path) -> None:
    a = load_config(environ={"MPILOT_ROUTING__POLICY": "all_opus"})
    b = load_config(
        environ={"MPILOT_ROUTING__POLICY": "all_opus", "MPILOT_ROUTING__EFFORT_POLICY": "all_high"}
    )
    assert a != b  # the record differs
    assert config_hash(a, tmp_path) == config_hash(b, tmp_path)  # the behaviour is the same
    assert config_hash(a, tmp_path) != config_hash(load_config(environ={}), tmp_path)


def test_the_example_file_documents_the_new_settings() -> None:
    text = (Path(__file__).resolve().parents[2] / "mpilot.toml.example").read_text()
    assert "routing.pins.editor" in text
    assert "no ANTHROPIC_* setting" in text
