#!/usr/bin/env bash
# One-time setup of the MigrationPilot dev host: Ubuntu 24.04 on WSL2.
# Installs native Docker Engine + gVisor (runsc), GitHub CLI and gitleaks.
# Idempotent: safe to re-run. Run as your normal user; it calls sudo itself.
#   bash scripts/setup-dev.sh
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run as your normal user, not root (the script uses sudo where needed)." >&2
  exit 1
fi
. /etc/os-release
if [[ "${ID}" != ubuntu ]]; then
  echo "This script targets Ubuntu (found ${ID}). See docs/requirements.md for manual steps." >&2
  exit 1
fi
ARCH="$(dpkg --print-architecture)"
KEYRINGS=/etc/apt/keyrings
sudo install -m 0755 -d "$KEYRINGS"
sudo apt-get update -q
sudo apt-get install -y -q ca-certificates curl gnupg

# --- Docker Engine (native, inside WSL; NOT Docker Desktop) -----------------
if ! dpkg -s docker-ce >/dev/null 2>&1; then
  echo ">> Installing Docker Engine"
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$KEYRINGS/docker.asc"
  sudo chmod a+r "$KEYRINGS/docker.asc"
  echo "deb [arch=${ARCH} signed-by=${KEYRINGS}/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo systemctl enable --now docker
if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  NEED_RELOGIN=1
fi

# --- gVisor (runsc) registered as a Docker runtime --------------------------
if ! command -v runsc >/dev/null 2>&1; then
  echo ">> Installing gVisor"
  curl -fsSL https://gvisor.dev/archive.key | sudo gpg --batch --yes --dearmor -o "$KEYRINGS/gvisor-archive-keyring.gpg"
  echo "deb [arch=${ARCH} signed-by=${KEYRINGS}/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    | sudo tee /etc/apt/sources.list.d/gvisor.list >/dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q runsc
fi
sudo runsc install          # adds the "runsc" runtime to /etc/docker/daemon.json
sudo systemctl restart docker

# --- GitHub CLI --------------------------------------------------------------
if ! command -v gh >/dev/null 2>&1; then
  echo ">> Installing GitHub CLI"
  sudo curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$KEYRINGS/githubcli-archive-keyring.gpg"
  sudo chmod go+r "$KEYRINGS/githubcli-archive-keyring.gpg"
  echo "deb [arch=${ARCH} signed-by=${KEYRINGS}/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q gh
fi

# --- gitleaks ----------------------------------------------------------------
if ! command -v gitleaks >/dev/null 2>&1; then
  echo ">> Installing gitleaks"
  sudo apt-get install -y -q gitleaks
fi

# --- Smoke test --------------------------------------------------------------
echo ">> Smoke test: container under gVisor"
if sudo docker run --rm --runtime=runsc busybox:1.36 uname -a; then
  echo "gVisor works."
else
  echo "WARNING: runsc failed on this WSL kernel. Per SPEC NFR-10 the sandbox falls back to runc" >&2
  echo "with a recorded warning. Tell Claude Code so it can be logged in PROGRESS.md." >&2
fi

echo
echo "Done."
if [[ "${NEED_RELOGIN:-0}" == 1 ]]; then
  echo "You were added to the 'docker' group: close this terminal and open a new one"
  echo "(or run: wsl --shutdown from Windows, then reopen) before using docker without sudo."
fi
echo "Next: gh auth login   (only needed from Phase 4)"
echo "Then: bash scripts/check-env.sh"
