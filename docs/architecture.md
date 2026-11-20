# Architecture

Status: §4–§11 are **real code** (P1-07: §4–§6, §8; P1-08: §7, §9–§11). Signatures below are summaries; the docstrings in the named modules are authoritative. §11's knowledge interfaces are explicitly **drafts** that Phases 2–5 may extend, recording the change here.
P1-12 freezes them once `spec-reviewer` approves.
After the freeze, changing an interface is a spec change (SPEC §18 rule 5).
The signatures assume the proposals in `docs/decisions.md`, referenced below as A1, B2 and so on.

## 1. Processes and trust boundaries

| Zone | Holds | Never |
|---|---|---|
| **Orchestrator (host process)** | DB, artifacts, GitHub token, Claude OAuth token, host mirror repo | Executes repo code; runs git inside the workspace (A2) |
| **Agent runtime subprocess** (Claude Code via the SDK) | OAuth token only; temp `cwd` and `HOME`; only `mcp__mpilot__*` tools (A5) | Built-in tools, user settings, plugins, the GitHub token |
| **Work container** (gVisor, `network_mode=none`) | `/workspace` bind mount (uid = host uid), caches | Credentials; any network (A7) |
| **Install container** (gVisor, short-lived, per-run internal network) | Same workspace mount and image; used only by `install_deps` | Credentials; any host except through the proxy on port 443 (A7) |
| **Egress proxy container** | Registry allowlist (Host + SNI, port 443 only) | Anything off the allowlist (denied and logged) |

Data paths:
- **Model → tool:** the model asks for a tool → in-process MCP server → `ToolHost` → pre-policy → `Tool.run` → post-policy (redaction) → back to the model. Read and edit tools act on `Workspace` on the host (A1); everything that executes runs through `Sandbox.exec`.
- **Diff, gate and PR (SPEC §9.5):** the host records snapshots as mirror refs (phases, attempts) → builds one mirror commit per phase from consecutive snapshots → checks that the mirror tree equals the walked tree → runs the **whole pipeline in a fresh sandbox built from that commit**, which every success signal and gate check judges → `VcsAdapter.push_branch` of exactly that commit (H1 checked again inside the adapter).
- **Child processes:** every subprocess (Docker, git, the SDK, tree-sitter) starts through `core.spawn` with an explicit env. The GitHub token is removed from the orchestrator's own env at startup (A10).

## 2. Package layout (adds to SPEC §15)

```
src/mpilot/
  core/        types.py (enums, ids), clock.py, errors.py, tokens.py (B3 estimator), spawn.py (A10)
  models/      domain.py (Pydantic, §12), artifacts.py (trees, diffs, blobs, FinalVerification,
               ApiPattern, ExportManifest), state.py (Checkpoint, BudgetState, failure history),
               tables.py (SQLAlchemy, P1-09), migrate.py + migrations/ (Alembic, shipped in the
               package, no alembic.ini), store.py (RunStore impl, P1-10)
  config.py    settings + NFR-12 validation + config_hash
  orchestrator/ base.py (interfaces), states.py (pure transition table), machine.py, budget.py,
               checkpoint.py, loop.py
  agents/      runtime.py, sdk_runtime.py, messages_runtime.py, fake_runtime.py, cassette.py,
               router.py, roles/<role>.py, prompts/<role>.md, schemas.py
  tools/       base.py (Tool, ToolHost), files.py, search.py, exec.py, knowledge.py, mcp_server.py
  policy/      hooks.py (tool policies), gate.py (GateInput, PolicyCheck, evaluate_gate),
               integrity.py, diff_limits.py, secrets.py, licenses.py,
               allowed_files.py, branch.py, test_files.py (D2 classifier)
  testing/     policy.py (E1 marker plugin), doubles.py (P1-07 doubles), infra_doubles.py (P1-08
               doubles: in-memory sandbox, workspace, mirror, store, bus, budget), all mypy-checked
  sandbox/     base.py (interfaces), driver.py (Docker), workspace.py (host-side safe FS, A1),
               network.py, proxy/, e2b.py
  vcs/         base.py (CredentialProvider, VcsAdapter, Mirror), github.py, mirror.py
  verify/      base.py (Verifier)        knowledge/  base.py (drafts)
  detect/  codemods/registry.yaml  telemetry/  api/  web/
```

## 3. Core enums and value types (`core/types.py`)

```python
class Tier(StrEnum): OPUS = "opus"; SONNET = "sonnet"            # no other members, ever
class Effort(StrEnum): HIGH = "high"; MEDIUM = "medium"          # no other members, ever
class Role(StrEnum): ANALYZER; PLANNER; EDITOR; DIAGNOSER; CRITIC; EXPLORER; PR_WRITER; B2_SINGLE_SHOT; B3_GENERIC
class StepType(StrEnum): BUMP; CODEMOD; EDIT_CLUSTER; CONFIG; TEST_TOUCH; VERIFY
class RunStatus(StrEnum):  # SPEC §7.4, one member per state plus terminal END_* members
    INIT; CLONED; ENV_DETECTED; BASELINE_OK; ANALYZED; PLANNED; AWAITING_APPROVAL; BUMPED;
    CODEMODS_APPLIED; REPAIR_LOOP; INTEGRITY_CHECK; REVIEW; PR_OPENED; ESCALATED; PAUSED_LIMIT;
    END_SUCCESS; END_BASELINE_RED; END_REJECTED; END_BUDGET_EXCEEDED; END_ERROR; END_ESCALATED;
    END_CANCELLED                                                  # C6 [SPEC]
class FailureCategory(StrEnum): ...  # exactly the entries of SPEC §14.5 (17 in v1.4, incl. CANCELLED)
class Outcome(StrEnum): IMPROVED; SAME; REGRESSED; GREEN          # C1
class NetworkMode(StrEnum): NONE = "none"; REGISTRY = "registry"
class ExitCode(IntEnum): SUCCESS=0; FAILED=1; INPUT_ERROR=2; BASELINE_RED=3; BUDGET_EXCEEDED=4; PAUSED=5
def exit_code_for(status: RunStatus) -> ExitCode: ...              # C6 matrix: every terminal state + PAUSED_LIMIT (5);
                                                                  # the CLI returns 130 when interrupted in AWAITING_APPROVAL

@dataclass(frozen=True)
class Usage: input_tokens: int; output_tokens: int; cache_read_tokens: int; cache_write_tokens: int
    def cap_tokens(self) -> int: ...  # input + output + cache_write + ceil(0.1 * cache_read)  (B2)

# D9 [SPEC]: TestResult (§12) gains passed_ids and failed_ids: frozenset[str]
# executed_count = passed + failed + errors (skipped excluded)
```

