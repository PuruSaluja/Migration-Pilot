# Ambiguities and proposed resolutions (P0-1)

Status: **APPROVED by the owner 2026-11-19** and written into SPEC v1.4 (task P0-2). Owner answers: D7 → 16 GB PC, WSL raised to 12 GB (Q10); D6 → loopback `runs/` approved; A4 → background model pinned to Sonnet in `.claude/settings.json`; B1 → no API key ever (Q9). All other proposals approved as written.

> **SPEC.md v1.4 supersedes this file where they differ.** The three spec-reviewer passes tightened several items; see the §20 rows marked "second review" and "third review".
Each item says what in SPEC.md is unclear, missing or in conflict, and what I propose.
**[CALL]** marks items that need your explicit decision. The rest are defaults I'll use unless you object.
**[SPEC]** marks items that change a [REC] decision, a requirement or a phase DoD. After approval they go into SPEC.md and §20 (task P0-2).
Nothing here weakens a §11 guardrail. Where a proposal changes one, it makes it stricter.

---

## A. Trust boundaries and security (highest impact)

**A1 [CALL][SPEC] The workspace lives on the host; read and edit tools run on the host.**
§7.1 says tools "execute inside the sandbox", and §9.3 allows one workspace mount.
Proposal:
- **Location:** the host directory `runs/<id>/workspace`, bind-mounted read-write at `/workspace`. The container uid equals the host uid.
- **Tools on the host:** `view_file`, `list_dir`, `str_replace`, `create_file` and `delete_file` run on the host through `Workspace`.
- **Tools in the sandbox:** everything that executes (ripgrep, build, tests, codemods, git) runs there.
- **Primary defense:** every open uses `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)`, plus an `fstat` check that the target is a regular file. That is race-free on its own. Setuid files, device files and FIFOs are rejected.
- **Secondary defense:** the container is frozen (`docker pause`, the cgroup freezer) whenever the host touches files. No sandbox process can run during host file operations, even one that re-spawned.
- **Export and import** never follow symlinks.
- **Host-side parsing** (tree-sitter) has file-size caps.
- **P2-01 also tests** that host writes are visible inside a runsc container immediately, and vice versa (gofer caching).

Why: export and import become trivial (FR-21), and edits produce exact diffs. Repo code still never executes on the host.

**A2 [CALL][SPEC] The host never trusts or runs sandbox-controlled git.**
The sandbox can write to `/workspace/.git`: config, hooks, the index, `info/exclude`. Two dangers follow:
- Host `git` run inside the workspace would execute sandbox-controlled code.
- A sandbox-produced `git diff` can **hide changes** from the gate: `update-index --skip-worktree`, gitignored new files such as a skipping `conftest.py`, or `diff.external`.

