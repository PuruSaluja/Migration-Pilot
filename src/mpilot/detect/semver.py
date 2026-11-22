"""The node-semver subset that `engines.node` uses (SPEC §9.4: runtime version).

Supported: comparators (`>=18`, `<20`, `=20.1.0`), bare and x-ranges (`20`, `20.x`, `20.*`, `*`),
caret and tilde (`^18.17.0`, `~18.17`), hyphen ranges (`16 - 20`), AND by space or comma, OR by
`||`. Not supported, and refused with `RangeSyntaxError` rather than guessed at: pre-release
ranges, dist-tags and aliases (`lts/*`, `latest`), and anything else that is not this grammar.
An empty range or `*` matches everything. Pre-release suffixes on the *version* being tested
are dropped.
"""

from __future__ import annotations

import re
from typing import Final

Version = tuple[int, int, int]
_Bound = tuple[Version, bool, Version | None, bool]  # lo, lo_inclusive, hi, hi_inclusive

MAX_RANGE_CHARS: Final = 500
_VERSION: Final = re.compile(r"v?(\d{1,9})\.(\d{1,9})\.(\d{1,9})(?:[-+].*)?")
_PART: Final = r"(?:\d{1,9}|[xX*])"
_TOKEN: Final = re.compile(rf"(>=|<=|>|<|=|\^|~)?v?({_PART})(?:\.({_PART}))?(?:\.({_PART}))?")
_HYPHEN: Final = re.compile(r"^(\S+)\s+-\s+(\S+)$")


class RangeSyntaxError(ValueError):
    """A range this module does not support."""


def parse_version(text: str) -> Version:
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a full version (expected MAJOR.MINOR.PATCH): {text!r}")
    return int(match[1]), int(match[2]), int(match[3])


def _wild(part: str | None) -> bool:
    return part is None or part in ("x", "X", "*")


def _parts(m: re.Match[str]) -> tuple[int | None, int | None, int | None]:
    values = [None if _wild(m[i]) else int(m[i]) for i in (2, 3, 4)]
    # a wildcard hides everything after it: `20.x.3` is `20.x`
    for i, v in enumerate(values):
        if v is None:
            values[i:] = [None] * (3 - i)
            break
    return values[0], values[1], values[2]


def _any() -> list[_Bound]:
    return [((0, 0, 0), True, None, False)]


def _partial_range(
    major: int | None, minor: int | None, patch: int | None
) -> tuple[Version, Version | None]:
    """The [lo, hi) range a bare or x-range version stands for."""
    if major is None:
        return (0, 0, 0), None
    if minor is None:
        return (major, 0, 0), (major + 1, 0, 0)
    if patch is None:
        return (major, minor, 0), (major, minor + 1, 0)
    return (major, minor, patch), (major, minor, patch + 1)


def _caret(
    major: int | None, minor: int | None, patch: int | None
) -> tuple[Version, Version | None]:
    if major is None:
        return (0, 0, 0), None
    lo = (major, minor or 0, patch or 0)
    if major > 0:
        return lo, (major + 1, 0, 0)
    if minor is None:
        return lo, (1, 0, 0)
    if minor > 0:
        return lo, (0, minor + 1, 0)
    if patch is None:
        return lo, (0, 1, 0)
    return lo, (0, 0, patch + 1)


def _tilde(
    major: int | None, minor: int | None, patch: int | None
) -> tuple[Version, Version | None]:
    if major is None:
        return (0, 0, 0), None
    lo = (major, minor or 0, patch or 0)
    if minor is None:
        return lo, (major + 1, 0, 0)
    return lo, (major, minor + 1, 0)


def _comparator(token: str) -> _Bound:
    if "-" in token.lstrip("<>=^~v") or "+" in token:
        raise RangeSyntaxError(f"pre-release and build ranges are not supported: {token!r}")
    m = _TOKEN.fullmatch(token)
    if m is None:
        raise RangeSyntaxError(f"unsupported range syntax: {token!r}")
    op = m[1] or ""
    major, minor, patch = _parts(m)
    if op in ("", "="):
        lo, hi = _partial_range(major, minor, patch)
        return lo, True, hi, False
    if op == "^":
        lo, hi = _caret(major, minor, patch)
        return lo, True, hi, False
    if op == "~":
        lo, hi = _tilde(major, minor, patch)
        return lo, True, hi, False
    if major is None:
        raise RangeSyntaxError(f"a comparator needs a version: {token!r}")
    lo, hi = _partial_range(major, minor, patch)
    full = patch is not None
    if op == ">=":
        return lo, True, None, False
    if op == ">":
        return (lo, False, None, False) if full else (hi or lo, True, None, False)
    if op == "<":
        return (0, 0, 0), True, lo, False
    # "<="
    return ((0, 0, 0), True, lo, True) if full else ((0, 0, 0), True, hi, False)


def _hyphen(low: str, high: str) -> list[_Bound]:
    lo_m, hi_m = _TOKEN.fullmatch(low), _TOKEN.fullmatch(high)
    if lo_m is None or hi_m is None or low[0] in "<>=^~" or high[0] in "<>=^~":
        raise RangeSyntaxError(f"unsupported hyphen range: {low} - {high}")
    lo, _ = _partial_range(*_parts(lo_m))
    h_major, h_minor, h_patch = _parts(hi_m)
    h_lo, h_hi = _partial_range(h_major, h_minor, h_patch)
    if h_patch is not None:
        return [(lo, True, h_lo, True)]
    return [(lo, True, h_hi, False)]


def _alternative(text: str) -> list[_Bound]:
    """One `||`-free part: comparators ANDed (here as a list that must all hold)."""
    hyphen = _HYPHEN.match(text.strip())
    if hyphen is not None:
        return _hyphen(hyphen[1], hyphen[2])
    squeezed = re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", text.replace(",", " "))
    tokens = squeezed.split()
    if not tokens:
        raise RangeSyntaxError("empty alternative in the range")
    return [_comparator(t) for t in tokens]


def _holds(version: Version, bound: _Bound) -> bool:
    lo, lo_inclusive, hi, hi_inclusive = bound
    if version < lo or (version == lo and not lo_inclusive):
        return False
    if hi is None:
        return True
    return version <= hi if hi_inclusive else version < hi


def satisfies(version: str, range_: str) -> bool:
    """Whether `version` (MAJOR.MINOR.PATCH) is in `range_`. Raises `RangeSyntaxError` for a
    range outside the supported grammar and `ValueError` for a bad version."""
    parsed = parse_version(version)
    if len(range_) > MAX_RANGE_CHARS:
        raise RangeSyntaxError("range too long")
    if range_.strip() in ("", "*", "x", "X"):
        return True
    alternatives = [_alternative(part) for part in range_.split("||")]
    return any(all(_holds(parsed, b) for b in alt) for alt in alternatives)
