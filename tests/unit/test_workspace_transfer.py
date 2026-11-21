"""P2-04: workspace export and restore, host side (SPEC §9.5, FR-21; A1).

`HostWorkspace.export_to` copies the whole workspace, `.git` included, to a host directory and
returns a hash manifest; `restore_from` rebuilds a workspace from an export and refuses unless
the result matches the digest the DB checkpoint holds. Both read the sandbox-controlled tree
through the same `openat2` containment as every other host access: links are copied as links,
never followed, and anything a tree cannot represent is refused.
"""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mpilot.core.errors import ExportIntegrityError, MpilotError, PathEscapeError
from mpilot.models.artifacts import ArtifactRules, ExportManifest, FileBlob, TreeEntry
from mpilot.sandbox import workspace as workspace_mod
from mpilot.sandbox.workspace import HostWorkspace
from mpilot.testing.infra_doubles import FakeSandbox

SECRET = b"SECRET-OUTSIDE\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(root: Path, rel: str, data: str | bytes = "x\n", mode: int | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    if mode is not None:
        os.chmod(path, mode)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "outside"
    path.mkdir()
    (path / "secret.txt").write_bytes(SECRET)
    return path


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    return tmp_path / "export"


def _populate(root: Path, outside: Path) -> None:
    """A workspace like a real run's: sources, a `.git`, dependencies, links, empty dirs."""
    _put(root, "src/app.py", "print('hi')\n")
    _put(root, "bin/run.sh", "#!/bin/sh\n", mode=0o755)
    _put(root, "docs/guide.md", "guide\n")
    _put(root, "binary.dat", bytes(range(256)))
    _put(root, "empty.txt", "")
    _put(root, ".git/HEAD", "ref: refs/heads/main\n")
    _put(root, ".git/config", "[core]\n\tbare = false\n")
    _put(root, ".git/objects/ab/cdef", b"\x78\x9c object")
    _put(root, "node_modules/react/index.js", "module.exports = 1\n")
    _put(root, "vendor/lib/.git/HEAD", "ref: nested\n")  # a nested repository
    (root / "build" / "empty-dir").mkdir(parents=True)  # directories with nothing in them
    (root / ".git" / "refs" / "tags").mkdir(parents=True)
    (root / "abs-link").symlink_to(outside / "secret.txt")
    (root / "rel-link").symlink_to("../outside/secret.txt")
    (root / "dir-link").symlink_to(outside)
    (root / "inner-link").symlink_to("src/app.py")
    (root / "dangling").symlink_to("/no/such/target")


def _manifest(ws: HostWorkspace, dest: Path) -> ExportManifest:
    return ws.export_to(dest)


def _tree(directory: Path) -> dict[str, tuple[str, str]]:
    """Every path under `directory` (no following) -> (kind, content or target)."""
    out: dict[str, tuple[str, str]] = {}
    for dirpath, dirnames, filenames in os.walk(directory):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = str(path.relative_to(directory))
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("dir", "")
            else:
                out[rel] = (
                    "file",
                    f"{stat.S_IMODE(path.stat().st_mode) & 0o111 != 0}:{_sha(path.read_bytes())}",
                )
    return out


# ---------------------------------------------------------------- export


