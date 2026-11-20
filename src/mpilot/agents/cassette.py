"""Cassettes: recorded role calls, replayed without a model (P1-11, decision B6).

A cassette records, at the `AgentRuntime` boundary, what the model did in each role call (its
tool calls turn by turn, its final answer, the usage and facts the runtime reported), keyed by
the request's content hash (`RoleCallRequest.request_hash`). `CassetteRuntime` replays it:

- the request must match a recorded one by content, or the call fails loudly with
  `CassetteMismatchError`, naming which parts of the request changed
- the recorded tool calls are re-issued to the live `ToolHost`, so policies, `ToolCall` rows
  and workspace effects happen for real; if the host decides differently from the recording
  (allow vs deny, a different denial code) the replay stops with `ScriptDivergedError`
- the result carries the recorded usage, turns, model facts and final answer, with the new
  call's own IDs

File format: JSON lines. Line 1 is `{"mpilot_cassette": 1}`; every other line is one
`CassetteEntry`. Output is deterministic (sorted keys, no timestamps), so a recording diffs
cleanly in git. Prompts are never stored, only their hashes; what is stored (tool arguments,
tool outputs, answers) can still contain repo content, so `write_cassette` demands a redactor.
Recording from a live runtime, with the gitleaks scan, is P4-22; this module is the format and
the replay.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from mpilot.agents.fake_runtime import PlayedCall, ScriptedCall, play
from mpilot.agents.runtime import (
    BudgetGuard,
    RoleCallRequest,
    RoleCallResult,
    RuntimeCapabilities,
    SelftestCheck,
    SelftestReport,
)
from mpilot.core.errors import CassetteMismatchError
from mpilot.core.types import Role
from mpilot.tools.base import ToolHost, ToolInvocation

CASSETTE_VERSION = 1
_HEADER = {"mpilot_cassette": CASSETTE_VERSION}


class CassetteEntry(BaseModel):
    """One recorded role call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Hash of each part of the request (`RoleCallRequest.part_hashes`), so a mismatch can say
    #: "the user prompt changed" instead of just "the hash changed".
    parts: dict[str, str]
    role: Role
    call: ScriptedCall

    @classmethod
    def for_request(cls, req: RoleCallRequest, call: ScriptedCall) -> Self:
        return cls(
            request_hash=req.request_hash(), parts=req.part_hashes(), role=req.role, call=call
        )


# ---------------------------------------------------------------- files


def write_cassette(
    path: Path, entries: Sequence[CassetteEntry], *, redact: Callable[[str], str]
) -> None:
    """Write `entries` to `path` atomically. `redact` is applied to every line before it is
    written (the secret scan, P4-22; pass `lambda line: line` only for synthetic data). Each
    redacted line must still parse as an entry, or nothing is written."""
    lines = [json.dumps(_HEADER, sort_keys=True)]
    for entry in entries:
        line = redact(json.dumps(entry.model_dump(mode="json"), sort_keys=True))
        CassetteEntry.model_validate_json(line)  # the redactor must not break the record
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_cassette(path: Path) -> list[CassetteEntry]:
    """Read a cassette; `ValueError` names the file and line of anything malformed."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError(f"{path}: empty cassette (no header)")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}:1: header is not JSON: {exc}") from exc
    if header != _HEADER:
        raise ValueError(f"{path}:1: not a version-{CASSETTE_VERSION} mpilot cassette: {header!r}")
    entries: list[CassetteEntry] = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        try:
            entries.append(CassetteEntry.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: bad cassette entry: {exc}") from exc
    return entries


# ---------------------------------------------------------------- replay


@dataclass
class CassetteRuntime:
    """Replays a cassette. Entries with the same request hash are used oldest first, once
    each, so a request repeated in a run (the same Explorer question twice) replays both.

    `strict_outputs` also compares each tool's output with the recorded one. It is off by
    default: outputs can legitimately carry timings or paths. The allow/deny decision and
    denial code are always compared.
    """

    entries: Sequence[CassetteEntry]
    strict_outputs: bool = False
    name: str = "cassette"
    capabilities: RuntimeCapabilities = field(
        default_factory=lambda: RuntimeCapabilities(
            per_call_effort=True,
            default_effort_verified_high=True,
            prompt_caching=False,
            context_editing=False,
        )
    )
    calls: list[PlayedCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queues: dict[str, list[CassetteEntry]] = defaultdict(list)
        for entry in self.entries:
            self._queues[entry.request_hash].append(entry)
        self._used: set[int] = set()

    @classmethod
    def from_file(cls, path: Path, *, strict_outputs: bool = False) -> Self:
        return cls(read_cassette(path), strict_outputs=strict_outputs)

    def unused(self) -> list[CassetteEntry]:
        """Entries no call has replayed yet."""
        return [e for e in self.entries if id(e) not in self._used]

    def assert_all_used(self) -> None:
        """Fail if the run under test no longer makes every recorded call (it drifted)."""
        left = self.unused()
        if left:
            roles = ", ".join(sorted(e.role.value for e in left))
            raise CassetteMismatchError(
                f"{len(left)} recorded call(s) were never replayed: {roles}"
            )

    def check_lockdown_static(self) -> None:
        return None

    async def call(
        self, req: RoleCallRequest, tools: ToolHost, guard: BudgetGuard
    ) -> RoleCallResult:
        entry = self._take(req)
        invocations: list[ToolInvocation] = []
        try:
            result = await play(
                entry.call, req, tools, guard, invocations, strict_outputs=self.strict_outputs
            )
        except BaseException as exc:
            self.calls.append(PlayedCall(req, None, tuple(invocations), exc))
            raise
        self.calls.append(PlayedCall(req, result, tuple(invocations)))
        return result

    async def selftest(self) -> SelftestReport:
        return SelftestReport(
            runtime=self.name,
            checks=(SelftestCheck(name="cassette", passed=True, detail="replay only"),),
        )

    # -- lookup

    def _take(self, req: RoleCallRequest) -> CassetteEntry:
        digest = req.request_hash()
        queue = self._queues.get(digest, [])
        for entry in queue:
            if id(entry) not in self._used:
                self._used.add(id(entry))
                return entry
        if queue:
            raise CassetteMismatchError(
                f"all {len(queue)} recorded {req.role.value} call(s) matching request "
                f"{digest[:12]} were already replayed; the run made this call more often than "
                "the recording did"
            )
        raise CassetteMismatchError(self._explain(req, digest))

    def _explain(self, req: RoleCallRequest, digest: str) -> str:
        """Say what differs from the closest recorded request, so a failure is fixable."""
        head = f"no cassette entry matches this {req.role.value} request (hash {digest[:12]})"
        same_role = [e for e in self.entries if e.role is req.role]
        if not same_role:
            have = sorted({e.role.value for e in self.entries}) or ["nothing"]
            return f"{head}: the cassette has no {req.role.value} calls (it has: {', '.join(have)})"
        mine = req.part_hashes()

        def distance(entry: CassetteEntry) -> int:
            return sum(1 for part, h in mine.items() if entry.parts.get(part) != h)

        closest = min(same_role, key=distance)
        changed = sorted(p for p, h in mine.items() if closest.parts.get(p) != h)
        return (
            f"{head}. Closest recorded {closest.role.value} request ({closest.request_hash[:12]}) "
            f"differs in: {', '.join(changed)}. Re-record the cassette if the change is intended."
        )
