"""Disease economy: isolated SQLite transactions, UI wiring and migrations.

SQLite exercises rollback and inspects the locking SQL. The marked race tests
also run against MySQL/MariaDB, where SELECT FOR UPDATE really takes row locks.
"""

import asyncio
import importlib.util
import io
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiogram import Dispatcher
from aiogram.types import Chat, Message, User as TelegramUser
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from PIL import Image
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    AppleMineShift, AppleTransaction, AppleWallet, BotAsset, PendingTransfer,
    SystemConfig, TransferLog, User, UserDisease, UserItem, UserPickaxe,
)
from sib.handlers import apple_mine as mine_handler
from sib.handlers import disease as handler
from sib.handlers import shop
from sib.services import apple_mine as mine
from sib.services import disease_service as disease
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.transfers import execute_transfer, parse_transfer_command
from sib.services.transfers.text import render_type

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database/migrations/versions/071_disease_items.py"
PLANT, ANTIDOTE, BUFF = handler.PLANT, handler.ANTIDOTE, handler.BUFF


def _migration():
    spec = importlib.util.spec_from_file_location("migration_071", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Real, independent SQLite DB without changing the global ORM metadata."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")

    @sa.event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    # Use cloned tables so the SQLite auto-increment shim never mutates ORM
    # metadata for the rest of the suite. Mine's optional FKs only need stubs.
    metadata = sa.MetaData()
    for model in (
        User, SystemConfig, UserDisease, UserItem, AppleWallet, AppleTransaction,
        AppleMineShift, UserPickaxe, TransferLog, PendingTransfer, BotAsset,
    ):
        model.__table__.to_metadata(metadata)
    sa.Table("groups", metadata, sa.Column("id", sa.BigInteger, primary_key=True))
    sa.Table("player_cards", metadata, sa.Column("id", sa.BigInteger, primary_key=True))
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
    monkeypatch.setattr(shop, "get_session", session_context)
    yield factory
    await engine.dispose()


async def _player(db, uid=1, *, plant=0, buff=0, antidote=0, apples=0, level=None):
    async with db.begin() as session:
        session.add(User(id=uid, first_name=f"player {uid}"))
        await session.flush()
        session.add(AppleWallet(user_id=uid, balance=apples))
        for key, quantity in ((PLANT, plant), (BUFF, buff), (ANTIDOTE, antidote)):
            if quantity:
                session.add(UserItem(user_id=uid, item_key=key, quantity=quantity))
        if level is not None:
            session.add(UserDisease(user_id=uid, infection_level=level, has_demon_power=level > 0))


async def _state(db, uid=1):
    async with db() as session:
        return (
            await ItemService.get_total(session, uid, PLANT),
            await ItemService.get_total(session, uid, BUFF),
            await ItemService.get_total(session, uid, ANTIDOTE),
            await AppleWalletService.get_balance(session, uid),
        )


def _message(uid=1):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), reply=AsyncMock())


def test_catalog_and_bundled_images():
    for key, emoji, label in (
        (PLANT, "🌿", "گیاه ویژه"), (ANTIDOTE, "💊", "پادزهر آماده"),
    ):
        assert (ITEM_CATALOG[key]["emoji"], ITEM_CATALOG[key]["label"]) == (emoji, label)
        with Image.open(ROOT / f"assets/items/{key}.jpg") as image:
            assert image.format == "JPEG" and image.mode == "RGB"
            assert image.size == (1024, 1024)
            for xy in ((0, 0), (1023, 0), (0, 1023), (1023, 1023)):
                pixel = image.getpixel(xy)
                assert all(abs(actual - expected) <= 10 for actual, expected in zip(pixel, (252, 248, 237)))


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [
    (None, 15), (0, 0), (-4, 0), (35, 35), (100, 100), (1000, 100), ("bad", 15),
])
async def test_plant_drop_config_is_typed_clamped_and_defaults(db, stored, expected):
    async with db.begin() as session:
        if stored is not None:
            await ConfigService.set(session, "disease_mine_plant_drop_chance", stored)
        assert await mine._plant_drop_chance(session) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("rolls,outcome,dropped", [
    ([0.9, 0.149], "normal", True),
    ([0.9, 0.15], "normal", False),
    ([0.0001, 0.149], "golden", True),
    ([0.0001, 0.9], "golden", False),
])
async def test_mine_rolls_independent_plant_after_primary_reward(db, monkeypatch, rolls, outcome, dropped):
    await _player(db)
    async with db.begin() as session:
        await ConfigService.set(session, "disease_mine_infection_chance", 0)
    rng = Mock(side_effect=rolls)
    monkeypatch.setattr(mine.random, "random", rng)
    monkeypatch.setattr(mine.random, "randint", lambda low, high: low)
    async with db.begin() as session:
        result = await mine.perform_shift(session, user_id=1, group_id=None)
    assert rng.call_count == 2
    assert result.outcome_code == outcome and result.plant_dropped is dropped
    caption = mine_handler._result_caption(result)
    assert ("🌿 یک گیاه ویژه پیدا کردی!" in caption) is dropped
    async with db() as session:
        assert await ItemService.get_total(session, 1, PLANT) == int(dropped)
        assert await session.scalar(sa.select(sa.func.count()).select_from(AppleMineShift)) == 1
        if outcome == "golden":
            assert await ItemService.get_total(session, 1, "apple_gold") == 3
            assert await AppleWalletService.get_balance(session, 1) == 0
        else:
            assert await AppleWalletService.get_balance(session, 1) == result.amount