def test_export_copies_everything_including_git_and_returns_the_manifest(
    root: Path, outside: Path, dest: Path
) -> None:
    _populate(root, outside)
    manifest = HostWorkspace(root).export_to(dest)
    assert manifest.export_dir == str(dest)
    expected = {
        "src/app.py": TreeEntry(mode="100644", sha256=_sha(b"print('hi')\n")),
        "bin/run.sh": TreeEntry(mode="100755", sha256=_sha(b"#!/bin/sh\n")),
        "binary.dat": TreeEntry(mode="100644", sha256=_sha(bytes(range(256)))),
        "empty.txt": TreeEntry(mode="100644", sha256=_sha(b"")),
        ".git/HEAD": TreeEntry(mode="100644", sha256=_sha(b"ref: refs/heads/main\n")),
        ".git/objects/ab/cdef": TreeEntry(mode="100644", sha256=_sha(b"\x78\x9c object")),
        "vendor/lib/.git/HEAD": TreeEntry(mode="100644", sha256=_sha(b"ref: nested\n")),
        "abs-link": TreeEntry(mode="120000", sha256=_sha(os.fsencode(outside / "secret.txt"))),
        "rel-link": TreeEntry(mode="120000", sha256=_sha(b"../outside/secret.txt")),
        "dir-link": TreeEntry(mode="120000", sha256=_sha(os.fsencode(outside))),
        "inner-link": TreeEntry(mode="120000", sha256=_sha(b"src/app.py")),
        "dangling": TreeEntry(mode="120000", sha256=_sha(b"/no/such/target")),
    }
    for key in expected:
        assert manifest.entries[key] == expected[key], key
    assert (dest / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert stat.S_IMODE((dest / "bin" / "run.sh").stat().st_mode) == 0o755
    assert (dest / "build" / "empty-dir").is_dir()  # empty directories survive
    assert (dest / ".git" / "refs" / "tags").is_dir()


def test_the_manifest_outside_git_equals_the_walked_tree(
    root: Path, outside: Path, dest: Path
) -> None:
    """The export and `walk_tree` must agree on modes and hashes, or gate check (i) and the
    checkpoint digest would describe different things."""
    _populate(root, outside)
    ws = HostWorkspace(root)
    manifest = ws.export_to(dest)
    walked = ws.walk_tree(ArtifactRules()).entries
    outside_git = {
        p: e
        for p, e in manifest.entries.items()
        if p != ".git" and not p.startswith(".git/") and "/.git/" not in p
    }
    assert outside_git == walked
    assert any(p.startswith(".git/") for p in manifest.entries)


def test_links_are_copied_as_links_and_their_targets_never_read(
    root: Path, outside: Path, dest: Path
) -> None:
    _populate(root, outside)
    HostWorkspace(root).export_to(dest)
    assert os.readlink(dest / "abs-link") == str(outside / "secret.txt")
    assert os.readlink(dest / "rel-link") == "../outside/secret.txt"
    assert os.readlink(dest / "dir-link") == str(outside)
    assert (dest / "dir-link").is_symlink()
    # Nothing under the export holds the secret's content.
    for path in dest.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert SECRET not in path.read_bytes(), path


def test_export_does_not_follow_a_link_to_a_directory_into_it(
    root: Path, outside: Path, dest: Path
) -> None:
    (outside / "inner").mkdir()
    (outside / "inner" / "deep.txt").write_bytes(SECRET)
    (root / "d").symlink_to(outside)
    manifest = HostWorkspace(root).export_to(dest)
    assert sorted(manifest.entries) == ["d"]
    assert not (dest / "d").is_dir() or (dest / "d").is_symlink()


def test_export_preserves_file_modification_times(root: Path, dest: Path) -> None:
    path = _put(root, "a.txt")
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_650_000_000_123_456_789))
    HostWorkspace(root).export_to(dest)
    assert (dest / "a.txt").stat().st_mtime_ns == 1_650_000_000_123_456_789


def test_an_empty_workspace_exports_an_empty_manifest(root: Path, dest: Path) -> None:
    assert HostWorkspace(root).export_to(dest).entries == {}


def test_export_digest_is_stable_and_changes_with_any_byte(root: Path, tmp_path: Path) -> None:
    _put(root, "a.txt", "one\n")
    ws = HostWorkspace(root)
    first = ws.export_to(tmp_path / "e1")
    again = ws.export_to(tmp_path / "e2")
    assert first.digest() == again.digest()
    _put(root, "a.txt", "two\n")
    assert ws.export_to(tmp_path / "e3").digest() != first.digest()
    _put(root, "a.txt", "one\n")
    assert ws.export_to(tmp_path / "e4").digest() == first.digest()
    os.chmod(root / "a.txt", 0o755)  # only the exec bit is part of the mode
    assert ws.export_to(tmp_path / "e5").digest() != first.digest()


