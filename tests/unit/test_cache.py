"""P2-07b (SPEC §9.3, NFR-5): cache keys and the cache directories, without Docker.
The mounts, the read-only rule and the disk meter are in `test_cache_docker.py`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from mpilot.sandbox import cache as c
from mpilot.sandbox.base import sandbox_env_problems
from mpilot.sandbox.cache import CacheError, CacheRole, CacheStore, cache_key, cache_volume

LOCK = {"package-lock.json": b'{"lockfileVersion": 3}\n'}


# --- the key ----------------------------------------------------------------------------


def test_the_key_is_sha256_hex_and_stable() -> None:
    key = cache_key("node", LOCK)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert cache_key("node", dict(LOCK)) == key
    # The encoding is spelled out so that changing it (which orphans every cache on disk) is
    # noticed: SHA-256 over this canonical JSON, with each lockfile replaced by its SHA-256.
    content_hash = hashlib.sha256(LOCK["package-lock.json"]).hexdigest()
    canonical = '{"ecosystem":"node","lockfiles":{"package-lock.json":"' + content_hash + '"}}'
    assert key == hashlib.sha256(canonical.encode("ascii")).hexdigest()


def test_the_key_changes_with_the_content_of_any_lockfile() -> None:
    base = cache_key("node", LOCK)
    assert cache_key("node", {"package-lock.json": b'{"lockfileVersion": 2}\n'}) != base
    two = {**LOCK, "packages/a/package-lock.json": b"{}"}
    changed = {**LOCK, "packages/a/package-lock.json": b"{ }"}
    assert cache_key("node", two) != base
    assert cache_key("node", changed) != cache_key("node", two)


def test_a_single_changed_byte_changes_the_key() -> None:
    content = b"x" * 10_000
    flipped = content[:5000] + b"y" + content[5001:]
    assert cache_key("python", {"uv.lock": content}) != cache_key("python", {"uv.lock": flipped})


def test_the_key_depends_on_the_file_name_and_the_ecosystem() -> None:
    body = b"same bytes"
    assert cache_key("node", {"yarn.lock": body}) != cache_key("node", {"package-lock.json": body})
    assert cache_key("node", {"a.lock": body}) != cache_key("python", {"a.lock": body})


def test_the_key_does_not_depend_on_the_order_of_the_lockfiles() -> None:
    a = {"a.lock": b"1", "b.lock": b"2"}
    b = {"b.lock": b"2", "a.lock": b"1"}
    assert cache_key("node", a) == cache_key("node", b)


def test_content_cannot_be_shifted_between_lockfiles_to_collide() -> None:
    assert cache_key("node", {"a": b"xy", "b": b"z"}) != cache_key("node", {"a": b"x", "b": b"yz"})
    assert cache_key("node", {"a": b"", "b": b"z"}) != cache_key("node", {"a": b"z", "b": b""})


def test_non_ascii_names_are_fine() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", cache_key("node", {"päckage/yarn.lock": b"1"}))


def test_no_lockfiles_or_no_ecosystem_is_refused() -> None:
    with pytest.raises(CacheError, match="no lockfiles"):
        cache_key("node", {})
    with pytest.raises(CacheError, match="ecosystem"):
        cache_key("", LOCK)


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "A" * 64, "g" * 64, "../" + "a" * 61, "a" * 63 + "\n", "a" * 65, "a/" + "b" * 62],
)
def test_malformed_keys_are_refused(bad: str) -> None:
    with pytest.raises(CacheError, match="not a cache key"):
        c.check_key(bad)


# --- the environment and the mount ------------------------------------------------------


def test_every_cache_variable_points_under_the_mount() -> None:
    for name, value in c.CACHE_ENV.items():
        assert value.startswith("/cache/"), name
        assert ".." not in value


def test_the_cache_environment_passes_the_credential_floor() -> None:
    assert sandbox_env_problems(c.CACHE_ENV) == []
    assert not any("anthropic" in k.lower() or "haiku" in v.lower() for k, v in c.CACHE_ENV.items())


def test_every_package_manager_has_its_own_directory() -> None:
    dirs = [v for k, v in c.CACHE_ENV.items() if k != "XDG_CACHE_HOME"]
    assert len(set(dirs)) == len(dirs)


def test_the_work_container_mounts_read_only_and_the_install_container_read_write(
    tmp_path: Path,
) -> None:
    assert cache_volume(tmp_path, CacheRole.WORK) == {
        str(tmp_path): {"bind": "/cache", "mode": "ro"}
    }
    assert cache_volume(tmp_path, CacheRole.INSTALL) == {
        str(tmp_path): {"bind": "/cache", "mode": "rw"}
    }


# --- the store --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> CacheStore:
    return CacheStore(tmp_path / "cache")


def test_the_root_is_created_inside_an_existing_parent(tmp_path: Path) -> None:
    store = CacheStore(tmp_path / "cache")
    assert store.root.is_dir()
    CacheStore(tmp_path / "cache")  # again: fine


def test_the_root_must_be_absolute_a_real_directory_with_a_parent(tmp_path: Path) -> None:
    with pytest.raises(CacheError, match="absolute"):
        CacheStore(Path("cache"))
    with pytest.raises(CacheError, match="parent"):
        CacheStore(tmp_path / "missing" / "cache")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(CacheError, match="symlink"):
        CacheStore(tmp_path / "link")
    (tmp_path / "file").write_text("x")
    with pytest.raises(CacheError, match="not a directory"):
        CacheStore(tmp_path / "file")


def test_a_run_cache_is_one_directory_per_key_and_is_reused(store: CacheStore) -> None:
    key = cache_key("node", LOCK)
    path = store.run_dir(key)
    assert path == store.root / key
    assert path.is_dir()
    (path / "kept").write_text("x")
    assert store.run_dir(key) == path
    assert (path / "kept").read_text() == "x"
    assert store.run_dir(cache_key("node", {"package-lock.json": b"other"})) != path


def test_a_run_cache_for_a_bad_key_is_refused_and_creates_nothing(store: CacheStore) -> None:
    with pytest.raises(CacheError):
        store.run_dir("../escape")
    assert list(store.root.iterdir()) == []


def test_a_symlink_planted_where_a_run_cache_belongs_is_refused(
    store: CacheStore, tmp_path: Path
) -> None:
    key = cache_key("node", LOCK)
    (tmp_path / "victim").mkdir()
    (store.root / key).symlink_to(tmp_path / "victim")
    with pytest.raises(CacheError, match="symlink"):
        store.run_dir(key)


def test_fresh_caches_are_new_empty_and_distinct(store: CacheStore) -> None:
    a, b = store.fresh_dir("run-1"), store.fresh_dir("run-1")
    assert a != b
    for path in (a, b):
        assert path.parent == store.root / "fresh"
        assert path.name.startswith("run-1-")
        assert list(path.iterdir()) == []


def test_a_bad_run_id_is_refused(store: CacheStore) -> None:
    for bad in ("", "../x", "a/b", "a" * 65, "a b", "x\n"):
        with pytest.raises(CacheError, match="run id"):
            store.fresh_dir(bad)
        with pytest.raises(CacheError, match="run id"):
            store.sweep_fresh(bad)


def test_a_planted_fresh_directory_symlink_is_refused(store: CacheStore, tmp_path: Path) -> None:
    (tmp_path / "victim").mkdir()
    (store.root / "fresh").symlink_to(tmp_path / "victim")
    with pytest.raises(CacheError, match="symlink"):
        store.fresh_dir("run-1")
    assert list((tmp_path / "victim").iterdir()) == []


def test_removing_a_fresh_cache_deletes_the_tree_but_never_follows_links(
    store: CacheStore, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    path = store.fresh_dir("run-1")
    (path / "sub").mkdir()
    (path / "sub" / "f").write_text("x")
    (path / "link").symlink_to(outside)
    (path / "filelink").symlink_to(outside / "keep.txt")
    store.remove_fresh(path)
    assert not path.exists()
    assert (outside / "keep.txt").read_text() == "keep"


def test_removing_copes_with_read_only_directories_left_by_an_install(store: CacheStore) -> None:
    path = store.fresh_dir("run-1")
    deep = path / "mod" / "v1"
    deep.mkdir(parents=True)
    (deep / "f.go").write_text("x")
    deep.chmod(0o555)
    (path / "mod").chmod(0o555)
    try:
        store.remove_fresh(path)
    finally:
        for p in (path / "mod", deep):
            if p.exists():
                p.chmod(0o755)
    assert not path.exists()


def test_remove_refuses_everything_that_is_not_a_fresh_cache(
    store: CacheStore, tmp_path: Path
) -> None:
    run = store.run_dir(cache_key("node", LOCK))
    fresh = store.fresh_dir("run-1")
    for bad in (
        store.root,
        run,
        store.root / "fresh",
        store.root / "fresh" / "..",
        store.root / "fresh" / ".." / "fresh" / fresh.name,
        store.root / "fresh" / "not-a-fresh-name",
        tmp_path,
        tmp_path / ("x-" + "0" * 12),
    ):
        with pytest.raises(CacheError, match="not a fresh cache"):
            store.remove_fresh(bad)
    assert all(p.is_dir() for p in (run, fresh, store.root))


def test_remove_refuses_a_symlink_named_like_a_fresh_cache(
    store: CacheStore, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f").write_text("x")
    parent = store.root / "fresh"
    parent.mkdir()
    link = parent / ("run-1-" + "0" * 12)
    link.symlink_to(victim)
    with pytest.raises(CacheError, match="not a real directory"):
        store.remove_fresh(link)
    assert (victim / "f").exists()


def test_removing_a_fresh_cache_that_is_already_gone_is_fine(store: CacheStore) -> None:
    path = store.fresh_dir("run-1")
    store.remove_fresh(path)
    store.remove_fresh(path)


def test_sweep_removes_only_that_runs_fresh_caches(store: CacheStore) -> None:
    mine = [store.fresh_dir("run-a"), store.fresh_dir("run-a")]
    other = store.fresh_dir("run-a-b")  # another run whose id starts with this one's
    third = store.fresh_dir("run-c")
    run = store.run_dir(cache_key("node", LOCK))
    assert store.sweep_fresh("run-a") == 2
    assert not any(p.exists() for p in mine)
    assert all(p.is_dir() for p in (other, third, run))


def test_sweep_with_nothing_to_do(store: CacheStore) -> None:
    assert store.sweep_fresh("run-a") == 0  # no `fresh/` directory yet
    store.fresh_dir("run-b")
    assert store.sweep_fresh("run-a") == 0


def test_the_store_never_opens_a_file_of_a_cache() -> None:
    """The module only creates, stats and removes directories; reading happens through the
    contained walker (§9.5). A crude guard against a later `open()`/`read_*` slipping in."""
    source = Path(c.__file__).read_text()
    assert not re.search(
        r"\bopen\(|read_text|read_bytes|os\.walk|glob\(", source.replace("openat2", "")
    )


def test_a_file_where_a_run_cache_belongs_is_refused(store: CacheStore) -> None:
    key = cache_key("node", LOCK)
    (store.root / key).write_text("not a directory")
    with pytest.raises(CacheError, match="not a directory"):
        store.run_dir(key)


def test_the_install_container_gets_every_cache_variable_the_work_container_all_but_yarns() -> None:
    assert c.cache_env(CacheRole.INSTALL) == dict(c.CACHE_ENV)
    work = c.cache_env(CacheRole.WORK)
    assert set(c.CACHE_ENV) - set(work) == {"YARN_CACHE_FOLDER"}
    assert all(work[k] == c.CACHE_ENV[k] for k in work)
    assert work is not c.cache_env(CacheRole.WORK), "a copy: callers may add to it"
