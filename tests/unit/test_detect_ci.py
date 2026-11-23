"""P2-12a (SPEC FR-3, §9.4): CI-config signals from `.github/workflows/*.yml`.

Tests first, one group per rule. CI is the strongest *heuristic* signal (override and
`mpilot.toml` beat it), but a workflow is repo text and so untrusted (H6): a `run:` line is never
copied into a command. It is only recognised as one of a closed set of forms, and what is
emitted is built from validated parts (a script name that exists in package.json, a path that is
in the tree, a flag from a short list).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from mpilot.detect.ci import (
    MAX_STEPS,
    MAX_WORKFLOW_FILES,
    CiSignals,
    read_ci_node,
    read_ci_python,
)
from mpilot.detect.node import detect_node
from mpilot.detect.python import detect_python
from mpilot.testing.doubles import DictTree

COMMIT = "a" * 40
PY = ".venv/bin/python"
CI = ".github/workflows/ci.yml"


def workflow(steps: str, *, job: str = "test", extra: str = "") -> str:
    """One job whose steps are `steps` (already indented as list items under `steps:`)."""
    head = f"name: ci\non: push\njobs:\n  {job}:\n    runs-on: ubuntu-latest\n"
    return f"{head}{extra}    steps:\n{steps}"


def run_steps(*lines: str) -> str:
    return "".join(f"      - run: {line}\n" for line in lines)


def package_json(scripts: Mapping[str, str] | None = None) -> str:
    return json.dumps({"name": "x", "version": "1.0.0", "scripts": scripts or {"test": "jest"}})


def node(
    ci: str | Mapping[str, str],
    scripts: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> CiSignals:
    files: dict[str, str] = {
        "package.json": package_json(scripts),
        "package-lock.json": "{}",
        **(extra or {}),
    }
    files.update({CI: ci} if isinstance(ci, str) else ci)
    tree = DictTree(COMMIT, files)
    return read_ci_node(tree, detect_node(tree))


def python(ci: str | Mapping[str, str], extra: Mapping[str, str] | None = None) -> CiSignals:
    files: dict[str, str] = {"requirements.txt": "pytest\n", **(extra or {})}
    files.update({CI: ci} if isinstance(ci, str) else ci)
    tree = DictTree(COMMIT, files)
    return read_ci_python(tree, detect_python(tree))


def source(step: int, job: str = "test", path: str = CI) -> str:
    return f"{path}#jobs.{job}.steps[{step}]"


# --- Node: test, typecheck and build steps --------------------------------------------------------


def test_no_workflows_means_no_signals() -> None:
    s = node({})
    assert (s.test_cmd, s.typecheck_cmd, s.build_cmd, s.runtime_version) == (None,) * 4
    assert s.warnings == ()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("npm test", "npm test"),
        ("npm run test", "npm test"),
        ("npm run-script test", "npm test"),
        ("npm run test:ci", "npm run test:ci"),
        ("yarn test:ci", "npm run test:ci"),
        ("pnpm test:ci", "npm run test:ci"),
        ("pnpm run test:ci", "npm run test:ci"),
        ("yarn run test:unit", "npm run test:unit"),
    ],
)
def test_a_ci_test_step_is_adopted_with_the_detected_package_manager(
    line: str, expected: str
) -> None:
    s = node(
        workflow(run_steps("npm ci", line)), {"test": "jest", "test:ci": "jest", "test:unit": "x"}
    )
    assert s.test_cmd is not None
    assert s.test_cmd.value == expected
    assert s.test_cmd.source == source(1)


def test_the_command_uses_the_package_manager_the_lockfile_says_not_the_one_ci_typed() -> None:
    tree = DictTree(
        COMMIT,
        {
            "package.json": package_json({"test": "x", "test:ci": "x"}),
            "pnpm-lock.yaml": "lockfileVersion: 9\n",
            CI: workflow(run_steps("npm run test:ci")),
        },
    )
    s = read_ci_node(tree, detect_node(tree))
    assert s.test_cmd is not None
    assert s.test_cmd.value == "pnpm run test:ci"


@pytest.mark.parametrize(
    ("script", "category"),
    [
        ("typecheck", "typecheck"),
        ("type-check", "typecheck"),
        ("check-types", "typecheck"),
        ("tsc", "typecheck"),
        ("build", "build"),
    ],
)
def test_typecheck_and_build_steps(script: str, category: str) -> None:
    s = node(workflow(run_steps(f"npm run {script}")), {"test": "x", script: "x"})
    got = s.typecheck_cmd if category == "typecheck" else s.build_cmd
    assert got is not None
    assert got.value == f"npm run {script}"
    assert got.source == source(0)
    assert (s.test_cmd is None) is True


@pytest.mark.parametrize(
    "script",
    ["test:e2e", "test:watch", "test:browser", "test:integration", "e2e", "lint", "deploy"],
)
def test_scripts_that_are_not_the_unit_suite_are_not_adopted_as_the_test_command(
    script: str,
) -> None:
    s = node(workflow(run_steps(f"npm run {script}")), {"test": "x", script: "x"})
    assert s.test_cmd is None


def test_a_script_that_does_not_exist_is_not_adopted_and_is_reported() -> None:
    s = node(workflow(run_steps("npm run test:ci")), {"test": "x"})
    assert s.test_cmd is None
    assert any("test:ci" in w and "package.json" in w for w in s.warnings)


@pytest.mark.parametrize(
    "line",
    [
        "npm test -- --coverage",
        "npm run test:ci --silent",
        "yarn test --watchAll=false",
    ],
)
def test_a_step_with_arguments_is_not_adopted_and_is_reported(line: str) -> None:
    s = node(workflow(run_steps(line)), {"test": "x", "test:ci": "x"})
    assert s.test_cmd is None
    assert any("argument" in w for w in s.warnings)


@pytest.mark.parametrize(
    "line",
    [
        "npm test && npm run build",
        "npm test || true",
        "npm test; echo done",
        "npm test | tee out.log",
        "npm test > out.log",
        "npm test $(whoami)",
        "npm test `id`",
        "CI=true npm test",
        "npm run ${{ matrix.script }}",
        "cd app && npm test",
        "(npm test)",
        "npm test &",
    ],
)
def test_lines_with_shell_syntax_or_expressions_are_never_adopted(line: str) -> None:
    s = node(workflow(run_steps(f"'{line}'")), {"test": "x"})
    assert s.test_cmd is None
    assert s.warnings == ()


@pytest.mark.parametrize(
    "name", ["test;rm -rf ~", "test$(id)", "test`id`", "test&&x", "-test", "t" * 80]
)
def test_a_script_name_with_odd_characters_never_reaches_a_command(name: str) -> None:
    # even when package.json really has such a script, only a plain name is ever emitted
    s = node(workflow(run_steps(json.dumps(f"npm run {name}"))), {"test": "x", name: "x"})
    assert s.test_cmd is None


def test_a_multi_line_run_block_is_read_line_by_line() -> None:
    steps = "      - run: |\n          npm ci\n          npm run typecheck\n          npm test\n"
    s = node(workflow(steps), {"test": "x", "typecheck": "x"})
    assert s.test_cmd is not None
    assert s.typecheck_cmd is not None
    assert s.test_cmd.source == s.typecheck_cmd.source == source(0)


def test_a_backslash_continued_line_is_joined() -> None:
    steps = "      - run: |\n          npm run \\\n            test:ci\n"
    s = node(workflow(steps), {"test": "x", "test:ci": "x"})
    assert s.test_cmd is not None
    assert s.test_cmd.value == "npm run test:ci"


def test_the_first_matching_step_wins_and_a_conflict_is_reported() -> None:
    s = node(workflow(run_steps("npm test", "npm run test:ci")), {"test": "x", "test:ci": "x"})
    assert s.test_cmd is not None
    assert (s.test_cmd.value, s.test_cmd.source) == ("npm test", source(0))
    assert any("test:ci" in w and "npm test" in w for w in s.warnings)


def test_a_step_with_a_working_directory_is_not_adopted() -> None:
    steps = "      - run: npm test\n        working-directory: packages/app\n"
    s = node(workflow(steps), {"test": "x"})
    assert s.test_cmd is None
    assert any("working-directory" in w for w in s.warnings)


def test_a_job_whose_default_working_directory_is_a_subfolder_is_not_adopted() -> None:
    extra = "    defaults:\n      run:\n        working-directory: web\n"
    s = node(workflow(run_steps("npm test"), extra=extra), {"test": "x"})
    assert s.test_cmd is None
    assert any("working-directory" in w for w in s.warnings)


def test_a_python_step_in_a_subfolder_is_not_adopted() -> None:
    steps = "      - run: pytest\n        working-directory: server\n"
    s = python(workflow(steps))
    assert s.test_cmd is None
    assert any("working-directory" in w for w in s.warnings)


@pytest.mark.parametrize("line", ["npm test:ci", "npm build", "npm typecheck", "npm -v"])
def test_npm_has_no_script_shorthand_only_yarn_and_pnpm_do(line: str) -> None:
    scripts = {"test": "x", "test:ci": "x", "build": "x", "typecheck": "x"}
    s = node(workflow(run_steps(line)), scripts)
    assert (s.test_cmd, s.typecheck_cmd, s.build_cmd) == (None, None, None)


def test_a_dot_working_directory_is_the_root() -> None:
    steps = "      - run: npm test\n        working-directory: .\n"
    s = node(workflow(steps), {"test": "x"})
    assert s.test_cmd is not None


def test_steps_are_numbered_by_position_including_steps_that_are_not_run_steps() -> None:
    steps = "      - uses: actions/checkout@v4\n" + run_steps("npm ci", "npm test")
    s = node(workflow(steps), {"test": "x"})
    assert s.test_cmd is not None
    assert s.test_cmd.source == source(2)


# --- Node: runtime version ------------------------------------------------------------------------


def setup_node(version: str, *, matrix: str = "") -> str:
    extra = f"    strategy:\n      matrix:\n        node: {matrix}\n" if matrix else ""
    steps = (
        f"      - uses: actions/setup-node@v4\n        with:\n          node-version: {version}\n"
    )
    return workflow(steps + run_steps("npm test"), extra=extra)


@pytest.mark.parametrize(
    ("version", "expected"),
    [("20", "20"), ("'20'", "20"), ("20.x", "20"), ("'22.4.1'", "22"), ("18.20.8", "18")],
)
def test_setup_node_version(version: str, expected: str) -> None:
    s = node(setup_node(version))
    assert s.runtime_version is not None
    assert s.runtime_version.value == expected
    assert s.runtime_version.source == source(0)


def test_a_matrix_of_versions_picks_the_lowest_supported() -> None:
    s = node(setup_node("${{ matrix.node }}", matrix="[22, 18, 20]"))
    assert s.runtime_version is not None
    assert s.runtime_version.value == "18"


@pytest.mark.parametrize("expr", ["${{ matrix.node }}", "${{matrix.node}}", "${{  matrix.node  }}"])
def test_matrix_expression_spacing(expr: str) -> None:
    s = node(setup_node(expr, matrix="[20, 22]"))
    assert s.runtime_version is not None
    assert s.runtime_version.value == "20"


def test_a_version_with_no_image_maps_to_the_closest_with_a_warning() -> None:
    s = node(setup_node("16"))
    assert s.runtime_version is not None
    assert s.runtime_version.value == "18"
    assert any("16" in w and "18" in w for w in s.warnings)


@pytest.mark.parametrize("version", ["lts/*", "node", "latest", "current"])
def test_a_floating_version_is_the_newest_image(version: str) -> None:
    s = node(setup_node(f"'{version}'"))
    assert s.runtime_version is not None
    assert s.runtime_version.value == "22"


@pytest.mark.parametrize(
    "version", ["${{ inputs.v }}", "${{ matrix.other }}", "''", "'lts/hydrogen'x", "banana"]
)
def test_an_unreadable_version_is_ignored(version: str) -> None:
    s = node(setup_node(version, matrix="[20]"))
    assert s.runtime_version is None


def test_node_version_file_is_left_to_the_heuristics() -> None:
    steps = (
        "      - uses: actions/setup-node@v4\n        with:\n          node-version-file: .nvmrc\n"
    )
    s = node(workflow(steps))
    assert s.runtime_version is None


def test_a_matrix_is_read_only_from_its_own_job() -> None:
    wf = (
        "name: ci\non: push\njobs:\n"
        "  a:\n    strategy:\n      matrix:\n        node: [18]\n"
        "    steps:\n      - run: npm test\n"
        "  b:\n    steps:\n      - uses: actions/setup-node@v4\n        with:\n"
        "          node-version: ${{ matrix.node }}\n"
    )
    assert node(wf).runtime_version is None


# --- workflow files -------------------------------------------------------------------------------


def test_workflow_files_are_read_in_name_order_and_the_first_signal_wins() -> None:
    a = ".github/workflows/a.yml"
    b = ".github/workflows/b.yaml"
    s = node(
        {b: workflow(run_steps("npm run test:ci")), a: workflow(run_steps("npm test"))},
        {"test": "x", "test:ci": "x"},
    )
    assert s.test_cmd is not None
    assert (s.test_cmd.value, s.test_cmd.source) == ("npm test", source(0, path=a))
    assert any("a.yml" in w and "b.yaml" in w for w in s.warnings)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.txt",
        ".github/workflows/nested/ci.yml",
        ".github/ci.yml",
        "ci.yml",
        ".gitlab-ci.yml",
        ".github/workflows/ci.yml.bak",
    ],
)
def test_only_yaml_files_directly_in_the_workflows_folder_are_read(path: str) -> None:
    assert node({path: workflow(run_steps("npm test"))}).test_cmd is None


def test_a_workflow_that_cannot_be_read_is_skipped() -> None:
    # `DictTree` returns None for nothing; an unreadable (symlink/binary/oversized) file reads as
    # None. Build one by listing the path with no content through a subclass.
    class Unreadable(DictTree):
        def read_text(self, rel_path: str) -> str | None:
            return None if rel_path == CI else super().read_text(rel_path)

    tree = Unreadable(COMMIT, {"package.json": package_json(), "package-lock.json": "{}", CI: "x"})
    s = read_ci_node(tree, detect_node(tree))
    assert s.test_cmd is None
    assert any("ci.yml" in w and "cannot be read" in w for w in s.warnings)


@pytest.mark.parametrize(
    "text",
    [
        "{{{{",
        "a: [",
        "- just\n- a list\n",
        "jobs: 3\n",
        "jobs:\n  t: 5\n",
        "jobs:\n  t:\n    steps: 5\n",
        "jobs:\n  t:\n    steps:\n      - 5\n      - [1]\n      - run: 7\n",
        "jobs:\n  t:\n    steps:\n      - run: [a, b]\n",
        "\x00\x01",
        "a: &a [*a]\n",
        "!!python/object/apply:os.system ['id']\n",
    ],
)
def test_malformed_or_hostile_yaml_never_raises(text: str) -> None:
    s = node(text)
    assert s.test_cmd is None


def test_a_yaml_anchor_chain_does_not_blow_up() -> None:
    lines = ["a0: &a0 [x, x]"] + [f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 40)]
    assert node("\n".join(lines) + "\n").test_cmd is None


def test_a_job_id_with_odd_characters_is_not_used_in_a_source_string() -> None:
    s = node(workflow(run_steps("npm test"), job="'a b#c'"), {"test": "x"})
    assert s.test_cmd is None


def test_only_so_many_workflow_files_are_read() -> None:
    files = {
        f".github/workflows/w{i:03}.yml": workflow(run_steps("npm test"))
        for i in range(MAX_WORKFLOW_FILES + 5)
    }
    s = node(files, {"test": "x"})
    assert s.test_cmd is not None
    assert any("workflow" in w and str(MAX_WORKFLOW_FILES) in w for w in s.warnings)


def test_a_workflow_past_the_file_cap_is_not_read() -> None:
    files = {
        f".github/workflows/w{i:03}.yml": workflow(run_steps("echo hi"))
        for i in range(MAX_WORKFLOW_FILES)
    }
    files[".github/workflows/z-last.yml"] = workflow(run_steps("npm test"))
    assert node(files, {"test": "x"}).test_cmd is None


def test_only_so_many_steps_are_read() -> None:
    steps = run_steps(*(["echo hi"] * MAX_STEPS)) + run_steps("npm test")
    s = node(workflow(steps), {"test": "x"})
    assert s.test_cmd is None
    assert any("steps" in w and str(MAX_STEPS) in w for w in s.warnings)


def test_numbers_in_yaml_stay_text() -> None:
    # unquoted `3.10` is the float 3.1 in plain YAML; nothing here may turn into a number
    s = python(
        workflow(
            "      - uses: actions/setup-python@v5\n        with:\n          python-version: 3.10\n"
        )
    )
    assert s.runtime_version is not None
    assert s.runtime_version.value == "3.10"


# --- Python: test, typecheck ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "pytest",
        "python -m pytest",
        "python3 -m pytest",
        "uv run pytest",
        "uv run python -m pytest",
        "poetry run pytest",
        "pdm run pytest",
        "pytest -q",
    ],
)
def test_a_pytest_step_is_adopted_with_the_venv_python(line: str) -> None:
    s = python(workflow(run_steps("pip install .", line)))
    assert s.test_cmd is not None
    assert s.test_cmd.value == f"{PY} -m pytest -q"
    assert s.test_cmd.source == source(1)


def test_pytest_paths_that_exist_in_the_tree_are_kept() -> None:
    s = python(
        workflow(run_steps("pytest tests/unit tests/test_a.py")),
        {"tests/unit/test_x.py": "", "tests/test_a.py": ""},
    )
    assert s.test_cmd is not None
    assert s.test_cmd.value == f"{PY} -m pytest -q tests/unit tests/test_a.py"


@pytest.mark.parametrize("path", ["tests/nope", "../etc", "/etc", "tests/../x", "-tests", "a b"])
def test_a_pytest_path_not_in_the_tree_is_not_adopted(path: str) -> None:
    s = python(workflow(run_steps(f"pytest '{path}'")), {"tests/unit/test_x.py": ""})
    assert s.test_cmd is None
    assert any("argument" in w for w in s.warnings)


@pytest.mark.parametrize("flag", ["-x", "-v", "-vv", "-rA", "--tb=short", "--maxfail=3"])
def test_safe_pytest_flags_are_kept(flag: str) -> None:
    s = python(workflow(run_steps(f"pytest {flag}")))
    assert s.test_cmd is not None
    assert s.test_cmd.value == f"{PY} -m pytest -q {flag}"


@pytest.mark.parametrize(
    "flag",
    [
        "--rootdir=/",
        "-c /etc/passwd",
        "-p evil",
        "--pyargs os",
        "--basetemp=/",
        "--junitxml=/etc/x",
        "-o addopts=x",
        "--confcutdir=/",
        "-n auto",
    ],
)
def test_other_pytest_flags_are_not_adopted(flag: str) -> None:
    s = python(workflow(run_steps(f"pytest {flag}")))
    assert s.test_cmd is None
    assert any("argument" in w for w in s.warnings)


def test_coverage_flags_are_dropped_with_a_note() -> None:
    s = python(workflow(run_steps("pytest --cov=pkg --cov-report=xml --no-cov-on-fail")))
    assert s.test_cmd is not None
    assert s.test_cmd.value == f"{PY} -m pytest -q"
    assert any("coverage" in w for w in s.warnings)


def test_a_marker_expression_is_kept_quoted() -> None:
    s = python(workflow(run_steps("pytest -m 'not slow and not network'")))
    assert s.test_cmd is not None
    assert s.test_cmd.value == f"{PY} -m pytest -q -m 'not slow and not network'"


@pytest.mark.parametrize("expr", ["$(id)", "a;b", "a|b", "a`b`", "'a'b'", "x" * 100])
def test_an_odd_marker_expression_is_not_adopted(expr: str) -> None:
    s = python(workflow(run_steps(f"pytest -m {json.dumps(expr)}")))
    assert s.test_cmd is None


def test_unittest_discover_is_adopted_but_unittest_with_arguments_is_not() -> None:
    s = python(workflow(run_steps("python -m unittest discover")))
    assert s.test_cmd is not None
    assert s.test_cmd.value == f"{PY} -m unittest discover"
    assert python(workflow(run_steps("python -m unittest discover -s x"))).test_cmd is None


@pytest.mark.parametrize(
    "line", ["tox", "tox -e py311", "nox -s tests", "make test", "./run-tests.sh"]
)
def test_tox_nox_and_scripts_are_not_adopted(line: str) -> None:
    assert python(workflow(run_steps(line))).test_cmd is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("mypy", f"{PY} -m mypy"),  # no target: the project's own `files` setting applies
        ("mypy src", f"{PY} -m mypy src"),
        ("python -m mypy src", f"{PY} -m mypy src"),
        ("uv run mypy src", f"{PY} -m mypy src"),
        ("poetry run mypy --strict src", f"{PY} -m mypy --strict src"),
        ("mypy --ignore-missing-imports src", f"{PY} -m mypy --ignore-missing-imports src"),
    ],
)
def test_a_mypy_step(line: str, expected: str) -> None:
    s = python(workflow(run_steps(line)), {"src/a.py": ""})
    assert s.typecheck_cmd is not None
    assert s.typecheck_cmd.value == expected
    assert s.typecheck_cmd.source == source(0)


@pytest.mark.parametrize(
    "line", ["mypy --config-file /etc/x src", "mypy --python-executable /bin/sh .", "mypy -p evil"]
)
def test_unlisted_mypy_flags_are_not_adopted(line: str) -> None:
    assert python(workflow(run_steps(line)), {"src/a.py": ""}).typecheck_cmd is None


def test_a_pyright_step() -> None:
    s = python(workflow(run_steps("pyright")))
    assert s.typecheck_cmd is not None
    assert s.typecheck_cmd.value == f"pyright --pythonpath {PY}"
    s = python(workflow(run_steps("poetry run pyright src")), {"src/a.py": ""})
    assert s.typecheck_cmd is not None
    assert s.typecheck_cmd.value == f"pyright --pythonpath {PY} src"


def test_python_has_no_build_signal() -> None:
    assert python(workflow(run_steps("python -m build"))).build_cmd is None


# --- Python: runtime version ----------------------------------------------------------------------


def setup_python(version: str, *, matrix: str = "") -> str:
    extra = f"    strategy:\n      matrix:\n        py: {matrix}\n" if matrix else ""
    steps = (
        "      - uses: actions/setup-python@v5\n        with:\n"
        f"          python-version: {version}\n"
    )
    return workflow(steps + run_steps("pytest"), extra=extra)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("'3.11'", "3.11"),
        ("3.12", "3.12"),
        ("3.10", "3.10"),
        ("'3.9.18'", "3.9"),
        ("'3.13.0-rc.1'", "3.13"),
        ("'3.x'", "3.13"),
        ("3.x", "3.13"),
        ("'3'", "3.13"),
    ],
)
def test_setup_python_version(version: str, expected: str) -> None:
    s = python(setup_python(version))
    assert s.runtime_version is not None
    assert s.runtime_version.value == expected
    assert s.runtime_version.source == source(0)


def test_a_python_matrix_picks_the_lowest_supported_even_with_unquoted_versions() -> None:
    s = python(setup_python("${{ matrix.py }}", matrix="[3.12, 3.9, 3.10]"))
    assert s.runtime_version is not None
    assert s.runtime_version.value == "3.9"


def test_a_python_without_an_image_maps_to_the_closest_with_a_warning() -> None:
    s = python(setup_python("'3.8'"))
    assert s.runtime_version is not None
    assert s.runtime_version.value == "3.9"
    assert any("3.8" in w and "3.9" in w for w in s.warnings)


@pytest.mark.parametrize("version", ["'pypy3.9'", "'pypy-3.10'", "'2.7'", "'3.1x'", "banana", "''"])
def test_an_unusable_python_version_is_ignored(version: str) -> None:
    assert python(setup_python(version)).runtime_version is None


def test_python_version_file_is_left_to_the_heuristics() -> None:
    steps = (
        "      - uses: actions/setup-python@v5\n        with:\n"
        "          python-version-file: .python-version\n"
    )
    assert python(workflow(steps)).runtime_version is None


def test_setup_node_is_not_read_for_python_nor_setup_python_for_node() -> None:
    assert python(setup_node("20")).runtime_version is None
    assert node(setup_python("'3.11'")).runtime_version is None
