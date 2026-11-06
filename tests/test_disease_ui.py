"""Phase 3 profile, doctor, menu, reminders and migration coverage.

SQLite exercises real transactions and compiles MySQL's locking SQL; real
SELECT FOR UPDATE races are separately marked for MySQL/MariaDB.
"""

import asyncio
import functools
import importlib.util
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import Chat, Message, User as TelegramUser
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import sib.database.connection as db_connection
from sib.database.models import (
    AppleTransaction, AppleWallet, Group, Inv2GroupMember, PanelOwner,
    SystemConfig, User, UserDisease, UserItem,
)
from sib.handlers import admin as admin_handler
from sib.handlers import disease as handler
from sib.handlers import start as start_handler
from sib.services import disease_service as disease
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.maintenance.service import DISEASE_NOTICE_TEXT, MaintenanceService
from sib.services.panel import PanelOwnershipService
import sib.services.maintenance.service as maintenance

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/migrations/versions/072_disease_ui.py"
GROUP = -100001
OTHER_GROUP = -100002
# 00:00 on October 29 in Tehran.
MIDNIGHT = datetime(2026, 10, 28, 20, 30, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'phase3.db'}")

    @sa.event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    metadata = sa.MetaData()
    for model in (User, Group, Inv2GroupMember, SystemConfig, UserDisease, UserItem,
                  AppleWallet, AppleTransaction, PanelOwner):
        model.__table__.to_metadata(metadata)
    # SQLite's integer PK is required for auto-generated ledger/member ids;
    # cloned metadata avoids mutating the app's global model definitions.
    for table in metadata.tables.values():
        for column in table.columns:
            if column.primary_key and column.autoincrement is True:
                column.type = sa.Integer()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_context():
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(handler, "get_session", session_context)
    monkeypatch.setattr(maintenance, "get_session", session_context)
    monkeypatch.setattr(db_connection, "get_session", session_context)
    monkeypatch.setattr(MaintenanceService, "_last_doctor_selection_day", None)
    monkeypatch.setattr(MaintenanceService, "_last_disease_notice_slot", None)
    yield factory
    await engine.dispose()


async def _seed(db, *, now=MIDNIGHT, doctor=1, patients=(2,), group=GROUP,
                infection_chance=0, limit=5, reward=50):
    async with db.begin() as session:
        session.add_all([Group(id=GROUP, title="group"), Group(id=OTHER_GROUP, title="other")])
        session.add_all([User(id=uid, first_name=f"بازیکن {uid}") for uid in {doctor, *patients}])
        await session.flush()
        for uid in {doctor, *patients}:
            session.add(Inv2GroupMember(user_id=uid, group_id=group, last_seen_at=now))
        for uid in patients:
            session.add(UserDisease(user_id=uid, infection_level=35, has_demon_power=True,
                                    infected_at=now))
        session.add(SystemConfig(key=handler.DOCTOR_STATE_KEY, value=json.dumps({
            "day": handler._tehran_day(now), "user_id": doctor, "heals": 0,
        })))
        for key, value in (
            ("disease_doctor_daily_limit", limit),
            ("disease_doctor_reward_apples", reward),
            ("disease_doctor_infection_chance", infection_chance),
        ):
            await ConfigService.set(session, key, value)


def _message(uid=1, *, chat_id=GROUP, kind="supergroup", message_id=70):
    message = MagicMock(spec=Message)
    message.chat = Chat(id=chat_id, type=kind)
    message.from_user = TelegramUser(id=uid, first_name=f"بازیکن {uid}", is_bot=False)
    message.text = "سیب من"
    message.message_id = message_id
    sent = MagicMock(spec=Message)
    sent.chat = message.chat
    sent.message_id = message_id + 1
    message.reply = AsyncMock(return_value=sent)
    message.reply_photo = AsyncMock(return_value=sent)
    return message


def _callback(uid=1, data="", *, chat_id=GROUP, kind="supergroup", message_id=71):
    callback = MagicMock()
    callback.data = data
    callback.from_user = TelegramUser(id=uid, first_name=f"بازیکن {uid}", is_bot=False)
    callback.message = MagicMock(spec=Message)
    callback.message.chat = Chat(id=chat_id, type=kind)
    callback.message.message_id = message_id
    callback.message.text = "menu"
    callback.message.reply_markup = None
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _callbacks(keyboard):
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def _bot(*, left=(), failure=()):
    bot = MagicMock()

    async def member(_group_id, user_id):
        if user_id in failure:
            raise RuntimeError("Telegram unavailable")
        return SimpleNamespace(status="left" if user_id in left else "member")

    bot.get_chat_member = AsyncMock(side_effect=member)
    bot.send_message = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# Own profiles: exact lines, both real entry paths and UTC handling.
# ---------------------------------------------------------------------------

