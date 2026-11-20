"""Gate checks over the final verification run and the commit to be pushed (SPEC §11.2, §9.5).

Every check judges only `GateInput`: the final verification run (a fresh sandbox from the
exact mirror commit), host-computed trees and diffs, and the baseline. Nothing in here comes
from the work container or from sandbox git. Checks are pure: same input, same violations.

Check ids: `integrity.a` ... `integrity.i` (§11.2), later `diff_limits`, `allowed_files`,
`secrets`, `licenses` (§11.3, §11.4, NFR-8). Concrete checks: P4-19 (minimal), P6-01..P6-10.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from mpilot.config import DiffSettings
from mpilot.core.types import StepId
from mpilot.models.artifacts import ApiPattern, FinalVerification, TreeSnapshot, WorkspaceDiff
from mpilot.models.domain import (
    Baseline,
    EnvProfile,
    IntegrityReport,
    MigrationSpec,
    Plan,
    Violation,
)
from mpilot.policy.test_files import TestFileClassifier

#: Check id used for violations the gate evaluator itself finds (not a §11.2 letter).
GATE_CHECK_ID = "gate"
REMOVED_API_CHECK_ID = "integrity.h"


@runtime_checkable
class TreeReader(Protocol):
    """Read-only view of one host mirror commit's tree (never the workspace, never mounted).

    Lets checks read file contents, for (e) config sections and (h) removed-API search,
    while staying pure: the commit is immutable.
    """

    @property
    def commit(self) -> str: ...

    def snapshot(self) -> TreeSnapshot:
        """The commit's tree as path -> entry, the same shape `Workspace.walk_tree` returns.
        Check (i) compares the `commit_tree` snapshot with the walked tree."""
        ...

    def paths(self) -> frozenset[str]:
        """Every path in the tree, symlinks included."""
        ...

    def read_text(self, rel_path: str) -> str | None:
        """UTF-8 text of a regular file, or None if the path is absent, a symlink, binary or
        over the parse size cap. A check must treat None for a file it needs as a violation,
        never as "nothing to check"."""
        ...


@dataclass(frozen=True, slots=True)
class GateInput:
    """Everything the gate may look at (§11.2, §9.5, §11.3, NFR-8).

    Commit binding (checked by `evaluate_gate`, never trusted):
    - `mirror_commit` is the commit to push. `final.mirror_commit`, `commit_tree.commit`
      and `diff.target` must equal it, and `diff.target_digest` must be its tree's digest.
    - `base_sha` is the run's base. `diff.base` and `base_tree.commit` must equal it.
    - `step_diffs`, in plan order, must chain: the first starts at `base_sha`, each starts
      where the previous ended, and the last ends at the pushed tree.

    Fields:
    - `final`: the only test results, resolved versions and coverage a check may use.
    - `walked_tree`: the host walk of the workspace; (i) compares it with
      `commit_tree.snapshot()`.
    - `diff`: `base_sha` -> `mirror_commit`, host-computed.
    - `step_diffs`: the diff each plan step produced, between consecutive host snapshots,
      used to map hunks to `test_touch` steps (d) (P6-01).
    - `base_tree` and `commit_tree`: file contents at `base_sha` and at `mirror_commit`.
    - `classifier`: built from host-owned data at baseline (D2).
    - `allowed_files`: usage sites, implicated diagnosis files, manifests and lockfiles, and
      plan-named configs (§11.3 control 5), computed by the orchestrator.
    - `diff_limits`: NFR-8 caps; over either one escalates and no PR is opened.
    - `flag_justifications`: H8 (A11), flag -> plan-level justification.
    - `removed_api_patterns`: the curated registry list only (D3). Model-extracted APIs never
      appear here.
    """

    spec: MigrationSpec
    baseline: Baseline
    env: EnvProfile
    plan: Plan
    base_sha: str
    mirror_commit: str
    final: FinalVerification
    walked_tree: TreeSnapshot
    diff: WorkspaceDiff
    base_tree: TreeReader
    commit_tree: TreeReader
    classifier: TestFileClassifier
    allowed_files: frozenset[str]
    diff_limits: DiffSettings
    step_diffs: Sequence[tuple[StepId, WorkspaceDiff]] = ()
    flag_justifications: Mapping[str, str] = field(default_factory=dict)
    removed_api_patterns: Sequence[ApiPattern] = ()


@runtime_checkable
class PolicyCheck(Protocol):
    """One gate check. `evaluate` is pure and returns every violation it finds (an empty
    list means pass). Every returned violation carries this check's `id`."""

    @property
    def id(self) -> str: ...

    def evaluate(self, inp: GateInput) -> list[Violation]: ...


