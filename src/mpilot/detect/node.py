"""Node/TypeScript environment detection (SPEC FR-3, §9.4).

`detect_node(tree)` reads a host mirror tree through `TreeReader` (never a workspace path) and
returns a `NodeDetection`: the package manager, the install, build, typecheck and test commands,
the test runner and its JUnit reporter, the runtime version (an image major) and any
workspaces, each with where it came from. This is the *heuristics* layer only; P2-12 layers CI
config, `mpilot.toml` and user overrides on top (§9.4 precedence) and builds the `EnvProfile`.

Everything in the repo is untrusted data (H6). Repo text only ever selects a branch here: every
command is assembled from fixed words and the detected package manager, so a hostile script
name, version or package name cannot reach a shell. The two places a repo-supplied name does
enter a command (a workspace directory and a workspace package name, for a scoped test) are
validated against a strict pattern first, and left out with a warning if they fail it.

Decisions (none changes a SPEC [REC] item; see PROGRESS.md):
- Runtime: `.nvmrc`, then `.node-version`, then `engines.node`, then the newest supported
  image. `engines.node` picks the *lowest* supported major that satisfies it (the oldest
  runtime the repo says it supports is the one its dependencies are most surely compatible
  with). A version with no image maps to the nearest supported one, with a warning.
- A typecheck script may be named `typecheck` or `type-check`; otherwise `tsc --noEmit` is
  used when there is a `tsconfig.json` and `typescript` is a dependency.
- JUnit flags are appended only when the test script itself runs the test runner (no `&&`,
  pipes, redirects, or `npm run` wrappers); otherwise results fall back to log parsing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import yaml

from mpilot.core.errors import InputError
from mpilot.detect.common import Detected, JunitInvocation, check_output_path
from mpilot.detect.semver import RangeSyntaxError, satisfies
from mpilot.policy.gate import TreeReader

#: Image runtime versions, kept equal to `images/digests.lock` by a test. A repo's engines range
#: is checked against these exact versions.
NODE_IMAGE_VERSIONS: Final[Mapping[str, str]] = {"18": "18.20.8", "20": "20.20.2", "22": "22.23.5"}
DEFAULT_NODE_MAJOR: Final = max(NODE_IMAGE_VERSIONS, key=int)
MAX_WORKSPACES: Final = 200
MAX_GLOBS: Final = 100

_LOCKFILES: Final[tuple[tuple[str, str], ...]] = (  # priority order: pnpm, yarn, then npm
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
)
_BUN_LOCKFILES: Final = ("bun.lockb", "bun.lock")
_PM_FIELD: Final = re.compile(r"(npm|pnpm|yarn)@(\d{1,4})(?:\.\d+\.\d+.*)?", re.DOTALL)
_NPM_STUB: Final = re.compile(
    r"""echo\s+(["'])Error: no test specified\1\s*&&\s*exit\s+1\s*;?""", re.DOTALL
)
_SCRIPT_REF: Final = re.compile(r"\b(?:npm|pnpm|yarn)\s+run\s+([\w:.-]+)")
_UNSAFE_TO_APPEND: Final = re.compile(r"[&|;<>`$()\n]")
_COVERS_WORKSPACES: Final = re.compile(
    r"--workspaces\b|(?<!\S)-ws(?!\S)|workspaces\s+foreach|\b(?:turbo|nx|lerna)\b"
    r"|\bpnpm\s+(?:-r|--recursive)\b|(?<!\S)--recursive(?!\S)"
)
_SAFE_SEGMENT: Final = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9_.@+-]*")
_SAFE_NAME: Final = re.compile(r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*")
_NODE_SPEC: Final = re.compile(r"v?(\d{1,3})(?:\.\d{1,5}){0,2}")
_LTS_NAMES: Final[Mapping[str, int]] = {
    "argon": 4, "boron": 6, "carbon": 8, "dubnium": 10, "erbium": 12, "fermium": 14,
    "gallium": 16, "hydrogen": 18, "iron": 20, "jod": 22, "krypton": 24,
}  # fmt: skip
_RUNNERS: Final = ("vitest", "jest", "mocha")
_REGULAR: Final = frozenset({"100644", "100755"})


class NodeDetectionError(InputError):
    """The tree cannot be treated as a Node project (exit 2)."""


@dataclass(frozen=True)
class WorkspacePackage:
    dir: str  # repo-relative, validated
    name: str | None  # None when absent or not a plain npm package name
    has_test_script: bool


@dataclass(frozen=True)
class NodeDetection:
    package_manager: Detected
    yarn_flavor: str | None  # "classic" | "berry" for yarn, else None
    yarn_flavor_source: str
    install_cmd: Detected  # frozen: what baseline runs
    install_unfrozen_cmd: Detected  # what the bump runs
    build_cmd: Detected | None
    typecheck_cmd: Detected | None
    test_cmd: Detected | None
    test_runner: Detected | None
    junit_reporter: Detected | None
    runtime_version: Detected  # an image major: "18" | "20" | "22"
    node_requested: str | None  # the raw request when one was read
    lockfiles: tuple[str, ...]  # every root lockfile, in priority order
    lockfile_hashes: dict[str, str] = field(default_factory=dict)  # path -> sha256
    workspaces: tuple[WorkspacePackage, ...] = ()
    root_test_covers_workspaces: bool | None = None
    warnings: tuple[str, ...] = ()
    #: Set only when reporter flags can be appended to the test script safely (see module doc).
    junit_runner_for_args: str | None = None

    @property
    def is_monorepo(self) -> bool:
        return bool(self.workspaces)


# --- loading -------------------------------------------------------------------------------------


def is_node_project(tree: TreeReader) -> bool:
    return "package.json" in tree.paths()


def _load_package_json(tree: TreeReader) -> dict[str, Any]:
    if "package.json" not in tree.paths():
        raise NodeDetectionError("no package.json at the repository root")
    text = tree.read_text("package.json")
    if text is None:
        raise NodeDetectionError("package.json cannot be read (a symlink, binary or too large)")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise NodeDetectionError(f"package.json is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise NodeDetectionError("package.json must be a JSON object")
    return data


def _section(pkg: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = pkg.get(name)
    return value if isinstance(value, dict) else {}


def _script(pkg: Mapping[str, Any], name: str) -> str | None:
    value = _section(pkg, "scripts").get(name)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _is_npm_stub(script: str) -> bool:
    return _NPM_STUB.fullmatch(script.strip()) is not None


def _real_test_script(pkg: Mapping[str, Any]) -> str | None:
    script = _script(pkg, "test")
    return None if script is None or _is_npm_stub(script) else script


def _dependency(pkg: Mapping[str, Any], name: str) -> str | None:
    """`package.json#<section>.<name>` for the first section that lists it."""
    for section in ("devDependencies", "dependencies"):
        if name in _section(pkg, section):
            return f"package.json#{section}.{name}"
    return None


# --- package manager -----------------------------------------------------------------------------


def _package_manager(
    pkg: Mapping[str, Any], tree: TreeReader, warnings: list[str]
) -> tuple[Detected, tuple[str, ...], re.Match[str] | None]:
    """The manager, the root lockfiles found, and the parsed `packageManager` field."""
    snapshot = tree.snapshot().entries
    found: list[tuple[str, str]] = []
    for name, manager in _LOCKFILES:
        entry = snapshot.get(name)
        if entry is None:
            continue
        if entry.mode not in _REGULAR:
            warnings.append(f"{name} is not a regular file: ignored")
            continue
        found.append((name, manager))
    raw_field = pkg.get("packageManager")
    field_match = _PM_FIELD.fullmatch(raw_field) if isinstance(raw_field, str) else None
    field_pm = field_match[1] if field_match else None
    bun = [n for n in _BUN_LOCKFILES if n in snapshot]
    managers = list(dict.fromkeys(m for _, m in found))

    if not managers:
        if bun:
            raise NodeDetectionError("Bun is unsupported (only a bun lockfile was found)")
        warnings.append("no lockfile: installs are not reproducible")
        if field_pm:
            return Detected(field_pm, "package.json#packageManager"), (), field_match
        if "pnpm-workspace.yaml" in snapshot:
            return Detected("pnpm", "pnpm-workspace.yaml"), (), field_match
        return Detected("npm", "default"), (), field_match
    if bun:
        warnings.append(f"{', '.join(bun)} ignored: Bun is unsupported; using another lockfile")
    chosen = managers[0]
    if len(managers) > 1:
        names = ", ".join(n for n, _ in found)
        chosen = field_pm if field_pm in managers else managers[0]
        warnings.append(
            f"several lockfiles ({names}): using {chosen}"
            + (" (named by packageManager)" if field_pm == chosen else " (fixed priority)")
        )
    elif field_pm and field_pm != chosen:
        warnings.append(
            f"packageManager says {field_pm} but the lockfile is {chosen}'s: using {chosen}"
        )
    source = next(n for n, m in found if m == chosen)
    return Detected(chosen, source), tuple(n for n, _ in found), field_match


def _yarn_flavor(
    tree: TreeReader, field_match: re.Match[str] | None, warnings: list[str]
) -> tuple[str, str]:
    if ".yarnrc.yml" in tree.paths():
        return "berry", ".yarnrc.yml"
    if field_match and field_match[1] == "yarn":
        return ("berry" if int(field_match[2]) >= 2 else "classic"), "package.json#packageManager"
    header = tree.read_text("yarn.lock") or ""
    if "__metadata:" in header[:4096]:
        return "berry", "yarn.lock"
    if "# yarn lockfile v1" in header[:4096]:
        return "classic", "yarn.lock"
    warnings.append("could not tell yarn classic from berry: assuming classic")
    return "classic", "default"


_INSTALL: Final[Mapping[str, tuple[str, str]]] = {  # manager -> (frozen, unfrozen)
    "npm": ("npm ci", "npm install"),
    "pnpm": ("pnpm install --frozen-lockfile", "pnpm install"),
    "yarn-classic": ("yarn install --frozen-lockfile", "yarn install"),
    "yarn-berry": ("yarn install --immutable", "yarn install"),
}


def _run(manager: str, script: str) -> str:
    return f"{manager} run {script}"


def _tsc(manager: str) -> str:
    return {
        "npm": "npx --no-install tsc --noEmit",  # never downloads: the work container is offline
        "pnpm": "pnpm exec tsc --noEmit",
        "yarn": "yarn tsc --noEmit",
    }[manager]


# --- test runner and JUnit -----------------------------------------------------------------------


def _commands(script: str) -> list[list[str]]:
    """The script split into simple commands, each as its words (quotes stripped)."""
    commands = []
    for part in re.split(r"&&|\|\||;|\||&|\n", script):
        words = [w.strip("'\"") for w in re.findall(r"[^\s()<>]+", part)]
        if words:
            commands.append(words)
    return commands


def _runner_of(script: str) -> str | None:
    for words in _commands(script):
        for word in words:
            base = word.rsplit("/", 1)[-1]
            if base in _RUNNERS:
                return base
            if base == "node" and any(w.startswith("--test") for w in words):
                return "node-test"
    return None


def _script_chain(pkg: Mapping[str, Any], start: str, hops: int = 3) -> list[str]:
    """The test script and the scripts it calls with `<pm> run <name>`, up to `hops` deep."""
    chain, seen, frontier = [start], {"test"}, [start]
    for _ in range(hops):
        following: list[str] = []
        for text in frontier:
            for name in _SCRIPT_REF.findall(text):
                target = _script(pkg, name)
                if target is not None and name not in seen:
                    seen.add(name)
                    following.append(target)
        chain += following
        frontier = following
    return chain


def _test_runner(pkg: Mapping[str, Any], script: str) -> Detected | None:
    for text in _script_chain(pkg, script):
        runner = _runner_of(text)
        if runner:
            return Detected(runner, "package.json#scripts.test")
    for runner in _RUNNERS:
        source = _dependency(pkg, runner)
        if source:
            return Detected(runner, source)
    return None


def _junit_reporter(
    pkg: Mapping[str, Any], runner: Detected | None, warnings: list[str]
) -> Detected | None:
    if runner is None:
        return None
    if runner.value == "vitest":
        return Detected("vitest-junit", "vitest built-in reporter")
    package = {"jest": "jest-junit", "mocha": "mocha-junit-reporter"}.get(runner.value)
    if package is None:
        return None
    source = _dependency(pkg, package)
    if source is None:
        warnings.append(
            f"{runner.value} runs the tests but {package} is not a dependency: "
            "results fall back to log parsing"
        )
        return None
    return Detected(package, source)


def _junit_runner_for_args(
    script: str, runner: Detected | None, reporter: Detected | None, warnings: list[str]
) -> str | None:
    """The runner whose flags can be appended to `<pm> test`, or None (with a warning if a
    reporter exists but cannot be used)."""
    if runner is None or reporter is None:
        return None
    direct = runner.source == "package.json#scripts.test" and _runner_of(script) == runner.value
    wrapped = _SCRIPT_REF.search(script) is not None
    if direct and not wrapped and not _UNSAFE_TO_APPEND.search(script):
        return runner.value
    warnings.append(
        "the test script runs several commands or wraps another script: "
        "cannot add reporter flags safely, so JUnit XML is not used"
    )
    return None


def junit_invocation(detection: NodeDetection, output_path: str) -> JunitInvocation | None:
    """The test command with JUnit reporter flags, or None if there is no safe way.

    `output_path` must be a plain relative path. jest-junit has no flag for its output file
    (only environment variables, which the sandbox does not pass), so for jest the XML is
    written to jest-junit's default, `junit.xml`, and that is what `output_path` reports.
    """
    check_output_path(output_path)
    runner = detection.junit_runner_for_args
    if runner is None or detection.test_cmd is None:
        return None
    if runner == "jest":
        flags, written = "--reporters=default --reporters=jest-junit", "junit.xml"
    elif runner == "vitest":
        flags = f"--reporter=default --reporter=junit --outputFile.junit={output_path}"
        written = output_path
    elif runner == "mocha":
        flags = f"--reporter mocha-junit-reporter --reporter-options mochaFile={output_path}"
        written = output_path
    else:
        return None
    manager = detection.package_manager.value
    separator = " -- " if manager == "npm" else " "  # npm needs `--` to pass flags on
    return JunitInvocation(cmd=f"{detection.test_cmd.value}{separator}{flags}", output_path=written)


# --- runtime version -----------------------------------------------------------------------------


def _supported() -> list[int]:
    return sorted(int(m) for m in NODE_IMAGE_VERSIONS)


def _nearest_supported(major: int) -> int:
    supported = _supported()
    if major in supported:
        return major
    if major < supported[0]:
        return supported[0]
    if major > supported[-1]:
        return supported[-1]
    return next(m for m in supported if m > major)


def _parse_node_spec(text: str) -> tuple[str, int | None] | None:
    """The first line of `.nvmrc` / `.node-version`: `("newest", None)` for node, stable,
    latest and lts/*; `("major", n)` for a version or an LTS name; None if unreadable."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    spec = lines[0].lower()
    if spec in ("node", "stable", "latest", "lts/*"):
        return "newest", None
    if spec.startswith("lts/"):
        name = spec[len("lts/") :]
        return ("major", _LTS_NAMES[name]) if name in _LTS_NAMES else None
    match = _NODE_SPEC.fullmatch(spec)
    return ("major", int(match[1])) if match else None


def _from_range(engines: str, warnings: list[str]) -> tuple[str, str] | None:
    """The image major for an `engines.node` range, or None if it cannot be used."""
    try:
        ok = [m for m in _supported() if satisfies(NODE_IMAGE_VERSIONS[str(m)], engines)]
        if ok:
            return str(ok[0]), engines
        satisfiable = [
            major
            for major in range(0, 41)
            if any(
                satisfies(f"{major}.{minor}.{patch}", engines)
                for minor in range(0, 100)
                for patch in (0, 999)
            )
        ]
    except RangeSyntaxError as exc:
        warnings.append(f"engines.node {engines!r} is not understood ({exc}): ignored")
        return None
    if not satisfiable:
        warnings.append(f"engines.node {engines!r} cannot be satisfied by any node: ignored")
        return None
    chosen = _nearest_supported(satisfiable[0])
    warnings.append(
        f"engines.node {engines!r}: no image satisfies it; using node {chosen}, the closest"
    )
    return str(chosen), engines


def _runtime(
    tree: TreeReader, pkg: Mapping[str, Any], warnings: list[str]
) -> tuple[Detected, str | None]:
    """The image major, and the raw request it came from (None for the default)."""
    for name in (".nvmrc", ".node-version"):
        text = tree.read_text(name)
        if text is None:
            continue
        parsed = _parse_node_spec(text)
        if parsed is None:
            warnings.append(f"{name} is not understood: ignored")
            continue
        requested = next(ln.strip() for ln in text.splitlines() if ln.strip())
        kind, major = parsed
        if kind == "newest" or major is None:
            return Detected(DEFAULT_NODE_MAJOR, name), requested
        chosen = _nearest_supported(major)
        if chosen != major:
            warnings.append(
                f"{name} asks for node {requested}; there is no image for it, using node {chosen}"
            )
        return Detected(str(chosen), name), requested
    engines = _section(pkg, "engines").get("node")
    if isinstance(engines, str):
        resolved = _from_range(engines, warnings)
        if resolved is not None:
            return Detected(resolved[0], "package.json#engines.node"), engines
    return Detected(DEFAULT_NODE_MAJOR, "default"), None


# --- workspaces ----------------------------------------------------------------------------------


def _glob_regex(pattern: str) -> re.Pattern[str]:
    segments = [s for s in pattern.removeprefix("./").strip("/").split("/") if s]
    out = ""
    for i, segment in enumerate(segments):
        last = i == len(segments) - 1
        if segment == "**":
            out += ".+" if last else "(?:[^/]+/)*"
            continue
        piece = "".join(
            "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in segment
        )
        out += piece + ("" if last else "/")
    return re.compile(out)


def _workspace_globs(pkg: Mapping[str, Any], tree: TreeReader, warnings: list[str]) -> list[str]:
    raw: list[Any] = []
    declared = pkg.get("workspaces")
    if isinstance(declared, dict):
        declared = declared.get("packages")
    if isinstance(declared, list):
        raw += declared
    text = tree.read_text("pnpm-workspace.yaml")
    if text is not None:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            warnings.append("pnpm-workspace.yaml is not valid YAML: ignored")
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("packages"), list):
            raw += doc["packages"]
    globs = [g for g in dict.fromkeys(raw) if isinstance(g, str) and g.strip()]
    if len(globs) > MAX_GLOBS:
        warnings.append(f"more than {MAX_GLOBS} workspace globs: the rest are ignored")
    return globs[:MAX_GLOBS]


def _workspace_package(
    directory: str, tree: TreeReader, warnings: list[str]
) -> WorkspacePackage | None:
    if not all(_SAFE_SEGMENT.fullmatch(s) for s in directory.split("/")):
        warnings.append(f"workspace directory {directory!r} left out: unsafe characters")
        return None
    text = tree.read_text(f"{directory}/package.json")
    try:
        data = json.loads(text) if text is not None else None
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        warnings.append(f"{directory}/package.json is not a readable JSON object: left out")
        return None
    name = data.get("name")
    if isinstance(name, str) and not _SAFE_NAME.fullmatch(name):
        warnings.append(f"workspace package name in {directory}/package.json left out: unsafe")
        name = None
    return WorkspacePackage(
        dir=directory,
        name=name if isinstance(name, str) else None,
        has_test_script=_real_test_script(data) is not None,
    )


def _workspaces(
    pkg: Mapping[str, Any], tree: TreeReader, warnings: list[str]
) -> tuple[WorkspacePackage, ...]:
    globs = _workspace_globs(pkg, tree, warnings)
    if not globs:
        return ()
    include = [_glob_regex(g) for g in globs if not g.startswith("!")]
    exclude = [_glob_regex(g[1:]) for g in globs if g.startswith("!")]
    candidates = sorted(
        p[: -len("/package.json")]
        for p in tree.paths()
        if p.endswith("/package.json") and "node_modules" not in p.split("/")
    )
    found: list[WorkspacePackage] = []
    for directory in candidates:
        if not any(r.fullmatch(directory) for r in include):
            continue
        if any(r.fullmatch(directory) for r in exclude):
            continue
        package = _workspace_package(directory, tree, warnings)
        if package is None:
            continue
        if len(found) >= MAX_WORKSPACES:
            warnings.append(
                f"more than {MAX_WORKSPACES} workspace packages: only the first are listed"
            )
            break
        found.append(package)
    return tuple(found)


def scoped_test_cmd(detection: NodeDetection, package: WorkspacePackage) -> str | None:
    """The command that runs one workspace package's tests, or None if it has none (or, for
    yarn, no usable name)."""
    if not package.has_test_script:
        return None
    manager = detection.package_manager.value
    if manager == "npm":
        return f"npm test --workspace={package.dir}"
    if manager == "pnpm":
        return f"pnpm --filter ./{package.dir} test"
    return f"yarn workspace {package.name} test" if package.name else None


# --- the entry point -----------------------------------------------------------------------------


def detect_node(tree: TreeReader) -> NodeDetection:
    """Detect the Node environment of `tree`'s root. Raises `NodeDetectionError` (an
    `InputError`) if there is no readable `package.json` or only a Bun lockfile."""
    warnings: list[str] = []
    pkg = _load_package_json(tree)
    manager, lockfiles, field_match = _package_manager(pkg, tree, warnings)
    pm = manager.value
    flavor, flavor_source = (None, "")
    if pm == "yarn":
        flavor, flavor_source = _yarn_flavor(tree, field_match, warnings)
    key = f"yarn-{flavor}" if pm == "yarn" else pm
    frozen, unfrozen = _INSTALL[key]
    has_lock = bool(lockfiles)
    install_source = lockfiles[0] if has_lock else manager.source
    install = Detected(frozen if has_lock else unfrozen, install_source)
    install_unfrozen = Detected(unfrozen, install_source)

    scripts_test = _real_test_script(pkg)
    test_cmd = runner = reporter = None
    junit_runner: str | None = None
    if scripts_test is not None:
        test_cmd = Detected(f"{pm} test", "package.json#scripts.test")
        runner = _test_runner(pkg, scripts_test)
        reporter = _junit_reporter(pkg, runner, warnings)
        junit_runner = _junit_runner_for_args(scripts_test, runner, reporter, warnings)
    else:
        warnings.append("no test command: package.json has no usable scripts.test")

    typecheck = None
    for name in ("typecheck", "type-check"):
        if _script(pkg, name) is not None:
            typecheck = Detected(_run(pm, name), f"package.json#scripts.{name}")
            break
    else:
        if "tsconfig.json" in tree.paths() and _dependency(pkg, "typescript"):
            typecheck = Detected(_tsc(pm), "tsconfig.json + typescript dependency")
    build = (
        Detected(_run(pm, "build"), "package.json#scripts.build")
        if _script(pkg, "build") is not None
        else None
    )

    runtime, requested = _runtime(tree, pkg, warnings)
    workspaces = _workspaces(pkg, tree, warnings)
    covers: bool | None = None
    if workspaces:
        covers = scripts_test is not None and _COVERS_WORKSPACES.search(scripts_test) is not None

    entries = tree.snapshot().entries
    hashes = {name: entries[name].sha256 for name in lockfiles}
    return NodeDetection(
        package_manager=manager,
        yarn_flavor=flavor,
        yarn_flavor_source=flavor_source,
        install_cmd=install,
        install_unfrozen_cmd=install_unfrozen,
        build_cmd=build,
        typecheck_cmd=typecheck,
        test_cmd=test_cmd,
        test_runner=runner,
        junit_reporter=reporter,
        runtime_version=runtime,
        node_requested=requested,
        lockfiles=lockfiles,
        lockfile_hashes=hashes,
        workspaces=workspaces,
        root_test_covers_workspaces=covers,
        warnings=tuple(dict.fromkeys(warnings)),
        junit_runner_for_args=junit_runner,
    )