def test_profile_lines_include_only_current_state():
    assert handler.disease_profile_lines({"is_sick": False}, now=MIDNIGHT) == []
    status = {"is_sick": True, "level": 35, "has_demon_power": True,
              "immunity_until": (MIDNIGHT + timedelta(hours=2, minutes=1)).replace(tzinfo=None)}
    assert handler.disease_profile_lines(status, now=MIDNIGHT) == [
        "🦠 وضعیت: بیمار (آلودگی: 35٪)",
        "😈 قدرت شیطانی: فعال",
        "🛡️ ایمنی موقت: تا 3 ساعت دیگر",
    ]
    assert handler.disease_profile_lines({
        "immunity_until": MIDNIGHT - timedelta(seconds=1)
    }, now=MIDNIGHT) == []
    assert handler.disease_profile_lines({
        "immunity_until": MIDNIGHT + timedelta(seconds=1)
    }, now=MIDNIGHT) == ["🛡️ ایمنی موقت: تا 1 ساعت دیگر"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sick", [True, False])
async def test_both_own_profile_entry_points_render_disease(db_session, sick):
    uid = 901234 if sick else 901235
    user = await db_session.get(User, uid)
    if user is None:
        db_session.add(User(id=uid, first_name="پروفایل"))
        await db_session.flush()
    row = await db_session.get(UserDisease, uid)
    if sick:
        if row is None:
            row = UserDisease(user_id=uid)
            db_session.add(row)
        row.infection_level = 35
        row.has_demon_power = True
        row.immunity_until = datetime.now(timezone.utc) + timedelta(hours=2)
    elif row is not None:
        await db_session.delete(row)
    await db_session.commit()
    message = _message(uid)
    await start_handler.handle_sib_profile_word(message, MagicMock())
    if message.reply_photo.await_count:
        message_text = message.reply_photo.await_args.kwargs["caption"]
    else:
        message_text = message.reply.await_args.args[0]

    callback = _callback(uid, "apple_profile", message_id=73)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.send_photo = AsyncMock(return_value=_message(uid).reply.return_value)
    bot.delete_message = AsyncMock()
    await start_handler.handle_apple_profile(callback, bot)
    if bot.send_photo.await_count:
        panel_text = bot.send_photo.await_args.kwargs["caption"]
    else:
        call = bot.edit_message_text.call_args or bot.edit_message_caption.call_args
        panel_text = call.args[0] if call.args else call.kwargs["text"]

    for text in (message_text, panel_text):
        if sick:
            assert "🦠 وضعیت: بیمار (آلودگی: 35٪)" in text
            assert "😈 قدرت شیطانی: فعال" in text
            assert "🛡️ ایمنی موقت: تا 2 ساعت دیگر" in text
        else:
            assert "🦠 وضعیت:" not in text
            assert "😈 قدرت شیطانی:" not in text
            assert "🛡️ ایمنی موقت:" not in text
    row = await db_session.get(UserDisease, uid)
    if row is not None:
        await db_session.delete(row)
        await db_session.commit()


# ---------------------------------------------------------------------------
# Daily doctor: Tehran midnight, eligible candidates and persisted quota.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doctor_selection_is_daily_persistent_and_unique_across_groups(db, monkeypatch):
    async with db.begin() as session:
        session.add_all([Group(id=GROUP, title="g"), Group(id=OTHER_GROUP, title="g2")])
        session.add_all([
            User(id=1, first_name="active1"), User(id=2, first_name="active2"),
            User(id=3, first_name="old"), User(id=4, first_name="bot", is_bot=True),
            User(id=5, first_name="banned", is_banned=True),
            User(id=6, first_name="inactive", is_active=False),
            User(id=7, first_name="future"),
        ])
        await session.flush()
        for uid, seen, group in [
            (1, MIDNIGHT - timedelta(hours=23), GROUP),
            (1, MIDNIGHT - timedelta(hours=1), OTHER_GROUP),
            (2, MIDNIGHT - timedelta(hours=24), GROUP),
            (3, MIDNIGHT - timedelta(hours=24, seconds=1), GROUP),
            (4, MIDNIGHT, GROUP), (5, MIDNIGHT, GROUP), (6, MIDNIGHT, GROUP),
            (7, MIDNIGHT + timedelta(seconds=1), GROUP),
        ]:
            session.add(Inv2GroupMember(user_id=uid, group_id=group, last_seen_at=seen))
    choice = Mock(side_effect=lambda candidates: candidates[-1])
    monkeypatch.setattr(handler.random, "choice", choice)
    async with db.begin() as session:
        assert await handler.select_daily_doctor(session, now=MIDNIGHT) == 2
    choice.assert_called_once_with([1, 2])
    async with db.begin() as session:
        row = await session.get(SystemConfig, handler.DOCTOR_STATE_KEY)
        assert json.loads(row.value) == {"day": "2026-10-29", "user_id": 2, "heals": 0}
        assert await handler.select_daily_doctor(session, now=MIDNIGHT + timedelta(hours=8)) == 2
    choice.assert_called_once()  # a second worker/restart does not reroll
    async with db.begin() as session:
        assert await handler.current_doctor(session, now=MIDNIGHT - timedelta(seconds=1)) is None
        assert await handler.current_doctor(session, now=MIDNIGHT + timedelta(days=1)) is None
        # On the next Tehran day, select again from the new 24-hour window.
        assert await handler.select_daily_doctor(session, now=MIDNIGHT + timedelta(days=1)) == 7
    assert choice.call_count == 2


@pytest.mark.asyncio
async def test_no_active_players_does_not_keep_yesterdays_doctor(db):
    async with db.begin() as session:
        session.add(SystemConfig(key=handler.DOCTOR_STATE_KEY, value=json.dumps({
            "day": "2026-10-28", "user_id": 123, "heals": 4,
        })))
    async with db.begin() as session:
        assert await handler.select_daily_doctor(session, now=MIDNIGHT) is None
    async with db() as session:
        state = json.loads((await session.get(SystemConfig, handler.DOCTOR_STATE_KEY)).value)
        assert state == {"day": "2026-10-29", "user_id": None, "heals": 0}


@pytest.mark.asyncio
async def test_doctor_heal_authorization_quota_reward_and_group_scope(db):
    await _seed(db, patients=(2, 3), limit=1, reward=73)
    async with db.begin() as session:
        # A player observed in another group must not be healable here.
        session.add(User(id=4, first_name="elsewhere"))
        await session.flush()
        session.add_all([
            UserDisease(user_id=4, infection_level=10),
            Inv2GroupMember(user_id=4, group_id=OTHER_GROUP, last_seen_at=MIDNIGHT),
        ])
    for doctor_id, patient_id, expected in [
        (2, 3, handler.DOCTOR_ONLY), (1, 4, handler.PATIENT_NOT_AVAILABLE),
        (1, 999999, handler.PATIENT_NOT_AVAILABLE),
    ]:
        with pytest.raises(handler.DiseaseItemError, match=expected):
            async with db.begin() as session:
                await handler.heal_as_doctor(session, doctor_id, patient_id, GROUP, now=MIDNIGHT)
    async with db.begin() as session:
        announcement = await handler.heal_as_doctor(session, 1, 2, GROUP, now=MIDNIGHT)
    assert announcement == "بازیکن 2"
    async with db() as session:
        patient = await disease.get_status(session, 2)
        assert not patient["is_sick"] and patient["immunity_until"] is not None
        assert await AppleWalletService.get_balance(session, 1) == 73
        assert json.loads((await session.get(SystemConfig, handler.DOCTOR_STATE_KEY)).value)["heals"] == 1
        assert await disease.is_sick(session, 3)
        assert await disease.is_sick(session, 4)
        transactions = (await session.scalars(sa.select(AppleTransaction))).all()
        assert len(transactions) == 1 and transactions[0].amount == 73
    with pytest.raises(handler.DiseaseItemError, match=handler.DOCTOR_LIMIT):
        async with db.begin() as session:
            await handler.heal_as_doctor(session, 1, 3, GROUP, now=MIDNIGHT)
    async with db() as session:
        assert await disease.is_sick(session, 3)
        assert await AppleWalletService.get_balance(session, 1) == 73


@pytest.mark.asyncio
async def test_doctor_infection_roll_and_phase1_immunity(db, monkeypatch):
    await _seed(db, patients=(2, 3), infection_chance=100)
    # First roll is the configured exposure; second is Phase 1's demon roll.
    rolls = Mock(side_effect=[0.0, 0.99])
    monkeypatch.setattr(handler.random, "random", rolls)
    async with db.begin() as session:
        await handler.heal_as_doctor(session, 1, 2, GROUP, now=MIDNIGHT)
    async with db() as session:
        assert await disease.is_sick(session, 1)
        assert not await disease.is_sick(session, 2)
    assert rolls.call_count == 2
    # Phase 1's cure grants immunity; a subsequent exposure must respect it.
    async with db.begin() as session:
        await disease.cure_user(session, 1)
    monkeypatch.setattr(handler.random, "random", Mock(return_value=0.0))
    async with db.begin() as session:
        await handler.heal_as_doctor(session, 1, 3, GROUP, now=MIDNIGHT)
    async with db() as session:
        assert not await disease.is_sick(session, 1)
        assert not await disease.is_sick(session, 3)


@pytest.mark.asyncio
async def test_heal_zero_infection_chance_does_not_roll(db, monkeypatch):
    await _seed(db, infection_chance=0)
    roll = Mock(side_effect=AssertionError("chance zero must not roll"))
    monkeypatch.setattr(handler.random, "random", roll)
    async with db.begin() as session:
        await handler.heal_as_doctor(session, 1, 2, GROUP, now=MIDNIGHT)
    async with db() as session:
        assert not await disease.is_sick(session, 1)
        assert not await disease.is_sick(session, 2)
    roll.assert_not_called()


@pytest.mark.asyncio
async def test_heal_rolls_back_patient_infection_and_quota_if_reward_fails(db, monkeypatch):
    await _seed(db, infection_chance=100)
    monkeypatch.setattr(handler.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(AppleWalletService, "earn", AsyncMock(side_effect=RuntimeError("wallet failed")))
    with pytest.raises(RuntimeError, match="wallet failed"):
        async with db.begin() as session:
            await handler.heal_as_doctor(session, 1, 2, GROUP, now=MIDNIGHT)
    async with db() as session:
        assert await disease.is_sick(session, 2)
        assert not await disease.is_sick(session, 1)
        assert json.loads((await session.get(SystemConfig, handler.DOCTOR_STATE_KEY)).value)["heals"] == 0
        assert await AppleWalletService.get_balance(session, 1) == 0


@pytest.mark.asyncio
async def test_doctor_locks_mutex_and_sorted_users_with_mysql_for_update(db):
    # Deliberately make patient id smaller than doctor id.
    await _seed(db, doctor=9, patients=(2,))
    statements = []
    async with db() as session:
        @sa.event.listens_for(session.sync_session, "do_orm_execute")
        def capture(execute_state):
            if execute_state.is_select and execute_state.statement._for_update_arg is not None:
                statements.append(str(execute_state.statement.compile(
                    dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
                )))
        async with session.begin():
            await handler.heal_as_doctor(session, 9, 2, GROUP, now=MIDNIGHT)
    locks = [sql for sql in statements if "FOR UPDATE" in sql.upper()]
    assert "system_config" in locks[0].lower()
    user_locks = [sql.lower() for sql in locks if "from users" in sql.lower()]
    assert "where users.id = 2" in user_locks[0]
    assert "where users.id = 9" in user_locks[1]
    assert any("user_diseases" in sql.lower() for sql in locks)


# ---------------------------------------------------------------------------
# Telegram doctor and disease menus, including forged and stale callbacks.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doctor_command_and_group_patient_list(db, monkeypatch):
    now = datetime.now(timezone.utc)
    await _seed(db, now=now, patients=(2, 3, 4, 5, 6, 7, 8))
    async with db.begin() as session:
        session.add(User(id=9, first_name="دیگر"))
        await session.flush()
        session.add_all([
            UserDisease(user_id=9, infection_level=15),
            Inv2GroupMember(user_id=9, group_id=OTHER_GROUP, last_seen_at=now),
        ])
    doctor_msg, other_msg = _message(1), _message(2)
    await handler.handle_doctor(doctor_msg)
    await handler.handle_doctor(other_msg)
    assert doctor_msg.reply.await_args.args[0] == "🩺 پنل پزشک روز"
    assert _callbacks(doctor_msg.reply.await_args.kwargs["reply_markup"]) == ["doc_heal_list"]
    assert other_msg.reply.await_args.args[0] == handler.DOCTOR_ONLY

    answer, edit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(handler, "safe_answer_callback", answer)
    monkeypatch.setattr(handler, "safe_edit_callback_message", edit)
    bot = _bot(left=(3,))
    await handler.handle_doctor_heal_list(_callback(1, "doc_heal_list"), bot)
    keys = _callbacks(edit.await_args.kwargs["reply_markup"])
    assert [key for key in keys if key.startswith("doc_heal:")] == [
        "doc_heal:2", "doc_heal:4", "doc_heal:5", "doc_heal:6", "doc_heal:7"
    ]
    assert "doc_heal:9" not in keys
    assert bot.get_chat_member.await_args_list[0].args == (GROUP, 1)
    answer.reset_mock(); edit.reset_mock(); bot.get_chat_member.reset_mock()
    await handler.handle_doctor_heal_list(_callback(2, "doc_heal_list"), bot)
    assert answer.await_args.args[1] == handler.DOCTOR_ONLY
    edit.assert_not_awaited(); bot.get_chat_member.assert_not_awaited()
    answer.reset_mock()
    await handler.handle_doctor_heal_list(_callback(1, "doc_heal_list", kind="private", chat_id=1), bot)
    assert answer.await_args.args[1] == handler.DOCTOR_GROUP_ONLY


@pytest.mark.asyncio
async def test_doctor_callback_rechecks_membership_and_announces_once(db, monkeypatch):
    await _seed(db, now=datetime.now(timezone.utc))
    answer = AsyncMock()
    monkeypatch.setattr(handler, "safe_answer_callback", answer)
    bot = _bot(left=(2,))
    callback = _callback(1, "doc_heal:2")
    await handler.handle_doctor_heal(callback, bot)
    assert answer.await_args.args[1] == handler.PATIENT_NOT_AVAILABLE
    bot.send_message.assert_not_awaited()
    async with db() as session:
        assert await disease.is_sick(session, 2)
    bot = _bot(failure=(1,))
    await handler.handle_doctor_heal(callback, bot)
    assert answer.await_args.args[1] == handler.DOCTOR_GROUP_ONLY
    bot.send_message.assert_not_awaited()
    bot = _bot()
    await handler.handle_doctor_heal(_callback(2, "doc_heal:2"), bot)
    assert answer.await_args.args[1] == handler.DOCTOR_ONLY
    bot.get_chat_member.assert_not_awaited()
    await handler.handle_doctor_heal(callback, bot)
    assert callback.message.answer.await_args.args == (
        "💉 \u200eبازیکن 1\u200e بیمار \u200eبازیکن 2\u200e را درمان کرد.",
    )
    await handler.handle_doctor_heal(callback, bot)  # stale button can't pay twice
    assert callback.message.answer.await_count == 1
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["doc_heal:0", "doc_heal:-1", "doc_heal:９", "doc_heal:9999999999999999999"])
async def test_malformed_doctor_callback_rejected_without_telegram_call(db, monkeypatch, data):
    await _seed(db, now=datetime.now(timezone.utc))
    answer, bot = AsyncMock(), _bot()
    monkeypatch.setattr(handler, "safe_answer_callback", answer)
    await handler.handle_doctor_heal(_callback(1, data), bot)
    assert answer.await_args.args[1] == handler.PATIENT_NOT_AVAILABLE
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_disease_menu_is_owned_and_reuses_transactional_craft_and_use(db, monkeypatch):
    await _seed(db, now=datetime.now(timezone.utc))
    async with db.begin() as session:
        session.add_all([
            UserItem(user_id=2, item_key=handler.PLANT, quantity=1),
            UserItem(user_id=2, item_key=handler.BUFF, quantity=1),
            AppleWallet(user_id=2, balance=200),
        ])
    message = _message(2)
    await handler.handle_disease_menu(message)
    assert "🦠 وضعیت: بیمار (آلودگی: 35٪)" in message.reply.await_args.args[0]
    keyboard = message.reply.await_args.kwargs["reply_markup"]
    assert _callbacks(keyboard) == ["craft_antidote_menu", "use_antidote"]
    async with db() as session:
        assert await PanelOwnershipService.get_owner(session, GROUP, 71) == 2
    answer, edit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(handler, "safe_answer_callback", answer)
    monkeypatch.setattr(handler, "safe_edit_callback_message", edit)
    bot = _bot()
    await handler.handle_craft_antidote_menu(_callback(1, "craft_antidote_menu"), bot)
    assert answer.await_args.args[1] == handler.LOCKED_MESSAGE
    edit.assert_not_awaited()
    # Unclaimed and wrong-panel callbacks also fail closed, even for the owner.
    await handler.handle_use_antidote_menu(_callback(2, "use_antidote", message_id=999), bot)
    assert answer.await_args.args[1] == handler.LOCKED_MESSAGE
    async with db.begin() as session:
        session.add(PanelOwner(chat_id=GROUP, message_id=72, owner_user_id=2, panel_type="shop"))
    await handler.handle_craft_antidote_menu(_callback(2, "craft_antidote_menu", message_id=72), bot)
    assert answer.await_args.args[1] == handler.LOCKED_MESSAGE

    await handler.handle_craft_antidote_menu(_callback(2, "craft_antidote_menu"), bot)
    assert answer.await_args.args[1] == handler.CRAFT_SUCCESS
    async with db() as session:
        assert await ItemService.get_total(session, 2, handler.ANTIDOTE) == 1
        assert await ItemService.get_total(session, 2, handler.PLANT) == 0
        assert await AppleWalletService.get_balance(session, 2) == 0
    await handler.handle_use_antidote_menu(_callback(2, "use_antidote"), bot)
    assert answer.await_args.args[1] == handler.USE_SUCCESS
    async with db() as session:
        assert not await disease.is_sick(session, 2)
        assert await ItemService.get_total(session, 2, handler.ANTIDOTE) == 0
    await handler.handle_use_antidote_menu(_callback(2, "use_antidote"), bot)
    assert answer.await_args.args[1] == handler.NOT_SICK
    await handler.handle_craft_antidote_menu(_callback(2, "craft_antidote_menu"), bot)
    assert answer.await_args.args[1] == handler.MISSING_PLANT


# ---------------------------------------------------------------------------
# Slash-less Persian aliases: «بیماری»/«درمان», «دکتر»/«پزشک» and
# «پادزهر»/«مصرف پادزهر». They must reach the very same handler functions as
# /disease, /doctor and /use_antidote and must never swallow a normal sentence.
# ---------------------------------------------------------------------------

#: Every alias and the command whose handler it must reach.
PLAIN_TEXT_ALIASES = {
    "/disease": ("handle_disease_menu", ("بیماری", "درمان")),
    "/doctor": ("handle_doctor", ("دکتر", "پزشک")),
    "/use_antidote": ("handle_use_antidote", ("پادزهر", "مصرف پادزهر")),
}


@functools.lru_cache(maxsize=1)
def _included_routers():
    """Top-level routers ``register_all_handlers`` includes, in include order.

    Recorded through a patched ``include_router``: aiogram refuses to attach one
    router to two dispatchers, so nothing is attached here.
    """
    from unittest.mock import patch as _patch

    from aiogram import Dispatcher

    from sib.handlers import register_all_handlers

    included: list = []
    with _patch.object(Dispatcher, "include_router", side_effect=included.append):
        register_all_handlers(Dispatcher())
    return tuple(included)


def _walk_routers(router):
    yield router
    for sub in router.sub_routers:
        yield from _walk_routers(sub)


async def _reached_handler(text, *, kind="private"):
    """The handler function aiogram dispatches ``text`` to (first match wins)."""
    event = Message(
        message_id=1, date=datetime.now(timezone.utc),
        chat=Chat(id=1, type=kind),
        from_user=TelegramUser(id=1, first_name="player", is_bot=False), text=text,
    )
    for root in _included_routers():
        for router in _walk_routers(root):
            for registered in router.message.handlers:
                matched = True
                for flt in registered.filters:
                    try:
                        if not await flt.call(event, bot=None):
                            matched = False
                            break
                    except Exception:
                        matched = False
                        break
                if matched:
                    return registered.callback
    return None


@pytest.mark.parametrize("command,aliases", [
    (command, aliases) for command, (_, aliases) in PLAIN_TEXT_ALIASES.items()
])
@pytest.mark.parametrize("kind", ["private", "supergroup"])
@pytest.mark.asyncio
async def test_plain_text_aliases_reach_the_exact_command_handler(command, aliases, kind):
    expected = getattr(handler, PLAIN_TEXT_ALIASES[command][0])
    assert await _reached_handler(command, kind=kind) is expected
    for alias in aliases:
        assert await _reached_handler(alias, kind=kind) is expected
        assert await _reached_handler(f"  {alias}  ", kind=kind) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected_name", [
    ("بيماري", "handle_disease_menu"),   # Arabic Yeh (U+064A)
    ("درمان", "handle_disease_menu"),
    ("دكتر", "handle_doctor"),           # Arabic Kaf (U+0643)
    ("پزشك", "handle_doctor"),
    ("پادزهر", "handle_use_antidote"),
    ("مصرف پادزهر", "handle_use_antidote"),
])
async def test_aliases_fold_typed_persian_variants(text, expected_name):
    assert await _reached_handler(text) is getattr(handler, expected_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "من بیمارم",
    "بیماری مرموزی در حال شیوع است",
    "درمان بیمار",
    "دکتر کیست",
    "پنل پزشک روز",
    "پادزهر آماده",
    "می‌خواهم پادزهر مصرف کنم",
    "انتقال پادزهر آماده ۱",
])
async def test_sentences_containing_the_words_are_not_shortcuts(text):
    # The transfer phrase reaches the transfer handler, never an antidote use.
    assert await _reached_handler(text) is not handler.handle_use_antidote
    assert await _reached_handler(text) is not handler.handle_disease_menu
    assert await _reached_handler(text) is not handler.handle_doctor


