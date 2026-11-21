"""P2-05 (D6, SPEC §9.3): measuring disk use without opening files, and the `runs/` check.

The driver polls the workspace (and later the package cache) while a command runs and kills it
at the limit. The measurement is metadata only (`lstat` allocated blocks through the contained
walker): it never opens a regular file, never follows a link, needs no freezing, and gives the
same number `du` would. `runs/` must sit on a fixed-size filesystem, so nothing can outgrow it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mpilot.core.errors import PathEscapeError
from mpilot.sandbox import disk
from mpilot.sandbox import workspace as workspace_mod
from mpilot.sandbox.workspace import HostWorkspace

MIB = 1024 * 1024


def _put(root: Path, rel: str, data: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _du(root: Path) -> int:
    """Independent reference: allocated bytes of every entry under and including `root`,
    links not followed, hard links once."""
    total = os.lstat(root).st_blocks * 512
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            st = os.lstat(os.path.join(dirpath, name))
            if not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1:
                if (st.st_dev, st.st_ino) in seen:
                    continue
                seen.add((st.st_dev, st.st_ino))
            total += st.st_blocks * 512
    return total


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


# ---------------------------------------------------------------- disk_usage_bytes


def test_usage_is_allocated_blocks_of_everything_including_git(root: Path, tmp_path: Path) -> None:
    _put(root, "src/app.py", b"print(1)\n")
    _put(root, "big.bin", os.urandom(3 * MIB + 17))
    _put(root, ".git/objects/ab/cdef", os.urandom(200_000))
    _put(root, "node_modules/x/index.js", b"y" * 70_000)
    (root / "empty-dir").mkdir()
    (root / "empty.txt").write_bytes(b"")
    (root / "link").symlink_to("big.bin")
    os.link(root / "big.bin", root / "hard.bin")  # the same blocks, counted once
    os.mkfifo(root / "pipe")
    ws = HostWorkspace(root)
    assert ws.disk_usage_bytes() == _du(root)
    assert ws.disk_usage_bytes() >= 3 * MIB + 200_000 + 70_000


def test_usage_grows_with_what_a_command_writes(root: Path) -> None:
    ws = HostWorkspace(root)
    before = ws.disk_usage_bytes()
    _put(root, "out.bin", os.urandom(5 * MIB))
    assert ws.disk_usage_bytes() - before >= 5 * MIB


def test_a_sparse_file_costs_only_what_it_allocates(root: Path) -> None:
    with open(root / "sparse", "wb") as handle:
        handle.truncate(2 * 1024 * MIB)  # 2 GiB of nothing
    assert HostWorkspace(root).disk_usage_bytes() < MIB


def test_links_are_never_followed(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _put(outside, "huge.bin", os.urandom(4 * MIB))
    (root / "dir-link").symlink_to(outside)
    (root / "file-link").symlink_to(outside / "huge.bin")
    assert HostWorkspace(root).disk_usage_bytes() < MIB


def test_measuring_opens_directories_only_never_a_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9.3: "metadata only ... no file opens, so it needs no freezing". A FIFO or device in
    the workspace could block or misbehave if it were opened."""
    _put(root, "a/b/c.txt", b"data")
    os.mkfifo(root / "a" / "pipe")
    flags: list[int] = []
    real = workspace_mod._openat2

    def spy(dirfd: int, path: str, open_flags: int, mode: int = 0) -> int:
        flags.append(open_flags)
        return real(dirfd, path, open_flags, mode)

    ws = HostWorkspace(root)
    monkeypatch.setattr(workspace_mod, "_openat2", spy)
    monkeypatch.setattr(
        workspace_mod.HostWorkspace,
        "_open_regular",
        lambda *a, **k: pytest.fail("a regular file was opened"),
    )
    ws.disk_usage_bytes()
    assert flags
    assert all(f & os.O_DIRECTORY for f in flags)


def test_measuring_needs_no_frozen_sandbox(root: Path) -> None:
    from mpilot.testing.infra_doubles import FakeSandbox

    _put(root, "a.txt")
    box = FakeSandbox()  # not frozen
    assert HostWorkspace(root, sandbox=box).disk_usage_bytes() > 0


def test_it_stops_early_once_the_limit_is_passed(root: Path) -> None:
    for i in range(20):
        _put(root, f"d{i:02d}/f.bin", os.urandom(MIB))
    ws = HostWorkspace(root)
    full = ws.disk_usage_bytes()
    capped = ws.disk_usage_bytes(limit=3 * MIB)
    assert capped > 3 * MIB  # over the limit, which is all the caller needs to know
    assert capped < full  # and the walk did not read the rest


def test_a_file_that_vanishes_mid_walk_is_ignored(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put(root, "keep.txt", b"k" * 8192)
    _put(root, "gone.txt", b"g" * 8192)
    real_scandir = os.scandir

    class Racy:
        def __init__(self, it: object) -> None:
            self._it = it

        def __iter__(self) -> Racy:
            return self

        def __next__(self) -> os.DirEntry[str]:
            entry: os.DirEntry[str] = next(self._it)  # type: ignore[call-overload]
            if entry.name == "gone.txt":
                os.unlink(root / "gone.txt")
            return entry

        def __enter__(self) -> Racy:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda fd: Racy(real_scandir(fd)))
    assert HostWorkspace(root).disk_usage_bytes() >= 8192  # keep.txt, and no exception


