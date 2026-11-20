"""Host-computed value types that the gate (§11.2) and the boundary (§9.5) judge.

These are data, not behaviour: the protocols that produce them (`Workspace.walk_tree`,
`Mirror`, `Verifier.final_verify`) arrive in P1-08 and their implementations in Phases 2-3.
They live here, next to the §12 models, because `GateInput` (P1-07) needs them now.

Everything here is computed on the host from host-owned data (§9.5). Nothing is ever read
from sandbox git state.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mpilot.models.domain import Ecosystem, NonNegInt, Percent, TestResult, TestStage, VerifyResult

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

GitMode = Literal["100644", "100755", "120000"]  # regular, executable, symlink (never followed)


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def check_rel_path(path: str) -> str:
    """A workspace-relative POSIX path: no absolute, no `.`/`..`/empty parts, never `.git`."""
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError(f"not a workspace-relative POSIX path: {path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"path has an empty, '.' or '..' component: {path!r}")
    if parts[0] == ".git":
        raise ValueError(f"paths under .git are never part of a tree or diff: {path!r}")
    return path


def _check_git_sha(value: str) -> str:
    if not _GIT_SHA.match(value):
        raise ValueError("must be a full lowercase hex git object id")
    return value


RelPath = Annotated[str, Field(min_length=1)]


class TreeEntry(ArtifactModel):
    """One path in a walked tree. A symlink is recorded as a link (mode 120000) whose hash is
    over the link target string; its target is never followed (§9.5, A1)."""

    mode: GitMode
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not _SHA256.match(v):
            raise ValueError("sha256 must be 64 lowercase hex chars")
        return v


class TreeSnapshot(ArtifactModel):
    """A tree as path -> entry: the output of `Workspace.walk_tree`, or a mirror commit's tree.

    Gate check (i) compares two of these with `==`.
    """

    entries: dict[RelPath, TreeEntry] = Field(default_factory=dict)

    @field_validator("entries")
    @classmethod
    def _paths(cls, v: dict[str, TreeEntry]) -> dict[str, TreeEntry]:
        for path in v:
            check_rel_path(path)
        return v

    def digest(self) -> str:
        """Stable SHA-256 over the sorted entries (independent of insertion order)."""
        canon = [[p, e.mode, e.sha256] for p, e in sorted(self.entries.items())]
        return hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()


class ChangeKind(StrEnum):
    """A renamed file is a DELETED plus an ADDED entry: the walk has no rename detection, so a
    renamed test always shows up as a deleted test (§11.2 c)."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


class Hunk(ArtifactModel):
    old_start: NonNegInt
    old_lines: NonNegInt
    new_start: NonNegInt
    new_lines: NonNegInt
    text: str  # the hunk body in unified-diff form, `@@` header excluded


class FileChange(ArtifactModel):
    path: RelPath
    kind: ChangeKind
    old: TreeEntry | None = None
    new: TreeEntry | None = None
    hunks: list[Hunk] = Field(default_factory=list)
    binary: bool = False  # no hunks for binary content; the entries still say what changed

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return check_rel_path(v)

    @model_validator(mode="after")
    def _check(self) -> Self:
        has_old, has_new = self.old is not None, self.new is not None
        if self.kind is ChangeKind.ADDED and (has_old or not has_new):
            raise ValueError("an added file has only a new entry")
        if self.kind is ChangeKind.DELETED and (has_new or not has_old):
            raise ValueError("a deleted file has only an old entry")
        if self.kind is ChangeKind.MODIFIED:
            if not (has_old and has_new):
                raise ValueError("a modified file has both entries")
            if self.old == self.new:
                raise ValueError("a modified file's entries must differ")
        if self.binary and self.hunks:
            raise ValueError("binary changes carry no hunks")
        return self


