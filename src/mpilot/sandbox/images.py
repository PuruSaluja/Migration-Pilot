"""Build the sandbox base images and keep `images/digests.lock` (SPEC §9.3, NFR-5).

The Dockerfiles under `images/` pin every base image by digest. This module

* reads those pins (`image_specs`),
* builds each image through the docker CLI (`build_image`),
* probes a built image for the versions of the tools in it (`probe_tools`),
* records the inputs and the tool versions of every image in `images/digests.lock`
  (`make_lock_entry`, `write_lock`), and
* checks that the lock still describes the Dockerfiles (`lock_problems`, no Docker needed)
  and the images on this host (`verify_images`).

**What the lock does and does not pin.** The base digests and the pinned downloads are fixed by
the Dockerfiles. apt packages (git, ripgrep) are not version-pinned: bookworm may update them
between two builds. So the lock records the versions that came out (`tools`), and
`verify_images` fails when a rebuilt image differs. The built image itself is identified by the
digest Docker reports for it (`resolve_image_ref`); a run records that digest (§9.3).

Run it as `python -m mpilot.sandbox.images {build,lock,check,verify}` or `scripts/build-images.sh`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import docker
from docker.errors import DockerException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mpilot.core import spawn
from mpilot.core.errors import MpilotError

IMAGE_PREFIX: Final = "mpilot"
LOCK_VERSION: Final = 1
LOCK_FILENAME: Final = "digests.lock"
BUILD_TIMEOUT_S: Final = 1800.0
PROBE_TIMEOUT_S: Final = 120.0
#: The env a docker CLI child gets: nothing but what it needs to find its daemon and config.
_DOCKER_CLI_ENV: Final = ("HOME", "PATH", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")

_FROM = re.compile(
    r"^FROM\s+(?P<name>[^\s@]+)@sha256:(?P<digest>[0-9a-f]{64})\s+AS\s+(?P<stage>\S+)\s*$",
    re.MULTILINE,
)
_ANY_FROM = re.compile(r"^FROM\s+(?P<rest>.+)$", re.MULTILINE)
_ARG_DEFAULT = re.compile(r"^ARG\s+(?P<name>[A-Z_]+)=(?P<value>\S*)\s*$", re.MULTILINE)
_REPO_DIGEST = re.compile(r"^(?P<name>[^@\s]+)@sha256:[0-9a-f]{64}$")


class ImageError(MpilotError):
    """An image could not be read, built, probed or verified."""


@dataclass(frozen=True)
class Ecosystem:
    name: str
    versions: tuple[str, ...]
    build_arg: str | None  # the Dockerfile ARG that selects the version (None: one version)
    #: `(label, shell command)` pairs run inside the image; the first output line is recorded.
    tools: tuple[tuple[str, str], ...]
    #: A sandbox image (work and install containers), or infrastructure (the egress proxy).
    sandbox: bool = True
    #: Build context relative to the repository root; default: the Dockerfile's directory.
    context: str | None = None
    #: Files of the context that go into the image; their hashes are part of the lock, so a
    #: changed source with a stale image fails `check`.
    sources: tuple[str, ...] = ()


_COMMON_TOOLS: Final[tuple[tuple[str, str], ...]] = (
    ("git", "git --version"),
    ("ripgrep", "rg --version"),
    ("tree-sitter", "tree-sitter --version"),
    ("dpkg:git", "dpkg-query -W -f='${Version}' git"),
    ("dpkg:ripgrep", "dpkg-query -W -f='${Version}' ripgrep"),
)

ECOSYSTEMS: Final[tuple[Ecosystem, ...]] = (
    Ecosystem(
        "node",
        ("18", "20", "22"),
        "NODE_MAJOR",
        (
            ("node", "node --version"),
            ("npm", "npm --version"),
            ("pnpm", "pnpm --version"),
            ("yarn", "yarn --version"),
            *_COMMON_TOOLS,
        ),
    ),
    Ecosystem(
        "python",
        ("3.9", "3.10", "3.11", "3.12", "3.13"),
        "PYTHON_VERSION",
        (
            ("python", "python --version"),
            ("pip", "pip --version"),
            ("uv", "uv --version"),
            ("poetry", "poetry --version"),
            ("pdm", "pdm --version"),
            ("pyright", "pyright --version"),
            *_COMMON_TOOLS,
        ),
    ),
    Ecosystem(
        "proxy",
        ("1",),
        None,
        (("python", "python --version"),),
        sandbox=False,
        context="src/mpilot/sandbox/proxy",
        sources=("server.py",),
    ),
)


@dataclass(frozen=True)
class ImageSpec:
    """One image, as the Dockerfile's pins describe it."""

    key: str  # "node:20"
    ecosystem: Ecosystem
    version: str
    tag: str  # "mpilot/node:20"
    context: Path
    base: str  # "node:20-bookworm-slim@sha256:..."
    dockerfile_sha256: str
    dockerfile: Path | None = None  # default: context / "Dockerfile"
    sources_sha256: tuple[tuple[str, str], ...] = ()

    @property
    def build_args(self) -> dict[str, str]:
        if self.ecosystem.build_arg is None:
            return {}
        return {self.ecosystem.build_arg: self.version}

    @property
    def dockerfile_path(self) -> Path:
        return self.dockerfile or self.context / "Dockerfile"


