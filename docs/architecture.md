# Architecture

Status: **frozen at the Phase 1 gate (P1-12d, 2026-11-20).** §4–§11 are real code (P1-07: §4–§6, §8; P1-08: §7, §9–§11; P1-12a–d: the gate fixes). Signatures below are summaries; the docstrings in the named modules are authoritative.
Changing a frozen interface is a spec change (SPEC §18 rule 5): record it in `PROGRESS.md` (and in SPEC §20 when it touches a [REC] decision or §12), update this file in the same change, and have `spec-reviewer` check it.
Still **drafts**, which later phases may change by recording it here: §11's knowledge interfaces (Phases 2–5), `RunStore.add_eval`/`evals` and `EvalResult` (P9-01), and the `run_events` table behind `EventBus` (P3-05). Migration `0001` is frozen too: schema changes are new revisions.
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
class StopReason(StrEnum): COMPLETED; SCHEMA_INVALID; MAX_TURNS; BUDGET_STOP; TOOL_CALL_CAP
    # §12 LLMCall.stop. Usage/rate limit raises UsageLimitError instead (row: limit_error, stop None);
    # cancellation is asyncio.CancelledError. Lives here so models/domain.py can use it
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
        """Thinking always on; exposes only req.tool_allowlist. Per turn, in this order:
        when the response arrives, guard.after_turn(usage_or_None) FIRST (a crossed cap ->
        BUDGET_STOP, none of the turn's tool calls run; overshoot <= one turn); then per tool
        call: guard.before_tool_call() -> tools.invoke(name_as_sent, args, req.context) ->
        for_model(); after a TOOL_CALL_CAP refusal the next tool call ends the call
        (TOOL_CALL_CAP). A turn without tool calls is the final answer (COMPLETED or
        SCHEMA_INVALID); none by req.max_turns -> MAX_TURNS. Raises UsageLimitError,
        ForbiddenModelError, LockdownViolation, RuntimeCallError."""
    async def selftest(self) -> SelftestReport: ...
```

Implementations: `AgentSDKRuntime` (default), `MessagesRuntime` (optional), `FakeRuntime` (scripted), `CassetteRuntime` (replay, B6).
Test double: `testing.doubles.EchoRuntime`.

Helpers in `agents/runtime.py` every runtime shares:

```python
def schema_errors(schema: dict, answer: dict) -> list[str]: ...    # JSON Schema 2020-12; non-empty => SCHEMA_INVALID
def llm_call_from(req: RoleCallRequest, res: RoleCallResult, *, runtime: str,
                  redact: Callable[[str], str], api_equiv_usd: float = 0.0) -> LLMCall: ...
    # step_id/attempt_id/parent_call_id from req.context; stop, turns, and the final answer as
    # output: redact() runs on its canonical JSON, and the result must still fit output_schema
    # (else ValueError, nothing recorded). The next role is always given this stored output.
RoleCallRequest.content() -> dict          # request_hash() hashes this; part_hashes() hashes each key, for cassette diffs
```

### Scripted runtime and cassettes (`agents/fake_runtime.py`, `agents/cassette.py`, P1-11)

`play()` is the one engine both use: it runs a `ScriptedCall` as the model's side of a role call, through the
real `ToolHost` and `BudgetGuard`. Per model turn: the turn's usage is charged (`after_turn`; a crossed cap ends
the call BUDGET_STOP before any of its tools run); each tool call is guard-checked then `invoke`d; once the host
refuses one with TOOL_CALL_CAP the next ends the call TOOL_CALL_CAP; a `final` answer is validated against the
request's schema (COMPLETED or SCHEMA_INVALID). After the last turn, `raises` fires (usage limit, runtime error).

```python
class ScriptedToolCall(BaseModel, frozen=True):   # name is exactly what the model sends: "Bash" is a valid script
    name: str; args: dict; expect_decision: PolicyDecision | None; expect_deny_code: str | None; expect_output: dict | None
class ScriptedTurn(BaseModel, frozen=True):       # tool calls XOR a final answer
    tool_calls: tuple[ScriptedToolCall, ...]
    final: dict | str | None                      # str = raw text: SCHEMA_INVALID unless it parses to a fitting object
    usage: Usage | None                           # None = no per-turn usage: guard.after_turn(None)
