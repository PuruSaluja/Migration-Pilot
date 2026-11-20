"""pytest plugin enforcing the E1 test-marker policy (docs/decisions.md E1).

- `docker`: needs a Docker daemon. Without one the test FAILS, never silently skips.
  Skipping requires an explicit `MPILOT_SKIP_DOCKER=1`; phase gates reject any skip.
- `network`: downloads from registries. Opt-in with `MPILOT_NETWORK_TESTS=1`.
- `e2e`: real Claude calls. Deselected unless selected with `-m` (owner-triggered only).

Loaded through `addopts = -p mpilot.testing.policy` in pyproject.toml.
"""

from __future__ import annotations

import os
from functools import cache

import pytest

SKIP_DOCKER_ENV = "MPILOT_SKIP_DOCKER"
NETWORK_TESTS_ENV = "MPILOT_NETWORK_TESTS"

_MARKERS = (
    "docker: needs a working Docker daemon; fails (not skips) without one "
    f"unless {SKIP_DOCKER_ENV}=1",
    f"network: downloads from registries; runs only with {NETWORK_TESTS_ENV}=1",
    "e2e: real Claude calls; deselected unless selected with -m e2e, owner-triggered only",
)


def _flag(name: str) -> bool:
    return os.environ.get(name) == "1"


@cache
def docker_available() -> bool:
    """True if a Docker daemon answers a ping (probed once per session)."""
    try:
        import docker

        client = docker.from_env(timeout=5)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


def pytest_configure(config: pytest.Config) -> None:
    for line in _MARKERS:
        config.addinivalue_line("markers", line)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect e2e tests unless the user's `-m` expression mentions e2e."""
    if "e2e" in (config.getoption("markexpr") or ""):
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        (deselected if item.get_closest_marker("e2e") else selected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("network") and not _flag(NETWORK_TESTS_ENV):
        pytest.skip(f"network test: set {NETWORK_TESTS_ENV}=1 to run")
    if item.get_closest_marker("docker") and not docker_available():
        if _flag(SKIP_DOCKER_ENV):
            pytest.skip(f"Docker unavailable and {SKIP_DOCKER_ENV}=1")
        pytest.fail(
            f"Docker is not available. Start it, or set {SKIP_DOCKER_ENV}=1 to skip "
            "Docker tests explicitly (phase gates reject skips).",
            pytrace=False,
        )