def test_export_requires_a_frozen_sandbox(root: Path, dest: Path) -> None:
    _put(root, "a.txt")
    box = FakeSandbox()
    ws = HostWorkspace(root, sandbox=box)
    with pytest.raises(RuntimeError, match="frozen"):
        ws.export_to(dest)
    assert not dest.exists()
    box.is_frozen = True
    assert ws.export_to(dest).entries["a.txt"].sha256 == _sha(b"x\n")


# ---------------------------------------------------------------- export refuses


def _fifo(root: Path) -> None:
    os.mkfifo(root / "sub" / "pipe")


def _setuid(root: Path) -> None:
    _put(root, "sub/tool", "#!/bin/sh\n", mode=0o4755)


def _setgid(root: Path) -> None:
    _put(root, "sub/tool", "#!/bin/sh\n", mode=0o2755)


def _non_utf8(root: Path) -> None:
    (root / "sub" / os.fsdecode(b"caf\xe9.txt")).write_text("x")


def _backslash(root: Path) -> None:
    (root / "sub" / "back\\slash.txt").write_text("x")


def _unreadable(root: Path) -> None:
    (root / "sub" / "locked").mkdir()
    os.chmod(root / "sub" / "locked", 0o000)


@pytest.fixture
def unix_socket(root: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], None]:
    held: list[socket.socket] = []

    def make(workspace_root: Path) -> None:
        monkeypatch.chdir(workspace_root / "sub")  # AF_UNIX paths are capped at 108 bytes
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind("sock")
        held.append(sock)

    yield make
    for sock in held:
        sock.close()


@pytest.mark.parametrize(
    ("make", "error", "match"),
    [
        (_fifo, PathEscapeError, "FIFO"),
        (_setuid, PathEscapeError, "setuid"),
        (_setgid, PathEscapeError, "setuid"),
        (_non_utf8, PathEscapeError, "UTF-8"),
        (_backslash, PathEscapeError, "back"),
        (_unreadable, PermissionError, "locked|Permission"),
    ],
    ids=["fifo", "setuid", "setgid", "non-utf8-name", "backslash-name", "unreadable-dir"],
)
def test_export_refuses_what_a_tree_cannot_hold_and_leaves_no_partial_export(
    root: Path,
    dest: Path,
    make: Callable[[Path], None],
    error: type[Exception],
    match: str,
) -> None:
    _put(root, "sub/ok.txt", "fine\n")
    _put(root, "zzz/after.txt", "after\n")
    make(root)
    try:
        with pytest.raises(error, match=match):
            HostWorkspace(root).export_to(dest)
    finally:
        locked = root / "sub" / "locked"
        if locked.exists():
            os.chmod(locked, 0o755)
    assert list(dest.iterdir()) == []  # whatever was copied is removed again


def test_export_refuses_a_socket(
    root: Path, dest: Path, unix_socket: Callable[[Path], None]
) -> None:
    (root / "sub").mkdir()
    unix_socket(root)
    with pytest.raises(PathEscapeError, match="socket"):
        HostWorkspace(root).export_to(dest)
    assert list(dest.iterdir()) == []


def test_export_refuses_a_tree_deeper_than_the_limit(
    root: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_mod, "MAX_DEPTH", 3)
    _put(root, "a/b/c/d/e.txt")
    with pytest.raises(PathEscapeError, match="deeper"):
        HostWorkspace(root).export_to(dest)
    assert list(dest.iterdir()) == []