class ScriptedCall(BaseModel, frozen=True):
    turns: tuple[ScriptedTurn, ...]; raises: RaiseKind | None; raise_message: str; expect_role: Role | None
    model_id: str | None; models_seen_extra: tuple[str, ...]; thinking_enabled: bool   # overrides, to build results
    effort_applied: Effort | None; latency_ms: int                                    # check_result must reject
    total_usage: Usage | None                     # the call's usage as finally reported; None = sum of the turns
    @classmethod def answer(output, **kw); @classmethod def using(*tool_calls, then=output, **kw)
class RaiseKind(StrEnum): USAGE_LIMIT; RUNTIME_ERROR
def tool(name: str, **args) -> ScriptedToolCall: ...      # exposes it as mcp__mpilot__<name>
@dataclass(frozen=True)
class PlayedCall: request; result | None; invocations; error | None
async def play(script, req, tools, guard, invocations, *, strict_outputs: bool) -> RoleCallResult: ...

@dataclass
class FakeRuntime:       # script: Sequence[ScriptedCall | Callable[[RoleCallRequest], ScriptedCall]], used in order
    calls: list[PlayedCall]; remaining: int; def assert_exhausted(); lockdown_error: str | None; strict_outputs=True
    # no step left, a script that just stops, or a failed expectation => ScriptDivergedError (loud, never improvised)
```

A cassette (decision B6) is JSON lines: `{"mpilot_cassette": 1}`, then one `CassetteEntry` per recorded role call,
keyed by `request_hash` (content only; IDs excluded). Prompts are never stored, only their per-part hashes;
tool arguments, outputs and answers are, so `write_cassette` requires a `redact` function (the gitleaks scan, P4-22).

```python
class CassetteEntry(BaseModel, frozen=True): request_hash: str; parts: dict[str, str]; role: Role; call: ScriptedCall
def write_cassette(path, entries, *, redact: Callable[[str], str]) -> None: ...   # atomic, deterministic bytes
def read_cassette(path) -> list[CassetteEntry]: ...                              # ValueError names file:line
@dataclass
class CassetteRuntime:   # replay: name "cassette"; same-hash entries used oldest first, once each
    entries; strict_outputs=False; calls; def unused(); def assert_all_used(); @classmethod from_file(path)
    # no matching entry => CassetteMismatchError naming the changed parts ("differs in: user_prompt, tool_allowlist");
    # the recorded tool calls run against the LIVE host, whose allow/deny must equal the recording
    # (else ScriptDivergedError); outputs are compared only when strict_outputs
```

## 5. Model Router (`agents/router.py`)

```python
class StickyState(BaseModel, frozen=True):
    tier_escalated: bool = False; effort_escalated: bool = False
    trigger_fired: bool = False                      # any trigger 1-8 in scope => no medium afterwards
    def covers(self, other: StickyState) -> bool: ...   # no de-escalation; any -> bool

class RoutingCounters(BaseModel, frozen=True):      # per StickyScope in Checkpoint.routing (FR-21)
    policy_denials: int; schema_invalid: int         # trigger 3 inputs; only grow in scope
    last_confidence: Confidence | None; last_confidence_call: LLMCallId | None   # trigger 5; set together
    confidence_calls: frozenset[str]                 # every call folded in: a change of call must be new
    explorer_questions: frozenset[str]               # explorer_question_hash of every question asked
    explorer_flags: frozenset[ExplorerFlag]; pr_template_rejected: bool   # effort-escalation inputs
    def covers(self, other: RoutingCounters) -> bool: ...   # nothing lowered or dropped; one call's
                                                            # confidence never rewritten, never a switch back
    def after_tool_call(self, call: ToolCall) -> RoutingCounters: ...   # +1 denial if denied
    def after_call(self, call: LLMCall) -> RoutingCounters: ...         # schema_invalid or confidence
    def asked(self, question_hash: str) -> RoutingCounters: ...         # a repeat adds REPEATED_QUESTION
    def flagged(self, *flags: ExplorerFlag) -> RoutingCounters: ...
    def pr_rejected(self) -> RoutingCounters: ...
    def apply(self, ctx: RoutingContext) -> RoutingContext: ...         # fills the ctx fields above, only
                                                                        # towards opus/high (min confidence)
