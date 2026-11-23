"""Python environment detection (SPEC FR-3, §9.4).

`detect_python(tree)` reads a host mirror tree through `TreeReader` (never a workspace path) and
returns a `PythonDetection`: the tool (uv, Poetry, PDM or pip), the install commands (frozen
for baseline, unfrozen after the bump) with the test extras and groups, the test command
(pytest when configured, else unittest when a test file subclasses `unittest.TestCase`),
mypy when configured, and the runtime version (an
image minor), each with where it came from. This is the *heuristics* layer; P2-12 layers CI
config, `mpilot.toml` and user overrides on top and builds the `EnvProfile`.

Conventions (checked against the images, P2-11):
- Commands are shell strings, run with `sh -c` from the workspace root. The install command
  creates the project's venv at `.venv` (uv, PDM and pip do by default; Poetry is told to,
  on every command that can create it), and every other command uses `.venv/bin/python`, so
  the work container needs no activation and no network.
- Repo text only ever selects a branch. Extras and groups are chosen from a fixed set of
  names (`test`, `dev`, ...) and emitted as those fixed words; requirements files are
  chosen by a strict filename pattern; a hostile name matches neither and is ignored.

Decisions (none changes a SPEC [REC] item; see PROGRESS.md):
- Extras and groups installed: those named test, tests, testing, dev, develop, development.
- `requires-python` picks the *lowest* supported minor that satisfies it (as for Node); with
  no hint the default is the newest image.
- mypy runs only if it is configured (a config section or file): a tool that is merely
  installed may not be enforced by the project, and running it would make the baseline red.
  pyright runs too, from the images' own copy (installed with the wrapper's `nodejs` extra, so
  nothing is downloaded at run time) against the repo's venv. mypy wins if both are configured.
- tox and nox are not run (the images do not carry them); a project that only has those gets
  no test command and a warning.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from mpilot.core.errors import InputError
from mpilot.detect.common import Detected, JunitInvocation, check_output_path
from mpilot.policy.gate import TreeReader

#: Image runtime versions, kept equal to `images/digests.lock` by a test.
PYTHON_IMAGE_VERSIONS: Final[Mapping[str, str]] = {
    "3.9": "3.9.25",
    "3.10": "3.10.22",
    "3.11": "3.11.18",
    "3.12": "3.12.16",
    "3.13": "3.13.18",
}
DEFAULT_PYTHON_VERSION: Final = max(PYTHON_IMAGE_VERSIONS, key=lambda v: int(v.split(".")[1]))
VENV_PYTHON: Final = ".venv/bin/python"
_PREFERRED: Final = ("test", "tests", "testing", "dev", "develop", "development")
_LOCKFILES: Final = (("uv.lock", "uv"), ("poetry.lock", "poetry"), ("pdm.lock", "pdm"))
_REGULAR: Final = frozenset({"100644", "100755"})
_REQUIREMENTS: Final = re.compile(
    r"requirements(?:[-_](?:dev|develop|development|test|tests|testing))?\.txt"
    r"|(?:dev|test)-requirements\.txt"
)
_ANY_REQUIREMENTS: Final = re.compile(r"requirements[^/]*\.txt|[a-z]+-requirements\.txt")
_REQ_NAME: Final = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?=\s|\[|[<>=!~;@(,]|$)")
_PDM_GROUP: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}")
_PY_VERSION_FILE: Final = re.compile(
    r"(?:cpython-|python-)?3(?:\.(\d{1,2})(?:\.\d{1,3})?(?:[-+._a-z0-9]*)?)?"
)
_TEST_FILE: Final = re.compile(r"(?:^|/)test_[^/]*\.py$|_test\.py$")
_SPEC_TOKEN: Final = re.compile(
    r"(\^|~=|~|===|==|!=|<=|>=|<|>)?(\d{1,3}(?:\.\d{1,3}){0,2}(?:\.\*)?)"
)
MAX_REQUIREMENT_LINES: Final = 5000
MAX_TEST_FILES_READ: Final = 200  # looked at for unittest evidence, in path order
_UNITTEST_IMPORT: Final = re.compile(
    r"^[ \t]*(?:import[ \t]+unittest(?![.\w])|from[ \t]+unittest[ \t]+import\b)", re.MULTILINE
)
_TESTCASE_CLASS: Final = re.compile(r"^[ \t]*class[ \t]+\w+[ \t]*\([^)\n]*TestCase\b", re.MULTILINE)


class PythonDetectionError(InputError):
    """The tree cannot be treated as a Python project (exit 2)."""


@dataclass(frozen=True)
class PythonDetection:
    tool: Detected  # "uv" | "poetry" | "pdm" | "pip"
    install_cmd: Detected  # frozen: what baseline runs
    install_unfrozen_cmd: Detected  # what the bump runs
    test_cmd: Detected | None
    test_runner: Detected | None  # "pytest" | "unittest"
    typecheck_cmd: Detected | None
    typecheck_tool: Detected | None  # "mypy" | "pyright"
    runtime_version: Detected  # an image minor: "3.9" ... "3.13"
    python_requested: str | None  # the raw request when one was read
    extras: tuple[str, ...]
    groups: tuple[str, ...]
    requirements_files: tuple[str, ...]
    lockfiles: tuple[str, ...]  # what keys the package cache (§9.3): locks, or pip's inputs
    lockfile_hashes: dict[str, str] = field(default_factory=dict)  # path -> sha256
    warnings: tuple[str, ...] = ()


# --- loading -------------------------------------------------------------------------------------


def is_python_project(tree: TreeReader) -> bool:
    for path in tree.paths():
        if "/" in path:
            continue
        if path in {"pyproject.toml", "setup.py", "setup.cfg", "Pipfile"}:
            return True
        if path in {name for name, _ in _LOCKFILES} or _ANY_REQUIREMENTS.fullmatch(path):
            return True
    return False


def _load_pyproject(tree: TreeReader) -> dict[str, Any]:
    if "pyproject.toml" not in tree.paths():
        return {}
    text = tree.read_text("pyproject.toml")
    if text is None:
        raise PythonDetectionError("pyproject.toml cannot be read (a symlink, binary or too large)")
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError, ValueError) as exc:
        raise PythonDetectionError(f"pyproject.toml is not valid TOML: {exc}") from None


def _table(data: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    node: Any = data
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _has_table(data: Mapping[str, Any], *keys: str) -> bool:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return isinstance(node, dict)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(line: str) -> str | None:
    text = line.strip()
    if not text or text[0] in "#-":
        return None
    match = _REQ_NAME.match(text)
    return _norm(match[1]) if match else None


def _names_of(requirements: Any) -> set[str]:
    """Package names in a PEP 508 list, a Poetry-style table, or a requirements file's lines."""
    if isinstance(requirements, dict):
        return {_norm(k) for k in requirements if isinstance(k, str)}
    if isinstance(requirements, list):
        names = (_requirement_name(r) for r in requirements if isinstance(r, str))
        return {n for n in names if n}
    return set()