Domain models are the §12 classes, one Pydantic v2 model each, with IDs as `NewType` strings (`RunId`, `StepId` and so on).
`LLMCall.tier/effort` and `RoutingDecision.tier/effort` use the enums above, so an invalid value can't even be represented.

## 4. Agent runtime (`agents/runtime.py`)

```python
class ModelChoice(BaseModel, frozen=True):           # rejects Haiku, Opus+medium, tier/model mismatch
    tier: Tier; effort: Effort; model_id: str

class RoleCallRequest(BaseModel, frozen=True):       # serializable: cassettes key on request_hash() (B6)
    context: ToolContext                             # run, step, role, caller, role_call_id, max_tool_calls (§6)
    choice: ModelChoice; routing_decision_id: RoutingDecisionId
    system_prompt: str                               # stable prefix: rules, H6, tool docs, repo map (§10.3)
    user_prompt: str                                 # untrusted content already in <untrusted_repo_content>
    tool_allowlist: frozenset[str]                   # bare registry names; exposed as mcp__mpilot__<name>
    output_schema: dict[str, Any]                    # must require `confidence: number [0, 1]` (validated)
    max_turns: PositiveInt                           # already clamped by the BudgetManager (B2)
    call_id -> LLMCallId; role -> Role
    def request_hash(self) -> str: ...               # content only: random run/call/step/attempt/
                                                     # routing IDs excluded, so a replay matches (B6)

class StopReason(StrEnum): COMPLETED; SCHEMA_INVALID; MAX_TURNS; BUDGET_STOP; TOOL_CALL_CAP
    # usage/rate limit raises UsageLimitError instead; cancellation is asyncio.CancelledError

class RoleCallResult(BaseModel, frozen=True):
    call_id: LLMCallId; stop: StopReason
    output: dict[str, Any] | None                    # present exactly when stop == COMPLETED
    model_id: str; effort_applied: Effort            # what actually ran, never just what was asked
    models_seen: frozenset[str]                      # A4 guard; Haiku unrepresentable
    thinking_enabled: bool; usage: Usage; turns: int; latency_ms: int
    tool_call_ids: tuple[ToolCallId, ...]            # every invoke, allowed and denied; <= max_tool_calls + 1

def check_result(req: RoleCallRequest, res: RoleCallResult) -> None:
    """Orchestrator-side second line: ForbiddenModelError (other model), LockdownViolation
    (thinking off), RuntimeCallError (call id, effort, turns, or more than max_tool_calls + 1
    invocations: the cap refusal itself is normal, a call after it is a runtime bug)."""

class RuntimeCapabilities(BaseModel, frozen=True):
    per_call_effort: bool; default_effort_verified_high: bool       # B1 fallback needs both
    prompt_caching: bool; context_editing: bool                     # verified in P4-01
class SelftestCheck(BaseModel, frozen=True): name: str; passed: bool; detail: str
class SelftestReport(BaseModel, frozen=True): runtime: str; checks: tuple[SelftestCheck, ...]; passed -> bool

class BudgetGuard(Protocol):                         # from BudgetManager.guard() (P1-08, P4-16)
    def after_turn(self, usage: Usage | None) -> None: ...   # None = runtime can't report per turn
    def before_tool_call(self) -> None: ...                  # both raise BudgetExceededError

class AgentRuntime(Protocol):
    name: str; capabilities: RuntimeCapabilities     # read-only properties
    def check_lockdown_static(self) -> None: ...     # §8.1 items 1-10 at every start; LockdownViolation
    async def call(self, req: RoleCallRequest, tools: ToolHost, guard: BudgetGuard) -> RoleCallResult:
        """Thinking always on; exposes only req.tool_allowlist; per tool call:
        guard.before_tool_call() -> tools.invoke(name_as_sent, args, req.context) -> for_model();
        after a TOOL_CALL_CAP refusal the next tool call ends the call (TOOL_CALL_CAP);
        per turn: guard.after_turn(). Raises UsageLimitError, ForbiddenModelError,
        LockdownViolation, RuntimeCallError."""
    async def selftest(self) -> SelftestReport: ...
```

Implementations: `AgentSDKRuntime` (default), `MessagesRuntime` (optional), `FakeRuntime` (scripted), `CassetteRuntime` (replay, B6).
Test double: `testing.doubles.EchoRuntime`.

## 5. Model Router (`agents/router.py`)

