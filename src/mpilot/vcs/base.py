"""VCS interfaces: GitHub credentials and adapter, and the host-owned mirror (SPEC §9.5,
§11.1 H1/H2, §11.3, §11.6; A2, A9, A10).

- Agents have no GitHub tools (§11.3 control 2). Only the orchestrator pushes and opens PRs,
  and only after every gate passed.
- The adapter has no merge method and no auto-merge method (H2); a test asserts it.
- All host git runs in the mirror, never in the workspace, with system/global config off, a
  temp HOME, `core.hooksPath=/dev/null`, `core.fsmonitor=false` and no LFS filters (§9.5).

Implementations: P3-03 (intake, mirror creation), P3-06 (snapshots, diff, phase commits),
P4-20 (PAT credentials, H1 checks), P4-21 (push and draft PR), P8-05 (App tokens).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, field_validator

from mpilot.core.types import RunStatus
from mpilot.models.artifacts import ArtifactRules, FileBlob, FinalVerification, WorkspaceDiff
from mpilot.models.domain import CriticVerdict, IntegrityReport, NonNegInt, PositiveInt
from mpilot.policy.gate import TreeReader
from mpilot.sandbox.base import Workspace

#: H1: the only branches MigrationPilot ever pushes.
BRANCH_PREFIX = "mpilot/"
#: FR-17: escalation branches, pushed by `push_wip_branch` only.
WIP_BRANCH_PREFIX = "mpilot/wip-"
NEEDS_HUMAN_LABEL = "needs-human"
_BRANCH = re.compile(r"^mpilot/[a-z0-9][a-z0-9._-]{0,99}$")
_GH_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

#: The run states whose walked tree is recorded as a phase snapshot (§9.5).
PHASE_SNAPSHOT_STATES = (RunStatus.BUMPED, RunStatus.CODEMODS_APPLIED)


def is_mpilot_branch(name: str) -> bool:
    """H1, the local part: `mpilot/<slug>`, lowercase, no `..`, no `.lock` suffix, no
    nested path. The API checks (default, protected, rulesets) come on top (A9)."""
    return bool(_BRANCH.match(name)) and ".." not in name and not name.endswith(".lock")


def phase_ref(state: RunStatus) -> str:
    if state not in PHASE_SNAPSHOT_STATES:
        raise ValueError(f"{state} is not a phase snapshot state")
    return f"refs/mpilot/phase/{state.value.lower()}"


def attempt_ref(n: int) -> str:
    """`n` is the run-wide attempt number (1-based), so refs never collide across steps."""
    if n < 1:
        raise ValueError("attempt numbers start at 1")
    return f"refs/mpilot/attempt/{n}"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepoRef(_Frozen):
    owner: str
    name: str

    @field_validator("owner", "name")
    @classmethod
    def _gh_name(cls, v: str) -> str:
        if not _GH_NAME.match(v) or v in (".", ".."):
            raise ValueError(f"not a GitHub owner or repo name: {v!r}")
        return v

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


class RepoInfo(_Frozen):
    """From an unauthenticated `GET /repos/{o}/{r}` before cloning (D8), or the token later."""

    default_branch: str = Field(min_length=1)
    size_kb: NonNegInt  # FR-2: over 500 MB is refused
    license_spdx: str | None = None
    private: bool = False


class BranchStatus(_Frozen):
    """What the API says about one branch (A9). Without Administration, classic protection
    patterns on branches that don't exist yet are invisible: GitHub enforces those itself."""

    exists: bool
    is_default: bool
    protected: bool  # the branch's own `protected` flag
    ruleset_protected: bool  # any active ruleset targets it

    @property
    def pushable(self) -> bool:
        return not (self.is_default or self.protected or self.ruleset_protected)


class PrInfo(_Frozen):
    number: PositiveInt
    url: str
    head_branch: str
    base_branch: str
    draft: bool
    created_at: AwareDatetime


class BotPrInfo(_Frozen):
    """FR-19 input: a Renovate or Dependabot PR. Metadata only; MigrationPilot never pushes
    to the bot's branch and works on its own `mpilot/*` branch instead."""

    number: PositiveInt
    url: str
    head_branch: str
    head_sha: str
    base_branch: str
    author_login: str
    title: str

    @field_validator("head_sha")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not _SHA.match(v):
            raise ValueError("head_sha must be a full git object id")
        return v


class SnapshotInfo(_Frozen):
    ref: str  # refs/mpilot/phase/<name> or refs/mpilot/attempt/<n>
    commit: str
    tree_digest: str  # TreeSnapshot.digest() of what was recorded