@pytest.mark.asyncio
async def test_mine_zero_chance_skips_extra_roll_and_cooldown_never_drops(db, monkeypatch):
    await _player(db)
    async with db.begin() as session:
        await ConfigService.set(session, "disease_mine_plant_drop_chance", 0)
        await ConfigService.set(session, "disease_mine_infection_chance", 0)
    monkeypatch.setattr(mine, "_roll_shift", lambda _: ("normal", "apple_normal", 10))
    rng = Mock(return_value=0)
    monkeypatch.setattr(mine.random, "random", rng)
    async with db.begin() as session:
        assert not (await mine.perform_shift(session, user_id=1, group_id=None)).plant_dropped
    rng.assert_not_called()
    with pytest.raises(mine.CooldownError):
        async with db.begin() as session:
            await mine.perform_shift(session, user_id=1, group_id=None)
    rng.assert_not_called()
    assert await _state(db) == (0, 0, 0, 10)


@pytest.mark.asyncio
async def test_mine_grant_failure_rolls_back_primary_reward_and_shift(db, monkeypatch):
    await _player(db)
    async with db.begin() as session:
        await ConfigService.set(session, "disease_mine_plant_drop_chance", 100)
    monkeypatch.setattr(mine, "_roll_shift", lambda _: ("normal", "apple_normal", 10))
    monkeypatch.setattr(ItemService, "grant", AsyncMock(side_effect=RuntimeError("grant failed")))
    with pytest.raises(RuntimeError, match="grant failed"):
        async with db.begin() as session:
            await mine.perform_shift(session, user_id=1, group_id=None)
    assert await _state(db) == (0, 0, 0, 0)
    async with db() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(AppleMineShift)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(AppleTransaction)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,price", [(None, 2000), (347, 347), ("invalid", 2000)])
async def test_shop_uses_one_dedicated_price_for_listing_detail_and_purchase(db, configured, price):
    await _player(db, apples=5000)
    async with db.begin() as session:
        if configured is not None:
            await ConfigService.set(session, "disease_antidote_shop_price", configured)
        await ConfigService.set(session, "item_buy_prices", {ANTIDOTE: 1})
        assert await ItemService.buy_price(session, ANTIDOTE) == price
        assert (await ItemService.buy_prices(session))[ANTIDOTE] == price
    listing, buttons = await shop._goods_view(1)
    detail, _, artwork = await shop._good_view(1, ANTIDOTE)
    assert f"💊 پادزهر آماده — `{price}` سیب" in listing
    assert f"💰 قیمت: `{price}` سیب 🍎" in detail
    # Telegram's Markdown treats a bare underscore as markup; the command
    # must be inside a code span so opening the item screen cannot fail.
    assert "`/use_antidote`" in detail
    assert any(button.callback_data == f"shop_good:{ANTIDOTE}" for row in buttons.inline_keyboard for button in row)
    assert artwork is not None
    async with db.begin() as session:
        assert await ItemService.buy(session, 1, ANTIDOTE, 2) == 2
    assert await _state(db) == (0, 0, 2, 5000 - 2 * price)


