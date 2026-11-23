"""Normalized test IDs (SPEC FR-11, D9, §11.2 a).

One canonical form per runner, chosen so the JUnit XML and the runner's own log (the P2-14b
fallback) give the same string for the same test: the baseline's P0 set and a later run's
`passed_ids` are compared as sets, so a test whose ID changed would count as missing.

- pytest: `classname::name`, pytest's own JUnit identity (`_pytest.junitxml`). The XML has it
  directly; a node ID from the log maps onto it with `pytest_id_from_nodeid`, the same
  mangling pytest applies. The default JUnit family (xunit2) has no file attribute, so the
  file is not part of the ID.
- vitest: `file > describe > test`, as vitest prints it.
- jest: `file › describe › test`, as jest prints it. The XML carries the file only when
  jest-junit is configured the way `detect/node.py` asks for; without that the ID has no file
  (and jest-junit joins titles with spaces), which `junit.py` flags.
- mocha: the full title (`describe test`, space-joined), which mocha-junit-reporter writes
  and the spec reporter shows; no file, because the root suite has none.
- unittest: `module.Class.method`, `TestCase.id()`, plus ` (params)` for a failing subtest.
  unittest has no JUnit output; its IDs come from the log, the same on every Python version
  (3.10 prints `test_x (mod.Class)`, 3.12 `test_x (mod.Class.test_x)`).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from typing import Final

PYTEST_SEPARATOR: Final = "::"
JEST_SEPARATOR: Final = " › "
VITEST_SEPARATOR: Final = " > "
#: Where the workspace is mounted in every container (SPEC §9.5).
WORKSPACE_MOUNT: Final = "/workspace"

_WHITESPACE_CONTROL = re.compile(r"[\t\n\r\f\v]")


def clean(text: str) -> str:
    """One line, no outer whitespace: tabs and line breaks become spaces."""
    return _WHITESPACE_CONTROL.sub(" ", text).strip()


def pytest_id(classname: str, name: str) -> str:
    """The ID of a pytest JUnit case: `classname::name`, or `name` alone when the classname
    is empty (a collection error names the module that failed)."""
    classname, name = clean(classname), clean(name)
    return f"{classname}{PYTEST_SEPARATOR}{name}" if classname else name


def pytest_id_from_nodeid(nodeid: str) -> str:
    """A node ID as pytest prints it (`tests/a.py::Class::test[p]`) mapped to `pytest_id`,
    exactly as `_pytest.junitxml.mangle_test_address` does: parameters are split off first
    and kept verbatim, the file's `/` become `.` and `.py` is dropped, and every part but the
    last joins the classname."""
    path, bracket, params = clean(nodeid).partition("[")
    names = path.split(PYTEST_SEPARATOR)
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return pytest_id(".".join(names[:-1]), names[-1])


def vitest_id(file: str, titles: Sequence[str]) -> str:
    """`file > title > ...`; just the file for a suite that failed as a whole."""
    return VITEST_SEPARATOR.join([clean(file), *(clean(t) for t in titles)])


def jest_id(file: str | None, titles: Sequence[str]) -> str:
    """`file › title › ...`, or the titles alone when the file is unknown."""
    parts = [clean(t) for t in titles]
    return JEST_SEPARATOR.join([clean(file), *parts] if file else parts)


def mocha_id(titles: Sequence[str]) -> str:
    """The full title: every enclosing describe and the test, space-joined."""
    return " ".join(clean(t) for t in titles)


def unittest_id(method: str, dotted: str, params: str | None = None) -> str:
    """`module.Class.method` from the two parts unittest prints, `method (dotted)`: Python
    3.12's `dotted` already ends in the method, 3.10's does not. A subtest adds ` (params)`."""
    method, dotted = clean(method), clean(dotted)
    base = dotted if dotted == method or dotted.endswith("." + method) else f"{dotted}.{method}"
    return f"{base} ({clean(params)})" if params else base


def workspace_relative(path: str) -> str | None:
    """`path` as a normalized relative POSIX path inside the workspace, or None when it is
    empty, outside the workspace, or not a plain path. Absolute paths are accepted only under
    the container's mount point (`/workspace/...`), which the tools write when they print
    absolute paths."""
    if not path or "\x00" in path or "\\" in path:
        return None
    if path.startswith("/"):
        if not path.startswith(WORKSPACE_MOUNT + "/"):
            return None
        path = path[len(WORKSPACE_MOUNT) + 1 :]
    normal = posixpath.normpath(path)
    if normal in (".", "") or normal.startswith("../") or normal == ".." or normal.startswith("/"):
        return None
    return normal
