"""P1-02: token estimator (B3), clocks, errors."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mpilot.core.clock import Clock, FakeClock, SystemClock
from mpilot.core.errors import (
    BudgetExceededError,
    ConfigError,
    ForbiddenModelError,
    InputError,
    MpilotError,
    UsageLimitError,
)
from mpilot.core.tokens import estimate_tokens


@pytest.mark.parametrize(
    ("text", "tokens"), [("", 0), ("a", 1), ("abc", 1), ("abcd", 2), ("a" * 9, 3)]
)
def test_estimate_is_ceil_chars_over_three(text: str, tokens: int) -> None:
    assert estimate_tokens(text) == tokens


@given(st.text())
def test_estimate_is_conservative_and_tight(text: str) -> None:
    n = estimate_tokens(text)
    assert n * 3 >= len(text)
    assert (n - 1) * 3 < len(text) or n == 0


def test_estimate_counts_characters_not_bytes() -> None:
    assert estimate_tokens("é" * 3) == 1


def test_fake_clock_moves_only_when_told() -> None:
    clock = FakeClock(datetime(2026, 5, 1, 12, 0, tzinfo=UTC))
    assert clock.now() == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    assert clock.monotonic() == 0.0
    clock.advance(90)
    clock.sleep(30)
    assert clock.now() == datetime(2026, 5, 1, 12, 2, tzinfo=UTC)
    assert clock.monotonic() == 120.0


def test_fake_clock_rejects_backwards_time_and_naive_start() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1))


def test_both_clocks_satisfy_the_protocol() -> None:
    clocks: list[Clock] = [SystemClock(), FakeClock()]
    for clock in clocks:
        assert clock.now().tzinfo is not None
        assert isinstance(clock.monotonic(), float)


def test_system_clock_is_monotonic_and_sleeps() -> None:
    clock = SystemClock()
    before = clock.monotonic()
    clock.sleep(0.01)
    assert clock.monotonic() - before >= 0.009
    assert abs(time.time() - clock.now().timestamp()) < 5


def test_errors_share_one_base() -> None:
    for error in (
        InputError,
        ConfigError,
        UsageLimitError,
        ForbiddenModelError,
        BudgetExceededError,
    ):
        assert issubclass(error, MpilotError)
