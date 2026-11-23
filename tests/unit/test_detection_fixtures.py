"""P2-13 (SPEC FR-3, decisions D1): detection accuracy on 15 labelled fixtures.

The acceptance is that the detected test command is correct for at least 14 of the 15
(`MIN_CORRECT`). The labels live in `detection_fixtures.py`, were written before the detector
was run, and are compared as plain strings; nothing here reads a label from the detector.
Other fields (package manager, runtime, install, typecheck, build) are scored and reported too,
but only the test command is the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import pytest
from detection_fixtures import FIXTURES, Fixture

from mpilot.core.errors import InputError
from mpilot.detect.env import EnvDetection, detect_env
from mpilot.models.domain import Ecosystem
from mpilot.testing.doubles import DictTree

MIN_CORRECT: Final = 14
FIELDS: Final = (
    "test_cmd",
    "package_manager",
    "runtime_version",
    "install_cmd",
    "typecheck_cmd",
    "build_cmd",
)


@dataclass
class Outcome:
    fixture: Fixture
    detection: EnvDetection | None = None
    error: str | None = None
    wrong: dict[str, tuple[object, object]] = field(default_factory=dict)  # field -> (want, got)

    @property
    def test_cmd_correct(self) -> bool:
        return self.detection is not None and "test_cmd" not in self.wrong


def run(fixture: Fixture) -> Outcome:
    outcome = Outcome(fixture)
    tree = DictTree("a" * 40, fixture.files)
    try:
        outcome.detection = detect_env(
            tree,
            image_ref=lambda tag: f"{tag}@sha256:{'0' * 64}",
            ecosystem=fixture.ecosystem if fixture.ecosystem_hint else None,
        )
    except InputError as exc:
        outcome.error = str(exc)
        outcome.wrong["test_cmd"] = (fixture.label.test_cmd, None)
        return outcome
    profile = outcome.detection.profile
    for name in FIELDS:
        want = getattr(fixture.label, name)
        if want is None:
            continue
        got = getattr(profile, name)
        if got != want:
            outcome.wrong[name] = (want, got)
    return outcome


OUTCOMES = [run(f) for f in FIXTURES]


def test_there_are_fifteen_fixtures_eight_node_and_seven_python() -> None:
    assert len(FIXTURES) == 15
    assert sum(f.ecosystem is Ecosystem.NODE for f in FIXTURES) == 8
    assert sum(f.ecosystem is Ecosystem.PYTHON for f in FIXTURES) == 7
    assert len({f.name for f in FIXTURES}) == 15


def test_each_fixture_is_labelled_for_every_gate_field() -> None:
    for f in FIXTURES:
        assert f.label.test_cmd, f.name
        assert f.label.package_manager, f.name
        assert f.label.runtime_version, f.name


def test_the_fixtures_differ_from_each_other() -> None:
    assert len({(*sorted(f.files), f.label.test_cmd) for f in FIXTURES}) == 15


def summary() -> str:
    lines = []
    for o in OUTCOMES:
        status = "ok " if o.test_cmd_correct else "MISS"
        extra = f" error={o.error!r}" if o.error else ""
        lines.append(f"{status} {o.fixture.name}: wrong={o.wrong}{extra}")
    right = sum(o.test_cmd_correct for o in OUTCOMES)
    per_field = {
        name: sum(1 for o in OUTCOMES if o.detection and name not in o.wrong) for name in FIELDS
    }
    lines.append(f"test command correct: {right}/{len(OUTCOMES)}; fields right: {per_field}")
    return "\n".join(lines)


def test_the_detected_test_command_is_correct_for_at_least_14_of_15() -> None:
    right = sum(o.test_cmd_correct for o in OUTCOMES)
    assert right >= MIN_CORRECT, "\n" + summary()


def test_detection_is_deterministic() -> None:
    again = [run(f) for f in FIXTURES]
    assert [o.detection and o.detection.profile for o in again] == [
        o.detection and o.detection.profile for o in OUTCOMES
    ]


@pytest.mark.parametrize("outcome", OUTCOMES, ids=[o.fixture.name for o in OUTCOMES])
def test_a_detection_that_succeeds_names_a_source_for_every_field(outcome: Outcome) -> None:
    if outcome.detection is None:
        pytest.skip("no detection for this fixture; reported by the accuracy test")
    sources = outcome.detection.profile.detection_sources
    for name in ("runtime_version", "package_manager", "install_cmd", "test_cmd"):
        assert sources.get(name), name


#: The fixtures whose test command is not detected correctly, as measured on 2026-11-22.
#: `py-tox-only`: the project runs pytest only through tox, which the images do not carry. Since
#: P2-13b the detector no longer guesses `unittest discover` for it (which would have found no
#: tests, a silent zero-test run): it reports no test command (exit 2, with a message), and the
#: owner can set `[detect].test_cmd`. Reading tox.ini deps (option (a) in PROGRESS.md) would
#: fix it; when that is done this set becomes empty and this test is updated with it.
KNOWN_MISSES: Final = frozenset({"py-tox-only"})


def test_the_misses_are_exactly_the_known_ones() -> None:
    missed = {o.fixture.name for o in OUTCOMES if not o.test_cmd_correct}
    assert missed == KNOWN_MISSES, "\n" + summary()


def test_a_miss_is_a_refusal_never_a_wrong_command() -> None:
    """A wrong command is worse than none (the baseline would run nothing and pass), so every
    miss must be an error that says so, not a different command."""
    for o in OUTCOMES:
        if not o.test_cmd_correct:
            assert o.detection is None, o.fixture.name
            assert o.error is not None
            assert "no test command" in o.error, o.error
