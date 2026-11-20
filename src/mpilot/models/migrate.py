"""Run Alembic migrations from code (P1-09), and small engine helpers.

There is no `alembic.ini`: the migration scripts ship inside the package
(`mpilot/models/migrations`) and the configuration is built here, so `mpilot` works the same
from a checkout and from an installed wheel.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str | None = None, *, connection: Connection | None = None) -> Config:
    """A config for the packaged migrations, against `url` or an open `connection`."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url is not None:
        # `%` is Alembic's interpolation character in option values.
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


def upgrade_connection(connection: Connection, revision: str = "head") -> None:
    command.upgrade(alembic_config(connection=connection), revision)


def downgrade_connection(connection: Connection, revision: str = "base") -> None:
    command.downgrade(alembic_config(connection=connection), revision)


def head_revision() -> str:
    """The single head of the migration history (more than one is an error)."""
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("the migration history is empty")
    return head


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def upgrade_sql(url: str, revision: str = "head") -> str:
    """The SQL an upgrade would run, for any dialect, without connecting (Alembic `--sql`)."""
    return _offline(command.upgrade, url, revision)


def downgrade_sql(url: str, from_revision: str, to_revision: str = "base") -> str:
    """The SQL a downgrade would run; offline downgrades need an explicit `from:to` range."""
    return _offline(command.downgrade, url, f"{from_revision}:{to_revision}")


def _offline(fn: Any, url: str, revision: str) -> str:
    buffer = io.StringIO()
    cfg = alembic_config(url)
    cfg.output_buffer = buffer
    fn(cfg, revision, sql=True)
    return buffer.getvalue()


def enable_sqlite_fks(engine: Engine) -> None:
    """Turn on `PRAGMA foreign_keys` for every connection of a SQLite engine (off by default)."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        if engine.dialect.name != "sqlite":
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