def explorer_question_hash(caller: Role, question: str, scope: str | None = None) -> str: ...
    # SHA-256 of caller + NFKC/casefold/whitespace-normalized question and scope

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
    parent_call_id: LLMCallId | None                 # set exactly with caller: the call that ran explore
    step_id: StepId | None; step_type: StepType | None; attempt_id: AttemptId | None
    step_target_files: frozenset[str]                # test_touch: the only test files writable (H3)
    flag_justifications: Mapping[str, str]           # H8 (A11): the running step's Step.flag_justifications
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
Snapshot refs: `phase_ref(RunStatus.BUMPED | CODEMODS_APPLIED)` → `refs/mpilot/phase/<name>`, `attempt_ref(n)` → `refs/mpilot/attempt/<n>` with a run-wide `n`: §12 `Attempt.n` is that same number (unique per run, enforced in the DB); `Attempt.n_in_step` is the per-step ordinal.
Test doubles: `FakeSandbox`, `FakeDriver`, `MemoryWorkspace`, `MemoryMirror`, `BlobTree` (`testing/infra_doubles.py`).
Implementation: `DockerDriver` / `DockerSandbox` (`sandbox/driver.py`, P2-02): runsc detection with a recorded runc fallback, the work container with the §9.3 hardening and limits, `exec` (output tails, timeout kill by exec tag, cancellation), `frozen()` (P2-03: `docker pause`, probed at create; else KILL_ALL, which kills every process but init and the recorded main pid and proves none is left; `exec` refuses while frozen, `frozen()` refuses while a command runs or an install container exists), `destroy`, `reap`. `export_workspace`/`restore` are P2-04, `NetworkMode.REGISTRY` P2-09, cache mounts P2-07; until then they raise `NotImplementedError`. Under gVisor a command that passes the memory or pids limit kills the whole sandbox: `exec` restarts the container and returns exit code 137 with a note on stderr.
Implementation: `HostWorkspace(root, *, sandbox=None)` (`sandbox/workspace.py`, P2-01). Every name resolves through `openat2` with RESOLVE_BENEATH | NO_SYMLINKS | NO_MAGICLINKS | NO_XDEV, each file is type-checked through `O_PATH` before any I/O open, writes are temp file + rename, a `.git` component is refused in caller paths and skipped at every depth in walks, and `walk_tree` applies `ArtifactRules` with git's gitignore precedence (via `pathspec`). With a `sandbox`, every call refuses unless it is frozen.

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
    mirror_commit: str; verify: VerifyResult       # whole pipeline from install; verify.tests.stage == final
    tests -> TestResult | None                     # = verify.tests; None if red before the tests (final_red)
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
    step_diffs: Sequence[tuple[StepId, WorkspaceDiff]]   # chained segments, each tagged with its step; a step
                                                         # may recur (repair, revise); test_touch hunk mapping (d)
    flag_justifications: Mapping[str, str]           # H8 (A11): Plan.flag_justifications, the union over steps
    removed_api_patterns: Sequence[ApiPattern]       # D3

class PolicyCheck(Protocol):
    id: str                                          # "integrity.a" ... "integrity.i", "diff_limits", ...
    def evaluate(self, inp: GateInput) -> list[Violation]: ...   # pure; empty list = pass

def binding_violations(inp: GateInput) -> list[Violation]: ...
    # "gate" violations: final run, commit_tree, diff target/digest not the pushed commit
    # (commit_mismatch); diff base or base_tree not base_sha (base_mismatch); plan of another
    # run, or a final test run of another run (run_mismatch); a final run with no test run, red
    # before the tests (final_red); step_diffs not one unbroken chain from base_sha to the pushed tree, a
    # segment for a non-plan step, steps beginning out of plan order, or changes with no step
    # diffs (step_chain)
