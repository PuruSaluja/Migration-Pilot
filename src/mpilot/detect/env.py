"""Environment detection: the merge into an `EnvProfile` (SPEC FR-3, §9.4; P2-12b).

Per field the order is: user override (`--test-cmd`, ...) > `mpilot.toml` `[detect]` > CI config
(`detect/ci.py`) > ecosystem heuristics (`detect/node.py`, `detect/python.py`). Every field of the
result says where it came from in `EnvProfile.detection_sources`: `override`,
`mpilot.toml#detect.<field>`, a CI step (`<workflow>#jobs.<job>.steps[<n>]`), or the heuristic's
own source (a file or `file#field`).

- The install command is never taken from CI: the lockfile decides between the frozen form
  (baseline) and the unfrozen form (after the bump). An install override replaces both, and says so.
- A runtime override must name an image that exists (node 18/20/22, python 3.9-3.13); it is
  refused, not rounded, because the owner asked for it by name. Runtimes found in files are
  mapped to the closest image by the detectors, with a warning.
- Nothing is run and nothing is downloaded: the `EnvProfile` is built from the tree, plus the
  image digest from an injected resolver (`resolve_image_ref` in production).
- The unfrozen install command, the JUnit invocation and the detections themselves are not part
  of `EnvProfile` (a frozen Phase 1 schema); `EnvDetection` carries them for the tasks that need
  them, and a later run can re-derive them from the same commit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from mpilot.core.errors import InputError
from mpilot.core.types import RunId
from mpilot.detect.ci import CiSignals, read_ci_node, read_ci_python
from mpilot.detect.common import Detected, JunitInvocation
from mpilot.detect.node import (
    NODE_IMAGE_VERSIONS,
    NodeDetection,
    detect_node,
    is_node_project,
)
from mpilot.detect.node import junit_invocation as node_junit
from mpilot.detect.python import (
    PYTHON_IMAGE_VERSIONS,
    PythonDetection,
    detect_python,
    is_python_project,
)
from mpilot.detect.python import junit_invocation as python_junit
from mpilot.knowledge.base import DetectOverrides
from mpilot.models.coordination import Lease
from mpilot.models.domain import Ecosystem, EnvProfile, Run
from mpilot.orchestrator.base import RunStore
from mpilot.policy.gate import TreeReader
from mpilot.sandbox import images

ImageRef = Callable[[str], str]
"""Image tag (`mpilot/node:20`) -> the `name@sha256:<digest>` form (`resolve_image_ref`)."""

_NODE_OVERRIDE: Final = re.compile(r"v?(\d+)(?:\.\d+){0,2}")
_PYTHON_OVERRIDE: Final = re.compile(r"(3)\.(\d+)(?:\.\d+)?")
_NO_OVERRIDE: Final = DetectOverrides()


class EnvDetectionError(InputError):
    """Detection cannot produce a usable `EnvProfile` (exit 2)."""


@dataclass(frozen=True)
class EnvDetection:
    profile: EnvProfile
    install_unfrozen_cmd: Detected  # what the bump runs (the profile's is the baseline one)
    warnings: tuple[str, ...]
    node: NodeDetection | None = None
    python: PythonDetection | None = None
    heuristic_test_cmd: str | None = None

    def junit(self, output_path: str) -> JunitInvocation | None:
        """The test command with JUnit flags, only while the profile's test command is the
        heuristic one (the flags are built for that command; an override or a CI step is run
        as it is and falls back to log parsing)."""
        if self.profile.test_cmd != self.heuristic_test_cmd:
            return None
        if self.node is not None:
            return node_junit(self.node, output_path)
        if self.python is not None:
            return python_junit(self.python, output_path)
        return None


def _pick(
    name: str,
    user: str | None,
    config: str | None,
    ci: Detected | None,
    heuristic: Detected | None,
) -> Detected | None:
    if user is not None:
        return Detected(user, "override")
    if config is not None:
        return Detected(config, f"mpilot.toml#detect.{name}")
    return ci or heuristic


def _override_image_version(ecosystem: Ecosystem, given: str) -> str:
    if ecosystem is Ecosystem.NODE:
        available = sorted(NODE_IMAGE_VERSIONS, key=int)
        match = _NODE_OVERRIDE.fullmatch(given)
        if match is not None and match[1] in NODE_IMAGE_VERSIONS:
            return match[1]
    else:
        available = sorted(PYTHON_IMAGE_VERSIONS, key=lambda v: int(v.split(".")[1]))
        match = _PYTHON_OVERRIDE.fullmatch(given)
        if match is not None and f"3.{match[2]}" in PYTHON_IMAGE_VERSIONS:
            return f"3.{match[2]}"
    raise EnvDetectionError(
        f"runtime_version {given!r} has no image for {ecosystem.value}; "
        f"available: {', '.join(available)}"
    )


def _image_tag(ecosystem: Ecosystem, version: str) -> str:
    key = f"{ecosystem.value}:{version}"
    for spec in images.image_specs():
        if spec.key == key:
            return spec.tag
    raise EnvDetectionError(f"no sandbox image is defined for {key}")  # unreachable in practice


def _choose_ecosystem(tree: TreeReader, ecosystem: Ecosystem | None) -> Ecosystem:
    if ecosystem is Ecosystem.JAVA:
        raise EnvDetectionError("Java (Maven/Gradle) detection comes in Phase 10")
    if ecosystem is not None:
        return ecosystem
    node, python = is_node_project(tree), is_python_project(tree)
    if node and python:
        raise EnvDetectionError(
            "the repository has both a package.json and Python project files; "
            "name the ecosystem (--ecosystem node|python)"
        )
    if node:
        return Ecosystem.NODE
    if python:
        return Ecosystem.PYTHON
    raise EnvDetectionError("no package.json or Python project files in the repository root")


def detect_env(
    tree: TreeReader,
    *,
    image_ref: ImageRef,
    ecosystem: Ecosystem | None = None,
    user: DetectOverrides = _NO_OVERRIDE,
    config: DetectOverrides = _NO_OVERRIDE,
) -> EnvDetection:
    """Detect the environment of `tree`'s root. `user` is the command-line layer, `config` the
    `[detect]` section of mpilot.toml. Raises `EnvDetectionError` (an `InputError`, exit 2) when
    there is no project, two ecosystems and none named, no test command from any layer, or a
    runtime override with no image."""
    chosen = _choose_ecosystem(tree, ecosystem)
    node: NodeDetection | None = None
    python: PythonDetection | None = None
    ci: CiSignals
    if chosen is Ecosystem.NODE:
        node = detect_node(tree)
        ci = read_ci_node(tree, node)
        manager = node.package_manager
        heuristic = (
            node.test_cmd,
            node.build_cmd,
            node.typecheck_cmd,
            node.install_cmd,
            node.install_unfrozen_cmd,
            node.runtime_version,
        )
        lockfiles, hashes = node.lockfiles, node.lockfile_hashes
        warnings = [*node.warnings, *ci.warnings]
    else:
        python = detect_python(tree)
        ci = read_ci_python(tree, python)
        manager = python.tool
        heuristic = (
            python.test_cmd,
            None,
            python.typecheck_cmd,
            python.install_cmd,
            python.install_unfrozen_cmd,
            python.runtime_version,
        )
        lockfiles, hashes = python.lockfiles, python.lockfile_hashes
        warnings = [*python.warnings, *ci.warnings]
    h_test, h_build, h_typecheck, h_install, h_unfrozen, h_runtime = heuristic

    test = _pick("test_cmd", user.test_cmd, config.test_cmd, ci.test_cmd, h_test)
    if test is None:
        raise EnvDetectionError(
            "no test command was detected; set `[detect].test_cmd` in mpilot.toml "
            "or pass --test-cmd"
        )
    build = _pick("build_cmd", user.build_cmd, config.build_cmd, ci.build_cmd, h_build)
    typecheck = _pick(
        "typecheck_cmd", user.typecheck_cmd, config.typecheck_cmd, ci.typecheck_cmd, h_typecheck
    )
    install = _pick("install_cmd", user.install_cmd, config.install_cmd, None, h_install)
    unfrozen = _pick("install_cmd", user.install_cmd, config.install_cmd, None, h_unfrozen)
    if install is None or unfrozen is None:  # both detectors always find an install command
        raise EnvDetectionError("no install command was detected")
    if install.source in {"override"} or install.source.startswith("mpilot.toml#"):
        warnings.append(
            "install_cmd is overridden: the same command is used for the install after the bump"
        )

    runtime_override = user.runtime_version or config.runtime_version
    if runtime_override is not None:
        version = _override_image_version(chosen, runtime_override)
        layer = "override" if user.runtime_version else "mpilot.toml#detect.runtime_version"
        runtime = Detected(version, layer)
    else:
        runtime = ci.runtime_version or h_runtime

    tag = _image_tag(chosen, runtime.value)
    digest = image_ref(tag)
    sources = {
        "runtime_version": runtime.source,
        "package_manager": manager.source,
        "install_cmd": install.source,
        "test_cmd": test.source,
        "image_digest": f"image {tag}",
    }
    if build is not None:
        sources["build_cmd"] = build.source
    if typecheck is not None:
        sources["typecheck_cmd"] = typecheck.source
    if hashes:
        sources["lockfile_hashes"] = ", ".join(lockfiles)
    profile = EnvProfile(
        language=chosen,
        runtime_version=runtime.value,
        package_manager=manager.value,
        install_cmd=install.value,
        build_cmd=build.value if build else None,
        typecheck_cmd=typecheck.value if typecheck else None,
        test_cmd=test.value,
        image_digest=digest,
        lockfile_hashes=dict(hashes),
        detection_sources=sources,
    )
    return EnvDetection(
        profile=profile,
        install_unfrozen_cmd=unfrozen,
        warnings=tuple(dict.fromkeys(warnings)),
        node=node,
        python=python,
        heuristic_test_cmd=h_test.value if h_test else None,
    )


def persist_env(store: RunStore, run_id: RunId, profile: EnvProfile, *, lease: Lease) -> Run:
    """Store `profile` as the run's `env` (FR-3: the result is persisted). Needs the run's
    driver lease; changes nothing else on the run. `KeyError` for an unknown run."""
    updated = store.get_run(run_id).model_copy(update={"env": profile})
    store.save_run(updated, lease=lease)
    return updated