@pytest.mark.asyncio
async def test_shop_callback_purchases_antidote_through_real_handler(db, monkeypatch):
    await _player(db, apples=2000)
    monkeypatch.setattr(shop, "_shop_actor", AsyncMock(return_value=1))
    answer = AsyncMock()
    edit = AsyncMock()
    monkeypatch.setattr(shop, "safe_answer_callback", answer)
    monkeypatch.setattr(shop, "safe_edit_callback_message", edit)
    callback = SimpleNamespace(data=f"shop:buy:{ANTIDOTE}:1", from_user=SimpleNamespace(id=1))
    await shop.handle_shop_good_buy(callback, bot=object())
    answer.assert_awaited_once_with(callback, "✅ با موفقیت 1 عدد پادزهر آماده خریدید.")
    assert edit.await_count == 1
    assert await _state(db) == (0, 0, 1, 0)


@pytest.mark.asyncio
async def test_shop_insufficient_funds_and_disabled_price_cannot_grant(db):
    await _player(db, apples=1999)
    with pytest.raises(ValueError):
        async with db.begin() as session:
            await ItemService.buy(session, 1, ANTIDOTE)
    assert await _state(db) == (0, 0, 0, 1999)
    async with db.begin() as session:
        await ConfigService.set(session, "disease_antidote_shop_price", 0)
        assert await ItemService.buy_price(session, ANTIDOTE) is None
        assert ANTIDOTE not in await ItemService.buy_prices(session)
    listing, _ = await shop._goods_view(1)
    assert "پادزهر آماده" not in listing


@pytest.mark.asyncio
async def test_disease_items_cannot_be_sold_even_if_configured(db):
    await _player(db, plant=2, antidote=2, apples=100)
    async with db.begin() as session:
        await ConfigService.set(session, "item_sell_prices", {PLANT: 100000, ANTIDOTE: 999999, "flower": 15})
        assert await ItemService.sell_price(session, PLANT) is None
        assert await ItemService.sell_price(session, ANTIDOTE) is None
        assert await ItemService.sell_prices(session) == {"flower": 15}
        assert await ItemService.sellable_inventory(session, 1) == []
    for key in (PLANT, ANTIDOTE):
        with pytest.raises(ValueError):
            async with db.begin() as session:
                await ItemService.sell(session, 1, key, 1)
    assert await _state(db) == (2, 0, 2, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,phrase,emoji", [(PLANT, "گیاه ویژه", "🌿"), (ANTIDOTE, "پادزهر آماده", "💊")])
async def test_disease_items_are_transferable_not_just_catalogued(db, key, phrase, emoji):
    await _player(db, uid=1, plant=2 if key == PLANT else 0, antidote=2 if key == ANTIDOTE else 0)
    await _player(db, uid=2)
    command = parse_transfer_command(f"انتقال {phrase} ۱")
    assert command is not None and (command.item_type, command.amount) == (key, 1)
    assert render_type(key) == (phrase, emoji)
    async with db.begin() as session:
        result = await execute_transfer(session, 1, 2, command.item_type, command.amount)
        assert result.ok
    async with db() as session:
        assert await ItemService.get_total(session, 1, key) == 1
        assert await ItemService.get_total(session, 2, key) == 1
        log = await session.scalar(sa.select(TransferLog))
        assert (log.item_type, log.amount) == (key, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,cost", [(None, 200), (37, 37), (0, 0), ("invalid", 200)])
async def test_craft_spends_exact_ingredients_and_configured_apples(db, configured, cost):
    await _player(db, plant=2, buff=3, antidote=1, apples=500)
    async with db.begin() as session:
        if configured is not None:
            await ConfigService.set(session, "disease_craft_apple_cost", configured)
        await handler.craft_antidote(session, 1)
    assert await _state(db) == (1, 2, 2, 500 - cost)
    async with db() as session:
        ledger = await session.scalar(sa.select(AppleTransaction))
        assert (ledger.amount, ledger.reason, ledger.balance_after) == (-cost, "disease_antidote_craft", 500 - cost)


@pytest.mark.asyncio
@pytest.mark.parametrize("plant,buff,apples,reason", [
    (0, 1, 200, handler.MISSING_PLANT),
    (1, 0, 200, handler.MISSING_BUFF),
    (1, 1, 199, handler.MISSING_APPLES),
])
async def test_craft_refuses_missing_resources_without_mutation(db, plant, buff, apples, reason):
    await _player(db, plant=plant, buff=buff, apples=apples)
    with pytest.raises(handler.DiseaseItemError, match=reason):
        async with db.begin() as session:
            await handler.craft_antidote(session, 1)
    assert await _state(db) == (plant, buff, 0, apples)
    async with db() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(AppleTransaction)) == 0


@pytest.mark.asyncio
async def test_craft_grant_failure_rolls_back_ingredients_wallet_and_ledger(db, monkeypatch):
    await _player(db, plant=1, buff=1, apples=200)
    monkeypatch.setattr(ItemService, "grant", AsyncMock(side_effect=RuntimeError("grant failed")))
    with pytest.raises(RuntimeError, match="grant failed"):
        async with db.begin() as session:
            await handler.craft_antidote(session, 1)
    assert await _state(db) == (1, 1, 0, 200)
    async with db() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(AppleTransaction)) == 0