def evaluate_gate(checks, inp, *, required) -> IntegrityReport: ...
    # binding_violations first, then every check; ValueError on no checks, empty `required`
    # (no default: the gate can't pass by running nothing), duplicate, reserved ("gate") or
    # missing check ids, or a violation under a foreign id. The report carries
    # mirror_commit = inp.mirror_commit and checks = the sorted ids that ran (P1-12 gate)

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
        # branch_status(...).pushable, commit == verified.mirror_commit == report.mirror_commit
        # == verdict.mirror_commit (a stale pass's report/verdict names an older commit),
        # report.passed, the Critic approved and all three from the same run, checked right
        # before the push
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
    async def drive(self, run_id: RunId) -> RunStatus: ...     # takes the lease; until terminal, PAUSED_LIMIT
                                                               # or AWAITING_APPROVAL; CancelRequestedError at a
                                                               # checkpoint -> teardown -> END_CANCELLED
    async def resume(self, run_id: RunId) -> RunStatus: ...    # B5 semantics; lease first, then control:
                                                               # a recorded cancel ends it END_CANCELLED
    async def decide_plan(self, run_id: RunId, *, approve: bool, reason: str | None) -> None: ...
        # records the CP-1 decision; a live waiting driver applies it, else this process takes the
        # lease and applies it (approve -> drive, reject -> END_REJECTED)
    async def cancel(self, run_id: RunId) -> None: ...
        # records the request (durable); no-op if ended; no live driver -> take the lease, reap,
        # END_CANCELLED; a live one -> wait until the run ends or its lease expires, then take over

class AttemptNumbers(BaseModel, frozen=True): n: int; n_in_step: int   # one counter update, n_in_step <= n

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
    def start_attempt(self, step_id: StepId) -> AttemptNumbers: ...   # (n, n_in_step); AttemptCapError at 6/40
    def record_sandbox_seconds(self, seconds: float) -> None: ...
    def pause(self) -> None: ...; def resume(self) -> None: ...; def check_time(self) -> None: ...

AppendRow = ToolCall | LLMCall | RoutingDecision | TestResult | Attempt | IntegrityReport | CriticVerdict | MigrationNote
SingleRow = Baseline | Plan | PullRequest | EscalationReport

# models/artifacts.py (frozen, like the other artifacts)
class MigrationNote(ArtifactModel): run_id: RunId; key: str; text: str          # one `write_note` call, all kept
class EscalationReport(ArtifactModel):                                           # FR-17, one per run
    run_id: RunId; category: FailureCategory; report_md: str; data: dict[str, Any]; created_at: datetime

class RunStore(Protocol):                            # SQLite WAL (P1-10); every write of run state is fenced
    def create_run(self, run: Run) -> None: ...
    def save_run(self, run: Run, *, lease: Lease) -> None: ...      # may not change CHECKPOINT_OWNED fields
    def get_run(self, run_id: RunId) -> Run: ...; def list_runs(self) -> Sequence[Run]: ...
    # coordination (P1-12c): LeaseLostError for a write whose lease is not the current one
    def acquire_lease(self, run_id: RunId, holder: str, *, ttl_s: float) -> Lease: ...   # LeaseHeldError for
                                                                    # any live lease (names are labels), RunEndedError; atomic
    def renew_lease(self, lease: Lease, *, ttl_s: float) -> Renewal: ...   # + the run's RunControl: the heartbeat
                                                                    # is how a cancel reaches a non-checkpointing driver
    def release_lease(self, lease: Lease) -> None: ...              # no-op if no longer current
    def current_lease(self, run_id: RunId) -> Lease | None: ...
    def control(self, run_id: RunId) -> RunControl: ...
    def request_cancel(self, run_id: RunId, *, reason: str | None = None) -> RunControl: ...   # no lease;
                                                                    # first request kept; no-op once ended
    def request_plan_decision(self, run_id: RunId, *, approve: bool, reason: str | None = None) -> RunControl: ...
                                                                    # only in AWAITING_APPROVAL, only once
    def append(self, row: AppendRow, *, lease: Lease) -> None: ...
    def put(self, row: SingleRow, *, lease: Lease) -> None: ...     # a Plan's approved must match plan_problem;
                                                                    # a Baseline once per run, never replaced
    def rows(self, run_id: RunId, kind: type[RowT]) -> Sequence[RowT]: ...
    def append_final_verification(self, run_id: RunId, result: FinalVerification, *, lease: Lease) -> None: ...
    def final_verifications(self, run_id: RunId) -> Sequence[FinalVerification]: ...             # oldest first
    def checkpoint(self, cp: Checkpoint, *, lease: Lease, rows: Sequence[AppendRow] = ()) -> None: ...
        # one atomic step: fence -> a recorded cancel refuses all but END_CANCELLED (CancelRequestedError,
        # nothing written) -> covers + transition_problems (StoreError) -> rows + checkpoint + the run's
        # status/paused/spent/terminal_reason -> a terminal checkpoint releases the lease
    def latest_checkpoint(self, run_id: RunId) -> Checkpoint | None: ...
    def add_eval(self, result: EvalResult) -> None: ...; def evals(self, suite: str) -> Sequence[EvalResult]: ...
        # DRAFT until P9-01: EvalResult gains run_id and bench_id (new migration); these may change

