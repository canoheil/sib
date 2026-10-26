"""Private Phase 6 controls: isolated SQLite, no live Bot or production DB."""
import importlib.util
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Integer, create_engine, func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models import (AdminLog, Base, Character, Group, Inv2Attack,
    Inv2GroupMember, Inv2GroupState, Inv2Invasion, Inv2Region, Inv2Reward,
    PlayerCard, SeedlingAward, SeedlingInventory, SystemConfig, User, UserItem)
from sib.handlers import inv2_controls as handler, inv2_dashboard as dashboard
from sib.services.invasion_v2 import admin_controls as ops, admin_runtime, circuit_breaker
from sib.services.invasion_v2.farsi import fa_number
from sib.services.invasion_v2.power_resolver import set_power_resolver
from sib.services.maintenance import service as maintenance
from sib.services.maintenance.service import MaintenanceService

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    from sib.database.models import GroupCastle
    tables = [GroupCastle.__table__, User.__table__, Group.__table__, Character.__table__, PlayerCard.__table__,
              Inv2Region.__table__, Inv2Invasion.__table__, Inv2Attack.__table__, Inv2Reward.__table__,
              Inv2GroupState.__table__, Inv2GroupMember.__table__, AdminLog.__table__,
              SystemConfig.__table__, UserItem.__table__,
              SeedlingInventory.__table__, SeedlingAward.__table__]
    columns = [UserItem.__table__.c.id, SeedlingAward.__table__.c.id]
    original_types = [column.type for column in columns]
    for column in columns:
        column.type = Integer()  # SQLite needs INTEGER for automatic PKs
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=tables)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

        @asynccontextmanager
        async def session():
            async with factory() as s:
                try:
                    yield s
                    await s.commit()
                except Exception:
                    await s.rollback()
                    raise

        monkeypatch.setattr(handler, "get_session", session)
        monkeypatch.setattr(dashboard, "get_session", session)
        monkeypatch.setattr(maintenance, "get_session", session)
        monkeypatch.setattr(handler, "is_admin", lambda uid: uid in (1, 2))
        monkeypatch.setattr(dashboard, "is_admin", lambda uid: uid in (1, 2))
        monkeypatch.setattr(circuit_breaker, "_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(ops, "get_power_resolver", lambda: SimpleNamespace(
            average_main_card_power=AsyncMock(return_value=20.0)))
        async with factory.begin() as s:
            s.add_all([User(id=1, first_name="مدیر"), User(id=2, first_name="مدیر دوم"),
                User(id=3, first_name="بازیکن"), Group(id=-100, title="گروه", is_active=True),
                Group(id=-200, title="گروه دیگر", is_active=False),
                Inv2Region(code="moon", name_fa="درّه ماه", controlling_faction="فرزندان ماه"),
                Character(id=10, code="sib_sabz", name_fa="سیب سبز", power=20, speed=20, intelligence=20),
                SystemConfig(key="invasion_enabled", value="false")])
        admin_runtime.last_successful_tick = None
        admin_runtime.last_error = None
        maintenance.invalidate_invasion_config_cache()
        yield factory, session
    finally:
        for column, original_type in zip(columns, original_types):
            column.type = original_type
        set_power_resolver(None)
        maintenance.invalidate_invasion_config_cache()
        await engine.dispose()


def event(text: str, *, user=1, chat_type="private"):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=user, first_name="مدیر"),
        chat=SimpleNamespace(type=chat_type), answer=AsyncMock(return_value=True))


async def inv(factory, *, status="ACTIVE", hp=300, group=-100):
    async with factory.begin() as s:
        item = Inv2Invasion(group_id=group, region_code="moon", enemy_type="soldier",
            enemy_name_fa="شیاسیب پیاده", enemy_hp=hp, enemy_hp_remaining=hp,
            difficulty_multiplier=4., status=status, scheduled_at=NOW,
            started_at=NOW if status == "ACTIVE" else None,
            ends_at=NOW + timedelta(minutes=5) if status == "ACTIVE" else None,
            occurrence_key=f"phase6:{status}:{hp}:{group}")
        s.add(item)
        await s.flush()
        return item.id


@pytest.mark.asyncio
async def test_every_command_rejects_nonadmin_without_writes(db):
    factory, _ = db
    cases = {
        "inv2_status": "", "inv2_toggle": "on", "inv2_force_spawn": "-100",
        "inv2_force_end": "1 victory", "inv2_regions": "", "inv2_reset_region": "moon",
        "inv2_mute": "-100 2", "inv2_unmute": "-100", "inv2_group_info": "-100",
        "inv2_invasions": "", "inv2_config": "", "inv2_set": "inv2_member_active_window_days 5",
        "inv2_health": "",
    }
    for name, args in cases.items():
        msg = event(f"/{name} {args}", user=999)
        await handler.inv2_command(msg)
        assert "دسترسی ندارید" in msg.answer.await_args.kwargs["text"]
    sim = event("/inv2_simulate -100 soldier", user=999)
    await handler.simulate_command(sim)
    assert "دسترسی ندارید" in sim.answer.await_args.kwargs["text"]
    await dashboard.dashboard(event("/inv2_dashboard", user=999))
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 0
        assert await s.scalar(select(SystemConfig.value).where(SystemConfig.key == "invasion_enabled")) == "false"


