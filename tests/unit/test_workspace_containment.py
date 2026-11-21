"""P2-01: host-side `Workspace` containment (A1, SPEC §9.5, FR-10, NFR-6).

The sandbox controls everything inside the workspace, so every path here is hostile: the
host must never read, write or delete anything outside it, never follow a symlink, never
open a FIFO, device or setuid file, and never read past the cap. Tree-level operations
(`walk_tree`, `iter_paths`, `replace_tree`) are in `test_workspace_tree.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from mpilot.core.errors import PathEscapeError
from mpilot.models.artifacts import ArtifactRules, ChangeKind, EntryType
from mpilot.sandbox import base as sb
from mpilot.sandbox import workspace as workspace_mod
from mpilot.sandbox.workspace import HostWorkspace
from mpilot.testing.infra_doubles import FakeSandbox

SECRET = "SECRET\n"


@dataclass(frozen=True)
class Tree:
    root: Path
    outside: Path

    @property
    def ws(self) -> HostWorkspace:
        return HostWorkspace(self.root)

    def outside_listing(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.outside.iterdir())}


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET)
    return Tree(root, outside)


def _ops(ws: HostWorkspace, rel: str) -> dict[str, Callable[[], object]]:
    """Every per-path operation, called on `rel`."""
    return {
        "read_text": lambda: ws.read_text(rel, max_bytes=1 << 20),
        "read_blob": lambda: ws.read_blob(rel, max_bytes=1 << 20),
        "write_create": lambda: ws.write_text(rel, "x\n", create=True),
        "write_modify": lambda: ws.write_text(rel, "x\n", create=False),
        "delete": lambda: ws.delete(rel),
        "list_dir": lambda: ws.list_dir(rel, 1),
    }


# ---------------------------------------------------------------- construction and protocol


def test_host_workspace_satisfies_the_protocol(tree: Tree) -> None:
    assert isinstance(tree.ws, sb.Workspace)
    assert tree.ws.root == tree.root


def test_root_must_be_an_absolute_real_directory(tree: Tree, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        HostWorkspace(Path("workspace"))
    with pytest.raises(FileNotFoundError):
        HostWorkspace(tmp_path / "missing")
    (tmp_path / "file").write_text("")
    with pytest.raises(NotADirectoryError):
        HostWorkspace(tmp_path / "file")
    (tmp_path / "root-link").symlink_to(tree.root)
    with pytest.raises(PathEscapeError, match="symlink"):
        HostWorkspace(tmp_path / "root-link")


def test_a_root_swapped_for_a_symlink_after_construction_is_refused(tree: Tree) -> None:
    ws = tree.ws
    (tree.root / "a.txt").write_text("a")
    tree.root.rename(tree.root.with_name("moved"))
    tree.root.symlink_to(tree.outside)
    for name, op in _ops(ws, "secret.txt").items():
        with pytest.raises(PathEscapeError):
            op()
        assert tree.outside_listing() == {"secret.txt": SECRET.encode()}, name


def test_every_call_refuses_while_the_sandbox_runs(tree: Tree) -> None:
    """§9.5: the host touches files only while every container mounting them is frozen."""
    sandbox = FakeSandbox()
    (tree.root / "a.txt").write_text("a\n")
    ws = HostWorkspace(tree.root, sandbox=sandbox)
    calls: list[Callable[[], object]] = [
        *_ops(ws, "a.txt").values(),
        lambda: ws.walk_tree(ArtifactRules()),
        lambda: ws.replace_tree({}),
        lambda: ws.iter_paths(),
        lambda: ws.list_dir("", 1),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="frozen"):
            call()
    assert (tree.root / "a.txt").read_text() == "a\n"


def test_calls_work_inside_frozen(tree: Tree) -> None:
    sandbox = FakeSandbox()
    (tree.root / "a.txt").write_text("a\n")
    ws = HostWorkspace(tree.root, sandbox=sandbox)

    async def body() -> str:
        async with sandbox.frozen():
            return ws.read_text("a.txt", max_bytes=10)

    assert asyncio.run(body()) == "a\n"


# ---------------------------------------------------------------- lexical refusals


BAD_PATHS = [
    "",
    "/etc/passwd",
    "../outside/secret.txt",
    "sub/../../outside/secret.txt",
    "sub/..",
    "./a.txt",
    "a//b",
    "a/",
    "a\\b",
    "a\x00b",
    ".git/config",
    "sub/.git/config",
]


@pytest.mark.parametrize("rel", BAD_PATHS)
def test_bad_paths_are_refused_by_every_operation(tree: Tree, rel: str) -> None:
    (tree.root / "sub").mkdir()
    (tree.root / ".git").mkdir()
    (tree.root / ".git" / "config").write_text("[core]\n")
    for name, op in _ops(tree.ws, rel).items():
        if name == "list_dir" and rel == "":
            continue  # "" is the root for list_dir
        with pytest.raises(PathEscapeError):
            op()
    assert tree.outside_listing() == {"secret.txt": SECRET.encode()}
    assert (tree.root / ".git" / "config").read_text() == "[core]\n"


# ---------------------------------------------------------------- symlinks


@pytest.mark.parametrize("absolute", [True, False], ids=["absolute", "relative"])
def test_a_symlinked_file_is_never_followed(tree: Tree, absolute: bool) -> None:
    target = tree.outside / "secret.txt" if absolute else Path("../outside/secret.txt")
    (tree.root / "link.txt").symlink_to(target)
    ws = tree.ws
    for name in ("read_text", "write_modify", "write_create", "delete"):
        with pytest.raises(PathEscapeError):
            _ops(ws, "link.txt")[name]()
    assert (tree.root / "link.txt").is_symlink()
    assert tree.outside_listing() == {"secret.txt": SECRET.encode()}


def test_read_blob_returns_a_link_as_a_link(tree: Tree) -> None:
    (tree.root / "link.txt").symlink_to("../outside/secret.txt")
    blob = tree.ws.read_blob("link.txt", max_bytes=100)
    assert blob.mode == "120000"
    assert blob.data == b"../outside/secret.txt"
    with pytest.raises(PathEscapeError, match="cap"):
        tree.ws.read_blob("link.txt", max_bytes=5)


def test_even_a_symlink_that_stays_inside_is_not_followed(tree: Tree) -> None:
    """RESOLVE_NO_SYMLINKS: no link is resolved, harmless-looking or not."""
    (tree.root / "real").mkdir()
    (tree.root / "real" / "f.txt").write_text("inside\n")
    (tree.root / "alias").symlink_to("real")
    (tree.root / "f-alias.txt").symlink_to("real/f.txt")
    ws = tree.ws
    with pytest.raises(PathEscapeError):
        ws.read_text("alias/f.txt", max_bytes=100)
    with pytest.raises(PathEscapeError):
        ws.read_text("f-alias.txt", max_bytes=100)
    with pytest.raises(PathEscapeError):
        ws.list_dir("alias", 1)


@pytest.mark.parametrize("depth", [1, 2], ids=["dir", "parent"])
def test_a_symlinked_directory_or_parent_is_never_traversed(tree: Tree, depth: int) -> None:
    link_parent = tree.root / "a" if depth == 2 else tree.root
    link_parent.mkdir(exist_ok=True)
    (link_parent / "d").symlink_to(tree.outside)
    prefix = "a/d" if depth == 2 else "d"
    ws = tree.ws
    for name, op in _ops(ws, f"{prefix}/secret.txt").items():
        with pytest.raises(PathEscapeError):
            op()
        assert tree.outside_listing() == {"secret.txt": SECRET.encode()}, name
    for op in _ops(ws, f"{prefix}/new/deeper.txt").values():
        with pytest.raises(PathEscapeError):
            op()
    with pytest.raises(PathEscapeError):
        ws.list_dir(prefix, 1)
    assert tree.outside_listing() == {"secret.txt": SECRET.encode()}


def test_create_never_creates_parents_through_a_symlink(tree: Tree) -> None:
    (tree.root / "d").symlink_to(tree.outside)
    with pytest.raises(PathEscapeError):
        tree.ws.write_text("d/x/y.txt", "y\n", create=True)
    assert not (tree.outside / "x").exists()


def test_create_refuses_a_dangling_symlink_at_the_target(tree: Tree) -> None:
    (tree.root / "new.txt").symlink_to(tree.outside / "planted.txt")
    with pytest.raises(PathEscapeError, match="symlink"):
        tree.ws.write_text("new.txt", "x\n", create=True)
    assert not (tree.outside / "planted.txt").exists()


# ---------------------------------------------------------------- mid-operation swaps


def _swap_dir_for_link(tree: Tree) -> None:
    os.rename(tree.root / "sub", tree.root / "sub_real")
    os.symlink(tree.outside, tree.root / "sub")


def _swap_file_for_link(tree: Tree) -> None:
    os.rename(tree.root / "sub" / "secret.txt", tree.root / "sub" / "moved.txt")
    os.symlink(tree.outside / "secret.txt", tree.root / "sub" / "secret.txt")


def _swap_file_for_fifo(tree: Tree) -> None:
    os.rename(tree.root / "sub" / "secret.txt", tree.root / "sub" / "moved.txt")
    os.mkfifo(tree.root / "sub" / "secret.txt")


def _swap_file_for_other_file(tree: Tree) -> None:
    os.rename(tree.root / "sub" / "secret.txt", tree.root / "sub" / "moved.txt")
    (tree.root / "sub" / "secret.txt").write_text("OTHER\n")


#: What each `openat2` call of a one-file operation on "sub/secret.txt" is (white box):
#: the parent directory, the O_PATH type check, then the real I/O open.
PARENT, TYPE_CHECK, REAL_OPEN = 0, 1, 2
_CALL_PATHS = {PARENT: "sub", TYPE_CHECK: "secret.txt", REAL_OPEN: "secret.txt"}

Arm = Callable[[int, Callable[[Tree], None]], HostWorkspace]


@pytest.fixture
def swap_at(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> Iterator[Arm]:
    """`arm(n, swap)` returns a workspace whose next operation runs `swap` right before its
    n-th `openat2` call. The kernel resolves each call atomically, so whatever was swapped in
    is what that call sees. Teardown checks the swap fired right before the intended call."""
    (tree.root / "sub").mkdir()
    (tree.root / "sub" / "secret.txt").write_text("INSIDE\n")
    real = workspace_mod._openat2
    fired: list[tuple[int, str, int]] = []

    def arm(n: int, swap: Callable[[Tree], None]) -> HostWorkspace:
        ws = tree.ws  # built before hooking: construction probes openat2 too
        calls = 0

        def hooked(dirfd: int, path: str, flags: int, mode: int = 0) -> int:
            nonlocal calls
            if calls == n:
                swap(tree)
                fired.append((n, path, flags))
            calls += 1
            return real(dirfd, path, flags, mode)

        monkeypatch.setattr(workspace_mod, "_openat2", hooked)
        return ws

    yield arm
    assert len(fired) == 1, "the swap never ran"
    n, path, flags = fired[0]
    assert path == _CALL_PATHS[n]
    assert bool(flags & os.O_PATH) == (n != REAL_OPEN)


@pytest.mark.parametrize(
    ("n", "swap"),
    [
        (PARENT, _swap_dir_for_link),
        (TYPE_CHECK, _swap_file_for_link),
        (REAL_OPEN, _swap_file_for_link),
    ],
    ids=["parent-before-resolve", "file-before-type-check", "file-between-check-and-open"],
)
def test_a_symlink_swapped_in_mid_read_is_refused(
    swap_at: Arm, n: int, swap: Callable[[Tree], None]
) -> None:
    ws = swap_at(n, swap)
    with pytest.raises(PathEscapeError):
        ws.read_text("sub/secret.txt", max_bytes=100)


def test_a_fifo_swapped_in_between_check_and_open_is_refused_without_blocking(
    swap_at: Arm,
) -> None:
    ws = swap_at(REAL_OPEN, _swap_file_for_fifo)
    _finishes_quickly(lambda: ws.read_text("sub/secret.txt", max_bytes=100))


def test_a_file_replaced_between_check_and_open_is_refused(swap_at: Arm) -> None:
    """Even a regular file: what is read must be the inode that was checked."""
    ws = swap_at(REAL_OPEN, _swap_file_for_other_file)
    with pytest.raises(PathEscapeError, match="replaced"):
        ws.read_text("sub/secret.txt", max_bytes=100)


@pytest.mark.parametrize(
    ("n", "swap"),
    [(PARENT, _swap_dir_for_link), (REAL_OPEN, _swap_file_for_link)],
    ids=["parent", "file-between-check-and-open"],
)
def test_a_symlink_swapped_in_mid_write_never_writes_outside(
    tree: Tree, swap_at: Arm, n: int, swap: Callable[[Tree], None]
) -> None:
    ws = swap_at(n, swap)
    with pytest.raises(PathEscapeError):
        ws.write_text("sub/secret.txt", "PWNED\n", create=False)
    assert tree.outside_listing() == {"secret.txt": SECRET.encode()}


def test_a_parent_swapped_in_mid_create_never_creates_outside(tree: Tree, swap_at: Arm) -> None:
    ws = swap_at(PARENT, _swap_dir_for_link)
    with pytest.raises(PathEscapeError):
        ws.write_text("sub/new.txt", "PWNED\n", create=True)
    assert tree.outside_listing() == {"secret.txt": SECRET.encode()}


def test_the_kernel_refuses_escapes_without_the_lexical_check(tree: Tree) -> None:
    """The `openat2` choke point on its own, with no lexical check in front of it: escapes
    are EXDEV (RESOLVE_BENEATH), any symlink component is ELOOP (RESOLVE_NO_SYMLINKS)."""
    import errno

    (tree.root / "out-link").symlink_to(tree.outside)
    (tree.root / "real").mkdir()
    (tree.root / "in-link").symlink_to("real")
    assert tree.ws.root == tree.root  # constructing one loads openat2
    root = os.open(tree.root, os.O_PATH | os.O_DIRECTORY)
    try:
        for path, code in [
            ("../outside/secret.txt", errno.EXDEV),
            (str(tree.outside / "secret.txt"), errno.EXDEV),
            ("real/../../outside/secret.txt", errno.EXDEV),
            ("out-link/secret.txt", errno.ELOOP),
            ("in-link", errno.ELOOP),
        ]:
            with pytest.raises(OSError, match=os.strerror(code)) as info:
                workspace_mod._openat2(root, path, os.O_RDONLY)
            assert info.value.errno == code, path
    finally:
        os.close(root)


def test_a_racing_symlink_swap_never_reaches_outside(tree: Tree) -> None:
    """A thread keeps swapping `sub` between the real directory and a link to `outside`
    while the host reads and rewrites a file in it. No read may return outside content and
    no write may land outside, whatever the interleaving."""
    sub = tree.root / "sub"
    sub.mkdir()
    (sub / "secret.txt").write_text("INSIDE\n")
    real = tree.root / "sub_real"
    stop = threading.Event()
    errors: list[BaseException] = []

    def swapper() -> None:
        try:
            while not stop.is_set():
                os.rename(sub, real)
                os.symlink(tree.outside, sub)
                os.unlink(sub)
                os.rename(real, sub)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    thread = threading.Thread(target=swapper)
    thread.start()
    seen: set[str] = set()
    ws = tree.ws
    try:
        for _ in range(1500):
            try:
                seen.add(ws.read_text("sub/secret.txt", max_bytes=100))
            except (PathEscapeError, FileNotFoundError):
                seen.add("refused")
            with contextlib.suppress(PathEscapeError, FileNotFoundError):
                ws.write_text("sub/secret.txt", "INSIDE\n", create=False)
    finally:
        stop.set()
        thread.join()
    assert not errors
    assert seen <= {"INSIDE\n", "refused"}
    assert tree.outside_listing() == {"secret.txt": SECRET.encode()}


# ---------------------------------------------------------------- non-regular files


def _finishes_quickly(call: Callable[[], object]) -> None:
    """`call` must raise PathEscapeError and must not block (a FIFO open would)."""
    outcome: list[BaseException | None] = []

    def target() -> None:
        try:
            call()
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert outcome, "the call blocked (it opened a FIFO?)"
    assert isinstance(outcome[0], PathEscapeError), outcome[0]


def test_a_fifo_is_refused_without_blocking(tree: Tree) -> None:
    os.mkfifo(tree.root / "pipe")
    for name, op in _ops(tree.ws, "pipe").items():
        if name in ("write_create", "list_dir"):
            continue
        _finishes_quickly(op)
    assert stat.S_ISFIFO(os.lstat(tree.root / "pipe").st_mode)


@pytest.fixture
def unix_socket(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tree.root)  # relative bind: AF_UNIX paths are capped at 108 bytes
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind("sock")
        yield


@pytest.mark.usefixtures("unix_socket")
def test_a_socket_is_refused(tree: Tree) -> None:
    for name, op in _ops(tree.ws, "sock").items():
        if name in ("write_create", "list_dir"):
            continue
        with pytest.raises(PathEscapeError, match="socket"):
            op()
    assert stat.S_ISSOCK(os.lstat(tree.root / "sock").st_mode)


@pytest.mark.parametrize("bits", [stat.S_ISUID, stat.S_ISGID], ids=["setuid", "setgid"])
def test_setuid_and_setgid_files_are_refused(tree: Tree, bits: int) -> None:
    path = tree.root / "tool"
    path.write_text("#!/bin/sh\n")
    os.chmod(path, 0o755 | bits)
    assert os.stat(path).st_mode & bits, "this filesystem dropped the bit"
    for name, op in _ops(tree.ws, "tool").items():
        if name in ("write_create", "list_dir"):
            continue
        with pytest.raises(PathEscapeError, match="setuid"):
            op()
    assert path.read_text() == "#!/bin/sh\n"


@pytest.mark.parametrize(
    ("kind", "word"),
    [
        (stat.S_IFCHR, "device"),
        (stat.S_IFBLK, "device"),
        (stat.S_IFIFO, "FIFO"),
        (stat.S_IFSOCK, "socket"),
        (stat.S_IFLNK, "symlink"),
    ],
)
def test_the_type_check_names_every_refused_kind(kind: int, word: str) -> None:
    reason = workspace_mod.refusal_reason(kind | 0o644)
    assert reason is not None
    assert word in reason
    assert workspace_mod.refusal_reason(stat.S_IFREG | 0o755) is None
    assert "setuid" in (workspace_mod.refusal_reason(stat.S_IFREG | 0o4755) or "")


_NS_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from mpilot.core.errors import PathEscapeError
    from mpilot.models.artifacts import ArtifactRules
    from mpilot.sandbox.workspace import HostWorkspace

    ws = HostWorkspace(Path(sys.argv[1]))
    out = {}
    for label, method, args, kwargs in json.loads(sys.argv[2]):
        if method == "walk_tree":
            args = [ArtifactRules()]
        try:
            result = getattr(ws, method)(*args, **kwargs)
            if method == "list_dir":
                out[label] = {e.path: e.type.value for e in result}
            else:
                out[label] = "allowed"
        except PathEscapeError:
            out[label] = "refused"
    print(json.dumps(out))
    """
)