# models/coordination.py (P1-12c): pure rules both stores apply
class PlanDecision(StrEnum): APPROVE; REJECT
class Lease(BaseModel, frozen=True):                 # one per run, updated in place, never deleted
    run_id: RunId; holder: str; token: int           # fencing token: +1 with every new holder
    acquired_at: datetime; expires_at: datetime; released_at: datetime | None
    def live(self, now: datetime) -> bool: ...       # not released and not expired
class RunControl(BaseModel, frozen=True):            # requests from other processes, each made once
    run_id: RunId; cancel_requested_at: datetime | None; cancel_reason: str | None
    plan_decision: PlanDecision | None; plan_reason: str | None; plan_decided_at: datetime | None
    cancel_requested -> bool
def take_lease(current: Lease | None, run_id: RunId, holder: str, *, now: datetime, ttl_s: float) -> Lease: ...
def check_fence(current: Lease | None, lease: Lease, run_id: RunId) -> Lease: ...   # expiry alone never refuses
def renew(current: Lease | None, lease: Lease, *, now: datetime, ttl_s: float) -> Lease: ...
def transition_problems(new_state: RunStatus, previous: RunStatus, control: RunControl) -> list[str]: ...
    # effective states (a paused run's paused_at_state): END_CANCELLED needs a cancel request,
    # END_REJECTED a reject; from AWAITING_APPROVAL only waiting, END_ERROR, END_BUDGET_EXCEEDED and
    # those two go without an approve
def plan_problem(plan_approved: bool | None, control: RunControl) -> str | None: ...   # Plan.approved = recorded decision
class Renewal(BaseModel, frozen=True): lease: Lease; control: RunControl
def run_following(run: Run, cp: Checkpoint) -> Run: ...   # CHECKPOINT_OWNED = status, paused_at_state,
                                                          # spent, terminal_reason, taken from cp

class EventKind(StrEnum): STATE_CHANGED; ROUTING_DECISION; LLM_CALL; TOOL_CALL; VERIFY; CHECKPOINT; APPROVAL_REQUESTED; LOG
class RunEventRecord(BaseModel, frozen=True): run_id: RunId; seq: int; at: datetime; kind: EventKind; data: dict
class EventBus(Protocol):                          # durable first: publish writes run_events (P3-05, new
                                                   # migration), then fans out; events are a view, never state
    def publish(self, run_id: RunId, kind: EventKind, data: dict[str, Any], *, lease: Lease) -> RunEventRecord: ...
        # assigns seq; fenced like every write of run state
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
    step_id: StepId | None; attempt_n: int | None      # run-wide Attempt.n (= attempt ref n)
    attempt_id: AttemptId | None                     # minted at attempt start; set exactly with attempt_n
    sticky: dict[str, StickyState]                   # sticky_scope -> state
    routing: dict[str, RoutingCounters]              # sticky_scope -> routing inputs from history
    budget: BudgetState; failure_history: tuple[FailureHistoryEntry, ...]
    last_tool_call_id: ToolCallId | None; last_llm_call_id: LLMCallId | None   # rows folded in; never cleared
    completed_calls: tuple[LLMCallId, ...]           # top-level calls finished in this unit, in order
    restore_epoch: int                               # +1 per crash restore (§10.1)
    in_flight: InFlightCall | None
    latest_snapshot_ref: str | None
    export_dir: str | None; export_digest: str | None   # both required when paused
    terminal_reason: str | None                      # only in a terminal state; copied to Run
    integrity_failures: int; critic_revisions: int   # §7.4 "once"; FR-15 at most one revise
    unit_key -> (RunStatus, StepId | None, int | None, int)   # (paused_at_state or status, step, attempt_n, epoch)
    scope -> str                                     # sticky_scope of the unit, never phase/PAUSED_LIMIT
    # attempt_n == budget.attempts_total while set; may not name its own attempt snapshot ref
    def covers(self, previous: Checkpoint) -> list[str]: ...     # nothing may go backwards (B5): sticky
        # flags, routing counters, attempt counters, spent, active time, failure-history attempts and
        # tried hypotheses, row marks, restore_epoch; a set attempt_n is the previous one (same id) or
        # a new attempt (above previous attempts_total, new id), even across a gap with no attempt;
        # within one unit_key completed_calls only grows at the end; a crash restore only cuts it back