```python
class StickyState(BaseModel, frozen=True):
    tier_escalated: bool = False; effort_escalated: bool = False
    trigger_fired: bool = False                      # any trigger 1-8 in scope => no medium afterwards
    def covers(self, other: StickyState) -> bool: ...   # no de-escalation; any -> bool

class ExplorerFlag(StrEnum): NO_CITATIONS; OVER_CAP; REPEATED_QUESTION

class RoutingContext(BaseModel, frozen=True):        # no IDs (B9); validated shape
    role: Role; caller: Role | None                  # set exactly for Explorer calls; one of
                                                     # EXPLORE_CALLER_ROLES (Analyzer, Planner, Editor, Diagnoser, Critic)
    step_type: StepType | None; touches_test_config: bool    # Editor/Diagnoser always in a step;
    migration_kind: MigrationKind; usage_sites: int | None   # Analyzer/Planner/PR Writer never
    attempt_in_step: int; attempts_on_signature: int         # 1-based; 0 = not in a step / none
    prev_outcome: Outcome | None; failure_category: FailureCategory | None
    expected_files: int | None; expected_lines: int | None   # None = unknown, never "small"
    has_changelog_mapping: bool; error_maps_to_changelog: bool; is_codemod_followup: bool
    last_confidence: Confidence | None; diagnosis_confidence: Confidence | None
    policy_denials_in_step: int; schema_invalid_in_step: bool
    critic_verdict: CriticDecision | None
    explorer_flags: frozenset[ExplorerFlag]; pr_template_rejected: bool
    sticky: StickyState
    def inputs_hash(self, policies: RoutingPolicies) -> str: ...   # sha256 of canonical JSON

class RoutingPolicies(BaseModel, frozen=True):       # all_opus => all_high; pins re-validated (P1-06)
    policy: RoutingPolicy; effort_policy: EffortPolicy; pins: Mapping[Role, RolePin]
    # SPEC §8.4 "Pins": a pin replaces the default tier/effort only (effort: high only, never
    # medium); triggers, effort
    # escalation, stickiness and medium conditions always apply on top

class RouteOutcome(BaseModel, frozen=True):          # B9: pure result, no IDs
    tier: Tier; effort: Effort; reason_codes: tuple[str, ...]   # at least one
    escalated_from: Literal[Tier.SONNET] | None; effort_escalated_from: Literal[Effort.MEDIUM] | None
    sticky: bool; new_sticky: StickyState; inputs_hash: str
    def to_decision(self, *, decision_id, run_id, step_id, ctx, policies) -> RoutingDecision: ...
        # refuses an outcome whose inputs_hash is not ctx.inputs_hash(policies)

class ModelRouter(Protocol):
    def route(self, ctx: RoutingContext, policies: RoutingPolicies) -> RouteOutcome: ...
        # pure, deterministic; never reads clock, budget or usage

class EscalationTrigger(StrEnum): ...                 # the eight §8.4 triggers, t1 ... t8
def fired_triggers(ctx: RoutingContext) -> frozenset[EscalationTrigger]: ...
def sonnet_allowed_by_default(ctx: RoutingContext) -> bool: ...   # §8.4 "Sonnet allowed only when"
def is_trivial_edit(ctx: RoutingContext) -> bool: ...             # the only Editor medium case
def minimal_sticky_after(ctx, policies) -> StickyState: ...       # least new_sticky any outcome must carry

def route_invariant_violations(ctx, policies, out) -> list[str]: ...
    # everything forbidden or forced, for every router version: fixed roles and policies (B8);
    # pins honoured upward only; Sonnet conditions for unpinned routed roles; any fired trigger
    # => high, recorded in new_sticky, and opus for routed roles under `routed`; never
    # de-escalates; medium only when every §8.4 condition holds (incl. nothing fired earlier in
    # scope, trivial edit for the Editor, no Explorer flags, no rejected PR draft); every
    # escalation (tier, effort, trigger) recorded in new_sticky so the next call can't undo it.
    # An Explorer call shares its step's StickyState, so its escalation also rules out Editor
    # medium for the rest of the step (stricter than required, on purpose).
```

The orchestrator stores `StickyState` per step and passes it back in each call. That's how stickiness works while `route` stays pure.
Test double: `testing.doubles.FixedRouter`.

## 6. Tools and tool policy (`tools/base.py`, `policy/hooks.py`)

```python
REGISTRY_TOOL_NAMES: frozenset[str]                  # exactly §9.1 + explore; a test parses SPEC §9.1
EXPLORE_CALLER_ROLES: frozenset[Role]                # roles with `explore` (§8.2, §8.5)
MCP_TOOL_PREFIX = "mcp__mpilot__"
def mcp_name(tool: str) -> str: ...; def bare_name(exposed: str) -> str | None: ...   # None => deny

class ToolEffect(StrEnum): READ; WRITE; EXEC; NETWORK; DELEGATE

@dataclass(frozen=True)
class ToolSpec:                                      # name must be in the registry
    name: str; description: str
    input_model: type[BaseModel]; output_model: type[BaseModel]; effect: ToolEffect

class ToolContext(BaseModel, frozen=True):           # built by the orchestrator, never from model output
    run_id: RunId; role_call_id: LLMCallId; role: Role; caller: Role | None
    step_id: StepId | None; step_type: StepType | None; attempt_id: AttemptId | None
    step_target_files: frozenset[str]                # test_touch: the only test files writable (H3)
    flag_justifications: Mapping[str, str]           # H8 (A11)
    max_tool_calls: int                              # remaining per-attempt allowance (§8.3); every
                                                     # invocation counts, denied ones too

class Tool(Protocol[InT_contra, OutT_co]):           # constructed per run with only its dependencies
    spec: ToolSpec                                   # (Workspace, Sandbox, ...): no ToolEnv
    async def run(self, args: InT_contra, ctx: ToolContext) -> OutT_co: ...

class HostDenyCode(StrEnum): UNKNOWN_TOOL; ROLE_NOT_ALLOWED; INVALID_ARGS; TOOL_CALL_CAP

class ToolInvocation(BaseModel, frozen=True):        # mirrors the §12 ToolCall row
    tool_call_id: ToolCallId; tool: str; decision: PolicyDecision
    output: dict | None; deny_code: str | None; deny_reason: str | None
    def for_model(self) -> dict[str, Any]: ...       # a denial is a normal result for the model

class ToolHost(Protocol):
    registry: Mapping[str, ToolSpec]                 # read-only property
    def specs_for(self, role: Role) -> Sequence[ToolSpec]: ...
    async def invoke(self, name: str, raw_args: Mapping[str, Any], ctx: ToolContext) -> ToolInvocation:
        """name = exactly what the model sent. Order: cap -> resolve name -> role allowlist ->
        validate args -> pre-policies (first deny wins) -> run -> post-policies (redaction last)
        -> ToolCall row + checkpoint. Refusals return a denied invocation, never raise; an
        unexpected tool exception is recorded, then re-raised (END_ERROR)."""

class PolicyVerdict(BaseModel, frozen=True):         # policy/hooks.py
    decision: PolicyDecision; policy: str | None; code: str | None; reason: str | None
    allowed -> bool; allow() / deny(policy, code, reason)

class ToolPolicy(Protocol):
    name: str
    def pre(self, spec: ToolSpec, args: BaseModel, ctx: ToolContext) -> PolicyVerdict: ...
    def post(self, spec: ToolSpec, output: BaseModel, ctx: ToolContext) -> BaseModel: ...  # redaction (A3)

def run_pre(policies, spec, args, ctx) -> PolicyVerdict: ...
def run_post(policies, spec, output, ctx) -> BaseModel: ...   # TypeError if a policy breaks the schema
```