@pytest.mark.asyncio
async def test_toggle_cache_invalidation_and_audit(db):
    factory, _ = db
    assert not await MaintenanceService._invasion_enabled()
    assert maintenance._invasion_flag_expires_at > 0
    on = event("/inv2_toggle on")
    await handler.inv2_command(on)
    assert "فعال شد" in on.answer.await_args.kwargs["text"]
    assert maintenance._invasion_flag_expires_at == 0
    assert await MaintenanceService._invasion_enabled()
    off = event("/inv2_toggle off")
    await handler.inv2_command(off)
    assert not await MaintenanceService._invasion_enabled()
    async with factory() as s:
        assert (await s.get(SystemConfig, "invasion_enabled")).value == "false"
        assert [r.action for r in (await s.scalars(select(AdminLog))).all()] == ["inv2_toggle", "inv2_toggle"]


@pytest.mark.asyncio
async def test_spawn_respects_limit_mute_group_and_flag(db, monkeypatch):
    factory, _ = db
    # Real is_group_available gate: disabled flag refuses even manual spawn.
    monkeypatch.setattr(circuit_breaker, "_enabled", AsyncMock(return_value=False))
    assert "غیرفعال" in await handler._run(1, "inv2_force_spawn", ["-100", "moon", "soldier"])
    circuit_breaker._enabled.return_value = True
    first = await handler._run(1, "inv2_force_spawn", ["-100", "moon", "soldier"])
    assert "یورش شمارهٔ" in first
    assert "سقف" in await handler._run(1, "inv2_force_spawn", ["-100"])
    async with factory.begin() as s:
        s.add(Inv2GroupState(group_id=-100, muted_until=NOW + timedelta(hours=2)))
    assert "خاموش" in await handler._run(1, "inv2_force_spawn", ["-100"])
    assert "غیرفعال" in await handler._run(1, "inv2_force_spawn", ["-200"])
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(Inv2Invasion)) == 1
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 5


@pytest.mark.asyncio
async def test_force_end_victory_rewards_once_and_defeat_region(db):
    factory, _ = db
    win_id = await inv(factory, hp=1)
    async with factory.begin() as s:
        s.add(PlayerCard(id=101, user_id=3, character_id=10, level=1, is_favorite=True))
        s.add(Inv2Attack(invasion_id=win_id, user_id=3, card_id=101, card_power=20,
            damage=20, is_crit=False, narrative="ضربه", joined_at=NOW))
    msg = await handler._run(1, "inv2_force_end", [str(win_id), "victory"])
    assert "پیروزی" in msg
    async with factory() as s:
        before = [(r.reward_type, r.reward_amount) for r in (await s.scalars(select(Inv2Reward))).all()]
        items = [(r.item_key, r.quantity) for r in (await s.scalars(select(UserItem))).all()]
    assert before and ("golden_apple", 5) in before
    assert "پیش‌تر" in await handler._run(1, "inv2_force_end", [str(win_id), "victory"])
    assert "فعال" in await handler._run(1, "inv2_force_end", [str(win_id), "defeat"])
    loss_id = await inv(factory, hp=99)
    assert "شکست" in await handler._run(1, "inv2_force_end", [str(loss_id), "defeat"])
    async with factory() as s:
        assert before == [(r.reward_type, r.reward_amount) for r in (await s.scalars(select(Inv2Reward))).all()]
        assert items == [(r.item_key, r.quantity) for r in (await s.scalars(select(UserItem))).all()]
        assert (await s.get(Inv2Region, "moon")).is_lost_to_enemy
        assert (await s.get(Inv2Region, "moon")).last_invasion_id == loss_id
        state = await s.get(Inv2GroupState, -100)
        assert (state.total_victories, state.total_defeats) == (1, 1)


@pytest.mark.asyncio
async def test_set_only_known_inv2_keys_and_resets_cache(db):
    factory, _ = db
    for key in ("invasion_enabled", "invasion_interval_seconds", "invalid", "inv2_nonexistent"):
        result = await handler._run(1, "inv2_set", [key, "3"])
        assert "مجاز نیست" in result
    msg = event("/inv2_set inv2_member_active_window_days 5")
    await handler.inv2_command(msg)
    assert "ثبت شد" in msg.answer.await_args.kwargs["text"]
    assert maintenance._invasion_flag_expires_at == 0
    async with factory() as s:
        assert await s.scalar(select(SystemConfig.value).where(
            SystemConfig.key == "inv2_member_active_window_days")) == "5"
        assert await s.scalar(select(SystemConfig.value).where(
            SystemConfig.key == "invasion_enabled")) == "false"


