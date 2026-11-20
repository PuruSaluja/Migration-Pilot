"""Knowledge interfaces, **drafts** (SPEC FR-1, FR-3, FR-5, FR-6, FR-9, §9.4, §10.2; A8, D3).

These are deliberately thin: the tasks that build them (P2-10..P2-13, P3-01, P3-14,
P5-01..P5-06) may extend the value types, recording the change in docs/architecture.md.
Everything here reads repo content through host-owned views (`TreeReader` of a mirror
commit, or `Workspace` while frozen), never by opening paths in the workspace directly, and
treats that content as untrusted data (H6).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from mpilot.models.domain import Ecosystem, EnvProfile, NonNegInt, PositiveInt
from mpilot.policy.gate import TreeReader
from mpilot.sandbox.base import Workspace


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DetectOverrides(_Frozen):
    """User override and `mpilot.toml` values: they always win (FR-3, §9.4 precedence)."""

    install_cmd: str | None = None
    build_cmd: str | None = None
    typecheck_cmd: str | None = None
    test_cmd: str | None = None
    runtime_version: str | None = None


@runtime_checkable
class EnvDetector(Protocol):
    def detect(self, tree: TreeReader, overrides: DetectOverrides) -> EnvProfile:
        """§9.4: override > mpilot.toml > CI config > heuristics, recording where each field
        came from in `detection_sources`. Reads the base commit from the mirror."""
        ...


class RegistryInfo(_Frozen):
    package: str
    ecosystem: Ecosystem
    versions: tuple[str, ...]  # sorted ascending, by the ecosystem's version rules
    latest_stable: str | None  # prereleases excluded
    yanked: frozenset[str] = frozenset()
    deprecated: frozenset[str] = frozenset()
    license_spdx: str | None = None
    published_at: dict[str, AwareDatetime] = Field(default_factory=dict)  # version -> time
    peer_deps: dict[str, str] = Field(default_factory=dict)  # of latest_stable


@runtime_checkable
class RegistryClient(Protocol):
    async def lookup(self, package: str, ecosystem: Ecosystem) -> RegistryInfo:
        """npm or PyPI metadata over the host fetch allowlist, no credentials (A8). An
        unknown package raises InputError (exit 2)."""
        ...


class ChangelogChunk(_Frozen):
    text: str
    source_url: str = Field(min_length=1)  # FR-6: every chunk carries its source
    version: str | None = None


class CorpusStats(_Frozen):
    documents: NonNegInt
    chunks: NonNegInt


@runtime_checkable
class ChangelogIndex(Protocol):
    async def build(
        self, package: str, ecosystem: Ecosystem, from_v: str, to_v: str
    ) -> CorpusStats:
        """Fetch release notes, CHANGELOGs and official guides between the versions, over
        the host fetch allowlist (A8), secret-scan and chunk them, index in SQLite FTS5."""
        ...

    def search(self, query: str, package: str | None, top_k: int) -> list[ChangelogChunk]:
        """BM25 search; `top_k` <= 8 (§9.1)."""
        ...


@runtime_checkable
class RepoMapper(Protocol):
    def render(self, ws: Workspace, personalize: Sequence[str], budget_tokens: int) -> str:
        """§10.2 repo map: tree-sitter tags -> reference graph -> personalized PageRank,
        rendered within `budget_tokens` (estimated as ceil(chars / 3), B3)."""
        ...


class UsageSite(_Frozen):
    path: str
    line: PositiveInt
    symbol: str  # the imported API, e.g. "ReactDOM.render"
    snippet: str = ""


class UsageIndex(_Frozen):
    package: str
    sites: tuple[UsageSite, ...] = ()


@runtime_checkable
class UsageIndexer(Protocol):
    def index(self, ws: Workspace, package: str, ecosystem: Ecosystem) -> UsageIndex:
        """Where the target package's APIs are used (FR-5: 100% recall of static imports)."""
        ...


class CodemodEntry(_Frozen):
    """One row of `codemods/registry.yaml` (FR-9, D3)."""

    id: str = Field(min_length=1)
    package: str
    ecosystem: Ecosystem
    from_major: NonNegInt
    to_major: NonNegInt
    command: tuple[str, ...] = Field(min_length=1)  # argv, run in the sandbox, network none
    removed_apis: tuple[str, ...] = ()  # names of curated `ApiPattern`s for gate check (h)


@runtime_checkable
class CodemodRegistry(Protocol):
    def lookup(self, package: str, from_major: int, to_major: int) -> list[CodemodEntry]:
        """Codemods for one major-version step, in the order they must run."""
        ...