No GitHub tools and no commit, push or PR tools (§11.3 control 2, §9.1); a test asserts it. `explore` is a `DELEGATE` tool whose `run()` asks the orchestrator for an Explorer role call (§8.5).
Test doubles: `InMemoryToolHost` (the invoke pipeline without the DB), `StaticTool`, `DenyToolsPolicy`.

## 7. Sandbox, workspace and mirror (`sandbox/base.py`, `vcs/base.py`)

```python
def sandbox_env_problems(env: Mapping[str, str]) -> list[str]: ...
    # the floor under the driver's allowlist: credential-like keys (TOKEN, SECRET, PASS, API_KEY,
    # *_KEY, AUTH, COOKIE, SESSION, ...), GITHUB_/GH_/ANTHROPIC_/CLAUDE_/MPILOT_/AWS_ variables and
    # values with URL credentials (https://user:tok@host) never enter a container

class ContainerRuntime(StrEnum): RUNSC; RUNC
class RuntimeInfo(BaseModel, frozen=True):           # runc always carries a warning
    runtime: ContainerRuntime; version: str; warnings: tuple[str, ...]
class ResourceLimits(BaseModel, frozen=True):        # values from config `sandbox.*` (D6, D7)
    cpus: float; memory_gb: int; disk_gb: int; pids: int
class CacheMode(StrEnum): RUN; FRESH; HASH_VERIFIED  # RUN: rw in install, ro in work container
class SandboxSpec(BaseModel, frozen=True):
    run_id: RunId; image: str                        # name@sha256:<digest>, validated
    limits: ResourceLimits; workspace_host_path: Path
    env: Mapping[str, str]                           # sandbox_env_problems() must be empty
    cache: CacheMode; cache_key: str                 # final verification (from_commit) never uses RUN
    from_commit: str | None; prefer_runtime: ContainerRuntime
class ExecResult(BaseModel, frozen=True):
    exit_code: int; stdout_tail: str; stderr_tail: str; duration_s: float
    truncated: bool; timed_out: bool; killed_disk_limit: bool   # at most one kill reason
class FreezeMechanism(StrEnum): PAUSE; KILL_ALL      # P2-03 records which works under runsc

class Sandbox(Protocol):                             # work container (no network) + install containers
    id: str; runtime: ContainerRuntime; freeze_mechanism: FreezeMechanism; warnings: Sequence[str]
    is_frozen: bool                                  # Workspace refuses unless True; exec refuses while True
    async def exec(self, argv: Sequence[str], *, network: NetworkMode, timeout_s: int,
                   cwd: str = "/workspace", env: Mapping[str, str] | None = None) -> ExecResult: ...
        # NONE: work container. REGISTRY: a fresh install container on the run's internal network,
        # destroyed and verified gone before returning (A7). Disk polled (D6). Never raises for a
        # failing command.
    def frozen(self) -> AbstractAsyncContextManager[None]: ...
        # no install container exists; work container paused (or KILL_ALL); every Workspace call
        # happens inside this (§9.5)
    async def export_workspace(self, dest: Path) -> ExportManifest: ...   # never follows links, keeps .git
    async def destroy(self) -> None: ...                                  # idempotent, also after errors

class SandboxDriver(Protocol):
    def detect_runtime(self) -> RuntimeInfo: ...
    async def create(self, spec: SandboxSpec) -> Sandbox: ...             # §9.3 hardening
    async def restore(self, spec: SandboxSpec, manifest: ExportManifest, *,
                      expected_digest: str) -> Sandbox: ...   # digest from the DB checkpoint
    async def reap(self, run_id: RunId) -> int: ...  # remove a crashed run's containers and network
                                                     # before any Workspace access on resume (§10.1)

class Workspace(Protocol):                           # host side, A1: openat2 RESOLVE_BENEATH | NO_SYMLINKS
    root: Path                                       # | NO_MAGICLINKS + S_ISREG; PathEscapeError otherwise
    def read_text(self, rel: str, *, max_bytes: int) -> str: ...
    def read_blob(self, rel: str, *, max_bytes: int) -> FileBlob: ...     # links as links, for the mirror
    def write_text(self, rel: str, content: str, *, create: bool) -> FileDiff: ...  # create_file / str_replace
    def delete(self, rel: str) -> FileDiff: ...
    def list_dir(self, rel: str, depth: int) -> list[DirEntry]: ...
    def walk_tree(self, rules: ArtifactRules) -> TreeSnapshot: ...       # .git ignored, links never followed
    def replace_tree(self, files: Mapping[str, FileBlob]) -> TreeSnapshot: ...   # revert, crash recovery
    def iter_paths(self) -> Iterable[str]: ...

class Mirror(Protocol):                              # vcs/base.py; runs/<id>/mirror, never mounted
    path: Path; base_sha: str
    def artifact_rules(self, fixed_dirs: frozenset[str]) -> ArtifactRules: ...   # .gitignore@base_sha
    def snapshot(self, ref: str, ws: Workspace, rules: ArtifactRules) -> SnapshotInfo: ...
    def resolve(self, ref: str) -> str: ...
    def read_tree(self, ref: str) -> Mapping[str, FileBlob]: ...
    def tree_reader(self, commit: str) -> TreeReader: ...                # for the gate (§8)
    def diff(self, base: str, target: str) -> WorkspaceDiff: ...
    def build_phase_commits(self, refs: Sequence[str]) -> str: ...      # one commit per phase
```

