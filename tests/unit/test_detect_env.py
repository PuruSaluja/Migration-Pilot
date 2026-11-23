"""P2-12b (SPEC FR-3, §9.4): the merge into an `EnvProfile`.

Precedence, per field: user override > `mpilot.toml` > CI config > heuristics. The result says
where each field came from in `detection_sources`. The install command is never taken from CI;
the image digest comes from an injected resolver (no Docker here).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_store import RUN, lease_for, run

from mpilot.core.clock import FakeClock
from mpilot.core.errors import InputError
from mpilot.detect.env import (
    EnvDetection,
    EnvDetectionError,
    detect_env,
    persist_env,
)
from mpilot.knowledge.base import DetectOverrides
from mpilot.models.domain import Ecosystem, EnvProfile
from mpilot.models.store import SqliteRunStore
from mpilot.testing.doubles import DictTree
from mpilot.testing.infra_doubles import MemoryStore

COMMIT = "a" * 40
CI = ".github/workflows/ci.yml"
DIGEST = "ab" * 32
PY = ".venv/bin/python"


class Resolver:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, tag: str) -> str:
        self.asked.append(tag)
        return f"{tag}@sha256:{DIGEST}"


def node_files(**over: str) -> dict[str, str]:
    return {
        "package.json": json.dumps(
            {
                "name": "x",
                "scripts": {
                    "test": "jest",
                    "test:ci": "jest --ci",
                    "build": "tsc",
                    "typecheck": "tsc --noEmit",
                },
                "engines": {"node": ">=18"},
            }
        ),
        "package-lock.json": "{}",
        **over,
    }


def python_files(**over: str) -> dict[str, str]:
    return {
        "pyproject.toml": '[project]\nname = "x"\nversion = "0"\n[tool.mypy]\n',
        "requirements.txt": "pytest\nmypy\n",
        **over,
    }


def workflow(*lines: str, extra: str = "") -> str:
    steps = "".join(f"      - run: {line}\n" for line in lines)
    return f"on: push\njobs:\n  test:\n{extra}    steps:\n{steps}"


def detect(
    files: Mapping[str, str],
    *,
    resolver: Resolver | None = None,
    **kwargs: object,
) -> EnvDetection:
    return detect_env(
        DictTree(COMMIT, files),
        image_ref=resolver or Resolver(),
        **kwargs,  # type: ignore[arg-type]
    )


# --- the heuristic result ---------------------------------------------------------------------


def test_a_node_repo_becomes_an_env_profile() -> None:
    d = detect(node_files())
    assert d.profile == EnvProfile(
        language=Ecosystem.NODE,
        runtime_version="18",
        package_manager="npm",
        install_cmd="npm ci",
        build_cmd="npm run build",
        typecheck_cmd="npm run typecheck",
        test_cmd="npm test",
        image_digest=f"mpilot/node:18@sha256:{DIGEST}",
        lockfile_hashes=d.profile.lockfile_hashes,
        detection_sources=d.profile.detection_sources,
    )
    assert set(d.profile.lockfile_hashes) == {"package-lock.json"}
    assert d.install_unfrozen_cmd.value == "npm install"


def test_a_python_repo_becomes_an_env_profile() -> None:
    d = detect(python_files())
    p = d.profile
    assert (p.language, p.package_manager, p.test_cmd) == (
        Ecosystem.PYTHON,
        "pip",
        f"{PY} -m pytest -q",
    )
    assert p.typecheck_cmd is not None
    assert p.typecheck_cmd.startswith(f"{PY} -m mypy")
    assert p.build_cmd is None
    assert p.image_digest == f"mpilot/python:{p.runtime_version}@sha256:{DIGEST}"


def test_every_field_has_a_source() -> None:
    p = detect(node_files()).profile
    assert p.detection_sources == {
        "runtime_version": "package.json#engines.node",
        "package_manager": p.detection_sources["package_manager"],
        "install_cmd": "package-lock.json",
        "build_cmd": "package.json#scripts.build",
        "typecheck_cmd": "package.json#scripts.typecheck",
        "test_cmd": "package.json#scripts.test",
        "image_digest": "image mpilot/node:18",
        "lockfile_hashes": "package-lock.json",
    }
    assert p.detection_sources["package_manager"]


def test_fields_that_are_absent_have_no_source() -> None:
    p = detect(python_files(**{"pyproject.toml": '[project]\nname = "x"\n'})).profile
    assert "build_cmd" not in p.detection_sources
    assert "typecheck_cmd" not in p.detection_sources


# --- precedence ---------------------------------------------------------------------------------


def with_ci(*lines: str, **files: str) -> dict[str, str]:
    return node_files(**{CI: workflow(*lines)}, **files)


USER = DetectOverrides(test_cmd="user test", build_cmd="user build", typecheck_cmd="user tc")
CONFIG = DetectOverrides(test_cmd="cfg test", build_cmd="cfg build", typecheck_cmd="cfg tc")


@pytest.mark.parametrize("field", ["test_cmd", "build_cmd", "typecheck_cmd"])
def test_user_beats_config_beats_ci_beats_heuristics(field: str) -> None:
    ci_lines = ("npm run test:ci", "npm run build", "npm run typecheck")
    files = with_ci(*ci_lines)
    heuristic = detect(node_files()).profile
    ci = detect(files).profile
    user_layer = DetectOverrides(**{field: "user cmd"})
    config_layer = DetectOverrides(**{field: "cfg cmd"})

    assert getattr(detect(node_files(), user=user_layer, config=config_layer).profile, field) == (
        "user cmd"
    )
    assert (
        detect(node_files(), user=user_layer, config=config_layer).profile.detection_sources[field]
        == "override"
    )
    c = detect(files, config=config_layer).profile
    assert getattr(c, field) == "cfg cmd"
    assert c.detection_sources[field] == f"mpilot.toml#detect.{field}"
    assert getattr(ci, field) == getattr(detect(files).profile, field)
    assert ci.detection_sources[field].startswith(CI + "#jobs.test.steps[")
    assert getattr(heuristic, field) != getattr(ci, field) or field != "test_cmd"
    assert heuristic.detection_sources[field].startswith("package.json")


def test_ci_beats_the_heuristic_test_command() -> None:
    p = detect(with_ci("npm run test:ci")).profile
    assert p.test_cmd == "npm run test:ci"
    assert p.detection_sources["test_cmd"] == f"{CI}#jobs.test.steps[0]"


def test_a_field_ci_does_not_set_falls_through_to_the_heuristic() -> None:
    p = detect(with_ci("npm run test:ci")).profile
    assert p.build_cmd == "npm run build"
    assert p.detection_sources["build_cmd"] == "package.json#scripts.build"


def test_the_user_layer_beats_config_for_a_single_field_only() -> None:
    p = detect(
        node_files(),
        user=DetectOverrides(test_cmd="user test"),
        config=DetectOverrides(test_cmd="cfg test", build_cmd="cfg build"),
    ).profile
    assert (p.test_cmd, p.build_cmd) == ("user test", "cfg build")
    assert p.detection_sources["test_cmd"] == "override"
    assert p.detection_sources["build_cmd"] == "mpilot.toml#detect.build_cmd"


def test_the_install_command_comes_from_overrides_or_the_lockfile_never_from_ci() -> None:
    p = detect(with_ci("npm install --legacy-peer-deps", "npm run test:ci")).profile
    assert p.install_cmd == "npm ci"
    assert p.detection_sources["install_cmd"] == "package-lock.json"


def test_an_install_override_is_also_the_post_bump_install_and_says_so() -> None:
    d = detect(node_files(), config=DetectOverrides(install_cmd="make deps"))
    assert d.profile.install_cmd == "make deps"
    assert d.install_unfrozen_cmd.value == "make deps"
    assert d.install_unfrozen_cmd.source == "mpilot.toml#detect.install_cmd"
    assert any("install_cmd" in w and "after the bump" in w for w in d.warnings)


def test_the_user_install_override_beats_the_config_one() -> None:
    d = detect(
        node_files(),
        user=DetectOverrides(install_cmd="a"),
        config=DetectOverrides(install_cmd="b"),
    )
    assert (d.profile.install_cmd, d.install_unfrozen_cmd.source) == ("a", "override")


def test_a_ci_warning_reaches_the_result() -> None:
    d = detect(with_ci("npm test -- --coverage"))
    assert any("argument" in w for w in d.warnings)


# --- runtime version ----------------------------------------------------------------------------


def setup_node(version: str) -> str:
    return (
        "on: push\njobs:\n  t:\n    steps:\n      - uses: actions/setup-node@v4\n"
        f"        with:\n          node-version: {version}\n"
    )


def test_ci_runtime_beats_the_heuristic_and_overrides_beat_ci() -> None:
    files = node_files(**{CI: setup_node("22")})
    assert detect(files).profile.runtime_version == "22"
    assert detect(files).profile.detection_sources["runtime_version"] == f"{CI}#jobs.t.steps[0]"
    cfg = detect(files, config=DetectOverrides(runtime_version="20")).profile
    assert (cfg.runtime_version, cfg.detection_sources["runtime_version"]) == (
        "20",
        "mpilot.toml#detect.runtime_version",
    )
    usr = detect(
        files,
        user=DetectOverrides(runtime_version="v18"),
        config=DetectOverrides(runtime_version="20"),
    ).profile
    assert (usr.runtime_version, usr.detection_sources["runtime_version"]) == ("18", "override")


@pytest.mark.parametrize(
    ("given", "expected"), [("20", "20"), ("v22", "22"), ("18.20.8", "18"), ("20.11", "20")]
)
def test_a_node_runtime_override_names_an_image(given: str, expected: str) -> None:
    p = detect(node_files(), user=DetectOverrides(runtime_version=given)).profile
    assert p.runtime_version == expected
    assert p.image_digest.startswith(f"mpilot/node:{expected}@")


@pytest.mark.parametrize("given", ["16", "21", "24", "0"])
def test_a_node_runtime_override_without_an_image_is_refused_not_guessed(given: str) -> None:
    with pytest.raises(EnvDetectionError, match=r"no image.*18, 20, 22"):
        detect(node_files(), user=DetectOverrides(runtime_version=given))


@pytest.mark.parametrize(
    ("given", "expected"), [("3.12", "3.12"), ("3.9.18", "3.9"), ("3.13.1", "3.13")]
)
def test_a_python_runtime_override(given: str, expected: str) -> None:
    p = detect(python_files(), config=DetectOverrides(runtime_version=given)).profile
    assert p.runtime_version == expected
    assert p.image_digest.startswith(f"mpilot/python:{expected}@")


@pytest.mark.parametrize("given", ["3.8", "3.14", "3", "20", "2.7"])
def test_a_python_runtime_override_without_an_image_is_refused(given: str) -> None:
    with pytest.raises(EnvDetectionError, match=r"no image.*3.9"):
        detect(python_files(), user=DetectOverrides(runtime_version=given))


# --- the image digest ---------------------------------------------------------------------------


def test_the_resolver_is_asked_for_the_detected_runtimes_image_once() -> None:
    resolver = Resolver()
    detect(node_files(), resolver=resolver, user=DetectOverrides(runtime_version="22"))
    assert resolver.asked == ["mpilot/node:22"]


def test_a_failing_resolver_fails_detection() -> None:
    def broken(tag: str) -> str:
        raise InputError(f"image {tag} is not built")

    with pytest.raises(InputError, match="mpilot/node:18 is not built"):
        detect_env(DictTree(COMMIT, node_files()), image_ref=broken)


def test_the_resolver_is_not_called_when_detection_fails_first() -> None:
    resolver = Resolver()
    with pytest.raises(EnvDetectionError):
        detect(
            {"package.json": json.dumps({"scripts": {}}), "package-lock.json": "{}"},
            resolver=resolver,
        )
    assert resolver.asked == []


# --- no test command, ecosystem ----------------------------------------------------------------


def test_no_test_command_anywhere_says_how_to_set_one() -> None:
    files = {"package.json": json.dumps({"name": "x"}), "package-lock.json": "{}"}
    with pytest.raises(EnvDetectionError, match=r"\[detect\]\.test_cmd.*--test-cmd") as info:
        detect(files)
    assert isinstance(info.value, InputError)


def test_a_test_override_fills_a_missing_test_command() -> None:
    files = {"package.json": json.dumps({"name": "x"}), "package-lock.json": "{}"}
    assert detect(files, user=DetectOverrides(test_cmd="make test")).profile.test_cmd == "make test"


def test_an_empty_directory_is_not_a_project() -> None:
    with pytest.raises(EnvDetectionError, match=r"no package.json or Python project"):
        detect({"README.md": "hi"})


def test_a_repo_with_both_ecosystems_needs_one_named() -> None:
    files = {**node_files(), **python_files()}
    with pytest.raises(EnvDetectionError, match=r"both.*ecosystem"):
        detect(files)
    assert detect(files, ecosystem=Ecosystem.NODE).profile.language is Ecosystem.NODE
    assert detect(files, ecosystem=Ecosystem.PYTHON).profile.language is Ecosystem.PYTHON


def test_a_named_ecosystem_the_repo_does_not_have_is_an_error() -> None:
    with pytest.raises(InputError):
        detect(node_files(), ecosystem=Ecosystem.PYTHON)


def test_java_is_not_supported_yet() -> None:
    with pytest.raises(EnvDetectionError, match="Java"):
        detect(node_files(), ecosystem=Ecosystem.JAVA)


# --- JUnit -------------------------------------------------------------------------------------


def test_junit_flags_follow_the_heuristic_command_only() -> None:
    files = node_files(
        **{
            "package.json": json.dumps(
                {
                    "name": "x",
                    "scripts": {"test": "vitest run", "test:ci": "vitest run"},
                    "devDependencies": {"vitest": "1"},
                }
            )
        }
    )
    plain = detect(files)
    assert plain.junit("junit.xml") is not None
    replaced = detect(files, user=DetectOverrides(test_cmd="make test"))
    assert replaced.junit("junit.xml") is None  # not the command the flags were built for
    python = detect(python_files())
    inv = python.junit("junit.xml")
    assert inv is not None
    assert inv.cmd.endswith("--junitxml=junit.xml")


# --- persistence --------------------------------------------------------------------------------


def stores(tmp_path: Path) -> list[object]:
    return [
        MemoryStore(),
        SqliteRunStore.open(tmp_path / "mpilot.db", FakeClock(datetime(2026, 11, 22, tzinfo=UTC))),
    ]


def test_the_profile_is_stored_on_the_run(tmp_path: Path) -> None:
    profile = detect(node_files()).profile
    for store in stores(tmp_path):
        store.create_run(run(env=None))  # type: ignore[attr-defined]
        lease = lease_for(store)
        stored = persist_env(store, RUN, profile, lease=lease)  # type: ignore[arg-type]
        assert stored.env == profile
        assert store.get_run(RUN).env == profile  # type: ignore[attr-defined]


def test_persisting_does_not_touch_anything_else_on_the_run(tmp_path: Path) -> None:
    profile = detect(node_files()).profile
    for store in stores(tmp_path):
        before = run(env=None)
        store.create_run(before)  # type: ignore[attr-defined]
        persist_env(store, RUN, profile, lease=lease_for(store))  # type: ignore[arg-type]
        after = store.get_run(RUN)  # type: ignore[attr-defined]
        assert after.model_copy(update={"env": None}) == before


def test_persisting_needs_the_runs_lease(tmp_path: Path) -> None:
    from mpilot.core.errors import LeaseLostError

    profile = detect(node_files()).profile
    for store in stores(tmp_path):
        store.create_run(run(env=None))  # type: ignore[attr-defined]
        held = lease_for(store)
        stale = held.model_copy(update={"token": held.token + 5})
        with pytest.raises(LeaseLostError):
            persist_env(store, RUN, profile, lease=stale)  # type: ignore[arg-type]
        assert store.get_run(RUN).env is None  # type: ignore[attr-defined]


def test_an_unknown_run_is_a_key_error(tmp_path: Path) -> None:
    profile = detect(node_files()).profile
    store = MemoryStore()
    store.create_run(run(env=None))
    held = lease_for(store)
    with pytest.raises(KeyError):
        persist_env(store, RUN.__class__("nope"), profile, lease=held)