def _selected(available: Mapping[str, Any] | set[str]) -> tuple[str, ...]:
    """The preferred names present, in fixed order, matched case-insensitively. Only the
    fixed words are returned, never the repo's own spelling."""
    have = {_norm(k) for k in available if isinstance(k, str)}
    return tuple(n for n in _PREFERRED if n in have)


# --- tool ----------------------------------------------------------------------------------------


def _tool(
    tree: TreeReader, pyproject: Mapping[str, Any], warnings: list[str]
) -> tuple[Detected, tuple[str, ...]]:
    entries = tree.snapshot().entries
    found: list[tuple[str, str]] = []
    for name, tool in _LOCKFILES:
        entry = entries.get(name)
        if entry is None:
            continue
        if entry.mode not in _REGULAR:
            warnings.append(f"{name} is not a regular file: ignored")
            continue
        found.append((name, tool))
    tables = [t for t in ("uv", "poetry", "pdm") if _has_table(pyproject, "tool", t)]
    if found:
        lockname, chosen = found[0]
        if len(found) > 1:
            warnings.append(
                f"several lockfiles ({', '.join(n for n, _ in found)}): using {chosen} "
                "(fixed priority)"
            )
        warnings.extend(
            f"pyproject.toml has [tool.{t}] but the lockfile is {lockname}'s: using {chosen}"
            for t in tables
            if t != chosen
        )
        return Detected(chosen, lockname), tuple(n for n, _ in found)
    if tables:
        warnings.append("no lockfile: installs are not reproducible")
        if len(tables) > 1:
            warnings.append(f"several tool tables ({', '.join(tables)}): using {tables[0]}")
        return Detected(tables[0], f"pyproject.toml#tool.{tables[0]}"), ()
    paths = tree.paths()
    requirements = sorted(p for p in paths if "/" not in p and _ANY_REQUIREMENTS.fullmatch(p))
    for source, present in (
        (requirements[0] if requirements else "", bool(requirements)),
        ("pyproject.toml", "pyproject.toml" in paths),
        ("setup.py", "setup.py" in paths),
        ("setup.cfg", "setup.cfg" in paths),
    ):
        if present:
            if "Pipfile" in paths:
                warnings.append("Pipfile ignored: Pipenv is unsupported; using pip")
            return Detected("pip", source), ()
    if "Pipfile" in paths:
        raise PythonDetectionError("Pipenv is unsupported (only a Pipfile was found)")
    raise PythonDetectionError("no Python project files at the repository root")