class WorkspaceDiff(ArtifactModel):
    """Host-computed diff between two host mirror commits (§9.5).

    Both ends are named, so the gate can prove what a diff is about: `target` is the commit
    whose tree the diff leads to (a phase or attempt snapshot ref's commit, or the commit to
    push), and `target_digest` is that tree's `TreeSnapshot.digest()`. Phase commits are
    rebuilt from snapshots, so two commits can share a tree; the digest is what chains.
    """

    base: str
    target: str
    target_digest: str
    files: list[FileChange] = Field(default_factory=list)

    @field_validator("base", "target")
    @classmethod
    def _commits(cls, v: str) -> str:
        return _check_git_sha(v)

    @field_validator("target_digest")
    @classmethod
    def _digest(cls, v: str) -> str:
        if not _SHA256.match(v):
            raise ValueError("target_digest must be 64 lowercase hex chars")
        return v

    @model_validator(mode="after")
    def _check(self) -> Self:
        paths = [f.path for f in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("files must be sorted by path with no duplicates")
        return self

    def paths(self, *kinds: ChangeKind) -> frozenset[str]:
        """Paths changed in any of `kinds` (all kinds if none given)."""
        return frozenset(f.path for f in self.files if not kinds or f.kind in kinds)


class TestCounts(ArtifactModel):
    """Counts as the test runner's own log summary states them (§11.2 a cross-check)."""

    __test__ = False  # not a pytest class

    passed: NonNegInt
    failed: NonNegInt
    skipped: NonNegInt
    errors: NonNegInt


class FinalVerification(ArtifactModel):
    """The whole pipeline run in a fresh sandbox built from `mirror_commit` (§9.5).

    Success, L0-L2 and every gate check judge only this record. `coverage_pct` comes from one
    extra instrumented run of the same commit, so `tests` is uninstrumented, as at baseline.
    """

    mirror_commit: str
    verify: VerifyResult  # install -> build -> typecheck -> full suite
    tests: TestResult  # stage == final; parsed from JUnit
    runner_exit_code: int | None  # exit code of the test command
    runner_summary: TestCounts | None  # parsed from the runner log; None if it printed none
    resolved_versions: dict[str, str] = Field(default_factory=dict)  # package -> version
    coverage_pct: Percent | None = None
    cache: Literal["fresh", "hash_verified"]

    @field_validator("mirror_commit")
    @classmethod
    def _commit(cls, v: str) -> str:
        return _check_git_sha(v)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.tests.stage is not TestStage.FINAL:
            raise ValueError("final verification judges a test run with stage == final")
        return self


class ApiPattern(ArtifactModel):
    """A removed API from the curated migration registry (D3), searched statically (§11.2 h).

    Only curated patterns decide the gate; APIs a model extracts itself are warnings.
    """

    api: str = Field(min_length=1)  # human-readable name, e.g. "ReactDOM.render"
    ecosystem: Ecosystem
    pattern: str = Field(min_length=1)  # Python `re` syntax, matched per line
    globs: tuple[str, ...] = ()  # limit the search to these path globs; empty = all files

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from None
        return v


# ---------------------------------------------------------------- P1-08: workspace and mirror data


class FileBlob(ArtifactModel):
    """One file as stored in a host mirror ref: its mode and raw bytes. For a symlink
    (mode 120000) `data` is the link target, which is written as a link, never followed."""

    mode: GitMode
    data: bytes


class FileDiff(ArtifactModel):
    """What one host-side edit did (FR-10: every edit records a unified diff)."""

    path: RelPath
    kind: ChangeKind
    unified: str  # unified diff text, "" for binary content

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return check_rel_path(v)


class EntryType(StrEnum):
    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"  # listed, never followed
    OTHER = "other"  # FIFO, device, socket: listed so the model sees it, never opened


class DirEntry(ArtifactModel):
    path: RelPath
    type: EntryType
    size: NonNegInt  # bytes from lstat; 0 for directories


class ArtifactRules(ArtifactModel):
    """Which *untracked* paths the host walk leaves out (§9.5). Built from host-owned data
    only: the mirror's `.gitignore` files at base_sha plus a fixed list. Workspace git state
    never decides exclusions, and paths tracked at base_sha are never excluded."""

    #: (directory the .gitignore lives in, "" for the root; pattern), as read at base_sha.
    gitignore: tuple[tuple[str, str], ...] = ()
    #: Fixed directory names: node_modules, .venv, and build output and caches named in the
    #: EnvProfile.
    fixed_dirs: frozenset[str] = frozenset()
    #: Paths tracked at base_sha. Never excluded, even if a pattern matches them.
    tracked: frozenset[str] = frozenset()


def _check_export_path(path: str) -> str:
    """Like `check_rel_path`, but `.git` is allowed: an export carries the whole workspace."""
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError(f"not a workspace-relative POSIX path: {path!r}")
    if any(p in ("", ".", "..") for p in path.split("/")):
        raise ValueError(f"path has an empty, '.' or '..' component: {path!r}")
    return path


class ExportManifest(ArtifactModel):
    """A workspace export (FR-21 pause, P2-04): every entry with its mode and hash, `.git`
    included, links recorded as links. `restore` must reproduce exactly this digest."""

    export_dir: str = Field(min_length=1)  # host directory holding the export
    entries: dict[str, TreeEntry]

    @field_validator("entries")
    @classmethod
    def _paths(cls, v: dict[str, TreeEntry]) -> dict[str, TreeEntry]:
        for path in v:
            _check_export_path(path)
        return v

    def digest(self) -> str:
        canon = [[p, e.mode, e.sha256] for p, e in sorted(self.entries.items())]
        return hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()
