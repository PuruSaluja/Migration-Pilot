# PROGRESS.md

## Current phase
Phase 1: Foundations

## Next task
[SONNET] P1-05: config loader (`mpilot.toml` < env `MPILOT_*` < CLI), every §6 default, tier→model ID, `routing.policy`/`effort_policy`, empty price table (B7), `config_hash`, `mpilot.toml.example` (see the Phase 1 table). Run the session on Sonnet 5.5 at high effort.

## In progress
(none)

## Done
- P1-04 (2026-11-20): `models/domain.py`, a Pydantic v2 model for every §12 class (+ nested `PackageChange`, `Budgets`, `Spent`, `FailedTest`, `VerifyStageResult`, `VerifyResult`, `Violation`, `RubricScores`, `HumanReview`), D9 `passed_ids`/`failed_ids` + `executed_count`. `RoutingPolicy`/`EffortPolicy` added to `core/types.py`. 99 new tests (round trip per model, §12 field-presence table checked against SPEC, invariants, syrupy JSON-schema snapshot). 283 tests green. Also made two P1-03 timeout tests robust (3 s instead of 0.5 s; one failed once under load).
- P1-03 (2026-11-20): `core/spawn.py` (`run`/`start`/`inherit`: explicit env only, absolute argv[0] and PATH entries, token refused in argv/env keys/cwd and in env values unless `pass_github_token`, own session, timeout kills the group and returns within a grace period) and `core/github_token.py` (read once from `MPILOT_GITHUB_TOKEN` or a 0600, owner-only, non-symlink file outside any git work tree; env var removed and its bytes wiped in place so `/proc/<pid>/environ` no longer shows it). CLI scrubs first thing. AST lint forbids every process-starting form outside `spawn.py`. Spec-reviewer: first pass 2 blockers + 5 non-blocking, all fixed; second pass approved, and 3 of its 5 non-blocking points fixed (duplicate env entries all wiped, reap on interrupt during the kill grace, `from importlib import import_module` in the lint). 184 tests green.
- P1-02 (2026-11-20): `core/` = `types.py` (enums, ID NewTypes + generators, `ExitCode`, `exit_code_for`, `Usage.cap_tokens`), `tokens.py` (chars/3), `clock.py` (`Clock`, `SystemClock`, `FakeClock`), `errors.py`. 80 tests, all checks green.
- P1-01 (2026-11-20): scaffold. `pyproject.toml` + `uv.lock` (full runtime/dev dependency list from `docs/requirements.md`), ruff/mypy strict/pytest config, E1 marker policy as a pytest plugin (`mpilot.testing.policy`, tested with pytester), `.pre-commit-config.yaml` (local hooks: ruff, mypy, gitleaks; pytest on pre-push; installed), `.gitignore`, JSON-lines logging (`mpilot.core.log`), Typer app with every §13.1 command stubbed (exit 1 + JSON log). 24 tests, all checks green.
- Pre-P0 env prep: `docs/requirements.md` (host tools, Python deps, accounts), `scripts/setup-dev.sh` (Docker Engine + gVisor + gh + gitleaks), `scripts/check-env.sh` (read-only verifier). Host OS resolved as SPEC Q7.
- Host setup run by owner 2026-11-19: native Docker Engine, gVisor verified (`docker run --runtime=runsc` reports kernel `4.19.0-gvisor`, systrap platform), gh and gitleaks installed. No runc fallback needed.
- Repo on GitHub: `PuruSaluja/Migration-Pilot` (public). History rewritten to the noreply email before the final push.
- P0-1: task list (below), `docs/decisions.md`, `docs/architecture.md`. Two spec-reviewer passes; all findings fixed. Approved by the owner 2026-11-19.
- Host finished 2026-11-20: WSL 12 GB (11 GiB visible), `runs/` on a 50 GB loopback image, `check-env.sh` all required checks pass.
- P0-2: SPEC v1.4 written (new §9.5, §20 rows), `.claude/settings.json` pins the background model to Sonnet, loopback `runs/` in `setup-dev.sh`, checks in `check-env.sh`.

## Blockers
- None for Phase 1.
- Before Phase 4 (owner): create the fine-grained PAT (§11.6).
- For P4-08 (found in P1-03): the Agent SDK's subprocess transport starts the Claude CLI with a copy of the whole `os.environ` merged with `options.env` (`claude_agent_sdk/_internal/transport/subprocess_cli.py`, ~line 764). The GitHub token is already scrubbed, but the §8.1 item 8 allowlist needs a custom transport, or a scrubbed `os.environ` at call time; `inherit()`/`spawn` can't cover it.
- For P1-05: move the token-file path from env `MPILOT_GITHUB_TOKEN_FILE` into the config loader.
- For P8-01 and any new entrypoint: call `load_github_token()` first thing, as the CLI callback does.

## Decisions (short notes; formal ones go in SPEC.md §20)
- 2026-11-19: SPEC v1.3 adds strict effort routing (medium/high) for both the product router (§8.4) and the build process (§16.1 [SONNET-MED] tag, §16.2 subagent efforts). See §20.
- 2026-11-19: Dev host = Ubuntu 24.04 / WSL2, native Docker Engine + gVisor, not Docker Desktop (SPEC Q7, §20). No requirements.txt: Python deps go in pyproject.toml + uv.lock in Phase 1; the planned list is in docs/requirements.md.
- 2026-11-19: Test repos and forks live on the owner's personal GitHub account `PuruSaluja` (SPEC Q8).
- 2026-11-19: SPEC v1.4 applies the approved P0 decisions and both review passes (see §20). Owner: no API key ever (Q9), WSL 12 GB (Q10), loopback `runs/`, background model pinned to Sonnet.

