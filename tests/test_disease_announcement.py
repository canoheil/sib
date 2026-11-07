"""Daily Disease & Doctor group announcement (migration 079).

Covers:

* migration ``079_disease_daily_announcement`` (revises 078, seeds the three
  announcement config keys, keeps operator overrides on downgrade);
* the once-per-Tehran-day broadcast: sent inside the configured window only,
  claimed durably in ``system_config.last_disease_announcement_date`` so a
  restart or a second maintenance tick cannot repost the same day;
* the message itself: doctor mention, up to three sick mentions followed by
  «و X نفر دیگر», and the safe HTML rendering the group actually receives.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from sib.core.text import format_html
from sib.database.models import (
    Group,
    GroupDoctorState,
    Inv2GroupMember,
    SystemConfig,
    User,
    UserDisease,
)
from sib.services.config_store import CONFIG_DEFAULTS
from sib.services.disease_announcement import (
    ANNOUNCEMENT_STATE_KEY,
    format_disease_announcement,
    run_disease_announcement,
)
from sib.handlers import disease as disease_handler

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/migrations/versions/079_disease_daily_announcement.py"
TEHRAN = ZoneInfo("Asia/Tehran")
EXPECTED_SEED = {
    "disease_announcement_hour": "18",
    "disease_announcement_minute": "15",
    "disease_announcement_enabled": "true",
}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _migration():
    spec = importlib.util.spec_from_file_location("migration_079", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(engine, direction):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(_migration(), direction)()


def _config_rows(engine):
    with engine.connect() as connection:
        return dict(
            connection.execute(sa.text("SELECT `key`, `value` FROM system_config")).all()
        )


def _foreign_keys_on(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture
def migration_engine():
    engine = sa.create_engine("sqlite:///:memory:")
    sa.event.listen(engine, "connect", _foreign_keys_on)
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE system_config (`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)")
        )
    yield engine
    engine.dispose()


def test_migration_079_revises_078_seeds_config_and_is_the_head():
    migration = _migration()
    assert migration.revision == "079_disease_daily_announcement"
    assert migration.down_revision == "078_wanted_decay"
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["081_card_rarity"]
    assert (
        scripts.get_revision("079_disease_daily_announcement").down_revision
        == "078_wanted_decay"
    )
    assert (
        scripts.get_revision("080_group_doctors").down_revision
        == "079_disease_daily_announcement"
    )
    # The code defaults and the seeded values agree.
    assert CONFIG_DEFAULTS["disease_announcement_enabled"] is True
    assert CONFIG_DEFAULTS["disease_announcement_hour"] == 18
    assert CONFIG_DEFAULTS["disease_announcement_minute"] == 15
    assert migration._CONFIG_DEFAULTS == EXPECTED_SEED


def test_migration_079_upgrade_seeds_and_downgrade_preserves_overrides(migration_engine):
    engine = migration_engine
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES "
                "('disease_announcement_hour', '21'), ('unrelated', 'keep')"
            )
        )

    _run_migration(engine, "upgrade")
    _run_migration(engine, "upgrade")  # idempotent
    # An operator override wins over the seed ...
    assert _config_rows(engine) == {
        **EXPECTED_SEED,
        "disease_announcement_hour": "21",
        "unrelated": "keep",
    }
    _run_migration(engine, "downgrade")
    _run_migration(engine, "downgrade")  # idempotent
    # ... and survives the downgrade (only an untouched default is removed).
    assert _config_rows(engine) == {"disease_announcement_hour": "21", "unrelated": "keep"}

    # Without overrides the defaults are seeded, and downgrade removes them.
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM system_config"))
    _run_migration(engine, "upgrade")
    assert _config_rows(engine) == EXPECTED_SEED
    _run_migration(engine, "downgrade")
    assert _config_rows(engine) == {}


def test_migration_079_mysql_sql_is_non_destructive():
    migration = _migration()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "output_buffer": output, "literal_binds": True},
    )
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    assert "ON DUPLICATE KEY UPDATE `key` = `key`" in sql
    assert "BINARY `key` = BINARY" in sql
    for key in EXPECTED_SEED:
        assert key in sql


def test_alembic_real_upgrade_and_downgrade_from_078(tmp_path):
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["081_card_rarity"]
    database = tmp_path / "announcement_migration.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id BIGINT PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE characters (code VARCHAR(64) PRIMARY KEY, rarity VARCHAR(20) NOT NULL DEFAULT 'COMMON')"))
        connection.execute(sa.text("CREATE TABLE shop_rotations (is_active BOOLEAN NOT NULL)"))
        connection.execute(
            sa.text("CREATE TABLE system_config (`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)")
        )
    engine.dispose()
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = f"sqlite:///{database}"
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT.parent), str(ROOT), env.get("PYTHONPATH", "")))

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    assert alembic("heads") == "081_card_rarity (head)"
    alembic("stamp", "078_wanted_decay")
    alembic("upgrade", "head")
    assert "081_card_rarity" in alembic("current")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert sa.inspect(engine).has_table("group_doctor_states")
    with engine.connect() as connection:
        assert _config_rows(engine) == EXPECTED_SEED
    engine.dispose()
    alembic("downgrade", "078_wanted_decay")
    engine = sa.create_engine(f"sqlite:///{database}")
    assert not sa.inspect(engine).has_table("group_doctor_states")
    with engine.connect() as connection:
        assert _config_rows(engine) == {}
    engine.dispose()
    alembic("upgrade", "head")


# ---------------------------------------------------------------------------
# Message formatting (pure)
# ---------------------------------------------------------------------------


def _mention(name: str, user_id: int) -> str:
    # Display names are fenced with LRM (see core.bidi.wrap_ltr).
    return f"[\u200e{name}\u200e](tg://user?id={user_id})"


def test_format_announcement_lists_doctor_and_sick_users():
    text = format_disease_announcement(
        (7, "دکتر شاپور"),
        [(11, "علی"), (12, "Sara")],
    )
    lines = text.splitlines()
    assert lines[0] == f"🩺 **پزشک امروز:** {_mention('دکتر شاپور', 7)}"
    assert lines[1] == (
        f"🦠 **بیماران امروز:** {_mention('علی', 11)}، {_mention('Sara', 12)}"
    )
    assert lines[2] == "⚠️ برای درمان، دستور /disease را بزنید یا منتظر دکتر بمانید!"


def test_format_announcement_lists_up_to_three_then_counts_the_rest():
    sick = [(uid, f"بازیکن {uid}") for uid in (101, 102, 103, 104, 105)]
    text = format_disease_announcement((7, "دکتر"), sick)
    sick_line = text.splitlines()[1]
    assert _mention("بازیکن 101", 101) in sick_line
    assert _mention("بازیکن 102", 102) in sick_line
    assert _mention("بازیکن 103", 103) in sick_line
    assert "104" not in sick_line and "105" not in sick_line
    assert "و ۲ نفر دیگر" in sick_line


def test_format_announcement_without_doctor_and_without_sick_users():
    text = format_disease_announcement(None, [])
    assert "🩺 **پزشک امروز:** هنوز انتخاب نشده است." in text
    assert "🦠 **بیماران امروز:** هیچ بیماری در این گروه نیست." in text
    assert "/disease" in text


def test_format_announcement_renders_safe_html_with_mentions_and_bold():
    text = format_disease_announcement(
        (7, "دکتر <b>شاپور</b>"),
        [(11, "علی & پسران")],
    )
    html = format_html(text)
    # Bold markers become <b>, mentions become real links, and the raw HTML
    # typed by players is stripped/escaped away before sending.
    assert "<b>پزشک امروز:</b>" in html
    assert "<b>بیماران امروز:</b>" in html
    assert '<a href="tg://user?id=7">' in html
    assert '<a href="tg://user?id=11">' in html
    assert "<b>شاپور</b>" not in html  # a tag typed in a name never survives
    assert "&amp;" in html
    assert "**" not in html


# ---------------------------------------------------------------------------
# Broadcast timing and the once-per-day claim
# ---------------------------------------------------------------------------


def _tehran(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 6, hour, minute, tzinfo=TEHRAN)


async def _seed_group(db_session, *, group_id=-1007001, active=True):
    db_session.add(Group(id=group_id, title="گروه بیماری", is_active=active))
    await db_session.flush()


async def _seed_user(db_session, user_id, name, *, group_id=None, sick=False):
    now = _tehran(12)
    existing = await db_session.get(User, user_id)
    if existing is None:
        db_session.add(User(id=user_id, first_name=name))
    else:
        existing.first_name = name
    await db_session.flush()
    if group_id is not None:
        db_session.add(
            Inv2GroupMember(user_id=user_id, group_id=group_id, last_seen_at=now)
        )
    if sick:
        db_session.add(
            UserDisease(user_id=user_id, infection_level=35, infected_at=now)
        )
    await db_session.flush()


async def _seed_doctor(db_session, doctor_id, *, chat_id=-1007001, on=None):
    selected_at = (on or _tehran(18, 15)).astimezone(timezone.utc)
    db_session.add(
        GroupDoctorState(
            chat_id=chat_id,
            doctor_user_id=doctor_id,
            selected_at=selected_at,
        )
    )
    await db_session.flush()


def _bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=9001))
    )


@pytest.mark.asyncio
async def test_announcement_sent_at_configured_time_and_not_twice_in_one_day(db_session):
    await _seed_group(db_session)
    await _seed_group(db_session, group_id=-1007002, active=False)
    await _seed_user(db_session, 7001, "دکتر امروز", group_id=-1007001)
    await _seed_user(db_session, 7002, "بیمار اول", group_id=-1007001, sick=True)
    await _seed_user(db_session, 7003, "بیمار دوم", group_id=-1007001, sick=True)
    await _seed_doctor(db_session, 7001)
    await db_session.commit()
    bot = _bot()

    # Not yet time: nothing is sent, but the day is not claimed either.
    assert await run_disease_announcement(bot, now=_tehran(18, 14)) == 0
    bot.send_message.assert_not_called()

    # The configured window opens at 18:15 Tehran.
    assert await run_disease_announcement(bot, now=_tehran(18, 15)) == 1
    assert bot.send_message.await_count == 1
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == -1007001  # the inactive group is skipped
    assert call.kwargs["parse_mode"] == "HTML"
    body = call.kwargs["text"]
    assert '<a href="tg://user?id=7001">' in body
    assert '<a href="tg://user?id=7002">' in body
    assert '<a href="tg://user?id=7003">' in body
    assert "**" not in body
    # The durable once-per-day marker is stored in system_config.
    row = await db_session.get(SystemConfig, ANNOUNCEMENT_STATE_KEY)
    assert row is not None
    assert row.value == _tehran(18, 15).astimezone(TEHRAN).date().isoformat()

    # A later tick the same Tehran day cannot repost - even inside the window.
    assert await run_disease_announcement(bot, now=_tehran(18, 20)) == 0
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_announcement_skips_outside_window_and_when_disabled(db_session):
    await _seed_group(db_session)
    await _seed_user(db_session, 7101, "دکتر", group_id=-1007001)
    await db_session.commit()
    bot = _bot()

    for moment in (_tehran(18, 0), _tehran(17, 59), _tehran(18, 30), _tehran(9, 15)):
        assert await run_disease_announcement(bot, now=moment) == 0
    bot.send_message.assert_not_called()

    # Disabled in config: the window opens but nothing is sent.
    db_session.add(SystemConfig(key="disease_announcement_enabled", value="false"))
    await db_session.commit()
    assert await run_disease_announcement(bot, now=_tehran(18, 15)) == 0
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_announcement_respects_a_reconfigured_hour_and_minute(db_session):
    await _seed_group(db_session)
    db_session.add_all(
        [
            SystemConfig(key="disease_announcement_hour", value="19"),
            SystemConfig(key="disease_announcement_minute", value="45"),
        ]
    )
    await db_session.commit()
    bot = _bot()

    assert await run_disease_announcement(bot, now=_tehran(18, 15)) == 0
    assert await run_disease_announcement(bot, now=_tehran(19, 45)) == 1
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_failed_group_send_does_not_block_other_groups(db_session):
    await _seed_group(db_session, group_id=-1007001)
    await _seed_group(db_session, group_id=-1007002)
    await db_session.commit()
    bot = _bot()
    bot.send_message.side_effect = [Exception("chat not found"), SimpleNamespace(message_id=9)]

    assert await run_disease_announcement(bot, now=_tehran(18, 15)) == 1
    assert bot.send_message.await_count == 2
    # The day is claimed regardless: no same-day retry storm.
    assert await run_disease_announcement(bot, now=_tehran(18, 16)) == 0


@pytest.mark.asyncio
async def test_announcement_tags_each_groups_local_doctor_and_fallback(db_session):
    group_a, group_b, group_c = -1007011, -1007012, -1007013
    await _seed_group(db_session, group_id=group_a)
    await _seed_group(db_session, group_id=group_b)
    await _seed_group(db_session, group_id=group_c)
    await _seed_user(db_session, 7201, "دکتر گروه اول", group_id=group_a)
    await _seed_user(db_session, 7202, "بیمار الف", group_id=group_a, sick=True)
    await _seed_user(db_session, 7203, "دکتر گروه دوم", group_id=group_b)
    await _seed_user(db_session, 7204, "بیمار ب", group_id=group_b, sick=True)
    await _seed_user(db_session, 7205, "بیمار ج", group_id=group_c, sick=True)
    await _seed_doctor(db_session, 7201, chat_id=group_a)
    await _seed_doctor(db_session, 7203, chat_id=group_b)
    # group_c intentionally has no doctor row (backwards compatibility fallback).
    await db_session.commit()
    bot = _bot()

    assert await run_disease_announcement(bot, now=_tehran(18, 15)) == 3
    by_chat = {
        call.kwargs["chat_id"]: call.kwargs["text"]
        for call in bot.send_message.await_args_list
    }
    assert set(by_chat) == {group_a, group_b, group_c}

    assert '<a href="tg://user?id=7201">' in by_chat[group_a]
    assert '<a href="tg://user?id=7202">' in by_chat[group_a]
    assert 'id=7203' not in by_chat[group_a]
    assert 'id=7204' not in by_chat[group_a]

    assert '<a href="tg://user?id=7203">' in by_chat[group_b]
    assert '<a href="tg://user?id=7204">' in by_chat[group_b]
    assert 'id=7201' not in by_chat[group_b]
    assert 'id=7202' not in by_chat[group_b]

    assert "هنوز انتخاب نشده است." in by_chat[group_c]
    assert '<a href="tg://user?id=7205">' in by_chat[group_c]
    assert 'id=7201' not in by_chat[group_c]
    assert 'id=7203' not in by_chat[group_c]