@runtime_checkable
class CredentialProvider(Protocol):
    """GitHub credentials (A10, §11.6). PAT with Contents and Pull requests write only in
    Phases 4-7, a 1-hour GitHub App installation token for exactly one repo in Phase 8. Never
    Workflows, never Administration. The token never enters a container, a prompt, argv,
    `.git/config` or a URL."""

    @property
    def allowed_repos(self) -> frozenset[str]:
        """`owner/name` slugs this provider may mint or hand out a token for."""
        ...

    async def token_for(self, repo: RepoRef) -> SecretStr:
        """The token for `repo`. Raises CredentialRefusedError for any repo off the list."""
        ...


@runtime_checkable
class VcsAdapter(Protocol):
    """GitHub REST. No merge and no auto-merge method, ever (H2)."""

    async def repo_info(self, repo: RepoRef) -> RepoInfo: ...

    async def branch_status(self, repo: RepoRef, branch: str) -> BranchStatus:
        """Default branch, `protected` flag and rulesets for `branch` (A9)."""
        ...

    async def push_branch(
        self,
        repo: RepoRef,
        mirror: Mirror,
        *,
        branch: str,
        commit: str,
        verified: FinalVerification,
        report: IntegrityReport,
        verdict: CriticVerdict,
    ) -> None:
        """The success push: `commit` from the mirror to `refs/heads/<branch>` (explicit
        refspec, token via GIT_ASKPASS), only after every gate passed (§9.1, §7.4 Success).
        Raises PushRefusedError unless all of these hold, checked right before the push:
        - `is_mpilot_branch(branch)` and not a `mpilot/wip-*` branch
        - `branch_status(...).pushable` (H1, A9)
        - `commit == verified.mirror_commit`, the commit final verification ran on (§9.5)
        - `report.passed` (the integrity gate) and `verdict.decision` is approve (FR-15)
        - `verified`, `report` and `verdict` belong to the same run, so a stale report or
          verdict can't be paired with a newer commit
        """
        ...

    async def push_wip_branch(
        self,
        repo: RepoRef,
        mirror: Mirror,
        *,
        branch: str,
        commit: str,
    ) -> None:
        """The escalation push (FR-17, US-3): a `mpilot/wip-*` branch with the work so far, for
        a human to pick up. Raises PushRefusedError unless `branch` starts with
        `WIP_BRANCH_PREFIX`, `is_mpilot_branch(branch)` and `branch_status(...).pushable`
        hold, and `commit` exists in the mirror (every mirror commit is host-made from host
        snapshots, never sandbox git). A PR on it opens only with `--pr-on-failure` and always
        carries the `needs-human` label (`NEEDS_HUMAN_LABEL`)."""
        ...

    async def upsert_draft_pr(
        self,
        repo: RepoRef,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> PrInfo:
        """Open a draft PR from `branch`, or update the existing one (FR-16: reruns are
        idempotent). Always a draft (CP-2). Raises PushRefusedError if `branch` is not an
        `mpilot/*` branch, or is a `mpilot/wip-*` branch without `NEEDS_HUMAN_LABEL` (FR-17;
        the orchestrator opens one only with `--pr-on-failure`)."""
        ...

    async def bot_pr(self, repo: RepoRef, number: int) -> BotPrInfo:
        """FR-19 metadata for a bot PR."""
        ...


@runtime_checkable
class Mirror(Protocol):
    """The host-owned clone at `runs/<id>/mirror`, never mounted into any container (§9.5)."""

    @property
    def path(self) -> Path: ...

    @property
    def base_sha(self) -> str: ...

    def artifact_rules(self, fixed_dirs: frozenset[str]) -> ArtifactRules:
        """Exclusion rules from the mirror's own `.gitignore` files at base_sha plus
        `fixed_dirs`, with every path tracked at base_sha protected (§9.5 Diff)."""
        ...

    def snapshot(self, ref: str, ws: Workspace, rules: ArtifactRules) -> SnapshotInfo:
        """Walk the workspace (caller holds `Sandbox.frozen()`), store every blob, and point
        `ref` at a commit of exactly that tree. Phase refs at BUMPED and CODEMODS_APPLIED,
        attempt refs at the end of each attempt (§9.5, §10.1)."""
        ...

    def resolve(self, ref: str) -> str:
        """The commit `ref` points to."""
        ...

    def read_tree(self, ref: str) -> Mapping[str, FileBlob]:
        """Every file of a snapshot, for `Workspace.replace_tree` (revert-to-best, crash
        recovery). Never reads sandbox git."""
        ...

    def tree_reader(self, commit: str) -> TreeReader:
        """Read-only view of one commit, for the gate (§11.2 e, h; check i)."""
        ...

    def diff(self, base: str, target: str) -> WorkspaceDiff:
        """Host-computed diff between two mirror commits (with `target_digest`)."""
        ...

    def build_phase_commits(self, refs: Sequence[str]) -> str:
        """At INTEGRITY_CHECK: one commit per phase, each the diff between consecutive
        snapshots (base -> bump -> codemods -> manual fixes). Returns the head commit."""
        ...
