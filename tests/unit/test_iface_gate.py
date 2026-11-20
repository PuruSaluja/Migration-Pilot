"""P1-07: gate interfaces (SPEC §11.2, §9.5): GateInput, PolicyCheck, TestFileClassifier and
the host-computed artifact types they judge."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mpilot.config import DiffSettings
from mpilot.core.types import PlanId, RunId, StepId, StepType
from mpilot.models import artifacts as art
from mpilot.models.domain import (
    Baseline,
    Ecosystem,
    EnvProfile,
    MigrationKind,
    MigrationSpec,
    PackageChange,
    Plan,
    Step,
    TestResult,
    TestStage,
    VerifyResult,
    VerifyStage,
    VerifyStageResult,
)
from mpilot.policy import gate
from mpilot.policy import test_files as tf
from mpilot.testing.doubles import ConstantCheck, DictTree, SetClassifier, violation

SHA_A = "a" * 40
SHA_B = "b" * 40
H1 = "1" * 64
H2 = "2" * 64
RUN = RunId("run-1")
ANY = [ConstantCheck(id="integrity.a")]
REQ = ("integrity.a",)
STEP_1 = Step(id=StepId("s1"), order=0, type=StepType.BUMP, description="bump", rationale="r")
STEP_2 = Step(
    id=StepId("s2"), order=1, type=StepType.EDIT_CLUSTER, description="fix", rationale="r"
)


def _tests(stage: TestStage, **kw: Any) -> TestResult:
    base: dict[str, Any] = {
        "run_id": RUN,
        "stage": stage,
        "passed": 3,
        "failed": 0,
        "skipped": 1,
        "errors": 0,
    }
    base["duration_s"] = 1.0
    base.update(kw)
    return TestResult(**base)


GREEN_STAGES = [
    VerifyStageResult(stage=stage, passed=True)
    for stage in (VerifyStage.INSTALL, VerifyStage.BUILD, VerifyStage.TYPECHECK, VerifyStage.TESTS)
]


def _final(**kw: Any) -> art.FinalVerification:
    base: dict[str, Any] = {
        "mirror_commit": SHA_B,
        "verify": VerifyResult(stages=GREEN_STAGES, tests=_tests(TestStage.FINAL)),
        "runner_exit_code": 0,
        "runner_summary": art.TestCounts(passed=3, failed=0, skipped=1, errors=0),
        "resolved_versions": {"react": "18.3.1"},
        "coverage_pct": 80.0,
        "cache": "fresh",
    }
    base.update(kw)
    return art.FinalVerification(**base)


COMMIT_TREE = DictTree(commit=SHA_B, files={"src/a.py": "print('hi')\n"})
SHA_C = "c" * 40


def _diff(base: str = SHA_A, target: str = SHA_B, digest: str | None = None) -> art.WorkspaceDiff:
    digest = COMMIT_TREE.snapshot().digest() if digest is None else digest
    return art.WorkspaceDiff(base=base, target=target, target_digest=digest)


def _input(**kw: Any) -> gate.GateInput:
    base: dict[str, Any] = {
        "spec": MigrationSpec(
            kind=MigrationKind.BUMP,
            ecosystem=Ecosystem.NODE,
            packages=[PackageChange(name="react", to_version="18.3.1")],
            goal_text="react 18",
        ),
        "baseline": Baseline(
            run_id=RUN,
            stages=[VerifyStageResult(stage=VerifyStage.INSTALL, passed=True)],
            runs=[_tests(TestStage.BASELINE)],
            passing_ids=frozenset({"t1"}),
            flaky_ids=frozenset(),
            test_file_hashes={},
            test_count=3,
            coverage_pct=81.5,
        ),
        "env": EnvProfile(
            language=Ecosystem.NODE,
            runtime_version="20",
            package_manager="npm",
            install_cmd="npm ci",
            test_cmd="npm test",
            image_digest="sha256:" + H1,
        ),
        "plan": Plan(id=PlanId("plan-1"), run_id=RUN, steps=[STEP_1, STEP_2]),
        "base_sha": SHA_A,
        "mirror_commit": SHA_B,
        "final": _final(),
        "walked_tree": COMMIT_TREE.snapshot(),
        "diff": _diff(),
        "base_tree": DictTree(commit=SHA_A),
        "commit_tree": COMMIT_TREE,
        "classifier": SetClassifier(),
        "allowed_files": frozenset({"src/a.py"}),
        "diff_limits": DiffSettings(),
    }
    base.update(kw)
    return gate.GateInput(**base)


# ---------------------------------------------------------------- conformance


def test_doubles_conform() -> None:
    assert isinstance(SetClassifier(), tf.TestFileClassifier)
    assert isinstance(DictTree(commit=SHA_A), gate.TreeReader)
    assert isinstance(ConstantCheck(id="integrity.a"), gate.PolicyCheck)


def test_classifier_predicates_are_independent() -> None:
    c = SetClassifier(
        test_files=frozenset({"tests/conftest.py"}), test_configs=frozenset({"tests/conftest.py"})
    )
    assert c.is_test_file("tests/conftest.py")
    assert c.is_test_config("tests/conftest.py")
    assert not c.may_embed_test_config("tests/conftest.py")


# ---------------------------------------------------------------- artifact types


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "a/../b",
        "./a",
        "a//b",
        "a/",
        ".git/config",
        ".git",
        "a\\b",
        "a\x00b",
        "..",
    ],
)
def test_rel_path_rejects(path: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        art.check_rel_path(path)


@pytest.mark.parametrize(
    "path", ["a.py", "src/.gitignore", ".github/workflows/ci.yml", "x/.git-keep", "a/.git/b"]
)
def test_rel_path_accepts(path: str) -> None:
    assert art.check_rel_path(path) == path


def test_tree_snapshot_digest_ignores_insertion_order() -> None:
    e1, e2 = art.TreeEntry(mode="100644", sha256=H1), art.TreeEntry(mode="120000", sha256=H2)
    a = art.TreeSnapshot(entries={"a": e1, "b/link": e2})
    b = art.TreeSnapshot(entries={"b/link": e2, "a": e1})
    assert a == b
    assert a.digest() == b.digest()
    c = art.TreeSnapshot(entries={"a": e1, "b/link": art.TreeEntry(mode="100644", sha256=H2)})
    assert c.digest() != a.digest()
    with pytest.raises(ValidationError):
        art.TreeSnapshot(entries={".git/HEAD": e1})
    with pytest.raises(ValidationError):
        art.TreeEntry(mode="100644", sha256="XYZ")
    with pytest.raises(ValidationError):
        art.TreeEntry(mode="040000", sha256=H1)  # type: ignore[arg-type]


def test_file_change_shapes() -> None:
    e1, e2 = art.TreeEntry(mode="100644", sha256=H1), art.TreeEntry(mode="100644", sha256=H2)
    art.FileChange(path="a", kind=art.ChangeKind.ADDED, new=e1)
    art.FileChange(path="a", kind=art.ChangeKind.DELETED, old=e1)
    art.FileChange(path="a", kind=art.ChangeKind.MODIFIED, old=e1, new=e2)
    for bad in [
        {"kind": art.ChangeKind.ADDED, "old": e1, "new": e2},
        {"kind": art.ChangeKind.ADDED},
        {"kind": art.ChangeKind.DELETED, "new": e1},
        {"kind": art.ChangeKind.DELETED, "old": e1, "new": e2},
        {"kind": art.ChangeKind.MODIFIED, "old": e1},
        {"kind": art.ChangeKind.MODIFIED, "old": e1, "new": e1},
        {
            "kind": art.ChangeKind.ADDED,
            "new": e1,
            "binary": True,
            "hunks": [art.Hunk(old_start=0, old_lines=0, new_start=1, new_lines=1, text="+x")],
        },
    ]:
        with pytest.raises(ValidationError):
            art.FileChange(path="a", **bad)


def test_workspace_diff_is_sorted_and_unique() -> None:
    e = art.TreeEntry(mode="100644", sha256=H1)
    add = art.FileChange(path="b", kind=art.ChangeKind.ADDED, new=e)
    rm = art.FileChange(path="a", kind=art.ChangeKind.DELETED, old=e)
    d = art.WorkspaceDiff(base=SHA_A, target=SHA_B, target_digest=H2, files=[rm, add])
    assert d.paths() == {"a", "b"}
    assert d.paths(art.ChangeKind.DELETED) == {"a"}
    with pytest.raises(ValidationError, match="sorted"):
        art.WorkspaceDiff(base=SHA_A, target=SHA_B, target_digest=H2, files=[add, rm])
    with pytest.raises(ValidationError, match="sorted"):
        art.WorkspaceDiff(base=SHA_A, target=SHA_B, target_digest=H2, files=[rm, rm])
    with pytest.raises(ValidationError, match="git object id"):
        art.WorkspaceDiff(base="HEAD", target=SHA_B, target_digest=H2)
    with pytest.raises(ValidationError, match="git object id"):
        art.WorkspaceDiff(base=SHA_A, target="main", target_digest=H2)
    with pytest.raises(ValidationError, match="target_digest"):
        art.WorkspaceDiff(base=SHA_A, target=SHA_B, target_digest="abc")


def test_final_verification_judges_only_a_final_run() -> None:
    with pytest.raises(ValidationError, match="stage == final"):
        _final(verify=VerifyResult(stages=GREEN_STAGES, tests=_tests(TestStage.FULL)))
    with pytest.raises(ValidationError, match="from install"):
        _final(verify=VerifyResult(stages=GREEN_STAGES[1:], tests=_tests(TestStage.FINAL)))
    with pytest.raises(ValidationError, match="from install"):
        _final(verify=VerifyResult())
    with pytest.raises(ValidationError):
        _final(tests=_tests(TestStage.FINAL))  # one copy only: the run lives in verify.tests
    with pytest.raises(ValidationError):
        _final(cache="reused")
    with pytest.raises(ValidationError, match="git object id"):
        _final(mirror_commit="main")


def test_api_pattern_must_compile() -> None:
    art.ApiPattern(api="ReactDOM.render", ecosystem=Ecosystem.NODE, pattern=r"ReactDOM\.render\(")
    with pytest.raises(ValidationError, match="compile"):
        art.ApiPattern(api="x", ecosystem=Ecosystem.NODE, pattern="(")


# ---------------------------------------------------------------- evaluate_gate


def test_gate_passes_with_clean_checks() -> None:
    report = gate.evaluate_gate(
        [ConstantCheck(id="integrity.a"), ConstantCheck(id="integrity.i")],
        _input(),
        required=("integrity.a", "integrity.i"),
    )
    assert report.passed
    assert report.violations == []
    assert report.run_id == RUN
    assert (report.test_count_before, report.test_count_after) == (3, 3)
    assert report.coverage_delta_pp == pytest.approx(-1.5)
    assert report.resolved_versions == {"react": "18.3.1"}


def test_the_report_names_the_commit_it_judged_and_the_checks_that_ran() -> None:
    """P1-12 review B1/S1: the push choke point compares `mirror_commit` with the commit it
    pushes, and the PR attestation lists `checks`."""
    inp = _input()
    report = gate.evaluate_gate(
        [ConstantCheck(id="integrity.i"), ConstantCheck(id="integrity.a")],
        inp,
        required=("integrity.a",),
    )
    assert report.mirror_commit == inp.mirror_commit
    assert report.checks == ["integrity.a", "integrity.i"]  # every check that ran, sorted


def test_gate_collects_violations_and_removed_api_usages() -> None:
    h = violation("integrity.h", "removed_api", "src/a.js:3 ReactDOM.render")
    checks = [
        ConstantCheck(id="integrity.c", violations=(violation("integrity.c"),)),
        ConstantCheck(id="integrity.h", violations=(h,)),
    ]
    report = gate.evaluate_gate(checks, _input(), required=("integrity.c",))
    assert not report.passed
    assert [v.check_id for v in report.violations] == ["integrity.c", "integrity.h"]
    assert report.removed_api_usages_remaining == ["src/a.js:3 ReactDOM.render"]


@pytest.mark.parametrize(
    ("override", "code", "fragment"),
    [
        ({"final": _final(mirror_commit=SHA_C)}, "commit_mismatch", "final verification"),
        (
            {"commit_tree": DictTree(commit=SHA_C, files=COMMIT_TREE.files)},
            "commit_mismatch",
            "commit_tree",
        ),
        ({"diff": _diff(target=SHA_C)}, "commit_mismatch", "diff target"),
        ({"diff": _diff(digest=H2)}, "commit_mismatch", "tree"),
        ({"diff": _diff(base=SHA_C)}, "base_mismatch", "diff base"),
        ({"base_tree": DictTree(commit=SHA_C)}, "base_mismatch", "base_tree"),
    ],
)
def test_gate_binds_every_input_to_its_commit(
    override: dict[str, Any], code: str, fragment: str
) -> None:
    """Review #4: a gate judging inputs about some other commit must fail (§9.5)."""
    report = gate.evaluate_gate(ANY, _input(**override), required=REQ)
    assert not report.passed
    assert [(v.check_id, v.code) for v in report.violations] == [(gate.GATE_CHECK_ID, code)]
    assert fragment in report.violations[0].message


