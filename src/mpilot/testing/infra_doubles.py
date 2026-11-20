"""Minimal test doubles for the P1-08 interfaces (sandbox, workspace, mirror, VCS,
orchestrator, store, events, budgets, verification, knowledge).

In `src` so `mypy --strict` proves conformance (`_static_conformance` at the bottom). They
are in-memory and deliberately simple: no containment, no git, no Docker. The real
implementations are the Phase 2-5 tasks named in each interface module. The in-memory
store, bus, workspace and mirror are meant for reuse by orchestrator tests (P3-05).
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, SecretStr

from mpilot.agents.runtime import BudgetGuard
from mpilot.core.errors import (
    AttemptCapError,
    BudgetExceededError,
    CredentialRefusedError,
    IllegalTransition,
    PathEscapeError,
    PushRefusedError,
    StoreError,
)
from mpilot.core.types import (
    AttemptId,
    LLMCallId,
    NetworkMode,
    RunId,
    RunStatus,
    StepId,
    Usage,
)
from mpilot.knowledge.base import (
    ChangelogChunk,
    CodemodEntry,
    CorpusStats,
    DetectOverrides,
    RegistryInfo,
    UsageIndex,
)
from mpilot.models.artifacts import (
    ArtifactRules,
    ChangeKind,
    DirEntry,
    EntryType,
    ExportManifest,
    FileBlob,
    FileChange,
    FileDiff,
    FinalVerification,
    TreeEntry,
    TreeSnapshot,
    WorkspaceDiff,
    check_rel_path,
)
from mpilot.models.domain import (
    Baseline,
    Budgets,
    CriticDecision,
    CriticVerdict,
    Ecosystem,
    EnvProfile,
    EvalResult,
    IntegrityReport,
    Run,
    VerifyResult,
)
from mpilot.models.state import BudgetState, Checkpoint
from mpilot.orchestrator.base import (
    AppendRow,
    CallEstimate,
    EventKind,
    RowT,
    RunEvent,
    RunEventRecord,
    RunRequest,
    SingleRow,
)
from mpilot.policy.gate import TreeReader
from mpilot.policy.test_files import TestFileClassifier
from mpilot.sandbox.base import (
    CacheMode,
    ContainerRuntime,
    ExecResult,
    FreezeMechanism,
    RuntimeInfo,
    Sandbox,
    SandboxDriver,
    SandboxSpec,
    Workspace,
)
from mpilot.vcs.base import (
    NEEDS_HUMAN_LABEL,
    WIP_BRANCH_PREFIX,
    BotPrInfo,
    BranchStatus,
    Mirror,
    PrInfo,
    RepoInfo,
    RepoRef,
    SnapshotInfo,
    is_mpilot_branch,
)

_LINK = "120000"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(blob: FileBlob) -> TreeEntry:
    return TreeEntry(mode=blob.mode, sha256=_sha256(blob.data))


# ---------------------------------------------------------------- sandbox


@dataclass
class FakeSandbox:
    """Records commands; answers with `result`. Tracks whether it is frozen."""

    id: str = "sbx-test"
    runtime: ContainerRuntime = ContainerRuntime.RUNSC
    freeze_mechanism: FreezeMechanism = FreezeMechanism.PAUSE
    warnings: list[str] = field(default_factory=list)
    result: ExecResult = field(
        default_factory=lambda: ExecResult(
            exit_code=0, stdout_tail="", stderr_tail="", duration_s=0.0, truncated=False
        )
    )
    execs: list[tuple[tuple[str, ...], NetworkMode]] = field(default_factory=list)
    is_frozen: bool = False
    destroyed: bool = False
    export_entries: dict[str, TreeEntry] = field(default_factory=dict)

    async def exec(
        self,
        argv: Sequence[str],
        *,
        network: NetworkMode,
        timeout_s: int,
        cwd: str = "/workspace",
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        if self.is_frozen:
            raise RuntimeError("exec while frozen")
        if self.destroyed:
            raise RuntimeError("exec after destroy")
        self.execs.append((tuple(argv), network))
        return self.result

    def frozen(self) -> AbstractAsyncContextManager[None]:
        @asynccontextmanager
        async def _cm() -> AsyncIterator[None]:
            if self.is_frozen:
                raise RuntimeError("already frozen")
            if self.destroyed:
                raise RuntimeError("frozen after destroy")
            self.is_frozen = True
            try:
                yield
            finally:
                self.is_frozen = False

        return _cm()

    async def export_workspace(self, dest: Path) -> ExportManifest:
        return ExportManifest(export_dir=str(dest), entries=dict(self.export_entries))

    async def destroy(self) -> None:
        self.destroyed = True


@dataclass
class FakeDriver:
    info: RuntimeInfo = field(default_factory=lambda: RuntimeInfo(runtime=ContainerRuntime.RUNSC))
    created: list[SandboxSpec] = field(default_factory=list)

    def detect_runtime(self) -> RuntimeInfo:
        return self.info

    async def create(self, spec: SandboxSpec) -> FakeSandbox:
        self.created.append(spec)
        return FakeSandbox(runtime=self.info.runtime, warnings=list(self.info.warnings))

    reaped: list[RunId] = field(default_factory=list)
    leftovers: dict[RunId, int] = field(default_factory=dict)  # containers a crash left behind

    async def restore(
        self, spec: SandboxSpec, manifest: ExportManifest, *, expected_digest: str
    ) -> FakeSandbox:
        if manifest.digest() != expected_digest:
            raise ValueError("export does not match the checkpoint's digest")
        sb = await self.create(spec)
        sb.export_entries = dict(manifest.entries)
        return sb

    async def reap(self, run_id: RunId) -> int:
        self.reaped.append(run_id)
        return self.leftovers.pop(run_id, 0)


# ---------------------------------------------------------------- workspace and mirror


@dataclass
class MemoryWorkspace:
    """Files in a dict. Refuses bad paths, links and over-cap reads like the real one, and
    refuses every call unless `sandbox` (if given) is frozen."""

    files: dict[str, FileBlob] = field(default_factory=dict)
    sandbox: FakeSandbox | None = None
    root: Path = Path("/nonexistent/workspace")

    def _check(self, rel: str) -> str:
        if self.sandbox is not None and not self.sandbox.is_frozen:
            raise RuntimeError("workspace touched while the sandbox runs (§9.5)")
        try:
            return check_rel_path(rel)
        except ValueError as exc:
            raise PathEscapeError(str(exc)) from None

    def _regular(self, rel: str) -> FileBlob:
        blob = self.files.get(self._check(rel))
        if blob is None:
            raise FileNotFoundError(rel)
        if blob.mode == _LINK:
            raise PathEscapeError(f"{rel} is a symlink")
        return blob

    def read_text(self, rel: str, *, max_bytes: int) -> str:
        blob = self._regular(rel)
        if len(blob.data) > max_bytes:
            raise PathEscapeError(f"{rel} is over the read cap")
        return blob.data.decode()

    def read_blob(self, rel: str, *, max_bytes: int) -> FileBlob:
        blob = self.files.get(self._check(rel))
        if blob is None:
            raise FileNotFoundError(rel)
        if len(blob.data) > max_bytes:
            raise PathEscapeError(f"{rel} is over the read cap")
        return blob

    def write_text(self, rel: str, content: str, *, create: bool) -> FileDiff:
        rel = self._check(rel)
        exists = rel in self.files
        if create and exists:
            raise FileExistsError(rel)
        if not create:
            self._regular(rel)
        self.files[rel] = FileBlob(mode="100644", data=content.encode())
        kind = ChangeKind.ADDED if create else ChangeKind.MODIFIED
        return FileDiff(path=rel, kind=kind, unified=f"+++ {rel}\n")

    def delete(self, rel: str) -> FileDiff:
        self._regular(rel)
        del self.files[rel]
        return FileDiff(path=rel, kind=ChangeKind.DELETED, unified=f"--- {rel}\n")

    def list_dir(self, rel: str, depth: int) -> list[DirEntry]:
        prefix = "" if rel in ("", ".") else self._check(rel) + "/"
        out: list[DirEntry] = []
        for path, blob in sorted(self.files.items()):
            if path.startswith(prefix) and path[len(prefix) :].count("/") < depth:
                kind = EntryType.SYMLINK if blob.mode == _LINK else EntryType.FILE
                out.append(DirEntry(path=path, type=kind, size=len(blob.data)))
        return out

    def walk_tree(self, rules: ArtifactRules) -> TreeSnapshot:
        return TreeSnapshot(
            entries={p: _entry(b) for p, b in self.files.items() if not _excluded(p, rules)}
        )

    def replace_tree(self, files: Mapping[str, FileBlob]) -> TreeSnapshot:
        for path in files:
            self._check(path)
        self.files = dict(files)
        return self.walk_tree(ArtifactRules())

    def iter_paths(self) -> list[str]:
        return sorted(self.files)


def _excluded(path: str, rules: ArtifactRules) -> bool:
    if path in rules.tracked:
        return False
    parts = path.split("/")
    if any(p in rules.fixed_dirs for p in parts[:-1]):
        return True
    for base, pattern in rules.gitignore:
        rel = path[len(base) + 1 :] if base else path
        if (not base or path.startswith(base + "/")) and fnmatch.fnmatch(rel, pattern):
            return True
    return False


@dataclass(frozen=True)
class BlobTree:
    """A TreeReader over stored blobs."""

    commit: str
    files: Mapping[str, FileBlob]

    def snapshot(self) -> TreeSnapshot:
        return TreeSnapshot(entries={p: _entry(b) for p, b in self.files.items()})

    def paths(self) -> frozenset[str]:
        return frozenset(self.files)

    def read_text(self, rel_path: str) -> str | None:
        blob = self.files.get(rel_path)
        if blob is None or blob.mode == _LINK:
            return None
        try:
            return blob.data.decode()
        except UnicodeDecodeError:
            return None


def _commit_id(files: Mapping[str, FileBlob], salt: str) -> str:
    digest = TreeSnapshot(entries={p: _entry(b) for p, b in files.items()}).digest()
    return hashlib.sha1((salt + digest).encode(), usedforsecurity=False).hexdigest()


@dataclass
class MemoryMirror:
    """Snapshots as dicts; commit ids are hashes of the tree. No git."""

    base_files: dict[str, FileBlob] = field(default_factory=dict)
    path: Path = Path("/nonexistent/mirror")
    commits: dict[str, dict[str, FileBlob]] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._base = _commit_id(self.base_files, "base")
        self.commits[self._base] = dict(self.base_files)

    @property
    def base_sha(self) -> str:
        return self._base

    def artifact_rules(self, fixed_dirs: frozenset[str]) -> ArtifactRules:
        return ArtifactRules(fixed_dirs=fixed_dirs, tracked=frozenset(self.base_files))

    def snapshot(self, ref: str, ws: Workspace, rules: ArtifactRules) -> SnapshotInfo:
        tree = ws.walk_tree(rules)
        files = {p: ws.read_blob(p, max_bytes=1 << 30) for p in tree.entries}
        commit = _commit_id(files, ref)
        self.commits[commit] = files
        self.refs[ref] = commit
        return SnapshotInfo(ref=ref, commit=commit, tree_digest=tree.digest())

    def resolve(self, ref: str) -> str:
        return self.refs[ref]

    def read_tree(self, ref: str) -> Mapping[str, FileBlob]:
        return dict(self.commits[self.refs.get(ref, ref)])

    def tree_reader(self, commit: str) -> BlobTree:
        return BlobTree(commit=commit, files=self.commits[commit])

    def diff(self, base: str, target: str) -> WorkspaceDiff:
        old, new = self.commits[base], self.commits[target]
        changes: list[FileChange] = []
        for path in sorted(set(old) | set(new)):
            a, b = old.get(path), new.get(path)
            if a == b:
                continue
            if a is None and b is not None:
                changes.append(FileChange(path=path, kind=ChangeKind.ADDED, new=_entry(b)))
            elif b is None and a is not None:
                changes.append(FileChange(path=path, kind=ChangeKind.DELETED, old=_entry(a)))
            elif a is not None and b is not None:
                changes.append(
                    FileChange(path=path, kind=ChangeKind.MODIFIED, old=_entry(a), new=_entry(b))
                )
        digest = self.tree_reader(target).snapshot().digest()
        return WorkspaceDiff(base=base, target=target, target_digest=digest, files=changes)

    def build_phase_commits(self, refs: Sequence[str]) -> str:
        if not refs:
            raise ValueError("no snapshots to commit")
        return self.refs[refs[-1]]


# ---------------------------------------------------------------- VCS


@dataclass
class AllowListCredentials:
    repos: frozenset[str]
    token: str = "test-token-not-real"  # noqa: S105

    @property
    def allowed_repos(self) -> frozenset[str]:
        return self.repos

    async def token_for(self, repo: RepoRef) -> SecretStr:
        if repo.slug not in self.repos:
            raise CredentialRefusedError(f"{repo.slug} is not on the credential list")
        return SecretStr(self.token)


@dataclass
class RecordingVcs:
    """Enforces the H1 and §9.5 push rules, records pushes and PRs. No merge method."""

    default_branch: str = "main"
    protected: frozenset[str] = frozenset()
    pushes: list[tuple[str, str]] = field(default_factory=list)
    prs: dict[str, PrInfo] = field(default_factory=dict)

    async def repo_info(self, repo: RepoRef) -> RepoInfo:
        return RepoInfo(default_branch=self.default_branch, size_kb=1)

    async def branch_status(self, repo: RepoRef, branch: str) -> BranchStatus:
        return BranchStatus(
            exists=branch in self.prs or branch == self.default_branch,
            is_default=branch == self.default_branch,
            protected=branch in self.protected,
            ruleset_protected=False,
        )

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
        if branch.startswith(WIP_BRANCH_PREFIX):
            raise PushRefusedError("wip branches go through push_wip_branch (FR-17)")
        await self._check_branch(repo, branch)
        if commit != verified.mirror_commit:
            raise PushRefusedError("only the commit final verification ran on is pushed")
        if not report.passed:
            raise PushRefusedError("the integrity gate did not pass")
        if len({verified.tests.run_id, report.run_id, verdict.run_id}) != 1:
            raise PushRefusedError("verification, report and verdict are from different runs")
        if verdict.decision is not CriticDecision.APPROVE:
            raise PushRefusedError("the Critic did not approve")
        self.pushes.append((branch, commit))

    async def push_wip_branch(
        self, repo: RepoRef, mirror: Mirror, *, branch: str, commit: str
    ) -> None:
        if not branch.startswith(WIP_BRANCH_PREFIX):
            raise PushRefusedError(f"{branch} is not a {WIP_BRANCH_PREFIX}* branch (FR-17)")
        await self._check_branch(repo, branch)
        try:
            mirror.tree_reader(commit)
        except KeyError:
            raise PushRefusedError(f"{commit} is not a mirror commit") from None
        self.pushes.append((branch, commit))

    async def _check_branch(self, repo: RepoRef, branch: str) -> None:
        if not is_mpilot_branch(branch):
            raise PushRefusedError(f"{branch} is not an mpilot/* branch (H1)")
        if not (await self.branch_status(repo, branch)).pushable:
            raise PushRefusedError(f"{branch} is the default or a protected branch (H1)")

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
        if not is_mpilot_branch(branch):
            raise PushRefusedError(f"{branch} is not an mpilot/* branch (H1)")
        if branch.startswith(WIP_BRANCH_PREFIX) and NEEDS_HUMAN_LABEL not in labels:
            raise PushRefusedError(f"a wip PR always carries {NEEDS_HUMAN_LABEL!r} (FR-17)")
        if branch not in self.prs:
            self.prs[branch] = PrInfo(
                number=len(self.prs) + 1,
                url=f"https://github.com/{repo.slug}/pull/{len(self.prs) + 1}",
                head_branch=branch,
                base_branch=base,
                draft=True,
                created_at=datetime.now(UTC),
            )
        return self.prs[branch]

    async def bot_pr(self, repo: RepoRef, number: int) -> BotPrInfo:
        return BotPrInfo(
            number=number,
            url=f"https://github.com/{repo.slug}/pull/{number}",
            head_branch="renovate/x",
            head_sha="0" * 40,
            base_branch=self.default_branch,
            author_login="renovate[bot]",
            title="Update x",
        )


# ---------------------------------------------------------------- state machine, orchestrator


@dataclass(frozen=True)
class DictTable:
    """A TransitionTable over an explicit edge dict (the real §7.4 table is P3-04's)."""

    edges: Mapping[tuple[RunStatus, RunEvent], RunStatus]

    def next_state(
        self, state: RunStatus, event: RunEvent, *, paused_at: RunStatus | None = None
    ) -> RunStatus:
        if state is RunStatus.PAUSED_LIMIT and event is RunEvent.RESUMED:
            if paused_at is None:
                raise IllegalTransition("RESUMED needs the state the run paused in")
            return paused_at
        if paused_at is not None:
            raise IllegalTransition("paused_at is only for RESUMED out of PAUSED_LIMIT")
        try:
            return self.edges[(state, event)]
        except KeyError:
            raise IllegalTransition(f"no edge {state} --{event}-->") from None

    def events_from(self, state: RunStatus) -> frozenset[RunEvent]:
        return frozenset(e for (s, e) in self.edges if s is state)


@dataclass
class ScriptedOrchestrator:
    """Records calls; `drive` returns `final`."""

    final: RunStatus = RunStatus.END_SUCCESS
    calls: list[str] = field(default_factory=list)

    async def start(self, req: RunRequest) -> RunId:
        self.calls.append("start")
        return RunId("run-test")

    async def drive(self, run_id: RunId) -> RunStatus:
        self.calls.append("drive")
        return self.final

    async def resume(self, run_id: RunId) -> RunStatus:
        self.calls.append("resume")
        return self.final

    async def decide_plan(self, run_id: RunId, *, approve: bool, reason: str | None) -> None:
        self.calls.append(f"decide:{approve}")

    async def cancel(self, run_id: RunId) -> None:
        self.calls.append("cancel")


# ---------------------------------------------------------------- budgets


@dataclass
class _CountingGuard:
    manager: SimpleBudget
    call_id: LLMCallId

    def after_turn(self, usage: Usage | None) -> None:
        self.manager.turns_in_flight += 1
        self.manager.check_turns()

    def before_tool_call(self) -> None:
        self.manager.check_turns()
        self.manager.check_time()


@dataclass
class SimpleBudget:
    """Turn and attempt caps only, on an explicit clock value. Enough to exercise callers;
    the real B2 arithmetic is P4-16's."""

    budgets: Budgets
    state_: BudgetState = field(default_factory=BudgetState)
    turns_in_flight: int = 0
    paused: bool = False
    calls: dict[LLMCallId, tuple[CallEstimate, int]] = field(default_factory=dict)

    def state(self) -> BudgetState:
        return self.state_.model_copy(update={"in_flight_turns": self.turns_in_flight})

    def _turns_left(self) -> int:
        return self.budgets.max_turns - self.state_.spent.turns - self.turns_in_flight

    def _tokens_left(self) -> int:
        s = self.state_.spent
        used = s.input_tokens + s.output_tokens + s.cache_write_tokens
        return self.budgets.max_tokens - used - -(-s.cache_read_tokens // 10)

    def check_turns(self) -> None:
        if self._turns_left() < 0:
            raise BudgetExceededError("turn cap crossed")

    def precheck(self, call_id: LLMCallId, est: CallEstimate) -> int:
        n = est.max_turns_within(self._tokens_left(), self._turns_left())
        if n == 0:
            raise BudgetExceededError("not even one turn fits")
        self.calls[call_id] = (est, n)
        return n

    def guard(self, call_id: LLMCallId) -> BudgetGuard:
        if call_id not in self.calls:
            raise KeyError(f"{call_id} was never prechecked")
        return _CountingGuard(self, call_id)

    def record_call(
        self, call_id: LLMCallId, usage: Usage, turns: int, api_equiv_usd: float
    ) -> None:
        s = self.state_.spent
        spent = s.model_copy(
            update={
                "turns": s.turns + turns,
                "input_tokens": s.input_tokens + usage.input_tokens,
                "output_tokens": s.output_tokens + usage.output_tokens,
                "cache_read_tokens": s.cache_read_tokens + usage.cache_read_tokens,
                "cache_write_tokens": s.cache_write_tokens + usage.cache_write_tokens,
                "api_equiv_usd": s.api_equiv_usd + api_equiv_usd,
            }
        )
        self.turns_in_flight = 0
        self.state_ = self.state_.model_copy(update={"spent": spent})

    def start_attempt(self, step_id: StepId) -> int:
        st = self.state_
        on_step = st.attempts_by_step.get(step_id, 0)
        if on_step >= self.budgets.max_attempts_step:
            raise AttemptCapError(f"{on_step} attempts on {step_id}")
        if st.attempts_total >= self.budgets.max_attempts_run:
            raise AttemptCapError(f"{st.attempts_total} attempts in the run")
        self.state_ = st.model_copy(
            update={
                "attempts_by_step": {**st.attempts_by_step, step_id: on_step + 1},
                "attempts_total": st.attempts_total + 1,
            }
        )
        return self.state_.attempts_total

    def record_sandbox_seconds(self, seconds: float) -> None:
        s = self.state_.spent
        spent = s.model_copy(update={"sandbox_seconds": s.sandbox_seconds + seconds})
        self.state_ = self.state_.model_copy(update={"spent": spent})

    def advance(self, seconds: float) -> None:
        """Test hook: wall-clock time passing (ignored while paused, NFR-2)."""
        if not self.paused:
            self.state_ = self.state_.model_copy(
                update={"active_seconds": self.state_.active_seconds + seconds}
            )

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def check_time(self) -> None:
        if self.state_.active_seconds > self.budgets.minutes_cap * 60:
            raise BudgetExceededError("time cap crossed")


# ---------------------------------------------------------------- store and events


@dataclass
class MemoryStore:
    """A RunStore in dicts. Enforces `Checkpoint.covers` on every write."""

    runs: dict[RunId, Run] = field(default_factory=dict)
    appended: dict[RunId, list[BaseModel]] = field(default_factory=lambda: defaultdict(list))
    singles: dict[tuple[RunId, type], BaseModel] = field(default_factory=dict)
    checkpoints: dict[RunId, Checkpoint] = field(default_factory=dict)
    eval_rows: list[EvalResult] = field(default_factory=list)
    final_verifs: dict[RunId, list[FinalVerification]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def create_run(self, run: Run) -> None:
        if run.id in self.runs:
            raise StoreError(f"run {run.id} exists")
        self.runs[run.id] = run

    def save_run(self, run: Run) -> None:
        if run.id not in self.runs:
            raise KeyError(run.id)
        self.runs[run.id] = run

    def get_run(self, run_id: RunId) -> Run:
        return self.runs[run_id]

    def list_runs(self) -> Sequence[Run]:
        return sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)

    def append(self, row: AppendRow) -> None:
        self.appended[row.run_id].append(row)

    def put(self, row: SingleRow) -> None:
        self.singles[(row.run_id, type(row))] = row

    def rows(self, run_id: RunId, kind: type[RowT]) -> Sequence[RowT]:
        single = self.singles.get((run_id, kind))
        if single is not None:
            return [cast(RowT, single)]  # put() keys singles by type(row)
        return [r for r in self.appended.get(run_id, []) if isinstance(r, kind)]

    def append_final_verification(self, run_id: RunId, result: FinalVerification) -> None:
        self.final_verifs[run_id].append(result)

    def final_verifications(self, run_id: RunId) -> Sequence[FinalVerification]:
        return list(self.final_verifs.get(run_id, []))

    def checkpoint(self, cp: Checkpoint) -> None:
        prev = self.checkpoints.get(cp.run_id)
        if prev is not None:
            problems = cp.covers(prev)
            if problems:
                raise StoreError("; ".join(problems))
        self.checkpoints[cp.run_id] = cp

    def latest_checkpoint(self, run_id: RunId) -> Checkpoint | None:
        return self.checkpoints.get(run_id)

    def add_eval(self, result: EvalResult) -> None:
        self.eval_rows.append(result)

    def evals(self, suite: str) -> Sequence[EvalResult]:
        return [r for r in self.eval_rows if r.suite == suite]


@dataclass
class MemoryBus:
    """Keeps every event; subscribers replay after `after_seq`, then wait for more until
    `close(run_id)`. Single event loop, like the orchestrator (C4)."""

    events: dict[RunId, list[RunEventRecord]] = field(default_factory=lambda: defaultdict(list))
    closed: set[RunId] = field(default_factory=set)
    _signal: asyncio.Event | None = None

    def _notify(self) -> None:
        if self._signal is not None:
            self._signal.set()
            self._signal = None

    def _wait(self) -> asyncio.Event:
        if self._signal is None:
            self._signal = asyncio.Event()
        return self._signal

    def publish(self, run_id: RunId, kind: EventKind, data: dict[str, Any]) -> RunEventRecord:
        if run_id in self.closed:
            raise ValueError(f"run {run_id} is closed")
        seq = len(self.events[run_id]) + 1
        ev = RunEventRecord(run_id=run_id, seq=seq, at=datetime.now(UTC), kind=kind, data=data)
        self.events[run_id].append(ev)
        self._notify()
        return ev

    def close(self, run_id: RunId) -> None:
        self.closed.add(run_id)
        self._notify()

    async def _gen(self, run_id: RunId, after_seq: int) -> AsyncIterator[RunEventRecord]:
        sent = after_seq
        while True:
            for ev in self.events[run_id][sent:]:
                sent = ev.seq
                yield ev
            if run_id in self.closed:
                return
            await self._wait().wait()

    def subscribe(self, run_id: RunId, *, after_seq: int = 0) -> AsyncIterator[RunEventRecord]:
        return self._gen(run_id, after_seq)


# ---------------------------------------------------------------- verification and knowledge


@dataclass
class StaticVerifier:
    baseline_result: Baseline
    verify_result: VerifyResult
    final: FinalVerification

    async def baseline(
        self,
        sb: Sandbox,
        ws: Workspace,
        env: EnvProfile,
        classifier: TestFileClassifier,
        *,
        run_id: RunId,
        runs: int = 3,
    ) -> Baseline:
        return self.baseline_result

    async def verify(
        self,
        sb: Sandbox,
        ws: Workspace,
        env: EnvProfile,
        *,
        run_id: RunId,
        attempt_id: AttemptId | None,
        manifests_changed: bool,
        selector: Sequence[str] | None,
        full: bool,
    ) -> VerifyResult:
        return self.verify_result

    async def final_verify(
        self,
        driver: SandboxDriver,
        mirror: Mirror,
        env: EnvProfile,
        *,
        run_id: RunId,
        commit: str,
        cache: CacheMode,
    ) -> FinalVerification:
        if cache is CacheMode.RUN:
            raise ValueError("final verification never uses the run's cache (§9.5)")
        return self.final.model_copy(update={"mirror_commit": commit})


@dataclass(frozen=True)
class StaticDetector:
    profile: EnvProfile

    def detect(self, tree: TreeReader, overrides: DetectOverrides) -> EnvProfile:
        changes = {k: v for k, v in overrides.model_dump().items() if v is not None}
        sources = {**self.profile.detection_sources, **dict.fromkeys(changes, "override")}
        return self.profile.model_copy(update={**changes, "detection_sources": sources})


@dataclass(frozen=True)
class StaticRegistry:
    infos: Mapping[str, RegistryInfo]

    async def lookup(self, package: str, ecosystem: Ecosystem) -> RegistryInfo:
        return self.infos[package]


@dataclass
class ListChangelog:
    chunks: list[ChangelogChunk] = field(default_factory=list)

    async def build(
        self, package: str, ecosystem: Ecosystem, from_v: str, to_v: str
    ) -> CorpusStats:
        return CorpusStats(documents=1, chunks=len(self.chunks))

    def search(self, query: str, package: str | None, top_k: int) -> list[ChangelogChunk]:
        hits = [c for c in self.chunks if query.lower() in c.text.lower()]
        return hits[: min(top_k, 8)]


@dataclass(frozen=True)
class FixedMapper:
    text: str = ""

    def render(self, ws: Workspace, personalize: Sequence[str], budget_tokens: int) -> str:
        return self.text[: budget_tokens * 3]


@dataclass(frozen=True)
class StaticUsageIndexer:
    result: UsageIndex

    def index(self, ws: Workspace, package: str, ecosystem: Ecosystem) -> UsageIndex:
        return self.result


@dataclass(frozen=True)
class ListCodemods:
    entries: tuple[CodemodEntry, ...] = ()

    def lookup(self, package: str, from_major: int, to_major: int) -> list[CodemodEntry]:
        return [
            e
            for e in self.entries
            if (e.package, e.from_major, e.to_major) == (package, from_major, to_major)
        ]


# ---------------------------------------------------------------- static conformance


def _static_conformance(  # pragma: no cover - checked by mypy, never called
    sandbox: FakeSandbox,
    driver: FakeDriver,
    ws: MemoryWorkspace,
    mirror: MemoryMirror,
    tree: BlobTree,
    creds: AllowListCredentials,
    vcs: RecordingVcs,
    table: DictTable,
    orch: ScriptedOrchestrator,
    budget: SimpleBudget,
    store: MemoryStore,
    bus: MemoryBus,
    verifier: StaticVerifier,
    detector: StaticDetector,
    registry: StaticRegistry,
    changelog: ListChangelog,
    mapper: FixedMapper,
    usage: StaticUsageIndexer,
    codemods: ListCodemods,
) -> None:
    from mpilot.knowledge import base as kb
    from mpilot.orchestrator import base as ob
    from mpilot.sandbox import base as sb
    from mpilot.vcs import base as vb
    from mpilot.verify.base import Verifier

    _a: sb.Sandbox = sandbox
    _b: sb.SandboxDriver = driver
    _c: sb.Workspace = ws
    _d: vb.Mirror = mirror
    _e: TreeReader = tree
    _f: vb.CredentialProvider = creds
    _g: vb.VcsAdapter = vcs
    _h: ob.TransitionTable = table
    _i: ob.Orchestrator = orch
    _j: ob.BudgetManager = budget
    _k: ob.RunStore = store
    _l: ob.EventBus = bus
    _m: Verifier = verifier
    _n: kb.EnvDetector = detector
    _o: kb.RegistryClient = registry
    _p: kb.ChangelogIndex = changelog
    _q: kb.RepoMapper = mapper
    _r: kb.UsageIndexer = usage
    _s: kb.CodemodRegistry = codemods
