"""Migration 022: legacy card stats -> Level 1 + derived attributes."""

import importlib.util

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.tests.test_migrations import REPO_ROOT, _run_alembic

MIGRATION_PATH = (
    REPO_ROOT / "database" / "migrations" / "versions" / "022_card_upgrade_levels.py"
)
STAT_COLUMNS = (
    "power", "speed", "intelligence",
    "boosted_power", "boosted_speed", "boosted_intelligence",
)


def _load():
    spec = importlib.util.spec_from_file_location("card_levels_022", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(engine):
    md = sa.MetaData()
    sa.Table(
        "characters", md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("power", sa.Integer, nullable=False),
        sa.Column("speed", sa.Integer, nullable=False),
        sa.Column("intelligence", sa.Integer, nullable=False),
    )
    sa.Table(
        "player_cards", md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("character_id", sa.Integer, nullable=False),
        sa.Column("level", sa.Integer, nullable=False, server_default="1"),
        sa.Column("power", sa.Integer, nullable=False, server_default="20"),
        sa.Column("speed", sa.Integer, nullable=False, server_default="20"),
        sa.Column("intelligence", sa.Integer, nullable=False, server_default="20"),
        sa.Column("boosted_power", sa.Integer, nullable=False, server_default="0"),
        sa.Column("boosted_speed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("boosted_intelligence", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_favorite", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    sa.Table(
        "apple_battle_queue", md,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("card_id", sa.Integer, nullable=False),
        sa.Column("level", sa.Integer, nullable=False),
    )
    md.create_all(engine)


def _seed(connection):
    connection.execute(
        sa.text("INSERT INTO characters VALUES (1, 22, 20, 18), (2, 14, 16, 30)")
    )
    connection.execute(
        sa.text(
            "INSERT INTO player_cards "
            "(id, user_id, character_id, level, power, speed, intelligence, "
            " boosted_power, boosted_speed, boosted_intelligence, is_favorite) VALUES "
            "(10, 1, 1, 4, 50, 40, 30, 28, 20, 12, 1),"
            "(11, 2, 2, 1, 14, 16, 30, 0, 0, 0, 0),"
            "(12, 1, 2, 7, 99, 60, 81, 85, 44, 51, 0)"
        )
    )
    connection.execute(sa.text("INSERT INTO apple_battle_queue VALUES (1, 10, 4)"))


def _run(connection, fn):
    with Operations.context(MigrationContext.configure(connection)):
        fn()


def test_upgrade_archives_legacy_data_resets_levels_and_keeps_identity():
    migration = _load()
    engine = sa.create_engine("sqlite://")
    _legacy_schema(engine)
    with engine.begin() as connection:
        _seed(connection)
        _run(connection, migration.upgrade)

        inspector = sa.inspect(connection)
        columns = {c["name"] for c in inspector.get_columns("player_cards")}
        assert columns.isdisjoint(STAT_COLUMNS)
        assert {"id", "user_id", "character_id", "level", "is_favorite"} <= columns

        cards = connection.execute(
            sa.text("SELECT id, user_id, character_id, level, is_favorite FROM player_cards ORDER BY id")
        ).all()
        # Ownership + identity + favorite intact; everything is Level 1.
        assert [tuple(r) for r in cards] == [
            (10, 1, 1, 1, 1), (11, 2, 2, 1, 0), (12, 1, 2, 1, 0),
        ]
        # Base stats (shared definitions) untouched.
        assert [tuple(r) for r in connection.execute(
            sa.text("SELECT id, power, speed, intelligence FROM characters ORDER BY id")
        )] == [(1, 22, 20, 18), (2, 14, 16, 30)]
        # Loadout mirrors the reset.
        assert connection.execute(sa.text("SELECT level FROM apple_battle_queue")).scalar() == 1

        archive = connection.execute(
            sa.text(
                "SELECT card_id, user_id, character_id, level, power, speed, intelligence, "
                "boosted_power, boosted_speed, boosted_intelligence "
                "FROM player_card_legacy_stats ORDER BY card_id"
            )
        ).all()
        # Nothing silently overwritten: the old values are all archived.
        assert [tuple(r) for r in archive] == [
            (10, 1, 1, 4, 50, 40, 30, 28, 20, 12),
            (11, 2, 2, 1, 14, 16, 30, 0, 0, 0),
            (12, 1, 2, 7, 99, 60, 81, 85, 44, 51),
        ]


def test_downgrade_restores_archived_stats_and_levels():
    migration = _load()
    engine = sa.create_engine("sqlite://")
    _legacy_schema(engine)
    with engine.begin() as connection:
        _seed(connection)
        _run(connection, migration.upgrade)
        # A card acquired AFTER the migration has no archive row.
        connection.execute(sa.text("INSERT INTO player_cards (id, user_id, character_id, level) VALUES (13, 3, 1, 1)"))
        _run(connection, migration.downgrade)

        assert not sa.inspect(connection).has_table("player_card_legacy_stats")
        rows = {
            r[0]: tuple(r[1:])
            for r in connection.execute(
                sa.text(
                    "SELECT id, level, power, speed, intelligence, boosted_power, "
                    "boosted_speed, boosted_intelligence FROM player_cards"
                )
            )
        }
    assert rows[10] == (4, 50, 40, 30, 28, 20, 12)
    assert rows[12] == (7, 99, 60, 81, 85, 44, 51)
    assert rows[11] == (1, 14, 16, 30, 0, 0, 0)
    # New card falls back to its character's base stats, nothing boosted.
    assert rows[13] == (1, 22, 20, 18, 0, 0, 0)


def test_upgrade_on_empty_tables_does_not_fail():
    migration = _load()
    engine = sa.create_engine("sqlite://")
    _legacy_schema(engine)
    with engine.begin() as connection:
        _run(connection, migration.upgrade)  # no rows: must not fail
        assert connection.execute(sa.text("SELECT COUNT(*) FROM player_cards")).scalar() == 0


def test_revision_chain_still_has_a_single_head_at_022_or_later(tmp_path):
    result = _run_alembic("heads", db_url=f"sqlite:///{tmp_path / 'h.db'}")
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1
    migration = _load()
    assert migration.down_revision == "021_bot_fallback_matchmaking"
    assert len(migration.revision) <= 32
