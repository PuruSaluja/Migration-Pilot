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


class LockdownViolation(MpilotError):  # noqa: N818  (the name SPEC §8.1/architecture use)
    """The agent runtime is not locked down as §8.1 items 1-10 require (H9).

    Raised by the static check at every runtime start, and when a call ran without extended
    thinking (§8.1 item 10). Never caught to carry on: the run ends in END_ERROR.
    """


class RuntimeCallError(MpilotError):
    """A role call failed for a reason other than a usage limit, a forbidden model or the
    lockdown: transport failure, a result that contradicts its request, and so on."""