Value types in `models/artifacts.py`: `FileBlob` (mode, data), `FileDiff` (path, kind, unified), `EntryType`, `DirEntry`, `ArtifactRules` (gitignore, fixed_dirs, tracked: tracked paths are never excluded), `ExportManifest` (export_dir, entries incl. `.git`; `digest()`).
Snapshot refs: `phase_ref(RunStatus.BUMPED | CODEMODS_APPLIED)` → `refs/mpilot/phase/<name>`, `attempt_ref(n)` → `refs/mpilot/attempt/<n>` with a run-wide `n`.
Test doubles: `FakeSandbox`, `FakeDriver`, `MemoryWorkspace`, `MemoryMirror`, `BlobTree` (`testing/infra_doubles.py`).

## 8. Gate checks (`policy/gate.py`, `policy/test_files.py`, `models/artifacts.py`)

```python
# models/artifacts.py: host-computed data, never from sandbox git (SPEC §9.5)
class TreeEntry(BaseModel, frozen=True): mode: Literal["100644", "100755", "120000"]; sha256: str
class TreeSnapshot(BaseModel, frozen=True):          # Workspace.walk_tree output or a commit's tree
    entries: dict[str, TreeEntry]                    # relative POSIX paths, never under .git
    def digest(self) -> str: ...
class ChangeKind(StrEnum): ADDED; MODIFIED; DELETED  # no renames: a renamed test is a deleted test
class Hunk(BaseModel, frozen=True): old_start; old_lines; new_start; new_lines; text
class FileChange(BaseModel, frozen=True): path; kind; old: TreeEntry | None; new: TreeEntry | None; hunks; binary
class WorkspaceDiff(BaseModel, frozen=True):         # between two host mirror commits
    base: str; target: str; target_digest: str       # target tree's TreeSnapshot.digest()
    files: list[FileChange]                          # sorted, unique
class TestCounts(BaseModel, frozen=True): passed; failed; skipped; errors          # runner log summary
class FinalVerification(BaseModel, frozen=True):     # fresh sandbox from mirror_commit (§9.5)
    mirror_commit: str; verify: VerifyResult; tests: TestResult       # tests.stage == final
    runner_exit_code: int | None; runner_summary: TestCounts | None   # (a) cross-check
    resolved_versions: dict[str, str]; coverage_pct: float | None     # (g), (f) extra run
    cache: Literal["fresh", "hash_verified"]
class ApiPattern(BaseModel, frozen=True): api; ecosystem; pattern (compiles); globs   # D3 curated only

# policy/gate.py
class TreeReader(Protocol):                          # read-only view of one mirror commit
    commit: str
    def snapshot(self) -> TreeSnapshot: ...          # (i) compares commit_tree.snapshot() with walked_tree
    def paths(self) -> frozenset[str]: ...
    def read_text(self, rel_path: str) -> str | None: ...   # None for a needed file => violation

@dataclass(frozen=True)
class GateInput:
    spec: MigrationSpec; baseline: Baseline; env: EnvProfile; plan: Plan
    base_sha: str; mirror_commit: str                # the run's base; the commit to push
    final: FinalVerification                         # the only results any check may use
    walked_tree: TreeSnapshot                        # (i) vs commit_tree.snapshot()
    diff: WorkspaceDiff                              # base_sha -> mirror_commit
    base_tree: TreeReader; commit_tree: TreeReader   # contents for (e), (h)
    classifier: TestFileClassifier
    allowed_files: frozenset[str]                    # §11.3 control 5
    diff_limits: DiffSettings                        # NFR-8
    step_diffs: Sequence[tuple[StepId, WorkspaceDiff]]   # plan order; test_touch hunk mapping (d)
    flag_justifications: Mapping[str, str]           # H8 (A11)
    removed_api_patterns: Sequence[ApiPattern]       # D3

class PolicyCheck(Protocol):
    id: str                                          # "integrity.a" ... "integrity.i", "diff_limits", ...
    def evaluate(self, inp: GateInput) -> list[Violation]: ...   # pure; empty list = pass

def binding_violations(inp: GateInput) -> list[Violation]: ...
    # "gate" violations: final run, commit_tree, diff target/digest not the pushed commit
    # (commit_mismatch); diff base or base_tree not base_sha (base_mismatch); plan of another
    # run (run_mismatch); step_diffs not one chain of plan steps, in plan order, from base_sha
    # to the pushed tree, or changes with no step diffs (step_chain)
def evaluate_gate(checks, inp, *, required) -> IntegrityReport: ...
    # binding_violations first, then every check; ValueError on no checks, empty `required`
    # (no default: the gate can't pass by running nothing), duplicate, reserved ("gate") or
    # missing check ids, or a violation under a foreign id

# policy/test_files.py (D2; implementation P2-15). Predicates are independent, not exclusive.
class TestFileClassifier(Protocol):
    def is_test_file(self, rel_path: str) -> bool: ...          # (c), (d), baseline hashes
    def is_test_config(self, rel_path: str) -> bool: ...        # (e) whole file
    def may_embed_test_config(self, rel_path: str) -> bool: ... # (e) sections: package.json, pyproject, ...
```

