# Architecture

Status: §4–§6 and §8 are **real code since P1-07** (signatures below are summaries; the docstrings in the named modules are authoritative). The rest are **DRAFT signatures from P0-1** until P1-08.
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
  models/      domain.py (Pydantic, §12), artifacts.py (trees, diffs, FinalVerification, ApiPattern),
               tables.py (SQLAlchemy), store.py (RunStore impl)
  config.py    settings + NFR-12 validation + config_hash
  orchestrator/ states.py (pure transition table), machine.py, budget.py, checkpoint.py, loop.py
  agents/      runtime.py, sdk_runtime.py, messages_runtime.py, fake_runtime.py, cassette.py,
               router.py, roles/<role>.py, prompts/<role>.md, schemas.py
  tools/       base.py (Tool, ToolHost), files.py, search.py, exec.py, knowledge.py, mcp_server.py
  policy/      hooks.py (tool policies), gate.py (GateInput, PolicyCheck, evaluate_gate),
               integrity.py, diff_limits.py, secrets.py, licenses.py,
               allowed_files.py, branch.py, test_files.py (D2 classifier)
  testing/     policy.py (E1 marker plugin), doubles.py (minimal Protocol doubles, mypy-checked)
  sandbox/     driver.py (Docker), workspace.py (host-side safe FS, A1), network.py, proxy/, e2b.py
  detect/  verify/  knowledge/  codemods/registry.yaml  vcs/  telemetry/  api/  web/
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
    # SPEC §8.4 "Pins": a pin replaces the default tier/effort only; triggers, effort
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

## 7. Sandbox and workspace (`sandbox/`)

```python
class ResourceLimits(BaseModel, frozen=True):
    cpus: float = 4; memory_gb: int = 8; disk_gb: int = 20; pids: int = 1024   # D6, D7

class SandboxSpec(BaseModel, frozen=True):
    image_digest: str; limits: ResourceLimits; workspace_host_path: Path
    env: Mapping[str, str]             # allowlisted keys only; validated
    from_commit: str | None = None     # build /workspace from this mirror commit (final verification)
    final_cache: Literal["fresh", "hash_verified"] | None = None   # set only for final verification
    # The driver mounts caches read-only in the work container and read-write in install containers (SPEC §9.3).
    prefer_runtime: Literal["runsc", "runc"] = "runsc"

class ExecResult(BaseModel, frozen=True):
    exit_code: int; stdout_tail: str; stderr_tail: str; duration_s: float
    truncated: bool; timed_out: bool

class Sandbox(Protocol):
    id: str; runtime: Literal["runsc", "runc"]; warnings: list[str]
    async def exec(self, argv: Sequence[str], *, network: NetworkMode, timeout_s: int,
                   cwd: str = "/workspace", env: Mapping[str, str] | None = None) -> ExecResult:
        """Run one command. NONE runs in the work container; REGISTRY runs in a short-lived
        install container on the per-run internal network (A7). `env` is checked against the
        same allowlist as SandboxSpec.env. Disk use is polled during the command; over the
        limit kills it (D6). The work container is paused (frozen) whenever the host touches
        the workspace (A1)."""
    def frozen(self) -> AbstractContextManager[None]: ...   # docker pause/unpause around host file ops
    async def export_workspace(self, dest: Path) -> ExportManifest: ...
    async def destroy(self) -> None: ...

class SandboxDriver(Protocol):
    def detect_runtime(self) -> RuntimeInfo: ...             # runsc present and working? else runc + warning
    async def create(self, spec: SandboxSpec) -> Sandbox: ...
    async def restore(self, spec: SandboxSpec, manifest: ExportManifest) -> Sandbox: ...

class Workspace(Protocol):                                   # host side, A1: openat2 RESOLVE_BENEATH
    root: Path                                               # | NO_SYMLINKS | NO_MAGICLINKS + S_ISREG
    def resolve(self, rel: str) -> Path: ...                 # raises PathEscapeError
    def read_text(self, rel: str) -> str: ...
    def write_text(self, rel: str, content: str) -> FileDiff: ...
    def delete(self, rel: str) -> FileDiff: ...
    def list_dir(self, rel: str, depth: int) -> list[DirEntry]: ...
    def walk_tree(self, exclude: ArtifactRules) -> TreeSnapshot: ...   # path → (mode, sha256); ignores .git, never follows links;
                                                             # exclusions from mirror .gitignore@base_sha + fixed list only
    def restore(self, snapshot_ref: str, mirror: Path) -> None: ...  # write tree from refs/mpilot/attempt/<n> (revert, crash)

class Mirror(Protocol):                                      # host-owned clone, never mounted (SPEC §9.5)
    path: Path; base_sha: str
    def record_snapshot(self, ref: str, tree: TreeSnapshot, ws: Workspace) -> None: ...  # refs/mpilot/phase/<n>, attempt/<n>
    def build_phase_commits(self, refs: Sequence[str]) -> str: ...   # diffs between consecutive snapshots; returns head sha
    def tree_equals(self, sha: str, tree: TreeSnapshot) -> bool: ...  # gate check (i)
```

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

## 9. VCS (`vcs/`)

