"""Phase 4: disease effects, truthful outcomes and scheduled infection ticks."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TelegramUser
from sqlalchemy import event, func, select
from sqlalchemy.dialects import mysql

from sib.database.connection import get_async_sessionmaker
from sib.database.models import (
    AppleBattle,
    AppleDuel,
    AppleMineShift,
    AppleTransaction,
    AppleWallet,
    Group,
    GroupCastle,
    GroupWar,
    Inv2GroupMember,
    SystemConfig,
    User,
    UserDisease,
    WarParticipant,
)
from sib.handlers import apple_duel as duel_handler
from sib.handlers import apple_mine as mine_handler
from sib.handlers import castle as castle_handler
from sib.handlers import war as war_handler
from sib.services import apple_mine as mine
from sib.services import disease_service as disease
from sib.services.apple_duel import (
    AppleDuelService,
    DuelError,
    SICK_DUEL_MESSAGE,
    SICK_OPPONENT_MESSAGE,
)
from sib.services.apple_duel_presentation import AppleDuelPresentation, scene_text
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.garden import GardenService
from sib.services.items import ItemService
from sib.services.maintenance.service import (
    DISEASE_INFECTION_STATE_KEY,
    MaintenanceService,
)
from sib.services.seedling import SeedlingService
from sib.services.war_service import SICK_WAR_MESSAGE, WarError, WarService
from sib.tests.conftest import requires_row_locks
from sib.tests.test_apple_battle import _card
from sib.tests.test_apple_duel import accept, create_test_user as duel_user, setup_duel, telegram_message
from sib.tests.test_disease_ui import db
from sib.tests.test_parties import create_test_user
from sib.tests.test_war import war_session

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
DUEL_INFECTION_LINE = "⚠️ بازنده پس از دوئل به بیماری مبتلا شد."
MINE_WARNING = "⚠️ گرد و غبار معدن باعث شد به بیماری مبتلا شوی!"
WAR_REFUSAL = "🦠 شما بیمار هستید و نمی‌توانید در جنگ شرکت کنید. ابتدا درمان شوید."


def session_context(session):
    @asynccontextmanager
    async def context():
        yield session

    return context


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def test_garden_slows_only_new_first_fruit_and_keeps_maturation(db_session):
    user = await create_test_user(db_session, "garden disease")
    await SeedlingService.admin_adjust(db_session, user.id, 3, reason="test")
    await ConfigService.set(db_session, "garden_growth_seconds", 6 * 3600)
    await ConfigService.set(db_session, "garden_fruit_seconds", 6 * 3600)
    await ConfigService.set(db_session, "disease_demon_power_chance", 0)
    await db_session.commit()

    healthy = await GardenService.plant(db_session, user.id, 0)
    healthy_ready = utc(healthy.fruit_ready_at)
    assert healthy_ready - utc(healthy.planted_at) == timedelta(hours=12)
    assert utc(healthy.mature_at) - utc(healthy.planted_at) == timedelta(hours=6)

    assert await disease.infect_user(db_session, user.id, "test")
    sick = await GardenService.plant(db_session, user.id, 1)
    sick_ready = utc(sick.fruit_ready_at)
    assert sick_ready - utc(sick.planted_at) == timedelta(hours=18)
    assert utc(sick.mature_at) - utc(sick.planted_at) == timedelta(hours=6)
    assert utc(healthy.fruit_ready_at) == healthy_ready

    # Subsequent harvest cycles are not changed, even while still sick.
    harvest_at = sick_ready + timedelta(seconds=1)
    await GardenService.harvest(db_session, user.id, 1, now=harvest_at, rng=Mock(random=lambda: 0.0))
    next_ready = utc(sick.fruit_ready_at)
    assert next_ready - harvest_at == timedelta(hours=6)

    await disease.cure_user(db_session, user.id)
    assert utc(sick.fruit_ready_at) == next_ready
    cured = await GardenService.plant(db_session, user.id, 2)
    assert utc(cured.fruit_ready_at) - utc(cured.planted_at) == timedelta(hours=12)
    assert utc(healthy.fruit_ready_at) == healthy_ready


async def test_garden_sick_first_fruit_rounds_fractional_second_up(db_session):
    user = await create_test_user(db_session, "garden odd seconds")
    await SeedlingService.admin_adjust(db_session, user.id, 1, reason="test")
    await ConfigService.set(db_session, "garden_growth_seconds", 101)
    await ConfigService.set(db_session, "garden_fruit_seconds", 100)
    assert await disease.infect_user(db_session, user.id, "test")
    plot = await GardenService.plant(db_session, user.id, 0)
    assert utc(plot.mature_at) - utc(plot.planted_at) == timedelta(seconds=101)
    assert utc(plot.fruit_ready_at) - utc(plot.planted_at) == timedelta(seconds=302)


async def test_garden_user_and_disease_locks_precede_garden_locks(db_session):
    user = await create_test_user(db_session, "garden lock order")
    await SeedlingService.admin_adjust(db_session, user.id, 1, reason="test")
    await ItemService.grant(db_session, user.id, "magic_powder", quantity=1)
    await db_session.commit()
    locked_tables = []

    @event.listens_for(db_session.sync_session, "do_orm_execute")
    def record_locks(execution):
        statement = execution.statement
        if getattr(statement, "_for_update_arg", None) is not None:
            sql = str(statement.compile(dialect=mysql.dialect()))
            assert "FOR UPDATE" in sql
            locked_tables.append(statement.get_final_froms()[0].name)

    plot = await GardenService.plant(db_session, user.id, 0)
    assert locked_tables.index("users") < locked_tables.index("user_diseases")
    assert locked_tables.index("user_diseases") < locked_tables.index("gardens")

    for method, args in ((GardenService.harvest, (0,)), (GardenService.harvest_all, ())):
        plot.fruit_ready_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.flush()
        locked_tables.clear()
        await method(db_session, user.id, *args, rng=Mock(random=lambda: 0.0))
        assert locked_tables.index("users") < locked_tables.index("gardens")

    locked_tables.clear()
    await GardenService.apply_magic(db_session, user.id, 0)
    assert locked_tables.index("users") < locked_tables.index("gardens")


@requires_row_locks
async def test_garden_uses_current_disease_state_after_mysql_lock_wait(db_session):
    user = await create_test_user(db_session, "garden infection race")
    await SeedlingService.admin_adjust(db_session, user.id, 1, reason="test")
    await ConfigService.set(db_session, "garden_growth_seconds", 6 * 3600)
    await ConfigService.set(db_session, "garden_fruit_seconds", 6 * 3600)
    await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()
    infected, stale_read, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    factory = get_async_sessionmaker()

    async def first():
        async with factory.begin() as session:
            assert await disease.infect_user(session, user.id, "test")
            infected.set()
            await release.wait()

    async def second():
        async with factory.begin() as session:
            # Pin a repeatable-read snapshot before the infection commits.
            assert not await disease.is_sick(session, user.id)
            stale_read.set()
            plot = await GardenService.plant(session, user.id, 0)
            return utc(plot.fruit_ready_at) - utc(plot.planted_at)

    first_task = asyncio.create_task(first())
    second_task = None
    try:
        await asyncio.wait_for(infected.wait(), timeout=10)
        second_task = asyncio.create_task(second())
        await asyncio.wait_for(stale_read.wait(), timeout=10)
        await asyncio.sleep(0.2)
        assert not second_task.done()
        release.set()
        _, delay = await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=10)
        assert delay == timedelta(hours=18)
    finally:
        release.set()
        for task in (first_task, second_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first_task, second_task) if task), return_exceptions=True)


async def test_war_blocks_sick_declarer_and_implicit_new_enrollment(war_session):
    s = war_session
    assert SICK_WAR_MESSAGE == WAR_REFUSAL
    assert await disease.infect_user(s, 1, "test")
    with pytest.raises(WarError) as error:
        await WarService.declare_war(s, -1001, -1002, user_id=1)
    assert str(error.value) == WAR_REFUSAL
    assert await s.scalar(select(func.count()).select_from(GroupWar)) == 0

    await disease.cure_user(s, 1)
    war = await WarService.declare_war(s, -1001, -1002, user_id=1)
    assert await disease.infect_user(s, 2, "test")
    with pytest.raises(WarError) as error:
        await WarService.get_or_create_participant(s, war.id, 2, -1002)
    assert str(error.value) == WAR_REFUSAL
    assert await s.get(WarParticipant, (war.id, 2)) is None
    await disease.cure_user(s, 2)
    assert (await WarService.get_or_create_participant(s, war.id, 2, -1002)).energy == 100


async def test_war_already_enrolled_player_cannot_attack_when_sick(war_session):
    s = war_session
    war = await WarService.declare_war(s, -1001, -1002, user_id=1)
    participant = await WarService.get_or_create_participant(s, war.id, 1, -1001)
    assert await disease.infect_user(s, 1, "test")
    with pytest.raises(WarError) as error:
        await WarService.attack(s, war.id, 1, -1001)
    assert str(error.value) == WAR_REFUSAL
    assert participant.energy == 100 and participant.total_damage == 0
    with pytest.raises(WarError) as error:
        await WarService.use_healing_potion(s, war.id, 1, -1001)
    assert str(error.value) == WAR_REFUSAL
    assert await ItemService.get_total(s, 1, "healing_potion") == 0


@pytest.mark.parametrize("action", ["war_start", "war_break_shield_confirm", "war_confirm"])
async def test_castle_war_actions_show_refusal_without_breaking_shield(
    war_session, monkeypatch, action
):
    s = war_session
    castle = await s.get(GroupCastle, -1001)
    castle.shield_until = datetime.now(timezone.utc) + timedelta(hours=1)
    await disease.infect_user(s, 1, "test")
    await s.commit()
    monkeypatch.setattr(castle_handler, "get_session", session_context(s))
    answer = AsyncMock()
    monkeypatch.setattr(castle_handler, "safe_answer_callback", answer)
    edit = AsyncMock()
    monkeypatch.setattr(castle_handler, "safe_edit_callback_message", edit)
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")))
    message = Message(message_id=1, date=NOW, chat=Chat(id=-1001, type="supergroup"))
    data = f"castle:{action}:-1001" + (":-1002" if action == "war_confirm" else "")
    callback = CallbackQuery(
        id="test", from_user=TelegramUser(id=1, first_name="مهاجم اول", is_bot=False),
        chat_instance="test", message=message, data=data,
    ).as_(bot)
    await castle_handler.castle_callback(callback)
    answer.assert_awaited_once_with(callback, WAR_REFUSAL, show_alert=True)
    edit.assert_not_awaited()
    assert castle.shield_until is not None
    assert await s.scalar(select(func.count()).select_from(GroupWar)) == 0


async def test_war_status_and_callback_render_refusal(war_session, monkeypatch):
    s = war_session
    war = await WarService.declare_war(s, -1001, -1002, user_id=1)
    await disease.infect_user(s, 2, "test")
    monkeypatch.setattr(war_handler, "get_session", session_context(s))
    message = SimpleNamespace(answer=AsyncMock())
    await war_handler.send_war_status(message, -1002, 2)
    message.answer.assert_awaited_once_with(WAR_REFUSAL)
    assert await s.get(WarParticipant, (war.id, 2)) is None

    callback = SimpleNamespace(bot=object())
    answer = AsyncMock()
    monkeypatch.setattr(war_handler, "safe_answer_callback", answer)
    assert not await war_handler.edit_war_status(callback, -1002, 2)
    answer.assert_awaited_once_with(callback, WAR_REFUSAL, show_alert=True)


async def test_new_war_status_viewer_is_registered_before_enrollment(war_session, monkeypatch):
    s = war_session
    war = await WarService.declare_war(s, -1001, -1002, user_id=1)
    monkeypatch.setattr(war_handler, "get_session", session_context(s))
    message = SimpleNamespace(
        from_user=TelegramUser(id=55, first_name="تازه‌وارد", is_bot=False),
        chat=Chat(id=-1001, type="supergroup"), answer=AsyncMock(), answer_photo=AsyncMock(),
    )
    await war_handler.war_status_entry(message)
    assert await s.get(User, 55) is not None
    assert await s.get(WarParticipant, (war.id, 55)) is not None
    assert message.answer.await_count + message.answer_photo.await_count == 1


async def test_duel_start_sick_refusal_service_and_message(db_session, monkeypatch):
    a = await duel_user(db_session, 701)
    b = await duel_user(db_session, 702)
    await _card(db_session, a, "duel_a")
    await _card(db_session, b, "duel_b")
    await disease.infect_user(db_session, a.id, "test")
    await db_session.commit()
    with pytest.raises(DuelError) as error:
        await AppleDuelService.create(db_session, -701, a.id, b.id)
    assert str(error.value) == SICK_DUEL_MESSAGE
    assert await db_session.scalar(select(func.count()).select_from(AppleDuel)) == 0

    original = telegram_message()
    message = SimpleNamespace(
        chat=original.chat, from_user=original.from_user, sender_chat=None,
        reply_to_message=original.reply_to_message, reply=AsyncMock(),
    )
    monkeypatch.setattr(duel_handler, "get_session", session_context(db_session))
    await duel_handler.command_duel(message, bot=object())
    message.reply.assert_awaited_once_with(SICK_DUEL_MESSAGE, parse_mode=None)


@pytest.mark.parametrize("sick_player", ["recipient", "inviter"])
async def test_duel_accept_checks_both_and_keeps_rejection_available(
    db_session, monkeypatch, sick_player
):
    duel, a, b, *_ = await setup_duel(db_session)
    sick_id = b.id if sick_player == "recipient" else a.id
    await disease.infect_user(db_session, sick_id, "test")
    await db_session.commit()
    expected = SICK_DUEL_MESSAGE if sick_player == "recipient" else SICK_OPPONENT_MESSAGE
    with pytest.raises(DuelError) as error:
        await accept(db_session, duel)
    assert str(error.value) == expected
    assert duel.status == "PENDING" and duel.battle_id is None
    assert await db_session.scalar(select(func.count()).select_from(AppleBattle)) == 0

    message = Message(message_id=90, date=NOW, chat=Chat(id=-701, type="supergroup"))
    callback = CallbackQuery(
        id="test", from_user=TelegramUser(id=b.id, first_name="علی", is_bot=False),
        chat_instance="test", message=message, data=f"ad:accept:{duel.id}",
    )
    answer = AsyncMock()
    monkeypatch.setattr(duel_handler, "get_session", session_context(db_session))
    monkeypatch.setattr(duel_handler, "safe_answer_callback", answer)
    await duel_handler.callback_duel(callback, bot=object())
    answer.assert_awaited_once_with(callback, expected, show_alert=True)
    rejected = await AppleDuelService.act(db_session, duel.id, b.id, -701, "reject", message_id=90)
    assert rejected.status == "REJECTED"


async def test_duel_infection_is_loser_only_durable_and_idempotent(db_session, monkeypatch):
    duel, a, b, *_ = await setup_duel(db_session)
    await ConfigService.set(db_session, "disease_duel_infection_chance", 5)
    await ConfigService.set(db_session, "disease_demon_power_chance", 0)
    rolls = Mock(side_effect=[0.049, 0.99])
    monkeypatch.setattr(disease.random, "random", rolls)
    infect = AsyncMock(wraps=disease.infect_user)
    monkeypatch.setattr(disease, "infect_user", infect)
    await accept(db_session, duel)
    await db_session.commit()
    battle = await db_session.get(AppleBattle, duel.battle_id)
    loser = b.id if battle.winner_id == a.id else a.id
    infect.assert_awaited_once_with(db_session, loser, "duel")
    assert await disease.is_sick(db_session, loser)
    assert not await disease.is_sick(db_session, battle.winner_id)
    assert json.loads(duel.snapshot_json)["disease_infected_user_id"] == loser
    assert DUEL_INFECTION_LINE in scene_text(duel, battle, 5)
    assert all(DUEL_INFECTION_LINE not in scene_text(duel, battle, i) for i in range(5))

    # Replayed acceptance and a fresh session see the same stored decision.
    await accept(db_session, duel)
    infect.assert_awaited_once()
    assert rolls.call_count == 2
    async with get_async_sessionmaker()() as fresh:
        persisted = await fresh.get(AppleDuel, duel.id)
        persisted_battle = await fresh.get(AppleBattle, persisted.battle_id)
        assert DUEL_INFECTION_LINE in scene_text(persisted, persisted_battle, 5)
        assert await disease.is_sick(fresh, loser)

    # The last scene and the durable terminal board both deliver that result.
    duel.cursor = 5
    duel.next_scene_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    monkeypatch.setattr(AppleDuelPresentation, "_photo", AsyncMock(return_value=None))
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
    await AppleDuelPresentation.tick(bot, duel.id)
    assert DUEL_INFECTION_LINE in bot.send_message.await_args.args[1]
    assert DUEL_INFECTION_LINE in bot.edit_message_text.await_args.args[0]
    infect.assert_awaited_once()


async def test_duel_default_five_percent_boundary_misses(db_session, monkeypatch):
    duel, *_ = await setup_duel(db_session)
    rolls = Mock(return_value=0.05)
    monkeypatch.setattr(disease.random, "random", rolls)
    infect = AsyncMock(wraps=disease.infect_user)
    monkeypatch.setattr(disease, "infect_user", infect)
    await accept(db_session, duel)
    battle = await db_session.get(AppleBattle, duel.battle_id)
    rolls.assert_called_once()
    infect.assert_not_awaited()
    assert "disease_infected_user_id" not in json.loads(duel.snapshot_json)
    assert DUEL_INFECTION_LINE not in scene_text(duel, battle, 5)


@pytest.mark.parametrize("reason", ["immune", "disabled"])
async def test_duel_never_reports_a_failed_infection(db_session, monkeypatch, reason):
    duel, a, b, *_ = await setup_duel(db_session)
    if reason == "immune":
        await disease.cure_user(db_session, a.id)
        await disease.cure_user(db_session, b.id)
    else:
        await ConfigService.set(db_session, "disease_enabled", False)
    await ConfigService.set(db_session, "disease_duel_infection_chance", 100)
    monkeypatch.setattr(disease.random, "random", lambda: 0.0)
    infect = AsyncMock(wraps=disease.infect_user)
    monkeypatch.setattr(disease, "infect_user", infect)
    await accept(db_session, duel)
    battle = await db_session.get(AppleBattle, duel.battle_id)
    infect.assert_awaited_once()
    assert not await disease.is_sick(db_session, a.id)
    assert not await disease.is_sick(db_session, b.id)
    assert DUEL_INFECTION_LINE not in scene_text(duel, battle, 5)
    assert "disease_infected_user_id" not in json.loads(duel.snapshot_json)


async def test_duel_infection_failure_rolls_back_settlement(db_session, monkeypatch):
    duel, *_ = await setup_duel(db_session)
    await ConfigService.set(db_session, "disease_duel_infection_chance", 100)
    monkeypatch.setattr(disease, "_roll", lambda chance: True)
    monkeypatch.setattr(disease, "infect_user", AsyncMock(side_effect=RuntimeError("infection failed")))
    with pytest.raises(RuntimeError, match="infection failed"):
        await accept(db_session, duel)
    await db_session.refresh(duel)
    assert duel.status == "PENDING" and duel.battle_id is None
    assert await db_session.scalar(select(func.count()).select_from(AppleBattle)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AppleTransaction)) == 0


async def setup_miner(session, *, infection_chance=None, plant_chance=0):
    user = await create_test_user(session, "معدنچی")
    user_id = user.id
    if infection_chance is not None:
        await ConfigService.set(session, "disease_mine_infection_chance", infection_chance)
    await ConfigService.set(session, "disease_mine_plant_drop_chance", plant_chance)
    await ConfigService.set(session, "disease_demon_power_chance", 0)
    await session.commit()
    return user_id


async def test_mine_default_roll_infects_and_preserves_plant_and_reward(db_session, monkeypatch):
    uid = await setup_miner(db_session, plant_chance=100)
    monkeypatch.setattr(mine, "_roll_shift", lambda _: ("normal", "apple_normal", 10))
    rolls = Mock(side_effect=[0.0, 0.019, 0.99])  # plant, infection, demon power
    monkeypatch.setattr(mine.random, "random", rolls)
    infect = AsyncMock(wraps=disease.infect_user)
    monkeypatch.setattr(disease, "infect_user", infect)
    result = await mine.perform_shift(db_session, user_id=uid, group_id=None)
    await db_session.commit()
    infect.assert_awaited_once_with(db_session, uid, "mine")
    assert rolls.call_count == 3
    assert result.plant_dropped and result.amount == 10
    assert MINE_WARNING in mine_handler._result_caption(result)
    assert await disease.is_sick(db_session, uid)
    assert await ItemService.get_total(db_session, uid, "disease_plant") == 1
    assert await AppleWalletService.get_balance(db_session, uid) == 10
    assert await db_session.scalar(select(func.count()).select_from(AppleMineShift)) == 1
    with pytest.raises(mine.CooldownError):
        await mine.perform_shift(db_session, user_id=uid, group_id=None)
    infect.assert_awaited_once()


async def test_mine_default_two_percent_boundary_misses(db_session, monkeypatch):
    uid = await setup_miner(db_session)
    monkeypatch.setattr(mine, "_roll_shift", lambda _: ("normal", "apple_normal", 10))
    rolls = Mock(return_value=0.02)
    monkeypatch.setattr(mine.random, "random", rolls)
    infect = AsyncMock(wraps=disease.infect_user)
    monkeypatch.setattr(disease, "infect_user", infect)
    result = await mine.perform_shift(db_session, user_id=uid, group_id=None)
    rolls.assert_called_once()
    infect.assert_not_awaited()
    assert MINE_WARNING not in mine_handler._result_caption(result)
    assert not await disease.is_sick(db_session, uid)


@pytest.mark.parametrize("reason", ["immune", "disabled", "already_sick"])
async def test_mine_never_warns_when_no_new_infection(db_session, monkeypatch, reason):
    uid = await setup_miner(db_session, infection_chance=100)
    if reason == "immune":
        await disease.cure_user(db_session, uid)
    elif reason == "disabled":
        await ConfigService.set(db_session, "disease_enabled", False)
    else:
        await disease.infect_user(db_session, uid, "test")
    monkeypatch.setattr(mine, "_roll_shift", lambda _: ("normal", "apple_normal", 10))
    monkeypatch.setattr(mine.random, "random", lambda: 0.0)
    infect = AsyncMock(wraps=disease.infect_user)
    monkeypatch.setattr(disease, "infect_user", infect)
    result = await mine.perform_shift(db_session, user_id=uid, group_id=None)
    infect.assert_awaited_once_with(db_session, uid, "mine")
    assert MINE_WARNING not in mine_handler._result_caption(result)
    assert await disease.is_sick(db_session, uid) is (reason == "already_sick")


async def test_mine_infection_failure_rolls_back_shift_plant_and_reward(db_session, monkeypatch):
    uid = await setup_miner(db_session, infection_chance=100, plant_chance=100)
    monkeypatch.setattr(mine, "_roll_shift", lambda _: ("normal", "apple_normal", 10))
    monkeypatch.setattr(mine.random, "random", lambda: 0.0)
    monkeypatch.setattr(disease, "infect_user", AsyncMock(side_effect=RuntimeError("infection failed")))
    with pytest.raises(RuntimeError, match="infection failed"):
        await mine.perform_shift(db_session, user_id=uid, group_id=None)
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(AppleMineShift)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AppleTransaction)) == 0
    assert await db_session.get(AppleWallet, uid) is None
    assert await ItemService.get_total(db_session, uid, "disease_plant") == 0


async def seed_activity(db, *, now=NOW):
    async with db.begin() as session:
        session.add_all([
            Group(id=-1, title="active"), Group(id=-2, title="also active"),
            Group(id=-3, title="inactive", is_active=False),
        ])
        session.add_all([
            User(id=1, first_name="recent"), User(id=2, first_name="boundary"),
            User(id=3, first_name="old"), User(id=4, first_name="future"),
            User(id=5, first_name="banned", is_banned=True),
            User(id=6, first_name="bot", is_bot=True),
            User(id=7, first_name="inactive", is_active=False),
            User(id=8, first_name="private only"), User(id=9, first_name="inactive group"),
        ])
        await session.flush()
        session.add_all([
            Inv2GroupMember(user_id=uid, group_id=gid, last_seen_at=seen)
            for uid, gid, seen in [
                (1, -1, now), (1, -2, now - timedelta(hours=1)),
                (2, -1, now - timedelta(hours=24)),
                (3, -1, now - timedelta(hours=24, seconds=1)),
                (4, -1, now + timedelta(seconds=1)), (5, -1, now),
                (6, -1, now), (7, -1, now), (9, -3, now),
            ]
        ])


async def test_six_hour_tick_selects_distinct_recent_active_users_and_survives_restart(db, monkeypatch):
    await seed_activity(db)
    monkeypatch.setattr(MaintenanceService, "_last_disease_infection_check_at", None)
    tick = AsyncMock(return_value=2)
    monkeypatch.setattr(disease, "process_daily_infection_tick", tick)
    assert await MaintenanceService.run_disease_infection_tick(now=NOW) == 2
    assert tick.await_args.args[1] == [1, 2]
    assert await MaintenanceService.run_disease_infection_tick(now=NOW + timedelta(seconds=30)) == 0
    tick.assert_awaited_once()

    # A new worker/restart must honor the committed shared timestamp too.
    monkeypatch.setattr(MaintenanceService, "_last_disease_infection_check_at", None)
    assert await MaintenanceService.run_disease_infection_tick(now=NOW + timedelta(hours=5, minutes=59)) == 0
    tick.assert_awaited_once()
    assert await MaintenanceService.run_disease_infection_tick(now=NOW + timedelta(hours=6)) == 2
    assert tick.await_count == 2
    assert tick.await_args.args[1] == [1, 4]  # boundary expired; future observation is now current
    async with db() as session:
        state = await session.get(SystemConfig, DISEASE_INFECTION_STATE_KEY)
        assert datetime.fromisoformat(json.loads(state.value)) == NOW + timedelta(hours=6)


async def test_tick_really_infects_but_respects_immunity(db, monkeypatch):
    await seed_activity(db)
    async with db.begin() as session:
        await ConfigService.set(session, "disease_daily_infection_chance", 100)
        await ConfigService.set(session, "disease_demon_power_chance", 0)
        await disease.cure_user(session, 2)
    monkeypatch.setattr(MaintenanceService, "_last_disease_infection_check_at", None)
    monkeypatch.setattr(disease.random, "random", lambda: 0.0)
    assert await MaintenanceService.run_disease_infection_tick(now=NOW) == 1
    async with db() as session:
        assert await disease.is_sick(session, 1)
        assert not await disease.is_sick(session, 2)


async def test_concurrent_workers_share_one_tick(db, monkeypatch):
    await seed_activity(db)
    monkeypatch.setattr(MaintenanceService, "_last_disease_infection_check_at", None)

    async def slow_tick(session, ids):
        await asyncio.sleep(0.05)
        return len(ids)

    tick = AsyncMock(side_effect=slow_tick)
    monkeypatch.setattr(disease, "process_daily_infection_tick", tick)
    results = await asyncio.gather(
        MaintenanceService.run_disease_infection_tick(now=NOW),
        MaintenanceService.run_disease_infection_tick(now=NOW),
    )
    assert sorted(results) == [0, 2]
    tick.assert_awaited_once()


async def test_tick_failure_rolls_back_and_is_retried(db, monkeypatch):
    await seed_activity(db)
    monkeypatch.setattr(MaintenanceService, "_last_disease_infection_check_at", None)
    monkeypatch.setattr(disease.random, "random", lambda: 0.9)

    async def fail_after_infection(session, ids):
        assert await disease.infect_user(session, ids[0], "daily_tick")
        raise RuntimeError("tick failed")

    monkeypatch.setattr(disease, "process_daily_infection_tick", fail_after_infection)
    with pytest.raises(RuntimeError, match="tick failed"):
        await MaintenanceService.run_disease_infection_tick(now=NOW)
    async with db() as session:
        assert await session.get(UserDisease, 1) is None
        assert await session.get(SystemConfig, DISEASE_INFECTION_STATE_KEY) is None
    tick = AsyncMock(return_value=0)
    monkeypatch.setattr(disease, "process_daily_infection_tick", tick)
    assert await MaintenanceService.run_disease_infection_tick(now=NOW) == 0
    tick.assert_awaited_once()
