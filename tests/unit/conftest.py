from __future__ import annotations

from collections.abc import Iterator

import pytest

from mpilot.core import github_token


@pytest.fixture(autouse=True)
def _fresh_github_token_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts with no token loaded and no token variables in the environment."""
    monkeypatch.delenv(github_token.GITHUB_TOKEN_ENV, raising=False)
    monkeypatch.delenv("MPILOT_GITHUB_TOKEN_FILE", raising=False)
    github_token._reset_for_tests()
    yield
    github_token._reset_for_tests()