def test_step_diffs_must_chain_from_base_to_the_pushed_tree() -> None:
    head = COMMIT_TREE.snapshot().digest()
    d = "d" * 40
    good = (
        (STEP_1.id, art.WorkspaceDiff(base=SHA_A, target=SHA_C, target_digest=H1)),
        (STEP_2.id, art.WorkspaceDiff(base=SHA_C, target=d, target_digest=head)),
    )
    assert gate.evaluate_gate(ANY, _input(step_diffs=good), required=REQ).passed
    gap = (good[0], (STEP_2.id, art.WorkspaceDiff(base=SHA_B, target=d, target_digest=head)))
    wrong_start = ((STEP_1.id, art.WorkspaceDiff(base=SHA_C, target=SHA_B, target_digest=head)),)
    wrong_end = (good[0], (STEP_2.id, art.WorkspaceDiff(base=SHA_C, target=d, target_digest=H2)))
    unknown = (good[0], (StepId("nope"), good[1][1]))
    out_of_order = (
        (STEP_2.id, art.WorkspaceDiff(base=SHA_A, target=SHA_C, target_digest=H1)),
        (STEP_1.id, art.WorkspaceDiff(base=SHA_C, target=d, target_digest=head)),
    )
    for bad in (gap, wrong_start, wrong_end, unknown, out_of_order):
        report = gate.evaluate_gate(ANY, _input(step_diffs=bad), required=REQ)
        assert [v.code for v in report.violations] == ["step_chain"], bad


