"""Host-side workspace access: the only way the host touches `runs/<id>/workspace` (A1, SPEC
§9.5, FR-10, NFR-6).

The sandbox controls every byte under the workspace, so every name in it is hostile. Rules:

- Every path is resolved by the kernel with `openat2(2)` beneath a root descriptor, with
  RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS, plus RESOLVE_NO_XDEV
  (stricter than §9.5: nothing under the workspace is ever a mount the host should cross).
  Resolution is atomic, so a component swapped for a symlink mid-operation fails the call
  instead of leading outside. Paths are also checked lexically first (no absolute paths,
  `..`, empty parts, `\\`, NUL or `.git` component).
- A file is opened for I/O only after an `O_PATH` descriptor of it shows a plain regular
  file: `O_PATH` never runs a device's open handler and never blocks on a FIFO. The I/O
  open then re-checks the type and the inode, so nothing swapped in between is read.
  Symlinks, FIFOs, sockets, devices and setuid/setgid files are refused (PathEscapeError).
- Writes go to a temp file in the same directory, renamed over the target: an existing inode
  (which could have other names) is never written through.
- Directory walks never follow links and never descend into anything but real directories;
  `.git` is skipped at every depth (git can't hold a `.git` component, so the mirror never
  sees one). Trees deeper than MAX_DEPTH are refused rather than exhausting descriptors.
- If a `Sandbox` is given, every call refuses unless it is frozen (§9.5).

Linux only (NFR-10). The macOS fallback (per-component `openat` + `O_NOFOLLOW`) is not
built; construction refuses on other platforms.
"""

from __future__ import annotations

import ctypes
import difflib
import errno
import hashlib
import os
import platform
import secrets
import stat
import sys
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import Final

import pathspec

from mpilot.core.errors import ExportIntegrityError, PathEscapeError
from mpilot.models.artifacts import (
    ArtifactRules,
    ChangeKind,
    DirEntry,
    EntryType,
    ExportManifest,
    FileBlob,
    FileDiff,
    GitMode,
    TreeEntry,
    TreeSnapshot,
    check_rel_path,
)
from mpilot.sandbox.base import Sandbox

#: Old content above this size is not diffed: the edit still happens, `FileDiff.unified` is "".
DIFF_MAX_BYTES = 8 << 20
_Descend = Callable[[str], bool]

#: Deepest path (in components) any tree operation handles; deeper trees are refused.
MAX_DEPTH = 256
_CHUNK: Final = 1 << 20
_LINK: Final[GitMode] = "120000"

# ---------------------------------------------------------------- openat2(2)

_SYS_OPENAT2: Final = {"x86_64": 437, "aarch64": 437}
_RESOLVE_NO_XDEV: Final = 0x01
_RESOLVE_NO_MAGICLINKS: Final = 0x02
_RESOLVE_NO_SYMLINKS: Final = 0x04
_RESOLVE_BENEATH: Final = 0x08
_RESOLVE: Final = (
    _RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_XDEV
)

_O_PATH_NOFOLLOW: Final = os.O_PATH | os.O_NOFOLLOW
_O_PATH_DIR: Final = os.O_PATH | os.O_DIRECTORY
_O_LIST_DIR: Final = os.O_RDONLY | os.O_DIRECTORY
#: Added to every I/O open: never follow, never block (FIFO), never take a controlling tty.
_O_SAFE_IO: Final = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


#: (libc, syscall number), loaded by the first HostWorkspace.
_openat2_sys: tuple[ctypes.CDLL, int] | None = None


def _load_openat2() -> None:
    global _openat2_sys
    if _openat2_sys is not None:
        return
    machine = platform.machine()
    if sys.platform != "linux" or machine not in _SYS_OPENAT2:
        raise RuntimeError(
            f"the host workspace needs openat2(2) on Linux x86_64/aarch64, not "
            f"{sys.platform}/{machine} (NFR-10: the macOS fallback is not built)"
        )
    _openat2_sys = (ctypes.CDLL(None, use_errno=True), _SYS_OPENAT2[machine])


def _openat2(dirfd: int, path: str, flags: int, mode: int = 0) -> int:
    """`openat2(dirfd, path)` with the containment flags; raises OSError like `os.open`.

    The single choke point for resolving workspace names (tests hook it to swap paths)."""
    if _openat2_sys is None:
        raise RuntimeError("HostWorkspace() loads openat2 first")
    libc, number = _openat2_sys
    how = _OpenHow(flags | os.O_CLOEXEC, mode, _RESOLVE)
    fd = int(
        libc.syscall(
            ctypes.c_long(number),
            ctypes.c_int(dirfd),
            ctypes.c_char_p(os.fsencode(path)),
            ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(how)),
        )
    )
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    return fd