@pytest.mark.asyncio
async def test_simulation_is_readonly_except_required_audit(db):
    factory, _ = db
    async with factory.begin() as s:
        s.add(PlayerCard(id=102, user_id=3, character_id=10, level=2, is_favorite=True))
        s.add(Inv2GroupMember(user_id=3, group_id=-100, last_seen_at=NOW))
        # A6: group power is known only from at least
        # invasion_min_players_with_card (3) members with a Main Card.
        for extra in (4, 5):
            s.add(User(id=extra, first_name="بازیکن"))
            s.add(PlayerCard(id=100 + extra, user_id=extra, character_id=10, level=2, is_favorite=True))
            s.add(Inv2GroupMember(user_id=extra, group_id=-100, last_seen_at=NOW))
    async with factory() as s:
        before = {table: await s.scalar(select(func.count()).select_from(table)) for table in
                  (Inv2Invasion, Inv2Attack, Inv2Reward, Inv2GroupState, UserItem)}
    msg = event("/inv2_simulate -100 soldier")
    await handler.simulate_command(msg)
    text = msg.answer.await_args.kwargs["text"]
    assert all(f"{fa_number(n)} نگهبان" in text for n in (1, 3, 5, 10))
    assert "بخت پیروزی" in text and "جان دشمن" in text
    async with factory() as s:
        assert before == {table: await s.scalar(select(func.count()).select_from(table)) for table in before}
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 1


@pytest.mark.asyncio
async def test_dashboard_owner_only_and_callback_audited(db, monkeypatch):
    factory, _ = db
    msg = event("/inv2_dashboard")
    await dashboard.dashboard(msg)
    keyboard = msg.answer.await_args.kwargs["reply_markup"]
    assert len(keyboard.inline_keyboard) == 3
    assert keyboard.inline_keyboard[0][0].callback_data == "inv2adm:1:on"
    cb_message = SimpleNamespace(chat=SimpleNamespace(type="private"), edit_text=AsyncMock(return_value=True))
    stolen = SimpleNamespace(from_user=SimpleNamespace(id=2), message=cb_message,
        data="inv2adm:1:on", answer=AsyncMock(return_value=True))
    await dashboard.dashboard_action(stolen)
    cb_message.edit_text.assert_not_awaited()
    assert "مدیر دیگری" in stolen.answer.await_args.kwargs["text"]
    async with factory() as s:
        assert await s.scalar(select(SystemConfig.value).where(SystemConfig.key == "invasion_enabled")) == "false"
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 1
    owner = SimpleNamespace(from_user=SimpleNamespace(id=1), message=cb_message,
        data="inv2adm:1:refresh", answer=AsyncMock(return_value=True))
    await dashboard.dashboard_action(owner)
    cb_message.edit_text.assert_awaited_once()
    assert cb_message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data == "inv2adm:1:off"
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 2


@pytest.mark.asyncio
async def test_read_commands_mute_unmute_reset_and_bad_inputs_audited(db):
    factory, _ = db
    assert "نواحی" in await handler._run(1, "inv2_regions", [])
    assert "سلامت" in await handler._run(1, "inv2_health", [])
    assert "خاموش شد" in await handler._run(1, "inv2_mute", ["-100", "2"])
    assert "خاموش تا" in await handler._run(1, "inv2_group_info", ["-100"])
    assert "خاموشی بیرون" in await handler._run(1, "inv2_unmute", ["-100"])
    assert "آزاد شد" in await handler._run(1, "inv2_reset_region", ["moon"])
    assert "تنظیمات" in await handler._run(1, "inv2_config", [])
    assert "یورش‌های اخیر" in await handler._run(1, "inv2_invasions", [])
    status_text = await handler._run(1, "inv2_status", [])
    assert "وضعیت یورش" in status_text
    assert "ساعت‌های اسپان (تهران): 20:00" in status_text
    assert "پنجره هر اسلات: 30 دقیقه" in status_text
    assert "فاصله" not in status_text  # dead interval reading was removed
    assert "ورودی" in await handler._run(1, "inv2_toggle", [])
    assert "نامعتبر" in await handler._run(1, "inv2_mute", ["n/a", "a"])
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 11


@pytest.mark.asyncio
async def test_status_falls_back_to_default_schedule_when_config_missing(db):
    # No invasion_spawn_hours/invasion_slot_window_minutes row exists in this
    # fixture's system_config table at all: the status text must still show
    # the scheduler's own defaults instead of raising or printing blanks.
    status_text = await handler._run(1, "inv2_status", [])
    assert "ساعت‌های اسپان (تهران): 20:00" in status_text
    assert "پنجره هر اسلات: 30 دقیقه" in status_text


