"""P1-01: logs are valid JSON lines."""

from __future__ import annotations

import io
import json
import logging

import pytest

from mpilot.core.log import configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logger() -> object:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_each_record_is_one_json_line() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    log = logging.getLogger("mpilot.test")
    log.info("hello %s", "world", extra={"run_id": "r1", "n": 3})
    log.debug("filtered out")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["message"] == "hello world"
    assert first["level"] == "INFO"
    assert first["logger"] == "mpilot.test"
    assert first["run_id"] == "r1"
    assert first["n"] == 3
    assert first["ts"].endswith("+00:00")
    assert "ValueError: boom" in second["exc_info"]
    assert "\n" not in lines[1]


def test_unserializable_extra_does_not_break_logging() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("mpilot.test").info("x", extra={"obj": object()})
    assert json.loads(stream.getvalue())["message"] == "x"


def test_configure_is_idempotent() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    configure_logging("INFO", stream)
    logging.getLogger("mpilot.test").info("once")
    assert len(stream.getvalue().splitlines()) == 1
