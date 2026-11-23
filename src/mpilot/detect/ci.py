"""CI-config signals for environment detection (SPEC FR-3, §9.4; P2-12a).

`.github/workflows/*.yml` is the strongest heuristic signal for which commands a repo really
runs, but it is repo text, so untrusted (H6). Nothing in a `run:` line is ever copied into a
command. A line is recognised as one of a closed set of forms, and the command that is emitted
is built from parts this module has validated itself:

- Node: `<pm> test` / `<pm> run <script>` / `yarn|pnpm <script>`, where `<pm>` is the package
  manager the *lockfile* says, and `<script>` is a plain name that exists in `package.json`.
- Python: `pytest`, `python -m pytest`, `uv|poetry|pdm run ...`, `python -m unittest discover`,
  `mypy`, `pyright`, with arguments limited to paths that exist in the tree, a short list of
  flags, and a marker expression of plain words. Coverage flags are dropped (the baseline
  coverage run is separate, D4).
- Runtime: `actions/setup-node` / `actions/setup-python` versions, a matrix resolved from the
  same job; the lowest supported image wins, as for the other version sources.

Anything else is skipped. A step that is recognised but whose arguments are not adopted is
reported in `warnings`, so a human can see why CI was not followed. Lines using shell syntax
(`&&`, pipes, `$`, redirects, `cd`) are skipped without comment: they are scripts, not commands.
The install command is never taken from CI: the lockfile decides between the frozen and the
unfrozen form (§9.4), and CI's own `npm ci` / `pip install` differ from them in ways that do not
matter here.

Caps: at most `MAX_WORKFLOW_FILES` files and `MAX_STEPS` steps per job are read; YAML numbers
stay text (an unquoted `3.10` is the float 3.1 in plain YAML).
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import yaml

from mpilot.detect.common import Detected
from mpilot.detect.node import DEFAULT_NODE_MAJOR, NODE_IMAGE_VERSIONS, NodeDetection
from mpilot.detect.python import (
    DEFAULT_PYTHON_VERSION,
    PYTHON_IMAGE_VERSIONS,
    VENV_PYTHON,
    PythonDetection,
)
from mpilot.policy.gate import TreeReader

MAX_WORKFLOW_FILES: Final = 20
MAX_STEPS: Final = 200

_WORKFLOW: Final = re.compile(r"\.github/workflows/[^/]+\.ya?ml")
_JOB_ID: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,60}")
_SHELL_SYNTAX: Final = re.compile(r"[;&|<>$`\\\n]")
_MATRIX_REF: Final = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_SCRIPT_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,59}")
_PATH_ARG: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_MARKER_EXPR: Final = re.compile(r"[A-Za-z0-9_ ()]{1,60}")
_TEST_EXCLUDED: Final = re.compile(
    r"e2e|watch|browser|integration|lint|types?|snapshot|update|debug|bench|visual", re.I
)
_TYPECHECK_SCRIPTS: Final = frozenset({"typecheck", "type-check", "check-types", "tsc"})
_FLOATING_NODE: Final = frozenset({"lts/*", "node", "latest", "current", "stable"})
_NODE_VERSION: Final = re.compile(r"v?(\d{1,2})(?:\.(?:x|\*|\d{1,3}))*")
_PYTHON_VERSION: Final = re.compile(
    r"3(?:\.(\d{1,2}|x|\*))?(?:\.(?:\d{1,3}|x|\*))?(?:-?(?:alpha|beta|rc|a|b)[.\d]*)?"
)
_PYTEST_FLAGS: Final = (
    re.compile(r"-x"),
    re.compile(r"-v{1,2}"),
    re.compile(r"-r[A-Za-z]{1,12}"),
    re.compile(r"--tb=(?:auto|long|short|line|native|no)"),
    re.compile(r"--maxfail=\d{1,4}"),
)
_MYPY_FLAGS: Final = frozenset({"--strict", "--ignore-missing-imports", "--no-incremental"})
_COVERAGE_FLAG: Final = re.compile(r"--(?:no-)?cov(?:[-=].*)?")
_RUNNERS: Final = frozenset({"uv", "poetry", "pdm"})


@dataclass(frozen=True)
class CiSignals:
    test_cmd: Detected | None = None
    typecheck_cmd: Detected | None = None
    build_cmd: Detected | None = None
    runtime_version: Detected | None = None
    warnings: tuple[str, ...] = ()


# --- loading workflows ----------------------------------------------------------------------------


class _Loader(yaml.SafeLoader):
    """SafeLoader that leaves numbers like `3.10` as text (plain YAML reads them as 3.1)."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in rules if tag != "tag:yaml.org,2002:float"]
    for first, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


