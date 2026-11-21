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


class AttemptCapError(MpilotError):
    """An NFR-3 attempt cap (per step or per run) was reached. Unlike a turn, token or time
    cap (BudgetExceededError -> END_BUDGET_EXCEEDED), this ends the run as ESCALATED with
    category BUDGET_EXHAUSTED (§7.4 Budget endings)."""


class PathEscapeError(MpilotError):
    """A workspace path tried to leave the workspace, follow a symlink, or open something that
    is not a regular file or directory (§9.5, FR-10). The tool call is denied."""


class ExportIntegrityError(MpilotError, ValueError):
    """A workspace export does not match the digest the DB checkpoint recorded: the manifest was
    swapped, or a file in the export was added, removed or changed (FR-21, §9.5). A `ValueError`
    too, so callers that only care that the restore was refused can say so."""


class IllegalTransition(MpilotError):  # noqa: N818  (the name the architecture uses)
    """The §7.4 state machine has no edge for this (state, event) pair."""


class PushRefusedError(MpilotError):
    """A push was refused: not an `mpilot/*` branch, the default or a protected branch (H1),
    or not the commit final verification ran on (§9.5)."""


class CredentialRefusedError(MpilotError):
    """The credential provider refused a repo that is not on its configured list (§11.3
    control 1, A10)."""


class StoreError(MpilotError, ValueError):
    """The run store refused a write: a duplicate id, a row for a run that does not exist, a
    value the schema's constraints forbid, or a checkpoint that would undo what the previous
    one recorded (B5). A `ValueError` too, so callers that only care that it failed can say so."""


class ScriptDivergedError(RuntimeCallError):
    """A scripted or replayed role call did not match what the code under test did: the script
    ran out, ended without an answer, or a tool call came back differently from what was
    expected. Loud on purpose: a test double that silently improvises proves nothing."""


class CassetteMismatchError(ScriptDivergedError):
    """No recorded cassette entry matches this request (its content hash is unknown, or every
    matching entry was used). The message names which parts of the request changed."""


class LeaseHeldError(StoreError):
    """Another live process holds the run's driver lease: it is being driven (P1-12c)."""


class LeaseLostError(StoreError):
    """A write carried a lease that is no longer the run's current one (taken over after it
    expired, released, or the run ended): the writer must stop driving at once."""


class RunEndedError(StoreError):
    """The run is in a terminal state: there is nothing left to drive."""


class CancelRequestedError(MpilotError):
    """Raised by `RunStore.checkpoint` for any checkpoint but END_CANCELLED once a cancel was
    requested (C6). Nothing was written; the driver tears down and checkpoints END_CANCELLED.
    Not a `StoreError`: it is a control signal, never a refused write to report as an error."""
