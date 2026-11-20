"""Sandbox and host-side workspace interfaces (SPEC §9.2, §9.3, §9.5; A1, A7, D6, D7).

Trust model: the sandbox controls everything inside `/workspace`, `.git` included. So:
- untrusted code runs only in containers (gVisor `runsc` on Linux), never on the host
- the host touches workspace files only through `Workspace`, with `openat2` containment,
  and only while every container mounting the workspace is frozen
- credentials never enter a container (NFR-6), and the work container has no network

Implementations: the Docker driver (P2-02, P2-03, P2-05), the host `Workspace` (P2-01,
tests first), export/restore (P2-04).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mpilot.core.types import NetworkMode, RunId
from mpilot.models.artifacts import (
    ArtifactRules,
    DirEntry,
    ExportManifest,
    FileBlob,
    FileDiff,
    TreeSnapshot,
)
from mpilot.models.domain import NonNegFloat, PositiveInt

#: A sandbox env key that looks like a credential is refused whatever the allowlist says.
#: The positive allowlist itself is the driver's (P2-02); this is the floor under it.
_CREDENTIAL_KEY = re.compile(
    r"TOKEN|SECRET|PASS|API_?KEY|_KEY$|AUTH|CREDENTIAL|PRIVATE|COOKIE|SESSION", re.I
)
_URL_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I)
_CREDENTIAL_PREFIXES = ("GITHUB_", "GH_", "ANTHROPIC_", "CLAUDE_", "MPILOT_", "AWS_", "NPM_TOKEN")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMAGE_DIGEST = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def sandbox_env_problems(env: Mapping[str, str]) -> list[str]:
    """Problems with an environment destined for a container (§9.3 Secrets). Credential-like
    keys, every orchestrator or GitHub/Claude variable, and values carrying URL credentials
    (`https://user:token@host`) are refused; so are malformed keys. The full gitleaks scan
    of values is the redactor's (A3); the driver's positive allowlist is the real control."""
    problems: list[str] = []
    for key, value in env.items():
        if not _ENV_KEY.match(key):
            problems.append(f"{key!r}: not a valid environment variable name")
        elif _CREDENTIAL_KEY.search(key) or key.upper().startswith(_CREDENTIAL_PREFIXES):
            problems.append(f"{key}: credentials never enter the sandbox (NFR-6)")
        elif _URL_CREDENTIALS.search(value):
            problems.append(f"{key}: value carries URL credentials (NFR-6)")
    return problems


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContainerRuntime(StrEnum):
    RUNSC = "runsc"  # gVisor, the Linux default (§9.2)
    RUNC = "runc"  # fallback, always with a recorded warning (NFR-10)


class RuntimeInfo(_Frozen):
    runtime: ContainerRuntime
    version: str = ""
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.runtime is ContainerRuntime.RUNC and not self.warnings:
            raise ValueError("falling back to runc always records a warning (§9.2)")
        return self


class ResourceLimits(_Frozen):
    """§9.3 limits. Defaults live in the config (P1-05, `sandbox.*`), so none here."""

    cpus: float = Field(gt=0)
    memory_gb: PositiveInt
    disk_gb: PositiveInt  # enforced by polling during each command (D6)
    pids: PositiveInt
    # The /tmp tmpfs cap is the driver's (P2-05); the spec gives no number to configure.


class CacheMode(StrEnum):
    """How the package cache is mounted (§9.3 Reproducibility)."""

    RUN = "run"  # the run's cache: read-write in install containers, read-only in the work one
    FRESH = "fresh"  # final verification: an empty cache
    HASH_VERIFIED = "hash_verified"  # final verification: contents verified against the lockfile


class SandboxSpec(_Frozen):
    run_id: RunId
    image: str  # `name@sha256:<digest>`, pinned (§9.3, NFR-5)
    limits: ResourceLimits
    workspace_host_path: Path  # runs/<id>/workspace, bind-mounted at /workspace
    env: Mapping[str, str] = Field(default_factory=dict)
    cache: CacheMode = CacheMode.RUN
    cache_key: str = Field(min_length=1)  # lockfile hash (P2-07)
    #: Set for final verification only: the mirror commit the workspace was built from (§9.5).
    from_commit: str | None = None
    prefer_runtime: ContainerRuntime = ContainerRuntime.RUNSC

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        if not _IMAGE_DIGEST.match(v):
            raise ValueError("image must be pinned by digest: name@sha256:<64 hex>")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: Mapping[str, str]) -> Mapping[str, str]:
        problems = sandbox_env_problems(v)
        if problems:
            raise ValueError("; ".join(problems))
        return v

    @model_validator(mode="after")
    def _check(self) -> Self:
        final = self.from_commit is not None
        if final != (self.cache is not CacheMode.RUN):
            raise ValueError("final verification (from_commit) uses a fresh or verified cache")
        return self


class ExecResult(_Frozen):
    exit_code: int  # -1 when killed (timeout or disk limit)
    stdout_tail: str
    stderr_tail: str
    duration_s: NonNegFloat
    truncated: bool  # output tails were cut
    timed_out: bool = False
    killed_disk_limit: bool = False  # D6: workspace or cache passed the disk limit -> ENV_INSTALL

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.timed_out and self.killed_disk_limit:
            raise ValueError("a command is killed for one reason")
        return self


class FreezeMechanism(StrEnum):
    """How `frozen()` stops the workspace's containers (§9.5). P2-03 records which works."""

    PAUSE = "pause"  # docker pause / unpause
    KILL_ALL = "kill_all"  # kill every process but init and verify none remain


