"""Migration 039 preserves legacy rows and is safe to rerun after partial DDL."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = Path(__file__).resolve().parents[2] / "database/migrations/versions/039_archive_inv1_tables.py"
TABLES = (
    ("invasions", "_archive_inv1_invasions"),
    ("invasion_waves", "_archive_inv1_waves"),
    ("invasion_contributions", "_archive_inv1_contributions"),
    ("invasion_defense_actions", "_archive_inv1_defense_actions"),
    ("invasion_participants", "_archive_inv1_participants"),
)


def _run(engine, direction):
    spec = importlib.util.spec_from_file_location("migration_039", MIGRATION)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()


def test_039_renames_without_losing_rows_and_downgrades(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        for old, _ in TABLES:
            if old == "invasions":
                conn.execute(sa.text('CREATE TABLE invasions (id INTEGER PRIMARY KEY)'))
            else:
                conn.execute(sa.text(f'CREATE TABLE {old} (id INTEGER PRIMARY KEY, '
                                     'invasion_id INTEGER REFERENCES invasions(id))'))
        conn.execute(sa.text('CREATE TABLE inv2_invasions (id INTEGER PRIMARY KEY)'))
        conn.execute(sa.text('INSERT INTO invasions (id) VALUES (17)'))
        for old, _ in TABLES[1:]:
            conn.execute(sa.text(f'INSERT INTO {old} (id, invasion_id) VALUES (19, 17)'))
    for direction, check_archived in (("upgrade", True), ("upgrade", True),
                                       ("downgrade", False), ("downgrade", False)):
        _run(engine, direction)
        inspector = sa.inspect(engine)
        for old, archived in TABLES:
            table = archived if check_archived else old
            assert inspector.has_table(table)
            assert not inspector.has_table(old if check_archived else archived)
            with engine.connect() as conn:
                assert conn.scalar(sa.text(f'SELECT count(*) FROM {table}')) == 1
                if old != "invasions":
                    refs = inspector.get_foreign_keys(table)
                    assert refs[0]["referred_table"] == ("_archive_inv1_invasions" if check_archived else "invasions")
        columns = {c["name"] for c in inspector.get_columns("inv2_invasions")}
        assert ("delivered_at" in columns) is check_archived
    engine.dispose()