@dataclass(frozen=True)
class _Step:
    source: str
    run: str | None
    uses: str | None
    version: Any  # `with.node-version` / `with.python-version` as written
    subdir: bool
    matrix: Mapping[str, Sequence[str]]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else str(value) if isinstance(value, int) else None


def _matrix(job: Mapping[str, Any]) -> dict[str, list[str]]:
    strategy = job.get("strategy")
    raw = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, values in raw.items():
        if isinstance(key, str) and isinstance(values, list):
            texts = [t for v in values if (t := _text(v)) is not None]
            if texts:
                out[key] = texts
    return out


def _is_subdir(value: Any) -> bool:
    return isinstance(value, str) and value.strip() not in {"", ".", "./"}


def _job_default_dir(job: Mapping[str, Any]) -> Any:
    defaults = job.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return run.get("working-directory") if isinstance(run, dict) else None


def _steps_of(path: str, job_id: str, job: Mapping[str, Any], warnings: list[str]) -> list[_Step]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    if len(steps) > MAX_STEPS:
        warnings.append(
            f"{path}: job {job_id!r} has more than {MAX_STEPS} steps; only the first "
            f"{MAX_STEPS} were read"
        )
    matrix = _matrix(job)
    default_dir = _job_default_dir(job)
    found: list[_Step] = []
    for index, step in enumerate(steps[:MAX_STEPS]):
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        uses = step.get("uses")
        with_ = step.get("with")
        working = step.get("working-directory", default_dir)
        found.append(
            _Step(
                source=f"{path}#jobs.{job_id}.steps[{index}]",
                run=run if isinstance(run, str) else None,
                uses=uses if isinstance(uses, str) else None,
                version=with_ if isinstance(with_, dict) else {},
                subdir=_is_subdir(working),
                matrix=matrix,
            )
        )
    return found


def _load_steps(tree: TreeReader, warnings: list[str]) -> list[_Step]:
    paths = sorted(p for p in tree.paths() if _WORKFLOW.fullmatch(p))
    if len(paths) > MAX_WORKFLOW_FILES:
        warnings.append(
            f"{len(paths)} workflow files found; only the first {MAX_WORKFLOW_FILES} "
            "(by name) were read"
        )
    steps: list[_Step] = []
    for path in paths[:MAX_WORKFLOW_FILES]:
        text = tree.read_text(path)
        if text is None:
            warnings.append(f"{path} cannot be read (a symlink, binary or too large): skipped")
            continue
        try:
            data = yaml.load(text, Loader=_Loader)  # noqa: S506  (a SafeLoader subclass)
        except (yaml.YAMLError, RecursionError, ValueError):
            warnings.append(f"{path} is not valid YAML: skipped")
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, dict):
            continue
        for job_id, job in jobs.items():
            if isinstance(job_id, str) and _JOB_ID.fullmatch(job_id) and isinstance(job, dict):
                steps.extend(_steps_of(path, job_id, job, warnings))
    return steps


# --- reading run lines ----------------------------------------------------------------------------


def _lines(run: str) -> list[list[str]]:
    """The simple commands of a `run:` block as word lists. Lines with shell syntax, comments
    and anything shlex cannot split are left out."""
    joined = re.sub(r"\\\n[ \t]*", " ", run)
    commands: list[list[str]] = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or _SHELL_SYNTAX.search(line):
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        if words:
            commands.append(words)
    return commands


@dataclass(frozen=True)
class _Hit:
    category: str  # "test" | "typecheck" | "build"
    command: str
    step: _Step


class _Candidates:
    """The first command seen per category, with conflicts reported."""

    def __init__(self, warnings: list[str]) -> None:
        self.first: dict[str, _Hit] = {}
        self._warnings = warnings

    def add(self, hit: _Hit) -> None:
        seen = self.first.get(hit.category)
        if seen is None:
            self.first[hit.category] = hit
        elif seen.command != hit.command:
            self._warnings.append(
                f"CI runs {seen.command!r} ({seen.step.source}) and {hit.command!r} "
                f"({hit.step.source}) as the {hit.category} command; using the first"
            )

    def detected(self, category: str) -> Detected | None:
        hit = self.first.get(category)
        return Detected(hit.command, hit.step.source) if hit else None


def _subdir_warning(step: _Step) -> str:
    return f"{step.source}: runs in a working-directory other than the repo root; not adopted"


# --- Node -----------------------------------------------------------------------------------------


def _node_scripts(tree: TreeReader) -> frozenset[str]:
    try:
        pkg = json.loads(tree.read_text("package.json") or "{}")
    except (ValueError, RecursionError):
        return frozenset()
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    if not isinstance(scripts, dict):
        return frozenset()
    return frozenset(k for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str))


def _node_category(name: str) -> str | None:
    if name in _TYPECHECK_SCRIPTS:
        return "typecheck"
    if name == "build":
        return "build"
    if name == "test" or (name.startswith("test:") and not _TEST_EXCLUDED.search(name[5:])):
        return "test"
    return None


