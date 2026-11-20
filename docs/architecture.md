# Architecture

Status: **DRAFT interface signatures from P0-1.**
P1-07 and P1-08 turn these into real Python Protocols with docstrings, and P1-12 freezes them once `spec-reviewer` approves.
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
  models/      domain.py (Pydantic, §12), tables.py (SQLAlchemy), store.py (RunStore impl)
  config.py    settings + NFR-12 validation + config_hash
  orchestrator/ states.py (pure transition table), machine.py, budget.py, checkpoint.py, loop.py
  agents/      runtime.py, sdk_runtime.py, messages_runtime.py, fake_runtime.py, cassette.py,
               router.py, roles/<role>.py, prompts/<role>.md, schemas.py
  tools/       base.py (Tool, ToolHost), files.py, search.py, exec.py, knowledge.py, mcp_server.py
  policy/      hooks.py (tool policies), integrity.py, diff_limits.py, secrets.py, licenses.py,
               allowed_files.py, branch.py, test_files.py (D2 classifier)
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
class ModelChoice(BaseModel, frozen=True):
    tier: Tier; effort: Effort; model_id: str          # model_id resolved from validated config

class RoleCallRequest(BaseModel, frozen=True):
    run_id: RunId; step_id: StepId | None; role: Role
    choice: ModelChoice; routing_decision_id: str
    system_prompt: str                 # stable prefix: rules, H6, tool docs, repo map (SPEC §10.3)
    user_prompt: str                   # untrusted content already wrapped in <untrusted_repo_content>
    tool_allowlist: frozenset[str]     # bare tool names; runtime exposes them as mcp__mpilot__<name>
    output_schema: dict[str, Any]      # JSON schema; must include `confidence: number[0,1]`
    max_turns: int                     # already clamped by BudgetManager (B2)
    max_tool_calls: int                # §8.3 Editor ≤ 15, §8.5 Explorer ≤ 20; enforced in ToolHost
    budget_guard: BudgetGuard          # checked before every tool call; can end the call (B2)

class RoleCallResult(BaseModel, frozen=True):
    output: dict[str, Any] | None      # schema-validated, or None if invalid
    schema_valid: bool
    usage: Usage; turns: int; latency_ms: int
    tool_call_ids: list[str]
    models_seen: frozenset[str]        # every model the runtime reports using (A4 guard)
    stop: Literal["completed", "max_turns", "schema_invalid", "budget_stop", "interrupted"]
    thinking_enabled: bool             # §8.3: must be True; the orchestrator rejects False

class RuntimeCapabilities(BaseModel, frozen=True):
    per_call_effort: bool; prompt_caching: bool; context_editing: bool   # B1

class AgentRuntime(Protocol):
    name: str
    capabilities: RuntimeCapabilities
    async def call(self, req: RoleCallRequest, tools: ToolHost) -> RoleCallResult:
        """Run one role call with extended thinking on, model and max_turns from req.
        Records usage incl. cache read/write. Maps SDK usage-limit and rate-limit errors to
        UsageLimitError (FR-21). Raises ForbiddenModelError (A4), LockdownViolation (H9),
        RuntimeCallError."""
    def check_lockdown_static(self) -> None:
        """Run at every start: validate SDK options and env against A5. Raises LockdownViolation."""
    async def selftest(self) -> SelftestReport:
        """Prove the §8.1 lockdown: built-in tools refused, no settings loaded, clean env."""
```

Implementations: `AgentSDKRuntime` (default), `MessagesRuntime` (optional), `FakeRuntime` (scripted), `CassetteRuntime` (replay, B6).

## 5. Model Router (`agents/router.py`)

```python
class StickyState(BaseModel, frozen=True):
    tier_escalated: bool = False; effort_escalated: bool = False        # per step; never reset mid-step

class RoutingContext(BaseModel, frozen=True):
    role: Role; caller: Role | None                 # caller set for Explorer calls (Critic ⇒ high)
    step_type: StepType | None; touches_test_config: bool
    migration_kind: MigrationKind; usage_sites: int | None
    attempt_in_step: int; attempts_on_signature: int
    prev_outcome: Outcome | None; failure_category: FailureCategory | None
    expected_files: int | None; expected_lines: int | None
    has_changelog_mapping: bool          # Editor: explicit before/after mapping exists
    error_maps_to_changelog: bool        # Diagnoser: the error maps to a changelog entry (B9)
    is_codemod_followup: bool
    last_confidence: float | None; diagnosis_confidence: float | None
    policy_denials_in_step: int; schema_invalid_in_step: bool
    critic_verdict: Literal["approve", "revise", "escalate"] | None
    explorer_flags: frozenset[Literal["no_citations", "over_cap", "repeated_question"]]
    pr_template_rejected: bool
    sticky: StickyState

class RoutingPolicy(BaseModel, frozen=True):
    policy: Literal["routed", "all_opus", "all_sonnet"]
    effort_policy: Literal["routed", "all_high"]           # all_opus ⇒ all_high (validated)