- 2026-11-20 (P1-01): the E1 policy plugin lives in `src/mpilot/testing/policy.py` (so mypy --strict covers it) and is loaded with `-p` in `addopts`. A docker-marked test without Docker is reported as a setup ERROR (red), not a skip. `run` also takes `--from` and `--replace` (from FR-1; absent from the §13.1 usage line). Budget flags default to `None` so the §6 defaults live only in the config loader (P1-05). Ruff excludes `*.md` because it otherwise reformats code blocks in `docs/architecture.md`. `types-docker` added to dev deps for mypy.
- 2026-11-20 (P1-02): §14.5 writes `BASELINE_RED/FLAKY` and `NO_PROGRESS/OSCILLATION`; each is ONE `FailureCategory` member named by its first token (matches §8.4's use of `NO_PROGRESS`), giving 17 members as docs/architecture.md says. A test parses SPEC §14.5 so the enum cannot drift. `exit_code_for` raises `ValueError` for non-terminal states other than PAUSED_LIMIT. Exit 130 is the constant `EXIT_INTERRUPTED`, not an `ExitCode` member. `Usage` gained `__add__` for budget accumulation.
- 2026-11-20 (P1-03): `spawn` requires an absolute executable and absolute PATH entries, so a binary planted in a workspace or mirror can never run on the host via cwd or PATH lookup (NFR-6). If both the env var and a token file are set, the env var wins (with a warning). The token check in `spawn` compares against the loaded token's value, so it needs startup to have run.
- 2026-11-20 (P1-03), left open from review pass 2, owner may want a spec note:
  - Residual risk: the token also lives in process heap memory. A same-uid child could read it through `/proc/<ppid>/mem` only if `kernel.yama.ptrace_scope=0`; this host has 1, which blocks it. `prctl(PR_SET_DUMPABLE, 0)` would close it, but it makes `/proc/self/*` unreadable to mpilot itself, so not done. Candidate for a SPEC §17.1 risk row.
  - `spawn.run` does not kill background grandchildren after a normal exit (the child is already reaped, so killing its group id could hit a reused pid). Callers start only trusted host tools. If a grandchild holds the pipes past `timeout_s`, the result reports `timed_out=True` even though the direct child had exited.
- 2026-11-20 (P1-04), model choices where §12 is only a sketch (none changes a [REC] decision, so no §20 row):
  - Added `run_id` to Baseline, Attempt, ToolCall, LLMCall, IntegrityReport, CriticVerdict ("everything is keyed by run_id", §7.3; the DB needs it); `confidence` on Diagnosis (§8.2/§8.4); `changelog_refs` on Step (FR-7).
  - `Baseline.runs` is `list[TestResult]` (the 3 baseline runs), `Step.attempts` is a count, `Run.base_sha` is optional until cloned, `EnvProfile.detection_sources` is `dict[str, str]` (field → source), `EvalResult.system` is a plain string, `RubricScores` has the four §14.3 criteria (1–5). Cheap to change before P1-09 if a later task needs different shapes.
  - Invariants enforced at the model level (spec-derived): Opus never at medium (LLMCall, RoutingDecision); no "haiku" in `LLMCall.model`; `escalated_from` implies opus, `effort_escalated_from` implies high; a test is not both passed and failed; flaky ids disjoint from P0; denied tool call needs `deny_reason`; `IntegrityReport.passed` iff no violations; PAUSED_LIMIT run records `paused_at_state`; ≤ 3 diagnosis hypotheses.
  - Test-ID sets are `frozenset[str]` that serialize as sorted lists (deterministic JSON).

## Task list

How to read it:
- Every task is sized ≤ ½ day and tagged by the SPEC §16.1 rubric.
- **Refs** are SPEC IDs; A1, B2 and so on refer to `docs/decisions.md`.
- **Deps** lists only the direct prerequisites. Every task also depends on the previous phase's gate.
- **Acceptance** is the test or check that proves the task done. The standard checks (ruff, format, mypy --strict, pytest) are always required too.
- Phases 5–10 are provisional: they get re-planned at the P4-25 checkpoint, using the measured pace (SPEC §16.4).

- Revised after two spec-reviewer passes (5 blockers fixed in total; see SPEC §20).

### Phase 0: Plan
| ID | Tag | Task | Refs | Deps | Acceptance |
|---|---|---|---|---|---|
| P0-1 | OPUS | Task list, interface signatures, ambiguity list | §18 rule 2 | — | Owner approves this file, `docs/decisions.md` and `docs/architecture.md` |
| P0-2 | OPUS | Write the approved [SPEC] decisions into SPEC.md (A1, A2, A3, A8, A11, B1, B5, C1, C6, D1, D2, D6, D9, D10) plus §20 rows; fix the version in the header; update `docs/requirements.md` (typer, respx, hypothesis); D6 loopback `runs/` in `setup-dev.sh` | §20 | P0-1 | Every approved item reflected in SPEC and §20. The stricter changes from review passes 2–3 (full-pipeline final verification, stricter (d)/(e), H8 via config files, host snapshot refs) were explicitly approved by the owner on 2026-11-20 |

### Phase 1: Foundations
| ID | Tag | Task | Refs | Deps | Acceptance |
|---|---|---|---|---|---|
| P1-01 | SONNET | Scaffold: `pyproject.toml`, `uv.lock`, ruff, mypy strict and pytest config with the `docker`/`network`/`e2e` markers and the E1 skip policy, pre-commit, `.gitignore` (`.env`, `mpilot.toml`, `runs/`, `*.db`), JSON logging (E6), Typer app with every §13.1 command stubbed (including `bench resume` and `bench report`) | §13.1, §15, NFR-7 | P0-2 | All checks green; `mpilot --help` lists every §13.1 command (test asserts it); `e2e` deselected by default; Docker tests fail without Docker unless `MPILOT_SKIP_DOCKER=1`; logs are valid JSON lines |
| P1-02 | SONNET | `core/`: enums (with END_CANCELLED), IDs, errors, `exit_code_for` (C6), `Usage.cap_tokens` (B2), token estimator `chars/3` (B3), `Clock` plus a fake clock | §7.4, §13.1, §14.5, NFR-1 | P1-01 | Tier and Effort have exactly 2 members; FailureCategory matches §14.5 exactly; exit-code matrix covers every terminal state plus PAUSED_LIMIT (exit 4 and 5 asserted); `cap_tokens` formula |
| P1-03 | OPUS | `core/spawn.py`: explicit-env subprocess helper + token scrub at startup (A10) | §9.3, §11.3, NFR-6 | P1-02 | Test: after startup `MPILOT_GITHUB_TOKEN` is gone from `os.environ`; a child started via `spawn` sees only the keys passed; a lint test forbids `subprocess`/`os.system` outside `spawn.py` |
| P1-04 | SONNET | Pydantic v2 domain models for every §12 class, plus `TestResult.passed_ids`/`failed_ids` (D9) | §12 | P1-02 | Every §12 field present; JSON round-trip per model; invalid tier or effort rejected; syrupy snapshot of the JSON schemas |
| P1-05 | SONNET | Config loader: `mpilot.toml` < env `MPILOT_*` < CLI; every §6 default; tier→model ID; `routing.policy`/`effort_policy`; empty price table (B7); `config_hash`; `mpilot.toml.example` | §6, §8.4, NFR-11 | P1-04 | Precedence tests; defaults equal §6; `config_hash` changes exactly when a prompt, model or setting changes; the default runtime is the Agent SDK and no API key is read or required (Q9) |
| P1-06 | SONNET | NFR-12 config validation | NFR-12, §8.2, FR-22 | P1-05 | Rejects: "haiku" in any model field or in the **value** of any `ANTHROPIC_*` override (any case; the Sonnet pin itself is accepted); effort outside {medium, high}; medium pinned to Opus or to Planner/Critic/Diagnoser/Analyzer; `all_opus` normalizes to `all_high` and is recorded |
| P1-07 | OPUS | Interfaces with docstrings, part 1: `AgentRuntime`, `ModelRouter`/`RouteOutcome`/`RoutingContext`/`StickyState` (B9), `Tool`, `ToolHost`, `ToolPolicy`, `PolicyCheck`, `GateInput`, `TestFileClassifier` | §8, §9.1, §11 | P1-04 | mypy strict; a conformance test per Protocol; `docs/architecture.md` matches the code |
| P1-08 | OPUS | Interfaces part 2: `Sandbox` (two containers, `frozen()`), `SandboxDriver`, `Workspace` (`walk_tree`), `VcsAdapter`, `CredentialProvider`, `Orchestrator`, `next_state`, `BudgetManager` (guard, attempts), `RunStore`, `EventBus`, `Verifier.final_verify`, knowledge drafts | §7, §9.3, §10, §11.6 | P1-07 | Same as P1-07; VcsAdapter has no merge method (asserted) |
| P1-09 | SONNET | SQLAlchemy tables + Alembic initial migration | §12 | P1-08 | Upgrade and downgrade on a temp DB; DDL compiles for SQLite and Postgres dialects |
| P1-10 | SONNET | `RunStore` implementation, SQLite in WAL mode | §12, NFR-4 | P1-09 | Round-trip of every model; `journal_mode` = wal; checkpoint then latest_checkpoint returns it |
| P1-11 | SONNET | `FakeRuntime` (scripted, goes through a `ToolHost`, can raise `UsageLimitError`) + `CassetteRuntime` replay (B6) | §18 rule 7, FR-21 | P1-07, P1-10 | Using a minimal test-double ToolHost: scripted run writes ToolCall and LLMCall rows; a scripted denial is recorded; a cassette request-hash mismatch fails loudly |
| P1-12 | OPUS | **Phase 1 gate:** spec-reviewer on the interfaces; fix findings; freeze `docs/architecture.md` | §16.4 P1 DoD | P1-01…11 | `mpilot --help` works; checks green; spec-reviewer approves |

### Phase 2: Sandbox, detection, baseline
| ID | Tag | Task | Refs | Deps | Acceptance |
|---|---|---|---|---|---|
| P2-01 | OPUS | Host-side `Workspace` (A1), tests first: `openat2` RESOLVE_BENEATH/NO_SYMLINKS/NO_MAGICLINKS + S_ISREG; `walk_tree` | FR-10, NFR-6 | P1-12 | Rejected: `..`, absolute paths, symlinked files/dirs/parents, symlink swapped in mid-operation, FIFOs, devices, setuid files; read size capped; every write returns a unified diff; `walk_tree` never follows links |
| P2-02 | SONNET | Docker driver lifecycle: create/destroy, §9.3 resource limits, runsc detection with runc fallback + warning, exec with output tails and timeouts | §9.2, §9.3, NFR-10 | P2-01 | Docker tests: pids and memory limits hold; a timeout kills the command; `runtime == runsc` on this host; runc fallback records a warning (fault-injected) |
| P2-03 | OPUS | Sandbox hardening + `frozen()` around host file ops (pause, or the kill-all fallback if pause fails under runsc) + uid mapping (§9.5) | §9.3, §9.5, NFR-6 | P2-02 | uid ≠ 0 and equals the host uid; CapEff = 0; no-new-privileges; root filesystem read-only; while frozen, no sandbox process runs (test with a spinning process); host writes visible inside runsc immediately and vice versa; the mechanism in use is recorded |
| P2-04 | SONNET | Workspace export/import with a hash manifest; never follows symlinks; `restore` | FR-21 | P2-03 | Export → destroy → restore gives an identical tree hash (including `.git`); a symlink in the workspace is exported as a link, never as its target |
| P2-05 | SONNET | Disk limits (D6): poll size during exec and kill at the limit; tmpfs `/tmp` cap; check the loopback `runs/` | §9.3, D6 | P2-02 | A command writing past the limit is killed with `ENV_INSTALL`; `/tmp` is capped |
| P2-06 | SONNET-MED | Base Dockerfiles: node 18/20/22, python 3.9–3.13; non-root user; ripgrep, git, tree-sitter CLI; FROM lines pinned by digest | §9.3 | P2-02 | Files only; reviewed in P2-07 |
| P2-07 | SONNET | Image build script + `images/digests.lock` + image smoke tests; package caches under `runs/cache/` keyed by lockfile hash, read-only in the work container, read-write only in install containers | §9.3, NFR-5 | P2-06 | Every image builds; `rg`/`git` run in each; one lock entry per image; cache key changes with the lockfile; a write to the cache from the work container fails |
| P2-08 | OPUS | Egress proxy container: CONNECT only on port 443, Host + SNI allowlist, denials logged (A7) | §9.3, §11.3 | P2-02 | Allowlisted registry reachable; example.com denied and logged; raw-IP CONNECT denied; non-443 denied; SNI/Host mismatch denied |
| P2-09 | OPUS | Install container on a per-run internal network; work container `network_mode=none` (A7) | §9.3, NFR-6, NFR-9 | P2-08 | Work container: registry, 1.1.1.1, host gateway and DNS all fail. Install container: npm registry OK, others denied, Docker DNS lookups fail. Two runs get separate networks; networks are removed after the run, even on exception |
| P2-10 | SONNET | Node env detection | FR-3, §9.4 | P1-12 | Tests first per rule: lockfile → package manager, yarn berry, npm stub ignored, typecheck, build, junit reporter, runtime version, simple monorepo |
| P2-11 | SONNET | Python env detection | FR-3, §9.4 | P1-12 | Tests first per rule: uv/poetry/pdm/requirements, extras, pytest vs unittest, mypy/pyright, runtime version |
| P2-12 | SONNET | CI-config parsing + precedence merge + `detection_sources` + EnvProfile persistence + `mpilot detect` | FR-3, §9.4 | P2-10, P2-11 | Override > `mpilot.toml` > CI > heuristics; the EnvProfile is stored in the DB; `mpilot detect` prints valid JSON |
| P2-13 | SONNET | 15 labelled detection fixtures (8 Node, 7 Python) (D1) | FR-3 | P2-12 | ≥ 14/15 test commands correct |
| P2-14 | SONNET | JUnit XML parsing (jest-junit, vitest, pytest) + log-parsing fallback + normalized test IDs | FR-11 | P1-12 | Recorded XML and logs, including parametrized IDs |
| P2-15 | OPUS | Test-file classifier with the full D2 list + test-file SHA-256 hashing | §11.2, FR-14 | P2-14 | Tests first: every D2 pattern, snapshot files, setup files, JUnit-named files and config files classified correctly; hashing stable |
| P2-16 | SONNET | Runnable offline fixtures: py-green, py-flaky, py-red, node-green, node-red | FR-4 | P2-07 | Each behaves as labelled in the sandbox |
| P2-17 | SONNET | Baseline: install → build → typecheck → test ×3; P0, flaky set, test count, hashes; BASELINE_RED; persisted | FR-4, §11.2 | P2-09, P2-12, P2-14, P2-15, P2-16 | py-red → BASELINE_RED; > 5% flaky → BASELINE_RED; in both cases no edit is made and no PR opened (asserted); py-flaky → flaky reported; green → P0 recorded |
| P2-18 | SONNET | Baseline coverage run (D4) | §11.2 (f) | P2-17 | Coverage recorded where measurable, "n/a" otherwise |
| P2-19 | SONNET | `mpilot selftest`, sandbox part | §13.1 | P2-09 | Passes on this host; fails when gVisor or network isolation is disabled (fault-injected) |
| P2-20 | OPUS | **Phase 2 gate** | §16.4 P2 DoD | P2-01…19 | Detection ≥ 14/15; baseline gate works; coverage ≥ 80% on `detect/` and `sandbox/`; no skipped Docker or network tests; spec-reviewer approves the sandbox, network and workspace |

### Phase 3: Deterministic path (B0, B1)
| ID | Tag | Task | Refs | Deps | Acceptance |
|---|---|---|---|---|---|
| P3-01 | SONNET | Registry client (npm, PyPI) | FR-1, §11.4 | P2-20 | respx tests: versions sorted; prereleases excluded from latest stable; yanked PyPI release flagged; deprecated npm version flagged; license and `published_at` parsed; 404 → UnknownPackage |
| P3-02 | SONNET | Goal parsing (C3) → MigrationSpec | FR-1 | P3-01 | 20 fixture goals parse to the expected JSON; a missing target becomes latest stable plus a recorded assumption; unknown package → exit 2 |
| P3-03 | OPUS | Intake: hardened host clone (A2 env and flags), depth ≤ 50, `base_sha`, unauthenticated `repo_info` size check (D8), local paths, mirror creation | FR-2, A2 | P2-20 | > 500 MB refused; a malicious fixture repo's hooks, submodules and LFS filters never run; depth ≤ 50 asserted; `base_sha` recorded |
| P3-04 | OPUS | Pure §7.4 transition table (with END_CANCELLED), tests first | §7.4 | P2-20 | Every legal edge tested; illegal edges raise; PAUSED_LIMIT returns to the previous state |
| P3-05 | OPUS | State-machine driver: handlers, checkpoints, EventBus events, resume skeleton | §7.4, NFR-4 | P3-04 | Kill mid-run in a test, then resume from the checkpoint; events stream in order |
| P3-06 | OPUS | Host diff (`walk_tree` vs mirror at `base_sha`, artifact rules from host-owned data only), apply in the mirror, tree-equality check (i), phase commits, attempt snapshot refs (§9.5) | FR-16, US-4, §9.5 | P3-03 | A diff hidden from sandbox git (skip-worktree, gitignored conftest.py, `diff.external`) still shows up; `node_modules`/`.venv` excluded; a workspace `.gitignore` edit appears in the diff; mirror tree = walked tree; one commit per phase; `restore` from an attempt or phase ref reproduces the tree; a file tracked at `base_sha` under an ignored pattern is never dropped; phase commits = diffs between consecutive snapshots; no host git runs with cwd in the workspace |
| P3-07 | SONNET | Bump with npm, including family bumps | FR-8 | P3-02, P2-17 | Resolved version (`npm ls --json`) satisfies the target; peer conflict → diagnostic |
| P3-08 | SONNET | Bump with pnpm and yarn (classic and berry) | FR-8 | P3-07 | Same as P3-07 per manager |
| P3-09 | SONNET | Bump with pip and uv | FR-8 | P3-02, P2-17 | Resolved version (`pip show`/lockfile) satisfies the target |
| P3-10 | SONNET | Bump with poetry and pdm | FR-8 | P3-09 | Same as P3-09 per manager |
| P3-11 | SONNET | Verification pipeline: install if manifests changed → build → typecheck → targeted → full; rerun-once **for diagnosis only** (C2); suite timeout; classification for C7 escalate-immediately | FR-11, NFR-2, §7.4 | P2-17 | Stage-order tests; a rerun result never changes the judged result; proxy 401 → private-registry escalation category |
| P3-12 | SONNET | `trim_log` (§10.3) + `failure_signature` (FR-12) | FR-12, §10.3 | P2-14 | ANSI and progress bars stripped; ≤ 60 head/tail lines; dedupe by signature; same error with different paths, lines or addresses → same signature |
| P3-13 | OPUS | `final_verify`: whole pipeline in a fresh sandbox from the mirror commit, fresh or hash-verified cache, resolved versions, extra coverage run (§9.5) | §9.5, §11.2 | P3-06, P3-11 | A gitignored `.d.ts` stub, a tampered `node_modules/<pkg>/package.json` or a poisoned cache entry in the work container changes nothing in the final result; success and gate inputs come only from this run |
| P3-14 | SONNET | Codemod registry: schema with `removed_apis` (D3) + 3 entries with flags verified | FR-9 | P3-11 | Schema validation; 3 entries load |
| P3-15 | SONNET | Codemod tools in images + `run_codemod` + own commit | FR-9 | P3-14, P3-06 | Codemods run before any edit; each gets its own commit; a failing codemod doesn't abort |
| P3-16 | SONNET | CLI: `mpilot run --no-pr`, `status`, `logs --follow`, `cancel` | §13.1 | P3-05, P3-07…15 | CLI tests; exit codes 0–3 and cancelled produced from real end states (4 in P4-16, 5 in P4-18, rejected and 130 in P5-10) |
| P3-17 | SONNET | 5 migration fixtures with real old dependencies (network marker) | §16.4 P3 | P3-07…10 | Each has a green baseline and goes red after the bump |
| P3-18 | SONNET | B0 and B1 system configurations + minimal EvalResult row | §14.6 | P3-15, P3-16, P3-17 | B0/B1 end to end on the 5 fixtures |
| P3-19 | OPUS | **Phase 3 gate** | §16.4 P3 DoD | P3-01…18 | Zero model calls (from the LLMCall table); spec-reviewer approves the orchestrator, intake and mirror |

### Phase 4: Thin vertical slice
| ID | Tag | Task | Refs | Deps | Acceptance |
|---|---|---|---|---|---|
| P4-01 | OPUS | Re-verify Appendix A: SDK options (A5), settings isolation, hooks, `max_turns`, per-call effort, thinking, usage and per-model reporting, error types, subscription policy, model IDs, prices, GitHub permissions | App. A, B1, B4, B7, A9 | P3-19 | Each fact in `docs/architecture.md` §12 with date and source; B1 outcome decided (asks the owner if effort can't be set at all) |
| P4-02 | SONNET | Model Router v0 (B10): Opus/high for routed roles, defaults for fixed roles, three policies (B8), RoutingDecision logging | FR-22, §8.4 | P3-19 | Hypothesis tests: never Haiku; never medium in v0; B2/B3 Opus/high under every policy; every LLMCall links to a RoutingDecision |
| P4-03 | SONNET | Read tools: `view_file`, `list_dir`, `search_code`, `find_symbol` v0, `git_status_diff` | §9.1 | P3-19 | Schema tests; `max_results` and `depth` limits enforced |
| P4-04 | SONNET | Write tools: `str_replace`, `create_file`, `delete_file` | FR-10 | P3-19 | Non-unique `old_str` rejected; escapes rejected; every edit records a diff |
| P4-05 | SONNET | Exec tools: `run_command`, `run_build`, `run_typecheck`, `run_tests` | §9.1 | P3-19 | `run_command` runs only in the work container; timeouts ≤ 900 s |
| P4-06 | SONNET | `install_deps` (install container) + `write_note` | §9.1, A7 | P4-05 | Only `install_deps` reaches the registry |
| P4-07 | SONNET | `ToolHost` + in-process MCP server; ToolCall rows, checkpoint per call, `max_tool_calls` | §9.1, NFR-4, §8.3 | P4-03…06 | Registry equals §9.1 + `explore` exactly (asserted); unknown tools rejected; the 16th Editor tool call is refused |
| P4-08 | OPUS | `AgentSDKRuntime` options and lockdown builder (A4, A5) + static check at every start | §8.1, H9, NFR-12 | P4-01, P4-07 | Fake-transport tests: only `mcp__mpilot__*`; full `disallowed_tools` list; no settings sources; temp cwd/HOME/config dir; env holds only the allowed keys; no bypass-permissions mode; deny-by-default callback |
| P4-09 | OPUS | `AgentSDKRuntime` call handling: model and `max_turns` per call; thinking on; usage including cache tokens; `models_seen` guard; usage-limit/rate-limit → `UsageLimitError` | §8.1, §8.3, FR-18, FR-21 | P4-08 | Fake-transport tests for each item; a non-routed model in usage → ForbiddenModelError |
| P4-10 | OPUS | `mpilot selftest`, runtime part + first owner-triggered real check | §8.1, H9 | P4-09 | Real call: built-in tool refused, no user settings loaded, no Haiku used |
| P4-11 | OPUS | Policy hooks 1: role allowlists, `run_command` denylist (A6, incl. environment dumps for H5), Editor non-test rule | §11.1, §9.1, H5 | P4-07 | Tests first: deny and allow cases for each |
| P4-12 | OPUS | Policy hooks 2: test-file writes only in `test_touch`, never delete tests, H4 workflows | H3, H4 | P4-11, P2-15 | Tests first: deny and allow cases for each |
| P4-13 | OPUS | H8 flag policy (A11) | H8, FR-8 | P4-11 | `--force`, `--legacy-peer-deps`, `--no-verify` denied in `install_deps`, `run_command` and bump without a plan justification for that flag, allowed with one; the same options set via `.npmrc`, `.yarnrc*`, workspace `pip.conf` or `npm_config_*`/`PIP_*` are denied; cheating fixture for the config-file route |
| P4-14 | OPUS | Minimal gitleaks redaction of every tool output and every model input (A3) | §9.3, §11.3 | P4-07 | A token planted in tool output, and one planted in a prompt built from repo content, never reach the runtime |
| P4-15 | OPUS | Editor and Diagnoser prompts + schemas with `confidence`; `<untrusted_repo_content>` wrapping; H6; Diagnoser input ≤ 8k tokens | §8.2, §8.3, FR-12 | P4-02, P3-12 | Schema tests; prompt snapshots; scripted FakeRuntime repair |
| P4-16 | OPUS | `BudgetManager` (B2): Σ-over-turns estimate, per-tool-call guard, attempts 6/40, time excluding pauses | NFR-1/2/3, US-5, H7 | P3-05 | Property test: overshoot ≤ one turn; `max_turns` clamped to what fits, refused only if one turn doesn't fit; paused time excluded; the 7th attempt on a step is refused; turn/token/time cap → END_BUDGET_EXCEEDED, exit 4 |
| P4-17 | OPUS | Minimal repair loop: outcome scoring (C1), no-progress, escalate-immediately (C7) | FR-13, §8.3, §7.4 | P4-12…16 | FakeRuntime scenarios: success; no progress after 3 identical signature sets; attempt cap → ESCALATED (`BUDGET_EXHAUSTED`); escalate-immediately; every non-success ending except rejected/cancelled writes `report.md`; termination reason recorded |
| P4-18 | SONNET | UsageLimitError → PAUSED_LIMIT → export → `mpilot resume` (B5) | FR-21, US-6 | P4-17, P2-04 | Pause mid-attempt destroys the sandbox and exits 5; resume finishes; completed calls not repeated; StickyState and counters survive |
| P4-19 | OPUS | Minimal integrity on the final verification run: (a), (b), (c), (g), no new or changed test files or test configs (classifier over the host diff), no diff under `.github/workflows/**` | §11.2, H3, H4 | P3-13, P2-15 | Tests first: failing P0 test, P0 test missing from the run (excluded or renamed), JUnit/exit-code mismatch, deleted test, lower count, unresolved target, edited test file, added `conftest.py` and workflow change → each blocked |
| P4-20 | OPUS | GitHub credentials (A10): PAT provider with a repo list; H1 checks (A9) | §11.3 (1), §11.6, H1 | P1-03 | Token refused for repos off the list; default, protected and non-`mpilot/*` branches refused |
| P4-21 | SONNET | Push from the mirror (refspec, `GIT_ASKPASS`) + idempotent draft PR + minimal body | FR-16, H1, H2 | P4-20, P3-06 | respx tests; a rerun updates the existing PR; the token never appears in `.git/config` or argv; a push of any commit other than the final-verified one is refused |
| P4-22 | SONNET | Cassette recording in the real runtime, secret scan before write (B6) | §18 rule 7 | P4-09, P1-11 | A recorded cassette replays with identical results |
| P4-23 | OPUS | Real e2e: Python repo → draft PR on a PuruSaluja test repo (owner-triggered) | §16.4 P4 DoD | P4-01…22 | Baseline → bump → agent fix → green → draft PR |
| P4-24 | OPUS | Real e2e: Node repo → draft PR (owner-triggered) | §16.4 P4 DoD | P4-23 | Same for Node |
| P4-25 | OPUS | Pause/resume demo + **Phase 4 gate** + re-plan Phases 5–10 using the measured pace | §16.4 P4 DoD | P4-24 | Pause/resume shown on a real run; spec-reviewer approves the runtime lockdown, policy hooks and loop; this list re-sized |

### Phase 5: Widen the agent (provisional)
| ID | Tag | Task | Refs | Acceptance |
|---|---|---|---|---|
| P5-01 | SONNET | Repo map part 1: tree-sitter tags (Python, JS/TS) and reference graph, with file-size caps | FR-5, §10.2 | Tags and graph correct on fixtures |
| P5-02 | SONNET | Repo map part 2: personalized PageRank + budgeted rendering | FR-5, §10.2 | Budget respected (4k / 1.5k); target-importing files ranked first |
| P5-03 | SONNET | Usage index for the target package's APIs | FR-5 | 100% recall of static imports on fixtures |
| P5-04 | OPUS | Host fetch allowlist (A8): no credentials, redirects only to allowlisted hosts, size caps | FR-6, A8 | Off-allowlist host, redirect off the allowlist and oversized response all refused |
| P5-05 | SONNET | Changelog fetch (releases, CHANGELOGs, guides) and chunking | FR-6 | React 17→18, Pydantic 1→2 and Jest 27→29 retrieve the official guides (recorded HTTP); every chunk carries a URL and version |
| P5-06 | SONNET | SQLite FTS5 index + `changelog_search` and `registry_lookup` tools | FR-6, §9.1 | `top_k` ≤ 8; results carry `source_url` |
| P5-07 | SONNET | Explorer role + `explore` tool (orchestrator-mediated, ≤ 20 tool calls) | FR-23, §8.5 | ≤ 1,500 tokens with `path:line` citations; write tools denied; counted in budgets |
| P5-08 | OPUS | Analyzer prompt + `AnalysisReport` schema | §8.2 | FakeRuntime and cassette tests; schema snapshots |
| P5-09 | OPUS | Planner prompt + Plan schema and validation | FR-7 | Schema-valid; every `edit_cluster` cites a usage site or a failure |
| P5-10 | OPUS | `--approve-plan` + `mpilot approve` (AWAITING_APPROVAL, END_REJECTED) | FR-7, CP-1, US-2 | The run blocks until approved; reject → END_REJECTED; interrupt → exit 130 (C6) |
| P5-11 | OPUS | Router v1 part 1: the 8 tier-escalation triggers + stickiness | FR-22, §8.4 | A unit test per trigger; never de-escalates |
| P5-12 | OPUS | Router v1 part 2: strict effort rules + effort escalation | FR-22, §8.4 | A unit test per medium condition and per effort trigger; property tests for every invariant |
| P5-13 | OPUS | Loop guards: oscillation (diff hash), regression revert-to-best (§7.4) from host attempt refs | FR-13, §7.4, §9.5 | Scripted oscillation and regression scenarios; revert restores the best attempt's tree exactly |
| P5-14 | SONNET | Migration notes (≤ 2k), failure history, escalation `report.md`, WIP branch, `needs-human` PR only with `--pr-on-failure` | FR-17, §10.1 | Report lists failing tests, top 3 hypotheses, attempts, budget; no PR without the flag |
| P5-15 | OPUS | Prompt tuning on React 17→18 fixtures (owner-triggered) | §16.4 P5 | L3-interim (D10) on ≥ 1 repo |
| P5-16 | OPUS | Prompt tuning on Pydantic v1→v2 fixtures | §16.4 P5 | L3-interim on ≥ 1 repo |
| P5-17 | OPUS | **Phase 5 gate** | §16.4 P5 DoD | Escalation reports on forced failures; router decisions visible in `mpilot logs`; spec-reviewer approves the router and orchestrator |

### Phase 6: Integrity and safety (provisional)
| ID | Tag | Task | Refs | Acceptance |
|---|---|---|---|---|
| P6-01 | OPUS | Gate checks (a), (b), (c), (g), (i) on the final verification run + `test_touch` hunk mapping | §11.2, §9.5 | Tests first. (i): mirror tree ≠ walked tree blocks. (a): a P0 test missing from the final run blocks. (a): any failure in `P0 \ baseline_flaky` blocks. (b): executed count (D9) below baseline blocks. (c): deleted test file blocks. (g): unresolved target blocks. Hunks outside `test_touch` steps block |
| P6-02 | OPUS | Gate check (d) for Python: skip/xfail markers and assertion changes (regex + AST) | §11.2 (d) | Every Python marker in §11.2 detected; changed assertion constants detected, including inside `test_touch` hunks |
| P6-03 | OPUS | Gate check (d) for JS/TS | §11.2 (d) | `it.skip`, `describe.skip`, `test.only`, `xit` and changed `expect` constants detected |
| P6-04 | OPUS | Gate check (e): test configs (full D2 list) | §11.2 (e) | Exclusions, narrowed discovery, skipping setup files and lowered thresholds always blocked, even if a plan step names them; plan-named format/key migrations allowed and flagged |
| P6-05 | OPUS | Gate check (f): coverage (2 pp gate, 5 pp reported, D4) | §11.2 (f) | A > 2 pp drop blocks; coverage lost since baseline blocks |
| P6-06 | OPUS | Gate check (h): removed-API usages (D3) | §11.2 (h) | A remaining usage of a curated removed API blocks |
| P6-07 | SONNET | Cheating-fixture suite: every FR-14 case plus variants (incl. the A2 hiding tricks) | FR-14 | 100% blocked |
| P6-08 | SONNET | Diff limits + allowed-file policy | NFR-8, §11.3 | Over-limit → escalation, no PR; off-policy file rejected |
| P6-09 | SONNET | Full gitleaks: diff, PR body, all model inputs; a credential added in the diff blocks the PR | §11.3, H5 | Planted secret never reaches a model input |
| P6-10 | SONNET | License and supply-chain checks | §11.4 | Copyleft or unknown license escalates; yanked, deprecated or < 7-day-old targets rejected; dependencies not in the plan rejected |
| P6-11 | SONNET | Ruleset-aware branch check | H1, A9 | respx tests for ruleset-protected branches |
| P6-12 | OPUS | Adversarial pass by a fresh Opus session | §16.4 P6 | Every hole becomes a fixture and is fixed |
| P6-13 | OPUS | **Phase 6 gate** | §16.4 P6 DoD | 100% of fixtures blocked; spec-reviewer approves |

### Phase 7: Review, PR quality, reliability (provisional)
| ID | Tag | Task | Refs | Acceptance |
|---|---|---|---|---|
| P7-01 | OPUS | Critic: fresh context, hunk mapping, masking-pattern checks, cites files it read itself | FR-15, §8.2, §8.5 | Scripted diffs with masking patterns are flagged |
| P7-02 | OPUS | Revise loop (max 1) in the state machine | FR-15, §7.4 | Second revise → ESCALATED; verdict in the PR |
| P7-03 | SONNET | PR Writer + §13.4 template (F4), H8 justifications, routing stats | FR-16, §13.4, H8 | Template validation; deterministic sections snapshot-tested |
| P7-04 | SONNET | Crash resume (kill -9 mid-run): restore the last attempt snapshot, restart the interrupted attempt | NFR-4, §10.1 | Kill-and-resume completes a run; partial effects of the in-flight tool call are gone |
| P7-05 | SONNET | OTel spans + Langfuse export (E4) | NFR-7, FR-18 | Spans for every model and tool call; redacted |
| P7-06 | SONNET | Run replay and export | FR-18, G6 | A stored run replays deterministically |
| P7-07 | OPUS | **Phase 7 gate** | §16.4 P7 DoD | A full run shows Critic verdict, attestation, Opus share and trace link in the PR; true L3 re-checked (D10) |

### Phase 8: API, UI, GitHub App (provisional)
| ID | Tag | Task | Refs | Acceptance |
|---|---|---|---|---|
| P8-01 | SONNET | FastAPI endpoints, static API key, in-process worker queue, bound to 127.0.0.1 | §13.2, E6 | Requests without the key → 401; non-loopback bind refused |
| P8-02 | SONNET | SSE event stream | §13.2 | In order; reconnect works |
| P8-03 | SONNET | UI templates: runs table, run detail | §13.3 | Render tests with fixture data |
| P8-04 | SONNET | UI handlers, HTMX actions, routing panel | §13.3 | Start, watch, approve, resume from the UI |
| P8-05 | SONNET | GitHub App auth: 1-hour token for exactly one repo, minted just before push | §11.6, §11.3 (1) | No Workflows permission; token scoped to one repo; never stored |
| P8-06 | SONNET | FR-19 bot-PR mode (F3) | FR-19 | Starts at step 5; never pushes to the bot branch |
| P8-07 | OPUS | **Phase 8 gate** | §16.4 P8 DoD | UI flow demonstrated |

### Phase 9: Benchmark (provisional)
| ID | Tag | Task | Refs | Acceptance |
|---|---|---|---|---|
| P9-01 | SONNET | Suite schema + harness with batching, pause/resume, `bench resume` | FR-20, US-7 | Batches resume after a simulated limit |
| P9-02 | SONNET | Success levels L0–L3 + EvalResult rows | §14.3 | One test per level boundary |
| P9-03 | SONNET | Metrics + `bench report` (Wilson CIs; CSV, JSON, Markdown) | §14.4, FR-20 | Metric tests on synthetic rows |
| P9-04 | SONNET | B2 single-shot (F1) | §14.6 | Runs on a fixture through the sandbox |
| P9-05 | SONNET | B3 generic agent (F2); gate applied afterwards to count cheats | §14.6 | Runs on a fixture; cheats counted |
| P9-06 | SONNET | Candidate curation tool | §14.2 | Rejects candidates failing any criterion |
| P9-07 | OPUS | Final suite review and freeze of `bench/suites/v1.yaml` | §14.2 | Committed before the final runs |
| P9-08 | SONNET | Review rubric + blinding tool | §14.3 | Blinded packets contain no system labels |
| P9-09 | SONNET | Run B0/B1 | §14.2, FR-20 | All rows present; each repo's baseline status reproduced |
| P9-10 | SONNET | Run Full/B2/B3 in batches | §14.2 | All rows present |
| P9-11 | SONNET | Ablation runs on the 15-task subset | §14.6 | All rows present |
| P9-12 | SONNET | Blinded L4 review of the ablation subset, for the effort guard (D10) | §8.4, §14.3 | Rubric scores recorded |
| P9-13 | OPUS | Apply the §8.4 model and effort quality guards; log in §20 | §8.4 | Decisions logged either way |
| P9-14 | OPUS | Results analysis | §14.7 | Every failure categorized; pre-declared aggregation only |

### Phase 10: Java stretch, write-up (provisional)
| ID | Tag | Task | Refs | Acceptance |
|---|---|---|---|---|
| P10-01 | SONNET | Java detection + jdk images | G2, §9.4 | Maven and Gradle detection fixtures |
| P10-02 | SONNET | 6 Java tasks, reported separately | §14.2 | Rows present |
| P10-03 | SONNET | L4 review of ≥ 20 PRs (owner + peers) | §14.3 | Rubric scores recorded |
| P10-04 | OPUS | Analysis for `docs/results/v1.md` | §14.7 | Numbers only from EvalResult data |
| P10-05 | SONNET-MED | Format `docs/results/v1.md` | §16.4 P10 | Matches the analysis |
| P10-06 | SONNET-MED | README with demo and limitations | §16.4 P10 | States the §14.7 limits |
| P10-07 | OPUS | Optional upstream PRs; fill resume bullets with measured numbers only | §14.2, §19 | Every bullet traceable to data |
