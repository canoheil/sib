"""Phase 5, SQLite-only DB and mocked Telegram; no bot startup or live group."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
from PIL import Image
from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sib.database.models import (Base, Character, Garden, GardenPlot, GardenWitherEvent,
    Group, Inv2Attack, Inv2GroupMember, Inv2GroupState,
    Inv2Invasion, Inv2Region, Inv2Reward, PlayerCard, SeedlingAward, SeedlingInventory, SystemConfig, User, UserItem)
from sib.handlers import inv2_admin, inv2_join
from sib.services.invasion_v2 import presentation
from sib.services.invasion_v2.imaging import cache, compose, backgrounds, templates
from sib.services.invasion_v2.rewards_service import grant_victory_rewards, settle_outcome
from sib.services.maintenance.service import MaintenanceService

NOW = datetime.now(timezone.utc)
ART = Path(__file__).resolve().parents[2] / "assets/characters/sib_sabz.jpg"


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    from sib.database.models import GroupCastle
    tables = [GroupCastle.__table__, User.__table__, Group.__table__, Character.__table__, PlayerCard.__table__,
              Inv2Region.__table__, Inv2Invasion.__table__, Inv2Attack.__table__,
              Inv2Reward.__table__, Inv2GroupState.__table__, UserItem.__table__, SystemConfig.__table__,
              SeedlingInventory.__table__, SeedlingAward.__table__,
              # G2 corruption settlement reads membership and garden tables.
              Inv2GroupMember.__table__, Garden.__table__, GardenPlot.__table__,
              GardenWitherEvent.__table__]
    # SQLite needs INTEGER, not BIGINT, to generate surrogate primary keys.
    columns = [UserItem.__table__.c.id, SeedlingAward.__table__.c.id,
               GardenPlot.__table__.c.id, GardenWitherEvent.__table__.c.id]
    original_types = [column.type for column in columns]
    for column in columns:
        column.type = Integer()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=tables)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

        @asynccontextmanager
        async def session():
            async with factory() as db:
                try:
                    yield db
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        monkeypatch.setattr(presentation, "get_session", session)
        monkeypatch.setattr(inv2_join, "get_session", session)
        monkeypatch.setattr(inv2_admin, "get_session", session)
        monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(presentation, "is_group_available", AsyncMock(return_value=True))
        monkeypatch.setattr(presentation, "record_send_success", AsyncMock())
        monkeypatch.setattr(presentation, "record_send_failure", AsyncMock())
        monkeypatch.setattr(presentation, "cached_image", lambda *a, **k: ART)
        monkeypatch.setattr(inv2_admin, "cached_image", lambda *a, **k: ART)
        async with factory.begin() as s:
            s.add_all([User(id=1, first_name="داریوش"), User(id=2, first_name="بی‌کارت"),
                Group(id=-100, title="گروه"),
                Character(id=10, code="sib_sabz", name_fa="سیب سبز", power=20, speed=20, intelligence=20),
                Inv2Region(code="moon", name_fa="درّه ماه", controlling_faction="فرزندان ماه")])
        yield factory, session
    finally:
        for column, original_type in zip(columns, original_types):
            column.type = original_type
        # Phase 8D: never leave a debounced HP-edit task against a closed DB.
        await presentation.cancel_pending_hp_edits()
        await engine.dispose()


async def create_inv(factory, *, status="PENDING", hp=200, group=-100, message_id=None):
    async with factory.begin() as s:
        inv = Inv2Invasion(group_id=group, region_code="moon", enemy_type="soldier",
            enemy_name_fa="شیاسیب پیاده", enemy_hp=hp, enemy_hp_remaining=hp,
            difficulty_multiplier=4.0, status=status, scheduled_at=NOW,
            started_at=NOW if status == "ACTIVE" else None,
            ends_at=NOW + timedelta(minutes=5) if status == "ACTIVE" else None,
            message_id=message_id, message_chat_id=group if message_id else None,
            occurrence_key=f"phase5:{status}:{hp}:{message_id}")
        s.add(inv)
        await s.flush()
        return inv.id


def callback(inv_id, *, user_id=1, message_id=123):
    msg = Message(message_id=message_id, date=NOW,
        chat=Chat(id=-100, type=ChatType.GROUP),
        from_user=TgUser(id=user_id, is_bot=False, first_name="داریوش"))
    return CallbackQuery(id=f"cb-{user_id}", from_user=msg.from_user,
        chat_instance="group", message=msg, data=f"inv2:join:{inv_id}")


@pytest.mark.asyncio
async def test_announce_activates_after_send_and_skips_duplicate(db):
    factory, _ = db
    inv_id = await create_inv(factory)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=123)))
    await presentation.InvasionPresentation().announce(bot, inv_id)
    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"inv2:join:{inv_id}"
    assert len(kwargs["caption"]) <= 1024
    async with factory() as s:
        inv = await s.get(Inv2Invasion, inv_id)
        assert inv.status == "ACTIVE" and inv.message_id == 123 and inv.ends_at is not None
    await presentation.InvasionPresentation().announce(bot, inv_id)
    bot.send_photo.assert_awaited_once()
    presentation.record_send_success.assert_awaited_once_with(-100)


@pytest.mark.asyncio
async def test_announce_send_bad_request_records_failure_and_no_activation(db):
    factory, _ = db
    inv_id = await create_inv(factory)
    bot = SimpleNamespace(send_photo=AsyncMock(side_effect=TelegramBadRequest(
        method=SendPhoto(chat_id=-100, photo="missing"), message="group blocked")))
    await presentation.InvasionPresentation().announce(bot, inv_id)
    presentation.record_send_failure.assert_awaited_once_with(-100)
    async with factory() as s:
        assert (await s.get(Inv2Invasion, inv_id)).status == "PENDING"


@pytest.mark.asyncio
async def test_join_guards_and_happy_path(db, monkeypatch):
    factory, _ = db
    bot = SimpleNamespace(send_photo=AsyncMock())
    ack = AsyncMock()
    monkeypatch.setattr(inv2_join, "_answer", ack)
    inv_id = await create_inv(factory, status="ACTIVE", message_id=123, hp=1)
    cb = callback(inv_id)
    MaintenanceService._invasion_enabled.return_value = False
    await inv2_join.handle_inv2_join(cb, bot)
    assert "غیرفعال" in ack.await_args.args[1]
    MaintenanceService._invasion_enabled.return_value = True
    await inv2_join.handle_inv2_join(callback(99999), bot)
    assert "پایان" in ack.await_args.args[1]
    await inv2_join.handle_inv2_join(cb, bot)
    assert "کارت اصلی" in ack.await_args.args[1]
    async with factory.begin() as s:
        s.add(PlayerCard(id=101, user_id=1, character_id=10, level=2, is_favorite=True))
    # Keep delivery mocked: the transaction and reward grant are the subject here.
    joined = AsyncMock()
    finalized = AsyncMock()
    monkeypatch.setattr(presentation.InvasionPresentation, "on_join", joined)
    monkeypatch.setattr(presentation.InvasionPresentation, "finalize", finalized)
    await inv2_join.handle_inv2_join(cb, bot)
    await asyncio.sleep(.05)
    async with factory() as s:
        inv = await s.get(Inv2Invasion, inv_id)
        attacks = (await s.scalars(select(Inv2Attack))).all()
        rewards = (await s.scalars(select(Inv2Reward))).all()
        state = await s.get(Inv2GroupState, -100)
        assert inv.status == "VICTORY" and inv.enemy_hp_remaining == 0
        assert len(attacks) == 1 and attacks[0].card_power == 30
        assert len(rewards) >= 1 and state.total_victories == 1
        assert (await s.get(Inv2Region, "moon")).is_lost_to_enemy is False
    joined.assert_awaited_once()
    finalized.assert_awaited_once()
    await inv2_join.handle_inv2_join(cb, bot)
    assert "پایان" in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_join_duplicate_and_optimistic_cas(db, monkeypatch):
    factory, _ = db
    inv_id = await create_inv(factory, status="ACTIVE", hp=500, message_id=123)
    async with factory.begin() as s:
        s.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
    ack = AsyncMock()
    monkeypatch.setattr(inv2_join, "_answer", ack)
    monkeypatch.setattr(presentation.InvasionPresentation, "on_join", AsyncMock())
    await inv2_join.handle_inv2_join(callback(inv_id), SimpleNamespace())
    await asyncio.sleep(.02)
    await inv2_join.handle_inv2_join(callback(inv_id), SimpleNamespace())
    assert "قبلاً" in ack.await_args.args[1]
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(Inv2Attack)) == 1


@pytest.mark.asyncio
async def test_reward_replay_and_defeat_region_counters(db):
    factory, _ = db
    victory = await create_inv(factory, status="VICTORY", hp=1)
    defeat = await create_inv(factory, status="DEFEAT", hp=100, message_id=123)
    async with factory.begin() as s:
        s.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
        s.add(Inv2Attack(invasion_id=victory, user_id=1, card_id=101, card_power=20,
            damage=20, is_crit=False, narrative="پیروزی", joined_at=NOW))
    async with factory.begin() as s:
        await settle_outcome(s, victory, "VICTORY")
    async with factory() as s:
        before = [(r.reward_type, r.reward_amount) for r in (await s.scalars(select(Inv2Reward))).all()]
        inventory = [(r.item_key, r.quantity) for r in (await s.scalars(select(UserItem))).all()]
    async with factory.begin() as s:
        await grant_victory_rewards(s, victory)
        await settle_outcome(s, victory, "VICTORY")
        await settle_outcome(s, defeat, "DEFEAT")
        await settle_outcome(s, defeat, "DEFEAT")
    async with factory() as s:
        assert before and before == [(r.reward_type, r.reward_amount) for r in (await s.scalars(select(Inv2Reward))).all()]
        assert inventory == [(r.item_key, r.quantity) for r in (await s.scalars(select(UserItem))).all()]
        assert (await s.get(Inv2GroupState, -100)).total_victories == 1
        assert (await s.get(Inv2GroupState, -100)).total_defeats == 1
        region = await s.get(Inv2Region, "moon")
        assert region.is_lost_to_enemy and region.last_invasion_id == defeat


@pytest.mark.asyncio
async def test_finalize_victory_and_defeat_messages(db):
    factory, _ = db
    bot = SimpleNamespace(edit_message_caption=AsyncMock(return_value=True),
                          send_photo=AsyncMock(return_value=True))
    victory = await create_inv(factory, status="VICTORY", hp=1, message_id=123)
    defeat = await create_inv(factory, status="DEFEAT", hp=200, message_id=124)
    async with factory.begin() as s:
        s.add(Inv2Reward(invasion_id=victory, user_id=1, reward_type="golden_apple", reward_amount=3, granted_at=NOW))
    service = presentation.InvasionPresentation()
    await service.finalize(bot, victory)
    await service.finalize(bot, defeat)
    assert bot.edit_message_caption.await_count == bot.send_photo.await_count == 2
    assert bot.edit_message_caption.await_args_list[0].kwargs["reply_markup"] is None
    assert "پاداش" in bot.send_photo.await_args_list[0].kwargs["caption"]
    assert "پاداش" not in bot.send_photo.await_args_list[1].kwargs["caption"]


def test_images_dimensions_cache_pruning_and_missing_art(tmp_path):
    assert set(templates.ENEMY_TYPES) == {"soldier", "shadow", "commander", "brute", "king"}
    for enemy in templates.ENEMY_TYPES:
        assert backgrounds.background(enemy).size == (1280, 720)
    renders = [compose.announcement_image("soldier", "درّه ماه", "فرزندان ماه"),
        compose.join_image("shadow", ART, "داریوش", 88, True),
        compose.result_image("king", "VICTORY", "درّه ماه", "فرزندان ماه"),
        compose.result_image("brute", "DEFEAT", "درّه ماه", "فرزندان ماه")]
    assert all(image.size == (1280, 720) for image in renders)
    calls = []
    def build():
        calls.append(True)
        return renders[1]
    first = cache.cached_image((1, 1, 88, True, "shadow"), build, directory=tmp_path)
    second = cache.cached_image((1, 1, 88, True, "shadow"), build, directory=tmp_path)
    assert first == second and first.stat().st_size > 1000 and len(calls) == 1
    with Image.open(first) as img:
        assert img.size == (1280, 720)
    import os, time
    old = time.time() - cache.MAX_AGE - 10
    os.utime(first, (old, old))
    assert cache.prune(directory=tmp_path) == 1 and not first.exists()
    with pytest.raises(FileNotFoundError):
        compose.join_image("soldier", None, "داریوش", 40, False)


@pytest.mark.asyncio
async def test_admin_preview_has_no_db_writes_and_simulation_read_only(db, monkeypatch):
    factory, _ = db
    admin = SimpleNamespace(id=2, first_name="مدیر")  # no Main Card → placeholder
    msg = SimpleNamespace(text="/inv2_preview soldier", from_user=admin,
        chat=SimpleNamespace(type="private"),
        answer_media_group=AsyncMock(return_value=True), answer=AsyncMock(return_value=True))
    async with factory() as s:
        before = {table: await s.scalar(select(func.count()).select_from(table)) for table in
            (Inv2Invasion, Inv2Attack, Inv2Reward, UserItem)}
    await inv2_admin.preview(msg)
    assert msg.answer_media_group.await_count == 1
    assert len(msg.answer_media_group.await_args.kwargs["media"]) == 4
    msg.text = "/inv2_simulate -100 soldier 5"
    await inv2_admin.simulate(msg)
    assert "نامشخص" in msg.answer.await_args.kwargs["text"]
    async with factory() as s:
        assert before == {table: await s.scalar(select(func.count()).select_from(table)) for table in before}


@pytest.mark.asyncio
async def test_on_join_renders_new_photo_and_meters_success(db):
    factory, _ = db
    inv_id = await create_inv(factory, status="ACTIVE", message_id=123)
    async with factory.begin() as s:
        s.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
        s.add(Inv2Attack(id=31, invasion_id=inv_id, user_id=1, card_id=101,
            card_power=20, damage=41, is_crit=True, narrative="ضربهٔ سنگین بر زره", joined_at=NOW))
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=True))
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, 31)
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["reply_to_message_id"] == 123
    # Phase 8D: the crit marker moved to the explicit damage line.
    assert kwargs["caption"] == "ضربهٔ سنگین بر زره\n\n💥 آسیب وارده به دشمن: **۴۱**"
    presentation.record_send_success.assert_awaited_once_with(-100)
    await presentation.InvasionPresentation().on_join(bot, inv_id, 2, 31)  # mismatched identity
    bot.send_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_pending_and_flag_disabled_prevent_sends(db, monkeypatch):
    factory, _ = db
    inv_id = await create_inv(factory)
    presenter = presentation.InvasionPresentation()
    announce = AsyncMock()
    monkeypatch.setattr(presenter, "announce", announce)
    bot_pending = SimpleNamespace()
    await presenter.publish_pending(bot_pending)
    announce.assert_awaited_once_with(bot_pending, inv_id)
    announce.reset_mock()
    MaintenanceService._invasion_enabled.return_value = False
    await presenter.publish_pending(SimpleNamespace())
    announce.assert_not_awaited()
    bot = SimpleNamespace(send_photo=AsyncMock(), edit_message_caption=AsyncMock())
    await presenter.announce(bot, inv_id)
    await presenter.on_join(bot, inv_id, 1, 1)
    await presenter.finalize(bot, inv_id)
    bot.send_photo.assert_not_awaited()
    bot.edit_message_caption.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_invalid_main_card_and_cas_conflict(db, monkeypatch):
    factory, _ = db
    inv_id = await create_inv(factory, status="ACTIVE", message_id=123)
    async with factory.begin() as s:
        s.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
    ack = AsyncMock()
    monkeypatch.setattr(inv2_join, "_answer", ack)
    async with factory.begin() as s:
        (await s.get(PlayerCard, 101)).level = 99
    await inv2_join.handle_inv2_join(callback(inv_id), SimpleNamespace())
    assert "مشخص نیست" in ack.await_args.args[1]
    async with factory.begin() as s:
        (await s.get(PlayerCard, 101)).level = 1
    from sqlalchemy import false
    original_update = inv2_join.update
    monkeypatch.setattr(inv2_join, "update", lambda model: original_update(model).where(false()))
    await inv2_join.handle_inv2_join(callback(inv_id), SimpleNamespace())
    assert "تغییر کرد" in ack.await_args.args[1]
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(Inv2Attack)) == 0


@pytest.mark.asyncio
async def test_callback_answer_uses_metered_wrapper(db):
    cb = callback(3)
    method = AsyncMock(return_value=True)
    # Aiogram's answer shortcut requires a bound Bot; test the adapter with
    # an equivalent callback object to avoid a live HTTP call.
    fake = SimpleNamespace(message=cb.message, answer=method)
    await inv2_join._answer(fake, "⚔️ ثبت شد")
    method.assert_awaited_once()
    presentation.record_send_success.assert_awaited_with(-100)
    fake.message = None
    await inv2_join._answer(fake, "⚔️ پایان", alert=True)
    assert method.await_count == 2


@pytest.mark.asyncio
async def test_telegram_unknown_error_is_contained(db):
    method = AsyncMock(side_effect=RuntimeError("offline"))
    assert await presentation._telegram(-100, method, text="سلام") is None
    presentation.record_send_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_presentation_no_bot_and_nonterminal_noop(db):
    factory, _ = db
    inv_id = await create_inv(factory)
    svc = presentation.InvasionPresentation()
    await svc.announce(None, inv_id)
    await svc.on_join(None, inv_id, 1, 2)
    await svc.finalize(None, inv_id)
    await svc.publish_pending(None)
    bot = SimpleNamespace(send_photo=AsyncMock(), edit_message_caption=AsyncMock())
    await svc.finalize(bot, inv_id)
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_commands_never_send_into_a_group(db):
    msg = SimpleNamespace(text="/inv2_preview soldier",
        chat=SimpleNamespace(type="supergroup"),
        from_user=SimpleNamespace(id=1),
        answer_media_group=AsyncMock(), answer=AsyncMock())
    await inv2_admin.preview(msg)
    msg.text = "/inv2_simulate -100 soldier 2"
    await inv2_admin.simulate(msg)
    msg.answer_media_group.assert_not_awaited()
    msg.answer.assert_not_awaited()


@pytest.fixture
async def rendered_delivery(db, tmp_path, monkeypatch):
    """Real byte composition + disk cache; only Telegram is mocked."""
    import threading
    from unittest.mock import Mock

    event_thread = threading.get_ident()
    spies = {}
    records = []
    builds = []
    for module in (presentation, inv2_admin):
        for name in ("compose_announcement_image", "compose_join_image", "compose_result_image"):
            spy = Mock(wraps=getattr(module, name))
            spies[module.__name__, name] = spy
            monkeypatch.setattr(module, name, spy)

    def render_cache(parts, build):
        assert threading.get_ident() != event_thread
        def render():
            data = build()
            assert isinstance(data, bytes), "Delivery must use the JPEG-byte API"
            builds.append(parts)
            return data
        path = cache.cached_image(parts, render, directory=tmp_path)
        with Image.open(path) as image:
            assert image.format == "JPEG" and image.size == (1280, 720)
        records.append((parts, path))
        return path

    monkeypatch.setattr(presentation, "cached_image", render_cache)
    monkeypatch.setattr(inv2_admin, "cached_image", render_cache)
    return SimpleNamespace(spies=spies, records=records, builds=builds,
                           directory=tmp_path, factory=db[0])


async def _phase8a_attack(factory, *, critical=False):
    inv_id = await create_inv(factory, status="ACTIVE", hp=159, message_id=123)
    async with factory.begin() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        inv.enemy_hp = 200  # Remaining HP must come from storage, not full HP.
        session.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
        session.add(Inv2Attack(id=31, invasion_id=inv_id, user_id=1, card_id=101,
            card_power=20, damage=41, is_crit=critical, narrative="ضربهٔ سنگین بر زره", joined_at=NOW))
    return inv_id


@pytest.mark.asyncio
async def test_phase8a_announcement_delivers_new_enemy_label(rendered_delivery):
    delivery = rendered_delivery
    inv_id = await create_inv(delivery.factory)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=123)))
    await presentation.InvasionPresentation().announce(bot, inv_id)
    spy = delivery.spies[presentation.__name__, "compose_announcement_image"]
    spy.assert_called_once_with("soldier", "درّه ماه", "شیاسیب پیاده")
    photo = Path(bot.send_photo.await_args.kwargs["photo"].path)
    assert photo.read_bytes() == compose.compose_announcement_image(*spy.call_args.args)
    # The new renderer cannot reuse legacy cache files even with the same manifest.
    assert photo.name != cache.cache_key("announcement", inv_id, "soldier") + ".jpg"
    async with delivery.factory() as session:
        assert (await session.get(Inv2Invasion, inv_id)).status == "ACTIVE"


@pytest.mark.asyncio
@pytest.mark.parametrize("critical", [False, True])
async def test_phase8a_join_delivers_card_hp_and_critical_style(rendered_delivery, critical):
    delivery = rendered_delivery
    inv_id = await _phase8a_attack(delivery.factory, critical=critical)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=True))
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, 31)
    spy = delivery.spies[presentation.__name__, "compose_join_image"]
    spy.assert_called_once_with("soldier", ART.read_bytes(), "داریوش", 41, critical, 159)
    kwargs = bot.send_photo.await_args.kwargs
    marker = "💥 آسیب وارده به دشمن: **۴۱**" if critical else "🔥 آسیب وارده به دشمن: **۴۱**"
    assert kwargs["caption"] == "ضربهٔ سنگین بر زره\n\n" + marker
    assert kwargs["reply_to_message_id"] == 123
    assert Path(kwargs["photo"].path).read_bytes() == compose.compose_join_image(*spy.call_args.args)
    # Presentation does not apply damage a second time or grant rewards.
    async with delivery.factory() as session:
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 159
        assert await session.scalar(select(func.count()).select_from(Inv2Attack)) == 1
        assert await session.scalar(select(func.count()).select_from(Inv2Reward)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["VICTORY", "DEFEAT"])
async def test_phase8a_result_uses_aggregated_rewards_only_for_victory(rendered_delivery, status):
    delivery = rendered_delivery
    inv_id = await create_inv(delivery.factory, status=status, message_id=123)
    async with delivery.factory.begin() as session:
        for user, kind, amount in [(1, "golden_apple", 2), (2, "golden_apple", 3),
                                    (1, "diamond_apple", 1), (2, "obsidian_apple", 2)]:
            session.add(Inv2Reward(invasion_id=inv_id, user_id=user, reward_type=kind,
                                  reward_amount=amount, granted_at=NOW))
    bot = SimpleNamespace(edit_message_caption=AsyncMock(return_value=True),
                          send_photo=AsyncMock(return_value=True))
    assert await presentation.InvasionPresentation().finalize(bot, inv_id) is True
    lines = ("سیب طلایی ×۵", "سیب الماسی ×۱", "سیب ابسیدینی ×۲") if status == "VICTORY" else ()
    spy = delivery.spies[presentation.__name__, "compose_result_image"]
    spy.assert_called_once_with(status.lower(), "درّه ماه", "soldier", lines)
    kwargs = bot.send_photo.await_args.kwargs
    assert Path(kwargs["photo"].path).read_bytes() == compose.compose_result_image(*spy.call_args.args)
    original_caption = bot.edit_message_caption.await_args.kwargs["caption"]
    # Phase 2: the reward block keeps a blank line and is fitted, not appended,
    # so the reward line itself survives a long narrative.
    suffix = "\n\n🎁 پاداش مدافعان: **" + "، ".join(lines) + "**" if lines else ""
    assert kwargs["caption"] == presentation._fit_caption(original_caption, suffix, limit=1020)
    async with delivery.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2Reward)) == 4
        assert await session.scalar(select(func.count()).select_from(UserItem)) == 0
        assert ((await session.get(Inv2Invasion, inv_id)).delivered_at is not None) == (status == "VICTORY")


@pytest.mark.asyncio
async def test_phase8a_victory_without_rewards_uses_empty_lines(rendered_delivery):
    delivery = rendered_delivery
    inv_id = await create_inv(delivery.factory, status="VICTORY", message_id=123)
    bot = SimpleNamespace(edit_message_caption=AsyncMock(return_value=True), send_photo=AsyncMock(return_value=True))
    assert await presentation.InvasionPresentation().finalize(bot, inv_id)
    delivery.spies[presentation.__name__, "compose_result_image"].assert_called_once_with(
        "victory", "درّه ماه", "soldier", ())
    assert "پاداش مدافعان" not in bot.send_photo.await_args.kwargs["caption"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["hp", "username", "card"])
async def test_phase8a_join_cache_tracks_visible_inputs(rendered_delivery, monkeypatch, tmp_path, change):
    delivery = rendered_delivery
    inv_id = await _phase8a_attack(delivery.factory)
    card_path = tmp_path / "card.jpg"
    card_path.write_bytes(ART.read_bytes())
    monkeypatch.setattr(presentation, "artwork_path", lambda character: card_path)
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=True))
    service = presentation.InvasionPresentation()
    await service.on_join(bot, inv_id, 1, 31)
    first = Path(bot.send_photo.await_args.kwargs["photo"].path)
    await service.on_join(bot, inv_id, 1, 31)
    assert Path(bot.send_photo.await_args.kwargs["photo"].path) == first
    assert len(delivery.builds) == 1  # Exact replay hits cache.
    if change == "card":
        # Same filename, different bytes: cache must not retain the previous art.
        Image.new("RGB", (420, 580), "#224466").save(card_path, "JPEG")
    else:
        async with delivery.factory.begin() as session:
            if change == "hp":
                (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining = 100
            else:
                (await session.get(User, 1)).first_name = "نگهبان"
    await service.on_join(bot, inv_id, 1, 31)
    second = Path(bot.send_photo.await_args.kwargs["photo"].path)
    assert first != second and first.read_bytes() != second.read_bytes()
    assert len(delivery.builds) == 2


@pytest.mark.asyncio
async def test_phase8a_result_cache_tracks_reward_lines(rendered_delivery):
    delivery = rendered_delivery
    inv_id = await create_inv(delivery.factory, status="VICTORY", message_id=123)
    bot = SimpleNamespace(edit_message_caption=AsyncMock(return_value=True), send_photo=AsyncMock(return_value=True))
    service = presentation.InvasionPresentation()
    await service.finalize(bot, inv_id)
    first = Path(bot.send_photo.await_args.kwargs["photo"].path)
    async with delivery.factory.begin() as session:
        session.add(Inv2Reward(invasion_id=inv_id, user_id=1, reward_type="golden_apple",
                              reward_amount=3, granted_at=NOW))
    await service.finalize(bot, inv_id)
    second = Path(bot.send_photo.await_args.kwargs["photo"].path)
    assert first != second and first.read_bytes() != second.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_card", [False, True])
async def test_phase8a_private_preview_uses_byte_api_without_db_writes(rendered_delivery, has_card):
    delivery = rendered_delivery
    if has_card:
        async with delivery.factory.begin() as session:
            session.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
    msg = SimpleNamespace(text="/inv2_preview soldier", from_user=SimpleNamespace(id=1, first_name="داریوش"),
        chat=SimpleNamespace(type="private"), answer_media_group=AsyncMock(return_value=True), answer=AsyncMock())
    await inv2_admin.preview(msg)
    msg.answer.assert_not_awaited()
    msg.answer_media_group.assert_awaited_once()
    assert len(msg.answer_media_group.await_args.kwargs["media"]) == 4
    delivery.spies[inv2_admin.__name__, "compose_announcement_image"].assert_called_once_with(
        "soldier", "درّه ماه", "شیاسیب پیاده")
    join_spy = delivery.spies[inv2_admin.__name__, "compose_join_image"]
    assert join_spy.call_count == 1
    args = join_spy.call_args.args
    assert args[0] == "soldier" and args[2:] == ("داریوش", 42, False, 958)
    assert args[1] == compose.load_card_image_bytes(ART if has_card else None, placeholder=not has_card)
    result_spy = delivery.spies[inv2_admin.__name__, "compose_result_image"]
    assert [call.args for call in result_spy.call_args_list] == [
        ("victory", "درّه ماه", "soldier", ()), ("defeat", "درّه ماه", "soldier", ())]
    async with delivery.factory() as session:
        for model in (Inv2Invasion, Inv2Attack, Inv2Reward, UserItem, Inv2GroupState):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_phase8a_disabled_flag_prevents_composition(rendered_delivery):
    delivery = rendered_delivery
    MaintenanceService._invasion_enabled.return_value = False
    bot = SimpleNamespace(send_photo=AsyncMock(), edit_message_caption=AsyncMock())
    service = presentation.InvasionPresentation()
    await service.announce(bot, 1)
    await service.on_join(bot, 1, 1, 1)
    await service.finalize(bot, 1)
    assert delivery.records == []
    for spy in delivery.spies.values():
        spy.assert_not_called()
    bot.send_photo.assert_not_awaited()
    bot.edit_message_caption.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase8a_failed_result_send_does_not_mark_delivered(rendered_delivery):
    delivery = rendered_delivery
    inv_id = await create_inv(delivery.factory, status="VICTORY", message_id=123)
    bot = SimpleNamespace(edit_message_caption=AsyncMock(return_value=True),
        send_photo=AsyncMock(side_effect=TelegramBadRequest(
            method=SendPhoto(chat_id=-100, photo="missing"), message="group blocked")))
    assert await presentation.InvasionPresentation().finalize(bot, inv_id) is False
    delivery.spies[presentation.__name__, "compose_result_image"].assert_called_once()
    presentation.record_send_failure.assert_awaited_once_with(-100)
    async with delivery.factory() as session:
        assert (await session.get(Inv2Invasion, inv_id)).delivered_at is None


@pytest.mark.asyncio
async def test_phase8a_missing_card_never_falls_back_to_preview(rendered_delivery, monkeypatch, tmp_path):
    delivery = rendered_delivery
    inv_id = await _phase8a_attack(delivery.factory)
    monkeypatch.setattr(presentation, "artwork_path", lambda character: tmp_path / "missing.jpg")
    bot = SimpleNamespace(send_photo=AsyncMock())
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, 31)
    bot.send_photo.assert_not_awaited()
    assert delivery.builds == []
    presentation.record_send_failure.assert_not_awaited()  # Not a Telegram failure.
    async with delivery.factory() as session:
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 159


@pytest.mark.asyncio
async def test_phase8a_render_failure_keeps_announcement_pending(rendered_delivery):
    delivery = rendered_delivery
    inv_id = await create_inv(delivery.factory)
    delivery.spies[presentation.__name__, "compose_announcement_image"].side_effect = OSError("missing plate")
    bot = SimpleNamespace(send_photo=AsyncMock())
    await presentation.InvasionPresentation().announce(bot, inv_id)
    bot.send_photo.assert_not_awaited()
    assert list(delivery.directory.glob("*.jpg")) == []
    async with delivery.factory() as session:
        inv = await session.get(Inv2Invasion, inv_id)
        assert inv.status == "PENDING" and inv.message_id is None and inv.started_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["region", "enemy_label"])
async def test_phase8a_announcement_cache_tracks_labels(rendered_delivery, change):
    delivery = rendered_delivery
    inv_id = await create_inv(delivery.factory)
    # Rejected send leaves PENDING, so a later attempt can use corrected labels.
    bot = SimpleNamespace(send_photo=AsyncMock(side_effect=TelegramBadRequest(
        method=SendPhoto(chat_id=-100, photo="missing"), message="group blocked")))
    service = presentation.InvasionPresentation()
    await service.announce(bot, inv_id)
    first = delivery.records[-1][1]
    async with delivery.factory.begin() as session:
        if change == "region":
            (await session.get(Inv2Region, "moon")).name_fa = "درّه"
        else:
            (await session.get(Inv2Invasion, inv_id)).enemy_name_fa = "شیاسیب"
    await service.announce(bot, inv_id)
    second = delivery.records[-1][1]
    assert first != second and first.read_bytes() != second.read_bytes()
