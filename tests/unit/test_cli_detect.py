"""P2-12b (SPEC FR-3, §13.1): `mpilot detect --repo <path>` prints the EnvProfile as JSON.

No Docker: the image digest comes from a seam (`mpilot.cli._image_ref`) that these tests replace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mpilot import cli
from mpilot.cli import app
from mpilot.core.errors import InputError
from mpilot.detect.env import ImageRef
from mpilot.models.domain import EnvProfile
from mpilot.sandbox.images import ImageError

runner = CliRunner()
DIGEST = "cd" * 32


def fake_ref(tag: str) -> str:
    return f"{tag}@sha256:{DIGEST}"


@pytest.fixture(autouse=True)
def no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_image_ref", lambda: fake_ref)


def node_repo(root: Path, **extra: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "package.json": json.dumps({"name": "x", "scripts": {"test": "jest"}}),
        "package-lock.json": "{}",
        **extra,
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def profile_of(output: str) -> EnvProfile:
    return EnvProfile.model_validate_json(output)


def test_it_prints_valid_json_that_is_an_env_profile(tmp_path: Path) -> None:
    result = runner.invoke(app, ["detect", "--repo", str(node_repo(tmp_path / "r"))])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    profile = profile_of(result.stdout)
    assert profile.test_cmd == "npm test" == data["test_cmd"]
    assert profile.image_digest == f"mpilot/node:22@sha256:{DIGEST}"
    assert data["detection_sources"]["test_cmd"] == "package.json#scripts.test"


def test_stdout_is_only_the_json(tmp_path: Path) -> None:
    workflow = "on: push\njobs:\n  t:\n    steps:\n      - run: npm test -- --ci\n"
    repo = node_repo(tmp_path / "r", **{".github/workflows/ci.yml": workflow})
    result = runner.invoke(app, ["detect", "--repo", str(repo)])
    assert result.exit_code == 0
    json.loads(result.stdout)  # warnings went to the log, not to stdout
    assert any("argument" in line for line in result.stderr.splitlines())


def test_test_cmd_option_wins(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["detect", "--repo", str(node_repo(tmp_path / "r")), "--test-cmd", "make check"]
    )
    profile = profile_of(result.stdout)
    assert profile.test_cmd == "make check"
    assert profile.detection_sources["test_cmd"] == "override"


def test_mpilot_toml_detect_section_is_used_and_the_option_still_wins(tmp_path: Path) -> None:
    cfg = tmp_path / "mpilot.toml"
    cfg.write_text('[detect]\ntest_cmd = "from config"\nbuild_cmd = "make"\n')
    repo = str(node_repo(tmp_path / "r"))
    first = profile_of(runner.invoke(app, ["--config", str(cfg), "detect", "--repo", repo]).stdout)
    assert (first.test_cmd, first.build_cmd) == ("from config", "make")
    assert first.detection_sources["test_cmd"] == "mpilot.toml#detect.test_cmd"
    second = profile_of(
        runner.invoke(
            app, ["--config", str(cfg), "detect", "--repo", repo, "--test-cmd", "from flag"]
        ).stdout
    )
    assert (second.test_cmd, second.build_cmd) == ("from flag", "make")


def test_a_relative_repo_path_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node_repo(tmp_path / "r")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["detect", "--repo", "r"]).exit_code == 0


def test_ecosystem_option_picks_between_two(tmp_path: Path) -> None:
    repo = node_repo(tmp_path / "r", **{"requirements.txt": "pytest\n"})
    both = runner.invoke(app, ["detect", "--repo", str(repo)])
    assert both.exit_code == 2
    assert "ecosystem" in both.stderr
    picked = runner.invoke(app, ["detect", "--repo", str(repo), "--ecosystem", "python"])
    assert picked.exit_code == 0
    assert profile_of(picked.stdout).language.value == "python"


@pytest.mark.parametrize(
    "setup",
    ["missing", "empty", "file", "no-test-command", "bad-package-json"],
)
def test_input_problems_exit_2_with_a_message_and_no_stdout(tmp_path: Path, setup: str) -> None:
    repo = tmp_path / "r"
    if setup == "empty":
        repo.mkdir()
    elif setup == "file":
        repo.write_text("x")
    elif setup == "no-test-command":
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "x"}')
        (repo / "package-lock.json").write_text("{}")
    elif setup == "bad-package-json":
        repo.mkdir()
        (repo / "package.json").write_text("{not json")
    result = runner.invoke(app, ["detect", "--repo", str(repo)])
    assert result.exit_code == 2
    assert result.stdout.strip() == ""
    records = [json.loads(ln) for ln in result.stderr.splitlines() if ln.startswith("{")]
    assert records[-1]["level"] == "ERROR"
    assert records[-1]["error"]


def test_a_missing_image_exits_1_and_says_how_to_build_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(tag: str) -> str:
        raise ImageError(f"{tag}: not built on this host (build it with scripts/build-images.sh)")

    monkeypatch.setattr(cli, "_image_ref", lambda: broken)
    result = runner.invoke(app, ["detect", "--repo", str(node_repo(tmp_path / "r"))])
    assert result.exit_code == 1
    assert "build-images.sh" in result.stderr
    assert result.stdout.strip() == ""


def test_the_image_seam_is_built_lazily_so_no_docker_is_needed_for_input_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode() -> ImageRef:
        raise AssertionError("Docker must not be touched before detection succeeds")

    monkeypatch.setattr(cli, "_image_ref", explode)
    result = runner.invoke(app, ["detect", "--repo", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_nothing_is_written_to_the_repository(tmp_path: Path) -> None:
    repo = node_repo(tmp_path / "r")
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))
    runner.invoke(app, ["detect", "--repo", str(repo)])
    assert sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*")) == before


def test_input_error_is_the_type_the_cli_maps() -> None:
    assert issubclass(InputError, Exception)