@contextmanager
def _closing(fd: int) -> Iterator[int]:
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _contained(rel: str) -> Iterator[None]:
    """Turn the kernel's containment refusals into PathEscapeError."""
    try:
        yield
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PathEscapeError(
                f"{rel!r}: a path component is a symlink (never followed)"
            ) from None
        if exc.errno == errno.EXDEV:
            raise PathEscapeError(
                f"{rel!r}: resolves outside the workspace or across a mount"
            ) from None
        if exc.errno == errno.EAGAIN:  # openat2 saw a concurrent rename or mount: fail closed
            raise PathEscapeError(f"{rel!r}: the tree changed during resolution") from None
        raise


# ---------------------------------------------------------------- checks


def refusal_reason(mode: int) -> str | None:
    """Why a non-directory with this `st_mode` is never opened, or None for a plain regular
    file (setuid and setgid regular files are refused too, §9.5)."""
    if stat.S_ISLNK(mode):
        return "is a symlink (never followed)"
    if stat.S_ISFIFO(mode):
        return "is a FIFO"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "is a device"
    if stat.S_ISSOCK(mode):
        return "is a socket"
    if not stat.S_ISREG(mode):
        return "is not a regular file"
    if mode & (stat.S_ISUID | stat.S_ISGID):
        return "is setuid or setgid"
    return None


def _require_regular(st: os.stat_result, rel: str) -> None:
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(errno.EISDIR, "is a directory", rel)
    reason = refusal_reason(st.st_mode)
    if reason is not None:
        raise PathEscapeError(f"{rel!r} {reason}")


def _parts(rel: str) -> list[str]:
    """The components of a caller-supplied workspace path, refused unless plainly relative."""
    try:
        check_rel_path(rel)
    except ValueError as exc:
        raise PathEscapeError(str(exc)) from None
    parts = rel.split("/")
    if ".git" in parts:
        raise PathEscapeError(f"{rel!r}: the host never touches a .git directory (§9.5)")
    return parts


def _open_directory(path: Path, what: str) -> int:
    """An O_PATH descriptor of the directory at `path`; a symlink is refused, never followed."""
    fd = os.open(path, _O_PATH_NOFOLLOW | os.O_CLOEXEC)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        os.close(fd)
        if stat.S_ISLNK(st.st_mode):
            raise PathEscapeError(f"{what} {path} is a symlink")
        raise NotADirectoryError(errno.ENOTDIR, f"{what} is not a directory", str(path))
    return fd


