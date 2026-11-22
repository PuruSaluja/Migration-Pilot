"""Disk limits (D6, SPEC §9.3): the `/tmp` cap, the poll interval, and the `runs/` check.

Docker cannot enforce a disk quota on ext4, so three things bound a sandbox's disk use:
1. `runs/` (workspaces, mirrors, exports, package caches) sits on a fixed-size loopback
   filesystem, a hard ceiling that protects the host disk. `check_runs_filesystem` verifies it.
2. The driver polls the workspace and the mounted package cache while a command runs and kills
   it at `sandbox.disk_gb` (`HostWorkspace.disk_usage_bytes`, `DockerSandbox.exec`).
3. `/tmp` is a size-capped tmpfs, and the container's root is read-only, so nothing else grows.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

GIB: Final = 1024**3
MIB: Final = 1024**2

#: Seconds between disk polls while a command runs, and before the first one. A writer can
#: overshoot the limit by roughly (write speed x this interval) before it is stopped; the
#: fixed-size `runs/` filesystem is the hard ceiling behind it.
DISK_POLL_S: Final = 1.0

#: `/tmp` cap. The spec gives no number; 1 GiB covers pip/npm/tsc scratch space. tmpfs pages are
#: memory, so the cap never exceeds half the container's memory limit.
TMP_MAX_BYTES: Final = GIB


def human_bytes(n: int) -> str:
    if n >= GIB:
        return f"{n / GIB:.1f} GiB"
    return f"{n / MIB:.1f} MiB"


def tmp_cap_bytes(memory_gb: int) -> int:
    return min(TMP_MAX_BYTES, memory_gb * GIB // 2)


def tmpfs_options(memory_gb: int) -> str:
    """Mount options of the container's `/tmp`: capped, sticky, never suid or a device."""
    return f"rw,nosuid,nodev,size={tmp_cap_bytes(memory_gb)},mode=1777"


@dataclass(frozen=True)
class RunsFilesystem:
    path: Path
    mount_point: str
    source: str
    fstype: str
    total_bytes: int
    loopback: bool
    problems: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


_ESCAPE = re.compile(r"\\([0-7]{3})")
_IN_MEMORY = frozenset({"tmpfs", "ramfs", "devtmpfs"})


def _unescape(field: str) -> str:
    """mountinfo writes space, tab, newline and backslash as octal escapes."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def _mounts(text: str) -> list[tuple[str, str, str]]:
    """(mount point, filesystem type, source) for every line of `/proc/self/mountinfo`."""
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if "-" not in parts:
            continue
        dash = parts.index("-")
        if dash < 6 or len(parts) < dash + 3:
            continue
        out.append((_unescape(parts[4]), parts[dash + 1], _unescape(parts[dash + 2])))
    return out


def check_runs_filesystem(
    path: Path,
    *,
    disk_gb: int,
    mountinfo: str | None = None,
    total_bytes: int | None = None,
) -> RunsFilesystem:
    """Is `path` (the `runs/` directory) on a fixed-size filesystem that cannot fill the host's
    disk? Problems (the sandbox selftest fails on them): missing directory, on the root
    filesystem, in memory, or smaller than one run's `disk_gb`. A real partition instead of a
    loop device is only a warning (it is just as bounded, but the spec says loopback).
    `mountinfo` and `total_bytes` are for tests; by default they come from the host."""
    problems: list[str] = []
    warnings: list[str] = []
    if mountinfo is None:
        if not path.exists():
            return RunsFilesystem(
                path, "", "", "", 0, False, (f"runs directory {path} does not exist",), ()
            )
        mountinfo = Path("/proc/self/mountinfo").read_text()
    real = str(path.resolve())
    best = ("", "", "")
    for mount_point, fstype, source in _mounts(mountinfo):
        inside = real == mount_point or real.startswith(mount_point.rstrip("/") + "/")
        if inside and len(mount_point) >= len(best[0]):
            best = (mount_point, fstype, source)
    mount_point, fstype, source = best
    if total_bytes is None:
        stats = os.statvfs(path)
        total_bytes = stats.f_blocks * stats.f_frsize
    loopback = source.startswith("/dev/loop")
    if mount_point == "/":
        problems.append(
            f"{path} is on the root filesystem: runs can fill the host's disk. Put it on the "
            "fixed-size loopback image scripts/setup-dev.sh creates (D6)"
        )
    elif fstype in _IN_MEMORY:
        problems.append(f"{path} is on {fstype}: run data would live in memory and be lost")
    elif not loopback:
        warnings.append(f"{path} is on {source or 'an unknown device'}, not a loopback image (D6)")
    if total_bytes < disk_gb * GIB:
        problems.append(
            f"the filesystem holding {path} is {total_bytes / GIB:.1f} GiB, smaller than one "
            f"run's disk limit (sandbox.disk_gb = {disk_gb})"
        )
    return RunsFilesystem(
        path, mount_point, source, fstype, total_bytes, loopback, tuple(problems), tuple(warnings)
    )