Proposal:
1. The host keeps a separate **mirror** clone (`runs/<id>/mirror`) that is never mounted. All host git runs with `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, a temp HOME, `core.hooksPath=/dev/null`, `core.fsmonitor=false`, no LFS filters, and a minimum git version check.
2. **The host computes the diff itself.** It walks the workspace through `Workspace`, ignoring sandbox git state, and compares the tree with the mirror at `base_sha`. Test-file hashes come from the same walk. Any new file the D2 classifier matches is flagged, even if it is gitignored.
3. The diff is applied in the mirror and committed. The mirror tree must then **equal** the host-walked workspace tree for every non-artifact path, or the gate fails.
4. **The whole pipeline (install → build → typecheck → full suite) runs in a fresh sandbox built from the exact mirror commit that will be pushed, and every success signal and gate check judges only that run** (SPEC §9.5).
5. Pushes use an explicit refspec, `refs/heads/mpilot/<x>`. The token goes in through `GIT_ASKPASS` and never appears in `.git/config` or the URL.

Attempt and phase snapshots are host-owned mirror refs (SPEC §9.5, §10.1), not sandbox git branches.
Intake clone hardening: no hooks, no submodules, no LFS smudge, `protocol.file.allow=never`.

**A3 [CALL][SPEC] Move tool-output secret redaction from Phase 6 to Phase 4.**
§9.3 says all tool output is redacted before any model sees it, but redaction is only built in Phase 6, after the first real model calls in Phase 4.
Proposal: a minimal gitleaks post-hook on every tool output from Phase 4 on. Phase 6 completes it. This only makes things stricter.

**A4 [CALL] No Haiku inside the Agent SDK either.**
Claude Code, which the SDK wraps, may use a small or fast background model, and NFR-12 forbids Haiku anywhere.
Proposal: `AgentSDKRuntime` pins every model setting the SDK exposes to Sonnet 5.5, and checks the per-model usage the SDK reports after each call. Any model other than the routed one gives `ForbiddenModelError`. The mechanism is verified in P4-01.
For this build process I recommend the same pin in `.claude/settings.json`. That's a settings change, so it needs your OK.

**A5 SDK lockdown, explicit list** (adds to §8.1):
- **Options:** an explicit `disallowed_tools` list covering every built-in, including Task, WebFetch and WebSearch; no SDK-native agents; a strict MCP config (only the in-process `mpilot` server); no settings sources.
- **Environment:** temp `HOME` and `CLAUDE_CONFIG_DIR`. The subprocess env holds only the OAuth token, PATH and the temp paths.
- **Permissions:** never use bypass-permissions mode. A deny-by-default permission callback runs alongside the PreToolUse hook.
- **Startup check:** every runtime start validates these options and env statically. `mpilot selftest` adds the live check, a real call proving a built-in tool is refused.
- **Optional hardening:** OS-level confinement of the SDK subprocess (bubblewrap or landlock), decided in P4-01.

**A6 The `run_command` denylist is defense in depth, not the boundary.**
A denylist can always be bypassed. The real controls are network `none`, no credentials, gVisor, and the host-computed diff (A2). The Editor's "non-test" rule exists for bookkeeping only.

**A7 [CALL] Two containers instead of switching networks.**
runsc sets up networking when the sandbox boots, so connecting and disconnecting networks on a running gVisor container is likely unsupported.
Proposal:
- A long-lived **work container** created with `network_mode=none`.
- A short-lived **install container** that shares the same workspace mount and image and sits on a **per-run** `internal` network whose only other member is the egress proxy. It is used only by `install_deps`.
- The proxy allows CONNECT only on port 443 to allowlisted hosts (checked by Host and SNI), and logs every denial.
- DNS lookups from the install container through Docker's built-in resolver must fail. Only the proxy resolves names.

Residual risk, documented: install lifecycle scripts can reach allowlisted registries.
Implemented in P2-08a (2026-11-22): the concrete host list, narrow-only config, ECH refusal and two further documented risks (whole `github.com`, domain fronting) are in SPEC §9.3 and §20.

**A8 [SPEC] Knowledge fetches happen on the host.**
FR-6 says changelog fetches go "through the egress allowlist", but the §9.3 proxy allowlist only covers package registries.
Proposal:
- Changelog, release-note and registry-metadata fetches run on the host through a separate `knowledge.fetch_allowlist`: GitHub API and raw content, npm/PyPI metadata, and the doc domains named in the migration registry.
- Fetches carry no credentials, follow redirects only to allowlisted hosts, and have size caps.
- Content is untrusted (H6) and secret-scanned.
- The allowlist enforcement is an [OPUS] task.

**A9 GitHub branch protection without Administration access.**
The checks use the repo's default branch, the branch's `protected` flag and the rulesets endpoint. Classic protection patterns on branches that don't exist yet can't be seen without Administration, so for those we rely on GitHub's server-side enforcement, plus the `mpilot/*`-only rule. Never request Administration or Workflows.

**A10 [CALL] GitHub token handling.**
- **Source:** the token is read once at startup, from `MPILOT_GITHUB_TOKEN` or a 0600 file outside the repo.
- **Scrubbing:** the env var is then **removed** from the orchestrator's own environment.
- **Spawning:** every child process (Docker, git, the SDK, tree-sitter) is started through one helper that passes an explicit env. A test asserts that no child sees the token.
- **Scope:** `CredentialProvider` refuses repos that aren't on its configured list (§11.3 control 1).
- **No OS keyring**, which is unreliable on WSL.

**A11 [SPEC] Enforce H8 and FR-8 AC3 in code.**
`--force`, `--legacy-peer-deps` and `--no-verify` are denied in `install_deps`, `run_command` and the bump step, unless a plan step carries a justification for that exact flag. The PR renders the justification. This is a new [OPUS] policy task, with tests written first.

## B. Runtime and model routing

**B1 [CALL][SPEC] What if effort can't be set per call?**
FR-22 depends on setting effort per call (Appendix A). Proposal:
- **Can't be set per call, but the SDK's default is verifiably high:** force `effort_policy = all_high`, record a warning, and log every call as `high`.
- **Can't be set at all, and the default can't be verified:** `LLMCall.effort` can't be recorded truthfully. P4-01 stops and asks you. (Superseded by Q9: no API-key fallback.)

A call that ran at high is never logged as medium.

**B2 Budget definitions.**
- **Turn:** one model response, counted across every role, including the Explorer.
- **Tokens for the cap:** `input + output (thinking included) + cache_write + ceil(0.1 × cache_read)`.
- **Pessimistic pre-check:** the estimate sums over the call's turns, each costing the prompt plus the accumulated tool output (capped per tool) plus the output ceiling, because each agent turn re-sends its context. `max_turns` is clamped to the largest number of turns whose estimate fits; the call is refused only if one turn doesn't fit.
- **Mid-call stop:** a PreToolUse budget check ends a running call as soon as a cap is crossed, so the overshoot is bounded by one turn. That is stricter than US-5's one call.

**B3 Token estimate without a tokenizer.**
`ceil(chars / 3)`, a conservative estimate, used wherever it enforces a cap: the repo map, the Explorer's 1,500 tokens, migration notes and the Diagnoser's 8k. Reported usage always comes from the runtime.

**B4 Determinism with extended thinking.**
With thinking on, temperature probably can't be lowered (verify in P4-01). NFR-5 is met by pinning model IDs, prompts (`config_hash`), image digests and lockfiles. Bench "seeds" are repeat indices, and the reports will say so.

**B5 [CALL][SPEC] What resume means.**
FR-21 says resume "continues from the last completed tool call". Proposal:
- Completed role calls are never redone.
- A role call interrupted by a limit **restarts**, from the workspace as it was after its last completed tool call, plus a summary of the tool calls it had made.
- The interrupted call's tokens still count.
- `StickyState`, attempt counters and failure history are part of every checkpoint, so a resume can never de-escalate.

**B6 Cassettes.**
A cassette records traffic at the `AgentRuntime` boundary as JSONL: role-call requests, tool calls and results. Recording happens only during owner-triggered e2e runs, and inputs are secret-scanned before writing, because cassettes go into a public repo.

**B7 Model IDs and prices.**
`claude-opus-5-5` and `claude-sonnet-5-5`, re-verified in P4-01. The price table stays empty and the USD column shows "n/a" until prices are copied from the official pricing page with a date. No guessed prices.

**B8 [CALL] What the routing policies mean.** Proposal:
- **`all_opus`:** every role on Opus/high, including the Explorer and PR Writer. It's the "all-Opus" comparison point.
- **`all_sonnet`:** every role on Sonnet except the Planner and Critic, which §8.4 says are never routed down.
- **B2 and B3:** always Opus/high whatever the config, and a test asserts it.
- **Explorer and PR Writer under `routed`:** they stay on Sonnet even after sticky escalation. Escalation moves only their effort to high.

**B9 Router interface is pure.**
`route()` returns a `RouteOutcome`: tier, effort, reason codes, `escalated_from`, `effort_escalated_from`, sticky flag, the new `StickyState`, and `inputs_hash`. The orchestrator wraps it into the §12 `RoutingDecision` with IDs.
Stickiness is scoped per step. Calls outside any step (Analyzer, Planner, PR Writer) are scoped per run phase.
`has_changelog_mapping` (Editor) and `error_maps_to_changelog` (Diagnoser) are separate inputs.

**B10 Router v0 (Phase 4) emits only Opus/high for the routed roles** (Editor, Diagnoser, Analyzer) and never emits medium. A test asserts this. The full rules arrive in Router v1 (P5-11, P5-12).

## C. Orchestrator and repair loop

**C1 [CALL][SPEC] What counts as an improved, same or regressed attempt.**
Score each attempt as (furthest stage reached: install < build < typecheck < tests, number of failing tests).
- **Improved:** a later stage, or fewer failures at the same stage.
- **Same:** an equal score.
- **Regressed:** a worse score.
- **Revert-to-best:** at the same test stage, more than `best × 1.2` failing tests (at least best + 1). This adds a second trigger to §7.4: reaching an earlier stage than the best attempt also reverts.
- **No progress:** an unchanged failure-signature set for 3 attempts.

**C2 Flaky rerun only helps diagnosis; it never changes the judgement.**
Before diagnosing, failing tests are rerun once, and the result goes into the Diagnoser's input. **Gate check (a) is judged on the final full run against `P0 \ baseline_flaky`, and any failure there blocks the PR.** No test becomes "newly flaky" and drops out of judgement during a run.

**C3 Free-text goal parsing is deterministic.**
A small grammar plus flags. Anything that doesn't parse exits 2 and suggests flags. No LLM at intake in v1.

**C4 Async everywhere.**
The orchestrator, runtime and API use asyncio. The Docker SDK and git run in worker threads. A `Clock` interface makes time budgets testable.

**C5 Extra interfaces** beyond the seven in §16.4: `RunStore`, `EventBus`, `BudgetManager`, `Workspace`, `CredentialProvider`, `Clock`, plus a `spawn` helper (A10).

**C6 [SPEC] Cancel and exit codes.**
Add `END_CANCELLED` to §7.4 (`mpilot cancel` exists, but there is no state for it). The exit-code mapping:

| Final state | Exit code |
|---|---|
| END_SUCCESS | 0 |
| END_ESCALATED, END_ERROR, END_REJECTED, END_CANCELLED | 1 |
| Input errors | 2 |
| END_BASELINE_RED | 3 |
| END_BUDGET_EXCEEDED | 4 |
| PAUSED_LIMIT | 5 |

With `--approve-plan`, `mpilot run` blocks in AWAITING_APPROVAL. If interrupted it exits 130, and `mpilot approve` continues the run. A matrix test covers every row.

**C7 "Escalate immediately" cases (§7.4).**
These go straight to ESCALATED with the matching category:
- the proxy returns 401/403 from a registry (private-registry auth)
- a known missing-toolchain error signature (`ENV_TOOLCHAIN`)
- connection-refused to a localhost service port during tests (external service)
- a license-incompatible replacement
- a diff-limit breach

## D. Detection, baseline, integrity

**D1 [CALL][SPEC] 15 detection fixtures in Phase 2.**
FR-3 needs ≥ 15 labelled fixtures with ≥ 90% correct, but the Phase 2 DoD says ≥ 9/10. Proposal: build 15 and change the DoD to ≥ 14/15.

**D2 [CALL][SPEC] How test files are identified.**
This extends the §11.2(e) list. A path is a test file if it matches any of these:
- **Test file patterns:** `tests/**`, `test/**`, `testdata/**`, `test_*.py`, `*_test.py`, `*.test.*`, `*.spec.*`, `__tests__/**`, `src/test/**`
- **Snapshots:** `__snapshots__/**`, `*.snap`
- **Setup files:** the files that jest/vitest `setupFiles` and `setupFilesAfterEach` point to
- **Baseline results:** any file the baseline JUnit results name

Test config (always flagged, and gated by §11.2(e)):
- `conftest.py`
- jest and vitest configs, including `vitest.workspace`
- `pytest.ini`, `[tool.pytest]`, `setup.cfg [tool:pytest]`, `tox.ini [pytest]`
- `.mocharc*`
- in `package.json`: `scripts.test` and the `jest` key
- coverage configs: `.coveragerc`, nyc/c8 config, and `--cov-fail-under` wherever it appears
- Surefire excludes and Gradle test filters

This is an [OPUS] task with tests written first.

**D3 [CALL] Where the removed-API list comes from.**
Each migration-registry entry carries a curated `removed_apis` pattern list, pre-registered for the bench. APIs the Analyzer extracts itself are warnings only and never decide the gate.

**D4 Coverage.**
- **When:** measured in one extra baseline run and in the final full run. It isn't part of the ×3 flaky detection.
- **Not measurable at baseline:** (f) is "n/a".
- **Measurable at baseline but not at the end:** that is a **violation**.
- **What's reported:** the 2 pp gate result, plus the 5 pp figure for comparison with FreshBrew.

**D5 [CALL] Diff limits and codemods.**
Keep NFR-8 exactly as specified for now. Record how often codemods cause breaches during Phase 5, then decide with that data.

**D6 [CALL][SPEC] Disk limit.**
Docker's per-container disk quota doesn't work on WSL's ext4. Proposal:
- (1) `runs/` lives on a fixed-size loopback ext4 image, a one-time sudo setup in `setup-dev.sh`. That's a hard ceiling that protects the host disk.
- (2) The driver polls workspace and cache size **during** each command and kills it at 20 GB (`ENV_INSTALL`).
- (3) `/tmp` is a size-capped tmpfs.

**D7 [CALL] Sandbox memory.**
The §9.3 limit is 8 GB, but WSL sees 7 GiB. Raise WSL to ≥ 12 GB in `%UserProfile%\.wslconfig` if the PC has ≥ 16 GB of RAM, or else set `sandbox.memory_gb = 5` in the local `mpilot.toml`. That's a local override, not a spec change.

**D8 Repo size check (FR-2).**
For GitHub URLs, an unauthenticated `GET /repos/{o}/{r}` gives the size before cloning, using a minimal `repo_info` in P3-03. For local paths, the directory is measured. Both are re-checked after cloning.

**D9 [SPEC] Gate inputs.**
- §12 `TestResult` stores counts only, so check (a) has nothing to compare. Add `passed_ids` and `failed_ids` to `TestResult`.
- The "executed test count" for (b) is passed + failed + errors, excluding skipped.

**D10 [SPEC] Phase-ordering conflicts in the spec.**
- **Phase 5 DoD:** it asks for L3, which needs the full gate (Phase 6), and "traces", which need OTel (Phase 7). Proposal: Phase 5 uses **L3-interim**, meaning the Phase 4 minimal gate on the final verification run: (a), (b), (c), (g), no new or changed test files or test configs, and no workflow diff. "Visible in traces" there means RoutingDecisions shown by `mpilot logs`. True L3 is re-checked at the Phase 6 and Phase 7 gates.
- **P9-12 effort guard:** it needs L4 rubric scores, which come from the Phase 10 review. Proposal: the blinded L4 review of the 15-task ablation subset moves into Phase 9 (P9-12), before the guard is applied.

## E. Tooling and tests

**E1 Test markers.** Proposal:
- **Default `pytest -q`:** unit and Docker tests. **If Docker is unavailable, Docker tests fail**; skipping requires an explicit `MPILOT_SKIP_DOCKER=1`, and phase gates reject any skip.
- **`network`:** tests that download from registries; opt-in with `MPILOT_NETWORK_TESTS=1`, and run at the gates.
- **`e2e`:** real Claude calls; only triggered by you.

**E2 Library picks.**
- **CLI:** Typer.
- **GitHub:** githubkit.
- **HTTP mocking:** respx.
- **Property tests:** hypothesis.
- **Changelog search:** SQLite FTS5 (BM25), with no embeddings.

**E3 Codemod tools** are preinstalled in the images and run with network `none`.

**E4 Langfuse.** Self-hosted v3 is heavy (Postgres, ClickHouse, Redis, MinIO). Decide in Phase 7 from measured memory. The fallback is the free cloud tier with redacted traces (Q4).

**E5 Spec header typo.** The status line says v1.2; it is v1.3. Fixed in P0-2.

**E6 Logs and the API.**
- JSON logs from Phase 1 on (NFR-7).
- The API binds to 127.0.0.1 only (§13.2), and a test asserts it.

## F. Deferred (resolved when the phase is reached)

- **F1 B2 inputs (Phase 9):** the files from the first failing stage log, plus usage sites, under the Editor's context budget.
- **F2 B3 turn cap (Phase 9):** the same `max_turns` cap as Full (300), not Full's measured turns.
- **F3 FR-19 output branch (Phase 8):** fixes go to `mpilot/fix-<bot-branch>`, and the draft PR targets the default branch and links the bot PR. The bot's branch is never pushed to. Package and versions come from the manifest diff, never from PR text.
- **F4 PR Writer (Phase 7):** it fills only free-text JSON fields. Tables, the attestation, the H8 justifications and the stats are rendered deterministically. Everything is gitleaks-scanned and template-validated.