def test_a_directory_swapped_for_a_link_mid_export_is_refused(
    root: Path, outside: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(root, "sub/a.txt")
    (outside / "planted.txt").write_bytes(SECRET)
    real = workspace_mod._openat2

    swapped = False

    def swapping(dirfd: int, path: str, flags: int, mode: int = 0) -> int:
        nonlocal swapped
        if path == "sub" and flags & os.O_DIRECTORY and not flags & os.O_PATH and not swapped:
            swapped = True
            os.rename(root / "sub", root / "sub_real")
            os.symlink(outside, root / "sub")
        return real(dirfd, path, flags, mode)

    ws = HostWorkspace(root)
    monkeypatch.setattr(workspace_mod, "_openat2", swapping)
    with pytest.raises(PathEscapeError):
        ws.export_to(dest)
    assert list(dest.iterdir()) == []
    assert SECRET not in b"".join(p.read_bytes() for p in dest.rglob("*") if p.is_file())


def test_a_file_swapped_for_a_link_mid_export_is_never_read_through_it(
    root: Path, outside: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(root, "a.txt", "inside\n")
    real = workspace_mod._openat2
    calls: list[str] = []

    def swapping(dirfd: int, path: str, flags: int, mode: int = 0) -> int:
        calls.append(path)
        if path == "a.txt" and not flags & os.O_PATH:  # the real read open
            os.rename(root / "a.txt", root / "moved.txt")
            os.symlink(outside / "secret.txt", root / "a.txt")
        return real(dirfd, path, flags, mode)

    ws = HostWorkspace(root)
    monkeypatch.setattr(workspace_mod, "_openat2", swapping)
    with pytest.raises(PathEscapeError):
        ws.export_to(dest)
    assert "a.txt" in calls
    assert list(dest.iterdir()) == []


def test_a_failed_write_removes_the_partial_export(
    root: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c", "d"):
        _put(root, f"{name}/f.txt", name)
    real = workspace_mod._write_all
    count = 0

    def flaky(fd: int, data: bytes) -> None:
        nonlocal count
        count += 1
        if count == 3:
            raise OSError(28, "No space left on device")
        real(fd, data)

    monkeypatch.setattr(workspace_mod, "_write_all", flaky)
    with pytest.raises(OSError, match="No space"):
        HostWorkspace(root).export_to(dest)
    assert list(dest.iterdir()) == []


# ---------------------------------------------------------------- export destination


def test_the_destination_is_created_when_its_parent_exists(root: Path, tmp_path: Path) -> None:
    _put(root, "a.txt")
    target = tmp_path / "new-export"
    HostWorkspace(root).export_to(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    (tmp_path / "empty-existing").mkdir()
    HostWorkspace(root).export_to(tmp_path / "empty-existing")


def test_the_destination_is_refused_unless_it_is_a_safe_empty_place(
    root: Path, outside: Path, tmp_path: Path
) -> None:
    _put(root, "a.txt")
    ws = HostWorkspace(root)
    with pytest.raises(ValueError, match="absolute"):
        ws.export_to(Path("relative-export"))
    with pytest.raises(FileNotFoundError):
        ws.export_to(tmp_path / "no-parent" / "export")
    with pytest.raises(ValueError, match="inside the workspace"):
        ws.export_to(root / "export")
    with pytest.raises(ValueError, match="inside the workspace"):
        ws.export_to(tmp_path / "workspace" / ".." / "workspace" / "sub" / "x")
    with pytest.raises(ValueError, match="contains the workspace"):
        ws.export_to(tmp_path)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "old.txt").write_text("old")
    with pytest.raises(ValueError, match="not empty"):
        ws.export_to(tmp_path / "busy")
    (tmp_path / "file").write_text("")
    with pytest.raises(NotADirectoryError):
        ws.export_to(tmp_path / "file")
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        ws.export_to(tmp_path / "link")
    assert [p.name for p in outside.iterdir()] == ["secret.txt"]
    assert (tmp_path / "busy" / "old.txt").read_text() == "old"


# ---------------------------------------------------------------- restore


def test_export_then_restore_gives_an_identical_tree_including_git(
    root: Path, outside: Path, tmp_path: Path
) -> None:
    _populate(root, outside)
    before = _tree(root)
    manifest = HostWorkspace(root).export_to(tmp_path / "export")

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    HostWorkspace(fresh).restore_from(manifest, manifest.digest())
    assert _tree(fresh) == before
    # The same digest from a second export of the restored tree: "identical tree hash".
    again = HostWorkspace(fresh).export_to(tmp_path / "export2")
    assert again.entries == manifest.entries
    assert again.digest() == manifest.digest()
    assert os.readlink(fresh / "abs-link") == str(outside / "secret.txt")  # still a link


def test_restore_keeps_modes_empty_directories_and_times(root: Path, tmp_path: Path) -> None:
    path = _put(root, "bin/run.sh", "#!/bin/sh\n", mode=0o755)
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_650_000_000_123_456_789))
    (root / "keep" / "empty").mkdir(parents=True)
    manifest = HostWorkspace(root).export_to(tmp_path / "export")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    HostWorkspace(fresh).restore_from(manifest, manifest.digest())
    assert stat.S_IMODE((fresh / "bin" / "run.sh").stat().st_mode) == 0o755
    assert (fresh / "keep" / "empty").is_dir()
    assert (fresh / "bin" / "run.sh").stat().st_mtime_ns == 1_650_000_000_123_456_789
    assert (fresh / "bin" / "run.sh").stat().st_uid == os.getuid()


def test_restore_refuses_a_digest_that_is_not_the_checkpoints_and_touches_nothing(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(root, "a.txt")
    manifest = HostWorkspace(root).export_to(tmp_path / "export")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    writes: list[int] = []
    real = workspace_mod._write_all
    monkeypatch.setattr(
        workspace_mod, "_write_all", lambda fd, data: (writes.append(1), real(fd, data))
    )
    with pytest.raises(ExportIntegrityError, match="digest"):
        HostWorkspace(fresh).restore_from(manifest, "0" * 64)
    assert writes == []  # refused before a single byte was copied
    assert list(fresh.iterdir()) == []
    assert isinstance(
        ExportIntegrityError("x"), ValueError
    )  # the contract double raises ValueError


def _exported(root: Path, tmp_path: Path) -> tuple[ExportManifest, Path]:
    _put(root, "a.txt", "alpha\n")
    _put(root, "sub/b.txt", "beta\n")
    manifest = HostWorkspace(root).export_to(tmp_path / "export")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    return manifest, fresh


def _tamper_content(export: Path, outside: Path) -> None:
    (export / "a.txt").write_text("ALPHA\n")


def _tamper_add(export: Path, outside: Path) -> None:
    (export / "extra.txt").write_text("extra\n")


def _tamper_remove(export: Path, outside: Path) -> None:
    os.unlink(export / "sub" / "b.txt")


def _tamper_to_link(export: Path, outside: Path) -> None:
    os.unlink(export / "a.txt")
    os.symlink(outside / "secret.txt", export / "a.txt")


def _tamper_dir_to_link(export: Path, outside: Path) -> None:
    for child in (export / "sub").iterdir():
        child.unlink()
    (export / "sub").rmdir()
    os.symlink(outside, export / "sub")


def _tamper_mode(export: Path, outside: Path) -> None:
    os.chmod(export / "a.txt", 0o755)


@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_content,
        _tamper_add,
        _tamper_remove,
        _tamper_to_link,
        _tamper_dir_to_link,
        _tamper_mode,
    ],
    ids=["content", "extra-file", "missing-file", "file-to-link", "dir-to-link", "mode"],
)
def test_a_tampered_export_is_refused_and_leaves_the_workspace_empty(
    root: Path, outside: Path, tmp_path: Path, tamper: Callable[[Path, Path], None]
) -> None:
    manifest, fresh = _exported(root, tmp_path)
    tamper(tmp_path / "export", outside)
    with pytest.raises(ExportIntegrityError, match="digest"):
        HostWorkspace(fresh).restore_from(manifest, manifest.digest())
    assert list(fresh.iterdir()) == []  # nothing half-restored is left
    assert [p.name for p in outside.iterdir()] == ["secret.txt"]
    assert (outside / "secret.txt").read_bytes() == SECRET


