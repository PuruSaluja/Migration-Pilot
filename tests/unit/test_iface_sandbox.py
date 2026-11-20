"""P1-08: sandbox and host workspace interfaces (SPEC §9.2, §9.3, §9.5; A1, A7, D6)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mpilot.core.errors import PathEscapeError
from mpilot.core.types import NetworkMode, RunId
from mpilot.models.artifacts import ArtifactRules, ExportManifest, FileBlob, TreeEntry
from mpilot.sandbox import base as sb
from mpilot.testing.infra_doubles import FakeDriver, FakeSandbox, MemoryWorkspace

DIGEST = "node@sha256:" + "a" * 64
LIMITS = sb.ResourceLimits(cpus=4, memory_gb=8, disk_gb=20, pids=1024)


def _spec(**kw: Any) -> sb.SandboxSpec:
    base: dict[str, Any] = {
        "run_id": RunId("run-1"),
        "image": DIGEST,
        "limits": LIMITS,
        "workspace_host_path": Path("/runs/run-1/workspace"),
        "cache_key": "lock-abc",
    }
    base.update(kw)
    return sb.SandboxSpec(**base)


def test_conformance() -> None:
    assert isinstance(FakeSandbox(), sb.Sandbox)
    assert isinstance(FakeDriver(), sb.SandboxDriver)
    assert isinstance(MemoryWorkspace(), sb.Workspace)


# ---------------------------------------------------------------- env and spec


@pytest.mark.parametrize(
    "key",
    [
        "GITHUB_TOKEN",
        "MPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "NPM_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "MY_SECRET",
        "db_password",
        "SOME_API_KEY",
        "PRIVATE_KEY_PATH",
        "X_AUTH",
        "MPILOT_ANYTHING",
        "DB_PASS",
        "SSH_KEY",
        "SESSION_ID",
        "COOKIE_JAR",
    ],
)
def test_credential_like_env_keys_never_enter_the_sandbox(key: str) -> None:
    assert sb.sandbox_env_problems({key: "x"})
    with pytest.raises(ValidationError, match="credentials never enter"):
        _spec(env={key: "x"})


@pytest.mark.parametrize("key", ["CI", "NODE_ENV", "PYTHONDONTWRITEBYTECODE", "LANG", "TZ"])
def test_ordinary_env_keys_pass_the_floor(key: str) -> None:
    assert sb.sandbox_env_problems({key: "1"}) == []


@pytest.mark.parametrize(
    "value", ["https://user:tok@registry.example/simple", "git+ssh://me:pw@host/repo"]
)
def test_url_credentials_in_values_are_refused(value: str) -> None:
    assert sb.sandbox_env_problems({"PIP_INDEX_URL": value})
    assert sb.sandbox_env_problems({"PIP_INDEX_URL": "https://pypi.org/simple"}) == []


def test_malformed_env_keys_are_refused() -> None:
    assert sb.sandbox_env_problems({"A=B": "x", "1X": "y", "": "z"})


@pytest.mark.parametrize(
    "image", ["node:20", "node@sha256:abc", "node@sha512:" + "a" * 64, "@sha256:" + "a" * 64]
)
def test_images_must_be_pinned_by_digest(image: str) -> None:
    with pytest.raises(ValidationError, match="pinned by digest"):
        _spec(image=image)


def test_final_verification_specs_never_reuse_the_run_cache() -> None:
    _spec()
    _spec(from_commit="b" * 40, cache=sb.CacheMode.FRESH)
    _spec(from_commit="b" * 40, cache=sb.CacheMode.HASH_VERIFIED)
    with pytest.raises(ValidationError, match="fresh or verified cache"):
        _spec(from_commit="b" * 40)
    with pytest.raises(ValidationError, match="fresh or verified cache"):
        _spec(cache=sb.CacheMode.FRESH)


def test_runc_fallback_always_records_a_warning() -> None:
    sb.RuntimeInfo(runtime=sb.ContainerRuntime.RUNSC)
    sb.RuntimeInfo(runtime=sb.ContainerRuntime.RUNC, warnings=("gVisor missing",))
    with pytest.raises(ValidationError, match="warning"):
        sb.RuntimeInfo(runtime=sb.ContainerRuntime.RUNC)


def test_exec_result_has_one_kill_reason() -> None:
    kw: dict[str, Any] = {
        "exit_code": -1,
        "stdout_tail": "",
        "stderr_tail": "",
        "duration_s": 1.0,
        "truncated": False,
    }
    sb.ExecResult(**kw, timed_out=True)
    sb.ExecResult(**kw, killed_disk_limit=True)
    with pytest.raises(ValidationError):
        sb.ExecResult(**kw, timed_out=True, killed_disk_limit=True)


# ---------------------------------------------------------------- freezing


def test_frozen_blocks_exec_and_is_not_reentrant() -> None:
    sandbox = FakeSandbox()

    async def run() -> None:
        async with sandbox.frozen():
            assert sandbox.is_frozen
            with pytest.raises(RuntimeError, match="while frozen"):
                await sandbox.exec(["true"], network=NetworkMode.NONE, timeout_s=5)
            with pytest.raises(RuntimeError, match="already frozen"):
                async with sandbox.frozen():
                    pass
        assert not sandbox.is_frozen
        await sandbox.exec(["true"], network=NetworkMode.NONE, timeout_s=5)

    asyncio.run(run())
    assert sandbox.execs == [(("true",), NetworkMode.NONE)]


def test_frozen_releases_on_error() -> None:
    sandbox = FakeSandbox()

    async def run() -> None:
        with pytest.raises(KeyError):
            async with sandbox.frozen():
                raise KeyError("boom")

    asyncio.run(run())
    assert not sandbox.is_frozen


# ---------------------------------------------------------------- workspace contract (double)


def _ws(**files: bytes) -> tuple[MemoryWorkspace, FakeSandbox]:
    sandbox = FakeSandbox(is_frozen=True)
    blobs = {k.replace("__", "/"): FileBlob(mode="100644", data=v) for k, v in files.items()}
    return MemoryWorkspace(files=blobs, sandbox=sandbox), sandbox


def test_workspace_refuses_while_the_sandbox_runs() -> None:
    ws, sandbox = _ws(a=b"x")
    sandbox.is_frozen = False
    with pytest.raises(RuntimeError, match="sandbox runs"):
        ws.read_text("a", max_bytes=10)


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "a/../b", ".git/config", ""])
def test_workspace_refuses_escapes(path: str) -> None:
    ws, _ = _ws(a=b"x")
    with pytest.raises(PathEscapeError):
        ws.read_text(path, max_bytes=10)
    with pytest.raises(PathEscapeError):
        ws.write_text(path, "x", create=True)


def test_workspace_refuses_symlinks_and_over_cap_reads() -> None:
    ws, _ = _ws(big=b"x" * 100)
    ws.files["link"] = FileBlob(mode="120000", data=b"/etc/passwd")
    with pytest.raises(PathEscapeError, match="symlink"):
        ws.read_text("link", max_bytes=1000)
    with pytest.raises(PathEscapeError, match="symlink"):
        ws.write_text("link", "x", create=False)
    with pytest.raises(PathEscapeError, match="read cap"):
        ws.read_text("big", max_bytes=10)
    assert ws.read_blob("link", max_bytes=100).mode == "120000"  # recorded as a link, not followed


def test_create_versus_modify() -> None:
    ws, _ = _ws(a=b"x")
    with pytest.raises(FileExistsError):
        ws.write_text("a", "y", create=True)
    with pytest.raises(FileNotFoundError):
        ws.write_text("new", "y", create=False)
    assert ws.write_text("new", "y", create=True).kind.value == "added"
    assert ws.write_text("a", "z", create=False).kind.value == "modified"
    assert ws.delete("a").kind.value == "deleted"


def test_walk_tree_excludes_untracked_artifacts_but_never_tracked_paths() -> None:
    ws, _ = _ws(
        src__a__py=b"1",
        node_modules__x__index__js=b"2",
        dist__out__js=b"3",
        keep__dist__out__js=b"4",
    )
    rules = ArtifactRules(
        fixed_dirs=frozenset({"node_modules"}),
        gitignore=(("", "dist/*"),),
        tracked=frozenset({"keep/dist/out/js"}),
    )
    tree = ws.walk_tree(rules)
    assert set(tree.entries) == {"src/a/py", "keep/dist/out/js"}
    tracked_but_matching = ArtifactRules(
        gitignore=(("", "src/*"),), tracked=frozenset({"src/a/py"})
    )
    assert "src/a/py" in ws.walk_tree(tracked_but_matching).entries


def test_replace_tree_makes_the_workspace_hold_exactly_the_snapshot() -> None:
    ws, _ = _ws(a=b"1", node_modules__dep=b"2")
    target = {"a": FileBlob(mode="100644", data=b"old"), "b": FileBlob(mode="100755", data=b"#!")}
    tree = ws.replace_tree(target)
    assert set(tree.entries) == {"a", "b"}
    assert ws.iter_paths() == ["a", "b"]  # dependency dirs gone: an install must follow


def test_export_manifest_keeps_git_and_has_a_stable_digest() -> None:
    e = TreeEntry(mode="100644", sha256="1" * 64)
    m1 = ExportManifest(export_dir="/runs/x/export", entries={".git/HEAD": e, "a": e})
    m2 = ExportManifest(export_dir="/elsewhere", entries={"a": e, ".git/HEAD": e})
    assert m1.digest() == m2.digest()
    with pytest.raises(ValidationError):
        ExportManifest(export_dir="/x", entries={"../a": e})


# ---------------------------------------------------------------- review fixes


def test_reap_removes_a_crashed_runs_containers_before_resume() -> None:
    """Review #2: after a crash, old containers may still mount the workspace (§10.1)."""
    driver = FakeDriver(leftovers={RunId("run-1"): 3})
    assert asyncio.run(driver.reap(RunId("run-1"))) == 3
    assert asyncio.run(driver.reap(RunId("run-1"))) == 0  # idempotent
    assert driver.reaped == [RunId("run-1"), RunId("run-1")]


def test_restore_checks_the_checkpoint_digest() -> None:
    e = TreeEntry(mode="100644", sha256="1" * 64)
    manifest = ExportManifest(export_dir="/runs/x/export", entries={"a": e})
    driver = FakeDriver()
    asyncio.run(driver.restore(_spec(), manifest, expected_digest=manifest.digest()))
    with pytest.raises(ValueError, match="digest"):
        asyncio.run(driver.restore(_spec(), manifest, expected_digest="0" * 64))


def test_is_frozen_is_part_of_the_contract() -> None:
    assert "is_frozen" in dir(sb.Sandbox)
