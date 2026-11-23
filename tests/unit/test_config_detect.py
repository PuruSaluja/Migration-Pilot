"""P2-12b (SPEC FR-3, §9.4): the `[detect]` overrides in `mpilot.toml`.

They are the second layer of the precedence (user override > mpilot.toml > CI > heuristics).
A command is typed by the owner, so it is trusted to be a command, but it still has to be one
line of sane text: it is run with `sh -c` in the sandbox, never on the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mpilot.config import Config, config_hash, load_config
from mpilot.core.errors import ConfigError
from mpilot.knowledge.base import DetectOverrides


def test_there_are_no_overrides_by_default() -> None:
    assert Config().detect.to_overrides() == DetectOverrides()


def test_a_toml_file_sets_overrides(tmp_path: Path) -> None:
    path = tmp_path / "mpilot.toml"
    path.write_text(
        '[detect]\ntest_cmd = "make check"\nbuild_cmd = "make"\nruntime_version = "20"\n'
    )
    config = load_config(config_file=path, environ={})
    assert config.detect.to_overrides() == DetectOverrides(
        test_cmd="make check", build_cmd="make", runtime_version="20"
    )


def test_the_environment_sets_an_override() -> None:
    config = load_config(environ={"MPILOT_DETECT__TEST_CMD": "pytest -x"})
    assert config.detect.test_cmd == "pytest -x"


@pytest.mark.parametrize(
    "value",
    ["", "   ", "a\nb", "a\x00b", "a\rb", "x" * 2001, "tab\there\x1b[0m"],
)
def test_a_command_must_be_one_plain_line(value: str) -> None:
    with pytest.raises(ValidationError):
        DetectOverrides(test_cmd=value)
    with pytest.raises(ConfigError):
        load_config(environ={"MPILOT_DETECT__INSTALL_CMD": value})


def test_a_command_is_stripped() -> None:
    assert DetectOverrides(test_cmd="  pytest -q  ").test_cmd == "pytest -q"


@pytest.mark.parametrize("field", ["install_cmd", "build_cmd", "typecheck_cmd", "test_cmd"])
def test_every_command_field_is_checked(field: str) -> None:
    with pytest.raises(ValidationError):
        DetectOverrides(**{field: "a\nb"})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "abc", "20; rm", "v", "3.", "20.x.y", "1" * 30])
def test_a_runtime_version_must_look_like_a_version(value: str) -> None:
    with pytest.raises(ValidationError):
        DetectOverrides(runtime_version=value)


@pytest.mark.parametrize("value", ["20", "v20", "3.12", "3.12.4", "22.23.5"])
def test_a_runtime_version_may_be_major_minor_or_full(value: str) -> None:
    assert DetectOverrides(runtime_version=value).runtime_version == value


def test_unknown_detect_settings_are_refused() -> None:
    with pytest.raises(ConfigError):
        load_config(environ={"MPILOT_DETECT__BASE_IMAGE": "evil"})


def test_the_overrides_are_part_of_the_config_hash() -> None:
    a = config_hash(Config())
    b = config_hash(load_config(environ={"MPILOT_DETECT__TEST_CMD": "make check"}))
    assert a != b
