"""IV2 balance, real shop/inventory, atomic buff joins and reward replays.

All Telegram calls are mocked; these tests never contact the production DB.
SQLite checks transactions/retries, not MariaDB's FOR UPDATE concurrency.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from sqlalchemy import Integer, event, false, func, select

from sib.database.models import (
    AppleTransaction, AppleWallet, Base, BotAsset, Inv2Attack, Inv2Invasion,
    Inv2Reward, PlayerCard, SeedlingAward, SeedlingInventory, SystemConfig,
)
from sib.handlers import inv2_join, shop
from sib.services.apple_wallet import AppleWalletService
from sib.services.assets import AssetService, bundled_item_path
from sib.services.config_store import (
    CONFIG_DEFAULTS, ConfigService, DEFAULT_INVASION_DIFFICULTY_MULTIPLIER,
    DEFAULT_INVASION_INTERVAL_SECONDS, DEFAULT_INVASION_JOIN_WINDOW_SECONDS,
)
from sib.services.invasion_v2 import narrative, narrative_pool, presentation, rewards_service
from sib.services.invasion_v2.enums import RewardType
from sib.services.invasion_v2.rewards import roll_rewards
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.seedling import SeedlingService
from sib.tests.invasion_v2.test_presentation_phase5 import db, callback, create_inv
from sib.tests.invasion_v2.test_rules import FixedRNG


ALL_REWARDS = [
    (RewardType.GOLDEN_APPLE, 5), (RewardType.DIAMOND_APPLE, 1),
    (RewardType.OBSIDIAN_APPLE, 1), (RewardType.SEEDLING, 1),
]


def _buff_callback(inv_id, **kwargs):
    return callback(inv_id, **kwargs).model_copy(update={"data": f"inv2:joinbuff:{inv_id}"})


async def _armed(factory, *, hp=500, buffs=2):
    inv_id = await create_inv(factory, status="ACTIVE", hp=hp, message_id=123)
    async with factory.begin() as session:
        session.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
        if buffs is not None:
            await ItemService.grant(session, 1, "sib_buff", buffs)
    return inv_id


@pytest.fixture
def ui(monkeypatch):
    delivered = asyncio.Event()
    ack = AsyncMock()
    joined = AsyncMock(side_effect=lambda *args: delivered.set())
    finalized = AsyncMock()
    monkeypatch.setattr(inv2_join, "_answer", ack)
    monkeypatch.setattr(presentation.InvasionPresentation, "on_join", joined)
    monkeypatch.setattr(presentation.InvasionPresentation, "finalize", finalized)
    return SimpleNamespace(ack=ack, joined=joined, finalized=finalized, delivered=delivered)


@pytest.mark.asyncio
async def test_invasion_config_updated(db):
    factory, _ = db
    expected = {
        "invasion_difficulty_multiplier": 2.0,
        "invasion_join_window_seconds": 1800,
        "invasion_interval_seconds": 14400,
    }
    assert (DEFAULT_INVASION_DIFFICULTY_MULTIPLIER, DEFAULT_INVASION_JOIN_WINDOW_SECONDS,
            DEFAULT_INVASION_INTERVAL_SECONDS) == (2.0, 1800, 14400)
    async with factory() as session:
        for key, value in expected.items():
            assert CONFIG_DEFAULTS[key] == ConfigService.default(key) == value
            assert await ConfigService.get(session, key) == value
            assert await session.get(SystemConfig, key) is None  # A read never seeds a row.


@pytest.mark.parametrize("draws,expected", [
    ((.149999, .049999, .099999), ALL_REWARDS),
    ((.15, .05, .10), ALL_REWARDS[:1]),
    ((.0, .9, .9), ALL_REWARDS[:2]),
    ((.9, .0, .9), [ALL_REWARDS[0], ALL_REWARDS[2]]),
    ((.9, .9, .0), [ALL_REWARDS[0], ALL_REWARDS[3]]),
])
def test_invasion_rewards_include_seedling_and_higher_chances(draws, expected):
    rng = FixedRNG(*draws)
    assert roll_rewards(rng) == expected
    with pytest.raises(StopIteration):
        rng.random()  # Exactly three independent rolls, including when all miss.


@pytest.mark.asyncio
async def test_rewards_and_seedling_ledger_granted_exactly_once(db, monkeypatch):
    factory, _ = db
    inv_id = await _armed(factory)
    async with factory.begin() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        inv.status = "VICTORY"
        session.add(Inv2Attack(invasion_id=inv_id, user_id=1, card_id=101, card_power=20,
                              damage=500, is_crit=False, narrative="پیروزی", joined_at=datetime.now(timezone.utc)))
    rolls = Mock(return_value=ALL_REWARDS)
    monkeypatch.setattr(rewards_service, "roll_rewards", rolls)
    for _ in range(3):  # Fresh transactions model callbacks and recovery retries.
        async with factory.begin() as session:
            await rewards_service.settle_outcome(session, inv_id, "VICTORY")
            await rewards_service.grant_victory_rewards(session, inv_id)
    rolls.assert_called_once()
    async with factory() as session:
        assert await ItemService.get_total(session, 1, "apple_gold") == 5
        assert await ItemService.get_total(session, 1, "apple_diamond") == 1
        assert await ItemService.get_total(session, 1, "apple_obsidian") == 1
        assert await ItemService.get_total(session, 1, "seedling") == 0
        assert (await session.get(SeedlingInventory, 1)).total == 1
        awards = (await session.scalars(select(SeedlingAward))).all()
        assert len(awards) == 1
        assert (awards[0].source, awards[0].source_ref_id, awards[0].amount) == ("invasion_v2", inv_id, 1)
        rewards = (await session.scalars(select(Inv2Reward))).all()
        assert {(row.reward_type, row.reward_amount) for row in rewards} == {
            (kind.value, amount) for kind, amount in ALL_REWARDS}
    # The seedling service's own source key also protects a direct replay.
    async with factory.begin() as session:
        assert await SeedlingService.award_invasion_victory(session, 1, 1, invasion_id=inv_id) == 1
    bot = SimpleNamespace(edit_message_caption=AsyncMock(), send_photo=AsyncMock(return_value=True))
    await presentation.InvasionPresentation().finalize(bot, inv_id)
    assert "نهال ×۱" in bot.send_photo.await_args.kwargs["caption"]


@pytest.fixture
async def shop_db(db, monkeypatch):
    factory, get_session = db
    columns = [AppleTransaction.__table__.c.id, BotAsset.__table__.c.id]
    old_types = [column.type for column in columns]
    for column in columns:
        column.type = Integer()
    try:
        async with factory.kw["bind"].begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=[
                AppleWallet.__table__, AppleTransaction.__table__, BotAsset.__table__])
        async with factory.begin() as session:
            session.add(AppleWallet(user_id=1, balance=100))
        monkeypatch.setattr(shop, "get_session", get_session)
        yield factory, get_session
    finally:
        for column, old_type in zip(columns, old_types):
            column.type = old_type


@pytest.mark.asyncio
async def test_sib_buff_is_in_shop(shop_db):
    factory, get_session = shop_db
    assert ITEM_CATALOG["sib_buff"]["label"] == "سیب باف"
    assert ITEM_CATALOG["sib_buff"]["description"] == "با خوردن این سیب، ضربه‌ی بعدی تو در تهاجم ۲ برابر آسیب می‌زند."
    text, keyboard = await shop._goods_view(1)
    assert "سیب باف" in text
    assert "shop_good:sib_buff" in [button.callback_data for row in keyboard.inline_keyboard for button in row]
    detail, keyboard, photo = await shop._good_view(1, "sib_buff")
    assert "سیب باف" in detail and "۲ برابر" in detail
    assert keyboard.inline_keyboard[0][0].callback_data == "shop:buy:sib_buff:1"
    assert Path(photo.path) == bundled_item_path("sib_buff")
    async with get_session() as session:
        assert await ItemService.buy_price(session, "sib_buff") == 50
        assert await ItemService.buy(session, 1, "sib_buff") == 1
    async with factory() as session:
        assert await AppleWalletService.get_balance(session, 1) == 50
        assert await ItemService.get_total(session, 1, "sib_buff") == 1
        assert (await session.scalar(select(AppleTransaction))).amount == -50


def test_sib_buff_image_is_bundled_square_png():
    path = bundled_item_path("sib_buff")
    assert path is not None and path.name == "sib_buff.png"
    with Image.open(path) as image:
        assert image.format == "PNG" and image.size == (1024, 1024)


@pytest.mark.asyncio
async def test_registered_sib_buff_art_still_overrides_bundled_png(shop_db):
    factory, _ = shop_db
    async with factory.begin() as session:
        await AssetService.register(session, "sib_buff", "replacement-file")
    async with factory() as session:
        assert await AssetService.resolve_photo(session, "sib_buff") == ("replacement-file", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("normal_damage,is_crit", [(21, False), (42, True)])
async def test_invasion_join_with_buff_consumes_item_and_doubles_damage(db, ui, monkeypatch, normal_damage, is_crit):
    factory, _ = db
    inv_id = await _armed(factory)
    damage = Mock(return_value=(normal_damage, is_crit))
    monkeypatch.setattr(inv2_join, "compute_damage", damage)
    cb = _buff_callback(inv_id)
    # Exercise router registration as well as the handler's business logic.
    await inv2_join.router.callback_query.trigger(cb, bot=SimpleNamespace())
    await asyncio.wait_for(ui.delivered.wait(), timeout=2)
    damage.assert_called_once()
    assert damage.call_args.args[0] == 20
    async with factory() as session:
        attack = await session.scalar(select(Inv2Attack))
        assert (attack.card_power, attack.damage, attack.is_crit) == (20, normal_damage * 2, is_crit)
        assert "سیب باف" in attack.narrative and "۲ برابر" in attack.narrative
        assert "داریوش" in attack.narrative
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 500 - normal_damage * 2
        assert await ItemService.get_total(session, 1, "sib_buff") == 1
    assert "سیب باف مصرف شد" in ui.ack.await_args.args[1]
    # Either callback for the same user/invasion shares the unique attack key.
    for retry in (cb, cb, callback(inv_id)):
        await inv2_join.handle_inv2_join(retry, SimpleNamespace())
        assert "قبلاً" in ui.ack.await_args.args[1]
    async with factory() as session:
        assert await ItemService.get_total(session, 1, "sib_buff") == 1
        assert await session.scalar(select(func.count()).select_from(Inv2Attack)) == 1
    ui.joined.assert_awaited_once()
    ui.finalized.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("buffs", [None, 0])
async def test_invasion_join_with_buff_fails_without_item(db, ui, buffs):
    factory, _ = db
    inv_id = await _armed(factory, buffs=buffs)
    cb = _buff_callback(inv_id)
    await inv2_join.handle_inv2_join(cb, SimpleNamespace())
    ui.ack.assert_awaited_once_with(cb, "شما سیب باف ندارید! از فروشگاه تهیه کنید.", alert=True)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2Attack)) == 0
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 500
        assert await ItemService.get_total(session, 1, "sib_buff") == 0
    ui.joined.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_join_never_consumes_or_applies_buff(db, ui, monkeypatch):
    factory, _ = db
    inv_id = await _armed(factory)
    monkeypatch.setattr(inv2_join, "compute_damage", Mock(return_value=(21, False)))
    await inv2_join.handle_inv2_join(callback(inv_id), SimpleNamespace())
    await asyncio.wait_for(ui.delivered.wait(), timeout=2)
    await inv2_join.handle_inv2_join(_buff_callback(inv_id), SimpleNamespace())
    assert "قبلاً" in ui.ack.await_args.args[1]
    async with factory() as session:
        assert (await session.scalar(select(Inv2Attack))).damage == 21
        assert await ItemService.get_total(session, 1, "sib_buff") == 2
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 479


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["expired", "wrong_message", "no_card", "invalid_card", "cas_conflict", "disabled"])
async def test_buff_join_refusal_never_consumes_item(db, ui, monkeypatch, reason):
    factory, _ = db
    inv_id = await _armed(factory)
    cb = _buff_callback(inv_id, message_id=999 if reason == "wrong_message" else 123)
    async with factory.begin() as session:
        if reason == "expired":
            (await session.get(Inv2Invasion, inv_id)).ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif reason == "no_card":
            (await session.get(PlayerCard, 101)).is_favorite = False
        elif reason == "invalid_card":
            (await session.get(PlayerCard, 101)).level = 99
    if reason == "cas_conflict":
        original = inv2_join.update
        monkeypatch.setattr(inv2_join, "update", lambda model: original(model).where(false()))
    if reason == "disabled":
        monkeypatch.setattr(inv2_join.MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))
    await inv2_join.handle_inv2_join(cb, SimpleNamespace())
    assert ui.ack.await_args.kwargs["alert"] is True
    async with factory() as session:
        assert await ItemService.get_total(session, 1, "sib_buff") == 2
        assert await session.scalar(select(func.count()).select_from(Inv2Attack)) == 0
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 500
    ui.joined.assert_not_awaited()


@pytest.mark.asyncio
async def test_buff_attack_insert_failure_rolls_back_consumption_and_hp(db, ui):
    factory, _ = db
    inv_id = await _armed(factory)
    engine = factory.kw["bind"].sync_engine

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO inv2_attacks"):
            raise RuntimeError("simulated attack persistence failure")

    event.listen(engine, "before_cursor_execute", fail_insert)
    try:
        await inv2_join.handle_inv2_join(_buff_callback(inv_id), SimpleNamespace())
    finally:
        event.remove(engine, "before_cursor_execute", fail_insert)
    async with factory() as session:
        assert await ItemService.get_total(session, 1, "sib_buff") == 2
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 500
        assert await session.scalar(select(func.count()).select_from(Inv2Attack)) == 0
    assert "ممکن نشد" in ui.ack.await_args.args[1]
    ui.joined.assert_not_awaited()


@pytest.mark.asyncio
async def test_buff_kill_reward_failure_rolls_back_everything_then_retries_once(db, ui, monkeypatch):
    factory, _ = db
    inv_id = await _armed(factory, hp=40)
    monkeypatch.setattr(inv2_join, "compute_damage", Mock(return_value=(25, True)))
    monkeypatch.setattr(rewards_service, "roll_rewards", Mock(return_value=ALL_REWARDS))
    original = SeedlingService.award_invasion_victory

    async def fail_after_seedling(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated failure after seedling inventory and ledger flush")

    with monkeypatch.context() as failing:
        failing.setattr(SeedlingService, "award_invasion_victory", AsyncMock(side_effect=fail_after_seedling))
        await inv2_join.handle_inv2_join(_buff_callback(inv_id), SimpleNamespace())
    async with factory() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        assert (inv.status, inv.enemy_hp_remaining) == ("ACTIVE", 40)
        assert await ItemService.get_total(session, 1, "sib_buff") == 2
        assert await ItemService.get_total(session, 1, "apple_gold") == 0
        assert await SeedlingService.get_total(session, 1) == 0
        for table in (Inv2Attack, Inv2Reward, SeedlingAward):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
    ui.joined.assert_not_awaited()
    await inv2_join.handle_inv2_join(_buff_callback(inv_id), SimpleNamespace())
    await asyncio.wait_for(ui.delivered.wait(), timeout=2)
    await inv2_join.handle_inv2_join(_buff_callback(inv_id), SimpleNamespace())
    async with factory() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        assert (inv.status, inv.enemy_hp_remaining) == ("VICTORY", 0)
        assert (await session.scalar(select(Inv2Attack))).damage == 50  # Not capped at enemy HP.
        assert await ItemService.get_total(session, 1, "sib_buff") == 1
        assert await ItemService.get_total(session, 1, "apple_gold") == 5
        assert await SeedlingService.get_total(session, 1) == 1
        assert await session.scalar(select(func.count()).select_from(Inv2Reward)) == 4
        assert await session.scalar(select(func.count()).select_from(SeedlingAward)) == 1
    ui.finalized.assert_awaited_once()


@pytest.mark.asyncio
async def test_buff_finisher_locks_all_recipients_before_consuming_and_rewards_each_once(db, ui, monkeypatch):
    factory, _ = db
    inv_id = await _armed(factory, hp=40)
    async with factory.begin() as session:
        session.add(PlayerCard(id=102, user_id=2, character_id=10, level=1, is_favorite=True))
        session.add(Inv2Attack(invasion_id=inv_id, user_id=1, card_id=101, card_power=20,
                              damage=10, is_crit=False, narrative="نگهبان ایستاد.", joined_at=datetime.now(timezone.utc)))
        (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining = 30
        await ItemService.grant(session, 2, "sib_buff", 1)
    locks = AsyncMock(wraps=inv2_join.lock_user_rows_in_order)
    monkeypatch.setattr(inv2_join, "lock_user_rows_in_order", locks)
    monkeypatch.setattr(inv2_join, "compute_damage", Mock(return_value=(20, False)))
    monkeypatch.setattr(rewards_service, "roll_rewards", Mock(return_value=ALL_REWARDS))
    original_consume = ItemService.consume

    async def consume_after_user_locks(*args, **kwargs):
        locks.assert_awaited_once()
        assert locks.await_args.args[1] == [1, 2]  # Lower-ID previous attacker first.
        return await original_consume(*args, **kwargs)

    monkeypatch.setattr(ItemService, "consume", AsyncMock(side_effect=consume_after_user_locks))
    await inv2_join.handle_inv2_join(_buff_callback(inv_id, user_id=2), SimpleNamespace())
    await asyncio.wait_for(ui.delivered.wait(), timeout=2)
    async with factory.begin() as session:
        await rewards_service.settle_outcome(session, inv_id, "VICTORY")
    async with factory() as session:
        assert (await session.get(Inv2Invasion, inv_id)).status == "VICTORY"
        assert await ItemService.get_total(session, 1, "sib_buff") == 2
        assert await ItemService.get_total(session, 2, "sib_buff") == 0
        for user_id in (1, 2):
            assert await ItemService.get_total(session, user_id, "apple_gold") == 5
            assert await SeedlingService.get_total(session, user_id) == 1
        assert await session.scalar(select(func.count()).select_from(Inv2Reward)) == 8
        assert await session.scalar(select(func.count()).select_from(SeedlingAward)) == 2


@pytest.mark.asyncio
async def test_announcement_has_two_join_buttons(db):
    factory, _ = db
    inv_id = await create_inv(factory)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=123)))
    await presentation.InvasionPresentation().announce(bot, inv_id)
    keyboard = bot.send_photo.await_args.kwargs["reply_markup"]
    assert [(button.text, button.callback_data) for row in keyboard.inline_keyboard for button in row] == [
        ("⚔️ پیوستن (عادی)", f"inv2:join:{inv_id}"),
        ("💥 پیوستن با سیب باف", f"inv2:joinbuff:{inv_id}"),
    ]
    async with factory() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        assert (inv.ends_at - inv.started_at).total_seconds() == 1800


def test_buff_narrative_uses_separate_pool_and_persian_damage():
    seen = []
    for _ in range(12):
        story = narrative.render_join(username="داریوش", card_name="سیب سبز", damage=42,
            is_crit=True, enemy_hp_remaining=0, is_buffed=True, rng=Random(3), invasion_id=777)
        assert "سیب باف" in story and "۲ برابر" in story and "۴۲" in story
        assert "داریوش" in story and "سیب سبز" in story
        assert "{" not in story and "}" not in story and len(story) < 900
        assert story not in seen[-5:]
        seen.append(story)
    assert "join_buff:777" in narrative_pool._history
