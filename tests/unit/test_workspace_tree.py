"""P2-01: tree-level `Workspace` operations (SPEC §9.5 Diff and Attempts, A1).

`walk_tree` is what the host diff and gate check (i) trust, so it must never follow a link,
never skip a tracked path, and exclude untracked artifacts using host-owned data only.
`replace_tree` restores a host-owned snapshot (revert-to-best, crash recovery) and must
never delete or write through a link.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mpilot.core.errors import PathEscapeError
from mpilot.models.artifacts import ArtifactRules, FileBlob, TreeEntry, TreeSnapshot
from mpilot.sandbox.workspace import HostWorkspace

SECRET = b"SECRET\n"


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
    (path / "inner").mkdir()
    (path / "inner" / "deep.txt").write_bytes(SECRET)
    return path


def _outside_intact(outside: Path) -> bool:
    return (outside / "secret.txt").read_bytes() == SECRET and (
        outside / "inner" / "deep.txt"
    ).read_bytes() == SECRET


def _paths(snapshot: TreeSnapshot) -> list[str]:
    return sorted(snapshot.entries)


# ---------------------------------------------------------------- walk_tree basics


def test_walk_records_modes_and_hashes_and_never_follows_links(root: Path, outside: Path) -> None:
    _put(root, "a.txt", "a\n")
    _put(root, "bin/run.sh", "#!/bin/sh\n", mode=0o755)
    _put(root, "src/pkg/mod.py", "x = 1\n")
    (root / "src" / "empty").mkdir()
    (root / "to-outside-dir").symlink_to(outside)
    (root / "to-outside-file").symlink_to(outside / "secret.txt")
    (root / "src" / "alias").symlink_to("pkg")
    snap = HostWorkspace(root).walk_tree(ArtifactRules())
    assert snap.entries == {
        "a.txt": TreeEntry(mode="100644", sha256=_sha(b"a\n")),
        "bin/run.sh": TreeEntry(mode="100755", sha256=_sha(b"#!/bin/sh\n")),
        "src/pkg/mod.py": TreeEntry(mode="100644", sha256=_sha(b"x = 1\n")),
        "to-outside-dir": TreeEntry(mode="120000", sha256=_sha(os.fsencode(outside))),
        "to-outside-file": TreeEntry(
            mode="120000", sha256=_sha(os.fsencode(outside / "secret.txt"))
        ),
        "src/alias": TreeEntry(mode="120000", sha256=_sha(b"pkg")),
    }


def test_the_exec_bit_is_the_owner_exec_bit(root: Path) -> None:
    """As git: 100755 iff S_IXUSR; group/other bits and read/write bits don't matter."""
    _put(root, "u", mode=0o700)
    _put(root, "g", mode=0o654)
    _put(root, "ro", mode=0o444)
    entries = HostWorkspace(root).walk_tree(ArtifactRules()).entries
    assert {p: e.mode for p, e in entries.items()} == {
        "u": "100755",
        "g": "100644",
        "ro": "100644",
    }


def test_walk_ignores_git_at_any_depth(root: Path) -> None:
    _put(root, ".git/config", "[core]\n")
    _put(root, "vendor/lib/.git/HEAD", "ref\n")
    _put(root, "vendor/lib/x.c", "int x;\n")
    (root / "sub").mkdir()
    (root / "sub" / ".git").symlink_to("/etc")
    assert _paths(HostWorkspace(root).walk_tree(ArtifactRules())) == ["vendor/lib/x.c"]


@pytest.mark.parametrize("make", ["fifo", "setuid", "setgid"])
def test_walk_refuses_special_files_it_would_include(root: Path, make: str) -> None:
    _put(root, "ok.txt")
    if make == "fifo":
        os.mkfifo(root / "special")
    else:
        bit = stat.S_ISUID if make == "setuid" else stat.S_ISGID
        _put(root, "special", mode=0o755 | bit)
    with pytest.raises(PathEscapeError, match="special"):
        HostWorkspace(root).walk_tree(ArtifactRules())


