"""Verification interfaces (SPEC FR-4, FR-11, §9.5, §11.2; C2, D4, D9).

Two kinds of run, never mixed up:
- `verify` runs in the run's work sandbox during the repair loop. Its results only steer
  the loop (attempt scoring, diagnosis); a rerun only informs the Diagnoser (C2).
- `final_verify` builds a *fresh* sandbox from the exact mirror commit to be pushed. Success,
  levels L0-L2 and every gate check judge only its `FinalVerification` (§9.5).

Implementations: baseline P2-17/P2-18, pipeline P3-11, final verification P3-13.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from mpilot.core.types import AttemptId, RunId
from mpilot.models.artifacts import FinalVerification
from mpilot.models.domain import Baseline, EnvProfile, VerifyResult
from mpilot.policy.test_files import TestFileClassifier
from mpilot.sandbox.base import CacheMode, Sandbox, SandboxDriver, Workspace
from mpilot.vcs.base import Mirror


@runtime_checkable
class Verifier(Protocol):
    async def baseline(
        self,
        sb: Sandbox,
        ws: Workspace,
        env: EnvProfile,
        classifier: TestFileClassifier,
        *,
        run_id: RunId,
        runs: int = 3,
    ) -> Baseline:
        """FR-4: install -> build -> typecheck -> test `runs` times on the unmodified repo,
        plus one coverage run (D4). Records P0 (passing in every run), flaky ids, the
        executed count (highest run, D9) and test-file hashes from the host walk (§11.2).
        A red or > 5% flaky baseline is reported in the result; the caller ends the run."""
        ...

    async def verify(
        self,
        sb: Sandbox,
        ws: Workspace,
        env: EnvProfile,
        *,
        run_id: RunId,
        attempt_id: AttemptId | None,
        manifests_changed: bool,
        selector: Sequence[str] | None,
        full: bool,
    ) -> VerifyResult:
        """FR-11 pipeline, stopping at the first failing stage: install (only if manifests
        changed) -> build -> typecheck -> targeted tests (`selector`) -> full suite (`full`).
        Results come from the JUnit XML the run wrote, read through `ws` (frozen, size-capped),
        with a log fallback; the suite timeout is NFR-2's."""
        ...

    async def final_verify(
        self,
        driver: SandboxDriver,
        mirror: Mirror,
        env: EnvProfile,
        *,
        run_id: RunId,
        commit: str,
        cache: CacheMode,
    ) -> FinalVerification:
        """§9.5: materialize `commit` from the mirror into a fresh workspace, start a fresh
        sandbox on it with `cache` (FRESH or HASH_VERIFIED, never the run's cache), run the
        whole pipeline uninstrumented, then one extra instrumented run for coverage. Nothing
        from the work container carries over except the commit. Raises ValueError for
        `CacheMode.RUN`."""
        ...