def default_images_dir() -> Path:
    """`images/` of this checkout (the package lives in `src/mpilot/sandbox/`)."""
    return Path(__file__).resolve().parents[3] / "images"


def image_specs(images_dir: Path | None = None) -> list[ImageSpec]:
    """Read every image's pin from its Dockerfile. Raises `ImageError` on a base that isn't
    pinned by digest, a missing or duplicate stage, or a Dockerfile that is not there."""
    root = images_dir or default_images_dir()
    repo = default_images_dir().parent
    specs: list[ImageSpec] = []
    for eco in ECOSYSTEMS:
        path = root / eco.name / "Dockerfile"
        context = repo / eco.context if eco.context else path.parent
        try:
            raw = path.read_bytes()
            sources = tuple(
                (name, hashlib.sha256((context / name).read_bytes()).hexdigest())
                for name in eco.sources
            )
        except OSError as exc:
            raise ImageError(f"cannot read {exc.filename}: {exc.strerror or exc}") from exc
        text = raw.decode("utf-8")
        for match in _ANY_FROM.finditer(text):
            rest = match.group("rest")
            if "${" in rest:  # the final stage: FROM base-${VERSION} AS final
                continue
            if not _FROM.fullmatch(match.group(0)):
                raise ImageError(f"{path}: FROM line is not pinned by digest: {match.group(0)!r}")
        stages = {m.group("stage"): m for m in _FROM.finditer(text)}
        defaults = {m.group("name"): m.group("value") for m in _ARG_DEFAULT.finditer(text)}
        if eco.build_arg is not None and eco.build_arg not in defaults:
            raise ImageError(f"{path}: no ARG {eco.build_arg}")
        for version in eco.versions:
            stage = stages.get(f"base-{version}")
            if stage is None:
                raise ImageError(f"{path}: no pinned FROM ... AS base-{version}")
            specs.append(
                ImageSpec(
                    key=f"{eco.name}:{version}",
                    ecosystem=eco,
                    version=version,
                    tag=f"{IMAGE_PREFIX}/{eco.name}:{version}",
                    context=context,
                    base=f"{stage.group('name')}@sha256:{stage.group('digest')}",
                    dockerfile_sha256=hashlib.sha256(raw).hexdigest(),
                    dockerfile=path,
                    sources_sha256=sources,
                )
            )
        extra = sorted(set(stages) - {f"base-{v}" for v in eco.versions})
        if extra:
            raise ImageError(f"{path}: stages with no image listed in ECOSYSTEMS: {extra}")
    return specs


# --- the lock ---------------------------------------------------------------------------


class LockEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    base: str = Field(pattern=r"^[^@\s]+@sha256:[0-9a-f]{64}$")
    dockerfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_args: dict[str, str]
    tools: dict[str, str]
    #: SHA-256 of each source file copied into the image (the proxy's server), by name.
    sources_sha256: dict[str, str] = Field(default_factory=dict)


class DigestsLock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = LOCK_VERSION
    images: dict[str, LockEntry]


def read_lock(path: Path) -> DigestsLock:
    try:
        return DigestsLock.model_validate_json(path.read_bytes())
    except OSError as exc:
        raise ImageError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except ValidationError as exc:
        raise ImageError(f"{path} is not a valid lock file: {exc}") from exc