```python
class CredentialProvider(Protocol):                          # A10; PAT now, App in Phase 8
    async def token_for(self, repo: RepoRef) -> SecretStr: ...  # refuses repos off the configured list;
                                                                # App tokens minted for exactly one repo

class VcsAdapter(Protocol):
    async def repo_info(self, repo: RepoRef) -> RepoInfo: ...          # default_branch, size_kb, license
    async def is_branch_protected(self, repo: RepoRef, branch: str) -> bool: ...   # A9
    async def push_branch(self, repo: RepoRef, mirror: Path, branch: str) -> None:
        """Refuses unless branch matches mpilot/* and is neither default nor protected (H1)."""
    async def upsert_draft_pr(self, repo: RepoRef, *, branch: str, base: str, title: str,
                              body: str, labels: Sequence[str]) -> PullRequest: ...   # idempotent (FR-16)
    async def bot_pr(self, repo: RepoRef, number: int) -> BotPrInfo: ...   # FR-19, metadata only
```

The adapter has no merge method and no auto-merge method (H2).

## 10. Orchestrator, store, budgets, events

```python
class Orchestrator(Protocol):
    async def start(self, req: RunRequest) -> RunId: ...
    async def drive(self, run_id: RunId) -> RunStatus: ...     # until terminal, PAUSED_LIMIT or AWAITING_APPROVAL
    async def resume(self, run_id: RunId) -> RunStatus: ...    # B5 semantics
    async def decide_plan(self, run_id: RunId, approve: bool, reason: str | None) -> None: ...
    async def cancel(self, run_id: RunId) -> None: ...

def next_state(state: RunStatus, event: RunEvent) -> RunStatus: ...   # pure table; illegal ⇒ IllegalTransition

class BudgetManager(Protocol):
    def precheck(self, est: CallEstimate) -> int: ...          # Σ-over-turns estimate (B2); returns clamped max_turns
    def guard(self) -> BudgetGuard: ...                        # per-tool-call check that can end a running call
    def record_attempt(self, step_id: StepId) -> None: ...     # NFR-3: raises at 6/step or 40/run
    def record(self, usage: Usage, turns: int) -> None: ...
    def check_time(self) -> None: ...                          # wall clock minus PAUSED_LIMIT time
    def snapshot(self) -> Spent: ...

class RunStore(Protocol):
    def create_run(self, run: Run) -> None: ...
    def get_run(self, run_id: RunId) -> Run: ...
    def set_status(self, run_id: RunId, status: RunStatus, *, reason: str | None = None) -> None: ...
    def append(self, row: ToolCall | LLMCall | RoutingDecision | TestResult | Attempt) -> None: ...
    def checkpoint(self, cp: Checkpoint) -> None: ...          # after every tool call (NFR-4)
    def latest_checkpoint(self, run_id: RunId) -> Checkpoint | None: ...

class EventBus(Protocol):                                     # feeds `mpilot logs --follow` and SSE
    def publish(self, ev: RunEventRecord) -> None: ...
    def subscribe(self, run_id: RunId) -> AsyncIterator[RunEventRecord]: ...

class Clock(Protocol):
    def now(self) -> datetime: ...; def monotonic(self) -> float: ...
```

## 11. Knowledge and verification (Phase 2–5, drafts)

```python
class EnvDetector(Protocol):
    def detect(self, repo: Path, overrides: DetectOverrides) -> EnvProfile: ...   # precedence SPEC §9.4

class Verifier(Protocol):
    async def verify(self, sb: Sandbox, env: EnvProfile, *, manifests_changed: bool,
                     selector: Sequence[str] | None, full: bool) -> VerifyResult: ...
    async def final_verify(self, driver: SandboxDriver, env: EnvProfile, mirror_commit: str) -> FinalVerification: ...
        # SPEC §9.5: fresh sandbox from the mirror commit, fresh or hash-verified cache; install → build →
        # typecheck → full suite, then one extra instrumented coverage run. Success, L0–L2 and every gate
        # check judge only this.

class FinalVerification(BaseModel, frozen=True):
    mirror_commit: str; verify: VerifyResult; tests: TestResult
    resolved_versions: dict[str, str]; coverage_pct: float | None; cache: Literal["fresh", "hash_verified"]

def trim_log(raw: str, *, max_lines: int = 60) -> str: ...  # §10.3: strip ANSI/progress bars, head/tail, dedupe

class RegistryClient(Protocol):
    async def lookup(self, package: str, ecosystem: Ecosystem) -> RegistryInfo: ...
class ChangelogIndex(Protocol):
    async def build(self, package: str, ecosystem: Ecosystem, from_v: str, to_v: str) -> CorpusStats: ...
    def search(self, query: str, package: str | None, top_k: int) -> list[ChangelogChunk]: ...
class RepoMapper(Protocol):
    def render(self, ws: Workspace, personalize: Sequence[str], budget_tokens: int) -> str: ...
class UsageIndexer(Protocol):
    def index(self, ws: Workspace, package: str, ecosystem: Ecosystem) -> UsageIndex: ...
class CodemodRegistry(Protocol):
    def lookup(self, package: str, from_major: int, to_major: int) -> list[CodemodEntry]: ...

def failure_signature(stage: str, log_excerpt: str) -> str: ...   # FR-12; normalizes paths, lines, addresses
```

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
