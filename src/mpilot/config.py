"""Configuration: `mpilot.toml` < environment `MPILOT_*` < CLI overrides (SPEC §6, §8.4).

Every default below is a [REC] value from the spec (the section comments name the source).
The config holds no secrets, and no API key exists anywhere in it: v1 runs on the Claude
subscription through the Agent SDK only (NFR-11, Q9). `ANTHROPIC_API_KEY` is never read.

Sources, lowest to highest precedence:
1. a TOML file: `--config`, else env `MPILOT_CONFIG`, else `~/.config/mpilot/mpilot.toml`
   (`$XDG_CONFIG_HOME` honoured). There is deliberately NO lookup in the current directory:
   mpilot is often run next to an untrusted repository, and a config file in it must never
   be picked up by accident.
2. environment variables `MPILOT_<SECTION>__<FIELD>`, e.g. `MPILOT_BUDGET__MAX_TURNS=100`.
   Variables without a double underscore (`MPILOT_LOG_LEVEL`, `MPILOT_GITHUB_TOKEN`,
   `MPILOT_CONFIG`, ...) are not config keys and are ignored here.
3. CLI overrides: a mapping of `"section.field"` to a value; `None` values are skipped, so an
   unset Typer option falls through to the lower sources.

Unknown sections, fields or variables are errors, so a typo never silently falls back to a
default. Model-ID, effort and pinning validation (NFR-12) is P1-06.

`config_hash` (NFR-5) fingerprints every behaviour-relevant setting plus the prompt files.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
import types
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mpilot.core.errors import ConfigError
from mpilot.core.types import EffortPolicy, Role, RoutingPolicy
from mpilot.model_policy import (
    RolePin,
    anthropic_env_problems,
    model_id_problems,
    pin_problems,
)
from mpilot.models.domain import Budgets

ENV_PREFIX = "MPILOT_"
ENV_CONFIG_PATH = "MPILOT_CONFIG"
CONFIG_HASH_VERSION = 1

PositiveInt = Annotated[int, Field(gt=0)]
NonNegFloat = Annotated[float, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
Percent = Annotated[float, Field(ge=0, le=100)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- sections


class BudgetSettings(_Section):
    """NFR-1, NFR-2, NFR-3."""

    max_turns: PositiveInt = 300
    max_tokens: PositiveInt = 8_000_000
    max_minutes: PositiveInt = 120  # wall clock per run, excluding PAUSED_LIMIT time
    suite_timeout_minutes: PositiveInt = 20  # per full-suite execution
    max_attempts_step: PositiveInt = 6
    max_attempts_run: PositiveInt = 40


class DiffSettings(_Section):
    """NFR-8: exceeding either triggers escalation, not a PR (lockfiles excluded)."""

    max_files: PositiveInt = 60
    max_lines: PositiveInt = 1500


class BenchSettings(_Section):
    """NFR-9 and §13.1 (`mpilot bench run`)."""

    parallel: PositiveInt = 1
    seeds: PositiveInt = 1
    batch_size: PositiveInt = 3


class SandboxSettings(_Section):
    """§9.3 limits. `memory_gb` may be lowered locally on small hosts (decisions D7)."""

    cpus: PositiveInt = 4
    memory_gb: PositiveInt = 8
    disk_gb: PositiveInt = 20
    pids: PositiveInt = 1024
    max_command_timeout_s: PositiveInt = 900  # `run_command` timeout_s upper bound (§9.1)


class IntakeSettings(_Section):
    """FR-2."""

    clone_depth: PositiveInt = 50
    max_repo_mb: PositiveInt = 500


class BaselineSettings(_Section):
    """FR-4."""

    runs: PositiveInt = 3
    max_flaky_pct: Percent = 5.0


class IntegritySettings(_Section):
    """§11.2 (f): coverage may drop at most `coverage_gate_pp`; `coverage_report_pp` is reported."""

    coverage_gate_pp: NonNegFloat = 2.0
    coverage_report_pp: NonNegFloat = 5.0


class RepairSettings(_Section):
    """§7.4."""

    no_progress_attempts: PositiveInt = 3  # same failure-signature set for this many attempts
    regression_pct: PositiveFloat = 20.0  # revert-to-best threshold at the test stage


class ContextSettings(_Section):
    """Token limits enforced on text we build, estimated as ceil(chars / 3) (B3, §10)."""

    repo_map_tokens_planner: PositiveInt = 4000  # also the Analyzer
    repo_map_tokens_editor: PositiveInt = 1500
    notes_tokens: PositiveInt = 2000
    diagnoser_input_tokens: PositiveInt = 8000
    explorer_answer_tokens: PositiveInt = 1500
    log_max_lines: PositiveInt = 60


class ToolSettings(_Section):
    """§8.3, §8.5, §9.1 per-call caps."""

    editor_max_tool_calls: PositiveInt = 15
    explorer_max_tool_calls: PositiveInt = 20
    changelog_top_k_max: PositiveInt = 8


class SupplyChainSettings(_Section):
    """§11.4: reject target versions published less than this many days ago."""

    min_release_age_days: PositiveInt = 7


class ModelSettings(_Section):
    """Tier to model ID (B7). Re-verified in P4-01."""

    opus: str = "claude-opus-5-5"
    sonnet: str = "claude-sonnet-5-5"

    @model_validator(mode="after")
    def _nfr_12(self) -> ModelSettings:
        if problems := model_id_problems(self.opus, self.sonnet):
            raise ValueError("; ".join(problems))
        return self


class RoutingSettings(_Section):
    """§8.4 Policies, plus optional per-role pins (validated against NFR-12).

    `policy = all_opus` implies `effort_policy = all_high` (§8.4): it is normalized here and
    the value the user wrote is recorded in `effort_policy_normalized_from`. That field is a
    record, not a setting: it is excluded from `config_hash`.
    """

    policy: RoutingPolicy = RoutingPolicy.ROUTED
    effort_policy: EffortPolicy = EffortPolicy.ROUTED
    pins: dict[Role, RolePin] = Field(default_factory=dict)
    effort_policy_normalized_from: EffortPolicy | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_all_opus(cls, data: Any) -> Any:
        if not isinstance(data, dict) or str(data.get("policy", "routed")) != "all_opus":
            return data
        written = data.get("effort_policy", EffortPolicy.ROUTED.value)
        if str(written) == EffortPolicy.ALL_HIGH.value:
            return data
        return {
            **data,
            "effort_policy": EffortPolicy.ALL_HIGH.value,
            "effort_policy_normalized_from": str(written),
        }

    @model_validator(mode="after")
    def _nfr_12(self) -> RoutingSettings:
        problems = pin_problems(self.pins, self.policy)
        if (
            self.effort_policy_normalized_from is not None
            and self.policy is not RoutingPolicy.ALL_OPUS
        ):
            problems.append("effort_policy_normalized_from is only set by the all_opus rule")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class RuntimeSettings(_Section):
    """The agent runtime. v1 has exactly one: the Agent SDK on the Claude subscription (Q9).

    There is intentionally no API-key field here, and `kind` cannot name `MessagesRuntime`.
    """

    kind: Literal["agent_sdk"] = "agent_sdk"
    #: Extra `ANTHROPIC_*` settings for the SDK subprocess, on top of the model pins that
    #: `Config.sdk_model_env()` always applies. Validated by `anthropic_env_problems`.
    anthropic_env: dict[str, str] = Field(default_factory=dict)


class ModelPrice(_Section):
    """USD per million tokens, copied from the official pricing page (never guessed, B7)."""

    input_per_mtok: NonNegFloat
    output_per_mtok: NonNegFloat
    cache_read_per_mtok: NonNegFloat
    cache_write_per_mtok: NonNegFloat
    source_url: str = Field(min_length=1)
    as_of: date


class GithubSettings(_Section):
    """§11.3 control 1 and §11.6. The token itself is never configured here (A10)."""

    repos: list[str] = Field(default_factory=list)  # "owner/name" the credential provider may touch
    token_file: Path | None = None  # 0600 file outside any repo; env MPILOT_GITHUB_TOKEN wins


class PathSettings(_Section):
    runs_dir: Path = Path("runs")
    db_path: Path = Path("runs/mpilot.db")


# ---------------------------------------------------------------- the config


class Config(_Section):
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    diff: DiffSettings = Field(default_factory=DiffSettings)
    bench: BenchSettings = Field(default_factory=BenchSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    intake: IntakeSettings = Field(default_factory=IntakeSettings)
    baseline: BaselineSettings = Field(default_factory=BaselineSettings)
    integrity: IntegritySettings = Field(default_factory=IntegritySettings)
    repair: RepairSettings = Field(default_factory=RepairSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    supply_chain: SupplyChainSettings = Field(default_factory=SupplyChainSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    #: Keyed by model ID. Empty until prices are copied from the pricing page (B7): the
    #: USD column then reads "n/a".
    prices: dict[str, ModelPrice] = Field(default_factory=dict)
    github: GithubSettings = Field(default_factory=GithubSettings)
    paths: PathSettings = Field(default_factory=PathSettings)

    @model_validator(mode="after")
    def _nfr_12_and_prices(self) -> Config:
        known = {self.models.opus, self.models.sonnet}
        problems = anthropic_env_problems(self.runtime.anthropic_env, known)
        unknown = sorted(set(self.prices) - known)
        if unknown:
            problems.append(f"prices given for models that are not configured: {unknown}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def sdk_model_env(self) -> dict[str, str]:
        """`ANTHROPIC_*` variables for the Agent SDK subprocess (§8.1 item 9).

        Every model setting the SDK exposes is pinned to Sonnet 5.5, including the background
        ("haiku") model, so that Haiku can never run. P4-01 re-verifies the variable names;
        user-supplied `runtime.anthropic_env` entries override these but must pass the same
        rules.
        """
        pins = {
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.models.sonnet,
            "ANTHROPIC_SMALL_FAST_MODEL": self.models.sonnet,
        }
        return pins | self.runtime.anthropic_env

    def to_budgets(self) -> Budgets:
        """The `Run.budgets` record for a run started under this config."""
        return Budgets(
            max_turns=self.budget.max_turns,
            max_tokens=self.budget.max_tokens,
            minutes_cap=self.budget.max_minutes,
            max_attempts_run=self.budget.max_attempts_run,
            max_attempts_step=self.budget.max_attempts_step,
        )


# ---------------------------------------------------------------- loading


def default_config_path(environ: Mapping[str, str]) -> Path:
    base = environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "mpilot" / "mpilot.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc


def _has_origin(annotation: Any, container: type) -> bool:
    origin = get_origin(annotation)
    if origin is container:
        return True
    if origin in (Union, types.UnionType):
        return any(_has_origin(arg, container) for arg in get_args(annotation))
    return False


def _parse_env_value(raw: str, annotation: Any) -> Any:
    """Env values are strings. Lists accept a JSON array or comma-separated items; dicts
    must be a JSON object."""
    if _has_origin(annotation, dict):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON object in environment value: {exc}") from exc
    if _has_origin(annotation, list):
        text = raw.strip()
        if text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"invalid JSON list in environment value: {exc}") from exc
        return [item.strip() for item in text.split(",") if item.strip()]
    return raw


def _env_overrides(environ: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(environ):
        if not name.startswith(ENV_PREFIX) or "__" not in name[len(ENV_PREFIX) :]:
            continue
        parts = name[len(ENV_PREFIX) :].lower().split("__")
        if len(parts) != 2 or not all(parts):
            raise ConfigError(f"{name}: expected MPILOT_<SECTION>__<FIELD>")
        section, field = parts
        section_field = Config.model_fields.get(section)
        section_model = section_field.annotation if section_field else None
        if not (isinstance(section_model, type) and issubclass(section_model, _Section)):
            raise ConfigError(f"{name}: unknown config section {section!r}")
        field_info = section_model.model_fields.get(field)
        if field_info is None:
            raise ConfigError(f"{name}: unknown field {field!r} in section {section!r}")
        result.setdefault(section, {})[field] = _parse_env_value(
            environ[name], field_info.annotation
        )
    return result


def _cli_overrides(overrides: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        section, dot, field = key.partition(".")
        if not dot or not field or "." in field:
            raise ConfigError(f"CLI override {key!r}: expected 'section.field'")
        result.setdefault(section, {})[field] = value
    return result


def _merge(*layers: Mapping[str, Any]) -> dict[str, Any]:
    """Merge section dicts; a later layer overrides an earlier one per field."""
    merged: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
                merged[key] = _merge(merged[key], value)
            elif isinstance(value, Mapping):
                merged[key] = _merge(value)
            else:
                merged[key] = value
    return merged


def load_config(
    *,
    config_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Build the effective config. Raises `ConfigError` on any problem."""
    env = os.environ if environ is None else environ

    explicit = config_file
    if explicit is None and env.get(ENV_CONFIG_PATH):
        explicit = Path(env[ENV_CONFIG_PATH])
    file_layer: dict[str, Any] = {}
    if explicit is not None:
        file_layer = _read_toml(explicit)
    else:
        default = default_config_path(env)
        if default.is_file():
            file_layer = _read_toml(default)

    merged = _merge(file_layer, _env_overrides(env), _cli_overrides(cli_overrides or {}))
    try:
        return Config.model_validate(merged)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"invalid configuration: {problems}") from None