def test_a_link_planted_in_the_export_is_restored_as_a_link_never_read(
    root: Path, outside: Path, tmp_path: Path
) -> None:
    """Even before the digest check rejects it, the content behind the link is not copied."""
    manifest, fresh = _exported(root, tmp_path)
    _tamper_to_link(tmp_path / "export", outside)
    seen: list[str] = []
    real = workspace_mod._write_all

    def spy(fd: int, data: bytes) -> None:
        seen.append(data.decode(errors="replace"))
        real(fd, data)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(workspace_mod, "_write_all", spy)
        with pytest.raises(ExportIntegrityError):
            HostWorkspace(fresh).restore_from(manifest, manifest.digest())
    assert not any("SECRET-OUTSIDE" in chunk for chunk in seen)


def test_restore_refuses_a_workspace_that_already_has_files(root: Path, tmp_path: Path) -> None:
    manifest, fresh = _exported(root, tmp_path)
    (fresh / "leftover.txt").write_text("keep me\n")
    with pytest.raises(ValueError, match="not empty"):
        HostWorkspace(fresh).restore_from(manifest, manifest.digest())
    assert [p.name for p in fresh.iterdir()] == ["leftover.txt"]
    assert (fresh / "leftover.txt").read_text() == "keep me\n"


def test_restore_refuses_an_unsafe_export_directory(
    root: Path, outside: Path, tmp_path: Path
) -> None:
    manifest, fresh = _exported(root, tmp_path)
    digest = manifest.digest()
    link = tmp_path / "export-link"
    link.symlink_to(tmp_path / "export")
    for bad in (
        "relative/export",
        str(tmp_path / "missing-export"),
        str(link),
        str(tmp_path / "export" / "a.txt"),
    ):
        broken = ExportManifest(export_dir=bad, entries=manifest.entries)
        with pytest.raises((ValueError, FileNotFoundError, NotADirectoryError, PathEscapeError)):
            HostWorkspace(fresh).restore_from(broken, digest)
        assert list(fresh.iterdir()) == []


