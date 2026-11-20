"""P1-01: `mpilot --help` lists every SPEC §13.1 command, and each is stubbed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from mpilot.cli import app
from mpilot.config import Config

# SPEC §13.1, one entry per command line (bench is a sub-app).
TOP_LEVEL = ["run", "status", "logs", "resume", "cancel", "approve", "detect", "selftest", "bench"]
BENCH = ["run", "resume", "report"]

runner = CliRunner()


def _listed_commands(output: str) -> set[str]:
    """Command names in a Typer help table: first word of each boxed row."""
    names: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip().strip("│| ").strip()
        if stripped:
            names.add(stripped.split()[0])
    return names


def test_help_lists_every_top_level_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert set(TOP_LEVEL) <= _listed_commands(result.output)


def test_bench_help_lists_every_subcommand() -> None:
    result = runner.invoke(app, ["bench", "--help"])
    assert result.exit_code == 0
    assert set(BENCH) <= _listed_commands(result.output)


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("mpilot ")


STUB_INVOCATIONS = [
    (["run", "--repo", "x", "--goal", "y"], "run"),
    (["status", "r1"], "status"),
    (["logs", "r1", "--follow"], "logs"),
    (["resume", "r1"], "resume"),
    (["cancel", "r1"], "cancel"),
    (["approve", "r1", "--reject", "--reason", "no"], "approve"),
    (["detect", "--repo", "."], "detect"),
    (["selftest"], "selftest"),
    (["bench", "run", "suite.yaml", "--systems", "b0,b1"], "bench run"),
    (["bench", "resume", "b1"], "bench resume"),
    (["bench", "report", ".", "--out", "report.md"], "bench report"),
]


@pytest.mark.parametrize(("args", "command"), STUB_INVOCATIONS)
def test_stub_exits_1_with_json_log(args: list[str], command: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    records = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert records, result.output
    assert records[-1]["command"] == command
    assert records[-1]["level"] == "ERROR"


def test_every_stub_invocation_covers_a_listed_command() -> None:
    covered = {args[0] for args, _ in STUB_INVOCATIONS}
    assert covered == set(TOP_LEVEL)


def test_invalid_config_exits_2_before_any_command(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[budget]\nmax_turns = 0\n")
    result = runner.invoke(app, ["--config", str(bad), "selftest"])
    assert result.exit_code == 2
    assert "max_turns" in result.output


def test_missing_config_file_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "nope.toml"), "selftest"])
    assert result.exit_code == 2


def test_config_flag_is_loaded_for_the_command(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text("[budget]\nmax_turns = 5\n")
    seen: list[Config] = []

    @app.command("probe-config")
    def probe(ctx: typer.Context) -> None:
        seen.append(ctx.obj)

    result = runner.invoke(app, ["--config", str(good), "probe-config"])
    assert result.exit_code == 0, result.output
    assert seen[0].budget.max_turns == 5
