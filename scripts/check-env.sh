#!/usr/bin/env bash
# Verify the MigrationPilot development host (see docs/requirements.md).
# Read-only: changes nothing. Exit code 0 only if every REQUIRED item passes.
set -uo pipefail

fail=0
pass() { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

# Binaries must be native Linux, never Windows interop shims under /mnt/c.
native() {
  local p
  p="$(command -v "$1" 2>/dev/null)" || return 1
  [[ "$p" != /mnt/* ]]
}

echo "Required now (Phase 0-1)"
if native git; then pass "git $(git --version | awk '{print $3}')"; else bad "git missing"; fi
if native uv || [[ -x "$HOME/.local/bin/uv" ]]; then
  pass "uv $("${HOME}/.local/bin/uv" --version 2>/dev/null | awk '{print $2}' || uv --version | awk '{print $2}')"
else bad "uv missing (curl -LsSf https://astral.sh/uv/install.sh | sh)"; fi
if native python3 && python3 -c 'import sys; sys.exit(sys.version_info[:2] < (3, 12))'; then
  pass "python $(python3 -c 'import platform; print(platform.python_version())')"
else bad "python >= 3.12 missing (uv can provide it: uv python install 3.12)"; fi
case "$PWD" in /mnt/*) bad "repo is on the Windows filesystem ($PWD); use ~/migrationpilot";; *) pass "repo on Linux filesystem";; esac

echo "Required from Phase 2 (sandbox)"
if native docker; then
  if docker info >/dev/null 2>&1; then
    pass "docker engine $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
    ctx="$(docker context show 2>/dev/null)"
    [[ "$ctx" == default ]] && pass "docker context: default (native engine)" \
      || warn "docker context is '$ctx'; expected 'default' (native engine, not Docker Desktop)"
    if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q runsc; then
      pass "gVisor runtime 'runsc' registered"
      if docker run --rm --runtime=runsc busybox:1.36 true >/dev/null 2>&1; then
        pass "container runs under runsc"
      else
        bad "runsc registered but a test container failed (SPEC NFR-10: runc fallback with warning)"
      fi
    else bad "gVisor runtime 'runsc' not registered (run scripts/setup-dev.sh)"; fi
  else
    bad "docker CLI present but daemon unreachable (in docker group? log out/in or: newgrp docker)"
  fi
else bad "native docker missing (run scripts/setup-dev.sh)"; fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runs_dir="${MPILOT_RUNS_DIR:-$repo_root/runs}"
runs_src="$(findmnt -n -o SOURCE --mountpoint "$runs_dir" 2>/dev/null || true)"
if [[ "$runs_src" == /dev/loop* ]] && losetup -n -O BACK-FILE "$runs_src" 2>/dev/null | grep -qx /var/lib/mpilot/runs.img; then
  pass "runs/ on fixed-size filesystem ($(df -h --output=size "$runs_dir" | tail -1 | tr -d ' '))"
else bad "runs/ is not on the fixed-size image /var/lib/mpilot/runs.img (run scripts/setup-dev.sh)"; fi
mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
if (( mem_gb >= 11 )); then pass "WSL memory ${mem_gb} GiB"
else bad "WSL memory ${mem_gb} GiB; set memory=12GB in %UserProfile%\\.wslconfig (SPEC Q10)"; fi

echo "Required from Phase 4+ (PRs, secret scanning)"
if native gh; then
  pass "gh $(gh --version | head -1 | awk '{print $3}')"
  gh auth status >/dev/null 2>&1 && pass "gh authenticated" || warn "gh not logged in (gh auth login) - needed from Phase 4"
else bad "gh missing (run scripts/setup-dev.sh)"; fi
if native gitleaks; then pass "gitleaks installed"; else bad "gitleaks missing (run scripts/setup-dev.sh)"; fi

echo "Not needed on the host (run inside sandbox images)"
for t in node npm java mvn; do
  p="$(command -v "$t" 2>/dev/null || true)"
  [[ "$p" == /mnt/* ]] && warn "$t resolves to a Windows binary ($p); harmless, but never rely on it"
done
pass "Node, Java, Maven, Gradle live in images/{node,python,jdk}"

echo
if [[ $fail -eq 0 ]]; then echo "All required checks passed."; else echo "Some required checks failed."; fi
exit $fail
