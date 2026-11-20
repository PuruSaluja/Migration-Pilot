"""Exceptions shared across layers. Later tasks add their own subclasses of `MpilotError`."""

from __future__ import annotations


class MpilotError(Exception):
    """Base class for every error MigrationPilot raises on purpose."""


class InputError(MpilotError):
    """Bad user input (unknown package, bad goal, bad flags). The CLI exits 2 (§13.1)."""


class ConfigError(MpilotError):
    """Invalid configuration, including NFR-12 violations (Haiku, bad effort)."""


class UsageLimitError(MpilotError):
    """Subscription usage or rate limit hit (FR-21). The run pauses; it never downgrades."""


class ForbiddenModelError(MpilotError):
    """A call used a model other than the routed one (NFR-12, §8.1 item 9)."""


class BudgetExceededError(MpilotError):
    """A turn, token or time cap was crossed (NFR-1, H7)."""