def test_walk_skips_special_files_in_excluded_directories(root: Path) -> None:
    _put(root, "ok.txt")
    (root / "node_modules").mkdir()
    os.mkfifo(root / "node_modules" / "pipe")
    _put(root, "node_modules/x/tool", mode=0o4755)
    (root / "node_modules" / "locked").mkdir(mode=0o000)  # never even listed
    rules = ArtifactRules(fixed_dirs=frozenset({"node_modules"}))
    try:
        assert _paths(HostWorkspace(root).walk_tree(rules)) == ["ok.txt"]
    finally:
        os.chmod(root / "node_modules" / "locked", 0o700)


def test_walk_refuses_names_a_tree_cannot_hold(root: Path) -> None:
    _put(root, "back\\slash.txt")
    with pytest.raises(PathEscapeError, match="back"):
        HostWorkspace(root).walk_tree(ArtifactRules())


def test_walk_refuses_non_utf8_names(root: Path) -> None:
    (root / os.fsdecode(b"caf\xe9.txt")).write_text("x")
    with pytest.raises(PathEscapeError, match="UTF-8"):
        HostWorkspace(root).walk_tree(ArtifactRules())


def test_walk_refuses_a_tree_deeper_than_the_limit(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mpilot.sandbox import workspace as workspace_mod

    monkeypatch.setattr(workspace_mod, "MAX_DEPTH", 3)
    _put(root, "a/b/c.txt")
    assert _paths(HostWorkspace(root).walk_tree(ArtifactRules())) == ["a/b/c.txt"]
    _put(root, "a/b/c/d.txt")
    with pytest.raises(PathEscapeError, match="deeper"):
        HostWorkspace(root).walk_tree(ArtifactRules())


# ---------------------------------------------------------------- exclusions (§9.5 Diff)


def test_fixed_dirs_are_pruned_at_any_depth_but_only_as_directories(root: Path) -> None:
    _put(root, "node_modules/react/index.js")
    _put(root, "packages/app/node_modules/x.js")
    _put(root, ".venv/bin/python")
    _put(root, "docs/node_modules")  # a file of that name is not a dependency dir
    rules = ArtifactRules(fixed_dirs=frozenset({"node_modules", ".venv"}))
    assert _paths(HostWorkspace(root).walk_tree(rules)) == ["docs/node_modules"]


def test_fixed_dirs_must_be_plain_names(root: Path) -> None:
    for bad in ("build/out", "", ".", ".."):
        with pytest.raises(ValueError, match="fixed_dirs"):
            HostWorkspace(root).walk_tree(ArtifactRules(fixed_dirs=frozenset({bad})))


def test_gitignore_patterns_follow_git_semantics(root: Path) -> None:
    for rel in (
        "app.log",
        "keep.log",
        "root.txt",
        "sub/root.txt",
        "build/out.js",
        "src/build/gen.js",
        "lib/build",  # a file: `build/` matches directories only
        "doc/a.md",
        "doc/deep/a.md",
        "sub/local.tmp",
        "sub/other.tmp",
        "other/local.tmp",
        "sub/nested/x.cache",
        "a/x/y/b",
        "src/main.py",
    ):
        _put(root, rel)
    rules = ArtifactRules(
        gitignore=(
            ("", "# comment"),
            ("", ""),
            ("", "*.log"),
            ("", "!keep.log"),
            ("", "/root.txt"),
            ("", "build/"),
            ("", "doc/*.md"),
            ("", "a/**/b"),
            ("sub", "*.tmp"),
            ("sub", "!other.tmp"),
            ("sub", "nested/*.cache"),
            ("", "!"),  # invalid: skipped, as git does
        )
    )
    assert _paths(HostWorkspace(root).walk_tree(rules)) == [
        "doc/deep/a.md",
        "keep.log",
        "lib/build",
        "other/local.tmp",
        "src/main.py",
        "sub/other.tmp",
        "sub/root.txt",
    ]


def test_a_deeper_gitignore_overrides_a_shallower_one(root: Path) -> None:
    _put(root, "a.txt")
    _put(root, "sub/a.txt")
    _put(root, "sub/b.txt")
    rules = ArtifactRules(gitignore=(("", "*.txt"), ("sub", "!a.txt")))
    assert _paths(HostWorkspace(root).walk_tree(rules)) == ["sub/a.txt"]


def test_a_file_in_an_ignored_directory_cannot_be_re_included(root: Path) -> None:
    """git: "It is not possible to re-include a file if a parent directory of that file is
    excluded." Neither by a later pattern nor by a .gitignore inside the directory."""
    _put(root, "out/keep.txt")
    _put(root, "out/sub/keep.txt")
    rules = ArtifactRules(gitignore=(("", "out/"), ("", "!out/keep.txt"), ("out", "!keep.txt")))
    assert _paths(HostWorkspace(root).walk_tree(rules)) == []


def test_tracked_paths_are_never_excluded(root: Path) -> None:
    _put(root, "dist/bundle.js", "tracked\n")
    _put(root, "dist/new.js", "untracked\n")
    _put(root, "node_modules/patched/index.js", "tracked\n")
    _put(root, "node_modules/other/index.js", "untracked\n")
    _put(root, "conf.log", "tracked\n")
    _put(root, "x.log", "untracked\n")
    rules = ArtifactRules(
        gitignore=(("", "dist/"), ("", "*.log")),
        fixed_dirs=frozenset({"node_modules"}),
        tracked=frozenset(
            {"dist/bundle.js", "node_modules/patched/index.js", "conf.log", "gone.txt"}
        ),
    )
    assert _paths(HostWorkspace(root).walk_tree(rules)) == [
        "conf.log",
        "dist/bundle.js",
        "node_modules/patched/index.js",
    ]


def test_gitignore_edits_in_the_workspace_never_decide_exclusions(root: Path) -> None:
    """Only `rules` (host-owned, from base_sha) exclude; the workspace's own .gitignore is
    just another file in the diff."""
    _put(root, ".gitignore", "*.py\nconftest.py\n")
    _put(root, "conftest.py", "import os\n")
    assert _paths(HostWorkspace(root).walk_tree(ArtifactRules())) == [".gitignore", "conftest.py"]


def test_gitignore_bases_must_be_workspace_paths(root: Path) -> None:
    with pytest.raises(ValueError, match="gitignore"):
        HostWorkspace(root).walk_tree(ArtifactRules(gitignore=(("../x", "*"),)))


# ---------------------------------------------------------------- iter_paths


def test_iter_paths_lists_every_non_directory_without_following_links(
    root: Path, outside: Path
) -> None:
    _put(root, "a.txt")
    _put(root, "node_modules/x.js")
    _put(root, ".git/HEAD")
    (root / "link").symlink_to(outside)
    os.mkfifo(root / "pipe")
    (root / "emptydir").mkdir()
    assert list(HostWorkspace(root).iter_paths()) == ["a.txt", "link", "node_modules/x.js", "pipe"]


# ---------------------------------------------------------------- replace_tree


def _blobs(**files: bytes) -> dict[str, FileBlob]:
    return {k.replace("__", "/"): FileBlob(mode="100644", data=v) for k, v in files.items()}


def test_replace_tree_makes_the_workspace_hold_exactly_the_files(root: Path) -> None:
    _put(root, ".git/HEAD", "ref: refs/heads/main\n")
    _put(root, "keep.txt", "same\n")
    _put(root, "change.txt", "old\n")
    _put(root, "extra.txt")
    _put(root, "node_modules/react/index.js")
    _put(root, "src/stale/deep.py")
    files = {
        "keep.txt": FileBlob(mode="100644", data=b"same\n"),
        "change.txt": FileBlob(mode="100644", data=b"new\n"),
        "bin/run.sh": FileBlob(mode="100755", data=b"#!/bin/sh\n"),
        "src/link": FileBlob(mode="120000", data=b"../keep.txt"),
        "src/new/mod.py": FileBlob(mode="100644", data=b"x = 1\n"),
    }
    ino = os.stat(root / "keep.txt").st_ino
    snap = HostWorkspace(root).replace_tree(files)

    assert snap.entries == {
        p: TreeEntry(mode=b.mode, sha256=_sha(b.data)) for p, b in files.items()
    }
    assert (root / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert not (root / "node_modules").exists()
    assert not (root / "src" / "stale").exists()
    assert os.readlink(root / "src" / "link") == "../keep.txt"
    assert stat.S_IMODE(os.stat(root / "bin" / "run.sh").st_mode) == 0o755
    assert os.stat(root / "keep.txt").st_ino == ino  # unchanged files are left alone


def test_replace_tree_removes_links_and_never_follows_them(root: Path, outside: Path) -> None:
    (root / "dir-link").symlink_to(outside)
    (root / "file-link").symlink_to(outside / "secret.txt")
    (root / "lib").symlink_to(outside)  # where a wanted directory goes
    (root / "deps").mkdir()
    (root / "deps" / "inner-link").symlink_to(outside / "inner")
    files = _blobs(lib__a__mod_py=b"x\n")
    HostWorkspace(root).replace_tree(files)
    assert _outside_intact(outside)
    assert sorted(os.listdir(outside)) == ["inner", "secret.txt"]
    assert sorted(os.listdir(root)) == ["lib"]
    assert not (root / "lib").is_symlink()
    assert (root / "lib" / "a" / "mod_py").read_bytes() == b"x\n"


def test_replace_tree_replaces_a_wrong_kind_or_mode(root: Path, outside: Path) -> None:
    _put(root, "was_file/x")  # wanted: was_file is a file, not a directory
    (root / "was_dir").mkdir()  # wanted: a link
    _put(root, "exe", mode=0o644)  # wanted: 100755
    (root / "link").symlink_to("old-target")  # wanted: other target
    _put(root, "fifo-slot")
    os.unlink(root / "fifo-slot")
    os.mkfifo(root / "fifo-slot")  # wanted: regular file
    _put(root, "suid", mode=0o4755)  # wanted: same content, but never keep setuid
    files = {
        "was_file": FileBlob(mode="100644", data=b"now a file\n"),
        "was_dir": FileBlob(mode="120000", data=b"elsewhere"),
        "exe": FileBlob(mode="100755", data=b"x\n"),
        "link": FileBlob(mode="120000", data=b"new-target"),
        "fifo-slot": FileBlob(mode="100644", data=b"regular\n"),
        "suid": FileBlob(mode="100755", data=b"x\n"),
    }
    snap = HostWorkspace(root).replace_tree(files)
    assert snap.entries == {
        p: TreeEntry(mode=b.mode, sha256=_sha(b.data)) for p, b in files.items()
    }
    assert not os.stat(root / "suid").st_mode & stat.S_ISUID
    assert _outside_intact(outside)


def test_replace_tree_removes_unreadable_and_read_only_directories(root: Path) -> None:
    _put(root, "locked/inner/f.txt")
    _put(root, "ro/f.txt")
    _put(root, "kept/old.txt")
    os.chmod(root / "locked" / "inner", 0o000)
    os.chmod(root / "locked", 0o000)
    os.chmod(root / "ro", 0o555)
    os.chmod(root / "kept", 0o500)
    try:
        snap = HostWorkspace(root).replace_tree(_blobs(kept__new_txt=b"n\n"))
    finally:
        for path in (root / "locked", root / "locked" / "inner", root / "ro", root / "kept"):
            if os.path.lexists(path):
                os.chmod(path, 0o755)
    assert _paths(snap) == ["kept/new_txt"]
    assert sorted(os.listdir(root)) == ["kept"]


def test_replace_tree_removes_nested_git_directories(root: Path) -> None:
    _put(root, "vendor/lib/.git/HEAD")
    _put(root, "vendor/lib/x.c", "int x;\n")
    HostWorkspace(root).replace_tree(_blobs(vendor__lib__x_c=b"int x;\n"))
    assert sorted(os.listdir(root / "vendor" / "lib")) == ["x_c"]


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({"../x": FileBlob(mode="100644", data=b"")}, PathEscapeError),
        ({".git/hooks/pre-commit": FileBlob(mode="100755", data=b"")}, PathEscapeError),
        ({"/abs": FileBlob(mode="100644", data=b"")}, PathEscapeError),
        (
            {
                "a": FileBlob(mode="100644", data=b""),
                "a/b": FileBlob(mode="100644", data=b""),
            },
            ValueError,
        ),
        ({"l": FileBlob(mode="120000", data=b"")}, ValueError),
        ({"l": FileBlob(mode="120000", data=b"a\x00b")}, ValueError),
    ],
    ids=["dotdot", "git", "absolute", "file-and-dir", "empty-link", "nul-link"],
)
def test_replace_tree_validates_everything_before_touching_anything(
    root: Path, files: dict[str, FileBlob], error: type[Exception]
) -> None:
    _put(root, "untouched.txt", "u\n")
    with pytest.raises(error):
        HostWorkspace(root).replace_tree(files)
    assert sorted(os.listdir(root)) == ["untouched.txt"]