def test_a_later_segment_may_return_to_an_earlier_step() -> None:
    """S4 (P1-12d): a repair after a failed INTEGRITY_CHECK, or a Critic revise, attributed
    to an earlier step (a `test_touch` one included) is a new segment, not a broken chain."""
    head = COMMIT_TREE.snapshot().digest()
    d, e = "d" * 40, "e" * 40
    segments = (
        (STEP_1.id, art.WorkspaceDiff(base=SHA_A, target=SHA_C, target_digest=H1)),
        (STEP_2.id, art.WorkspaceDiff(base=SHA_C, target=d, target_digest=H2)),
        (STEP_1.id, art.WorkspaceDiff(base=d, target=e, target_digest=head)),  # the repair
    )
    assert gate.evaluate_gate(ANY, _input(step_diffs=segments), required=REQ).passed
    twice = (segments[0], (STEP_1.id, art.WorkspaceDiff(base=SHA_C, target=d, target_digest=head)))
    assert gate.evaluate_gate(ANY, _input(step_diffs=twice), required=REQ).passed
    broken = (
        *segments[:2],
        (STEP_1.id, art.WorkspaceDiff(base=SHA_C, target=e, target_digest=head)),
    )
    report = gate.evaluate_gate(ANY, _input(step_diffs=broken), required=REQ)
    assert [v.code for v in report.violations] == ["step_chain"]  # still must chain


