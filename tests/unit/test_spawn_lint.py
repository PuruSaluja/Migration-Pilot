"""P1-03 / A10: nothing in src/mpilot starts a process except core/spawn.py.

This lint is the only enforcement of "every child starts through one helper", so it errs
on the side of flagging: modules that can start processes are forbidden outright.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "mpilot"
ALLOWED = SRC / "core" / "spawn.py"

# Top-level modules (and any submodule) that can start processes.
FORBIDDEN_MODULES = {
    "subprocess",
    "_posixsubprocess",
    "pty",
    "multiprocessing",
    "webbrowser",
    "git",  # GitPython
    "sh",
    "pexpect",
    "plumbum",
}
FORBIDDEN_SUBMODULES = {"asyncio.subprocess", "concurrent.futures.process"}
FORBIDDEN_NAMES = {"ProcessPoolExecutor"}
FORBIDDEN_OS = re.compile(r"^(system|popen|spawn\w*|exec\w*|posix_spawn\w*|fork\w*)$")
# Any attribute with these names, whatever object it is called on (loop.subprocess_exec, ...).
FORBIDDEN_ATTRS = re.compile(r"^(create_subprocess_\w+|subprocess_exec|subprocess_shell)$")


def _module_forbidden(name: str) -> bool:
    return name.split(".")[0] in FORBIDDEN_MODULES or any(
        name == sub or name.startswith(sub + ".") for sub in FORBIDDEN_SUBMODULES
    )


def violations(source: str) -> list[str]:
    tree = ast.parse(source)
    os_names = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names |= {a.asname or a.name for a in node.names if a.name == "os"}

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_forbidden(alias.name):
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _module_forbidden(node.module):
                found.append(f"from {node.module} import ...")
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if node.module == "os" and (alias.name == "*" or FORBIDDEN_OS.match(alias.name)):
                    found.append(f"from os import {alias.name}")
                elif (
                    alias.name in FORBIDDEN_NAMES
                    or FORBIDDEN_ATTRS.match(alias.name)
                    or _module_forbidden(full)
                ):
                    found.append(f"from {node.module} import {alias.name}")
        elif isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id in os_names
                and FORBIDDEN_OS.match(node.attr)
            ):
                found.append(f"os.{node.attr}")
            if FORBIDDEN_ATTRS.match(node.attr) or node.attr in FORBIDDEN_NAMES:
                found.append(f".{node.attr}")
            if node.attr == "subprocess" and isinstance(node.value, ast.Name):
                found.append(f"{node.value.id}.subprocess")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"__import__", "getattr"}:
                if func.id == "__import__":
                    found.append("__import__(...)")
                elif (
                    len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in os_names
                ):
                    found.append("getattr(os, ...)")
            if (isinstance(func, ast.Attribute) and func.attr == "import_module") or (
                isinstance(func, ast.Name) and func.id == "import_module"
            ):
                found.append("import_module(...)")
    return found


def test_no_process_spawning_outside_spawn_py() -> None:
    offenders = {
        str(path.relative_to(SRC)): found
        for path in sorted(SRC.rglob("*.py"))
        if path != ALLOWED and (found := violations(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_spawn_py_is_the_module_that_uses_subprocess() -> None:
    assert "import subprocess" in violations(ALLOWED.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "snippet",
    [
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run",
        "import _posixsubprocess",
        "import pty",
        "import multiprocessing",
        "from multiprocessing import Process",
        "import multiprocessing.pool",
        "from concurrent.futures import ProcessPoolExecutor",
        "import concurrent.futures.process",
        "from concurrent import futures\nfutures.ProcessPoolExecutor()",
        "import webbrowser",
        "import git",
        "import sh",
        "import pexpect",
        "import plumbum",
        "import asyncio.subprocess",
        "from asyncio import subprocess",
        "from asyncio import create_subprocess_exec",
        "import asyncio\nasyncio.create_subprocess_shell('x')",
        "import asyncio\nasyncio.subprocess.PIPE",
        "loop.subprocess_exec(proto, 'x')",
        "asyncio.get_running_loop().subprocess_shell(proto, 'x')",
        "from os import system",
        "from os import execvp",
        "from os import posix_spawn",
        "from os import *",
        "import os\nos.system('x')",
        "import os\nos.popen('x')",
        "import os\nos.execv('a', [])",
        "import os\nos.spawnlp(0, 'a')",
        "import os\nos.forkpty()",
        "import os\nos.posix_spawnp('a', [], {})",
        "import os as o\no.system('x')",
        "import os\ngetattr(os, 'system')('x')",
        "__import__('subprocess')",
        "import importlib\nimportlib.import_module('subprocess')",
        "from importlib import import_module\nimport_module('subprocess')",
    ],
)
def test_the_lint_catches(snippet: str) -> None:
    assert violations(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "import os\nos.environ.get('A'); os.path.join('a'); os.getuid(); os.killpg(1, 9)",
        "from concurrent.futures import ThreadPoolExecutor",
        "import asyncio\nasyncio.run(main())",
        "from . import git",
        "from mpilot.vcs import git",
    ],
)
def test_the_lint_allows_ordinary_code(snippet: str) -> None:
    assert violations(snippet) == []
