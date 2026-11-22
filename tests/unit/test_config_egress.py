"""P2-08a: `sandbox.egress_allowlist` (SPEC §9.3 Network)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mpilot.config import (
    DEFAULT_EGRESS_ALLOWLIST,
    Config,
    SandboxSettings,
    config_hash,
    load_config,
)
from mpilot.core.errors import ConfigError

# §9.3: "npm, PyPI, files.pythonhosted.org, Maven Central, Gradle plugins and GitHub release
# tarballs". One name per family at least; nothing else.
FAMILIES = {
    "npm": {"registry.npmjs.org", "registry.yarnpkg.com"},
    "pypi": {"pypi.org", "files.pythonhosted.org"},
    "maven": {"repo.maven.apache.org", "repo1.maven.org"},
    "gradle": {"plugins.gradle.org", "plugins-artifacts.gradle.org"},
    "github": {
        "github.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    },
}


def test_the_default_is_exactly_the_spec_families() -> None:
    assert set(DEFAULT_EGRESS_ALLOWLIST) == set().union(*FAMILIES.values())
    assert list(DEFAULT_EGRESS_ALLOWLIST) == Config().sandbox.egress_allowlist


def test_no_wildcards_no_addresses_no_general_hosts_in_the_default() -> None:
    for host in DEFAULT_EGRESS_ALLOWLIST:
        assert "*" not in host
        assert not host.replace(".", "").isdigit()
    for never in ("example.com", "githubusercontent.com", "gist.github.com", "api.github.com"):
        assert never not in DEFAULT_EGRESS_ALLOWLIST


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([], "empty"),
        (["Registry.npmjs.org"], "must be written as"),
        (["registry.npmjs.org."], "must be written as"),
        (["*.npmjs.org"], "invalid_host"),
        (["1.1.1.1"], "ip_literal"),
        (["[::1]"], "ip_literal"),
        (["2130706433"], "ip_literal"),
        (["localhost"], "invalid_host"),
        (["pypi.org:443"], "invalid_host"),
        (["pypi.org", "pypi.org"], "twice"),
        (["pypi.org/simple"], "invalid_host"),
    ],
)
def test_bad_allowlists_are_refused(entries: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SandboxSettings(egress_allowlist=entries)


def test_the_allowlist_can_be_narrowed_from_a_file_and_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "mpilot.toml"
    path.write_text('[sandbox]\negress_allowlist = ["pypi.org", "files.pythonhosted.org"]\n')
    assert load_config(config_file=path, environ={}).sandbox.egress_allowlist == [
        "pypi.org",
        "files.pythonhosted.org",
    ]
    env = {"MPILOT_SANDBOX__EGRESS_ALLOWLIST": "registry.npmjs.org"}
    assert load_config(environ=env).sandbox.egress_allowlist == ["registry.npmjs.org"]


@pytest.mark.parametrize(
    "extra", ["example.com", "nodejs.org", "services.gradle.org", "api.github.com"]
)
def test_the_allowlist_can_only_be_narrowed_never_widened(extra: str) -> None:
    with pytest.raises(ValueError, match="only narrow"):
        SandboxSettings(egress_allowlist=[*DEFAULT_EGRESS_ALLOWLIST, extra])
    with pytest.raises(ConfigError):
        load_config(environ={"MPILOT_SANDBOX__EGRESS_ALLOWLIST": f"pypi.org,{extra}"})


def test_the_spec_lists_the_same_hosts_as_the_default() -> None:
    """SPEC §9.3 spells the list out; the default must not drift from it."""
    spec = (Path(__file__).parents[2] / "SPEC.md").read_text()
    line = next(
        x for x in spec.splitlines() if "sandbox.egress_allowlist" in x and "exact hosts" in x
    )
    named = set(re.findall(r"`([a-z0-9.-]+\.[a-z]+)`", line))
    assert named == set(DEFAULT_EGRESS_ALLOWLIST)


def test_a_bad_entry_in_the_environment_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        load_config(environ={"MPILOT_SANDBOX__EGRESS_ALLOWLIST": "evil.example,1.2.3.4"})


def test_the_allowlist_is_part_of_the_config_hash() -> None:
    narrowed = Config(sandbox=SandboxSettings(egress_allowlist=["pypi.org"]))
    assert config_hash(narrowed) != config_hash(Config())
