# CLAUDE.md: MigrationPilot

## Start of every session
1. Read `SPEC.md` and `PROGRESS.md`. Read nothing else unless the task names it or directly depends on it.
2. If the last session was cut off, run the checks below first and find the exact stopping point in `PROGRESS.md`.
3. Work on exactly ONE task: the "Next task" in `PROGRESS.md`.

## End of every task
1. Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q`
2. Everything must be green. Never move on while anything is red.
3. Update `PROGRESS.md`: Done / In progress / Next task / Blockers / Decisions.
4. Commit using Conventional Commits, one logical change per commit.

## Hard rules
- `SPEC.md` is the source of truth. Changing a [REC] decision means updating SPEC.md in the same change and adding a row to §20.
- Never weaken any §11 guardrail to make something pass. Propose a spec change instead.
- Tests never call real Claude. Use `FakeRuntime` or recorded cassettes. Real calls only in e2e checks the owner explicitly asks for.
- The agent runtime must never expose Claude Code built-in tools (§8.1 lockdown, H9).
- Credentials never enter the sandbox.
- Never fabricate benchmark numbers or resume numbers.
- If a task grows past half a day, stop, split it, and update `PROGRESS.md`.
- Don't scan the whole repo.

## Build order (SPEC §16)
Phase 0 plan → 1 foundations + interfaces → 2 sandbox/detect/baseline → 3 bump/codemods/verify (B0, B1)
→ 4 thin vertical slice (SDK runtime lockdown, repair loop, PR) → 5 widen agent → 6 integrity & safety
→ 7 Critic/PR/reliability → 8 API/UI/App → 9 benchmark → 10 Java + write-up

## Models and quality
- Quality over speed. Never cut corners to save tokens or time.
- Only Opus 5.5 and Sonnet 5.5. Never Haiku, in code or config. Effort is only ever `high` or `medium`.
- Each task is tagged using the rubric in SPEC §16.1: [OPUS] = Opus, high · [SONNET] = Sonnet, high · [SONNET-MED] = Sonnet, medium (logic-free tasks only). Run the session on the model and effort the tag names. If you're on the wrong one, say so at the start.
- Opus always runs at high effort. Never lower effort to save usage.
- Escalate a [SONNET-MED] task to [SONNET] after one red session or as soon as it needs program logic. Escalate a [SONNET] task to [OPUS] after two red Sonnet sessions or when it needs a design decision. Log every escalation in PROGRESS.md.
- In the product, the Model Router picks effort as well as model (SPEC §8.4, strict rules). Medium is never allowed for Opus, the Planner, Critic, Diagnoser or Analyzer, or `test_touch` steps.

## Subagents (.claude/agents/, SPEC §16.2)
- `test-runner` (Sonnet, medium): run checks through it so long logs stay out of the main context.
- `explorer` (Sonnet, medium): use it to find code instead of reading broadly yourself. Verify anything surprising yourself.
- `implementer` (Sonnet, high): in [OPUS] tasks, design first and write critical code yourself, then hand routine parts over as a precise brief (exact files, signatures, acceptance tests).
- `spec-reviewer` (Opus, high): run at every phase gate and after [OPUS] tasks touching §11, the orchestrator, the Model Router or the runtime lockdown.
- Never delegate one-file changes or vague work. Subagents start cold.

## Stack
Python 3.12, uv, ruff, mypy --strict, pytest, Pydantic v2, SQLAlchemy 2 + SQLite, FastAPI, Docker SDK (+ gVisor), claude-agent-sdk, tree-sitter, OpenTelemetry → Langfuse.
