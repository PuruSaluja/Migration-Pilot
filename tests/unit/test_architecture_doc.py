"""P1-07 acceptance: `docs/architecture.md` matches the code for the part-1 interfaces.

Every interface class must appear as `class <Name>` in the doc, and every member of every
Protocol must be named there. Run on each change, so the doc can't drift silently.
"""

from __future__ import annotations

import inspect
import re
import typing
from pathlib import Path

import pytest

from mpilot.agents import router, runtime
from mpilot.knowledge import base as kb
from mpilot.models import artifacts, state
from mpilot.orchestrator import base as ob
from mpilot.policy import gate, hooks
from mpilot.policy import test_files as tf
from mpilot.sandbox import base as sb
from mpilot.tools import base
from mpilot.vcs import base as vb
from mpilot.verify import base as verify

DOC = (Path(__file__).resolve().parents[2] / "docs" / "architecture.md").read_text(encoding="utf-8")

PROTOCOLS: list[type] = [
    runtime.AgentRuntime,
    runtime.BudgetGuard,
    router.ModelRouter,
    base.Tool,
    base.ToolHost,
    hooks.ToolPolicy,
    gate.PolicyCheck,
    gate.TreeReader,
    tf.TestFileClassifier,
    # P1-08
    sb.Sandbox,
    sb.SandboxDriver,
    sb.Workspace,
    vb.Mirror,
    vb.CredentialProvider,
    vb.VcsAdapter,
    ob.TransitionTable,
    ob.Orchestrator,
    ob.BudgetManager,
    ob.RunStore,
    ob.EventBus,
    verify.Verifier,
    kb.EnvDetector,
    kb.RegistryClient,
    kb.ChangelogIndex,
    kb.RepoMapper,
    kb.UsageIndexer,
    kb.CodemodRegistry,
]
VALUE_TYPES: list[type] = [
    runtime.ModelChoice,
    runtime.RoleCallRequest,
    runtime.RoleCallResult,
    runtime.StopReason,
    runtime.RuntimeCapabilities,
    runtime.SelftestCheck,
    runtime.SelftestReport,
    router.StickyState,
    router.EscalationTrigger,
    router.ExplorerFlag,
    router.RoutingContext,
    router.RoutingPolicies,
    router.RouteOutcome,
    base.ToolEffect,
    base.ToolSpec,
    base.ToolContext,
    base.HostDenyCode,
    base.ToolInvocation,
    hooks.PolicyVerdict,
    gate.GateInput,
    artifacts.TreeEntry,
    artifacts.TreeSnapshot,
    artifacts.ChangeKind,
    artifacts.Hunk,
    artifacts.FileChange,
    artifacts.WorkspaceDiff,
    artifacts.TestCounts,
    artifacts.FinalVerification,
    artifacts.ApiPattern,
    artifacts.MigrationNote,  # P1-10
    artifacts.EscalationReport,
    # P1-08
    sb.ContainerRuntime,
    sb.RuntimeInfo,
    sb.ResourceLimits,
    sb.CacheMode,
    sb.SandboxSpec,
    sb.ExecResult,
    sb.FreezeMechanism,
    vb.RepoRef,
    vb.RepoInfo,
    vb.BranchStatus,
    vb.PrInfo,
    vb.BotPrInfo,
    vb.SnapshotInfo,
    ob.RunRequest,
    ob.RunEvent,
    ob.CallEstimate,
    ob.EventKind,
    ob.RunEventRecord,
    state.FailureHistoryEntry,
    state.InFlightCall,
    state.BudgetState,
    state.Checkpoint,
    kb.DetectOverrides,
    kb.RegistryInfo,
    kb.ChangelogChunk,
    kb.CorpusStats,
    kb.UsageSite,
    kb.UsageIndex,
    kb.CodemodEntry,
]
FUNCTIONS = [
    "check_result",
    "route_invariant_violations",
    "mcp_name",
    "bare_name",
    "run_pre",
    "run_post",
    "evaluate_gate",
    "binding_violations",
    "fired_triggers",
    "sonnet_allowed_by_default",
    "is_trivial_edit",
    "minimal_sticky_after",
    "sandbox_env_problems",
    "is_mpilot_branch",
    "sticky_scope",
]


@pytest.mark.parametrize("cls", PROTOCOLS + VALUE_TYPES, ids=lambda c: c.__name__)
def test_class_is_documented(cls: type) -> None:
    assert re.search(rf"^\s*class {cls.__name__}\b", DOC, flags=re.MULTILINE), cls.__name__


@pytest.mark.parametrize("proto", PROTOCOLS, ids=lambda c: c.__name__)
def test_protocol_members_are_documented(proto: type) -> None:
    """Methods must appear as `def name(`; attributes and properties as `name:` or
    `name ->` (so short names like `id` can't match by accident)."""
    members = typing._get_protocol_attrs(proto)  # type: ignore[attr-defined]
    assert members, proto.__name__
    for name in members:
        attr = inspect.getattr_static(proto, name)
        if inspect.isfunction(attr):
            pattern = rf"def {re.escape(name)}\("
        else:
            pattern = rf"\b{re.escape(name)}\s*(:|->)"
        assert re.search(pattern, DOC), f"{proto.__name__}.{name} ({pattern})"


def test_registry_constants_are_documented() -> None:
    for name in ("REGISTRY_TOOL_NAMES", "EXPLORE_CALLER_ROLES", "MCP_TOOL_PREFIX"):
        assert name in DOC, name


@pytest.mark.parametrize("fn", FUNCTIONS)
def test_function_is_documented(fn: str) -> None:
    assert f"def {fn}(" in DOC, fn


def test_doc_matches_the_registry_size() -> None:
    assert "exactly §9.1 + explore" in DOC
    assert len(base.REGISTRY_TOOL_NAMES) == 18


FIELD_CHECKED: list[type] = [
    runtime.RoleCallRequest,
    runtime.RoleCallResult,
    runtime.RuntimeCapabilities,
    router.StickyState,
    router.RoutingContext,
    router.RoutingPolicies,
    router.RouteOutcome,
    base.ToolContext,
    base.ToolInvocation,
    hooks.PolicyVerdict,
    gate.GateInput,
    artifacts.WorkspaceDiff,
    artifacts.FinalVerification,
    sb.SandboxSpec,
    sb.ExecResult,
    vb.BranchStatus,
    ob.RunRequest,
    ob.CallEstimate,
    state.BudgetState,
    state.Checkpoint,
]


@pytest.mark.parametrize("cls", FIELD_CHECKED, ids=lambda c: c.__name__)
def test_fields_are_documented(cls: type) -> None:
    """Every field of the main value types appears as `name:` in the doc (catches a field
    added in code but not in the doc, e.g. `trigger_fired`)."""
    fields = getattr(cls, "model_fields", None) or cls.__dataclass_fields__
    for name in fields:
        assert re.search(rf"\b{re.escape(name)}\s*:", DOC), f"{cls.__name__}.{name}"


@pytest.mark.parametrize(
    "cls",
    [
        artifacts.FileBlob,
        artifacts.FileDiff,
        artifacts.EntryType,
        artifacts.DirEntry,
        artifacts.ArtifactRules,
        artifacts.ExportManifest,
    ],
    ids=lambda c: c.__name__,
)
def test_p1_08_artifact_types_are_named(cls: type) -> None:
    assert f"`{cls.__name__}`" in DOC, cls.__name__


def test_snapshot_ref_helpers_are_documented() -> None:
    assert "phase_ref(" in DOC
    assert "attempt_ref(" in DOC
