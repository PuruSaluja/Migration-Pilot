"""P1-01: the E1 marker policy (docker fails without Docker, network opt-in, e2e deselected)."""

from __future__ import annotations

import pytest

PLUGIN = "mpilot.testing.policy"

SUITE = """
import pytest

@pytest.mark.docker
def test_docker():
    pass

@pytest.mark.network
def test_network():
    pass

@pytest.mark.e2e
def test_e2e():
    pass

def test_plain():
    pass
"""


def _run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *args: str, docker: bool, **env: str
) -> pytest.RunResult:
    for name in ("MPILOT_SKIP_DOCKER", "MPILOT_NETWORK_TESTS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    pytester.makeconftest(
        f"import mpilot.testing.policy as p\np.docker_available = lambda: {docker!r}\n"
    )
    pytester.makepyfile(SUITE)
    return pytester.runpytest_inprocess("-p", PLUGIN, "-p", "no:cacheprovider", *args)


def test_docker_test_fails_without_docker(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(pytester, monkeypatch, docker=False)
    result.assert_outcomes(passed=1, errors=1, skipped=1, deselected=1)
    result.stdout.fnmatch_lines(["*Docker is not available*"])


def test_docker_test_skips_only_with_explicit_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(pytester, monkeypatch, docker=False, MPILOT_SKIP_DOCKER="1")
    result.assert_outcomes(passed=1, skipped=2, deselected=1)


def test_docker_test_runs_with_docker(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(pytester, monkeypatch, docker=True)
    result.assert_outcomes(passed=2, skipped=1, deselected=1)


def test_network_tests_run_when_opted_in(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(pytester, monkeypatch, docker=True, MPILOT_NETWORK_TESTS="1")
    result.assert_outcomes(passed=3, deselected=1)


def test_e2e_runs_only_when_selected(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(pytester, monkeypatch, "-m", "e2e", docker=True)
    result.assert_outcomes(passed=1, deselected=3)
