"""Validated text types for owner-typed commands and runtime versions (SPEC FR-3, §9.4).

A leaf module (pydantic only) so that `config.py` and `knowledge/base.py` can share the types
without an import cycle.
"""

from __future__ import annotations

import re
from typing import Annotated, Final

from pydantic import AfterValidator

MAX_COMMAND_CHARS: Final = 2000
_RUNTIME_VERSION: Final = re.compile(r"v?\d{1,3}(?:\.\d{1,3}){0,2}")


def check_command(value: str) -> str:
    """A command the owner typed: one non-empty line of printable text (it is run with
    `sh -c` inside the sandbox, never on the host), stripped."""
    text = value.strip()
    if not text or len(text) > MAX_COMMAND_CHARS:
        raise ValueError(f"a command must be 1 to {MAX_COMMAND_CHARS} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("a command must be one line of text, with no control characters")
    return text


def check_runtime_version(value: str) -> str:
    """`20`, `v20`, `3.12`, `22.23.5`: the image is chosen from it by the detector."""
    if not _RUNTIME_VERSION.fullmatch(value):
        raise ValueError(f"not a version: {value!r} (e.g. 20, 3.12, 22.23.5)")
    return value


Command = Annotated[str, AfterValidator(check_command)]
RuntimeVersion = Annotated[str, AfterValidator(check_runtime_version)]