# --- requirements and installed dependencies -----------------------------------------------------


def _requirements_files(tree: TreeReader) -> tuple[str, ...]:
    """`requirements.txt` first, then the dev/test files in name order."""
    paths = sorted(p for p in tree.paths() if "/" not in p and _REQUIREMENTS.fullmatch(p))
    base = [p for p in paths if p == "requirements.txt"]
    return tuple(base + [p for p in paths if p != "requirements.txt"])


def _file_requirement_names(tree: TreeReader, name: str, warnings: list[str]) -> set[str]:
    text = tree.read_text(name)
    if text is None:
        warnings.append(f"{name} cannot be read: its packages are not considered")
        return set()
    return {
        n
        for line in text.splitlines()[:MAX_REQUIREMENT_LINES]
        if (n := _requirement_name(line)) is not None
    }


@dataclass(frozen=True)
class _Plan:
    tool: str
    extras: tuple[str, ...]
    groups: tuple[str, ...]
    requirements: tuple[str, ...]
    has_project: bool  # pip: `-e .` is part of the install
    #: (source, package names) for everything the install command installs
    installed: tuple[tuple[str, set[str]], ...]


def _plan(tree: TreeReader, tool: str, pyproject: Mapping[str, Any], warnings: list[str]) -> _Plan:
    project = _table(pyproject, "project")
    optional = _table(project, "optional-dependencies")
    dep_groups = _table(pyproject, "dependency-groups")
    paths = tree.paths()
    sources: list[tuple[str, set[str]]] = []
    extras: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    has_project = tool != "pip"

    if tool == "pip":
        has_project = (
            _has_table(pyproject, "project")
            or _has_table(pyproject, "build-system")
            or "setup.py" in paths
            or "setup.cfg" in paths
        )
        requirements = _requirements_files(tree)
        if _selected(dep_groups):
            warnings.append("dependency groups are not installed by pip: put them in an extra")
    if tool == "poetry":
        extras = _selected(set(optional) | set(_table(pyproject, "tool", "poetry", "extras")))
        groups = _selected(_table(pyproject, "tool", "poetry", "group"))
    elif tool == "pdm":
        extras = _selected(optional)
        groups = _selected(
            set(_table(pyproject, "tool", "pdm", "dev-dependencies")) | set(dep_groups)
        )
    elif tool == "uv":
        extras = _selected(optional)
        groups = _selected(dep_groups)
    elif has_project:
        extras = _selected(optional)

    if has_project and _names_of(project.get("dependencies")):
        sources.append(("pyproject.toml#project.dependencies", _names_of(project["dependencies"])))
    keys = {_norm(k): k for k in optional if isinstance(k, str)}
    sources.extend(
        (f"pyproject.toml#project.optional-dependencies.{e}", _names_of(optional[keys[e]]))
        for e in extras
        if e in keys
    )
    if tool == "poetry":
        sources.extend(
            (f"pyproject.toml#tool.poetry.group.{g}.dependencies",
             _names_of(_table(pyproject, "tool", "poetry", "group", g, "dependencies")))
            for g in groups
        )  # fmt: skip
        for legacy in ("dev-dependencies", "dependencies"):
            table = _table(pyproject, "tool", "poetry", legacy)
            sources.append((f"pyproject.toml#tool.poetry.{legacy}", _names_of(table)))
    elif tool in ("uv", "pdm"):
        sources.extend(
            (f"pyproject.toml#dependency-groups.{g}", _names_of(dep_groups[g]))
            for g in groups
            if g in dep_groups
        )
        if tool == "uv":
            uv_dev = _table(pyproject, "tool", "uv").get("dev-dependencies")
            sources.append(("pyproject.toml#tool.uv.dev-dependencies", _names_of(uv_dev)))
        else:
            dev = _table(pyproject, "tool", "pdm", "dev-dependencies")
            sources.extend(
                (f"pyproject.toml#tool.pdm.dev-dependencies.{g}", _names_of(dev[g]))
                for g in groups
                if g in dev
            )
    sources.extend((r, _file_requirement_names(tree, r, warnings)) for r in requirements)
    return _Plan(tool, extras, groups, requirements, has_project, tuple(sources))


