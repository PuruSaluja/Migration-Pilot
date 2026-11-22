"""P2-07 (SPEC §9.3, NFR-5): the image Dockerfiles, `images/digests.lock` and the build module,
without Docker. The real builds are in `test_images_docker.py`.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from mpilot.sandbox import images as im
from mpilot.sandbox.images import (
    DigestsLock,
    ImageError,
    ImageSpec,
    LockEntry,
    image_specs,
    lock_problems,
    read_lock,
    render_lock,
)

IMAGES_DIR = im.default_images_dir()
LOCK_PATH = IMAGES_DIR / im.LOCK_FILENAME
SHA = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture
def specs() -> list[ImageSpec]:
    return image_specs()


@pytest.fixture
def lock() -> DigestsLock:
    return read_lock(LOCK_PATH)


@pytest.fixture
def copy_of_images(tmp_path: Path) -> Path:
    """A scratch copy of `images/` that a test may damage."""
    dest = tmp_path / "images"
    shutil.copytree(IMAGES_DIR, dest)
    return dest


# --- the Dockerfiles ---------------------------------------------------------------------


def test_the_spec_lists_every_image(specs: list[ImageSpec]) -> None:
    assert [s.key for s in specs] == [
        "node:18",
        "node:20",
        "node:22",
        "python:3.9",
        "python:3.10",
        "python:3.11",
        "python:3.12",
        "python:3.13",
        "proxy:1",
    ]
    assert [s.tag for s in specs][:2] == ["mpilot/node:18", "mpilot/node:20"]


def test_every_base_is_pinned_by_digest_to_the_right_tag(specs: list[ImageSpec]) -> None:
    for spec in specs:
        name, _, digest = spec.base.partition("@sha256:")
        assert SHA.match(digest), spec.base
        expected = {
            "node": f"node:{spec.version}-bookworm-slim",
            "python": f"python:{spec.version}-slim-bookworm",
            "proxy": "python:3.12-slim-bookworm",
        }[spec.ecosystem.name]
        assert name == expected


def test_no_two_sandbox_images_share_a_base_digest(specs: list[ImageSpec]) -> None:
    digests = [s.base.partition("@")[2] for s in specs if s.ecosystem.sandbox]
    assert len(set(digests)) == len(digests)


def test_the_proxy_uses_the_python_3_12_pin(specs: list[ImageSpec]) -> None:
    by_key = {s.key: s for s in specs}
    assert by_key["proxy:1"].base == by_key["python:3.12"].base
    assert not by_key["proxy:1"].ecosystem.sandbox


@pytest.mark.parametrize("eco", ["node", "python"])
def test_dockerfile_hygiene(eco: str) -> None:
    text = (IMAGES_DIR / eco / "Dockerfile").read_text()
    assert "latest" not in text.lower().replace("releases/latest", "")
    assert text.rstrip().splitlines()[-1] == 'CMD ["sleep", "infinity"]'
    users = re.findall(r"^USER\s+(\S+)", text, re.MULTILINE)
    assert users == ["sandbox"], "exactly one USER line, the non-root sandbox user"
    assert "apt-get purge -y --auto-remove curl" in text, "curl must not stay in the image"
    assert re.search(r"sha256sum -c", text), "downloads are checked against a SHA-256"
    assert "--no-install-recommends" in text
    assert "rm -rf /var/lib/apt/lists/*" in text


def test_tree_sitter_is_the_same_release_in_both_ecosystems() -> None:
    """A release built against glibc 2.39 prints a warning at every start on bookworm (2.36)."""
    found = {
        eco: dict(
            re.findall(
                r"^ARG (TREE_SITTER_\w+)=(\S+)", (IMAGES_DIR / eco / "Dockerfile").read_text(), re.M
            )
        )
        for eco in ("node", "python")
    }
    assert found["node"] == found["python"]
    assert found["node"]["TREE_SITTER_VERSION"] == "v0.25.10"
    assert SHA.match(found["node"]["TREE_SITTER_SHA256_AMD64"])
    assert SHA.match(found["node"]["TREE_SITTER_SHA256_ARM64"])


def test_package_managers_are_pinned_not_floating() -> None:
    node = (IMAGES_DIR / "node" / "Dockerfile").read_text()
    assert re.search(r"^ARG PNPM_VERSION=\d+\.\d+\.\d+$", node, re.M)
    assert '"pnpm@${PNPM_VERSION}"' in node
    py = (IMAGES_DIR / "python" / "Dockerfile").read_text()
    for name in ("UV_VERSION", "POETRY_VERSION", "PDM_VERSION"):
        assert re.search(rf"^ARG {name}=\d+\.\d+\.\d+$", py, re.M), name
    assert SHA.match(re.search(r"^ARG UV_SHA256_AMD64=(\S+)", py, re.M).group(1))  # type: ignore[union-attr]


def test_an_unpinned_from_line_is_refused(copy_of_images: Path) -> None:
    path = copy_of_images / "node" / "Dockerfile"
    path.write_text(
        re.sub(r"(FROM node:20-bookworm-slim)@sha256:[0-9a-f]{64}", r"\1", path.read_text())
    )
    with pytest.raises(ImageError, match="not pinned by digest"):
        image_specs(copy_of_images)


def test_a_missing_stage_is_refused(copy_of_images: Path) -> None:
    path = copy_of_images / "python" / "Dockerfile"
    path.write_text(re.sub(r"^FROM python:3\.11.*\n", "", path.read_text(), flags=re.M))
    with pytest.raises(ImageError, match=r"base-3\.11"):
        image_specs(copy_of_images)


def test_a_stage_the_module_does_not_know_is_refused(copy_of_images: Path) -> None:
    path = copy_of_images / "node" / "Dockerfile"
    line = next(x for x in path.read_text().splitlines() if x.endswith("AS base-22"))
    path.write_text(
        path.read_text().replace(line, line + "\n" + line.replace("base-22", "base-24"))
    )
    with pytest.raises(ImageError, match="base-24"):
        image_specs(copy_of_images)


def test_a_missing_dockerfile_is_refused(copy_of_images: Path) -> None:
    (copy_of_images / "node" / "Dockerfile").unlink()
    with pytest.raises(ImageError, match="cannot read"):
        image_specs(copy_of_images)


# --- the lock ----------------------------------------------------------------------------


def test_the_committed_lock_matches_the_dockerfiles(
    specs: list[ImageSpec], lock: DigestsLock
) -> None:
    assert lock_problems(specs, lock) == []


def test_one_lock_entry_per_image(specs: list[ImageSpec], lock: DigestsLock) -> None:
    assert sorted(lock.images) == sorted(s.key for s in specs)
    assert len({e.tag for e in lock.images.values()}) == len(specs)


def test_every_entry_records_the_tools_of_its_ecosystem(
    specs: list[ImageSpec], lock: DigestsLock
) -> None:
    for spec in specs:
        wanted = {label for label, _ in spec.ecosystem.tools}
        assert set(lock.images[spec.key].tools) == wanted
        assert all(v for v in lock.images[spec.key].tools.values()), "no empty version"


def test_the_lock_records_what_the_images_were_built_from(lock: DigestsLock) -> None:
    for key, entry in lock.images.items():
        assert SHA.match(entry.dockerfile_sha256)
        assert "@sha256:" in entry.base
        if key == "proxy:1":
            server = Path(im.__file__).parent / "proxy" / "server.py"
            assert entry.sources_sha256 == {
                "server.py": hashlib.sha256(server.read_bytes()).hexdigest()
            }
            assert entry.build_args == {}
            continue
        assert entry.sources_sha256 == {}
        assert entry.tools["tree-sitter"].startswith("tree-sitter 0.25.10")
        if key.startswith("node:"):
            assert entry.tools["pnpm"] == "10.34.9"
        else:
            assert "2.2.1" in entry.tools["poetry"]
            assert "2.26.9" in entry.tools["pdm"]
            assert "0.13.9" in entry.tools["uv"]


def test_the_lock_file_is_in_the_canonical_form(lock: DigestsLock) -> None:
    assert LOCK_PATH.read_text() == render_lock(lock)


def test_render_and_write_round_trip(tmp_path: Path, lock: DigestsLock) -> None:
    path = tmp_path / "digests.lock"
    im.write_lock(path, lock)
    assert read_lock(path) == lock
    assert path.read_text().endswith("}\n")


def _tweak(lock: DigestsLock, key: str, **changes: Any) -> DigestsLock:
    entry = lock.images[key].model_copy(update=changes)
    return lock.model_copy(update={"images": {**lock.images, key: entry}})


def test_a_changed_base_digest_is_reported(specs: list[ImageSpec], lock: DigestsLock) -> None:
    other = "node:20-bookworm-slim@sha256:" + "0" * 64
    problems = lock_problems(specs, _tweak(lock, "node:20", base=other))
    assert any("node:20" in p and "base" in p for p in problems)


def test_an_edited_dockerfile_is_reported(specs: list[ImageSpec], lock: DigestsLock) -> None:
    problems = lock_problems(specs, _tweak(lock, "python:3.12", dockerfile_sha256="0" * 64))
    assert any("python:3.12" in p and "Dockerfile changed" in p for p in problems)


def test_a_changed_proxy_source_is_reported(specs: list[ImageSpec], lock: DigestsLock) -> None:
    stale = _tweak(lock, "proxy:1", sources_sha256={"server.py": "0" * 64})
    assert any("proxy:1" in p and "server.py" in p for p in lock_problems(specs, stale))
    dropped = _tweak(lock, "proxy:1", sources_sha256={})
    assert any("proxy:1" in p and "server.py" in p for p in lock_problems(specs, dropped))


def test_the_proxy_dockerfile_adds_only_the_server() -> None:
    text = (IMAGES_DIR / "proxy" / "Dockerfile").read_text()
    instructions = [
        line.split()[0] for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]
    assert "RUN" not in instructions, "nothing installed: the server is stdlib only"
    assert re.findall(r"^COPY .*$", text, re.M) == [
        "COPY --chmod=0444 server.py /opt/egress-proxy/server.py"
    ]
    assert re.findall(r"^USER (\S+)", text, re.M) == ["65534:65534"]
    assert 'ENTRYPOINT ["python", "-I", "-S", "/opt/egress-proxy/server.py"]' in text


def test_a_missing_entry_is_reported(specs: list[ImageSpec], lock: DigestsLock) -> None:
    fewer = lock.model_copy(
        update={"images": {k: v for k, v in lock.images.items() if k != "node:22"}}
    )
    assert lock_problems(specs, fewer) == ["node:22: no entry in the lock"]


def test_an_extra_entry_is_reported(specs: list[ImageSpec], lock: DigestsLock) -> None:
    more = lock.model_copy(update={"images": {**lock.images, "node:24": lock.images["node:22"]}})
    assert any("node:24" in p for p in lock_problems(specs, more))


def test_a_tool_missing_from_an_entry_is_reported(
    specs: list[ImageSpec], lock: DigestsLock
) -> None:
    tools = {k: v for k, v in lock.images["node:18"].tools.items() if k != "pnpm"}
    assert any("pnpm" in p for p in lock_problems(specs, _tweak(lock, "node:18", tools=tools)))


def test_a_wrong_tag_or_build_args_are_reported(specs: list[ImageSpec], lock: DigestsLock) -> None:
    assert any(
        "tag" in p for p in lock_problems(specs, _tweak(lock, "node:18", tag="mpilot/node:19"))
    )
    assert any(
        "build args" in p for p in lock_problems(specs, _tweak(lock, "node:18", build_args={}))
    )


def test_a_lock_with_a_malformed_entry_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "digests.lock"
    path.write_text('{"version": 1, "images": {"node:20": {"tag": "x"}}}')
    with pytest.raises(ImageError, match="not a valid lock"):
        read_lock(path)
    with pytest.raises(ImageError, match="cannot read"):
        read_lock(tmp_path / "absent.lock")


def test_a_lock_entry_needs_a_digest_pinned_base() -> None:
    with pytest.raises(ValueError, match="base"):
        LockEntry(tag="t", base="node:20", dockerfile_sha256="0" * 64, build_args={}, tools={})


def test_check_command_passes_here_and_fails_on_a_stale_lock(copy_of_images: Path) -> None:
    assert im.main(["check"]) == 0
    assert im.main(["check", "--images-dir", str(copy_of_images)]) == 0
    path = copy_of_images / "node" / "Dockerfile"
    path.write_text(path.read_text() + "\n# a comment changes the file's hash\n")
    assert im.main(["check", "--images-dir", str(copy_of_images)]) == 1


def test_check_command_fails_without_a_lock(copy_of_images: Path) -> None:
    (copy_of_images / im.LOCK_FILENAME).unlink()
    assert im.main(["check", "--images-dir", str(copy_of_images)]) == 1


def test_lock_command_refuses_named_images_before_building(copy_of_images: Path) -> None:
    assert im.main(["lock", "node:20", "--images-dir", str(copy_of_images)]) == 1


def test_unknown_image_names_are_refused(copy_of_images: Path) -> None:
    assert im.main(["build", "node:99", "--images-dir", str(copy_of_images)]) == 1


# --- probing and refs, with a fake client ------------------------------------------------


def test_parse_probe_keeps_the_first_value_per_label_and_skips_noise() -> None:
    text = "git=git version 2.39.5\nnoise line\ntree-sitter=tree-sitter 0.25.10 (abc)\nempty=\n=x\n"
    assert im.parse_probe(text) == {
        "git": "git version 2.39.5",
        "tree-sitter": "tree-sitter 0.25.10 (abc)",
        "empty": "",
    }


def test_the_probe_script_covers_every_tool_of_the_ecosystem() -> None:
    for eco in im.ECOSYSTEMS:
        script = im._probe_script(eco)
        for label, command in eco.tools:
            assert f"'{label}'" in script
            assert command in script
        # a missing tool must not stop the rest of the probe
        assert script.count("|| true") == len(eco.tools)


class _Image:
    def __init__(self, attrs: dict[str, Any]) -> None:
        self.attrs = attrs


class _Images:
    def __init__(self, attrs: dict[str, Any] | None) -> None:
        self._attrs = attrs

    def get(self, tag: str) -> _Image:
        from docker.errors import ImageNotFound

        if self._attrs is None:
            raise ImageNotFound(tag)
        return _Image(self._attrs)


class _Client:
    def __init__(self, attrs: dict[str, Any] | None) -> None:
        self.images = _Images(attrs)


def _ref(client: _Client, tag: str = "mpilot/node:20") -> str:
    return im.resolve_image_ref(client, tag)  # type: ignore[arg-type]


def test_resolve_image_ref_returns_the_digest_form() -> None:
    digest = "mpilot/node@sha256:" + "a" * 64
    assert _ref(_Client({"RepoDigests": [digest]})) == digest


def test_resolve_image_ref_ignores_digests_of_other_repositories() -> None:
    other = "someone/else@sha256:" + "b" * 64
    mine = "mpilot/node@sha256:" + "c" * 64
    assert _ref(_Client({"RepoDigests": [other, mine]})) == mine
    with pytest.raises(ImageError, match="no digest"):
        _ref(_Client({"RepoDigests": [other]}))


def test_resolve_image_ref_refuses_an_image_without_a_digest_rather_than_guessing() -> None:
    with pytest.raises(ImageError, match="no digest"):
        _ref(_Client({"RepoDigests": [], "Id": "sha256:" + "d" * 64}))
    with pytest.raises(ImageError, match="no digest"):
        _ref(_Client({"RepoDigests": None}))


def test_resolve_image_ref_names_a_missing_image() -> None:
    with pytest.raises(ImageError, match="not built"):
        _ref(_Client(None))


class _Containers:
    def __init__(self, output: bytes) -> None:
        self.output = output
        self.kwargs: dict[str, Any] = {}

    def run(self, tag: str, argv: list[str], **kwargs: Any) -> bytes:
        self.kwargs = {"tag": tag, "argv": argv, **kwargs}
        return self.output


class _ProbeClient:
    def __init__(self, output: bytes) -> None:
        self.containers = _Containers(output)


def test_the_probe_runs_the_image_offline_unprivileged_and_read_only(
    specs: list[ImageSpec],
) -> None:
    spec = specs[0]
    labels = [label for label, _ in spec.ecosystem.tools]
    client = _ProbeClient("".join(f"{x}=v1\n" for x in labels).encode())
    assert im.probe_tools(client, spec) == dict.fromkeys(labels, "v1")  # type: ignore[arg-type]
    run = client.containers.kwargs
    assert run["tag"] == spec.tag
    assert run["network_mode"] == "none"
    assert run["cap_drop"] == ["ALL"]
    assert run["read_only"] is True
    assert "no-new-privileges" in run["security_opt"]
    assert run["remove"] is True


def test_a_tool_that_printed_nothing_fails_the_probe(specs: list[ImageSpec]) -> None:
    spec = specs[0]
    labels = [label for label, _ in spec.ecosystem.tools]
    output = "".join(f"{x}={'' if x == 'pnpm' else 'v'}\n" for x in labels).encode()
    with pytest.raises(ImageError, match="pnpm"):
        im.probe_tools(_ProbeClient(output), spec)  # type: ignore[arg-type]