@pytest.mark.asyncio
async def test_craft_locks_user_then_sorted_inventory_then_wallet_for_update(db):
    await _player(db, plant=1, buff=1, apples=200)
    locks = []
    async with db.begin() as session:
        @sa.event.listens_for(session.sync_session, "do_orm_execute")
        def record(execution):
            stmt = execution.statement
            if getattr(stmt, "_for_update_arg", None) is not None:
                compiled = stmt.compile(dialect=mysql.dialect())
                locks.append((stmt.get_final_froms()[0].name, compiled.params, str(compiled)))

        await handler.craft_antidote(session, 1)
    assert [table for table, _, _ in locks[:5]] == [
        "users", "user_items", "user_items", "user_items", "apple_wallets"
    ]
    keys = [params["item_key_1"] for _, params, _ in locks[1:4]]
    assert keys == sorted((ANTIDOTE, PLANT, BUFF))
    assert all("FOR UPDATE" in sql for _, _, sql in locks)


@pytest.mark.asyncio
async def test_craft_command_reports_success_and_specific_refusal(db):
    await _player(db, plant=1, buff=1, apples=200)
    message = _message()
    await handler.handle_craft_antidote(message)
    message.reply.assert_awaited_once_with("✅ پادزهر آماده ساخته شد!")
    assert await _state(db) == (0, 0, 1, 0)
    message = _message()
    await handler.handle_craft_antidote(message)
    message.reply.assert_awaited_once_with("🌿 گیاه ویژه کافی نداری!")


@pytest.mark.asyncio
async def test_use_on_sick_user_consumes_one_and_applies_phase1_cure(db, monkeypatch):
    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(disease, "_now", lambda: now)
    await _player(db, antidote=2, level=10)
    async with db.begin() as session:
        await handler.use_antidote(session, 1)
    assert await _state(db) == (0, 0, 1, 0)
    async with db() as session:
        status = await disease.get_status(session, 1)
        row = await session.get(UserDisease, 1)
        assert status["is_sick"] is False and status["has_demon_power"] is False
        assert disease._as_utc(row.immunity_until) == now.replace(day=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [None, 0])
async def test_healthy_user_does_not_consume_or_get_immunity(db, level):
    await _player(db, antidote=1, level=level)
    with pytest.raises(handler.DiseaseItemError, match="تو بیمار نیستی!"):
        async with db.begin() as session:
            await handler.use_antidote(session, 1)
    assert await _state(db) == (0, 0, 1, 0)
    async with db() as session:
        row = await session.get(UserDisease, 1)
        assert row is None if level is None else row.immunity_until is None


@pytest.mark.asyncio
async def test_sick_user_without_antidote_is_not_cured(db):
    await _player(db, level=10)
    with pytest.raises(handler.DiseaseItemError, match="💊 پادزهر آماده نداری!"):
        async with db.begin() as session:
            await handler.use_antidote(session, 1)
    async with db() as session:
        row = await session.get(UserDisease, 1)
        assert row.infection_level == 10 and row.immunity_until is None


@pytest.mark.asyncio
async def test_use_cure_failure_rolls_back_consumption(db, monkeypatch):
    await _player(db, antidote=1, level=10)
    monkeypatch.setattr(disease, "cure_user", AsyncMock(side_effect=RuntimeError("cure failed")))
    with pytest.raises(RuntimeError, match="cure failed"):
        async with db.begin() as session:
            await handler.use_antidote(session, 1)
    assert await _state(db) == (0, 0, 1, 0)
    async with db() as session:
        assert (await session.get(UserDisease, 1)).infection_level == 10


@pytest.mark.asyncio
async def test_use_command_exact_success_and_healthy_messages(db):
    await _player(db, antidote=2, level=10)
    message = _message()
    await handler.handle_use_antidote(message)
    message.reply.assert_awaited_once_with("✅ درمان شدی! پادزهر مصرف شد.")
    message = _message()
    await handler.handle_use_antidote(message)
    message.reply.assert_awaited_once_with("تو بیمار نیستی!")
    assert await _state(db) == (0, 0, 1, 0)