`Violation` is the §12 model in `models/domain.py`.
Test doubles: `SetClassifier`, `DictTree`, `ConstantCheck`.

## 9. VCS (`vcs/base.py`)

```python
BRANCH_PREFIX = "mpilot/"; WIP_BRANCH_PREFIX = "mpilot/wip-"; NEEDS_HUMAN_LABEL = "needs-human"
def is_mpilot_branch(name: str) -> bool: ...         # H1 local part: mpilot/<slug>, no nesting, no ..
class RepoRef(BaseModel, frozen=True): owner: str; name: str; slug -> str
class RepoInfo(BaseModel, frozen=True): default_branch: str; size_kb: int; license_spdx: str | None; private: bool
class BranchStatus(BaseModel, frozen=True):          # A9; pushable -> not default/protected/ruleset
    exists: bool; is_default: bool; protected: bool; ruleset_protected: bool
class PrInfo(BaseModel, frozen=True): number; url; head_branch; base_branch; draft; created_at
class BotPrInfo(BaseModel, frozen=True): number; url; head_branch; head_sha; base_branch; author_login; title
class SnapshotInfo(BaseModel, frozen=True): ref: str; commit: str; tree_digest: str

class CredentialProvider(Protocol):                  # A10; PAT now, App in Phase 8
    allowed_repos: frozenset[str]
    async def token_for(self, repo: RepoRef) -> SecretStr: ...   # CredentialRefusedError off the list

class VcsAdapter(Protocol):                          # no merge and no auto-merge method (H2, asserted)
    async def repo_info(self, repo: RepoRef) -> RepoInfo: ...
    async def branch_status(self, repo: RepoRef, branch: str) -> BranchStatus: ...
    async def push_branch(self, repo: RepoRef, mirror: Mirror, *, branch: str, commit: str,
                          verified: FinalVerification, report: IntegrityReport,
                          verdict: CriticVerdict) -> None: ...
        # the success push. PushRefusedError unless is_mpilot_branch(branch) (and not wip),
        # branch_status(...).pushable, commit == verified.mirror_commit, report.passed, the
        # Critic approved and all three from the same run, checked right before the push
    async def push_wip_branch(self, repo: RepoRef, mirror: Mirror, *, branch: str, commit: str) -> None: ...
        # FR-17 escalation branch: mpilot/wip-* only, H1 checks, a mirror commit; a PR on it only
        # with --pr-on-failure, always labelled needs-human
    async def upsert_draft_pr(self, repo: RepoRef, *, branch: str, base: str, title: str,
                              body: str, labels: Sequence[str]) -> PrInfo: ...   # idempotent, always draft;
        # head must be mpilot/*, and a mpilot/wip-* head needs the needs-human label (FR-17)
    async def bot_pr(self, repo: RepoRef, number: int) -> BotPrInfo: ...       # FR-19 metadata only
```

Test doubles: `AllowListCredentials`, `RecordingVcs`.

## 10. Orchestrator, state, store, budgets, events (`orchestrator/base.py`, `models/state.py`)