def _with_bind_mounts(
    root: Path,
    binds: dict[str, Path],
    calls: list[tuple[str, str, list[object], dict[str, object]]],
) -> dict[str, object]:
    """Run workspace `calls` in a child inside an unprivileged user + mount namespace where
    each `binds` source is bind-mounted over `root/<rel>`. This is how the tests get a real
    device node and a real mount under the workspace without root: an unprivileged user can
    neither `mknod` nor `mount` in the host namespace."""
    unshare = shutil.which("unshare")
    assert unshare is not None, "util-linux `unshare` is needed for this test"
    mounts = " && ".join(
        f"mount --bind {shlex.quote(str(src))} {shlex.quote(str(root / rel))}"
        for rel, src in binds.items()
    )
    shell = f'{mounts} && exec "$1" -c "$2" "$3" "$4"'
    argv = [unshare, "-rm", "sh", "-c", shell, "sh", sys.executable, _NS_SCRIPT]
    proc = subprocess.run(  # noqa: S603  (fixed argv, test only)
        [*argv, str(root), json.dumps(calls)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result: dict[str, object] = json.loads(proc.stdout)
    return result


def test_a_real_device_node_is_refused(tree: Tree) -> None:
    """/dev/null bind-mounted over a workspace file is a real character device."""
    (tree.root / "dev").write_text("")
    (tree.root / "ok.txt").write_text("ok\n")
    out = _with_bind_mounts(
        tree.root,
        {"dev": Path("/dev/null")},
        [
            ("read_text", "read_text", ["dev"], {"max_bytes": 10}),
            ("read_blob", "read_blob", ["dev"], {"max_bytes": 10}),
            ("write_modify", "write_text", ["dev", "x"], {"create": False}),
            ("delete", "delete", ["dev"], {}),
            ("walk_tree", "walk_tree", [], {}),
            ("list_dir", "list_dir", ["", 1], {}),
        ],
    )
    assert out == {
        "read_text": "refused",
        "read_blob": "refused",
        "write_modify": "refused",
        "delete": "refused",
        "walk_tree": "refused",
        "list_dir": {"dev": "other", "ok.txt": "file"},
    }
    assert (tree.root / "dev").read_text() == ""  # the mount namespace is gone


def test_a_mount_under_the_workspace_is_never_crossed(tree: Tree) -> None:
    """A directory from outside bind-mounted under the workspace: no symlink and no `..`
    anywhere, so only RESOLVE_NO_XDEV stops the host from following it out."""
    (tree.root / "mnt").mkdir()
    (tree.root / "ok.txt").write_text("ok\n")
    out = _with_bind_mounts(
        tree.root,
        {"mnt": tree.outside},
        [
            ("read_text", "read_text", ["mnt/secret.txt"], {"max_bytes": 100}),
            ("write_modify", "write_text", ["mnt/secret.txt", "PWNED"], {"create": False}),
            ("write_create", "write_text", ["mnt/new.txt", "PWNED"], {"create": True}),
            ("delete", "delete", ["mnt/secret.txt"], {}),
            ("list_dir", "list_dir", ["mnt", 1], {}),
            ("walk_tree", "walk_tree", [], {}),
            ("replace_tree", "replace_tree", [{}], {}),
        ],
    )
    refused = (
        "read_text",
        "write_modify",
        "write_create",
        "delete",
        "list_dir",
        "walk_tree",
        "replace_tree",
    )
    assert out == dict.fromkeys(refused, "refused")
    assert tree.outside_listing() == {"secret.txt": SECRET.encode()}


# ---------------------------------------------------------------- read caps


def test_read_text_is_capped(tree: Tree) -> None:
    (tree.root / "f.txt").write_text("12345")
    ws = tree.ws
    assert ws.read_text("f.txt", max_bytes=5) == "12345"
    with pytest.raises(PathEscapeError, match="cap"):
        ws.read_text("f.txt", max_bytes=4)
    with pytest.raises(ValueError, match="max_bytes"):
        ws.read_text("f.txt", max_bytes=-1)


def test_the_cap_holds_while_reading_a_file_that_grows() -> None:
    """The size check before reading is not the only guard: the read loop stops at the cap
    whatever fstat said (shown on a pipe, whose size is always 0)."""
    r, w = os.pipe()
    try:
        os.write(w, b"x" * 100)
        os.close(w)
        with pytest.raises(PathEscapeError, match="cap"):
            workspace_mod._read_capped(r, 99, "grows.txt")
    finally:
        os.close(r)


def test_read_blob_is_capped_and_keeps_the_mode(tree: Tree) -> None:
    path = tree.root / "run.sh"
    path.write_bytes(b"\x00\x01binary")
    os.chmod(path, 0o755)
    ws = tree.ws
    blob = ws.read_blob("run.sh", max_bytes=8)
    assert (blob.mode, blob.data) == ("100755", b"\x00\x01binary")
    with pytest.raises(PathEscapeError, match="cap"):
        ws.read_blob("run.sh", max_bytes=7)


def test_read_text_is_strict_utf8(tree: Tree) -> None:
    (tree.root / "bin").write_bytes(b"\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        tree.ws.read_text("bin", max_bytes=10)


def test_missing_paths_and_directories(tree: Tree) -> None:
    (tree.root / "dir").mkdir()
    ws = tree.ws
    with pytest.raises(FileNotFoundError):
        ws.read_text("nope.txt", max_bytes=10)
    with pytest.raises(FileNotFoundError):
        ws.read_text("nodir/nope.txt", max_bytes=10)
    with pytest.raises(IsADirectoryError):
        ws.read_text("dir", max_bytes=10)
    with pytest.raises(IsADirectoryError):
        ws.delete("dir")
    with pytest.raises(FileNotFoundError):
        ws.delete("nope.txt")
    assert (tree.root / "dir").is_dir()


# ---------------------------------------------------------------- writes and diffs


def test_create_returns_a_unified_diff_and_creates_parents(tree: Tree) -> None:
    diff = tree.ws.write_text("new/deep/f.txt", "a\nb\n", create=True)
    assert diff.kind is ChangeKind.ADDED
    assert diff.path == "new/deep/f.txt"
    assert diff.unified == "--- /dev/null\n+++ b/new/deep/f.txt\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    path = tree.root / "new" / "deep" / "f.txt"
    assert path.read_text() == "a\nb\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_create_of_an_empty_file_still_returns_a_diff_header(tree: Tree) -> None:
    diff = tree.ws.write_text("empty.txt", "", create=True)
    assert diff.unified == "--- /dev/null\n+++ b/empty.txt\n"


def test_create_refuses_an_existing_file(tree: Tree) -> None:
    (tree.root / "f.txt").write_text("old\n")
    with pytest.raises(FileExistsError):
        tree.ws.write_text("f.txt", "new\n", create=True)
    assert (tree.root / "f.txt").read_text() == "old\n"


def test_modify_needs_an_existing_file_and_creates_no_parents(tree: Tree) -> None:
    with pytest.raises(FileNotFoundError):
        tree.ws.write_text("f.txt", "x\n", create=False)
    with pytest.raises(FileNotFoundError):
        tree.ws.write_text("nodir/f.txt", "x\n", create=False)
    assert list(tree.root.iterdir()) == []


def test_modify_returns_a_unified_diff_and_keeps_the_mode(tree: Tree) -> None:
    path = tree.root / "f.sh"
    path.write_text("a\nb\nc\n")
    os.chmod(path, 0o750)
    diff = tree.ws.write_text("f.sh", "a\nB\nc", create=False)
    assert diff.kind is ChangeKind.MODIFIED
    assert diff.unified == (
        "--- a/f.sh\n+++ b/f.sh\n@@ -1,3 +1,3 @@\n"
        " a\n-b\n-c\n+B\n+c\n\\ No newline at end of file\n"
    )
    assert path.read_text() == "a\nB\nc"
    assert stat.S_IMODE(path.stat().st_mode) == 0o750
    assert [p.name for p in tree.root.iterdir()] == ["f.sh"]  # no temp file left


def test_modify_never_writes_through_a_hard_link(tree: Tree) -> None:
    """Writes replace the directory entry (temp file + rename), so another name for the
    same inode keeps its content."""
    (tree.root / "f.txt").write_text("old\n")
    os.link(tree.root / "f.txt", tree.root / "g.txt")
    tree.ws.write_text("f.txt", "new\n", create=False)
    assert (tree.root / "f.txt").read_text() == "new\n"
    assert (tree.root / "g.txt").read_text() == "old\n"


def test_a_failed_write_leaves_the_file_and_no_temp_file(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tree.root / "f.txt").write_text("old\n")

    def boom(fd: int, data: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(workspace_mod, "_write_all", boom)
    with pytest.raises(OSError, match="No space"):
        tree.ws.write_text("f.txt", "new\n", create=False)
    with pytest.raises(OSError, match="No space"):
        tree.ws.write_text("g.txt", "new\n", create=True)
    assert [p.name for p in tree.root.iterdir()] == ["f.txt"]
    assert (tree.root / "f.txt").read_text() == "old\n"


def test_delete_returns_a_unified_diff(tree: Tree) -> None:
    (tree.root / "f.txt").write_text("a\nb\n")
    diff = tree.ws.delete("f.txt")
    assert diff.kind is ChangeKind.DELETED
    assert diff.unified == "--- a/f.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    assert not (tree.root / "f.txt").exists()


def test_binary_content_has_an_empty_diff(tree: Tree) -> None:
    (tree.root / "img.bin").write_bytes(b"\x89PNG\x00\x01")
    (tree.root / "latin1.txt").write_bytes(b"caf\xe9\n")
    ws = tree.ws
    assert ws.write_text("img.bin", "now text\n", create=False).unified == ""
    assert ws.delete("latin1.txt").unified == ""
    assert ws.write_text("nul.txt", "a\x00b", create=True).unified == ""


def test_diff_lines_split_on_newline_only(tree: Tree) -> None:
    """Form feeds, lone CRs and U+2028 are content, not line breaks (str.splitlines would
    split on them)."""
    (tree.root / "f.txt").write_text("a\x0cb\r\nc\u2028d\n", newline="")
    diff = tree.ws.write_text("f.txt", "a\x0cb\r\nX\n", create=False)
    assert diff.unified == ("--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n a\x0cb\r\n-c\u2028d\n+X\n")


def test_an_oversized_old_file_still_writes_with_an_empty_diff(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_mod, "DIFF_MAX_BYTES", 4)
    (tree.root / "big.txt").write_text("0123456789\n")
    diff = tree.ws.write_text("big.txt", "small\n", create=False)
    assert diff.unified == ""
    assert (tree.root / "big.txt").read_text() == "small\n"


# ---------------------------------------------------------------- list_dir


def test_list_dir_lists_sorted_entries_to_the_given_depth(tree: Tree) -> None:
    root = tree.root
    (root / "b").mkdir()
    (root / "b" / "c").mkdir()
    (root / "b" / "c" / "d").mkdir()
    (root / "b" / "c" / "d" / "deep.txt").write_text("deep")
    (root / "b" / "f.txt").write_text("hello")
    (root / "a.txt").write_text("abc")
    (root / "link").symlink_to(tree.outside)
    os.mkfifo(root / "pipe")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: x\n")
    ws = tree.ws

    one = ws.list_dir("", 1)
    assert [(e.path, e.type) for e in one] == [
        ("a.txt", EntryType.FILE),
        ("b", EntryType.DIR),
        ("link", EntryType.SYMLINK),
        ("pipe", EntryType.OTHER),
    ]
    assert [e.size for e in one][:2] == [3, 0]
    assert ws.list_dir(".", 1) == one

    three = [e.path for e in ws.list_dir("", 3)]
    assert three == ["a.txt", "b", "b/c", "b/c/d", "b/f.txt", "link", "pipe"]
    assert [e.path for e in ws.list_dir("b", 2)] == ["b/c", "b/c/d", "b/f.txt"]


def test_list_dir_depth_is_one_to_three(tree: Tree) -> None:
    for depth in (0, 4):
        with pytest.raises(ValueError, match="depth"):
            tree.ws.list_dir("", depth)


def test_list_dir_of_a_file_is_refused(tree: Tree) -> None:
    (tree.root / "f.txt").write_text("")
    with pytest.raises(NotADirectoryError):
        tree.ws.list_dir("f.txt", 1)