def test_replace_tree_with_nothing_leaves_only_git(root: Path) -> None:
    _put(root, ".git/HEAD")
    _put(root, "a/b/c.txt")
    assert HostWorkspace(root).replace_tree({}).entries == {}
    assert sorted(os.listdir(root)) == [".git"]


def test_replace_tree_then_walk_round_trips_read_blob(root: Path, outside: Path) -> None:
    _put(root, "a.txt", "a\n")
    _put(root, "bin/x", "x", mode=0o755)
    (root / "l").symlink_to(outside)
    ws = HostWorkspace(root)
    before = ws.walk_tree(ArtifactRules())
    blobs = {p: ws.read_blob(p, max_bytes=1 << 20) for p in before.entries}
    _put(root, "junk/more.txt")
    os.unlink(root / "a.txt")
    after = ws.replace_tree(blobs)
    assert after == before
    assert after.digest() == before.digest()


_NAMES = st.sampled_from(["a", "b", "c.txt", "d.py", "e"])
_PATHS = st.lists(_NAMES, min_size=1, max_size=3).map("/".join)
_BLOBS = st.one_of(
    st.builds(FileBlob, mode=st.sampled_from(["100644", "100755"]), data=st.binary(max_size=40)),
    st.builds(FileBlob, mode=st.just("120000"), data=st.sampled_from([b"a", b"../x", b"/etc"])),
)


def _consistent(files: dict[str, FileBlob]) -> dict[str, FileBlob]:
    """Drop paths that are also another path's directory."""
    dirs = {"/".join(p.split("/")[:i]) for p in files for i in range(1, p.count("/") + 1)}
    return {p: b for p, b in files.items() if p not in dirs}


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    first=st.dictionaries(_PATHS, _BLOBS, max_size=8).map(_consistent),
    second=st.dictionaries(_PATHS, _BLOBS, max_size=8).map(_consistent),
)
def test_replace_tree_reaches_any_tree_from_any_tree(
    first: dict[str, FileBlob], second: dict[str, FileBlob]
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = HostWorkspace(Path(tmp))
        for files in (first, second):
            snap = ws.replace_tree(files)
            expected = {p: TreeEntry(mode=b.mode, sha256=_sha(b.data)) for p, b in files.items()}
            assert snap.entries == expected
            assert ws.walk_tree(ArtifactRules()) == snap
            assert {p: ws.read_blob(p, max_bytes=100) for p in files} == files