```python
class RunRequest(BaseModel, frozen=True):            # mpilot run / POST /runs, after config merge
    repo: str; goal: str | None; package: str | None; from_version: str | None; to_version: str | None
    replace: tuple[str, str] | None; ref: str | None    # branch, tag or pinned SHA (FR-2); validated so
                                                     # it can never read as a git option
    test_cmd: str | None; bot_pr_url: str | None
    approve_plan: bool; open_pr: bool; pr_on_failure: bool; system: str; budgets: Budgets

class RunEvent(StrEnum): ...                         # CLONED ... CANCELLED: the §7.4 event vocabulary

class TransitionTable(Protocol):                     # pure §7.4 table (P3-04)
    def next_state(self, state: RunStatus, event: RunEvent, *, paused_at: RunStatus | None = None) -> RunStatus: ...
    def events_from(self, state: RunStatus) -> frozenset[RunEvent]: ...   # IllegalTransition otherwise
    # pure over (state, event); `paused_at` only with RESUMED. Counted rules are the driver's, which
    # picks the event: second gate failure or second Critic revise -> ESCALATE; --no-pr -> FINISHED

class Orchestrator(Protocol):
    async def start(self, req: RunRequest) -> RunId: ...
    async def drive(self, run_id: RunId) -> RunStatus: ...     # until terminal, PAUSED_LIMIT or AWAITING_APPROVAL
    async def resume(self, run_id: RunId) -> RunStatus: ...    # B5 semantics
    async def decide_plan(self, run_id: RunId, *, approve: bool, reason: str | None) -> None: ...
    async def cancel(self, run_id: RunId) -> None: ...

class CallEstimate(BaseModel, frozen=True):          # B2: turn k costs prompt + (k-1)*tool + output
    prompt_tokens: int; tool_output_per_turn: int; output_ceiling: int; requested_turns: int
    def tokens_for(self, turns: int) -> int: ...
    def max_turns_within(self, token_room: int, turn_room: int) -> int: ...   # 0 = refuse

class BudgetManager(Protocol):                       # never downgrades: refuses, stops or ends
    def state(self) -> BudgetState: ...
    def precheck(self, call_id: LLMCallId, est: CallEstimate) -> int: ...   # clamped, bound to the call;
                                                               # nested calls (explore) kept apart
    def guard(self, call_id: LLMCallId) -> BudgetGuard: ...    # §4; KeyError if never prechecked
    def record_call(self, call_id: LLMCallId, usage: Usage, turns: int, api_equiv_usd: float) -> None: ...
    def start_attempt(self, step_id: StepId) -> int: ...      # run-wide n; AttemptCapError at 6/40
    def record_sandbox_seconds(self, seconds: float) -> None: ...
    def pause(self) -> None: ...; def resume(self) -> None: ...; def check_time(self) -> None: ...

AppendRow = ToolCall | LLMCall | RoutingDecision | TestResult | Attempt | IntegrityReport | CriticVerdict | MigrationNote
SingleRow = Baseline | Plan | PullRequest | EscalationReport

# models/artifacts.py (frozen, like the other artifacts)
class MigrationNote(ArtifactModel): run_id: RunId; key: str; text: str          # one `write_note` call, all kept
class EscalationReport(ArtifactModel):                                           # FR-17, one per run
    run_id: RunId; category: FailureCategory; report_md: str; data: dict[str, Any]; created_at: datetime

class RunStore(Protocol):                            # SQLite WAL (P1-10)
    def create_run(self, run: Run) -> None: ...; def save_run(self, run: Run) -> None: ...
    def get_run(self, run_id: RunId) -> Run: ...; def list_runs(self) -> Sequence[Run]: ...
    def append(self, row: AppendRow) -> None: ...; def put(self, row: SingleRow) -> None: ...
    def rows(self, run_id: RunId, kind: type[RowT]) -> Sequence[RowT]: ...
    def append_final_verification(self, run_id: RunId, result: FinalVerification) -> None: ...   # has no run_id itself
    def final_verifications(self, run_id: RunId) -> Sequence[FinalVerification]: ...             # oldest first
    def checkpoint(self, cp: Checkpoint) -> None: ...          # StoreError if cp.covers(previous) fails; check + write atomic
    def latest_checkpoint(self, run_id: RunId) -> Checkpoint | None: ...
    def add_eval(self, result: EvalResult) -> None: ...; def evals(self, suite: str) -> Sequence[EvalResult]: ...

class EventKind(StrEnum): STATE_CHANGED; ROUTING_DECISION; LLM_CALL; TOOL_CALL; VERIFY; CHECKPOINT; APPROVAL_REQUESTED; LOG
class RunEventRecord(BaseModel, frozen=True): run_id: RunId; seq: int; at: datetime; kind: EventKind; data: dict
class EventBus(Protocol):
    def publish(self, run_id: RunId, kind: EventKind, data: dict[str, Any]) -> RunEventRecord: ...   # assigns seq
    def subscribe(self, run_id: RunId, *, after_seq: int = 0) -> AsyncIterator[RunEventRecord]: ...  # replay + live

# models/state.py
def sticky_scope(step_id: StepId | None, state: RunStatus) -> str: ...   # "step/<id>" or "phase/<STATE>"
class FailureHistoryEntry(BaseModel, frozen=True): signature: str; attempts: int; hypotheses_tried; outcome
class InFlightCall(BaseModel, frozen=True): role_call_id; role; step_id; tool_calls_done; summary   # B5 restart
class BudgetState(BaseModel, frozen=True):
    spent: Spent; active_seconds: float; attempts_by_step: dict[StepId, int]; attempts_total: int
    in_flight_turns: int; in_flight_tokens: int      # the running call's guard charges (crash-safe)
class Checkpoint(BaseModel, frozen=True):            # after every tool call (NFR-4)
    run_id: RunId; seq: int; at: datetime; status: RunStatus; paused_at_state: RunStatus | None
    step_id: StepId | None; attempt_n: int | None
    sticky: dict[str, StickyState]                   # sticky_scope -> state
    budget: BudgetState; failure_history: tuple[FailureHistoryEntry, ...]
    last_tool_call_id: ToolCallId | None; in_flight: InFlightCall | None
    latest_snapshot_ref: str | None
    export_dir: str | None; export_digest: str | None   # both required when paused
    integrity_failures: int; critic_revisions: int   # §7.4 "once"; FR-15 at most one revise
    def covers(self, previous: Checkpoint) -> list[str]: ...     # nothing may go backwards (B5): sticky
        # flags, attempt counters, spent, active time, failure-history attempts and tried hypotheses
```

### Tables (`models/tables.py`, P1-09)

Each table keeps the **whole Pydantic model as JSON in `doc`** (reads return it, so a round trip is
exact) plus plain columns copied from the model for what is queried, joined, ordered or
constrained. The columns are derived: the store writes both from one model object. A test checks
that every column is a field of its model (or listed as derived). Nested §12 classes live inside
their parent's `doc`.

| Table | Stores | Kind |
|---|---|---|
| `runs` | `Run` (+ nested `MigrationSpec`, `EnvProfile`, budgets, spent) | upserted |
| `baselines`, `plans`, `pull_requests`, `escalation_reports` | `Baseline`, `Plan` (+ nested `Step`), `PullRequest`, `EscalationReport` (FR-17; every field is a column; P5-14 fixes the shape of `data`) | one per run, upserted |
| `checkpoints` | `Checkpoint`, PK `(run_id, seq)` | append-only |
| `attempts` | `Attempt` (+ nested `Diagnosis`; `signature`, `category` copied out) | append-only |
| `tool_calls`, `routing_decisions`, `llm_calls` | `ToolCall`, `RoutingDecision`, `LLMCall` (FK to its decision) | append-only |
| `test_results`, `final_verifications`, `integrity_reports`, `critic_verdicts` | `TestResult`, `FinalVerification`, `IntegrityReport`, `CriticVerdict` | append-only |
| `migration_notes` | `MigrationNote`, one row per `write_note` call; the current note for a key is its highest `seq` | append-only |
| `eval_results` | `EvalResult` (bench rows, no run FK) | append-only |