def _installed_source(plan: _Plan, package: str) -> str | None:
    for source, names in plan.installed:
        if package in names:
            return source
    return None


def _pdm_lock_groups(text: str | None) -> list[str] | None:
    """The optional groups recorded in pdm.lock's header, or None if they cannot be trusted."""
    if not text:
        return None
    try:
        header = tomllib.loads(text.split("[[package]]", 1)[0])
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    groups = _table(header, "metadata").get("groups")
    if not isinstance(groups, list) or not groups or len(groups) > 20:
        return None
    if not all(isinstance(g, str) and _PDM_GROUP.fullmatch(g) for g in groups):
        return None
    return [g for g in groups if g != "default"]


def _install_cmds(plan: _Plan, tree: TreeReader, has_lock: bool) -> tuple[str, str]:
    """(frozen, unfrozen). Without a lockfile both are the unfrozen one."""
    tool = plan.tool
    if tool == "uv":
        flags = "".join(f" --extra {e}" for e in plan.extras) + "".join(
            f" --group {g}" for g in plan.groups
        )
        return f"uv sync --frozen{flags}", f"uv sync{flags}"
    if tool == "poetry":
        env = "POETRY_VIRTUALENVS_IN_PROJECT=true"
        flags = "".join(f" --extras {e}" for e in plan.extras)
        if plan.groups:
            flags += f" --with {','.join(plan.groups)}"
        install = f"{env} poetry install --no-interaction{flags}"
        return install, f"{env} poetry lock && {install}"
    if tool == "pdm":
        selected = "".join(f" -G {g}" for g in (*plan.extras, *plan.groups))
        locked = _pdm_lock_groups(tree.read_text("pdm.lock")) if has_lock else None
        lock_flags = " -G :all" if locked is None else "".join(f" -G {g}" for g in locked)
        return (
            f"pdm install --frozen-lockfile{selected}",
            f"pdm lock{lock_flags} && pdm install{selected}",
        )
    parts = ["python -m venv .venv &&", ".venv/bin/pip install"]
    parts += [f"-r {r}" for r in plan.requirements]
    if plan.has_project:
        parts.append(f"-e '.[{','.join(plan.extras)}]'" if plan.extras else "-e .")
    command = " ".join(parts)
    return command, command


# --- tests and typecheck -------------------------------------------------------------------------


def _has_section(text: str | None, header: str) -> bool:
    return text is not None and re.search(rf"^\[{header}\]", text, re.MULTILINE) is not None