def render_lock(lock: DigestsLock) -> str:
    """Deterministic text: sorted keys, 2-space indent, one trailing newline."""
    data = lock.model_dump(mode="json")
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def write_lock(path: Path, lock: DigestsLock) -> None:
    path.write_text(render_lock(lock), encoding="utf-8")


def make_lock_entry(spec: ImageSpec, tools: Mapping[str, str]) -> LockEntry:
    return LockEntry(
        tag=spec.tag,
        base=spec.base,
        dockerfile_sha256=spec.dockerfile_sha256,
        build_args=spec.build_args,
        tools=dict(tools),
        sources_sha256=dict(spec.sources_sha256),
    )


def lock_problems(specs: Sequence[ImageSpec], lock: DigestsLock) -> list[str]:
    """Compare the lock with the Dockerfiles. Needs no Docker. Empty means they agree."""
    problems: list[str] = []
    if lock.version != LOCK_VERSION:
        problems.append(f"lock version {lock.version}, expected {LOCK_VERSION}")
    wanted = {s.key: s for s in specs}
    for key in sorted(set(wanted) - set(lock.images)):
        problems.append(f"{key}: no entry in the lock")
    for key in sorted(set(lock.images) - set(wanted)):
        problems.append(f"{key}: lock entry for an image the Dockerfiles do not define")
    for key, spec in wanted.items():
        entry = lock.images.get(key)
        if entry is None:
            continue
        if entry.tag != spec.tag:
            problems.append(f"{key}: tag {entry.tag!r} in the lock, {spec.tag!r} expected")
        if entry.base != spec.base:
            problems.append(
                f"{key}: base {entry.base} in the lock, the Dockerfile pins {spec.base}"
            )
        if entry.dockerfile_sha256 != spec.dockerfile_sha256:
            problems.append(f"{key}: the Dockerfile changed since the lock was written")
        if entry.sources_sha256 != dict(spec.sources_sha256):
            changed = sorted(set(entry.sources_sha256) ^ {n for n, _ in spec.sources_sha256}) or [
                n for n, h in spec.sources_sha256 if entry.sources_sha256.get(n) != h
            ]
            problems.append(f"{key}: source(s) {changed} changed since the lock was written")
        if entry.build_args != spec.build_args:
            problems.append(
                f"{key}: build args {entry.build_args} in the lock, {spec.build_args} expected"
            )
        missing = [label for label, _ in spec.ecosystem.tools if label not in entry.tools]
        if missing:
            problems.append(f"{key}: no recorded version for {missing}")
        unknown = sorted(set(entry.tools) - {label for label, _ in spec.ecosystem.tools})
        if unknown:
            problems.append(f"{key}: recorded versions for unknown tools {unknown}")
    return problems


# --- docker ------------------------------------------------------------------------------


def _docker_cli() -> str:
    path = shutil.which("docker")
    if path is None or not Path(path).is_absolute():
        raise ImageError("the docker CLI is not on PATH")
    return path


def build_image(spec: ImageSpec, *, no_cache: bool = False) -> None:
    """`docker build` one image. The base is pinned by digest, so nothing is pulled needlessly."""
    argv = [_docker_cli(), "build", "--quiet"]
    if no_cache:
        argv.append("--no-cache")
    for name, value in spec.build_args.items():
        argv += ["--build-arg", f"{name}={value}"]
    argv += ["--file", str(spec.dockerfile_path), "--tag", spec.tag, str(spec.context)]
    result = spawn.run(argv, env=spawn.inherit(*_DOCKER_CLI_ENV), timeout_s=BUILD_TIMEOUT_S)
    if result.timed_out:
        raise ImageError(f"{spec.key}: build timed out after {BUILD_TIMEOUT_S:.0f} s")
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-30:]
        raise ImageError(
            f"{spec.key}: docker build failed ({result.returncode}):\n" + "\n".join(tail)
        )


def resolve_image_ref(client: docker.DockerClient, tag: str) -> str:
    """The `name@sha256:<digest>` form of a built image, which `SandboxSpec.image` requires.

    Needs a digest Docker holds for the image (the containerd image store gives every local
    image one). An engine that doesn't is refused rather than guessed at: an image id is not a
    digest the engine can resolve there.
    """
    try:
        digests: list[str] = client.images.get(tag).attrs.get("RepoDigests") or []
    except DockerException as exc:
        raise ImageError(
            f"{tag}: not built on this host (build it with scripts/build-images.sh)"
        ) from exc
    name = tag.rsplit(":", 1)[0]
    for digest in digests:
        match = _REPO_DIGEST.match(digest)
        if match and match.group("name") == name:
            return digest
    raise ImageError(f"{tag}: the engine reports no digest for it (RepoDigests={digests})")