Append-only tables have `seq` (insertion order) and `recorded_at` (the store's `Clock`, UTC). Child
tables cascade from `runs`. NFR-12 is repeated as CHECK constraints (tier opus/sonnet, effort
medium/high, never Opus at medium, no "haiku" in a model name), and so are the model invariants
that fit in SQL (deny reason iff denied, paused state iff `PAUSED_LIMIT`, non-negative counts).
Types are portable: `JSON` (JSONB on Postgres), timezone-aware UTC datetimes through `UtcDateTime`,
named constraints. SQLite needs `enable_sqlite_fks(engine)` for foreign keys; P1-10 calls it.

`models/migrate.py` runs the packaged Alembic migrations from code (`upgrade`, `downgrade`, the
same on an open connection, and `upgrade_sql`/`downgrade_sql` that print the SQL for any dialect
without a server). `0001_initial_schema` is generated, then frozen (it imports no app code); a
test compares the migrated schema to `tables.py`, so changing a table without a new revision
fails the build.

### The SQLite store (`models/store.py`, P1-10)

`SqliteRunStore.open(path, clock)` creates the file, migrates it to the latest Alembic revision
and returns the `RunStore`. It takes any engine in its constructor; only `sqlite_engine` is
SQLite-specific.

- **WAL**, `synchronous=NORMAL`, foreign keys on, a 30 s busy timeout (a second writer waits).
- **Transactions are explicit.** Writes begin `BEGIN IMMEDIATE`, reads a plain `BEGIN` (never
  blocked by a writer in WAL). A checkpoint reads its predecessor and writes inside one write
  transaction, so two writers cannot both pass `covers()` against the same predecessor.
- **Errors:** `StoreError` (a `ValueError`) for any refused write: duplicate id, a row for an
  unknown run, a CHECK or FK violation, a checkpoint that undoes something. `KeyError` for an
  unknown run on `get_run`/`save_run`. `TypeError` for a record type the store doesn't hold.
- A database whose revision is newer than the code is refused at open (Alembic error), never
  silently used.
- `MemoryStore` (the double) runs the same contract tests as the real store.

`Clock` is `core/clock.py` (P1-02).
Test doubles: `DictTable`, `ScriptedOrchestrator`, `SimpleBudget`, `MemoryStore`, `MemoryBus`.

## 11. Verification and knowledge (`verify/base.py`, `knowledge/base.py`)

```python
class Verifier(Protocol):
    async def baseline(self, sb: Sandbox, ws: Workspace, env: EnvProfile, classifier: TestFileClassifier,
                       *, run_id: RunId, runs: int = 3) -> Baseline: ...      # FR-4, D4, D9
    async def verify(self, sb: Sandbox, ws: Workspace, env: EnvProfile, *, run_id: RunId, attempt_id: AttemptId | None,
                     manifests_changed: bool, selector: Sequence[str] | None, full: bool) -> VerifyResult: ...
    async def final_verify(self, driver: SandboxDriver, mirror: Mirror, env: EnvProfile, *,
                           run_id: RunId, commit: str, cache: CacheMode) -> FinalVerification: ...
        # §9.5: fresh workspace from `commit`, fresh sandbox, FRESH or HASH_VERIFIED cache (RUN refused),
        # whole pipeline uninstrumented, then one instrumented coverage run. Success, L0-L2 and every
        # gate check judge only this.

# knowledge/base.py: DRAFTS (Phases 2-5 may extend; record changes here)
class DetectOverrides(BaseModel, frozen=True): install_cmd; build_cmd; typecheck_cmd; test_cmd; runtime_version
class EnvDetector(Protocol):
    def detect(self, tree: TreeReader, overrides: DetectOverrides) -> EnvProfile: ...   # reads the mirror
class RegistryInfo(BaseModel, frozen=True): package; ecosystem; versions; latest_stable; yanked; deprecated
                                            license_spdx; published_at; peer_deps
class RegistryClient(Protocol):
    async def lookup(self, package: str, ecosystem: Ecosystem) -> RegistryInfo: ...
class ChangelogChunk(BaseModel, frozen=True): text; source_url; version
class CorpusStats(BaseModel, frozen=True): documents; chunks
class ChangelogIndex(Protocol):
    async def build(self, package: str, ecosystem: Ecosystem, from_v: str, to_v: str) -> CorpusStats: ...
    def search(self, query: str, package: str | None, top_k: int) -> list[ChangelogChunk]: ...   # top_k <= 8
class RepoMapper(Protocol):
    def render(self, ws: Workspace, personalize: Sequence[str], budget_tokens: int) -> str: ...
class UsageSite(BaseModel, frozen=True): path; line; symbol; snippet
class UsageIndex(BaseModel, frozen=True): package; sites
class UsageIndexer(Protocol):
    def index(self, ws: Workspace, package: str, ecosystem: Ecosystem) -> UsageIndex: ...
class CodemodEntry(BaseModel, frozen=True): id; package; ecosystem; from_major; to_major; command; removed_apis
class CodemodRegistry(Protocol):
    def lookup(self, package: str, from_major: int, to_major: int) -> list[CodemodEntry]: ...
```

Still to come as plain functions: `trim_log` and `failure_signature` (P3-12, FR-12, §10.3).
Test doubles: `StaticVerifier`, `StaticDetector`, `StaticRegistry`, `ListChangelog`, `FixedMapper`, `StaticUsageIndexer`, `ListCodemods`.

## 12. Facts to record here at build time (SPEC Appendix A)

These are filled in by P4-01 and later tasks, with the date and the source:
- the SDK's option names
- the per-call effort mechanism
- usage reporting
- the error types
- the subscription policy
- the model IDs
- the price table
- GitHub permission names
- codemod flags
