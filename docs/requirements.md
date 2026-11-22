# Development requirements

What a machine needs to build and run MigrationPilot, and which phase first needs it.
Set up once with `bash scripts/setup-dev.sh`, then verify with `bash scripts/check-env.sh`.

## Dev host

| Item | Value |
|---|---|
| OS | Ubuntu 24.04 on WSL2 (Windows 11), systemd enabled |
| Repo location | `~/migrationpilot` on the Linux filesystem. Never work under `/mnt/c` (slow I/O, permission and line-ending drift) |
| Editor | VS Code with the WSL extension (`cd ~/migrationpilot && code .`) |
| Docker | **Native Docker Engine inside WSL**, not Docker Desktop. gVisor needs the `runsc` binary installed next to the engine and registered in its `daemon.json`. That's simple with a native engine and awkward inside Docker Desktop's own VM. Keep Docker Desktop's WSL integration **off** for this distro so the two don't fight over `docker` |

## System tools

| Tool | Version | Needed from | Why | Installed by |
|---|---|---|---|---|
| git | any recent | Phase 0 | VCS | Ubuntu (present) |
| uv | ≥ 0.13 | Phase 1 | Python toolchain, venv, lockfile | present (`~/.local/bin`) |
| Python | 3.12 | Phase 1 | Project language (SPEC §15) | system / `uv python install 3.12` |
| Docker Engine + buildx + compose | **28.0 or later** (isolated gateway mode for run networks; mpilot refuses older engines), current stable | Phase 2 | Sandbox (§9.3); compose for self-hosted Langfuse (Phase 7) | `setup-dev.sh` |
| gVisor `runsc` | current release | Phase 2 | Default sandbox runtime on Linux (§9.2). If it fails on the WSL kernel, use runc with a recorded warning (NFR-10) | `setup-dev.sh` |
| GitHub CLI `gh` | current | Phase 4 | Creating test repos/forks, checking PATs by hand. The product itself uses githubkit + a fine-grained PAT (§11.6), not `gh` | `setup-dev.sh` |
| gitleaks | ≥ 8.18 | Phase 6 | Secret scanning of diffs, PR bodies, model inputs (§11.3) | `setup-dev.sh` |
| Claude Code | current | Phase 0 | Build-time agent (this session) | present |
| gcc (build-essential, static glibc) | Ubuntu default | Phase 2 | Tests only: builds a tiny static helper whose main thread exits while another runs, to prove the KILL_ALL freeze stops it (P2-03). Never compiles repo code | Ubuntu (present) |
| busybox:1.36 image | pinned tag | Phase 2 | Tests only: stand-in sandbox image for the Docker tests until P2-07 builds the real images | `setup-dev.sh` |

**Not needed on the host:** Node, npm/pnpm/yarn, Java, Maven, Gradle, ripgrep, the tree-sitter CLI and codemod tools.
They all live in the sandbox images (`images/{node,python,jdk}/Dockerfile`, §9.3), because untrusted repo code never runs on the host (NFR-6).
Windows `node`/`npm` showing up on the WSL `PATH` is harmless, but never rely on it.

## Python packages

These go into `pyproject.toml` in the Phase 1 scaffold task. `uv.lock` pins exact versions, and there is no `requirements.txt`, so the dependency list lives in one place.
Before pinning, re-check every version and API at build time (SPEC Appendix A).

**Runtime**

| Package | Purpose (SPEC §15) |
|---|---|
| `pydantic` ≥ 2 | Data models (§12) |
| `pydantic-settings` | `mpilot.toml` / env config loader |
| `sqlalchemy` ≥ 2, `alembic` | SQLite store (WAL) and migrations |
| `typer` | `mpilot` CLI (§13.1) |
| `fastapi`, `uvicorn`, `sse-starlette` | REST API and SSE (§13.2) |
| `jinja2` | Web UI templates (HTMX and diff2html are vendored static assets, not Python packages) |
| `docker` | Docker SDK for the sandbox driver |
| `claude-agent-sdk` | Default `AgentSDKRuntime` (§8.1) |
| `anthropic` | `MessagesRuntime` (§8.1): interface only, never used in v1 because no API key is used (SPEC Q9) |
| `tree-sitter`, `tree-sitter-language-pack`, `networkx` | Repo map (§10.2) |
| `githubkit` | GitHub REST adapter (§11.6) |
| `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp` | Tracing → Langfuse via OTLP (NFR-7) |
| `pyyaml` | `codemods/registry.yaml`, `bench/suites/v1.yaml`, CI-config parsing |
| `packaging` | PEP 440 version specifiers for `requires-python` (Python env detection, P2-11) |
| `httpx` | Registry/changelog fetches (knowledge layer) |
| `pathspec` | gitignore matching for the host walk's artifact rules (§9.5 Diff; P2-01) |

**Dev**

| Package | Purpose |
|---|---|
| `ruff` | Lint and format |
| `mypy` | `--strict` type checking |
| `pytest`, `pytest-cov` | Tests and the ≥ 80% coverage gates |
| `syrupy` | Snapshot tests |
| `hypothesis` | Property tests (router invariants, budgets) |
| `respx` | HTTP mocking for registry, changelog and GitHub tests |
| `pre-commit` | Runs the checks before each commit |
| `types-pyyaml` | Type stubs for mypy |

## Accounts and credentials

| Item | Needed from | Notes |
|---|---|---|
| Claude Pro subscription | Phase 4 (first real call) | Tests never call real Claude; they use `FakeRuntime` or cassettes |
| GitHub account for test repos and forks | Phase 4 | The personal account `PuruSaluja` (SPEC Q8) |
| Fine-grained PAT: Contents write + Pull requests write, scoped to the test repos only | Phase 4 | Never the Workflows permission (§11.6). Stored on the host only, never in the sandbox |

## Hardware (this machine)

8 CPUs and 16 GB RAM. WSL must see 12 GB (SPEC Q10) so the 8 GB sandbox limit fits. Set it in `%UserProfile%\.wslconfig`:

```
[wsl2]
memory=12GB
```

Then run `wsl --shutdown` in PowerShell and reopen Ubuntu.
Run files (workspaces, mirrors, artifacts, package caches) live in `runs/` on a fixed-size 50 GB filesystem image created by `setup-dev.sh` (sparse, so only used space takes room). Containers can't write anywhere else (read-only root, capped `/tmp`). Docker's own image storage stays in `/var/lib/docker`.