def test_alias_patterns_are_anchored_and_disjoint():
    cases = (
        (handler.DISEASE_TEXT_PATTERN, ("بیماری", "درمان"),
         ("پزشک", "پادزهر", "من بیمارم", "/disease")),
        (handler.DOCTOR_TEXT_PATTERN, ("دکتر", "پزشک"),
         ("بیماری", "درمان", "دکتر کیست", "/doctor")),
        (handler.USE_ANTIDOTE_TEXT_PATTERN, ("پادزهر", "مصرف پادزهر"),
         ("پادزهر آماده", "پزشک", "مصرف", "/use_antidote")),
    )
    for pattern, accepted, rejected in cases:
        for text in accepted:
            assert handler.is_plain_text_trigger(pattern, text), text
        for text in rejected:
            assert not handler.is_plain_text_trigger(pattern, text), text


@pytest.mark.asyncio
async def test_disease_menu_alias_renders_the_same_panel_as_the_command(db):
    await _seed(db, now=datetime.now(timezone.utc))
    command_msg, alias_msg = _message(2, message_id=70), _message(2, message_id=80)
    await handler.handle_disease_menu(command_msg)
    alias_msg.text = "بیماری"
    await handler.handle_disease_menu(alias_msg)
    assert alias_msg.reply.await_args.args[0] == command_msg.reply.await_args.args[0]
    assert (_callbacks(alias_msg.reply.await_args.kwargs["reply_markup"])
            == _callbacks(command_msg.reply.await_args.kwargs["reply_markup"])
            == ["craft_antidote_menu", "use_antidote"])


