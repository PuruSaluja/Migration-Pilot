"""P2-11 (SPEC §9.4): the commands `detect_python` emits, run for real.

For each tool (uv, Poetry, PDM, pip) a toy project is built in a real sandbox on the image the
detector picks: its lockfile is generated, `detect_python` reads the files (as a mirror tree),
and the detected commands run through the real install container and work container: the
frozen install (through the egress proxy), the offline test command, the JUnit flag, and,
after a dependency is added, the unfrozen install. This is what showed that Poetry only keeps
its venv in the project if the setting is on `poetry lock` too, and that PDM in the image
crashed (both found while writing this task).

Needs Docker and the network (PyPI through the proxy): marked `network`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import docker
import pytest
from docker_helpers import NONE, Runner, client, make_spec, run, run_id  # noqa: F401

from mpilot.config import SandboxSettings
from mpilot.core.types import NetworkMode, RunId
from mpilot.detect.python import detect_python, junit_invocation
from mpilot.sandbox import images as im
from mpilot.sandbox.cache import cache_key
from mpilot.sandbox.driver import DockerDriver, DockerSandbox
from mpilot.sandbox.network import EgressProxyManager, EgressSetup, system_resolve
from mpilot.testing.doubles import DictTree

pytestmark = [pytest.mark.docker, pytest.mark.network]

REGISTRY = NetworkMode.REGISTRY
KEY = cache_key("python", {"pyproject.toml": b"toy"})
BUILD = '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
TEST_FILE = "import six\n\n\ndef test_toy() -> None:\n    assert six.PY3\n"


def uv_project(extra: str) -> dict[str, str]:
    return {
        "pyproject.toml": (
            '[project]\nname = "toyproj"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
            f'dependencies = ["six"{extra}]\n'
            '[project.optional-dependencies]\ntest = ["pytest"]\n'
            f'[tool.setuptools]\npackages = ["toyproj"]\n{BUILD}'
        )
    }


def pdm_project(extra: str) -> dict[str, str]:
    return {
        "pyproject.toml": (
            '[project]\nname = "toyproj"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
            f'dependencies = ["six"{extra}]\n'
            '[project.optional-dependencies]\ntest = ["pytest"]\n'
            "[tool.pdm]\ndistribution = false\n"
        )
    }


def poetry_project(extra: str) -> dict[str, str]:
    attrs = 'attrs = "*"\n' if extra else ""
    return {
        "pyproject.toml": (
            '[tool.poetry]\nname = "toyproj"\nversion = "0.1.0"\ndescription = ""\n'
            'authors = ["x <x@example.invalid>"]\npackages = [{include = "toyproj"}]\n'
            f'[tool.poetry.dependencies]\npython = "^3.11"\nsix = "*"\n{attrs}'
            '[tool.poetry.group.test.dependencies]\npytest = "*"\n'
            '[build-system]\nrequires = ["poetry-core"]\nbuild-backend = "poetry.core.masonry.api"\n'
        )
    }


def pip_project(extra: str) -> dict[str, str]:
    return {
        "pyproject.toml": (
            '[project]\nname = "toyproj"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
            f'dependencies = ["six"{extra}]\n[tool.setuptools]\npackages = ["toyproj"]\n{BUILD}'
        ),
        "requirements-dev.txt": "pytest\n",
    }


#: tool -> (project files for a given extra-dependency text, command that writes the lockfile)
TOOLS: dict[str, tuple[Callable[[str], dict[str, str]], str | None, str | None]] = {
    "uv": (uv_project, "uv lock", "uv.lock"),
    "poetry": (poetry_project, "POETRY_VIRTUALENVS_IN_PROJECT=true poetry lock", "poetry.lock"),
    "pdm": (pdm_project, "pdm lock -G :all", "pdm.lock"),
    "pip": (pip_project, None, None),
}


def tree_of(workspace: Path) -> DictTree:
    """The workspace's text files as a mirror tree (what the host would read from the mirror)."""
    files: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        parts = path.relative_to(workspace).parts
        if not path.is_file() or {".venv", ".pytest_cache", "__pycache__"} & set(parts):
            continue
        try:
            files["/".join(parts)] = path.read_text()
        except UnicodeDecodeError:
            continue
    return DictTree("a" * 40, files)


@pytest.fixture(scope="module")
def refs() -> dict[str, str]:
    c = docker.from_env(timeout=120)
    try:
        out = {}
        for spec in im.image_specs():
            if spec.key in ("proxy:1", "python:3.11"):
                im.build_image(spec)
                out[spec.key] = im.resolve_image_ref(c, spec.tag)
        return out
    finally:
        c.close()


def write(workspace: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (workspace / name).write_text(text)


@pytest.mark.parametrize("tool", list(TOOLS))
def test_the_detected_commands_install_test_and_relock_a_real_project(
    tool: str,
    client: docker.DockerClient,
    refs: dict[str, str],
    tmp_path: Path,
    run_id: RunId,
    run: Runner,
) -> None:
    project, lock_cmd, lockfile = TOOLS[tool]
    workspace = tmp_path / "workspace"
    (workspace / "toyproj").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "toyproj" / "__init__.py").write_text("")
    (workspace / "tests" / "test_toy.py").write_text(TEST_FILE)
    write(workspace, project(""))
    (tmp_path / "cache").mkdir()
    egress = EgressSetup(
        manager=EgressProxyManager(client, resolve=system_resolve),
        settings=SandboxSettings(),
        proxy_image=refs["proxy:1"],
    )
    driver = DockerDriver(client, cache_root=tmp_path / "cache", egress=egress)
    box = run(driver.create(make_spec(run_id, refs["python:3.11"], workspace, cache_key=KEY)))
    assert isinstance(box, DockerSandbox)

    def sh(command: str, network: NetworkMode, timeout_s: int = 600) -> tuple[int, str]:
        res = run(box.exec(["sh", "-c", command], network=network, timeout_s=timeout_s))
        return res.exit_code, res.stdout_tail + res.stderr_tail

    try:
        if lock_cmd is not None:
            code, out = sh(lock_cmd, REGISTRY)
            assert code == 0, out
            assert lockfile is not None
            assert (workspace / lockfile).is_file()

        d = detect_python(tree_of(workspace))
        assert d.tool.value == tool
        assert d.runtime_version.value == "3.11", d.runtime_version
        assert d.test_runner is not None
        assert d.test_runner.value == "pytest"
        assert d.test_cmd is not None

        code, out = sh(d.install_cmd.value, REGISTRY)
        assert code == 0, (d.install_cmd.value, out)
        assert (workspace / ".venv" / "pyvenv.cfg").is_file(), "the install made .venv"

        code, out = sh(d.test_cmd.value, NONE)
        assert code == 0, (d.test_cmd.value, out)
        assert "1 passed" in out, out

        inv = junit_invocation(d, "junit.xml")
        assert inv is not None
        code, out = sh(inv.cmd, NONE)
        assert code == 0, (inv.cmd, out)
        assert "<testsuite" in (workspace / inv.output_path).read_text()

        # after the bump: a new dependency, then the unfrozen install
        write(workspace, project(', "attrs"'))
        again = detect_python(tree_of(workspace))
        code, out = sh(again.install_unfrozen_cmd.value, REGISTRY)
        assert code == 0, (again.install_unfrozen_cmd.value, out)
        code, out = sh(
            f"{again.test_cmd.value if again.test_cmd else ''} && .venv/bin/python -c 'import attrs'",
            NONE,
        )
        assert code == 0, out

        assert box.last_egress is not None
        assert box.last_egress.denials == (), box.last_egress.denials
    finally:
        run(box.destroy())
