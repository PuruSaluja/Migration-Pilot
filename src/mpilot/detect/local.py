"""A read-only `TreeReader` over a directory on disk (SPEC FR-3; P2-12b).

`mpilot detect --repo <path>` has no mirror commit to read, so it reads the directory itself.
The directory is untrusted repo content (H6): everything goes through `HostWorkspace`
(`openat2` containment), so a symlink is never followed, a FIFO, device or setuid file is
refused, and a read is size-capped. Vendored and cache folders (`node_modules`, `.venv`, ...)
and `.git` are left out: detection reads the repository's own files.

`commit` is **not a git object**. It is a 40-hex fingerprint of the tree's content (so two
reads of an unchanged directory agree and a changed file changes it), because the protocol
wants a commit-shaped id. A real run reads a mirror commit instead.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final

from mpilot.core.errors import InputError, PathEscapeError
from mpilot.models.artifacts import ArtifactRules, TreeSnapshot
from mpilot.sandbox.workspace import HostWorkspace

MAX_READ_BYTES: Final = 1 << 20  # one file: detection only parses small config files
MAX_PATHS: Final = 200_000
SKIPPED_DIRS: Final = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
_REGULAR: Final = frozenset({"100644", "100755"})


class LocalTree:
    """Build one with `open_local_tree`."""

    def __init__(self, workspace: HostWorkspace, snapshot: TreeSnapshot) -> None:
        self._workspace = workspace
        self._snapshot = snapshot
        fingerprint = "local-tree:" + snapshot.digest()
        self._commit = hashlib.sha1(fingerprint.encode(), usedforsecurity=False).hexdigest()

    @property
    def commit(self) -> str:
        return self._commit

    def snapshot(self) -> TreeSnapshot:
        return self._snapshot

    def paths(self) -> frozenset[str]:
        return frozenset(self._snapshot.entries)

    def read_text(self, rel_path: str) -> str | None:
        entry = self._snapshot.entries.get(rel_path)
        if entry is None or entry.mode not in _REGULAR:
            return None
        try:
            text = self._workspace.read_text(rel_path, max_bytes=MAX_READ_BYTES)
        except (PathEscapeError, OSError, UnicodeDecodeError):
            return None
        return None if "\x00" in text else text


def open_local_tree(path: Path) -> LocalTree:
    """Walk `path` once and return a reader over it. Raises `InputError` (exit 2) for a path
    that is not a directory or is a symlink, a tree holding a file that cannot be read safely
    (a FIFO, a device, a setuid file), or one with more than `MAX_PATHS` files."""
    root = Path(os.path.abspath(path))
    if root.is_symlink():
        raise InputError(f"{path} is a symlink; give the real directory")
    if not root.is_dir():
        raise InputError(f"{path} is not a directory")
    workspace = HostWorkspace(root)
    try:
        snapshot = workspace.walk_tree(ArtifactRules(fixed_dirs=SKIPPED_DIRS))
    except PathEscapeError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if len(snapshot.entries) > MAX_PATHS:
        raise InputError(f"{path} has more than {MAX_PATHS} files; refusing to scan it")
    return LocalTree(workspace, snapshot)
