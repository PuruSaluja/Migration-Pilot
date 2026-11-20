"""P1-08: verification and knowledge interfaces (SPEC FR-4, FR-11, §9.5; drafts for Phase 2-5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mpilot.core.types import RunId
from mpilot.knowledge import base as kb
from mpilot.models.artifacts import FinalVerification
from mpilot.models.domain import (
    Baseline,
    Ecosystem,
    EnvProfile,
    TestResult,
    TestStage,
    VerifyResult,
)
from mpilot.sandbox.base import CacheMode
from mpilot.testing.doubles import DictTree
from mpilot.testing.infra_doubles import (
    FakeDriver,
    FixedMapper,
    ListChangelog,
    ListCodemods,
    MemoryMirror,
    MemoryWorkspace,
    StaticDetector,
    StaticRegistry,
    StaticUsageIndexer,
    StaticVerifier,
)
from mpilot.verify.base import Verifier

RUN = RunId("run-1")
ENV = EnvProfile(
    language=Ecosystem.NODE,
    runtime_version="20",
    package_manager="npm",
    install_cmd="npm ci",
    test_cmd="npm test",
    image_digest="sha256:" + "a" * 64,
    detection_sources={"test_cmd": "heuristic"},
)


def _tests(stage: TestStage) -> TestResult:
    return TestResult(
        run_id=RUN, stage=stage, passed=1, failed=0, skipped=0, errors=0, duration_s=1
    )


def _verifier() -> StaticVerifier:
    return StaticVerifier(
        baseline_result=Baseline(
            run_id=RUN,
            runs=[_tests(TestStage.BASELINE)],
            passing_ids=frozenset(),
            flaky_ids=frozenset(),
            test_file_hashes={},
            test_count=1,
        ),
        verify_result=VerifyResult(),
        final=FinalVerification(
            mirror_commit="a" * 40,
            verify=VerifyResult(),
            tests=_tests(TestStage.FINAL),
            runner_exit_code=0,
            runner_summary=None,
            cache="fresh",
        ),
    )


def test_conformance() -> None:
    assert isinstance(_verifier(), Verifier)
    assert isinstance(StaticDetector(ENV), kb.EnvDetector)
    assert isinstance(StaticRegistry({}), kb.RegistryClient)
    assert isinstance(ListChangelog(), kb.ChangelogIndex)
    assert isinstance(FixedMapper(), kb.RepoMapper)
    assert isinstance(StaticUsageIndexer(kb.UsageIndex(package="react")), kb.UsageIndexer)
    assert isinstance(ListCodemods(), kb.CodemodRegistry)


def test_final_verification_never_uses_the_run_cache() -> None:
    v = _verifier()
    kw = {"run_id": RUN, "commit": "b" * 40}
    with pytest.raises(ValueError, match="run's cache"):
        asyncio.run(v.final_verify(FakeDriver(), MemoryMirror(), ENV, cache=CacheMode.RUN, **kw))
    out = asyncio.run(
        v.final_verify(FakeDriver(), MemoryMirror(), ENV, cache=CacheMode.FRESH, **kw)
    )
    assert out.mirror_commit == "b" * 40  # judged on exactly the commit asked for


def test_overrides_always_win_and_are_recorded() -> None:
    profile = StaticDetector(ENV).detect(
        DictTree(commit="a" * 40), kb.DetectOverrides(test_cmd="pytest -q")
    )
    assert profile.test_cmd == "pytest -q"
    assert profile.detection_sources["test_cmd"] == "override"


def test_changelog_chunks_carry_sources_and_top_k_is_capped() -> None:
    with pytest.raises(ValidationError):
        kb.ChangelogChunk(text="x", source_url="")
    chunks = [
        kb.ChangelogChunk(text=f"createRoot {i}", source_url=f"https://react.dev/{i}")
        for i in range(20)
    ]
    assert len(ListChangelog(chunks).search("createroot", None, top_k=50)) == 8


def test_codemod_entries_need_a_command() -> None:
    kb.CodemodEntry(
        id="react-18",
        package="react",
        ecosystem=Ecosystem.NODE,
        from_major=17,
        to_major=18,
        command=("npx", "types-react-codemod", "preset-18"),
    )
    with pytest.raises(ValidationError):
        kb.CodemodEntry(
            id="x",
            package="react",
            ecosystem=Ecosystem.NODE,
            from_major=17,
            to_major=18,
            command=(),
        )


def test_registry_info_shape() -> None:
    info = kb.RegistryInfo(
        package="react",
        ecosystem=Ecosystem.NODE,
        versions=("17.0.2", "18.3.1"),
        latest_stable="18.3.1",
        published_at={"18.3.1": datetime(2024, 4, 26, tzinfo=UTC)},
    )
    assert asyncio.run(StaticRegistry({"react": info}).lookup("react", Ecosystem.NODE)) == info


def test_repo_mapper_respects_its_budget() -> None:
    assert len(FixedMapper("x" * 10_000).render(MemoryWorkspace(), (), budget_tokens=100)) <= 300