def _gate(code: str, message: str) -> Violation:
    return Violation(check_id=GATE_CHECK_ID, code=code, message=message)


def binding_violations(inp: GateInput) -> list[Violation]:
    """Violations if any input is not about the commits it claims to describe (§9.5)."""
    out: list[Violation] = []
    head, base = inp.mirror_commit, inp.base_sha
    for what, got in (
        ("final verification", inp.final.mirror_commit),
        ("commit_tree", inp.commit_tree.commit),
        ("diff target", inp.diff.target),
    ):
        if got != head:
            out.append(
                _gate("commit_mismatch", f"{what} is {got}, but the commit to push is {head}")
            )
    for what, got in (("diff base", inp.diff.base), ("base_tree", inp.base_tree.commit)):
        if got != base:
            out.append(_gate("base_mismatch", f"{what} is {got}, but the run's base is {base}"))
    head_digest = inp.commit_tree.snapshot().digest()
    if inp.diff.target_digest != head_digest:
        out.append(_gate("commit_mismatch", "diff target tree is not the pushed commit's tree"))

    if inp.plan.run_id != inp.baseline.run_id:
        out.append(_gate("run_mismatch", "plan and baseline belong to different runs"))

    order = {s.id: s.order for s in inp.plan.steps}
    seen: set[str] = set()
    last_order = -1
    expected_base = base
    for step_id, d in inp.step_diffs:
        if step_id not in order or step_id in seen:
            out.append(_gate("step_chain", f"step diff for {step_id} is not one plan step"))
        elif order[step_id] <= last_order:
            out.append(_gate("step_chain", f"step diff for {step_id} is out of plan order"))
        else:
            last_order = order[step_id]
        seen.add(step_id)
        if d.base != expected_base:
            out.append(
                _gate("step_chain", f"step {step_id} starts at {d.base}, not {expected_base}")
            )
        expected_base = d.target
    if inp.step_diffs and inp.step_diffs[-1][1].target_digest != head_digest:
        out.append(_gate("step_chain", "the last step diff does not end at the pushed tree"))
    if inp.diff.files and not inp.step_diffs:
        out.append(_gate("step_chain", "the diff has changes that no plan step accounts for"))
    return out


def evaluate_gate(
    checks: Sequence[PolicyCheck], inp: GateInput, *, required: Collection[str]
) -> IntegrityReport:
    """Run every check and build the §12 `IntegrityReport`.

    `required` has no default on purpose: each caller names the checks its phase needs
    (P4-19 minimal set, P6 the full set), so the gate can never pass by running nothing.

    Raises ValueError (a programming error, never a gate result) if there are no checks or
    no required ids, check ids repeat, a `required` check is missing, or a check returns a
    violation under another check's id.
    Adds the evaluator's own `gate` violations from `binding_violations` first: a gate
    judging inputs about some other commit must fail.
    """
    if not checks or not required:
        raise ValueError("the gate needs at least one check and a non-empty required set")
    ids = [c.id for c in checks]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate gate check ids: {sorted(ids)}")
    if GATE_CHECK_ID in ids:
        raise ValueError(f"{GATE_CHECK_ID!r} is reserved for the evaluator")
    missing = set(required) - set(ids)
    if missing:
        raise ValueError(f"required gate checks missing: {sorted(missing)}")

    violations = binding_violations(inp)
    for check in checks:
        found = check.evaluate(inp)
        foreign = [v for v in found if v.check_id != check.id]
        if foreign:
            raise ValueError(f"check {check.id!r} returned violations for {foreign[0].check_id!r}")
        violations.extend(found)

    base_cov, final_cov = inp.baseline.coverage_pct, inp.final.coverage_pct
    return IntegrityReport(
        run_id=inp.baseline.run_id,
        mirror_commit=inp.mirror_commit,
        checks=sorted(ids),
        passed=not violations,
        violations=violations,
        coverage_delta_pp=None if base_cov is None or final_cov is None else final_cov - base_cov,
        test_count_before=inp.baseline.test_count,
        test_count_after=inp.final.tests.executed_count,
        resolved_versions=dict(inp.final.resolved_versions),
        removed_api_usages_remaining=[
            v.message for v in violations if v.check_id == REMOVED_API_CHECK_ID
        ],
    )