@pytest.mark.asyncio
async def test_doctor_alias_serves_the_doctor_and_refuses_everyone_else(db):
    await _seed(db, now=datetime.now(timezone.utc))
    doctor_msg, other_msg = _message(1, message_id=80), _message(2, message_id=81)
    doctor_msg.text, other_msg.text = "پزشک", "دکتر"
    await handler.handle_doctor(doctor_msg)
    await handler.handle_doctor(other_msg)
    assert doctor_msg.reply.await_args.args[0] == "🩺 پنل پزشک روز"
    assert _callbacks(doctor_msg.reply.await_args.kwargs["reply_markup"]) == ["doc_heal_list"]
    assert other_msg.reply.await_args.args[0] == handler.DOCTOR_ONLY


@pytest.mark.asyncio
async def test_antidote_alias_uses_the_same_transactional_path(db):
    await _seed(db)
    async with db.begin() as session:
        session.add(UserItem(user_id=2, item_key=handler.ANTIDOTE, quantity=1))
    message = _message(2)
    message.text = "مصرف پادزهر"
    await handler.handle_use_antidote(message)
    assert message.reply.await_args.args[0] == handler.USE_SUCCESS
    async with db() as session:
        assert not await disease.is_sick(session, 2)
        assert await ItemService.get_total(session, 2, handler.ANTIDOTE) == 0

    # The bare alias answers exactly like /use_antidote for a healthy player.
    again = _message(2)
    again.text = "پادزهر"
    await handler.handle_use_antidote(again)
    assert again.reply.await_args.args[0] == handler.NOT_SICK


