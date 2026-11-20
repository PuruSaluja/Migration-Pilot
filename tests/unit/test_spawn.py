"""P1-03 / A10: children see only the env they are given; the token never leaks."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mpilot.cli import app
from mpilot.core import spawn
from mpilot.core.github_token import GITHUB_TOKEN_ENV, load_github_token

TOKEN = "github_pat_TESTONLY_0123456789abcdef"
ENV_BIN = shutil.which("env") or "/usr/bin/env"
SH = shutil.which("sh") or "/bin/sh"


def _child_env(env: dict[str, str]) -> dict[str, str]:
    result = spawn.run([ENV_BIN, "-0"], env=env)
    assert result.returncode == 0, result.stderr
    pairs = [item for item in result.stdout.decode().split("\0") if item]
    return dict(pair.split("=", 1) for pair in pairs)


def test_child_sees_exactly_the_keys_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MPILOT_PARENT_ONLY", "x")
    assert _child_env({"A": "1", "B": "two words"}) == {"A": "1", "B": "two words"}


def test_child_with_empty_env_sees_nothing() -> None:
    assert _child_env({}) == {}


def test_inherit_copies_only_named_set_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MPILOT_X", "1")
    monkeypatch.delenv("MPILOT_UNSET", raising=False)
    assert spawn.inherit("MPILOT_X", "MPILOT_UNSET") == {"MPILOT_X": "1"}


def test_after_cli_startup_the_token_is_gone_and_no_child_sees_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, TOKEN)
    CliRunner().invoke(app, ["selftest"])  # any command runs the startup callback
    assert GITHUB_TOKEN_ENV not in os.environ
    # Even a child handed the orchestrator's whole remaining environment can't see it.
    child = _child_env(spawn.inherit(*os.environ))
    assert GITHUB_TOKEN_ENV not in child
    assert all(TOKEN not in value for value in child.values())


def test_cli_rejects_a_bad_token_file_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token"
    path.write_text(TOKEN)
    path.chmod(0o644)
    monkeypatch.setenv("MPILOT_GITHUB__TOKEN_FILE", str(path))
    result = CliRunner().invoke(app, ["selftest"])
    assert result.exit_code == 2
    assert TOKEN not in result.output


def test_token_variable_can_never_be_inherited_or_passed() -> None:
    with pytest.raises(spawn.SpawnError, match="never be inherited"):
        spawn.inherit(GITHUB_TOKEN_ENV)
    with pytest.raises(spawn.SpawnError, match="never passed"):
        spawn.run([ENV_BIN], env={GITHUB_TOKEN_ENV: "x"})


def test_token_value_in_env_needs_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, TOKEN)
    load_github_token()
    with pytest.raises(spawn.SpawnError, match="pass_github_token"):
        spawn.run([ENV_BIN], env={"ASKPASS_TOKEN": TOKEN})
    with pytest.raises(spawn.SpawnError, match="pass_github_token"):
        spawn.run([ENV_BIN], env={"URL": f"https://x-access-token:{TOKEN}@github.com/o/r"})
    child = spawn.run([ENV_BIN, "-0"], env={"ASKPASS_TOKEN": TOKEN}, pass_github_token=True)
    assert child.returncode == 0


def test_token_in_argv_is_always_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, TOKEN)
    load_github_token()
    for pass_token in (False, True):
        with pytest.raises(spawn.SpawnError, match="argv"):
            spawn.run(
                ["/usr/bin/git", "push", f"https://{TOKEN}@github.com/o/r"],
                env={},
                pass_github_token=pass_token,
            )


@pytest.mark.parametrize("argv", ["echo hi", b"echo hi", [], ["echo", "a\0b"]])
def test_shell_strings_and_bad_argv_are_refused(argv: object) -> None:
    with pytest.raises(spawn.SpawnError):
        spawn.run(argv, env={})  # type: ignore[arg-type]


@pytest.mark.parametrize("env", [{"": "x"}, {"A=B": "x"}, {"A": "x\0y"}])
def test_bad_env_entries_are_refused(env: dict[str, str]) -> None:
    with pytest.raises(spawn.SpawnError, match="invalid env entry"):
        spawn.run([ENV_BIN], env=env)


def test_missing_executable_raises_spawn_error() -> None:
    with pytest.raises(spawn.SpawnError, match="could not start"):
        spawn.run(["/nonexistent/mpilot-no-such-binary"], env={})


def test_stdin_is_devnull_by_default_and_input_is_passed() -> None:
    cat = shutil.which("cat") or "/bin/cat"
    assert spawn.run([cat], env={}, timeout_s=10).stdout == b""
    assert spawn.run([cat], env={}, input_bytes=b"hi").stdout == b"hi"


def test_cwd_and_exit_code(tmp_path: Path) -> None:
    result = spawn.run([SH, "-c", "pwd; exit 3"], env={}, cwd=tmp_path)
    assert result.returncode == 3
    assert result.stdout.decode().strip() == str(tmp_path)
    assert not result.timed_out


def _alive(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return False
    return "\nState:\tZ" not in status


def test_timeout_kills_the_whole_process_group() -> None:
    result = spawn.run(
        [SH, "-c", "sleep 60 & echo $!; wait"],
        env={},
        timeout_s=3,
    )
    assert result.timed_out
    assert result.returncode < 0
    grandchild = int(result.stdout.decode().split()[0])
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)


def test_python_child_does_not_see_parent_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MPILOT_PARENT_SECRET", "s3cret")
    result = spawn.run(
        [sys.executable, "-I", "-c", "import os; print(os.environ.get('MPILOT_PARENT_SECRET'))"],
        env={},
    )
    assert result.stdout.decode().strip() == "None"


@pytest.mark.parametrize("argv0", ["env", "./git", "bin/git", ""])
def test_relative_executables_are_refused(argv0: str) -> None:
    with pytest.raises(spawn.SpawnError, match="absolute path"):
        spawn.run([argv0], env={})


@pytest.mark.parametrize("path", ["", ".", "bin", "/usr/bin::/bin", "/usr/bin:.", ":/usr/bin"])
def test_relative_or_empty_path_entries_are_refused(path: str) -> None:
    with pytest.raises(spawn.SpawnError, match="PATH entry"):
        spawn.run([ENV_BIN], env={"PATH": path})


def test_absolute_path_entries_are_accepted() -> None:
    assert _child_env({"PATH": "/usr/bin:/bin"}) == {"PATH": "/usr/bin:/bin"}


def test_token_in_env_keys_or_cwd_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, TOKEN)
    load_github_token()
    with pytest.raises(spawn.SpawnError, match="env keys or cwd"):
        spawn.run([ENV_BIN], env={TOKEN: "1"}, pass_github_token=True)
    leaky = tmp_path / TOKEN
    leaky.mkdir()
    with pytest.raises(spawn.SpawnError, match="env keys or cwd"):
        spawn.run([ENV_BIN], env={}, cwd=leaky)


def test_timeout_is_bounded_even_if_a_grandchild_escapes_the_group() -> None:
    setsid = shutil.which("setsid") or "/usr/bin/setsid"
    began = time.monotonic()
    result = spawn.run(
        [SH, "-c", f"{setsid} sleep 30 & echo started; sleep 30"],
        env={},
        timeout_s=3,
    )
    elapsed = time.monotonic() - began
    assert result.timed_out
    assert elapsed < 3 + spawn.KILL_GRACE_S + 2
    assert b"started" in result.stdout


CHILD_SCRIPT = """
import sys
from mpilot.core import spawn
from mpilot.core.github_token import load_github_token
assert load_github_token() is not None
r = spawn.run(["/bin/sh", "-c", "cat /proc/$PPID/environ"], env={})
sys.stdout.buffer.write(r.stdout)
"""


def test_child_cannot_read_the_token_from_the_parents_proc_environ() -> None:
    # A fresh interpreter gets the token in its *initial* environment block, as at real
    # startup; its own child then reads the parent's /proc/<pid>/environ.
    result = spawn.run(
        [ENV_BIN, f"{GITHUB_TOKEN_ENV}={TOKEN}", sys.executable, "-I", "-c", CHILD_SCRIPT],
        env={},
        timeout_s=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert f"{GITHUB_TOKEN_ENV}=".encode() in result.stdout  # the read itself worked
    assert TOKEN.encode() not in result.stdout


LAUNCHER = """
import ctypes, sys
token, child = sys.argv[1].encode(), sys.argv[2].encode()
args = [sys.executable.encode(), b"-I", b"-c", child]
envp = [b"MPILOT_GITHUB_TOKEN=ghp_FIRSTDUPLICATE", b"MPILOT_GITHUB_TOKEN=" + token]
argv_c = (ctypes.c_char_p * (len(args) + 1))(*args, None)
envp_c = (ctypes.c_char_p * (len(envp) + 1))(*envp, None)
ctypes.CDLL(None).execve(sys.executable.encode(), argv_c, envp_c)
sys.exit("execve failed")
"""


def test_duplicate_token_entries_are_all_wiped() -> None:
    # Raw execve with two MPILOT_GITHUB_TOKEN entries, which a dict-based env can't express.
    result = spawn.run(
        [sys.executable, "-I", "-c", LAUNCHER, TOKEN, CHILD_SCRIPT],
        env={},
        timeout_s=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.count(f"{GITHUB_TOKEN_ENV}=".encode()) == 2
    assert TOKEN.encode() not in result.stdout
    assert b"ghp_FIRSTDUPLICATE" not in result.stdout