def _check_walked(path: str) -> None:
    """A name found on disk must be one a tree (and the mirror) can hold."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise PathEscapeError(f"{path!r}: file name is not UTF-8") from None
    try:
        check_rel_path(path)
    except ValueError as exc:
        raise PathEscapeError(str(exc)) from None


def _git_mode(st: os.stat_result) -> GitMode:
    return "100755" if st.st_mode & stat.S_IXUSR else "100644"


def _same_inode(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


# ---------------------------------------------------------------- byte helpers


def _read_capped(fd: int, max_bytes: int, rel: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(fd, min(_CHUNK, max_bytes + 1 - total)):
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise PathEscapeError(f"{rel!r} is over the read cap of {max_bytes} bytes")
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(fd, _CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _text_lines(data: bytes | None) -> list[str] | None:
    """Lines split on "\\n" only, or None for binary content."""
    if data is None:
        return []
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    pieces = text.split("\n")
    lines = [p + "\n" for p in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def unified_diff(rel: str, old: bytes | None, new: bytes | None) -> str:
    """git-style unified diff of one file (`None` = absent: created or deleted); "" for
    binary content. A file with no hunks (an empty file created) still gets its header."""
    a, b = _text_lines(old), _text_lines(new)
    if a is None or b is None:
        return ""
    src = "/dev/null" if old is None else f"a/{rel}"
    dst = "/dev/null" if new is None else f"b/{rel}"
    out: list[str] = []
    for line in difflib.unified_diff(a, b, src, dst):
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    if not out and (old is None or new is None):
        out = [f"--- {src}\n", f"+++ {dst}\n"]
    return "".join(out)


# ---------------------------------------------------------------- exclusions (§9.5 Diff)


def _compile(lines: Sequence[str]) -> pathspec.GitIgnoreSpec:
    """One .gitignore file's patterns. Lines git can't parse are skipped, as git skips them
    (skipping can only leave more in the diff, never less)."""
    valid: list[str] = []
    for line in lines:
        try:
            pathspec.GitIgnoreSpec.from_lines([line])
        except ValueError:
            continue
        valid.append(line)
    return pathspec.GitIgnoreSpec.from_lines(valid)


class _Exclusions:
    """Decides which untracked paths a walk leaves out, from `ArtifactRules` only.

    git semantics: patterns are relative to their .gitignore's directory; the deepest
    .gitignore with a matching pattern decides (last match wins inside one file); an
    excluded directory is not entered, so nothing under it can be re-included. Tracked paths
    are never excluded, so an excluded directory holding one is entered, and only its
    tracked paths are kept."""

    def __init__(self, rules: ArtifactRules) -> None:
        for name in rules.fixed_dirs:
            if not name or "/" in name or name in (".", ".."):
                raise ValueError(f"fixed_dirs holds directory names, not {name!r}")
        by_dir: dict[str, list[str]] = {}
        for base, line in rules.gitignore:
            if base:
                try:
                    check_rel_path(base)
                except ValueError as exc:
                    raise ValueError(f"gitignore base {base!r}: {exc}") from None
            by_dir.setdefault(base, []).append(line)
        self._specs = {base: _compile(lines) for base, lines in by_dir.items()}
        self._fixed = rules.fixed_dirs
        self._tracked = rules.tracked
        self._tracked_dirs = {
            "/".join(parts[:i])
            for parts in (p.split("/") for p in rules.tracked)
            for i in range(1, len(parts))
        }
        self._ignored_dir: dict[str, bool] = {"": False}

    def _matches(self, path: str, *, is_dir: bool) -> bool:
        parts = path.split("/")
        for i in range(len(parts) - 1, -1, -1):
            spec = self._specs.get("/".join(parts[:i]))
            if spec is None:
                continue
            include = spec.check_file("/".join(parts[i:]) + ("/" if is_dir else "")).include
            if include is not None:
                return include
        return False

    def descend(self, path: str) -> bool:
        """Called once per directory, parents first: whether the walk enters it."""
        parent, _, name = path.rpartition("/")
        ignored = (
            self._ignored_dir[parent] or name in self._fixed or self._matches(path, is_dir=True)
        )
        if ignored and path not in self._tracked_dirs:
            return False
        self._ignored_dir[path] = ignored
        return True

    def included(self, path: str) -> bool:
        if path in self._tracked:
            return True
        if self._ignored_dir[path.rpartition("/")[0]]:
            return False
        return not self._matches(path, is_dir=False)


# ---------------------------------------------------------------- the workspace


class HostWorkspace:
    """`Workspace` (sandbox/base.py) over a host directory, with `openat2` containment.

    Errors: PathEscapeError for anything containment refuses (bad path, symlink, special or
    setuid file, over the read cap, unrepresentable name, too deep); otherwise the OSError
    the operation hit (FileNotFoundError, FileExistsError, IsADirectoryError,
    NotADirectoryError, PermissionError). RuntimeError while the sandbox is not frozen.
    """

    def __init__(self, root: Path, *, sandbox: Sandbox | None = None) -> None:
        if not root.is_absolute():
            raise ValueError(f"workspace root must be an absolute path: {root}")
        _load_openat2()
        self._root = root
        self._sandbox = sandbox
        with _closing(self._open_root()) as fd:  # probe: ENOSYS on kernels before 5.6
            os.close(_openat2(fd, ".", _O_PATH_DIR))

    @property
    def root(self) -> Path:
        return self._root

    # -- plumbing

    def _require_frozen(self) -> None:
        if self._sandbox is not None and not self._sandbox.is_frozen:
            raise RuntimeError("workspace touched while the sandbox is not frozen (§9.5)")

    def _open_root(self) -> int:
        """A fresh O_PATH descriptor of the root, refused if the root became a symlink."""
        return _open_directory(self._root, "workspace root")

    def _open_dir(self, root: int, parts: Sequence[str], *, create: bool) -> int:
        """O_PATH descriptor of the directory `parts` under `root`; with `create`, missing
        levels are made one at a time (mkdirat never follows, and each level is re-opened
        with openat2, so a link planted at any level is refused)."""
        if not parts:
            return os.dup(root)
        if not create:
            return _openat2(root, "/".join(parts), _O_PATH_DIR)
        fd = os.dup(root)
        try:
            for name in parts:
                try:
                    nxt = _openat2(fd, name, _O_PATH_DIR)
                except FileNotFoundError:
                    os.mkdir(name, 0o755, dir_fd=fd)
                    nxt = _openat2(fd, name, _O_PATH_DIR)
                os.close(fd)
                fd = nxt
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _stat_entry(self, dirfd: int, name: str) -> os.stat_result:
        """Type of `name` in `dirfd` via an O_PATH descriptor (opens nothing for I/O)."""
        with _closing(_openat2(dirfd, name, _O_PATH_NOFOLLOW)) as fd:
            return os.fstat(fd)

    def _open_regular(
        self, dirfd: int, name: str, rel: str, flags: int
    ) -> tuple[int, os.stat_result]:
        """Open a plain regular file for I/O: type-check through O_PATH first, then open
        with O_NOFOLLOW | O_NONBLOCK and re-check type and inode on the open descriptor."""
        st = self._stat_entry(dirfd, name)
        _require_regular(st, rel)
        fd = _openat2(dirfd, name, flags | _O_SAFE_IO)
        try:
            opened = os.fstat(fd)
            if not _same_inode(st, opened):
                raise PathEscapeError(f"{rel!r} was replaced while it was being opened")
            _require_regular(opened, rel)
        except BaseException:
            os.close(fd)
            raise
        return fd, opened

    def _read_regular(
        self, dirfd: int, name: str, rel: str, max_bytes: int
    ) -> tuple[bytes, os.stat_result]:
        fd, st = self._open_regular(dirfd, name, rel, os.O_RDONLY)
        with _closing(fd):
            if st.st_size > max_bytes:
                raise PathEscapeError(f"{rel!r} is over the read cap of {max_bytes} bytes")
            return _read_capped(fd, max_bytes, rel), st

    def _old_for_diff(self, dirfd: int, name: str, rel: str) -> tuple[bytes | None, os.stat_result]:
        """Current content for the diff (None if over DIFF_MAX_BYTES) and its stat."""
        fd, st = self._open_regular(dirfd, name, rel, os.O_RDONLY)
        with _closing(fd):
            if st.st_size > DIFF_MAX_BYTES:
                return None, st
            try:
                return _read_capped(fd, DIFF_MAX_BYTES, rel), st
            except PathEscapeError:  # grew past the cap while being read
                return None, st

    def _create_file(self, dirfd: int, name: str, data: bytes, mode: int) -> None:
        """Create `name` (it must not exist) holding `data` with exactly `mode`."""
        fd = _openat2(dirfd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_SAFE_IO, 0o600)
        try:
            with _closing(fd):
                _write_all(fd, data)
                os.fchmod(fd, mode)
        except BaseException:
            with suppress(OSError):
                os.unlink(name, dir_fd=dirfd)
            raise

    def _replace_file(self, dirfd: int, name: str, data: bytes, mode: int) -> None:
        tmp = f".mpilot-{secrets.token_hex(8)}.tmp"
        self._create_file(dirfd, tmp, data, mode)
        try:
            os.rename(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp, dir_fd=dirfd)
            raise

    def _open_listing(self, dirfd: int, name: str) -> int:
        """O_RDONLY descriptor of a real directory (for listing); never follows."""
        return _openat2(dirfd, name, _O_LIST_DIR)

    def _entries(
        self, dirfd: int, prefix: str, level: int, max_level: int | None, *, descend: _Descend
    ) -> Generator[tuple[str, os.stat_result, int, str]]:
        """(path, lstat, parent fd, name) for every entry under `dirfd`, sorted by name,
        `.git` skipped, parents before children. Directories are entered only if they are
        real directories, `level < max_level` and `descend(path)` agrees. The parent fd is
        valid only until the next item."""
        names = sorted(n for n in os.listdir(dirfd) if n != ".git")
        if names and level > MAX_DEPTH:
            raise PathEscapeError(f"{prefix!r}: the tree is deeper than {MAX_DEPTH} levels")
        for name in names:
            path = prefix + name
            st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            yield path, st, dirfd, name
            if (
                stat.S_ISDIR(st.st_mode)
                and (max_level is None or level < max_level)
                and descend(path)
            ):
                with _closing(self._open_listing(dirfd, name)) as child:
                    yield from self._entries(
                        child, path + "/", level + 1, max_level, descend=descend
                    )

    # -- per-file operations (view_file, create_file, str_replace, delete_file)

    def read_text(self, rel: str, *, max_bytes: int) -> str:
        self._require_frozen()
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        parts = _parts(rel)
        with (
            _contained(rel),
            _closing(self._open_root()) as root,
            _closing(self._open_dir(root, parts[:-1], create=False)) as dirfd,
        ):
            data = self._read_regular(dirfd, parts[-1], rel, max_bytes)[0]
        return data.decode("utf-8")

    def read_blob(self, rel: str, *, max_bytes: int) -> FileBlob:
        self._require_frozen()
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        parts = _parts(rel)
        name = parts[-1]
        with (
            _contained(rel),
            _closing(self._open_root()) as root,
            _closing(self._open_dir(root, parts[:-1], create=False)) as dirfd,
        ):
            st = self._stat_entry(dirfd, name)
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(os.fsencode(name), dir_fd=dirfd)
                if not _same_inode(st, os.stat(name, dir_fd=dirfd, follow_symlinks=False)):
                    raise PathEscapeError(f"{rel!r} was replaced while it was being read")
                if len(target) > max_bytes:
                    raise PathEscapeError(f"{rel!r} is over the read cap of {max_bytes} bytes")
                return FileBlob(mode=_LINK, data=target)
            data, st = self._read_regular(dirfd, name, rel, max_bytes)
            return FileBlob(mode=_git_mode(st), data=data)

    def write_text(self, rel: str, content: str, *, create: bool) -> FileDiff:
        self._require_frozen()
        parts = _parts(rel)
        name = parts[-1]
        data = content.encode("utf-8")
        with (
            _contained(rel),
            _closing(self._open_root()) as root,
            _closing(self._open_dir(root, parts[:-1], create=create)) as dirfd,
        ):
            if create:
                try:
                    existing = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                except FileNotFoundError:
                    self._create_file(dirfd, name, data, 0o644)
                    return FileDiff(
                        path=rel, kind=ChangeKind.ADDED, unified=unified_diff(rel, None, data)
                    )
                if stat.S_ISLNK(existing.st_mode):
                    raise PathEscapeError(f"{rel!r} is a symlink (never followed)")
                raise FileExistsError(errno.EEXIST, "already exists", rel)
            old, st = self._old_for_diff(dirfd, name, rel)
            self._replace_file(dirfd, name, data, stat.S_IMODE(st.st_mode))
        unified = "" if old is None else unified_diff(rel, old, data)
        return FileDiff(path=rel, kind=ChangeKind.MODIFIED, unified=unified)

    def delete(self, rel: str) -> FileDiff:
        self._require_frozen()
        parts = _parts(rel)
        name = parts[-1]
        with (
            _contained(rel),
            _closing(self._open_root()) as root,
            _closing(self._open_dir(root, parts[:-1], create=False)) as dirfd,
        ):
            old, st = self._old_for_diff(dirfd, name, rel)
            if not _same_inode(st, os.stat(name, dir_fd=dirfd, follow_symlinks=False)):
                raise PathEscapeError(f"{rel!r} was replaced while it was being deleted")
            os.unlink(name, dir_fd=dirfd)
        unified = "" if old is None else unified_diff(rel, old, None)
        return FileDiff(path=rel, kind=ChangeKind.DELETED, unified=unified)

    # -- listings and trees

    def list_dir(self, rel: str, depth: int) -> list[DirEntry]:
        self._require_frozen()
        if not 1 <= depth <= 3:
            raise ValueError("list_dir depth is 1 to 3 (§9.1)")
        top = rel not in ("", ".")
        parts = _parts(rel) if top else []
        out: list[DirEntry] = []
        with (
            _contained(rel),
            _closing(self._open_root()) as root,
            _closing(_openat2(root, "/".join(parts) or ".", _O_LIST_DIR)) as dirfd,
            closing(
                self._entries(dirfd, rel + "/" if top else "", 1, depth, descend=_always)
            ) as walk,
        ):
            for path, st, _, _ in walk:
                _check_walked(path)
                out.append(DirEntry(path=path, type=_entry_type(st), size=_entry_size(st)))
        return sorted(out, key=lambda e: e.path)

    def walk_tree(self, rules: ArtifactRules) -> TreeSnapshot:
        self._require_frozen()
        exclusions = _Exclusions(rules)
        entries: dict[str, TreeEntry] = {}
        with (
            _contained("."),
            _closing(self._open_root()) as root,
            _closing(_openat2(root, ".", _O_LIST_DIR)) as top,
            closing(self._entries(top, "", 1, None, descend=exclusions.descend)) as walk,
        ):
            for path, st, dirfd, name in walk:
                if stat.S_ISDIR(st.st_mode) or not exclusions.included(path):
                    continue
                _check_walked(path)
                entries[path] = self._tree_entry(dirfd, name, path, st)
        return TreeSnapshot(entries=entries)

    def _tree_entry(self, dirfd: int, name: str, path: str, st: os.stat_result) -> TreeEntry:
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(os.fsencode(name), dir_fd=dirfd)
            return TreeEntry(mode=_LINK, sha256=hashlib.sha256(target).hexdigest())
        reason = refusal_reason(st.st_mode)
        if reason is not None:
            raise PathEscapeError(f"{path!r} {reason}")
        fd, opened = self._open_regular(dirfd, name, path, os.O_RDONLY)
        with _closing(fd):
            return TreeEntry(mode=_git_mode(opened), sha256=_hash_fd(fd))

    def iter_paths(self) -> Iterable[str]:
        """Every non-directory path (files, links, special files), sorted, `.git` excluded,
        links not followed. A list, so no descriptor outlives the call."""
        self._require_frozen()
        out: list[str] = []
        with (
            _contained("."),
            _closing(self._open_root()) as root,
            _closing(_openat2(root, ".", _O_LIST_DIR)) as top,
            closing(self._entries(top, "", 1, None, descend=_always)) as walk,
        ):
            for path, st, _, _ in walk:
                if not stat.S_ISDIR(st.st_mode):
                    _check_walked(path)
                    out.append(path)
        return out

    def replace_tree(self, files: Mapping[str, FileBlob]) -> TreeSnapshot:
        """Make the workspace hold exactly `files`, `.git` (at the root) untouched.

        Everything is validated before anything changes. Files that already match (kind,
        exec bit, content; never a setuid file) are left alone; everything else, links and
        nested `.git` directories included, is removed without following any link.
        Directories the sandbox made unreadable or read-only are given owner rwx first.
        Not atomic: if it raises part-way, the tree is partly replaced; retry, or rebuild
        the workspace (P2-04)."""
        self._require_frozen()
        wanted, wanted_dirs = _validate_tree(files)
        with _contained("."), _closing(self._open_root()) as root:
            keep: set[str] = set()
            with _closing(self._open_writable_dir(root, ".")) as top:
                self._prune(top, "", 1, wanted, wanted_dirs, keep)
            for path in sorted(wanted.keys() - keep):
                parts = path.split("/")
                with _closing(self._open_dir(root, parts[:-1], create=True)) as dirfd:
                    blob = wanted[path]
                    if blob.mode == _LINK:
                        os.symlink(blob.data, os.fsencode(parts[-1]), dir_fd=dirfd)
                    else:
                        mode = 0o755 if blob.mode == "100755" else 0o644
                        self._create_file(dirfd, parts[-1], blob.data, mode)
        return self.walk_tree(ArtifactRules())

    def _open_writable_dir(self, dirfd: int, name: str) -> int:
        """O_RDONLY descriptor of a real directory, with owner rwx added if it lacked any
        (chmod goes through /proc/self/fd of an O_PATH descriptor of the verified inode, so
        no name is resolved again)."""
        with _closing(_openat2(dirfd, name, _O_PATH_DIR)) as pfd:
            mode = stat.S_IMODE(os.fstat(pfd).st_mode)
            if mode & 0o700 != 0o700:
                os.chmod(f"/proc/self/fd/{pfd}", mode | 0o700)
            return self._open_listing(dirfd, name)

    def _prune(
        self,
        dirfd: int,
        prefix: str,
        level: int,
        wanted: Mapping[str, FileBlob],
        wanted_dirs: set[str],
        keep: set[str],
        *,
        keep_git: bool = True,
    ) -> None:
        names = [n for n in os.listdir(dirfd) if not (keep_git and level == 1 and n == ".git")]
        if names and level > MAX_DEPTH:
            raise PathEscapeError(f"{prefix!r}: the tree is deeper than {MAX_DEPTH} levels")
        for name in names:
            path = prefix + name
            st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                with _closing(self._open_writable_dir(dirfd, name)) as child:
                    if path in wanted_dirs:
                        self._prune(child, path + "/", level + 1, wanted, wanted_dirs, keep)
                    else:
                        self._prune(
                            child, path + "/", level + 1, {}, set(), keep, keep_git=keep_git
                        )
                if path not in wanted_dirs:
                    os.rmdir(name, dir_fd=dirfd)
                continue
            blob = wanted.get(path)
            if blob is not None and self._matches(dirfd, name, path, st, blob):
                keep.add(path)
            else:
                os.unlink(name, dir_fd=dirfd)

    def _matches(
        self, dirfd: int, name: str, path: str, st: os.stat_result, blob: FileBlob
    ) -> bool:
        if blob.mode == _LINK:
            return stat.S_ISLNK(st.st_mode) and (
                os.readlink(os.fsencode(name), dir_fd=dirfd) == blob.data
            )
        if refusal_reason(st.st_mode) is not None or _git_mode(st) != blob.mode:
            return False
        if st.st_size != len(blob.data):
            return False
        try:
            data, _ = self._read_regular(dirfd, name, path, len(blob.data))
        except (PermissionError, PathEscapeError):
            return False
        return data == blob.data

    # -- export and restore (FR-21, §9.5)

    def export_to(self, dest: Path) -> ExportManifest:
        """Copy the whole workspace, `.git` included, to the host directory `dest` and return
        the hash manifest (what `Sandbox.export_workspace` calls, while frozen).

        Every read is contained like any other host access: links are copied as links and never
        followed, and a FIFO, socket, device, setuid/setgid file, non-UTF-8 or backslash name,
        or a tree deeper than MAX_DEPTH is refused (PathEscapeError), as is an unreadable
        directory (PermissionError). Contents are hashed as they are copied, so the manifest
        describes exactly the bytes in `dest`. Empty directories are kept; modes are reduced to
        the exec bit as git does; modification times are kept for files. `dest` must be absent
        or empty, outside the workspace, and not a symlink. If anything fails, what was copied
        is removed again, so a partial export never looks complete."""
        self._require_frozen()
        _export_destination(dest, self._root)
        entries: dict[str, TreeEntry] = {}
        try:
            with (
                _contained("."),
                _closing(self._open_root()) as root,
                _closing(_openat2(root, ".", _O_LIST_DIR)) as src,
                _closing(_open_dest(dest)) as dst,
            ):
                self._copy_dir(src, dst, "", 1, entries)
        except BaseException:
            self._wipe(dest)
            raise
        return ExportManifest(export_dir=str(dest), entries=entries)

    def restore_from(self, manifest: ExportManifest, expected_digest: str) -> None:
        """Rebuild this (empty) workspace from the export `manifest` describes.

        Refused with ExportIntegrityError, before anything is written, if the manifest's digest
        is not `expected_digest` (the digest the DB checkpoint holds), and again, with the
        workspace emptied, if what the export directory really holds hashes to something
        else: a file added, removed, changed, or swapped for a link. The export is read with
        the same containment as a workspace: a planted link is copied as a link, never read
        through. The workspace must be empty."""
        self._require_frozen()
        if manifest.digest() != expected_digest:
            raise ExportIntegrityError(
                "the export's manifest does not match the checkpoint's digest"
            )
        source = Path(manifest.export_dir)
        if not source.is_absolute():
            raise ValueError(f"export directory must be an absolute path: {source}")
        with (
            _contained("."),
            _closing(self._open_root()) as root,
            _closing(_openat2(root, ".", _O_LIST_DIR)) as top,
        ):
            if os.listdir(top):
                raise ValueError(f"workspace is not empty: {self._root}")
        entries: dict[str, TreeEntry] = {}
        try:
            with (
                _contained("."),
                _closing(self._open_root()) as root,
                _closing(_open_directory(source, "export directory")) as src_path,
                _closing(_openat2(src_path, ".", _O_LIST_DIR)) as src,
                _closing(_openat2(root, ".", _O_LIST_DIR)) as dst,
            ):
                self._copy_dir(src, dst, "", 1, entries)
            actual = ExportManifest(export_dir=str(source), entries=entries).digest()
            if actual != expected_digest:
                raise ExportIntegrityError(
                    "the export does not match the checkpoint's digest: a file was added, "
                    "removed or changed since it was written"
                )
        except BaseException:
            self._wipe(self._root)  # nothing half-restored is left behind
            raise

    def clear(self) -> None:
        """Remove everything in the workspace, `.git` included, without following any link
        (undoes a failed restore). Needs a frozen sandbox if one is bound."""
        self._require_frozen()
        self._wipe(self._root)

    def _wipe(self, directory: Path) -> None:
        """Remove everything under `directory`, `.git` included, without following any link."""
        with (
            suppress(OSError, PathEscapeError),
            _closing(_open_directory(directory, "wipe")) as path_fd,
            _closing(_openat2(path_fd, ".", _O_LIST_DIR)) as top,
        ):
            self._prune(top, "", 1, {}, set(), set(), keep_git=False)

    def _copy_dir(
        self, src: int, dst: int, prefix: str, level: int, entries: dict[str, TreeEntry]
    ) -> None:
        """Copy the directory `src` into the (writable, host-owned) directory `dst`, recording
        each file and link in `entries`. `src` is read only through `openat2`."""
        names = sorted(os.listdir(src))
        if names and level > MAX_DEPTH:
            raise PathEscapeError(f"{prefix!r}: the tree is deeper than {MAX_DEPTH} levels")
        for name in names:
            path = prefix + name
            _check_export_name(path)
            st = os.stat(name, dir_fd=src, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                os.mkdir(name, 0o755, dir_fd=dst)
                with (
                    _closing(self._open_listing(src, name)) as child_src,
                    _closing(_open_child_directory(dst, name)) as child_dst,
                ):
                    self._copy_dir(child_src, child_dst, path + "/", level + 1, entries)
            elif stat.S_ISLNK(st.st_mode):
                target = os.readlink(os.fsencode(name), dir_fd=src)
                if not _same_inode(st, os.stat(name, dir_fd=src, follow_symlinks=False)):
                    raise PathEscapeError(f"{path!r} was replaced while it was being copied")
                os.symlink(target, os.fsencode(name), dir_fd=dst)
                entries[path] = TreeEntry(mode=_LINK, sha256=hashlib.sha256(target).hexdigest())
            else:
                entries[path] = self._copy_file(src, dst, name, path)

    def _copy_file(self, src: int, dst: int, name: str, path: str) -> TreeEntry:
        fd, opened = self._open_regular(src, name, path, os.O_RDONLY)
        with _closing(fd):
            out = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dst,
            )
            digest = hashlib.sha256()
            with _closing(out):
                while chunk := os.read(fd, _CHUNK):
                    digest.update(chunk)
                    _write_all(out, chunk)
                mode = _git_mode(opened)
                os.fchmod(out, 0o755 if mode == "100755" else 0o644)
                os.utime(out, ns=(opened.st_atime_ns, opened.st_mtime_ns))
        return TreeEntry(mode=mode, sha256=digest.hexdigest())


# ---------------------------------------------------------------- export and restore (FR-21)


def _check_export_name(path: str) -> None:
    """A name found while copying a whole workspace must be one the manifest can hold. `.git`
    is allowed here (an export carries the whole workspace), unlike in `_check_walked`."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise PathEscapeError(f"{path!r}: file name is not UTF-8") from None
    if "\\" in path or "\x00" in path:
        raise PathEscapeError(f"{path!r}: file name holds a backslash or NUL")