# ---------------------------------------------------------------------------
# Reminders: scheduled Tehran slots, cooldown, failures and isolation.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("offset,expected", [
    (timedelta(hours=13, minutes=59), None),
    (timedelta(hours=14), (MIDNIGHT.astimezone(disease.TEHRAN).date(), 14)),
    (timedelta(hours=14, minutes=4, seconds=59), (MIDNIGHT.astimezone(disease.TEHRAN).date(), 14)),
    (timedelta(hours=14, minutes=5), None),
    (timedelta(hours=21, minutes=59), None),
    (timedelta(hours=22), (MIDNIGHT.astimezone(disease.TEHRAN).date(), 22)),
    (timedelta(hours=22, minutes=5), None),
])
def test_notification_time_slots_are_tehran_and_bounded(offset, expected):
    assert MaintenanceService._disease_notice_slot(MIDNIGHT + offset) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [14, 22])
async def test_scheduled_dm_only_sick_users_and_four_hour_cooldown(db, hour):
    now = MIDNIGHT + timedelta(hours=hour)
    async with db.begin() as session:
        session.add_all([User(id=i, first_name=str(i)) for i in range(1, 6)])
        await session.flush()
        session.add_all([
            UserDisease(user_id=1, infection_level=10),
            UserDisease(user_id=2, infection_level=10, last_notified_at=now - timedelta(hours=3, minutes=58)),
            UserDisease(user_id=3, infection_level=10, last_notified_at=now - timedelta(hours=4)),
            UserDisease(user_id=4, infection_level=0),
            UserDisease(user_id=5, infection_level=10, last_notified_at=now + timedelta(hours=1)),
        ])
    bot = _bot()
    assert await MaintenanceService.run_disease_notifications(bot, now=now) == 2
    calls = bot.send_message.await_args_list
    assert {call.args[0] for call in calls} == {1, 3}
    assert all(call.args[1] == DISEASE_NOTICE_TEXT and call.kwargs["parse_mode"] is None for call in calls)
    assert DISEASE_NOTICE_TEXT == (
        "⚠️ یادآوری: شما به بیماری آلوده هستید.\n"
        "برای درمان، دستور /disease را بزنید یا از پزشک گروه کمک بگیرید."
    )
    assert await MaintenanceService.run_disease_notifications(bot, now=now + timedelta(minutes=1)) == 0
    async with db() as session:
        delivered_at = disease._as_utc((await session.get(UserDisease, 1)).last_notified_at)
        assert now <= delivered_at <= now + timedelta(seconds=15)
        assert disease._as_utc((await session.get(UserDisease, 2)).last_notified_at) == now - timedelta(hours=3, minutes=58)
    assert await MaintenanceService.run_disease_notifications(bot, now=now - timedelta(minutes=1)) == 0
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_blocked_dm_restores_claim_and_does_not_block_other_users(db):
    now = MIDNIGHT + timedelta(hours=14)
    async with db.begin() as session:
        session.add_all([User(id=i, first_name=str(i)) for i in (1, 2)])
        await session.flush()
        session.add_all([UserDisease(user_id=i, infection_level=10) for i in (1, 2)])
    bot = _bot()

    async def send(uid, *_args, **_kwargs):
        if uid == 1:
            raise TelegramForbiddenError(method=None, message="blocked")
    bot.send_message.side_effect = send
    assert await MaintenanceService.run_disease_notifications(bot, now=now) == 1
    async with db() as session:
        assert (await session.get(UserDisease, 1)).last_notified_at is None
        delivered_at = disease._as_utc((await session.get(UserDisease, 2)).last_notified_at)
        assert now <= delivered_at <= now + timedelta(seconds=15)
    bot.send_message.side_effect = None
    assert await MaintenanceService.run_disease_notifications(bot, now=now + timedelta(minutes=1)) == 1


@pytest.mark.asyncio
async def test_maintenance_cycle_selects_once_per_day_and_runs_each_slot(db, monkeypatch):
    selector, notify = AsyncMock(return_value=None), AsyncMock(return_value=0)
    monkeypatch.setattr(handler, "select_daily_doctor", selector)
    monkeypatch.setattr(MaintenanceService, "run_disease_notifications", notify)
    bot = _bot()
    await MaintenanceService.run_disease_cycle(bot, now=MIDNIGHT)
    await MaintenanceService.run_disease_cycle(bot, now=MIDNIGHT + timedelta(hours=14))
    await MaintenanceService.run_disease_cycle(bot, now=MIDNIGHT + timedelta(hours=14, minutes=1))
    await MaintenanceService.run_disease_cycle(bot, now=MIDNIGHT + timedelta(hours=22))
    assert selector.await_count == 1
    assert notify.await_count == 2
    await MaintenanceService.run_disease_cycle(bot, now=MIDNIGHT + timedelta(days=1))
    assert selector.await_count == 2


@pytest.mark.asyncio
async def test_maintenance_reminders_are_isolated_from_failed_doctor_selection(db, monkeypatch):
    selector = AsyncMock(side_effect=RuntimeError("selection failed"))
    notify = AsyncMock(return_value=0)
    monkeypatch.setattr(handler, "select_daily_doctor", selector)
    monkeypatch.setattr(MaintenanceService, "run_disease_notifications", notify)
    await MaintenanceService.run_disease_cycle(_bot(), now=MIDNIGHT + timedelta(hours=14))
    assert selector.await_count == 1 and notify.await_count == 1
    assert MaintenanceService._last_doctor_selection_day is None


# ---------------------------------------------------------------------------
# Migration: real 071 -> 072 -> 071, idempotence and operator-state safety.
# ---------------------------------------------------------------------------

def _migration():
    spec = importlib.util.spec_from_file_location("migration_072", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_072_single_head_and_real_round_trip(tmp_path):
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["079_disease_daily_announcement"]
    assert scripts.get_revision("074_theft_system").down_revision == "073_disease_started_flag"
    assert scripts.get_revision("073_disease_started_flag").down_revision == "072_disease_ui"
    assert scripts.get_revision("072_disease_ui").down_revision == "071_disease_items"
    db_path = tmp_path / "ui_migration.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE system_config (`key` VARCHAR(255) PRIMARY KEY, `value` TEXT NOT NULL)"))
    env = {**os.environ, "DATABASE_SYNC_URL": f"sqlite:///{db_path}",
           "PYTHONPATH": f"{ROOT.parent}:{ROOT}"}

    def alembic(*args):
        result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                                text=True, capture_output=True, check=True)
        return result.stdout.strip()

    assert alembic("heads") == "079_disease_daily_announcement (head)"
    alembic("stamp", "071_disease_items")
    alembic("upgrade", "072_disease_ui")
    alembic("upgrade", "072_disease_ui")
    assert "072_disease_ui" in alembic("current")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT `value` FROM system_config WHERE `key`=:key"),
                                  {"key": handler.DOCTOR_STATE_KEY}).scalar_one() == handler.DOCTOR_STATE_DEFAULT
    alembic("downgrade", "071_disease_items")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM system_config")).scalar_one() == 0
    alembic("upgrade", "072_disease_ui")
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE system_config SET `value`='operator-state'"))
    alembic("downgrade", "071_disease_items")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT `value` FROM system_config")).scalar_one() == "operator-state"
    engine.dispose()