TREE_WRITER_ROLES = {EDITOR, B2_SINGLE_SHOT, B3_GENERIC}   # calls a crash restore undoes (§10.1)
class ResumePoint(BaseModel, frozen=True):
    checkpoints: tuple[Checkpoint, ...]              # 1-2, written first: reconciliation, then (crash) the restore
    completed: tuple[LLMCall, ...]                   # never re-run; their outputs feed what comes next
    restart: InFlightCall | None                     # after a pause only: restart with its summary (FR-21)
    undone: tuple[LLMCall, ...]                      # after a crash only: from the unit's first tree
                                                     # writer on, re-run (the snapshot restore undid them)
def resume_point(cp: Checkpoint, calls: Sequence[LLMCall], tool_calls: Sequence[ToolCall], *, at: datetime) -> ResumePoint: ...
    # Reconcile: rows after the checkpoint's marks (a crash between a row and its checkpoint) are
    # folded into routing[cp.scope] and finished top-level ones promoted to completed; the in-flight
    # call gains its newer tool calls. Pause: the export is the state after the last tool call, so
    # all finished calls are kept. Crash: the workspace goes back to latest_snapshot_ref, which
    # predates the unit, so the restore checkpoint bumps restore_epoch, keeps only the calls before
    # the first tree writer and clears in_flight (re-issued without a summary).
    # ValueError if the rows contradict cp (missing, limit, nested, other step/attempt, mark without row)
def latest_output(calls: Sequence[LLMCall], role: Role) -> dict[str, Any] | None: ...
    # last COMPLETED top-level call's output: the AnalysisReport's home for the Planner (§8.2)
