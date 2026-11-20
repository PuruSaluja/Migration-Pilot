"""`mpilot` command line (SPEC §13.1).

Every command is a stub in Phase 1: it logs a structured "not implemented" record and exits 1.
The real behaviour arrives in later phases (PROGRESS.md task list). Defaults for the budget
flags come from the config loader (P1-05), so those flags default to `None` here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from mpilot import __version__
from mpilot.config import Config, load_config
from mpilot.core.errors import ConfigError
from mpilot.core.github_token import load_github_token
from mpilot.core.log import configure_logging
from mpilot.core.types import ExitCode

logger = logging.getLogger("mpilot.cli")

app = typer.Typer(
    name="mpilot",
    help="MigrationPilot: upgrade a dependency across a repository, judged by its own tests.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
bench_app = typer.Typer(
    help="Run and report the benchmark suite.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(bench_app, name="bench")

RunId = Annotated[str, typer.Argument(help="Run id.")]


def _not_implemented(command: str) -> None:
    """Phase 1 stub body: record the call and exit 1 (error)."""
    logger.error("command not implemented yet", extra={"command": command})
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mpilot {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    log_level: Annotated[
        str,
        typer.Option("--log-level", envvar="MPILOT_LOG_LEVEL", help="Log level (JSON lines)."),
    ] = "INFO",
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Config file (default: $MPILOT_CONFIG, else ~/.config/mpilot/mpilot.toml).",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """MigrationPilot command line."""
    del version  # handled eagerly by the callback
    # Startup order matters. A10: the GitHub token is read once and scrubbed from the
    # environment before anything else runs, even when the config is invalid. Every other
    # entrypoint (the P8 API server, bench workers) must do the same, first.
    config: Config | None = None
    problems: list[ConfigError] = []
    try:
        config = load_config(config_file=config_file)
    except ConfigError as exc:
        problems.append(exc)
    try:
        load_github_token(token_file=config.github.token_file if config else None)
    except ConfigError as exc:
        problems.append(exc)
    configure_logging(log_level.upper())
    if problems:
        for problem in problems:
            logger.error("invalid configuration", extra={"error": str(problem)})
        raise typer.Exit(code=ExitCode.INPUT_ERROR) from problems[0]
    ctx.obj = config


@app.command()
def run(
    repo: Annotated[str, typer.Option("--repo", help="GitHub URL or local path.")],
    goal: Annotated[
        str, typer.Option("--goal", help='Migration goal, e.g. "upgrade react to 18".')
    ],
    package: Annotated[str | None, typer.Option("--package", help="Package to migrate.")] = None,
    from_version: Annotated[str | None, typer.Option("--from", help="Current version.")] = None,
    to: Annotated[str | None, typer.Option("--to", help="Target version.")] = None,
    replace: Annotated[str | None, typer.Option("--replace", help="Replace X=Y.")] = None,
    max_turns: Annotated[
        int | None, typer.Option("--max-turns", help="Model-turn cap (config default 300).")
    ] = None,
    max_tokens: Annotated[
        int | None, typer.Option("--max-tokens", help="Token cap (config default 8000000).")
    ] = None,
    max_minutes: Annotated[
        int | None, typer.Option("--max-minutes", help="Wall-clock cap (config default 120).")
    ] = None,
    approve_plan: Annotated[
        bool, typer.Option("--approve-plan", help="Wait for plan approval.")
    ] = False,
    no_pr: Annotated[bool, typer.Option("--no-pr", help="Stop before opening a PR.")] = False,
    pr_on_failure: Annotated[
        bool, typer.Option("--pr-on-failure", help="Open a needs-human PR on escalation.")
    ] = False,
    test_cmd: Annotated[
        str | None, typer.Option("--test-cmd", help="Override the detected test command.")
    ] = None,
) -> None:
    """Migrate a dependency in a repository and open a draft PR."""
    del repo, goal, package, from_version, to, replace, max_turns, max_tokens, max_minutes
    del approve_plan, no_pr, pr_on_failure, test_cmd
    _not_implemented("run")


@app.command()
def status(run_id: RunId) -> None:
    """Show the state of a run."""
    del run_id
    _not_implemented("status")


@app.command()
def logs(
    run_id: RunId,
    follow: Annotated[bool, typer.Option("--follow", help="Stream new events.")] = False,
) -> None:
    """Show a run's events."""
    del run_id, follow
    _not_implemented("logs")


@app.command()
def resume(run_id: RunId) -> None:
    """Continue a paused or interrupted run."""
    del run_id
    _not_implemented("resume")


@app.command()
def cancel(run_id: RunId) -> None:
    """Cancel a run."""
    del run_id
    _not_implemented("cancel")


@app.command()
def approve(
    run_id: RunId,
    reject: Annotated[bool, typer.Option("--reject", help="Reject the plan instead.")] = False,
    reason: Annotated[str | None, typer.Option("--reason", help="Reason for rejecting.")] = None,
) -> None:
    """Approve (or reject) the plan of a run waiting in AWAITING_APPROVAL."""
    del run_id, reject, reason
    _not_implemented("approve")


@app.command()
def detect(
    repo: Annotated[Path, typer.Option("--repo", help="Path to the repository.")],
) -> None:
    """Detect the repository's environment and print the EnvProfile as JSON."""
    del repo
    _not_implemented("detect")


@app.command()
def selftest() -> None:
    """Check runtime lockdown, sandbox isolation and egress denial."""
    _not_implemented("selftest")


@bench_app.command("run")
def bench_run(
    suite: Annotated[Path, typer.Argument(help="Suite file, e.g. suite.yaml.")],
    systems: Annotated[
        str, typer.Option("--systems", help="Comma-separated systems.")
    ] = "full,b0,b1,b2,b3",
    parallel: Annotated[int, typer.Option("--parallel", help="Parallel sandboxes.")] = 1,
    seeds: Annotated[int, typer.Option("--seeds", help="Repeats per task.")] = 1,
    batch_size: Annotated[int, typer.Option("--batch-size", help="Tasks per batch.")] = 3,
) -> None:
    """Run a benchmark suite in resumable batches."""
    del suite, systems, parallel, seeds, batch_size
    _not_implemented("bench run")


@bench_app.command("resume")
def bench_resume(
    bench_id: Annotated[str, typer.Argument(help="Benchmark id.")],
) -> None:
    """Continue a paused benchmark."""
    del bench_id
    _not_implemented("bench resume")


@bench_app.command("report")
def bench_report(
    directory: Annotated[Path, typer.Argument(help="Results directory.")],
    out: Annotated[Path, typer.Option("--out", help="Report file to write.")],
) -> None:
    """Build a report from benchmark results."""
    del directory, out
    _not_implemented("bench report")
