"""Docker labels on everything mpilot creates for a run, so `reap` can find it (§10.1)."""

from __future__ import annotations

from typing import Final

LABEL_RUN: Final = "mpilot.run_id"
LABEL_ROLE: Final = "mpilot.role"
ROLE_WORK: Final = "work"
ROLE_INSTALL: Final = "install"
ROLE_PROXY: Final = "proxy"
ROLE_NETWORK: Final = "internal-network"
