from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mpilot.core import github_token


@pytest.fixture(autouse=True)
def _fresh_github_token_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Every test starts with no token loaded and no token variables in the environment."""
    monkeypatch.delenv(github_token.GITHUB_TOKEN_ENV, raising=False)
    for name in [n for n in os.environ if n.startswith("MPILOT_") and "__" in n[7:]]:
        monkeypatch.delenv(name)  # config variables from the developer's shell
    for name in ("MPILOT_CONFIG", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no ~/.config/mpilot/mpilot.toml
    monkeypatch.chdir(tmp_path)
    github_token._reset_for_tests()
    yield
    github_token._reset_for_tests()