# ---------------------------------------------------------------- config_hash (NFR-5)

DEFAULT_PROMPTS_DIR = Path(__file__).parent / "agents" / "prompts"

#: Host-local settings that say nothing about behaviour: they differ between machines, so
#: including them would make the hash useless for reproducing a run elsewhere.
_HASH_EXCLUDED: dict[str, set[str]] = {
    "github": {"token_file"},
    "paths": {"runs_dir", "db_path"},
    # A record of a normalization, not a setting (the normalized effort_policy is hashed).
    "routing": {"effort_policy_normalized_from"},
}


def prompt_hashes(prompts_dir: Path = DEFAULT_PROMPTS_DIR) -> dict[str, str]:
    """SHA-256 of each `*.md` prompt file in `prompts_dir` (empty if it doesn't exist yet)."""
    if not prompts_dir.is_dir():
        return {}
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(prompts_dir.glob("*.md"))
        if path.is_file()
    }


def config_hash(config: Config, prompts_dir: Path = DEFAULT_PROMPTS_DIR) -> str:
    """Fingerprint of every setting (models and routing included) and every prompt file.

    It changes when a prompt, a model ID, or any behaviour-relevant setting changes, and only
    then: host-local paths are excluded, and nothing from the environment outside the config
    (such as `ANTHROPIC_API_KEY`) can influence it.
    """
    settings = config.model_dump(mode="json")
    for section, fields in _HASH_EXCLUDED.items():
        for field in fields:
            settings[section].pop(field, None)
    payload = {
        "version": CONFIG_HASH_VERSION,
        "settings": settings,
        "prompts": prompt_hashes(prompts_dir),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
