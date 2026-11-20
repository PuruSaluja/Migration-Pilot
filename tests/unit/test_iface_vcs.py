"""P1-08: VCS interfaces (SPEC §9.5, §11.1 H1/H2, §11.3, §11.6; A9, A10)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from pydantic import ValidationError

from mpilot.core.errors import CredentialRefusedError, PushRefusedError
from mpilot.core.types import RunId, RunStatus
from mpilot.models.artifacts import ArtifactRules, FileBlob, FinalVerification
from mpilot.models.domain import (
    CriticDecision,
    CriticVerdict,
    IntegrityReport,
    TestResult,
    TestStage,
    VerifyResult,
    Violation,
)
from mpilot.testing.infra_doubles import (
    AllowListCredentials,
    FakeSandbox,
    MemoryMirror,
    MemoryWorkspace,
    RecordingVcs,
)
from mpilot.vcs import base as vb

REPO = vb.RepoRef(owner="PuruSaluja", name="test-repo")


def test_conformance() -> None:
    assert isinstance(AllowListCredentials(frozenset()), vb.CredentialProvider)
    assert isinstance(RecordingVcs(), vb.VcsAdapter)
    assert isinstance(MemoryMirror(), vb.Mirror)


def test_vcs_adapter_has_no_merge_method() -> None:
    """H2: never merge or enable auto-merge. Not even a method that could."""
    members = [name for name, _ in inspect.getmembers(vb.VcsAdapter) if not name.startswith("_")]
    assert members, "protocol has members"
    for name in members:
        for word in ("merge", "automerge", "auto_merge", "approve", "delete_branch"):
            assert word not in name.lower(), name
    for name in dir(RecordingVcs):
        assert "merge" not in name.lower(), name


# ---------------------------------------------------------------- names and refs


@pytest.mark.parametrize("branch", ["mpilot/react-18-abc123", "mpilot/x", "mpilot/pydantic_2.0-1"])
def test_mpilot_branches(branch: str) -> None:
    assert vb.is_mpilot_branch(branch)


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "mpilot/",
        "mpilot/a/b",
        "mpilot/../main",
        "mpilot/x.lock",
        "Mpilot/x",
        "mpilot/X",
        "feature/mpilot/x",
        "refs/heads/mpilot/x",
        "mpilot/" + "a" * 101,
        "mpilot/-x",
    ],
)
def test_non_mpilot_branches(branch: str) -> None:
    assert not vb.is_mpilot_branch(branch)


def test_snapshot_ref_names() -> None:
    assert vb.phase_ref(RunStatus.BUMPED) == "refs/mpilot/phase/bumped"
    assert vb.phase_ref(RunStatus.CODEMODS_APPLIED) == "refs/mpilot/phase/codemods_applied"
    with pytest.raises(ValueError, match="phase snapshot"):
        vb.phase_ref(RunStatus.REVIEW)
    assert vb.attempt_ref(3) == "refs/mpilot/attempt/3"
    with pytest.raises(ValueError, match="start at 1"):
        vb.attempt_ref(0)


@pytest.mark.parametrize(
    ("owner", "name"), [("", "x"), ("a/b", "x"), ("x", ".."), ("x", "a b"), ("-x", "y")]
)
def test_repo_ref_validation(owner: str, name: str) -> None:
    with pytest.raises(ValidationError):
        vb.RepoRef(owner=owner, name=name)


def test_branch_status_pushable() -> None:
    ok = vb.BranchStatus(exists=False, is_default=False, protected=False, ruleset_protected=False)
    assert ok.pushable
    for field in ("is_default", "protected", "ruleset_protected"):
        assert not ok.model_copy(update={field: True}).pushable


def test_bot_pr_needs_a_full_sha() -> None:
    kw: dict[str, Any] = {
        "number": 1,
        "url": "u",
        "head_branch": "renovate/x",
        "base_branch": "main",
        "author_login": "renovate[bot]",
        "title": "t",
    }
    vb.BotPrInfo(**kw, head_sha="a" * 40)
    with pytest.raises(ValidationError):
        vb.BotPrInfo(**kw, head_sha="abc")


# ---------------------------------------------------------------- credentials and push rules


def test_credentials_refuse_repos_off_the_list() -> None:
    creds = AllowListCredentials(frozenset({"PuruSaluja/test-repo"}))
    assert asyncio.run(creds.token_for(REPO)).get_secret_value()
    with pytest.raises(CredentialRefusedError):
        asyncio.run(creds.token_for(vb.RepoRef(owner="someone", name="else")))
    assert "test-token" not in repr(asyncio.run(creds.token_for(REPO)))  # SecretStr hides it


def _verified(commit: str) -> FinalVerification:
    tests = TestResult(
        run_id=RunId("r"),
        stage=TestStage.FINAL,
        passed=1,
        failed=0,
        skipped=0,
        errors=0,
        duration_s=1,
    )
    return FinalVerification(
        mirror_commit=commit,
        verify=VerifyResult(),
        tests=tests,
        runner_exit_code=0,
        runner_summary=None,
        cache="fresh",
    )


def _gate(passed: bool = True) -> IntegrityReport:
    violations = (
        [] if passed else [Violation(check_id="integrity.a", code="p0_failed", message="t1 failed")]
    )
    return IntegrityReport(
        run_id=RunId("r"),
        passed=passed,
        violations=violations,
        test_count_before=1,
        test_count_after=1,
    )


def _verdict(decision: CriticDecision = CriticDecision.APPROVE) -> CriticVerdict:
    return CriticVerdict(run_id=RunId("r"), decision=decision)


def _push(vcs: RecordingVcs, branch: str, commit: str = "a" * 40, **kw: Any) -> None:
    args: dict[str, Any] = {
        "verified": _verified("a" * 40),
        "report": _gate(),
        "verdict": _verdict(),
    }
    args.update(kw)
    asyncio.run(vcs.push_branch(REPO, MemoryMirror(), branch=branch, commit=commit, **args))


@pytest.mark.parametrize(
    ("branch", "commit", "extra", "match"),
    [
        ("main", "a" * 40, {}, "mpilot/\\* branch"),
        ("feature/x", "a" * 40, {}, "mpilot/\\* branch"),
        ("mpilot/locked", "a" * 40, {}, "protected"),
        ("mpilot/ok", "b" * 40, {}, "final verification ran on"),
        ("mpilot/ok", "a" * 40, {"report": _gate(passed=False)}, "integrity gate"),
        ("mpilot/ok", "a" * 40, {"verdict": _verdict(CriticDecision.REVISE)}, "Critic"),
        ("mpilot/ok", "a" * 40, {"verdict": _verdict(CriticDecision.ESCALATE)}, "Critic"),
        ("mpilot/wip-x", "a" * 40, {}, "push_wip_branch"),
    ],
)
def test_success_push_rules(branch: str, commit: str, extra: dict[str, Any], match: str) -> None:
    """§9.1/§7.4: the success push happens only after every gate passed, H1 on top."""
    vcs = RecordingVcs(protected=frozenset({"mpilot/locked"}))
    with pytest.raises(PushRefusedError, match=match):
        _push(vcs, branch, commit, **extra)
    assert vcs.pushes == []


def test_push_of_the_verified_commit_and_idempotent_pr() -> None:
    vcs = RecordingVcs()
    _push(vcs, "mpilot/ok")
    assert vcs.pushes == [("mpilot/ok", "a" * 40)]
    kw: dict[str, Any] = {
        "branch": "mpilot/ok",
        "base": "main",
        "title": "t",
        "body": "b",
        "labels": (),
    }
    first = asyncio.run(vcs.upsert_draft_pr(REPO, **kw))
    again = asyncio.run(vcs.upsert_draft_pr(REPO, **kw))
    assert first == again
    assert first.draft


def test_wip_push_for_escalations() -> None:
    """FR-17: the escalation branch needs no final verification, only H1 and a mirror commit."""
    mirror = MemoryMirror(base_files={"a.py": FileBlob(mode="100644", data=b"v1")})
    vcs = RecordingVcs(protected=frozenset({"mpilot/wip-locked"}))
    asyncio.run(
        vcs.push_wip_branch(REPO, mirror, branch="mpilot/wip-react-1", commit=mirror.base_sha)
    )
    assert vcs.pushes == [("mpilot/wip-react-1", mirror.base_sha)]
    for branch, commit, match in [
        ("mpilot/react-1", mirror.base_sha, "wip-"),
        ("wip-react", mirror.base_sha, "wip-"),
        ("mpilot/wip-locked", mirror.base_sha, "protected"),
        ("mpilot/wip-x", "c" * 40, "not a mirror commit"),
    ]:
        with pytest.raises(PushRefusedError, match=match):
            asyncio.run(vcs.push_wip_branch(REPO, mirror, branch=branch, commit=commit))
    assert vb.WIP_BRANCH_PREFIX.startswith(vb.BRANCH_PREFIX)
    assert vb.NEEDS_HUMAN_LABEL == "needs-human"


# ---------------------------------------------------------------- mirror contract (double)


def test_snapshot_restore_round_trip() -> None:
    """§9.5 Attempts: restoring a host snapshot reproduces the snapshotted tree exactly."""
    base = {"a.py": FileBlob(mode="100644", data=b"v1")}
    mirror = MemoryMirror(base_files=base)
    sandbox = FakeSandbox(is_frozen=True)
    ws = MemoryWorkspace(files=dict(base), sandbox=sandbox)
    ws.write_text("a.py", "v2", create=False)
    ws.write_text("b.py", "new", create=True)
    rules = mirror.artifact_rules(frozenset({"node_modules"}))
    info = mirror.snapshot(vb.attempt_ref(1), ws, rules)
    assert mirror.resolve(vb.attempt_ref(1)) == info.commit
    ws.write_text("a.py", "broken", create=False)
    restored = ws.replace_tree(mirror.read_tree(vb.attempt_ref(1)))
    assert restored.digest() == info.tree_digest


def test_mirror_diff_names_both_ends() -> None:
    mirror = MemoryMirror(base_files={"a.py": FileBlob(mode="100644", data=b"v1")})
    ws = MemoryWorkspace(
        files={"a.py": FileBlob(mode="100644", data=b"v2")}, sandbox=FakeSandbox(is_frozen=True)
    )
    info = mirror.snapshot(vb.phase_ref(RunStatus.BUMPED), ws, ArtifactRules())
    d = mirror.diff(mirror.base_sha, info.commit)
    assert (d.base, d.target, d.target_digest) == (mirror.base_sha, info.commit, info.tree_digest)
    assert [f.path for f in d.files] == ["a.py"]
    reader = mirror.tree_reader(info.commit)
    assert reader.commit == info.commit
    assert reader.read_text("a.py") == "v2"


def test_push_refuses_a_report_or_verdict_from_another_run() -> None:
    vcs = RecordingVcs()
    stale = _gate().model_copy(update={"run_id": RunId("other")})
    with pytest.raises(PushRefusedError, match="different runs"):
        _push(vcs, "mpilot/ok", report=stale)
    with pytest.raises(PushRefusedError, match="different runs"):
        _push(vcs, "mpilot/ok", verdict=_verdict().model_copy(update={"run_id": RunId("other")}))
    assert vcs.pushes == []


def test_draft_pr_rules_for_heads_and_wip_branches() -> None:
    vcs = RecordingVcs()
    kw: dict[str, Any] = {"base": "main", "title": "t", "body": "b"}
    with pytest.raises(PushRefusedError, match="mpilot/\\*"):
        asyncio.run(vcs.upsert_draft_pr(REPO, branch="main", labels=(), **kw))
    with pytest.raises(PushRefusedError, match="needs-human"):
        asyncio.run(vcs.upsert_draft_pr(REPO, branch="mpilot/wip-x", labels=(), **kw))
    pr = asyncio.run(
        vcs.upsert_draft_pr(REPO, branch="mpilot/wip-x", labels=(vb.NEEDS_HUMAN_LABEL,), **kw)
    )
    assert pr.draft