@runtime_checkable
class Sandbox(Protocol):
    """One run's containers: a long-lived work container with no network, plus a short-lived
    install container for each `REGISTRY` command (A7)."""

    @property
    def id(self) -> str: ...

    @property
    def runtime(self) -> ContainerRuntime: ...

    @property
    def freeze_mechanism(self) -> FreezeMechanism: ...

    @property
    def is_frozen(self) -> bool:
        """True inside `frozen()`. The host `Workspace` refuses every file operation unless
        this holds (§9.5); `exec` refuses while it holds."""
        ...

    @property
    def warnings(self) -> Sequence[str]:
        """Recorded and shown in the PR (runc fallback, freeze fallback, ...)."""
        ...

    async def exec(
        self,
        argv: Sequence[str],
        *,
        network: NetworkMode,
        timeout_s: int,
        cwd: str = "/workspace",
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        """Run one command.

        - `NONE`: in the work container, which has no network at all.
        - `REGISTRY`: in a fresh install container on the run's internal network, whose only
          other member is the egress proxy. The container is destroyed, and verified gone,
          before this returns.
        - `env` is checked with `sandbox_env_problems` and the driver's allowlist.
        - Disk use is polled during the command; past the limit it is killed (D6).
        - A timeout kills the command's process group and returns `timed_out=True`.
        Never raises for a failing command. Raises RuntimeError while `is_frozen` (the host is
        touching files) or after `destroy`, and if the sandbox itself is broken. `exec` and
        `frozen()` never overlap: the orchestrator runs one tool call at a time, and `frozen()`
        raises if a command is still running, so a KILL_ALL freeze never kills a command.
        """
        ...

    def frozen(self) -> AbstractAsyncContextManager[None]:
        """Hold every container that mounts the workspace stopped while the host touches it.

        On entry: no install container exists (they never outlive their command), and the
        work container is paused, or with KILL_ALL every process but init is killed and
        verified gone. On exit: the work container runs again. Every `Workspace` call that
        reads or writes files happens inside this (§9.5).
        """
        ...

    async def export_workspace(self, dest: Path) -> ExportManifest:
        """Copy the workspace to `dest` without following symlinks, `.git` included, and
        return the hash manifest (FR-21 pause, P2-04). Runs frozen."""
        ...

    async def destroy(self) -> None:
        """Remove every container and the run's network, even after an error. Idempotent."""
        ...


@runtime_checkable
class SandboxDriver(Protocol):
    def detect_runtime(self) -> RuntimeInfo:
        """`runsc` if it is installed and works, else `runc` plus a warning (§9.2)."""
        ...

    async def create(self, spec: SandboxSpec) -> Sandbox:
        """Start the work container with the §9.3 hardening: uid = host uid, no capabilities,
        no-new-privileges, read-only root, capped /tmp, `network_mode=none`, caches mounted
        as `spec.cache` says."""
        ...

    async def restore(
        self, spec: SandboxSpec, manifest: ExportManifest, *, expected_digest: str
    ) -> Sandbox:
        """Rebuild the workspace from an export and start a sandbox on it. The export must
        match `expected_digest`, the digest held in the DB checkpoint, not only the manifest
        stored next to the export (P2-04)."""
        ...

    async def reap(self, run_id: RunId) -> int:
        """Remove every container and network labelled with `run_id`, verify they are gone,
        and return how many objects were removed. After a crash the old process's containers
        may still run with the workspace mounted, so resume calls this before any `Workspace`
        access (§10.1, §9.5). Idempotent."""
        ...


@runtime_checkable
class Workspace(Protocol):
    """Host-side access to `runs/<id>/workspace` (A1, §9.5). Every open uses `openat2` with
    RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS and a regular-file check
    (NFR-10: `O_NOFOLLOW` per component on macOS). Symlinks, FIFOs, devices and setuid files
    are refused with `PathEscapeError`, as is any path that is absolute or contains `..`.
    Callers hold `Sandbox.frozen()` around every call.
    """

    @property
    def root(self) -> Path: ...

    def read_text(self, rel: str, *, max_bytes: int) -> str:
        """UTF-8 text of a regular file; more than `max_bytes` raises PathEscapeError (the
        read cap is part of containment)."""
        ...

    def read_blob(self, rel: str, *, max_bytes: int) -> FileBlob:
        """Raw bytes and mode of a regular file, or the target of a symlink as a link (never
        followed). Used by the host mirror to record snapshots (§9.5)."""
        ...

    def write_text(self, rel: str, content: str, *, create: bool) -> FileDiff:
        """`create=True`: the file must not exist (create_file). `create=False`: it must be
        an existing regular file (str_replace). Parent directories are created only with
        `create=True`, and never through a symlink."""
        ...

    def delete(self, rel: str) -> FileDiff:
        """Delete one regular file (delete_file; never a directory or a link target)."""
        ...

    def list_dir(self, rel: str, depth: int) -> list[DirEntry]:
        """Entries under `rel`, depth <= 3 (§9.1), sorted, links listed but not followed."""
        ...

    def walk_tree(self, rules: ArtifactRules) -> TreeSnapshot:
        """The whole tree as path -> (mode, sha256), `.git` ignored, links recorded as links
        and never followed (§9.5 Diff). Untracked paths matched by `rules` are left out;
        tracked ones never are."""
        ...

    def replace_tree(self, files: Mapping[str, FileBlob]) -> TreeSnapshot:
        """Make the workspace hold exactly `files` (`.git` untouched): revert-to-best and
        crash recovery (§9.5 Attempts). Everything else, dependency directories included, is
        removed, so an install must follow (§10.1). Returns the walked result."""
        ...

    def iter_paths(self) -> Iterable[str]:
        """Every path in the tree, `.git` excluded, without following links."""
        ...
