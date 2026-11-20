"""Alembic environment. Driven in code by `mpilot.models.migrate`, not by an `alembic.ini`.

The caller passes either a live connection (`config.attributes["connection"]`, used by tests
and by the store so one transaction covers the migration) or a database URL
(`sqlalchemy.url`). With `--sql` style offline runs only the URL is needed.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from mpilot.models.tables import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url is not None and url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        url = config.get_main_option("sqlalchemy.url")
        if url is None:
            raise RuntimeError("no database url or connection given to the migration")
        engine = create_engine(url)
        with engine.connect() as conn:
            _run(conn)
        engine.dispose()
    else:
        _run(connection)


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite cannot ALTER most things; batch mode is a no-op elsewhere
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