def _probe_script(eco: Ecosystem) -> str:
    lines = []
    for label, command in eco.tools:
        # `|| true`: a missing tool must show up as an empty value, not stop the probe.
        lines.append(f"printf '%s=%s\\n' '{label}' \"$({command} 2>&1 | head -n 1 || true)\"")
    return "\n".join(lines)


def parse_probe(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        label, sep, value = line.partition("=")
        if sep and label:
            result[label] = value.strip()
    return result


def probe_tools(client: docker.DockerClient, spec: ImageSpec) -> dict[str, str]:
    """Run the image once, offline and with every capability dropped, and read tool versions.

    Raises `ImageError` when a tool printed nothing (it is missing or broken)."""
    try:
        raw = client.containers.run(
            spec.tag,
            [_probe_script(spec.ecosystem)],
            entrypoint=["/bin/sh", "-c"],  # the proxy image's entrypoint is the server
            network_mode="none",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            read_only=True,
            tmpfs={"/tmp": "size=16m,mode=1777"},  # noqa: S108  (inside the container)
            remove=True,
            stdout=True,
            stderr=False,
        )
    except DockerException as exc:
        raise ImageError(f"{spec.key}: probe failed: {exc}") from exc
    tools = parse_probe(bytes(raw).decode("utf-8", "replace"))
    empty = [label for label, _ in spec.ecosystem.tools if not tools.get(label)]
    if empty:
        raise ImageError(f"{spec.key}: no version printed for {empty}")
    return tools


def verify_images(
    client: docker.DockerClient, specs: Sequence[ImageSpec], lock: DigestsLock
) -> list[str]:
    """`lock_problems`, plus: every image exists here and reports the locked tool versions."""
    problems = lock_problems(specs, lock)
    for spec in specs:
        entry = lock.images.get(spec.key)
        if entry is None:
            continue
        try:
            tools = probe_tools(client, spec)
        except ImageError as exc:
            problems.append(str(exc))
            continue
        for label, locked in sorted(entry.tools.items()):
            if tools.get(label) != locked:
                problems.append(
                    f"{spec.key}: {label} is {tools.get(label)!r}, the lock has {locked!r}"
                )
    return problems


# --- command line ------------------------------------------------------------------------


def _select(specs: Sequence[ImageSpec], only: Sequence[str]) -> list[ImageSpec]:
    if not only:
        return list(specs)
    known = {s.key for s in specs}
    unknown = sorted(set(only) - known)
    if unknown:
        raise ImageError(f"unknown image(s) {unknown}; known: {sorted(known)}")
    return [s for s in specs if s.key in only]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-images", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("build", "lock", "check", "verify"))
    parser.add_argument("images", nargs="*", help="image keys such as node:20 (default: all)")
    parser.add_argument("--images-dir", type=Path, default=None)
    parser.add_argument("--no-cache", action="store_true", help="build without the layer cache")
    args = parser.parse_args(argv)
    try:
        root = args.images_dir or default_images_dir()
        specs = image_specs(root)
        lock_path = root / LOCK_FILENAME
        if args.command == "check":
            problems = lock_problems(specs, read_lock(lock_path))
        else:
            if args.command == "lock" and args.images:
                raise ImageError("`lock` rewrites the whole file; do not name images")
            chosen = _select(specs, args.images)
            client = docker.from_env(timeout=120)
            problems = []
            if args.command in ("build", "lock"):
                for spec in chosen:
                    sys.stdout.write(f"building {spec.tag}\n")
                    sys.stdout.flush()
                    build_image(spec, no_cache=args.no_cache)
            if args.command == "lock":
                entries = {s.key: make_lock_entry(s, probe_tools(client, s)) for s in specs}
                write_lock(lock_path, DigestsLock(images=entries))
                sys.stdout.write(f"wrote {lock_path}\n")
            elif args.command == "verify":
                problems = verify_images(client, chosen, read_lock(lock_path))
    except (ImageError, spawn.SpawnError, DockerException) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    for problem in problems:
        sys.stderr.write(f"problem: {problem}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