def _node_script_of(words: Sequence[str]) -> tuple[str, list[str]] | None:
    """(script name, extra words) of `npm test`, `npm run X`, `yarn X`, `pnpm X`."""
    if len(words) < 2 or words[0] not in {"npm", "pnpm", "yarn"}:
        return None
    manager, rest = words[0], list(words[1:])
    if rest[0] == "test":
        return "test", rest[1:]
    if rest[0] in {"run", "run-script"} and len(rest) >= 2:
        return rest[1], rest[2:]
    if manager in {"yarn", "pnpm"} and not rest[0].startswith("-"):
        return rest[0], rest[1:]
    return None


def _node_hits(
    steps: Sequence[_Step], manager: str, scripts: frozenset[str], warnings: list[str]
) -> _Candidates:
    found = _Candidates(warnings)
    for step in steps:
        for words in _lines(step.run or ""):
            parsed = _node_script_of(words)
            if parsed is None:
                continue
            name, extra = parsed
            if not _SCRIPT_NAME.fullmatch(name) or (category := _node_category(name)) is None:
                continue
            if step.subdir:
                warnings.append(_subdir_warning(step))
                continue
            if name not in scripts:
                warnings.append(
                    f"{step.source}: CI runs script {name!r} but package.json has no such script"
                )
                continue
            if extra:
                warnings.append(
                    f"{step.source}: the {category} step has arguments that are not adopted "
                    f"({' '.join(extra)})"
                )
                continue
            command = f"{manager} test" if name == "test" else f"{manager} run {name}"
            found.add(_Hit(category, command, step))
    return found


# --- runtime versions -----------------------------------------------------------------------------


def _nearest(supported: Sequence[int], wanted: int) -> int:
    if wanted in supported:
        return wanted
    if wanted < supported[0]:
        return supported[0]
    if wanted > supported[-1]:
        return supported[-1]
    return next(v for v in supported if v > wanted)


def _version_texts(step: _Step, key: str) -> list[str]:
    value = _text(step.version.get(key))
    if value is None:
        return []
    ref = _MATRIX_REF.fullmatch(value.strip())
    if ref is not None:
        return list(step.matrix.get(ref[1], ()))
    return [value.strip()]


def _node_major(text: str) -> int | None:
    """The image major a `node-version` value asks for; floating names mean the newest."""
    spec = text.lower()
    if spec in _FLOATING_NODE:
        return int(DEFAULT_NODE_MAJOR)
    match = _NODE_VERSION.fullmatch(spec)
    return int(match[1]) if match else None


def _python_minor(text: str) -> int | None:
    match = _PYTHON_VERSION.fullmatch(text.lower())
    if match is None:
        return None
    minor = match[1]
    if minor is None or not minor.isdigit():
        return int(DEFAULT_PYTHON_VERSION.split(".")[1])
    return int(minor)


def _runtime(
    steps: Sequence[_Step],
    action: str,
    key: str,
    parse: Callable[[str], int | None],
    supported: Sequence[int],
    label: str,
    show: Callable[[int], str],
    warnings: list[str],
) -> tuple[int, str] | None:
    """(image number, source) of the first setup step with a usable version; the lowest of a
    matrix."""
    for step in steps:
        if step.uses is None or not step.uses.startswith(f"{action}@"):
            continue
        wanted = [(n, t) for t in _version_texts(step, key) if (n := parse(t)) is not None]
        if not wanted:
            continue
        number, text = min(wanted, key=lambda pair: _nearest(supported, pair[0]))
        chosen = _nearest(supported, number)
        if chosen != number:
            warnings.append(
                f"{step.source}: CI asks for {label} {text}; there is no image for it, "
                f"using {label} {show(chosen)}"
            )
        return chosen, step.source
    return None


def read_ci_node(tree: TreeReader, detection: NodeDetection) -> CiSignals:
    """Test, typecheck and build commands and the runtime that the workflows use, for a Node
    repo. Commands use the package manager `detection` found, never the one CI typed."""
    warnings: list[str] = []
    steps = _load_steps(tree, warnings)
    scripts = _node_scripts(tree)
    found = _node_hits(steps, detection.package_manager.value, scripts, warnings)
    supported = sorted(int(m) for m in NODE_IMAGE_VERSIONS)
    runtime = _runtime(
        steps, "actions/setup-node", "node-version", _node_major, supported, "node", str, warnings
    )
    return CiSignals(
        test_cmd=found.detected("test"),
        typecheck_cmd=found.detected("typecheck"),
        build_cmd=found.detected("build"),
        runtime_version=Detected(str(runtime[0]), runtime[1]) if runtime else None,
        warnings=tuple(dict.fromkeys(warnings)),
    )


# --- Python ---------------------------------------------------------------------------------------