def test_an_unreadable_directory_is_skipped_not_fatal(root: Path) -> None:
    _put(root, "ok.txt", b"o" * 4096)
    (root / "locked").mkdir()
    _put(root, "locked/inner.txt", b"i" * 4096)
    os.chmod(root / "locked", 0o000)
    try:
        usage = HostWorkspace(root).disk_usage_bytes()
    finally:
        os.chmod(root / "locked", 0o755)
    assert usage >= 4096


def test_a_tree_deeper_than_the_walk_limit_cannot_be_measured(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_mod, "MAX_DEPTH", 3)
    _put(root, "a/b/c/d/e.txt")
    with pytest.raises(PathEscapeError, match="deeper"):
        HostWorkspace(root).disk_usage_bytes()


def test_a_directory_swapped_for_a_link_mid_walk_is_not_followed(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _put(outside, "huge.bin", os.urandom(4 * MIB))
    _put(root, "sub/a.txt", b"a" * 4096)
    real = workspace_mod._openat2
    swapped = False

    def swapping(dirfd: int, path: str, flags: int, mode: int = 0) -> int:
        nonlocal swapped
        if path == "sub" and not swapped:
            swapped = True
            os.rename(root / "sub", root / "sub_real")
            os.symlink(outside, root / "sub")
        return real(dirfd, path, flags, mode)

    ws = HostWorkspace(root)
    monkeypatch.setattr(workspace_mod, "_openat2", swapping)
    assert ws.disk_usage_bytes() < MIB  # the planted link was refused, not measured


# ---------------------------------------------------------------- the /tmp cap


def test_the_tmp_cap_is_one_gib_but_never_more_than_half_the_memory_limit() -> None:
    assert disk.tmp_cap_bytes(8) == 1024 * MIB
    assert disk.tmp_cap_bytes(2) == 1024 * MIB
    assert disk.tmp_cap_bytes(1) == 512 * MIB  # tmpfs pages are memory
    assert "size=536870912" in disk.tmpfs_options(1)
    options = disk.tmpfs_options(8).split(",")
    assert {"rw", "nosuid", "nodev", "mode=1777", f"size={1024 * MIB}"} <= set(options)


# ---------------------------------------------------------------- the runs/ filesystem check

MOUNTINFO = r"""
22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
30 22 0:25 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw
392 22 7:2 / /srv/mp/runs rw,nosuid,nodev,relatime - ext4 /dev/loop2 rw,discard
393 392 7:3 / /srv/mp/runs/nested rw,relatime - ext4 /dev/loop3 rw
401 22 8:17 / /mnt/data\040disk rw,relatime - ext4 /dev/sdb1 rw
"""


def _report(
    path: str, *, total_gib: float, disk_gb: int = 20, info: str = MOUNTINFO
) -> disk.RunsFilesystem:
    return disk.check_runs_filesystem(
        Path(path),
        disk_gb=disk_gb,
        mountinfo=info,
        total_bytes=int(total_gib * 1024 * MIB),
    )


def test_runs_on_a_loopback_filesystem_of_enough_size_is_fine() -> None:
    report = _report("/srv/mp/runs/run-1/workspace", total_gib=48.9)
    assert report.ok
    assert report.problems == ()
    assert (report.mount_point, report.source, report.fstype) == (
        "/srv/mp/runs",
        "/dev/loop2",
        "ext4",
    )
    assert report.loopback is True


def test_the_deepest_matching_mount_wins() -> None:
    report = _report("/srv/mp/runs/nested/x", total_gib=48.9)
    assert (report.mount_point, report.source) == ("/srv/mp/runs/nested", "/dev/loop3")


def test_runs_on_the_root_filesystem_is_a_problem_because_it_can_fill_the_hosts_disk() -> None:
    report = _report("/home/user/migrationpilot/runs", total_gib=500)
    assert not report.ok
    assert any("root filesystem" in p for p in report.problems)


def test_runs_on_a_real_partition_is_only_a_warning() -> None:
    report = _report("/mnt/data disk/runs", total_gib=100)
    assert report.ok
    assert report.loopback is False
    assert any("loopback" in w for w in report.warnings)
    assert report.mount_point == "/mnt/data disk"  # octal escapes in mountinfo are decoded


def test_runs_in_memory_is_a_problem() -> None:
    report = _report("/tmp/runs", total_gib=4)  # noqa: S108  (a path in the fake mountinfo)
    assert not report.ok
    assert any("tmpfs" in p for p in report.problems)


def test_a_filesystem_too_small_for_one_run_is_a_problem() -> None:
    report = _report("/srv/mp/runs/x", total_gib=10, disk_gb=20)
    assert not report.ok
    assert any("smaller" in p and "sandbox.disk_gb" in p for p in report.problems)


def test_a_missing_runs_directory_is_a_problem(tmp_path: Path) -> None:
    report = disk.check_runs_filesystem(tmp_path / "nope", disk_gb=20)
    assert not report.ok
    assert any("does not exist" in p for p in report.problems)


def test_the_real_runs_directory_of_this_host_passes() -> None:
    """The dev host's `runs/` is the loopback image `scripts/setup-dev.sh` creates."""
    runs = Path(__file__).resolve().parents[2] / "runs"
    report = disk.check_runs_filesystem(runs, disk_gb=20)
    assert report.ok, report.problems
    assert report.loopback is True
    assert report.total_bytes >= 20 * 1024 * MIB