def test_migration_072_mysql_seed_and_downgrade_sql():
    from io import StringIO

    module = _migration()
    output = StringIO()
    context = MigrationContext.configure(dialect_name="mysql", opts={
        "as_sql": True, "output_buffer": output, "literal_binds": True,
    })
    module.op = Operations(context)
    module.upgrade(); module.downgrade()
    sql = output.getvalue()
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "disease_doctor_state" in sql
    assert "BINARY `value` = BINARY" in sql


@pytest.mark.asyncio
async def test_notification_claim_prevents_another_worker_during_delivery(db):
    now = MIDNIGHT + timedelta(hours=14)
    async with db.begin() as session:
        session.add(User(id=1, first_name="player"))
        await session.flush()
        session.add(UserDisease(user_id=1, infection_level=10))
    sending, release = asyncio.Event(), asyncio.Event()
    bot = _bot()

    async def slow_send(*_args, **_kwargs):
        sending.set()
        await release.wait()

    bot.send_message.side_effect = slow_send
    first = asyncio.create_task(MaintenanceService._notify_sick_user(bot, 1, now))
    try:
        await asyncio.wait_for(sending.wait(), timeout=3)
        assert not await MaintenanceService._notify_sick_user(bot, 1, now)
        assert bot.send_message.await_count == 1
    finally:
        release.set()
        assert await asyncio.wait_for(first, timeout=3)


_ROW_LOCK_DATABASE = any(
    dialect in (os.getenv("sib_TEST_DATABASE_URL") or "")
    for dialect in ("mysql", "mariadb")
)


@pytest.mark.asyncio
@pytest.mark.skipif(not _ROW_LOCK_DATABASE, reason="real SELECT FOR UPDATE races require MySQL/MariaDB")
async def test_concurrent_doctor_heals_serialize_and_enforce_one_shared_quota(db_session):
    from sib.database.connection import get_async_sessionmaker
    from sib.tests.test_parties import create_test_user

    doctor = await create_test_user(db_session, "doctor race")
    patients = [await create_test_user(db_session, f"patient race {i}") for i in range(2)]
    group_id = -100971234
    db_session.add(Group(id=group_id, title="doctor race"))
    await db_session.flush()
    now = datetime.now(timezone.utc)
    for patient in patients:
        db_session.add_all([
            Inv2GroupMember(user_id=patient.id, group_id=group_id, last_seen_at=now),
            UserDisease(user_id=patient.id, infection_level=10),
        ])
    db_session.add(SystemConfig(key=handler.DOCTOR_STATE_KEY, value=json.dumps({
        "day": handler._tehran_day(now), "user_id": doctor.id, "heals": 0,
    })))
    await ConfigService.set(db_session, "disease_doctor_daily_limit", 1)
    await ConfigService.set(db_session, "disease_doctor_infection_chance", 0)
    await ConfigService.set(db_session, "disease_doctor_reward_apples", 50)
    await db_session.commit()
    first_ready, release = asyncio.Event(), asyncio.Event()
    factory = get_async_sessionmaker()

    async def first():
        async with factory.begin() as session:
            await handler.heal_as_doctor(session, doctor.id, patients[0].id, group_id, now=now)
            first_ready.set()
            await release.wait()

    async def second():
        try:
            async with factory.begin() as session:
                await handler.heal_as_doctor(session, doctor.id, patients[1].id, group_id, now=now)
            return "unexpected success"
        except handler.DiseaseItemError as exc:
            return str(exc)

    task1, task2 = asyncio.create_task(first()), None
    try:
        await asyncio.wait_for(first_ready.wait(), timeout=10)
        task2 = asyncio.create_task(second())
        await asyncio.sleep(0.2)
        assert not task2.done(), "second healer did not wait on the shared doctor mutex"
        release.set()
        _, result = await asyncio.wait_for(asyncio.gather(task1, task2), timeout=10)
        assert result == handler.DOCTOR_LIMIT
    finally:
        release.set()
        for task in (task1, task2):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (task1, task2) if task), return_exceptions=True)
    await db_session.rollback()
    assert not await disease.is_sick(db_session, patients[0].id)
    assert await disease.is_sick(db_session, patients[1].id)
    assert await AppleWalletService.get_balance(db_session, doctor.id) == 50


@pytest.mark.asyncio
@pytest.mark.skipif(not _ROW_LOCK_DATABASE, reason="real SELECT FOR UPDATE races require MySQL/MariaDB")
async def test_simultaneous_notification_claims_send_only_once(db_session):
    from sib.database.connection import get_async_sessionmaker
    from sib.tests.test_parties import create_test_user

    player = await create_test_user(db_session, "reminder race")
    db_session.add(UserDisease(user_id=player.id, infection_level=10))
    await db_session.commit()
    bot = _bot()
    now = MIDNIGHT + timedelta(hours=14)
    tasks = []
    try:
        # Hold the disease row until both workers are waiting for the claim.
        async with get_async_sessionmaker().begin() as session:
            await session.scalar(sa.select(UserDisease).where(UserDisease.user_id == player.id).with_for_update())
            tasks = [asyncio.create_task(MaintenanceService._notify_sick_user(bot, player.id, now))
                     for _ in range(2)]
            await asyncio.sleep(0.2)
            assert not any(task.done() for task in tasks)
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert sorted(results) == [False, True]
        assert bot.send_message.await_count == 1
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_parallel_notification_claims_never_double_send_on_sqlite(db):
    now = MIDNIGHT + timedelta(hours=14)
    async with db.begin() as session:
        session.add(User(id=1, first_name="player"))
        await session.flush()
        session.add(UserDisease(user_id=1, infection_level=10))
    bot = _bot()
    results = await asyncio.gather(
        *(MaintenanceService._notify_sick_user(bot, 1, now) for _ in range(2)),
        return_exceptions=True,
    )
    assert sorted(results) == [False, True]
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_doctor_heals_observe_the_single_quota_on_sqlite(db):
    await _seed(db, patients=(2, 3), limit=1)
    ready, release = asyncio.Event(), asyncio.Event()

    async def first():
        async with db.begin() as session:
            await handler.heal_as_doctor(session, 1, 2, GROUP, now=MIDNIGHT)
            ready.set()
            await release.wait()

    async def second():
        try:
            async with db.begin() as session:
                await handler.heal_as_doctor(session, 1, 3, GROUP, now=MIDNIGHT)
            return "unexpected success"
        except handler.DiseaseItemError as exc:
            return str(exc)

    first_task, second_task = asyncio.create_task(first()), None
    try:
        await asyncio.wait_for(ready.wait(), timeout=3)
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0.1)
        assert not second_task.done()
        release.set()
        _, result = await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=5)
        assert result == handler.DOCTOR_LIMIT
    finally:
        release.set()
        for task in (first_task, second_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first_task, second_task) if task), return_exceptions=True)
    async with db() as session:
        assert not await disease.is_sick(session, 2)
        assert await disease.is_sick(session, 3)
        assert await AppleWalletService.get_balance(session, 1) == 50