def _pytest_config_source(tree: TreeReader, pyproject: Mapping[str, Any]) -> str | None:
    paths = tree.paths()
    if "pytest.ini" in paths:
        return "pytest.ini"
    if _has_table(pyproject, "tool", "pytest", "ini_options"):
        return "pyproject.toml#tool.pytest.ini_options"
    if _has_table(pyproject, "tool", "pytest"):
        return "pyproject.toml#tool.pytest"
    if _has_section(tree.read_text("setup.cfg"), r"tool:pytest"):
        return "setup.cfg#tool:pytest"
    if _has_section(tree.read_text("tox.ini"), "pytest"):
        return "tox.ini#pytest"
    for conftest in ("conftest.py", "tests/conftest.py", "test/conftest.py"):
        if conftest in paths:
            return conftest
    return None


def _test_file_paths(tree: TreeReader) -> list[str]:
    return sorted(
        p
        for p in tree.paths()
        if _TEST_FILE.search(p)
        or (p.endswith(".py") and p.startswith(("tests/", "test/", "testing/")))
    )


def _unittest_evidence(tree: TreeReader, test_files: list[str]) -> str | None:
    """The first test file (in path order, at most `MAX_TEST_FILES_READ` of them) that imports
    unittest *and* defines a `...TestCase` subclass. Pytest-style tests that merely use
    `unittest.mock`, and Django's own `TestCase`, are not evidence."""
    for path in test_files[:MAX_TEST_FILES_READ]:
        text = tree.read_text(path)
        if text and _UNITTEST_IMPORT.search(text) and _TESTCASE_CLASS.search(text):
            return path
    return None


def _test_command(
    tree: TreeReader, pyproject: Mapping[str, Any], plan: _Plan, warnings: list[str]
) -> tuple[Detected | None, Detected | None]:
    config = _pytest_config_source(tree, pyproject)
    dependency = _installed_source(plan, "pytest")
    if config or dependency:
        source = config or f"{dependency} lists pytest"
        if config and dependency is None:
            warnings.append(
                "pytest is configured but is not a dependency of anything the install "
                "command installs: it may be missing from the environment"
            )
        return (
            Detected("pytest", source),
            Detected(f"{VENV_PYTHON} -m pytest -q", source),
        )
    test_files = _test_file_paths(tree)
    evidence = _unittest_evidence(tree, test_files)
    if evidence is not None:
        source = f"unittest.TestCase in {evidence}"
        return Detected("unittest", source), Detected(f"{VENV_PYTHON} -m unittest discover", source)
    paths = tree.paths()
    if "tox.ini" in paths or "noxfile.py" in paths:
        warnings.append(
            "only tox or nox defines the tests: not supported yet (the images lack them)"
        )
    if test_files:
        warnings.append(
            "no test command: test files exist but nothing shows how to run them (no pytest "
            "configuration or dependency, and no test file subclasses unittest.TestCase)"
        )
    else:
        warnings.append("no test command: no pytest configuration, no test files")
    return None, None


def junit_invocation(detection: PythonDetection, output_path: str) -> JunitInvocation | None:
    """The test command with a JUnit XML flag (pytest only), or None. `output_path` must be a
    plain relative path."""
    check_output_path(output_path)
    runner, command = detection.test_runner, detection.test_cmd
    if runner is None or command is None or runner.value != "pytest":
        return None
    return JunitInvocation(cmd=f"{command.value} --junitxml={output_path}", output_path=output_path)


def _mypy_files_configured(tree: TreeReader, source: str, pyproject: Mapping[str, Any]) -> bool:
    if source == "pyproject.toml#tool.mypy":
        return bool(_table(pyproject, "tool", "mypy").get("files"))
    name = source.partition("#")[0]
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(tree.read_text(name) or "")
    except configparser.Error:
        return False
    return parser.has_option("mypy", "files")


