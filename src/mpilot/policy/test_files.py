"""Test-file classifier interface (SPEC §11.2, D2). The implementation is P2-15 (tests first).

The three predicates are independent, not exclusive classes: `tests/conftest.py` is both a
test file and a test config, and `package.json` embeds test config (`scripts.test`, `jest`)
without being one. Each predicate feeds different gate checks:
- `is_test_file` -> (c) deletions, (d) hunks only inside `test_touch` steps, baseline hashes
- `is_test_config` -> (e) the whole file is gated
- `may_embed_test_config` -> (e) only the test-config sections of the file are gated
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TestFileClassifier(Protocol):
    """Classifies workspace-relative POSIX paths. Pure: no file I/O.

    An implementation is built per run from host-owned data only: the D2 patterns, the
    jest/vitest `setupFiles` targets read from the base tree, and the files named in the
    baseline JUnit results. Never from anything the sandbox reports later.

    (No `__test__ = False` here: on a Protocol it would become a required member. Tests
    import this module rather than the name, so pytest never tries to collect it.)
    """

    def is_test_file(self, rel_path: str) -> bool:
        """D2 test-file patterns (`tests/**`, `test/**`, `testdata/**`, `test_*.py`,
        `*_test.py`, `*.test.*`, `*.spec.*`, `__tests__/**`, `src/test/**`), snapshots
        (`__snapshots__/**`, `*.snap`), jest/vitest setup files, and files the baseline
        JUnit results name."""
        ...

    def is_test_config(self, rel_path: str) -> bool:
        """Whole-file test configs (D2): `conftest.py`, jest/vitest configs including
        `vitest.workspace`, `pytest.ini`, `.mocharc*`, `.coveragerc`, nyc/c8 configs."""
        ...

    def may_embed_test_config(self, rel_path: str) -> bool:
        """Files with test-config sections (D2): `package.json` (`scripts.test`, `jest`),
        `pyproject.toml` (`[tool.pytest]`, coverage), `setup.cfg` (`[tool:pytest]`),
        `tox.ini` (`[pytest]`), and the Surefire / Gradle build files (excludes, filters).
        Any file may also carry `--cov-fail-under`; that is checked on content, in (e)."""
        ...