def test_gate_check_id_is_reserved() -> None:
    with pytest.raises(ValueError, match="reserved"):
        gate.evaluate_gate([ConstantCheck(id=gate.GATE_CHECK_ID)], _input(), required=("gate",))


def test_gate_coverage_delta_is_none_when_not_measured() -> None:
    report = gate.evaluate_gate(ANY, _input(final=_final(coverage_pct=None)), required=REQ)
    assert report.coverage_delta_pp is None


@pytest.mark.parametrize(
    ("checks", "required", "match"),
    [
        ([ConstantCheck(id="integrity.a"), ConstantCheck(id="integrity.a")], REQ, "duplicate"),
        ([], REQ, "at least one check"),
        ([ConstantCheck(id="integrity.a")], (), "at least one check"),
        ([ConstantCheck(id="integrity.a")], ("integrity.a", "integrity.i"), "missing"),
        (
            [ConstantCheck(id="integrity.a", violations=(violation("integrity.b"),))],
            REQ,
            "returned violations for",
        ),
    ],
)
def test_gate_programming_errors_raise(
    checks: list[ConstantCheck], required: tuple[str, ...], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        gate.evaluate_gate(checks, _input(), required=required)


def test_gate_input_is_immutable() -> None:
    inp = _input()
    with pytest.raises(AttributeError):
        inp.mirror_commit = SHA_A  # type: ignore[misc]


def test_changes_no_step_accounts_for_are_flagged() -> None:
    e = art.TreeEntry(mode="100644", sha256=H1)
    changed = _diff().model_copy(
        update={"files": [art.FileChange(path="src/a.py", kind=art.ChangeKind.ADDED, new=e)]}
    )
    report = gate.evaluate_gate(ANY, _input(diff=changed), required=REQ)
    assert [v.code for v in report.violations] == ["step_chain"]


def test_plan_from_another_run_is_flagged() -> None:
    other = Plan(id=PlanId("plan-2"), run_id=RunId("run-other"), steps=[STEP_1, STEP_2])
    report = gate.evaluate_gate(ANY, _input(plan=other), required=REQ)
    assert [v.code for v in report.violations] == ["run_mismatch"]


def test_steps_cannot_share_an_order_value_or_an_id() -> None:
    """Steps begin in plan order (§9.5), so the plan itself refuses ambiguous orders."""
    twin = STEP_2.model_copy(update={"order": STEP_1.order})
    with pytest.raises(ValueError, match="orders are unique"):
        Plan(id=PlanId("plan-1"), run_id=RUN, steps=[STEP_1, twin])
    same_id = STEP_2.model_copy(update={"id": STEP_1.id})
    with pytest.raises(ValueError, match="ids are unique"):
        Plan(id=PlanId("plan-1"), run_id=RUN, steps=[STEP_1, same_id])


def test_a_final_run_red_before_the_tests_fails_the_gate() -> None:
    """Review B1 (P1-12d): no test run to judge is a gate failure, with no executed tests."""
    red = _final(
        verify=VerifyResult(
            stages=[
                VerifyStageResult(stage=VerifyStage.INSTALL, passed=True),
                VerifyStageResult(stage=VerifyStage.BUILD, passed=False),
            ]
        )
    )
    report = gate.evaluate_gate(ANY, _input(final=red), required=REQ)
    assert [v.code for v in report.violations] == ["final_red"]
    assert "build" in report.violations[0].message
    assert report.test_count_after == 0


@pytest.mark.parametrize(
    ("env_update", "stages", "codes"),
    [
        (
            {"build_cmd": "npm run build"},
            [VerifyStage.INSTALL, VerifyStage.TESTS],
            ["final_incomplete"],
        ),
        ({"typecheck_cmd": "tsc"}, [VerifyStage.INSTALL, VerifyStage.TESTS], ["final_incomplete"]),
        ({}, [VerifyStage.INSTALL, VerifyStage.TESTS], []),  # the repo has no build/typecheck
        ({}, [VerifyStage.INSTALL, VerifyStage.BUILD], ["final_red"]),  # never reached the tests
    ],
)
def test_the_final_run_must_cover_the_repos_own_stages(
    env_update: dict[str, str], stages: list[VerifyStage], codes: list[str]
) -> None:
    """Re-review (P1-12d): L0 judges build and typecheck on this record, so a final run that
    skipped a stage the repo has is not evidence of success."""
    inp = _input()
    env = inp.env.model_copy(update=env_update)
    reached = VerifyStage.TESTS in stages
    final = _final(
        verify=VerifyResult(
            stages=[VerifyStageResult(stage=s, passed=True) for s in stages],
            tests=_tests(TestStage.FINAL) if reached else None,
        )
    )
    report = gate.evaluate_gate(ANY, _input(env=env, final=final), required=REQ)
    assert [v.code for v in report.violations] == codes


def test_a_final_test_run_of_another_run_is_flagged() -> None:
    other = _final(
        verify=VerifyResult(
            stages=GREEN_STAGES, tests=_tests(TestStage.FINAL, run_id=RunId("run-other"))
        )
    )
    report = gate.evaluate_gate(ANY, _input(final=other), required=REQ)
    assert [v.code for v in report.violations] == ["run_mismatch"]