def _typecheck(
    tree: TreeReader, pyproject: Mapping[str, Any], plan: _Plan, warnings: list[str]
) -> tuple[Detected | None, Detected | None]:
    paths = tree.paths()
    mypy: str | None = None
    for name in ("mypy.ini", ".mypy.ini"):
        if name in paths and _has_section(tree.read_text(name), "mypy"):
            mypy = name
            break
    if mypy is None and _has_table(pyproject, "tool", "mypy"):
        mypy = "pyproject.toml#tool.mypy"
    if mypy is None and re.search(
        r"^\[mypy(?:-[^\]]*)?\]", tree.read_text("setup.cfg") or "", re.MULTILINE
    ):
        mypy = "setup.cfg#mypy"
    pyright: str | None = None
    if "pyrightconfig.json" in paths:
        pyright = "pyrightconfig.json"
    elif _has_table(pyproject, "tool", "pyright"):
        pyright = "pyproject.toml#tool.pyright"
    if pyright is not None and mypy is None:
        command = f"pyright --pythonpath {VENV_PYTHON}"
        return Detected("pyright", pyright), Detected(command, pyright)
    if mypy is None:
        return None, None
    if _installed_source(plan, "mypy") is None:
        warnings.append(
            "mypy is configured but is not a dependency of anything the install command "
            "installs: it may be missing from the environment"
        )
    if _mypy_files_configured(tree, mypy, pyproject):
        target = ""
    else:
        target = " src" if any(p.startswith("src/") and p.endswith(".py") for p in paths) else " ."
    return Detected("mypy", mypy), Detected(f"{VENV_PYTHON} -m mypy{target}", mypy)


# --- runtime version -----------------------------------------------------------------------------


def _supported_minors() -> list[int]:
    return sorted(int(v.split(".")[1]) for v in PYTHON_IMAGE_VERSIONS)


def _nearest_minor(minor: int) -> int:
    supported = _supported_minors()
    return min(max(minor, supported[0]), supported[-1])


def _specifier_sets(text: str) -> list[SpecifierSet]:
    """PEP 440 or Poetry constraint text -> alternatives of ANDed PEP 440 specifiers."""
    alternatives = []
    for alternative in text.split("||"):
        squeezed = re.sub(r"(\^|~=|~|===|==|!=|<=|>=|<|>)\s+", r"\1", alternative.strip())
        tokens = [t for t in re.split(r"[,\s]+", squeezed) if t]
        if not tokens:
            raise InvalidSpecifier(text)
        parts: list[str] = []
        for token in tokens:
            match = _SPEC_TOKEN.fullmatch(token)
            if match is None:
                raise InvalidSpecifier(token)
            parts.extend(_pep440(match[1] or "", match[2]))
        alternatives.append(SpecifierSet(",".join(parts)))
    return alternatives


def _pep440(op: str, version: str) -> list[str]:
    numbers = [int(x) for x in version.removesuffix(".*").split(".")]
    wildcard = version.endswith(".*")
    if op == "^":  # Poetry caret: nothing above the next major (or, for 0.x, the next minor)
        if numbers[0] > 0:
            upper = f"{numbers[0] + 1}"
        else:
            upper = f"0.{numbers[1] + 1}" if len(numbers) > 1 else "1"
        return [f">={version}", f"<{upper}"]
    if op == "~":
        upper = f"{numbers[0]}.{numbers[1] + 1}" if len(numbers) > 1 else f"{numbers[0] + 1}"
        return [f">={version}", f"<{upper}"]
    if op == "":
        return [f"=={version}" if wildcard or len(numbers) == 3 else f"=={version}.*"]
    return [f"{op}{version}"]


def _from_specifier(text: str, label: str, warnings: list[str]) -> tuple[str, str] | None:
    try:
        alternatives = _specifier_sets(text)
    except (InvalidSpecifier, ValueError):
        warnings.append(f"{label} {text!r} is not understood: ignored")
        return None

    def satisfied(version: str) -> bool:
        return any(s.contains(version, prereleases=True) for s in alternatives)

    ok = [m for m in _supported_minors() if satisfied(PYTHON_IMAGE_VERSIONS[f"3.{m}"])]
    if ok:
        return f"3.{ok[0]}", text
    possible = [
        minor
        for minor in range(0, 41)
        if any(satisfied(f"3.{minor}.{patch}") for patch in (0, 999))
    ]
    if not possible:
        warnings.append(f"{label} {text!r} cannot be satisfied by any python 3: ignored")
        return None
    chosen = _nearest_minor(possible[0])
    warnings.append(
        f"{label} {text!r}: no image satisfies it; using python 3.{chosen}, the closest"
    )
    return f"3.{chosen}", text


