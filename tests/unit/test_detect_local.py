"""P2-12b: `LocalTree`, a read-only `TreeReader` over a directory on disk (for `mpilot detect`).

The directory is untrusted repo content, so it is read only through `HostWorkspace` (`openat2`
containment): a symlink is never followed, a file that is not regular is refused, and reads are
size-capped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mpilot.core.errors import InputError
from mpilot.detect import local
from mpilot.detect.local import open_local_tree
from mpilot.policy.gate import TreeReader


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_paths_and_text_of_a_plain_tree(tmp_path: Path) -> None:
    tree = open_local_tree(
        write(tmp_path, {"package.json": "{}", "src/a.js": "x", "a/b/c.txt": "y"})
    )
    assert tree.paths() == {"package.json", "src/a.js", "a/b/c.txt"}
    assert tree.read_text("package.json") == "{}"
    assert tree.read_text("a/b/c.txt") == "y"
    assert tree.read_text("missing") is None
    assert tree.read_text("src") is None  # a directory


@pytest.mark.parametrize(
    "folder",
    [
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    ],
)
def test_vendored_and_cache_folders_are_left_out(tmp_path: Path, folder: str) -> None:
    tree = open_local_tree(write(tmp_path, {"a.txt": "x", f"{folder}/inner/f.txt": "y"}))
    assert tree.paths() == {"a.txt"}
    assert tree.read_text(f"{folder}/inner/f.txt") is None


def test_a_symlink_is_listed_but_never_read_or_followed(tmp_path: Path) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("secret")
    root = tmp_path / "repo"
    write(root, {"real.txt": "ok"})
    (root / "link.txt").symlink_to(secret)
    (root / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    tree = open_local_tree(root)
    assert "link.txt" in tree.paths()
    assert tree.read_text("link.txt") is None
    assert tree.read_text("dirlink/outside.txt") is None
    assert not any(p.startswith("dirlink/") for p in tree.paths())
    assert tree.snapshot().entries["link.txt"].mode == "120000"


def test_a_package_json_that_is_a_symlink_reads_as_none(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text("{}")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package.json").symlink_to(other)
    assert open_local_tree(root).read_text("package.json") is None


@pytest.mark.parametrize("data", [b"\xff\xfe\x00bad", b"a\x00b", b"\x80abc"])
def test_a_binary_or_non_utf8_file_reads_as_none(tmp_path: Path, data: bytes) -> None:
    (tmp_path / "f.bin").write_bytes(data)
    assert open_local_tree(tmp_path).read_text("f.bin") is None


def test_a_file_over_the_read_cap_reads_as_none(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * (local.MAX_READ_BYTES + 1))
    (tmp_path / "ok.txt").write_text("x" * local.MAX_READ_BYTES)
    tree = open_local_tree(tmp_path)
    assert tree.read_text("big.txt") is None
    assert tree.read_text("ok.txt") is not None
    assert "big.txt" in tree.paths()


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "", ".", "a/../b", "a\\b", "a\x00b"])
def test_odd_paths_read_as_none(tmp_path: Path, path: str) -> None:
    write(tmp_path, {"a/b": "x", "x": "y"})
    assert open_local_tree(tmp_path).read_text(path) is None


def test_the_snapshot_hashes_files_and_the_commit_is_a_stable_fingerprint(tmp_path: Path) -> None:
    write(tmp_path, {"a.txt": "one", "b.txt": "two"})
    first = open_local_tree(tmp_path)
    again = open_local_tree(tmp_path)
    assert first.commit == again.commit
    assert len(first.commit) == 40
    assert set(first.commit) <= set("0123456789abcdef")
    assert first.snapshot().entries["a.txt"].sha256 == (
        "7692c3ad3540bb803c020b3aee66cd8887123234ea0c6e7143c0add73ff431ed"
    )
    (tmp_path / "a.txt").write_text("changed")
    assert open_local_tree(tmp_path).commit != first.commit


def test_executable_mode_is_recorded(tmp_path: Path) -> None:
    f = tmp_path / "run.sh"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    assert open_local_tree(tmp_path).snapshot().entries["run.sh"].mode == "100755"


@pytest.mark.parametrize("bad", ["relative/dir", "missing"])
def test_a_missing_directory_is_an_input_error(tmp_path: Path, bad: str) -> None:
    with pytest.raises(InputError, match="not a directory"):
        open_local_tree(tmp_path / bad)


def test_a_file_is_not_a_repository(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(InputError, match="not a directory"):
        open_local_tree(f)


def test_a_relative_path_is_resolved_against_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {"repo/package.json": "{}"})
    monkeypatch.chdir(tmp_path)
    assert open_local_tree(Path("repo")).paths() == {"package.json"}


def test_a_symlinked_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    write(real, {"a": "x"})
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(InputError, match="symlink"):
        open_local_tree(link)


def test_a_fifo_in_the_tree_is_an_input_error_not_a_hang(tmp_path: Path) -> None:
    write(tmp_path, {"a.txt": "x"})
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(InputError, match="FIFO"):
        open_local_tree(tmp_path)


def test_a_tree_with_too_many_files_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local, "MAX_PATHS", 5)
    write(tmp_path, {f"f{i}.txt": "x" for i in range(6)})
    with pytest.raises(InputError, match="more than 5"):
        open_local_tree(tmp_path)
    write(tmp_path, {})  # exactly at the cap is fine
    (tmp_path / "f5.txt").unlink()
    assert len(open_local_tree(tmp_path).paths()) == 5


def test_a_local_tree_is_a_tree_reader(tmp_path: Path) -> None:
    write(tmp_path, {"a": "x"})
    tree: TreeReader = open_local_tree(tmp_path)  # mypy checks the protocol
    assert tree.paths() == {"a"}