```

**Coordination (P1-12c).** One driver per run: whatever drives a run (`mpilot run`, `resume`,
`approve`, `cancel` with no live driver, the API) first takes the run's lease, keeps that one
lease, and renews it at least once every ttl/3 from a heartbeat that also runs during long tool
calls and while waiting for approval (SPEC §10.1: 60 s, every 20 s; P3-05 makes it a setting). A
live lease is never handed out again, not even to the same holder name. Every write of the
run's state carries the lease, and the store checks it in the write's own transaction, so a driver
that stalled past its lease and was taken over can write nothing, ever. `mpilot cancel` and
`mpilot approve` record a request (`RunControl`) without a lease: a live driver meets a cancel at
its next checkpoint (`CancelRequestedError`, nothing written) or heartbeat (`Renewal.control`),
stops any running tool call and ends END_CANCELLED; it sees a plan decision the same way while it
waits. With no live driver the requester takes the lease and applies the request itself; the CLI
waits for a live driver to finish, the REST API answers 202. A cancel recorded before the final
checkpoint wins even after the PR was opened (the PR stays a draft). The
store refuses END_CANCELLED without a request, END_REJECTED without a reject, leaving
AWAITING_APPROVAL (a pause counts as the state it pauses in) for anything but END_ERROR or
END_BUDGET_EXCEEDED without an approval, a `Plan.approved` that disagrees with the recorded
decision (CP-1 is a human decision), and anything after a terminal checkpoint. The checkpoint is authoritative: it sets the run's status, paused state, spent and
terminal reason in the same transaction, and `save_run` may not change them. A role call's row
should go in the same `checkpoint(..., rows=[row])` call as the checkpoint that folds it in;
`resume_point`'s reconciliation stays as the backstop for rows written on their own (the ToolHost's
tool rows).

**Resume protocol (FR-21, P1-12b).** For each role call: a checkpoint naming it `in_flight`
before the runtime is invoked; when it ends, its `LLMCall` row (finished: `stop` set; or
limit-interrupted: `limit_error` set), then a checkpoint that folds the row in
(`completed_calls`, routing counters, `last_llm_call_id`), or after a limit the paused checkpoint
that keeps it in flight. Tool calls likewise (row, then a checkpoint with `last_tool_call_id`).
A resume writes `resume_point(...).checkpoints` before routing anything. Phase snapshots are
taken only as their phase ends, like attempt snapshots, so a crash restore never keeps a unit's
own edits. Reconciling the BudgetManager with a promoted call's usage (`record_call`) is the
driver's (P4-16/P4-18); until then its guard charges stay in `in_flight_*` and still count. The next
role is always given the stored `LLMCall.output`, never the in-memory result, so a resumed run
and an uninterrupted one see the same input. After every tool call and role call the checkpoint
folds the row into `routing[sticky_scope(...)]` (`after_tool_call`, `after_call`), and routing
builds its context with `RoutingCounters.apply`, so one denial before a crash and one after still
fire trigger 3. `ToolCall.role_call_id` joins tool calls to their role call (FR-18 replay and
span nesting, §8.5 "the Critic cites files it read itself"); `LLMCall.parent_call_id` nests an
Explorer call under the call that ran `explore`.

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
| `attempts` | `Attempt` (+ nested `Diagnosis`; `signature`, `category` copied out); `UNIQUE(run_id, n)` (`n` names the attempt ref) and `UNIQUE(run_id, step_id, n_in_step)` | append-only |
| `tool_calls`, `routing_decisions`, `llm_calls` | `ToolCall` (`role_call_id` indexed, no FK: the call's row comes later), `RoutingDecision`, `LLMCall` (FK to its decision; `step_id`, `attempt_id`, `parent_call_id`, `stop`, `turns` as columns, `output` only in `doc`) | append-only |
| `test_results`, `final_verifications`, `integrity_reports`, `critic_verdicts` | `TestResult`, `FinalVerification`, `IntegrityReport`, `CriticVerdict` (the last three with a `mirror_commit` column: the commit judged) | append-only |
| `migration_notes` | `MigrationNote`, one row per `write_note` call; the current note for a key is its highest `seq` | append-only |
| `eval_results` | `EvalResult` (bench rows, no run FK) | append-only |
| `run_leases`, `run_controls` | `Lease`, `RunControl` (P1-12c; every field a column, no `doc`) | one per run, updated in place; a lease row is never deleted, so its token only rises |

Append-only tables have `seq` (insertion order) and `recorded_at` (the store's `Clock`, UTC). Child
tables cascade from `runs`. NFR-12 is repeated as CHECK constraints (tier opus/sonnet, effort
medium/high, never Opus at medium, no "haiku" in a model name), and so are the model invariants
that fit in SQL (deny reason iff denied, paused state iff `PAUSED_LIMIT`, non-negative counts,
`stop` iff no `limit_error`, `parent_call_id` iff Explorer, an attempt only inside a step).
Types are portable: `JSON` (JSONB on Postgres), timezone-aware UTC datetimes through `UtcDateTime`,
named constraints. SQLite needs `enable_sqlite_fks(engine)` for foreign keys; P1-10 calls it.

`models/migrate.py` runs the packaged Alembic migrations from code (`upgrade`, `downgrade`, the
same on an open connection, and `upgrade_sql`/`downgrade_sql` that print the SQL for any dialect
without a server). `0001_initial_schema` is generated, then frozen (it imports no app code). It was
amended at the Phase 1 gate (P1-12a, P1-12b), before any database existed outside tests; from the gate on,
every schema change is a new revision. A test compares the migrated schema to `tables.py`, so
changing a table without a new revision fails the build.

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
                       *, run_id: RunId, runs: int = 3) -> Baseline: ...      # FR-4, D4, D9; never raises for
                                                   # red: Baseline.stages ends with a failing pre-test stage
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