def _runtime(
    tree: TreeReader, pyproject: Mapping[str, Any], warnings: list[str]
) -> tuple[Detected, str | None]:
    text = tree.read_text(".python-version")
    if text is not None:
        lines = [
            ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        match = _PY_VERSION_FILE.fullmatch(lines[0].lower()) if lines else None
        if match is None:
            warnings.append(".python-version is not understood: ignored")
        else:
            requested = lines[0]
            if match[1] is None:  # "3": any python 3
                return Detected(DEFAULT_PYTHON_VERSION, ".python-version"), requested
            minor = int(match[1])
            chosen = _nearest_minor(minor)
            if chosen != minor:
                warnings.append(
                    f".python-version asks for python 3.{minor}; there is no image for it, "
                    f"using python 3.{chosen}"
                )
            return Detected(f"3.{chosen}", ".python-version"), requested
    candidates = (
        (
            "pyproject.toml#project.requires-python",
            "requires-python",
            _table(pyproject, "project").get("requires-python"),
        ),
        (
            "pyproject.toml#tool.poetry.dependencies.python",
            "tool.poetry python",
            _table(pyproject, "tool", "poetry", "dependencies").get("python"),
        ),
    )
    for source, label, value in candidates:
        if isinstance(value, str) and value.strip():
            resolved = _from_specifier(value.strip(), label, warnings)
            if resolved is not None:
                return Detected(resolved[0], source), resolved[1]
    return Detected(DEFAULT_PYTHON_VERSION, "default"), None


# --- the entry point -----------------------------------------------------------------------------


def detect_python(tree: TreeReader) -> PythonDetection:
    """Detect the Python environment of `tree`'s root. Raises `PythonDetectionError` (an
    `InputError`) if there are no Python project files, only a Pipfile, or an unreadable
    or invalid `pyproject.toml`."""
    warnings: list[str] = []
    pyproject = _load_pyproject(tree)
    tool, locks = _tool(tree, pyproject, warnings)
    plan = _plan(tree, tool.value, pyproject, warnings)
    frozen, unfrozen = _install_cmds(plan, tree, has_lock=bool(locks))
    install = Detected(
        frozen if locks or tool.value == "pip" else unfrozen, locks[0] if locks else tool.source
    )
    install_unfrozen = Detected(unfrozen, install.source)
    runner, test_cmd = _test_command(tree, pyproject, plan, warnings)
    typecheck_tool, typecheck_cmd = _typecheck(tree, pyproject, plan, warnings)
    runtime, requested = _runtime(tree, pyproject, warnings)

    if tool.value == "pip":
        project_files = [
            f
            for f, present in (
                ("pyproject.toml", "pyproject.toml" in tree.paths() and plan.has_project),
                ("setup.py", "setup.py" in tree.paths() and plan.has_project),
                ("setup.cfg", "setup.cfg" in tree.paths() and plan.has_project),
            )
            if present
        ]
        lockfiles: tuple[str, ...] = (*plan.requirements, *project_files)
    else:
        lockfiles = locks
    entries = tree.snapshot().entries
    return PythonDetection(
        tool=tool,
        install_cmd=install,
        install_unfrozen_cmd=install_unfrozen,
        test_cmd=test_cmd,
        test_runner=runner,
        typecheck_cmd=typecheck_cmd,
        typecheck_tool=typecheck_tool,
        runtime_version=runtime,
        python_requested=requested,
        extras=plan.extras,
        groups=plan.groups,
        requirements_files=plan.requirements,
        lockfiles=lockfiles,
        lockfile_hashes={name: entries[name].sha256 for name in lockfiles if name in entries},
        warnings=tuple(dict.fromkeys(warnings)),
    )
