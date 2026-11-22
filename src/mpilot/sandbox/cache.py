"""Package caches (SPEC §9.3 Hardening and Reproducibility, NFR-5).

Layout, under the cache root (`runs/cache/`, on the same fixed-size loopback filesystem as the
rest of `runs/`):

    <root>/<key>/                    a run cache, shared by every run whose lockfiles hash to <key>
    <root>/fresh/<run_id>-<random>/  a fresh cache: one sandbox, empty at the start, removed after

Mounting. A container sees its cache at `/cache`. The work container (no network) gets it
**read-only**; only an install container (P2-09) gets it read-write, so nothing a command in the
work container does can reach the cache. The package managers are pointed at `/cache` through
`CACHE_ENV` (set at container level by the driver; not part of the per-command environment, so a
command cannot move them).

What the cache is trusted for. An install container runs package install scripts and writes
the cache, so a later run that reads a run cache reads what that script left. The package
managers check content hashes when they read (npm's cacache, the pnpm store) but pip's HTTP
cache is not checked against anything unless the lockfile carries hashes. That is why final
verification never uses a run cache (§9.3, §9.5): it uses `CacheMode.FRESH`, or later (P3-13)
`HASH_VERIFIED`, which this module does not implement.

The host never opens files in a cache except through the contained walker (`HostWorkspace` over
the cache directory, metadata only), the same rule as for workspaces (§9.5).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import shutil
import stat
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

CACHE_MOUNT: Final = "/cache"
CACHE_KEY: Final = re.compile(r"[0-9a-f]{64}")
_RUN_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FRESH_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,64}-[0-9a-f]{12}")
FRESH_DIR: Final = "fresh"

#: Where each package manager keeps its cache (verified in the images by
#: `tests/unit/test_cache_docker.py`). XDG_CACHE_HOME catches tools that follow the standard
#: (corepack, node-gyp, the pip/uv fallbacks).
CACHE_ENV: Final[Mapping[str, str]] = {
    "XDG_CACHE_HOME": f"{CACHE_MOUNT}/xdg",
    "npm_config_cache": f"{CACHE_MOUNT}/npm",
    "npm_config_store_dir": f"{CACHE_MOUNT}/pnpm-store",
    "YARN_CACHE_FOLDER": f"{CACHE_MOUNT}/yarn",
    "PIP_CACHE_DIR": f"{CACHE_MOUNT}/pip",
    "UV_CACHE_DIR": f"{CACHE_MOUNT}/uv",
    "POETRY_CACHE_DIR": f"{CACHE_MOUNT}/poetry",
    "PDM_CACHE_DIR": f"{CACHE_MOUNT}/pdm",
}


#: Left out of the work container's environment. Yarn classic creates its cache folder when it
#: starts, even for `yarn test`, and fails with EROFS on a read-only mount until some install
#: has created `/cache/yarn/v6`. Without the variable it uses its default under `/tmp`.
WORK_ENV_EXCLUDED: Final = frozenset({"YARN_CACHE_FOLDER"})


class CacheError(ValueError):
    """A cache key, path or directory that may not be used."""


class CacheRole(StrEnum):
    WORK = "work"  # read-only
    INSTALL = "install"  # read-write


def cache_env(role: CacheRole) -> dict[str, str]:
    """The container-level cache environment for a container of `role`."""
    if role is CacheRole.INSTALL:
        return dict(CACHE_ENV)
    return {k: v for k, v in CACHE_ENV.items() if k not in WORK_ENV_EXCLUDED}


def cache_volume(host_path: Path, role: CacheRole) -> dict[str, dict[str, str]]:
    """The Docker `volumes` entry that mounts `host_path` at `/cache` for a container of `role`."""
    mode = "rw" if role is CacheRole.INSTALL else "ro"
    return {str(host_path): {"bind": CACHE_MOUNT, "mode": mode}}


def cache_key(ecosystem: str, lockfiles: Mapping[str, bytes]) -> str:
    """The key of a run cache: SHA-256 over the ecosystem and every lockfile's path and content.

    `lockfiles` maps repo-relative paths (`package-lock.json`, `packages/a/yarn.lock`) to their
    bytes, as read from the mirror at the commit being worked on. The key changes when any
    lockfile's content changes, when one is added, removed or renamed, and between ecosystems.
    The caller decides which files count; none at all is refused, since the key would then
    name every repo's cache the same.
    """
    if not ecosystem:
        raise CacheError("an ecosystem is needed to key a cache")
    if not lockfiles:
        raise CacheError("no lockfiles: a cache cannot be keyed by nothing (use a fresh cache)")
    document = {
        "ecosystem": ecosystem,
        "lockfiles": {
            path: hashlib.sha256(content).hexdigest() for path, content in sorted(lockfiles.items())
        },
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def check_key(key: str) -> None:
    if not CACHE_KEY.fullmatch(key):
        raise CacheError(f"not a cache key (expected 64 lowercase hex characters): {key!r}")


def _make_dir(path: Path) -> None:
    """`mkdir` that accepts something already there; `_real_dir` then judges what it is."""
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o755, exist_ok=True)


def _real_dir(path: Path, what: str) -> None:
    if not path.is_absolute():
        raise CacheError(f"{what} must be an absolute path: {path}")
    mode = path.lstat().st_mode  # FileNotFoundError if it is missing
    if stat.S_ISLNK(mode):
        raise CacheError(f"{what} is a symlink: {path}")
    if not stat.S_ISDIR(mode):
        raise CacheError(f"{what} is not a directory: {path}")


class CacheStore:
    """The cache directories under one root. Creates and removes them; never reads their files."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise CacheError(f"the cache root must be an absolute path: {root}")
        try:
            _make_dir(root)  # the parent (`runs/`) must already exist
        except FileNotFoundError:
            raise CacheError(
                f"the parent of the cache root does not exist: {root.parent}"
            ) from None
        _real_dir(root, "the cache root")
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def run_dir(self, key: str) -> Path:
        """The run cache for `key`, created if it is missing."""
        check_key(key)
        path = self._root / key
        _make_dir(path)
        _real_dir(path, "the run cache")
        return path

    def fresh_dir(self, run_id: str) -> Path:
        """A new, empty, unshared cache directory for one sandbox of `run_id`."""
        if not _RUN_ID.fullmatch(run_id):
            raise CacheError(f"not a usable run id: {run_id!r}")
        parent = self._root / FRESH_DIR
        _make_dir(parent)
        _real_dir(parent, "the fresh-cache directory")
        path = parent / f"{run_id}-{secrets.token_hex(6)}"
        path.mkdir(mode=0o755)  # exist_ok is False: never adopt a directory that is already there
        return path

    def _is_fresh(self, path: Path) -> bool:
        """Only a directory this store's `fresh_dir` could have made: `<run id>-<12 hex>` directly
        under `fresh/`. (`..`, a run directory, the root and outside paths all fail the pattern.)"""
        return path.parent == self._root / FRESH_DIR and bool(_FRESH_NAME.fullmatch(path.name))

    def remove_fresh(self, path: Path) -> None:
        """Delete a fresh cache directory. Refuses anything that is not one, and a symlink."""
        if not self._is_fresh(path):
            raise CacheError(f"not a fresh cache directory of this store: {path}")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise CacheError(f"not a real directory: {path}")
        shutil.rmtree(path, onexc=_make_writable_and_retry)

    def sweep_fresh(self, run_id: str) -> int:
        """Remove every fresh cache of `run_id` (a crashed or finished run); return how many."""
        if not _RUN_ID.fullmatch(run_id):
            raise CacheError(f"not a usable run id: {run_id!r}")
        parent = self._root / FRESH_DIR
        if not parent.is_dir() or parent.is_symlink():
            return 0
        removed = 0
        own = re.compile(re.escape(run_id) + r"-[0-9a-f]{12}")
        for entry in sorted(parent.iterdir()):
            if own.fullmatch(entry.name):  # `a-b-<hex>` is run `a-b`'s, not run `a`'s
                self.remove_fresh(entry)
                removed += 1
        return removed


def _make_writable_and_retry(func: Callable[[str], Any], path: str, exc: BaseException) -> None:
    """`rmtree` hook: an install may leave read-only directories (Go-style module caches).
    Make the entry's parent writable and try once more; a second failure propagates."""
    del exc
    parent = Path(path).parent
    with contextlib.suppress(OSError):
        parent.chmod(0o755)
    func(path)