@pytest.mark.asyncio
async def test_ambiguous_dm_failure_keeps_claim_to_avoid_duplicate(db):
    now = MIDNIGHT + timedelta(hours=14, microseconds=500000)
    async with db.begin() as session:
        session.add(User(id=1, first_name="player"))
        await session.flush()
        session.add(UserDisease(user_id=1, infection_level=10))
    bot = _bot()
    bot.send_message.side_effect = RuntimeError("network response lost")
    assert not await MaintenanceService._notify_sick_user(bot, 1, now)
    async with db() as session:
        claimed = disease._as_utc((await session.get(UserDisease, 1)).last_notified_at)
        assert claimed >= now + timedelta(seconds=maintenance.DISEASE_DM_TIMEOUT_SECONDS)
    bot.send_message.side_effect = None
    assert not await MaintenanceService._notify_sick_user(bot, 1, now + timedelta(minutes=1))
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_successful_dm_timestamp_rounds_up_so_cooldown_is_not_shortened(db):
    now = MIDNIGHT + timedelta(hours=14, microseconds=500000)
    async with db.begin() as session:
        session.add(User(id=1, first_name="player"))
        await session.flush()
        session.add(UserDisease(user_id=1, infection_level=10))
    bot = _bot()
    assert await MaintenanceService._notify_sick_user(bot, 1, now)
    async with db() as session:
        recorded = disease._as_utc((await session.get(UserDisease, 1)).last_notified_at)
        assert recorded >= now
        assert recorded.microsecond == 0
    assert not await MaintenanceService._notify_sick_user(bot, 1, recorded + timedelta(hours=4) - timedelta(seconds=1))
    assert await MaintenanceService._notify_sick_user(bot, 1, recorded + timedelta(hours=4))
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_delayed_worker_cannot_rewind_doctor_selection_across_midnight(db, monkeypatch):
    async with db.begin() as session:
        session.add_all([Group(id=GROUP, title="g"), User(id=1, first_name="active")])
        await session.flush()
        session.add(Inv2GroupMember(user_id=1, group_id=GROUP, last_seen_at=MIDNIGHT - timedelta(hours=1)))
    choice = Mock(side_effect=lambda candidates: candidates[0])
    monkeypatch.setattr(handler.random, "choice", choice)
    ready, release = asyncio.Event(), asyncio.Event()

    async def current_worker():
        async with db.begin() as session:
            selected = await handler.select_daily_doctor(session, now=MIDNIGHT)
            ready.set()
            await release.wait()
            return selected

    async def delayed_worker():
        async with db.begin() as session:
            return await handler.select_daily_doctor(session, now=MIDNIGHT - timedelta(seconds=1))

    first, delayed = asyncio.create_task(current_worker()), None
    try:
        await asyncio.wait_for(ready.wait(), timeout=3)
        delayed = asyncio.create_task(delayed_worker())
        await asyncio.sleep(0.1)
        assert not delayed.done()
        release.set()
        assert await asyncio.wait_for(asyncio.gather(first, delayed), timeout=5) == [1, None]
    finally:
        release.set()
        for task in (first, delayed):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, delayed) if task), return_exceptions=True)
    async with db() as session:
        state = json.loads((await session.get(SystemConfig, handler.DOCTOR_STATE_KEY)).value)
        assert state == {"day": "2026-10-29", "user_id": 1, "heals": 0}
    choice.assert_called_once()


# ---------------------------------------------------------------------------
# One-time initial disease wave and migration 073.
# ---------------------------------------------------------------------------

_WAVE_FLAG = "disease_initial_wave_triggered"
_WAVE_WARNING = "⚠️ موج اولیه بیماری قبلاً اجرا شده است."


@pytest.fixture
def wave_db(db, monkeypatch):
    @asynccontextmanager
    async def session_context():
        async with db.begin() as session:
            yield session

    monkeypatch.setattr(admin_handler, "get_session", session_context)
    monkeypatch.setattr(admin_handler, "is_admin", lambda user_id: user_id == 99)
    return db


async def _seed_wave(db, flag=None):
    async with db.begin() as session:
        session.add_all([
            Group(id=GROUP, title="group"), Group(id=OTHER_GROUP, title="other"),
            User(id=1, first_name="one"), User(id=2, first_name="two"),
        ])
        if flag is not None:
            session.add(SystemConfig(key=_WAVE_FLAG, value=flag))
        await ConfigService.set(session, "disease_initial_infection_percent", 100)
        await session.flush()
        now = datetime.now(timezone.utc)
        session.add_all(Inv2GroupMember(user_id=user_id, group_id=GROUP, last_seen_at=now)
                        for user_id in (1, 2))


@pytest.mark.parametrize("flag", [None, "false"])
async def test_initial_wave_runs_once_and_persists_flag(wave_db, monkeypatch, flag):
    from sib.core.text import format_html

    await _seed_wave(wave_db, flag)
    seed = AsyncMock(wraps=disease.process_initial_infection)
    monkeypatch.setattr(disease, "process_initial_infection", seed)
    first, second, bot = _message(99), _message(99), _bot()
    during_broadcast = []

    async def verify_before_send(*args, **kwargs):
        async with wave_db() as session:
            during_broadcast.append((
                (await session.get(SystemConfig, _WAVE_FLAG)).value,
                await disease.is_sick(session, 1), await disease.is_sick(session, 2),
            ))

    bot.send_message.side_effect = verify_before_send
    await admin_handler.handle_disease_start(first, bot)
    first.reply.assert_awaited_once_with(
        "✅ موج اولیه بیماری با موفقیت شروع شد. تعداد مبتلایان: 2"
    )
    assert seed.await_count == 1 and seed.await_args.args[1] == [1, 2]
    # Infections and the durable claim precede broadcast; true follows it.
    assert during_broadcast == [('"in_progress"', True, True)] * 2
    assert [call.args[0] for call in bot.send_message.await_args_list] == [OTHER_GROUP, GROUP]
    for call in bot.send_message.await_args_list:
        assert call.args[1] == format_html(admin_handler.DISEASE_START_ANNOUNCEMENT)
        assert call.kwargs == {"parse_mode": "HTML"}

    async with wave_db.begin() as session:
        assert (await session.get(SystemConfig, _WAVE_FLAG)).value == "true"
        # Make a player susceptible again: a second command must not reinfect.
        await session.delete(await session.get(UserDisease, 1))
    await admin_handler.handle_disease_start(second, bot)
    second.reply.assert_awaited_once_with(_WAVE_WARNING)
    assert seed.await_count == 1 and bot.send_message.await_count == 2
    async with wave_db() as session:
        assert not await disease.is_sick(session, 1)
        assert (await session.get(SystemConfig, _WAVE_FLAG)).value == "true"


@pytest.mark.parametrize("flag", ["true", '"in_progress"', "invalid"])
async def test_initial_wave_existing_or_interrupted_claim_refuses(wave_db, monkeypatch, flag):
    await _seed_wave(wave_db, flag)
    seed = AsyncMock()
    monkeypatch.setattr(disease, "process_initial_infection", seed)
    msg, bot = _message(99), _bot()
    await admin_handler.handle_disease_start(msg, bot)
    msg.reply.assert_awaited_once_with(_WAVE_WARNING)
    seed.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    async with wave_db() as session:
        assert (await session.get(SystemConfig, _WAVE_FLAG)).value == flag