@pytest.mark.asyncio
async def test_status_renders_custom_schedule_config(db):
    factory, _ = db
    async with factory.begin() as s:
        s.add_all([
            SystemConfig(key="invasion_spawn_hours", value="18,20"),
            SystemConfig(key="invasion_slot_window_minutes", value="120"),
        ])
    status_text = await handler._run(1, "inv2_status", [])
    assert "ساعت‌های اسپان (تهران): 18:00، 20:00" in status_text
    assert "پنجره هر اسلات: 120 دقیقه" in status_text


def test_migration_038_up_down():
    path = Path(__file__).resolve().parents[2] / "database/migrations/versions/038_inv2_admin_logs.py"
    spec = importlib.util.spec_from_file_location("migration_038", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "037_inv2_group_members"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        assert "admin_logs" in inspect(conn).get_table_names()
        assert len(inspect(conn).get_indexes("admin_logs")) == 2
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
        assert "admin_logs" not in inspect(conn).get_table_names()
    engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_actions_edit_owner_message_and_audit(db):
    factory, _ = db
    text = AsyncMock(return_value=True)
    cb = SimpleNamespace(from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat=SimpleNamespace(type="private"), edit_text=text),
        answer=AsyncMock(return_value=True), data="")
    for action in ("on", "off", "regions", "muted", "active", "config", "health", "simulate"):
        cb.data = f"inv2adm:1:{action}"
        await dashboard.dashboard_action(cb)
    assert text.await_count == 8
    assert "شبیه‌سازی" in text.await_args.kwargs["text"]
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 8
        assert await s.scalar(select(SystemConfig.value).where(SystemConfig.key == "invasion_enabled")) == "false"


@pytest.mark.asyncio
async def test_dashboard_invalid_callback_and_group_cannot_mutate(db):
    factory, _ = db
    cb = SimpleNamespace(from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat=SimpleNamespace(type="private"), edit_text=AsyncMock()),
        answer=AsyncMock(return_value=True), data="inv2adm:1:bad")
    await dashboard.dashboard_action(cb)
    assert "معتبر نیست" in cb.answer.await_args.kwargs["text"]
    cb.data = "inv2adm:wrong:on"
    await dashboard.dashboard_action(cb)
    assert "مدیر دیگری" in cb.answer.await_args.kwargs["text"]
    cb.message.chat.type = "group"
    cb.data = "inv2adm:1:on"
    await dashboard.dashboard_action(cb)
    assert "دسترسی ندارید" in cb.answer.await_args.kwargs["text"]
    cb.message.edit_text.assert_not_awaited()
    await dashboard.dashboard(event("/inv2_dashboard", chat_type="group"))
    await handler.inv2_command(event("/inv2_toggle on", chat_type="group"))
    await handler.simulate_command(event("/inv2_simulate -100", chat_type="group"))
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 0
        assert await s.scalar(select(SystemConfig.value).where(SystemConfig.key == "invasion_enabled")) == "false"


@pytest.mark.asyncio
async def test_command_invalid_operation_and_sim_bad_input_are_audited(db, monkeypatch):
    factory, _ = db
    assert "مجاز نیست" in await handler._run(1, "inv2_set", ["x", "3"])
    assert "ورودی" in await handler._run(1, "inv2_force_spawn", [])
    assert "نامعتبر" in await handler._run(1, "inv2_force_end", ["abc", "victory"])
    # Read-only estimate refuses to invent a power for missing group.
    sim = event("/inv2_simulate -987 shadow")
    await handler.simulate_command(sim)
    assert "نامشخص" in sim.answer.await_args.kwargs["text"]
    invalid = event("/inv2_simulate nonsense")
    await handler.simulate_command(invalid)
    assert "نمونه" in invalid.answer.await_args.kwargs["text"]
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 5


@pytest.mark.asyncio
async def test_admin_error_is_persian_and_no_uncaught_exception(db, monkeypatch):
    @asynccontextmanager
    async def broken():
        raise RuntimeError("DB down")
        yield
    monkeypatch.setattr(handler, "get_session", broken)
    result = await handler._run(1, "inv2_status", [])
    assert "انجام نشد" in result


@pytest.mark.asyncio
async def test_spawn_rejects_invalid_region_and_enemy_without_creating_rows(db):
    factory, _ = db
    assert "کد ناحیه" in await handler._run(1, "inv2_force_spawn", ["-100", "unknown", "soldier"])
    assert "گونهٔ دشمن" in await handler._run(1, "inv2_force_spawn", ["-100", "moon", "unknown"])
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(Inv2Invasion)) == 0
        assert await s.scalar(select(func.count()).select_from(AdminLog)) == 2
