"""Types and helpers shared by the per-ecosystem detectors (SPEC FR-3, §9.4)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

_SAFE_OUTPUT: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


@dataclass(frozen=True)
class Detected:
    value: str
    source: str  # where it came from: a file, `file#field`, or "default"


@dataclass(frozen=True)
class JunitInvocation:
    cmd: str  # the test command with the reporter flags added
    output_path: str  # where the XML will be, relative to the workspace root
    #: Variables the command needs in its exec environment (all on the driver's allowlist).
    env: Mapping[str, str] = field(default_factory=dict)


def check_output_path(path: str) -> str:
    """`path` if it is a plain relative path safe to put in a command, else ValueError."""
    if (
        not _SAFE_OUTPUT.fullmatch(path)
        or ".." in path.split("/")
        or path.endswith("/")
        or len(path) > 200
    ):
        raise ValueError(f"output path must be a plain relative path: {path!r}")
    return path
