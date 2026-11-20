"""Tool policy hooks (SPEC §11.1, §9.1, §9.3; A3, A6, A11).

Pre-policies decide on validated arguments; post-policies rewrite outputs (redaction, A3).
Both are enforced in code by the ToolHost, never only in prompts (§11.1). Concrete
policies arrive in P4-11..P4-14: role allowlists, the `run_command` denylist, test-file
rules (H3), workflows (H4), flags (H8) and gitleaks redaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from mpilot.models.domain import PolicyDecision
from mpilot.tools.base import ToolContext, ToolSpec


class PolicyVerdict(BaseModel):
    """Result of one pre-policy. A denial always says why (`code` for machines, `reason`
    for the model and the ToolCall row)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: PolicyDecision
    policy: str | None = None  # name of the denying policy
    code: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        denied = self.decision is PolicyDecision.DENY
        if denied != all((self.policy, self.code, self.reason)):
            raise ValueError("a denial has policy, code and reason; an allow has none")
        if not denied and any((self.policy, self.code, self.reason)):
            raise ValueError("an allow carries no policy, code or reason")
        return self

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW

    @classmethod
    def allow(cls) -> PolicyVerdict:
        return cls(decision=PolicyDecision.ALLOW)

    @classmethod
    def deny(cls, policy: str, code: str, reason: str) -> PolicyVerdict:
        return cls(decision=PolicyDecision.DENY, policy=policy, code=code, reason=reason)


@runtime_checkable
class ToolPolicy(Protocol):
    """One policy hook. Pure with respect to its inputs plus construction-time data (the
    plan, the classifier): no I/O on the workspace, no model calls."""

    @property
    def name(self) -> str: ...

    def pre(self, spec: ToolSpec, args: BaseModel, ctx: ToolContext) -> PolicyVerdict:
        """Allow or deny one call before it runs. `args` is an instance of
        `spec.input_model`. Deny by returning `PolicyVerdict.deny`; never raise for a
        denial."""
        ...

    def post(self, spec: ToolSpec, output: BaseModel, ctx: ToolContext) -> BaseModel:
        """Rewrite the output before the model sees it, e.g. redact secrets (A3). Must return
        an instance of `spec.output_model`; return `output` unchanged when nothing applies."""
        ...


def run_pre(
    policies: Sequence[ToolPolicy], spec: ToolSpec, args: BaseModel, ctx: ToolContext
) -> PolicyVerdict:
    """Every pre-policy in order; the first denial wins and later policies are not asked."""
    for policy in policies:
        verdict = policy.pre(spec, args, ctx)
        if not verdict.allowed:
            return verdict
    return PolicyVerdict.allow()


def run_post(
    policies: Sequence[ToolPolicy], spec: ToolSpec, output: BaseModel, ctx: ToolContext
) -> BaseModel:
    """Every post-policy in order, each seeing the previous one's output.

    Raises TypeError if a policy returns something other than `spec.output_model`: a
    redactor that breaks the schema is a bug, never something to pass to the model.
    """
    for policy in policies:
        output = policy.post(spec, output, ctx)
        if not isinstance(output, spec.output_model):
            raise TypeError(
                f"policy {policy.name!r} returned {type(output).__name__}, "
                f"not {spec.output_model.__name__}"
            )
    return output
