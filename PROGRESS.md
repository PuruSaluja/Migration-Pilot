# PROGRESS.md

## Current phase
Phase 0: Plan

## Next task
[OPUS] P0-1: Turn SPEC.md into the full task list (≤ ½ day each, each tagged [OPUS], [SONNET] or [SONNET-MED] by the §16.1 rubric, mapped to FR/NFR IDs, with acceptance tests), interface signatures, and an ambiguity list. Write the task list into this file. Wait for owner approval.

## In progress
(none)

## Done
- Pre-P0 env prep: `docs/requirements.md` (host tools, Python deps, accounts), `scripts/setup-dev.sh` (Docker Engine + gVisor + gh + gitleaks), `scripts/check-env.sh` (read-only verifier). Host OS resolved as SPEC Q7.

## Blockers
- Not blocking P0-1. Owner must run `bash scripts/setup-dev.sh` (needs sudo) before Phase 2, then `bash scripts/check-env.sh` must pass. If `runsc` fails on the WSL kernel, log it here and use the runc fallback (NFR-10).
- Open (SPEC §17.3): which GitHub account/org hosts test repos and forks. Needed before Phase 4.

## Decisions (short notes; formal ones go in SPEC.md §20)
- 2026-11-19: SPEC v1.3 adds strict effort routing (medium/high) for both the product router (§8.4) and the build process (§16.1 [SONNET-MED] tag, §16.2 subagent efforts). See §20.
- 2026-11-19: Dev host = Ubuntu 24.04 / WSL2, native Docker Engine + gVisor, not Docker Desktop (SPEC Q7, §20). No requirements.txt: Python deps go in pyproject.toml + uv.lock in Phase 1; the planned list is in docs/requirements.md.

## Task list
(to be filled in Phase 0)