@pytest.mark.asyncio
async def test_command_router_is_registered_and_filters_both_commands():
    from sib.handlers import register_all_handlers

    included = []
    with patch.object(Dispatcher, "include_router", side_effect=included.append):
        register_all_handlers(Dispatcher())
    assert handler.router in included
    for command, callback in (
        ("/craft_antidote", handler.handle_craft_antidote),
        ("/use_antidote", handler.handle_use_antidote),
    ):
        event = Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=1, type="private"),
            from_user=TelegramUser(id=1, first_name="player", is_bot=False), text=command,
        )
        # The command may be registered more than once (it also has a Persian
        # plain-text alias), so ANY registration matching the slash command is
        # enough - exactly what aiogram's dispatcher does.
        registered = [h for h in handler.router.message.handlers if h.callback is callback]
        matched = False
        for entry in registered:
            if all([await f.call(event, bot=None) for f in entry.filters]):
                matched = True
                break
        assert matched


def test_migration_idempotence_and_override_preservation():
    migration = _migration()
    assert (migration.revision, migration.down_revision) == ("071_disease_items", "070_disease_system")
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE system_config (`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
                assert connection.execute(sa.text("SELECT `value` FROM system_config WHERE `key`=:key"), {"key": migration._KEY}).scalar() == "15"
                migration.downgrade()
                migration.downgrade()
                assert connection.execute(sa.text("SELECT COUNT(*) FROM system_config")).scalar() == 0
                connection.execute(sa.text("INSERT INTO system_config (`key`, `value`) VALUES (:key, '42')"), {"key": migration._KEY})
                migration.upgrade()
                migration.downgrade()
                assert connection.execute(sa.text("SELECT `value` FROM system_config WHERE `key`=:key"), {"key": migration._KEY}).scalar() == "42"
    finally:
        engine.dispose()


def test_migration_mysql_sql_is_non_destructive():
    migration = _migration()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql", opts={"as_sql": True, "output_buffer": output, "literal_binds": True}
    )
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    assert "ON DUPLICATE KEY UPDATE `key` = `key`" in sql
    assert "BINARY `key` = BINARY" in sql
    assert "disease_mine_plant_drop_chance" in sql


def test_alembic_real_upgrade_and_downgrade_from_070(tmp_path):
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["080_group_doctors"]
    assert scripts.get_revision("071_disease_items").down_revision == "070_disease_system"
    database = tmp_path / "migration.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE system_config (`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"))
    engine.dispose()
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = f"sqlite:///{database}"
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT.parent), env.get("PYTHONPATH", "")))

    def alembic(*args):
        result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    assert alembic("heads") == "080_group_doctors (head)"
    alembic("stamp", "070_disease_system")
    alembic("upgrade", "071_disease_items")
    alembic("upgrade", "071_disease_items")
    assert "071_disease_items" in alembic("current")
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT `value` FROM system_config")).scalar() == "15"
    engine.dispose()
    alembic("downgrade", "070_disease_system")
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT COUNT(*) FROM system_config")).scalar() == 0
    engine.dispose()
    alembic("upgrade", "071_disease_items")


_ROW_LOCK_DATABASE = any(
    dialect in (os.getenv("sib_TEST_DATABASE_URL") or "")
    for dialect in ("mysql", "mariadb")
)


async def _race_two_transactions(factory, action, user_id):
    """Keep the first transaction open while the second attempts the action."""
    first_ready = asyncio.Event()
    release_first = asyncio.Event()

    async def first():
        async with factory.begin() as session:
            await action(session, user_id)
            first_ready.set()
            await release_first.wait()

    async def second():
        try:
            async with factory.begin() as session:
                await action(session, user_id)
            return "unexpected success"
        except handler.DiseaseItemError as exc:
            return str(exc)

    task1 = asyncio.create_task(first())
    task2 = None
    try:
        await asyncio.wait_for(first_ready.wait(), timeout=10)
        task2 = asyncio.create_task(second())
        await asyncio.sleep(0.2)
        assert not task2.done(), "the second transaction did not wait for the user lock"
        release_first.set()
        _, result = await asyncio.wait_for(asyncio.gather(task1, task2), timeout=10)
        return result
    finally:
        release_first.set()
        for task in (task1, task2):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (task1, task2) if t), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(not _ROW_LOCK_DATABASE, reason="real SELECT FOR UPDATE races require MySQL/MariaDB")