def test_restore_requires_a_frozen_sandbox_if_one_is_bound(root: Path, tmp_path: Path) -> None:
    manifest, fresh = _exported(root, tmp_path)
    box = FakeSandbox()
    with pytest.raises(RuntimeError, match="frozen"):
        HostWorkspace(fresh, sandbox=box).restore_from(manifest, manifest.digest())
    assert list(fresh.iterdir()) == []


def test_restore_failure_part_way_leaves_the_workspace_empty(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, fresh = _exported(root, tmp_path)
    real = workspace_mod._write_all
    count = 0

    def flaky(fd: int, data: bytes) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError(28, "No space left on device")
        real(fd, data)

    monkeypatch.setattr(workspace_mod, "_write_all", flaky)
    with pytest.raises(OSError, match="No space"):
        HostWorkspace(fresh).restore_from(manifest, manifest.digest())
    assert list(fresh.iterdir()) == []


def test_errors_are_mpilot_errors() -> None:
    assert issubclass(ExportIntegrityError, MpilotError)


# ---------------------------------------------------------------- property: any tree round-trips

_NAMES = st.sampled_from(["a", "b", "c.txt", ".git", "d.py", "e"])
_PATHS = st.lists(_NAMES, min_size=1, max_size=3).map("/".join)
_BLOBS = st.one_of(
    st.builds(FileBlob, mode=st.sampled_from(["100644", "100755"]), data=st.binary(max_size=40)),
    st.builds(FileBlob, mode=st.just("120000"), data=st.sampled_from([b"a", b"../x", b"/etc"])),
)


def _consistent(files: dict[str, FileBlob]) -> dict[str, FileBlob]:
    dirs = {"/".join(p.split("/")[:i]) for p in files for i in range(1, p.count("/") + 1)}
    return {p: b for p, b in files.items() if p not in dirs}


def _materialize(base: Path, files: dict[str, FileBlob]) -> None:
    for rel, blob in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if blob.mode == "120000":
            os.symlink(blob.data, target)
        else:
            target.write_bytes(blob.data)
            os.chmod(target, 0o755 if blob.mode == "100755" else 0o644)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(files=st.dictionaries(_PATHS, _BLOBS, max_size=10).map(_consistent))
def test_any_tree_round_trips_through_export_and_restore(files: dict[str, FileBlob]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        src, fresh = base / "src", base / "fresh"
        src.mkdir()
        fresh.mkdir()
        _materialize(src, files)
        manifest = HostWorkspace(src).export_to(base / "export")
        assert set(manifest.entries) == set(files)
        HostWorkspace(fresh).restore_from(manifest, manifest.digest())
        assert _tree(fresh) == _tree(src)
        assert HostWorkspace(fresh).export_to(base / "export2").digest() == manifest.digest()