@pytest.mark.parametrize("user_id", [98, None])
async def test_initial_wave_refuses_non_admin_without_accessing_db(wave_db, monkeypatch, user_id):
    monkeypatch.setattr(admin_handler, "get_session", Mock(side_effect=AssertionError("database accessed")))
    seed = AsyncMock()
    monkeypatch.setattr(disease, "process_initial_infection", seed)
    msg, bot = _message(user_id or 98), _bot()
    if user_id is None:
        msg.from_user = None
    await admin_handler.handle_disease_start(msg, bot)
    seed.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    msg.reply.assert_not_awaited()


@pytest.mark.parametrize("dialect_name", ["mysql", "mariadb"])
async def test_initial_wave_claim_compiles_for_mysql(dialect_name, monkeypatch):
    statements = []

    class Session:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))

        async def execute(self, statement):
            statements.append(str(statement.compile(
                dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
            )))
            return SimpleNamespace(rowcount=0)

    @asynccontextmanager
    async def session_context():
        yield Session()

    monkeypatch.setattr(admin_handler, "get_session", session_context)
    monkeypatch.setattr(admin_handler, "is_admin", lambda user_id: user_id == 99)
    msg, bot = _message(99), _bot()
    await admin_handler.handle_disease_start(msg, bot)
    msg.reply.assert_awaited_once_with(_WAVE_WARNING)
    bot.send_message.assert_not_awaited()
    assert "ON DUPLICATE KEY UPDATE" in statements[0]
    assert _WAVE_FLAG in statements[0] and _WAVE_FLAG in statements[1]
    assert "'false'" in statements[1] and "in_progress" in statements[1]


@pytest.mark.parametrize("flag", [None, "false"])
async def test_initial_wave_serializes_concurrent_claims_and_broadcasts(wave_db, monkeypatch, flag):
    await _seed_wave(wave_db, flag)
    infection_started, release_infection = asyncio.Event(), asyncio.Event()
    second_started, broadcast_started, release_broadcast = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sessions = 0

    @asynccontextmanager
    async def tracked_session():
        nonlocal sessions
        sessions += 1
        if sessions == 2:
            second_started.set()
        async with wave_db.begin() as session:
            yield session

    async def seed_once(session, ids):
        infection_started.set()
        await release_infection.wait()
        return 0

    async def send_once(*args, **kwargs):
        broadcast_started.set()
        await release_broadcast.wait()

    monkeypatch.setattr(admin_handler, "get_session", tracked_session)
    seed = AsyncMock(side_effect=seed_once)
    monkeypatch.setattr(disease, "process_initial_infection", seed)
    first_msg, second_msg, bot = _message(99), _message(99), _bot()
    bot.send_message.side_effect = send_once
    first = asyncio.create_task(admin_handler.handle_disease_start(first_msg, bot))
    second = None
    try:
        await asyncio.wait_for(infection_started.wait(), timeout=5)
        second = asyncio.create_task(admin_handler.handle_disease_start(second_msg, bot))
        await asyncio.wait_for(second_started.wait(), timeout=5)
        await asyncio.sleep(0.1)
        assert not second.done() and seed.await_count == 1
        release_infection.set()
        await asyncio.wait_for(broadcast_started.wait(), timeout=5)
        await asyncio.wait_for(second, timeout=5)
        second_msg.reply.assert_awaited_once_with(_WAVE_WARNING)
        assert seed.await_count == 1 and bot.send_message.await_count == 1
        async with wave_db() as session:
            assert (await session.get(SystemConfig, _WAVE_FLAG)).value == '"in_progress"'
        release_broadcast.set()
        await asyncio.wait_for(first, timeout=5)
    finally:
        release_infection.set()
        release_broadcast.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
    assert bot.send_message.await_count == 2
    async with wave_db() as session:
        assert (await session.get(SystemConfig, _WAVE_FLAG)).value == "true"


@pytest.mark.skipif(not _ROW_LOCK_DATABASE, reason="MySQL/MariaDB race requires a live server")
@pytest.mark.parametrize("flag", [None, "false"])
async def test_initial_wave_serializes_mysql_claims(db_session, monkeypatch, flag):
    from sib.database.connection import get_async_sessionmaker

    monkeypatch.setattr(admin_handler, "is_admin", lambda user_id: user_id == 99)
    await test_initial_wave_serializes_concurrent_claims_and_broadcasts(
        get_async_sessionmaker(), monkeypatch, flag
    )


@pytest.mark.parametrize("flag", [None, "false"])
async def test_initial_wave_infection_failure_rolls_back_claim(wave_db, monkeypatch, flag):
    await _seed_wave(wave_db, flag)

    async def fail(session, ids):
        session.add(UserDisease(user_id=1, infection_level=10))
        await session.flush()
        raise RuntimeError("infection failed")

    monkeypatch.setattr(disease, "process_initial_infection", fail)
    msg, bot = _message(99), _bot()
    with pytest.raises(RuntimeError, match="infection failed"):
        await admin_handler.handle_disease_start(msg, bot)
    bot.send_message.assert_not_awaited()
    msg.reply.assert_not_awaited()
    async with wave_db() as session:
        row = await session.get(SystemConfig, _WAVE_FLAG)
        assert (row.value if row else None) == flag
        assert not await disease.is_sick(session, 1)


async def test_initial_wave_broadcast_error_does_not_allow_a_repeat(wave_db, monkeypatch):
    await _seed_wave(wave_db, "false")
    broadcast = AsyncMock(side_effect=RuntimeError("ambiguous delivery"))
    monkeypatch.setattr(admin_handler, "_broadcast_to_active_groups", broadcast)
    first, second, bot = _message(99), _message(99), _bot()
    with pytest.raises(RuntimeError, match="ambiguous delivery"):
        await admin_handler.handle_disease_start(first, bot)
    async with wave_db() as session:
        assert (await session.get(SystemConfig, _WAVE_FLAG)).value == "true"
    await admin_handler.handle_disease_start(second, bot)
    second.reply.assert_awaited_once_with(_WAVE_WARNING)
    broadcast.assert_awaited_once()


def test_migration_073_is_idempotent_and_preserves_claims():
    from io import StringIO

    path = ROOT / "database/migrations/versions/073_disease_started_flag.py"
    spec = importlib.util.spec_from_file_location("migration_073", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("073_disease_started_flag", "072_disease_ui")
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE system_config (`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
                def value():
                    return connection.execute(sa.text(
                        "SELECT `value` FROM system_config WHERE `key`=:key"
                    ), {"key": _WAVE_FLAG}).scalar_one_or_none()

                assert value() == "false"
                for saved in ("true", '"in_progress"'):
                    connection.execute(sa.text("UPDATE system_config SET `value`=:value"), {"value": saved})
                    migration.upgrade()
                    migration.downgrade()
                    assert value() == saved
                connection.execute(sa.text("UPDATE system_config SET `value`='false'"))
                migration.downgrade()
                migration.downgrade()
                assert value() is None
    finally:
        engine.dispose()

    output = StringIO()
    context = MigrationContext.configure(dialect_name="mysql", opts={
        "as_sql": True, "output_buffer": output, "literal_binds": True,
    })
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    assert "ON DUPLICATE KEY UPDATE `key` = `key`" in sql
    assert "BINARY `value` = BINARY" in sql
    assert _WAVE_FLAG in sql