def _strip_runner(words: Sequence[str]) -> list[str]:
    rest = list(words)
    if len(rest) >= 3 and rest[0] in _RUNNERS and rest[1] == "run":
        rest = rest[2:]
    if len(rest) >= 3 and rest[0] in {"python", "python3"} and rest[1] == "-m":
        rest = [rest[2], *rest[3:]]
    return rest


def _tree_path(tree_paths: frozenset[str], word: str) -> bool:
    if word == ".":
        return True
    path = word.rstrip("/")
    if not _PATH_ARG.fullmatch(path) or ".." in path.split("/"):
        return False
    return path in tree_paths or any(p.startswith(path + "/") for p in tree_paths)


def _pytest_args(
    args: Sequence[str], tree_paths: frozenset[str]
) -> tuple[list[str], list[str], bool]:
    """(kept arguments, rejected ones, coverage flags were dropped)."""
    kept: list[str] = []
    bad: list[str] = []
    dropped = False
    iterator = iter(args)
    for word in iterator:
        if word == "-q":
            continue
        if _COVERAGE_FLAG.fullmatch(word):
            dropped = True
        elif word == "-m":
            expr = next(iterator, "")
            if _MARKER_EXPR.fullmatch(expr):
                kept += ["-m", shlex.quote(expr)]
            else:
                bad.append(f"-m {expr}".strip())
        elif word.startswith("-"):
            if any(rx.fullmatch(word) for rx in _PYTEST_FLAGS):
                kept.append(word)
            else:
                bad.append(word)
        elif _tree_path(tree_paths, word):
            kept.append(word)
        else:
            bad.append(word)
    return kept, bad, dropped


def _checker_args(
    args: Sequence[str], flags: frozenset[str], tree_paths: frozenset[str]
) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    bad: list[str] = []
    for word in args:
        if word in flags or (not word.startswith("-") and _tree_path(tree_paths, word)):
            kept.append(word)
        else:
            bad.append(word)
    return kept, bad


def _python_command(
    step: _Step, words: Sequence[str], tree_paths: frozenset[str], warnings: list[str]
) -> _Hit | None:
    rest = _strip_runner(words)
    if not rest:
        return None
    tool, args = rest[0], rest[1:]
    if tool == "pytest":
        category = "test"
        kept, bad, dropped = _pytest_args(args, tree_paths)
        base = f"{VENV_PYTHON} -m pytest -q"
        extra = f" {' '.join(kept)}" if kept else ""
        command = base + extra
        if dropped and not bad:
            warnings.append(
                f"{step.source}: coverage flags dropped from the test command (the baseline "
                "coverage run is separate)"
            )
    elif tool == "unittest" and args == ["discover"]:
        return _Hit("test", f"{VENV_PYTHON} -m unittest discover", step)
    elif tool == "mypy":
        category = "typecheck"
        kept, bad = _checker_args(args, _MYPY_FLAGS, tree_paths)
        command = f"{VENV_PYTHON} -m mypy" + (f" {' '.join(kept)}" if kept else "")
    elif tool == "pyright":
        category = "typecheck"
        kept, bad = _checker_args(args, frozenset(), tree_paths)
        command = f"pyright --pythonpath {VENV_PYTHON}" + (f" {' '.join(kept)}" if kept else "")
    else:
        return None
    if bad:
        warnings.append(
            f"{step.source}: the {category} step has arguments that are not adopted "
            f"({' '.join(bad)})"
        )
        return None
    return _Hit(category, command, step)


def read_ci_python(tree: TreeReader, detection: PythonDetection) -> CiSignals:
    """Test and typecheck commands and the runtime that the workflows use, for a Python repo.
    `detection` is accepted for symmetry with Node and for later rules (it is not consulted
    yet: the emitted commands all use the repo venv)."""
    del detection
    warnings: list[str] = []
    steps = _load_steps(tree, warnings)
    tree_paths = tree.paths()
    found = _Candidates(warnings)
    for step in steps:
        for words in _lines(step.run or ""):
            hit = _python_command(step, words, tree_paths, warnings)
            if hit is None:
                continue
            if step.subdir:
                warnings.append(_subdir_warning(step))
                continue
            found.add(hit)
    supported = sorted(int(v.split(".")[1]) for v in PYTHON_IMAGE_VERSIONS)
    runtime = _runtime(
        steps,
        "actions/setup-python",
        "python-version",
        _python_minor,
        supported,
        "python",
        lambda minor: f"3.{minor}",
        warnings,
    )
    return CiSignals(
        test_cmd=found.detected("test"),
        typecheck_cmd=found.detected("typecheck"),
        runtime_version=Detected(f"3.{runtime[0]}", runtime[1]) if runtime else None,
        warnings=tuple(dict.fromkeys(warnings)),
    )
