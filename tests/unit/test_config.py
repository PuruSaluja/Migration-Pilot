"""P1-05: config loader, precedence, §6 defaults, config_hash, no API key (Q9)."""

from __future__ import annotations

import enum
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from mpilot.config import (
    Config,
    _Section,
    config_hash,
    default_config_path,
    load_config,
    prompt_hashes,
)
from mpilot.core.errors import ConfigError
from mpilot.core.types import Effort, EffortPolicy, Role, RoutingPolicy, Tier
from mpilot.model_policy import RolePin

ROOT = Path(__file__).resolve().parents[2]


def _toml(tmp_path: Path, text: str, name: str = "c.toml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# ---------------------------------------------------------------- defaults equal §6 / §8.4

SPEC_DEFAULTS: dict[str, Any] = {
    # NFR-1, NFR-2, NFR-3
    "budget.max_turns": 300,
    "budget.max_tokens": 8_000_000,
    "budget.max_minutes": 120,
    "budget.suite_timeout_minutes": 20,
    "budget.max_attempts_step": 6,
    "budget.max_attempts_run": 40,
    # NFR-8, NFR-9
    "diff.max_files": 60,
    "diff.max_lines": 1500,
    "bench.parallel": 1,
    "bench.seeds": 1,
    "bench.batch_size": 3,  # §13.1
    # §9.3
    "sandbox.cpus": 4,
    "sandbox.memory_gb": 8,
    "sandbox.disk_gb": 20,
    "sandbox.pids": 1024,
    "sandbox.max_command_timeout_s": 900,  # §9.1
    # FR-2, FR-4, §11.2 (f), §7.4
    "intake.clone_depth": 50,
    "intake.max_repo_mb": 500,
    "baseline.runs": 3,
    "baseline.max_flaky_pct": 5.0,
    "integrity.coverage_gate_pp": 2.0,
    "integrity.coverage_report_pp": 5.0,
    "repair.no_progress_attempts": 3,
    "repair.regression_pct": 20.0,
    # §10, §8.3, §8.5, §9.1, §11.4
    "context.repo_map_tokens_planner": 4000,
    "context.repo_map_tokens_editor": 1500,
    "context.notes_tokens": 2000,
    "context.diagnoser_input_tokens": 8000,
    "context.explorer_answer_tokens": 1500,
    "context.log_max_lines": 60,
    "tools.editor_max_tool_calls": 15,
    "tools.explorer_max_tool_calls": 20,
    "tools.changelog_top_k_max": 8,
    "supply_chain.min_release_age_days": 7,
    # B7, §8.4
    "models.opus": "claude-opus-5-5",
    "models.sonnet": "claude-sonnet-5-5",
    "routing.policy": RoutingPolicy.ROUTED,
    "routing.effort_policy": EffortPolicy.ROUTED,
    "runtime.kind": "agent_sdk",
}


#: Structured settings validated by the NFR-12 tests (test_config_nfr12.py).
NON_NUMERIC_FIELDS = {
    "sandbox.egress_allowlist",  # a host list, tested in test_config_egress.py
    "routing.pins",
    "routing.effort_policy_normalized_from",
}


def _get(config: Config, dotted: str) -> Any:
    section, field = dotted.split(".")
    return getattr(getattr(config, section), field)


@pytest.mark.parametrize(("key", "expected"), SPEC_DEFAULTS.items())
def test_defaults_equal_the_spec(key: str, expected: Any) -> None:
    assert _get(load_config(environ={}), key) == expected


def test_every_numeric_default_is_covered_by_the_spec_table() -> None:
    """A new numeric setting must be added to SPEC_DEFAULTS with its spec source."""
    config = load_config(environ={})
    covered = set(SPEC_DEFAULTS)
    for section_name in Config.model_fields:
        section = getattr(config, section_name)
        if not isinstance(section, _Section) or section_name in {"github", "paths", "detect"}:
            continue
        for field in type(section).model_fields:
            if f"{section_name}.{field}" in NON_NUMERIC_FIELDS:
                continue
            assert f"{section_name}.{field}" in covered, f"{section_name}.{field}"


def test_spec_section_6_numbers_appear_in_the_spec_text() -> None:
    spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    row = next(line for line in spec.splitlines() if line.startswith("| NFR-1 Budget"))
    assert "300 turns, 8M tokens" in row
    assert "| 120 min; 20 min |" in next(
        line for line in spec.splitlines() if line.startswith("| NFR-2 Time")
    )
    assert "| 6 / 40 |" in next(
        line for line in spec.splitlines() if line.startswith("| NFR-3 Iterations")
    )
    assert "60 files / 1,500 lines" in next(
        line for line in spec.splitlines() if line.startswith("| NFR-8 Diff limits")
    )


def test_prices_are_empty_by_default() -> None:
    assert load_config(environ={}).prices == {}


def test_default_runtime_is_the_agent_sdk_and_no_api_key_exists() -> None:
    assert load_config(environ={}).runtime.kind == "agent_sdk"
    # no field anywhere in the config can hold an API key
    names: list[str] = []

    def walk(model: type[BaseModel], prefix: str) -> None:
        for name, info in model.model_fields.items():
            names.append(f"{prefix}{name}")
            inner = info.annotation
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                walk(inner, f"{prefix}{name}.")

    walk(Config, "")
    assert not [n for n in names if "api" in n.lower() or "key" in n.lower()]


def test_api_key_is_neither_read_nor_accepted(tmp_path: Path) -> None:
    plain = load_config(environ={})
    with_key = load_config(
        environ={"ANTHROPIC_API_KEY": "sk-ant-nope", "ANTHROPIC_AUTH_TOKEN": "x"}
    )
    assert with_key == plain
    assert config_hash(with_key) == config_hash(plain)
    with pytest.raises(ConfigError, match="api_key"):
        load_config(config_file=_toml(tmp_path, '[runtime]\napi_key = "sk-ant-x"\n'), environ={})
    with pytest.raises(ConfigError, match="kind"):
        load_config(config_file=_toml(tmp_path, '[runtime]\nkind = "messages"\n'), environ={})
    with pytest.raises(ConfigError, match="unknown field"):
        load_config(environ={"MPILOT_RUNTIME__API_KEY": "sk-ant-x"})


# ---------------------------------------------------------------- precedence: file < env < cli


def test_file_overrides_defaults(tmp_path: Path) -> None:
    config = load_config(
        config_file=_toml(tmp_path, "[budget]\nmax_turns = 100\n[sandbox]\nmemory_gb = 5\n"),
        environ={},
    )
    assert config.budget.max_turns == 100
    assert config.sandbox.memory_gb == 5
    assert config.budget.max_tokens == 8_000_000  # untouched fields keep their defaults


def test_env_overrides_file(tmp_path: Path) -> None:
    config = load_config(
        config_file=_toml(tmp_path, "[budget]\nmax_turns = 100\nmax_tokens = 1000\n"),
        environ={"MPILOT_BUDGET__MAX_TURNS": "50"},
    )
    assert config.budget.max_turns == 50
    assert config.budget.max_tokens == 1000


def test_cli_overrides_env_and_file(tmp_path: Path) -> None:
    config = load_config(
        config_file=_toml(tmp_path, "[budget]\nmax_turns = 100\nmax_minutes = 10\n"),
        environ={"MPILOT_BUDGET__MAX_TURNS": "50", "MPILOT_BUDGET__MAX_MINUTES": "20"},
        cli_overrides={"budget.max_turns": 7},
    )
    assert config.budget.max_turns == 7  # cli
    assert config.budget.max_minutes == 20  # env beats file


def test_none_cli_values_fall_through() -> None:
    config = load_config(
        environ={"MPILOT_BUDGET__MAX_TURNS": "50"},
        cli_overrides={"budget.max_turns": None, "budget.max_tokens": None},
    )
    assert config.budget.max_turns == 50
    assert config.budget.max_tokens == 8_000_000


def test_the_same_value_from_any_source_gives_the_same_config_and_hash(tmp_path: Path) -> None:
    via_file = load_config(config_file=_toml(tmp_path, "[budget]\nmax_turns = 9\n"), environ={})
    via_env = load_config(environ={"MPILOT_BUDGET__MAX_TURNS": "9"})
    via_cli = load_config(environ={}, cli_overrides={"budget.max_turns": 9})
    assert via_file == via_env == via_cli
    assert config_hash(via_file) == config_hash(via_env) == config_hash(via_cli)


def test_sections_merge_per_field_across_sources(tmp_path: Path) -> None:
    config = load_config(
        config_file=_toml(tmp_path, "[routing]\npolicy = 'all_opus'\n"),
        environ={"MPILOT_ROUTING__EFFORT_POLICY": "all_high"},
    )
    assert config.routing.policy is RoutingPolicy.ALL_OPUS
    assert config.routing.effort_policy is EffortPolicy.ALL_HIGH


# ---------------------------------------------------------------- file lookup


def test_explicit_path_beats_env_path_beats_default(tmp_path: Path) -> None:
    explicit = _toml(tmp_path, "[budget]\nmax_turns = 1\n", "explicit.toml")
    from_env = _toml(tmp_path, "[budget]\nmax_turns = 2\n", "env.toml")
    default = tmp_path / "xdg" / "mpilot" / "mpilot.toml"
    default.parent.mkdir(parents=True)
    default.write_text("[budget]\nmax_turns = 3\n")
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert default_config_path(env) == default
    assert load_config(environ=env).budget.max_turns == 3
    assert load_config(environ=env | {"MPILOT_CONFIG": str(from_env)}).budget.max_turns == 2
    both = env | {"MPILOT_CONFIG": str(from_env)}
    assert load_config(config_file=explicit, environ=both).budget.max_turns == 1


def test_current_directory_is_never_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untrusted repo's mpilot.toml must not configure the tool."""
    (tmp_path / "mpilot.toml").write_text("[budget]\nmax_turns = 1\n")
    monkeypatch.chdir(tmp_path)
    assert load_config(environ={"XDG_CONFIG_HOME": str(tmp_path / "none")}).budget.max_turns == 300


def test_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(config_file=tmp_path / "nope.toml", environ={})
    with pytest.raises(ConfigError, match="not found"):
        load_config(environ={"MPILOT_CONFIG": str(tmp_path / "nope.toml")})


def test_malformed_toml_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(config_file=_toml(tmp_path, "[budget\n"), environ={})


# ---------------------------------------------------------------- strictness


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[budgett]\nmax_turns = 1\n", "budgett"),
        ("[budget]\nmax_turn = 1\n", "max_turn"),
        ("[budget]\nmax_turns = 0\n", "max_turns"),
        ("[budget]\nmax_turns = 'many'\n", "max_turns"),
        ("[routing]\npolicy = 'cheapest'\n", "policy"),
        ("[routing]\neffort_policy = 'medium'\n", "effort_policy"),
        ("[baseline]\nmax_flaky_pct = 101\n", "max_flaky_pct"),
    ],
)
def test_invalid_files_are_rejected(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(config_file=_toml(tmp_path, text), environ={})


@pytest.mark.parametrize(
    ("name", "match"),
    [
        ("MPILOT_NOPE__X", "unknown config section"),
        ("MPILOT_BUDGET__NOPE", "unknown field"),
        ("MPILOT_BUDGET__MAX_TURNS__EXTRA", "expected MPILOT_<SECTION>__<FIELD>"),
        ("MPILOT___MAX_TURNS", "expected MPILOT_<SECTION>__<FIELD>"),
        ("MPILOT_PRICES__X", "unknown config section"),  # prices is a table, not a section
    ],
)
def test_bad_env_names_are_rejected(name: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(environ={name: "1"})


def test_non_config_mpilot_variables_are_ignored() -> None:
    env = {
        "MPILOT_LOG_LEVEL": "DEBUG",
        "MPILOT_GITHUB_TOKEN": "github_pat_x",
        "MPILOT_CONFIG": "",
        "MPILOT_SKIP_DOCKER": "1",
        "MPILOT_NETWORK_TESTS": "1",
        "UNRELATED__THING": "1",
    }
    assert load_config(environ=env) == load_config(environ={})


def test_bad_values_from_env_are_rejected() -> None:
    with pytest.raises(ConfigError, match="max_turns"):
        load_config(environ={"MPILOT_BUDGET__MAX_TURNS": "lots"})
    with pytest.raises(ConfigError, match="effort_policy"):
        load_config(environ={"MPILOT_ROUTING__EFFORT_POLICY": "low"})


def test_error_messages_never_echo_the_token(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_config(environ={"MPILOT_GITHUB_TOKEN": "github_pat_SECRET", "MPILOT_BAD__X": "1"})
    assert "SECRET" not in str(info.value)


# ---------------------------------------------------------------- lists and other types


def test_github_repos_from_env_comma_or_json(tmp_path: Path) -> None:
    assert load_config(environ={"MPILOT_GITHUB__REPOS": "a/b, c/d"}).github.repos == ["a/b", "c/d"]
    assert load_config(environ={"MPILOT_GITHUB__REPOS": '["a/b"]'}).github.repos == ["a/b"]
    assert load_config(environ={"MPILOT_GITHUB__REPOS": ""}).github.repos == []
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(environ={"MPILOT_GITHUB__REPOS": "[oops"})


def test_token_file_path_comes_from_config(tmp_path: Path) -> None:
    config = load_config(environ={"MPILOT_GITHUB__TOKEN_FILE": str(tmp_path / "t")})
    assert config.github.token_file == tmp_path / "t"


def test_tier_to_model_id_and_to_budgets() -> None:
    config = load_config(environ={"MPILOT_MODELS__SONNET": "claude-sonnet-5-5"})
    assert (config.models.opus, config.models.sonnet) == ("claude-opus-5-5", "claude-sonnet-5-5")
    budgets = config.to_budgets()
    assert (budgets.max_turns, budgets.max_tokens, budgets.minutes_cap) == (300, 8_000_000, 120)
    assert (budgets.max_attempts_run, budgets.max_attempts_step) == (40, 6)


def test_prices_need_source_and_date_and_a_configured_model(tmp_path: Path) -> None:
    good = """
[prices."claude-opus-5-5"]
input_per_mtok = 1.0
output_per_mtok = 2.0
cache_read_per_mtok = 0.1
cache_write_per_mtok = 1.25
source_url = "https://example.test/pricing"
as_of = 2026-11-20
"""
    config = load_config(config_file=_toml(tmp_path, good), environ={})
    assert config.prices["claude-opus-5-5"].as_of == date(2026, 11, 20)
    with pytest.raises(ConfigError, match="as_of"):
        load_config(
            config_file=_toml(tmp_path, good.replace("as_of = 2026-11-20\n", "")), environ={}
        )
    with pytest.raises(ConfigError, match="source_url"):
        load_config(
            config_file=_toml(
                tmp_path, good.replace('source_url = "https://example.test/pricing"\n', "")
            ),
            environ={},
        )
    with pytest.raises(ConfigError, match="not configured"):
        load_config(
            config_file=_toml(tmp_path, good.replace("claude-opus-5-5", "claude-haiku-4-5")),
            environ={},
        )


def test_config_is_immutable() -> None:
    config = load_config(environ={})
    with pytest.raises(Exception, match="frozen"):
        config.budget.max_turns = 1  # type: ignore[misc]


# ---------------------------------------------------------------- the example file


def test_the_example_file_equals_the_defaults() -> None:
    assert load_config(config_file=ROOT / "mpilot.toml.example", environ={}) == Config()


# ---------------------------------------------------------------- config_hash


def _leaf_fields(config: Config) -> list[tuple[str, str]]:
    return [
        (section, field)
        for section in Config.model_fields
        if isinstance(getattr(config, section), _Section)
        for field in type(getattr(config, section)).model_fields
    ]


def _perturbed(value: Any, annotation: Any) -> Any:
    if isinstance(value, enum.Enum):
        members = list(type(value))
        return members[(members.index(value) + 1) % len(members)]
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, list):
        return [*value, "a/b"]
    if value is None:  # an optional text setting (the `[detect]` overrides)
        return "x"
    raise AssertionError(f"no perturbation for {annotation}")


_HOST_LOCAL = {
    ("github", "token_file"),
    ("paths", "runs_dir"),
    ("paths", "db_path"),
    ("routing", "effort_policy_normalized_from"),  # a record of a normalization, not a setting
}


def test_config_hash_changes_when_any_behavioural_setting_changes(tmp_path: Path) -> None:
    base = load_config(environ={})
    base_hash = config_hash(base, tmp_path)
    for section, field in _leaf_fields(base):
        if (section, field) in _HOST_LOCAL:
            continue
        section_obj = getattr(base, section)
        if (section, field) == ("runtime", "kind"):  # single-member Literal
            continue
        if (section, field) == ("routing", "pins"):
            new_value = {Role.EDITOR: RolePin(tier=Tier.SONNET, effort=Effort.MEDIUM)}
        else:
            new_value = _perturbed(getattr(section_obj, field), None)
        changed = base.model_copy(
            update={section: section_obj.model_copy(update={field: new_value})}
        )
        assert config_hash(changed, tmp_path) != base_hash, f"{section}.{field}"


def test_config_hash_ignores_host_local_paths(tmp_path: Path) -> None:
    base = load_config(environ={})
    other = load_config(
        environ={
            "MPILOT_GITHUB__TOKEN_FILE": "/elsewhere/token",
            "MPILOT_PATHS__RUNS_DIR": "/mnt/other/runs",
            "MPILOT_PATHS__DB_PATH": "/mnt/other/db.sqlite",
        }
    )
    assert other != base
    assert config_hash(other, tmp_path) == config_hash(base, tmp_path)


def test_config_hash_changes_with_prices(tmp_path: Path) -> None:
    base = config_hash(load_config(environ={}), tmp_path)
    priced = Config.model_validate(
        {
            "prices": {
                "claude-opus-5-5": {
                    "input_per_mtok": 1,
                    "output_per_mtok": 1,
                    "cache_read_per_mtok": 1,
                    "cache_write_per_mtok": 1,
                    "source_url": "https://example.test",
                    "as_of": "2026-11-20",
                }
            }
        }
    )
    assert config_hash(priced, tmp_path) != base


def test_config_hash_tracks_prompt_files(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    config = load_config(environ={})
    empty = config_hash(config, prompts)  # directory does not exist yet
    prompts.mkdir()
    assert config_hash(config, prompts) == empty
    (prompts / "editor.md").write_text("You are the Editor.\n")
    one = config_hash(config, prompts)
    assert one != empty
    (prompts / "editor.md").write_text("You are the Editor!\n")
    two = config_hash(config, prompts)
    assert two != one
    (prompts / "critic.md").write_text("Critic.\n")
    assert config_hash(config, prompts) != two
    (prompts / "notes.txt").write_text("not a prompt")
    assert prompt_hashes(prompts).keys() == {"critic.md", "editor.md"}
    (prompts / "editor.md").write_text("You are the Editor!\n")
    assert config_hash(config, prompts) == config_hash(config, prompts)


def test_config_hash_is_stable_and_order_independent(tmp_path: Path) -> None:
    a = load_config(environ={"MPILOT_BUDGET__MAX_TURNS": "9", "MPILOT_DIFF__MAX_FILES": "5"})
    b = load_config(environ={"MPILOT_DIFF__MAX_FILES": "5", "MPILOT_BUDGET__MAX_TURNS": "9"})
    assert config_hash(a, tmp_path) == config_hash(b, tmp_path)
    assert len(config_hash(a, tmp_path)) == 64


def test_default_prompts_dir_is_inside_the_package() -> None:
    from mpilot.config import DEFAULT_PROMPTS_DIR

    assert DEFAULT_PROMPTS_DIR == ROOT / "src" / "mpilot" / "agents" / "prompts"