async def test_concurrent_craft_serializes_and_cannot_double_spend(db_session):
    from sib.database.connection import get_async_sessionmaker
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "craft race")
    uid = user.id
    await ItemService.grant(db_session, uid, PLANT, 1)
    await ItemService.grant(db_session, uid, BUFF, 1)
    await AppleWalletService.earn(db_session, uid, 200, reason="test_seed")
    await db_session.commit()
    result = await _race_two_transactions(get_async_sessionmaker(), handler.craft_antidote, uid)
    assert result == handler.MISSING_PLANT
    await db_session.rollback()
    assert await ItemService.get_total(db_session, uid, PLANT) == 0
    assert await ItemService.get_total(db_session, uid, BUFF) == 0
    assert await ItemService.get_total(db_session, uid, ANTIDOTE) == 1
    assert await AppleWalletService.get_balance(db_session, uid) == 0


@pytest.mark.asyncio
@pytest.mark.skipif(not _ROW_LOCK_DATABASE, reason="real SELECT FOR UPDATE races require MySQL/MariaDB")
async def test_concurrent_use_serializes_and_never_consumes_after_cure(db_session):
    from sib.database.connection import get_async_sessionmaker
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "use race")
    uid = user.id
    await ItemService.grant(db_session, uid, ANTIDOTE, 2)
    db_session.add(UserDisease(user_id=uid, infection_level=10, has_demon_power=False))
    await db_session.commit()
    result = await _race_two_transactions(get_async_sessionmaker(), handler.use_antidote, uid)
    assert result == handler.NOT_SICK
    await db_session.rollback()
    assert await ItemService.get_total(db_session, uid, ANTIDOTE) == 1
    assert not await disease.is_sick(db_session, uid)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", ["normal", "golden"])
async def test_mine_plant_inventory_is_locked_before_wallet_and_in_key_order(db, monkeypatch, primary):
    await _player(db)
    async with db.begin() as session:
        await ConfigService.set(session, "disease_mine_plant_drop_chance", 100)
    if primary == "normal":
        outcome = ("normal", "apple_normal", 10)
    else:
        outcome = mine.SPECIAL_GOLD
    monkeypatch.setattr(mine, "_roll_shift", lambda _: outcome)
    locks = []
    async with db.begin() as session:
        @sa.event.listens_for(session.sync_session, "do_orm_execute")
        def record(execution):
            statement = execution.statement
            if getattr(statement, "_for_update_arg", None) is not None:
                compiled = statement.compile(dialect=mysql.dialect())
                locks.append((statement.get_final_froms()[0].name, compiled.params))

        assert (await mine.perform_shift(session, user_id=1, group_id=None)).plant_dropped
    relevant = [(table, params) for table, params in locks if table in {"users", "user_items", "apple_wallets"}]
    assert relevant[0][0] == "users"
    if primary == "normal":
        assert [table for table, _ in relevant[:3]] == ["users", "user_items", "apple_wallets"]
        assert relevant[1][1]["item_key_1"] == PLANT
    else:
        assert [params["item_key_1"] for table, params in relevant[1:3]] == ["apple_gold", PLANT]


@pytest.mark.asyncio
async def test_antidote_purchase_locks_user_inventory_then_wallet(db):
    await _player(db, apples=2000)
    locks = []
    async with db.begin() as session:
        @sa.event.listens_for(session.sync_session, "do_orm_execute")
        def record(execution):
            statement = execution.statement
            if getattr(statement, "_for_update_arg", None) is not None:
                compiled = statement.compile(dialect=mysql.dialect())
                locks.append((statement.get_final_froms()[0].name, compiled.params, str(compiled)))

        await ItemService.buy(session, 1, ANTIDOTE)
    assert [table for table, _, _ in locks[:3]] == ["users", "user_items", "apple_wallets"]
    assert locks[1][1]["item_key_1"] == ANTIDOTE
    assert all("FOR UPDATE" in sql for _, _, sql in locks)


@pytest.mark.asyncio
async def test_use_reads_disease_and_antidote_under_row_locks(db):
    await _player(db, antidote=1, level=10)
    tables = []
    async with db.begin() as session:
        @sa.event.listens_for(session.sync_session, "do_orm_execute")
        def record(execution):
            statement = execution.statement
            if getattr(statement, "_for_update_arg", None) is not None:
                assert "FOR UPDATE" in str(statement.compile(dialect=mysql.dialect()))
                tables.append(statement.get_final_froms()[0].name)

        await handler.use_antidote(session, 1)
    assert tables[:3] == ["users", "user_diseases", "user_items"]