class RouteOutcome(BaseModel, frozen=True):          # B9: pure result, no IDs
    tier: Tier; effort: Effort; reason_codes: tuple[str, ...]
    escalated_from: Tier | None; effort_escalated_from: Effort | None
    sticky: bool; new_sticky: StickyState; inputs_hash: str

class ModelRouter(Protocol):
    def route(self, ctx: RoutingContext, policy: RoutingPolicy) -> RouteOutcome:
        """Pure and deterministic. The orchestrator wraps the outcome into a §12 RoutingDecision.
        Invariants (property-tested): never Haiku; effort ∈ {medium, high}; Opus ⇒ high;
        Planner/Critic/Diagnoser/Analyzer/test_touch ⇒ high; any trigger fired ⇒ high; sticky
        (never de-escalates); B2/B3 ⇒ opus/high under every policy; Explorer and PR Writer stay
        sonnet under `routed` (only their effort escalates) (B8). Sticky scope: per step, or per
        run phase for step-less calls."""
```

The orchestrator stores `StickyState` per step and passes it back in each call. That's how stickiness works while `route` stays pure.

## 6. Tools and tool policy (`tools/base.py`, `policy/hooks.py`)

```python
class ToolSpec(BaseModel, frozen=True):
    name: str; description: str
    input_model: type[BaseModel]; output_model: type[BaseModel]
    effect: Literal["read", "write", "exec", "network"]

class ToolContext(BaseModel, frozen=True):
    run_id: RunId; step_id: StepId | None; attempt_id: str | None; role: Role
    step_type: StepType | None; plan_test_touch_files: frozenset[str]

class Tool(Protocol):
    spec: ClassVar[ToolSpec]
    async def run(self, args: BaseModel, ctx: ToolContext, env: ToolEnv) -> BaseModel: ...

class ToolEnv(Protocol):            # what tools may touch; built by the orchestrator per run
    workspace: Workspace; sandbox: Sandbox; knowledge: KnowledgeService; notes: NotesStore

class PolicyDecision(BaseModel, frozen=True):
    allow: bool; code: str | None; reason: str | None

class ToolPolicy(Protocol):
    def pre(self, tool: str, args: BaseModel, ctx: ToolContext) -> PolicyDecision: ...
    def post(self, tool: str, output: BaseModel, ctx: ToolContext) -> BaseModel: ...  # redaction (A3)

class ToolHost(Protocol):
    """The only path from a model to a tool. Enforces role allowlist + pre policies,
    runs the tool, applies post policies, persists a ToolCall row, checkpoints (NFR-4)."""
    def specs_for(self, role: Role) -> list[ToolSpec]: ...
    async def invoke(self, name: str, raw_args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]: ...

# Tool registry = exactly the §9.1 tools + `explore` (§8.5). A test asserts the set: no GitHub
# tools, no commit/push/PR tools (§11.3 control 2, §9.1). `explore` is a ToolSpec whose run()
# asks the orchestrator for an Explorer role call (orchestrator-mediated, §8.5).
```

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
```

## 8. Gate checks (`policy/`)

```python
class GateInput(BaseModel, frozen=True):
    baseline: Baseline                                       # incl. P0 ids, flaky ids, test-file hashes
    final: FinalVerification                                 # whole pipeline from mirror_commit (SPEC §9.5); all checks judge this
    diff: WorkspaceDiff                                      # host-computed: walk_tree vs mirror@base_sha (A2)
    tree: TreeSnapshot; mirror_commit: str                   # mirror tree must equal walked tree
    plan: Plan
    env: EnvProfile                                          # resolved versions and coverage come from `final` (D4: lost coverage ⇒ violation)
    flag_justifications: dict[str, str]                      # H8 (A11): flag → plan justification
    removed_api_patterns: list[ApiPattern]                   # D3: curated registry list only

class Violation(BaseModel, frozen=True):
    check_id: str; code: str; message: str; path: str | None; hunk: str | None

class PolicyCheck(Protocol):
    id: str                                                  # "integrity.a" ... "integrity.h", "diff_limits", ...
    def evaluate(self, inp: GateInput) -> list[Violation]: ...   # pure; empty list = pass

class TestFileClassifier(Protocol):                          # D2
    def is_test_file(self, rel_path: str) -> bool: ...
    def is_test_config(self, rel_path: str) -> bool: ...
```

class Mirror(Protocol):                                      # host-owned clone, never mounted (SPEC §9.5)
    path: Path; base_sha: str
    def record_snapshot(self, ref: str, tree: TreeSnapshot, ws: Workspace) -> None: ...  # refs/mpilot/phase/<n>, attempt/<n>
    def build_phase_commits(self, refs: Sequence[str]) -> str: ...   # diffs between consecutive snapshots; returns head sha
    def tree_equals(self, sha: str, tree: TreeSnapshot) -> bool: ...  # gate check (i)

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