def _export_destination(dest: Path, root: Path) -> None:
    """Refuse a destination that is not a safe, empty, host-owned place. Created (mode 0700)
    when its parent exists."""
    if not dest.is_absolute():
        raise ValueError(f"export destination must be an absolute path: {dest}")
    try:
        mode = dest.lstat().st_mode
    except FileNotFoundError:
        _refuse_overlap(dest, root)
        dest.parent.lstat()  # FileNotFoundError if the parent is missing too
        dest.mkdir(mode=0o700)
        return
    if stat.S_ISLNK(mode):
        raise ValueError(f"export destination is a symlink: {dest}")
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError(errno.ENOTDIR, "export destination is not a directory", str(dest))
    _refuse_overlap(dest, root)
    if any(dest.iterdir()):
        raise ValueError(f"export destination is not empty: {dest}")


def _refuse_overlap(dest: Path, root: Path) -> None:
    real_dest, real_root = dest.resolve(), root.resolve()
    if real_dest == real_root or real_dest.is_relative_to(real_root):
        raise ValueError(f"export destination {dest} is inside the workspace {root}")
    if real_root.is_relative_to(real_dest):
        raise ValueError(f"export destination {dest} contains the workspace {root}")


def _open_dest(dest: Path) -> int:
    """O_RDONLY descriptor of the export destination; a symlink is refused, never followed."""
    return os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _open_child_directory(dirfd: int, name: str) -> int:
    """O_RDONLY descriptor of a directory this process just created in a host-owned tree."""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)


def _always(path: str) -> bool:
    return True


def _entry_type(st: os.stat_result) -> EntryType:
    if stat.S_ISDIR(st.st_mode):
        return EntryType.DIR
    if stat.S_ISLNK(st.st_mode):
        return EntryType.SYMLINK
    if stat.S_ISREG(st.st_mode):
        return EntryType.FILE
    return EntryType.OTHER


def _entry_size(st: os.stat_result) -> int:
    return 0 if stat.S_ISDIR(st.st_mode) else st.st_size


def _validate_tree(files: Mapping[str, FileBlob]) -> tuple[dict[str, FileBlob], set[str]]:
    """Check a whole target tree before anything is touched; return it and its directories."""
    for path, blob in files.items():
        _parts(path)
        if blob.mode == _LINK and (not blob.data or b"\x00" in blob.data):
            raise ValueError(f"{path!r}: a symlink target must be non-empty and contain no NUL")
    dirs = {"/".join(p.split("/")[:i]) for p in files for i in range(1, p.count("/") + 1)}
    clash = sorted(dirs & files.keys())
    if clash:
        raise ValueError(f"{clash[0]!r} is both a file and a directory")
    return dict(files), dirs
