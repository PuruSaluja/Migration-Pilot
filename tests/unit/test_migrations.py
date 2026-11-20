"""P1-09: the Alembic initial migration upgrades, downgrades, matches `tables.py`, and its SQL
is valid for Postgres as well as SQLite."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from mpilot.models.migrate import (
    MIGRATIONS_DIR,
    alembic_config,
    current_revision,
    downgrade,
    downgrade_connection,
    downgrade_sql,
    head_revision,
    upgrade,
    upgrade_connection,
    upgrade_sql,
)
from mpilot.models.tables import Base

POSTGRES = "postgresql://mpilot@localhost/mpilot"
TABLES = set(Base.metadata.tables)


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'temp.db'}"


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_there_is_exactly_one_head_and_it_is_the_initial_revision() -> None:
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == ["0001"]
    assert head_revision() == "0001"
    assert script.get_revision("0001").down_revision is None


def test_the_migrations_ship_inside_the_package() -> None:
    assert MIGRATIONS_DIR.is_dir()
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert (MIGRATIONS_DIR / "versions" / "0001_initial_schema.py").is_file()


def test_upgrade_creates_every_table(url: str) -> None:
    upgrade(url)
    assert _tables(url) == TABLES | {"alembic_version"}
    engine = create_engine(url)
    with engine.connect() as conn:
        assert current_revision(conn) == head_revision()
    engine.dispose()


def test_downgrade_removes_every_table(url: str) -> None:
    upgrade(url)
    downgrade(url)
    assert _tables(url) == {"alembic_version"}
    engine = create_engine(url)
    with engine.connect() as conn:
        assert current_revision(conn) is None
    engine.dispose()


def test_upgrade_downgrade_upgrade_again(url: str) -> None:
    upgrade(url)
    downgrade(url)
    upgrade(url)
    assert _tables(url) == TABLES | {"alembic_version"}


def test_upgrading_twice_is_a_no_op(url: str) -> None:
    upgrade(url)
    upgrade(url)
    assert _tables(url) == TABLES | {"alembic_version"}


def test_the_same_steps_work_on_an_open_connection(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'conn.db'}")
    with engine.begin() as conn:
        upgrade_connection(conn)
    assert set(inspect(engine).get_table_names()) == TABLES | {"alembic_version"}
    with engine.begin() as conn:
        downgrade_connection(conn)
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()


def test_the_migrated_schema_matches_tables_py(url: str) -> None:
    """Fails when `tables.py` changes without a new revision."""
    upgrade(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    engine.dispose()
    assert diff == []


def test_the_migration_creates_the_check_constraints(url: str) -> None:
    upgrade(url)
    engine = create_engine(url)
    checks = {
        (table, c["name"]) for table in TABLES for c in inspect(engine).get_check_constraints(table)
    }
    engine.dispose()
    expected = {
        (table.name, c.name)
        for table in Base.metadata.tables.values()
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }
    assert checks == expected
    assert ("llm_calls", "ck_llm_calls_opus_never_medium") in checks
    assert ("llm_calls", "ck_llm_calls_no_haiku") in checks


def test_the_migration_creates_the_indexes_and_foreign_keys(url: str) -> None:
    upgrade(url)
    engine = create_engine(url)
    insp = inspect(engine)
    for table in Base.metadata.tables.values():
        assert {i["name"] for i in insp.get_indexes(table.name)} >= {
            i.name for i in table.indexes
        }, table.name
        assert {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in insp.get_foreign_keys(table.name)
        } == {
            (tuple(c.name for c in fk.columns), fk.referred_table.name)
            for fk in table.foreign_key_constraints
        }, table.name
    engine.dispose()


def test_percent_signs_in_a_url_survive_alembics_option_interpolation(tmp_path: Path) -> None:
    folder = tmp_path / "a%20b"  # a real `%` in the path: `%2520` in the URL
    folder.mkdir()
    upgrade(f"sqlite:///{tmp_path}/a%2520b/x.db")
    assert (folder / "x.db").is_file()


# ---------------------------------------------------------------- Postgres, without a server


def test_the_upgrade_is_valid_postgres_sql_for_every_table() -> None:
    sql = upgrade_sql(POSTGRES)
    for table in TABLES:
        assert f"CREATE TABLE {table} (" in sql, table
    assert "JSONB" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "ck_llm_calls_opus_never_medium" in sql
    assert "INSERT INTO alembic_version" in sql
    assert "'0001'" in sql


def test_the_downgrade_drops_every_table_on_postgres() -> None:
    sql = downgrade_sql(POSTGRES, "0001")
    for table in TABLES:
        assert f"DROP TABLE {table}" in sql, table
    assert "DROP TABLE alembic_version" not in sql  # Alembic keeps its own table


def test_the_sqlite_script_and_the_postgres_script_create_the_same_tables() -> None:
    names = {
        line.split()[2]
        for dialect_sql in (upgrade_sql(POSTGRES), upgrade_sql("sqlite://"))
        for line in dialect_sql.splitlines()
        if line.startswith("CREATE TABLE") and "alembic_version" not in line
    }
    assert names == TABLES


def test_postgres_sql_has_no_sqlite_isms() -> None:
    sql = upgrade_sql(POSTGRES)
    assert "AUTOINCREMENT" not in sql
    assert "PRAGMA" not in sql
    assert "SERIAL" in sql or "GENERATED" in sql  # the `seq` columns auto-number


#: SHA-256 of `0001_initial_schema.py`, frozen at the Phase 1 gate (P1-12d). Editing a
#: shipped revision would make existing databases disagree with new ones without any
#: migration running; change the schema with a new revision instead.
FROZEN_0001_SHA256 = "7e8c71926731af72dc8e51677154b18d82d13f98ad26aa3a31440c69b77b32b8"


def test_the_initial_revision_is_frozen() -> None:
    import mpilot.models.migrations.versions as versions

    path = Path(versions.__file__).parent / "0001_initial_schema.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == FROZEN_0001_SHA256, (
        "0001_initial_schema.py changed after the Phase 1 gate: add a new revision instead"
    )
