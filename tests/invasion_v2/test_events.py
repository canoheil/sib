"""IV3 random events: planning, modifiers, chests, delivery and migration.

Everything runs on isolated SQLite with mocked Telegram; no network, no
production database and no bot startup. Row-lock races cannot be exercised on
SQLite, so the tests assert the *arbiters* that work everywhere: the CAS
counter and the two unique constraints on ``inv2_event_claims``.
"""

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
from aiogram.enums import ChatType
from sqlalchemy import Integer, func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sib.database.models import (
    AppleTransaction, AppleWallet, Base, Character, Group, GroupCastle, Inv2Attack,
    Inv2EventClaim, Inv2EventEffect, Inv2GroupState, Inv2Invasion, Inv2RandomEvent,
    Inv2Region, Inv2Reward, PlayerCard, SystemConfig, User, UserItem,
)
from sib.handlers import inv2_join
from sib.services.invasion_v2 import event_catalog as catalog
from sib.services.invasion_v2 import event_scheduler, presentation
from sib.services.invasion_v2.enums import InvasionStatus
from sib.services.invasion_v2.event_scheduler import (
    APPLIED, CLOSED, PLANNED, SKIPPED, InvasionEventScheduler, Modifiers,
    PlanConfig, build_plan, choose_templates, claim_loot, load_active_modifiers,
    plan_offsets,
)
from sib.services.maintenance.service import MaintenanceService

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)
TABLES = [
    GroupCastle.__table__, User.__table__, Group.__table__, Character.__table__,
    PlayerCard.__table__, Inv2Region.__table__, Inv2Invasion.__table__,
    Inv2Attack.__table__, Inv2Reward.__table__, Inv2GroupState.__table__,
    UserItem.__table__, SystemConfig.__table__, AppleWallet.__table__,
    AppleTransaction.__table__, Inv2RandomEvent.__table__,
    Inv2EventEffect.__table__, Inv2EventClaim.__table__,
]
BIGINT_PKS = [
    UserItem.__table__.c.id, AppleTransaction.__table__.c.id,
    Inv2EventClaim.__table__.c.id,
]


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    originals = [column.type for column in BIGINT_PKS]
    for column in BIGINT_PKS:
        column.type = Integer()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=TABLES)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=True))
        monkeypatch.setattr(presentation, "is_group_available", AsyncMock(return_value=True))
        monkeypatch.setattr(presentation, "record_send_success", AsyncMock())
        monkeypatch.setattr(presentation, "record_send_failure", AsyncMock())
        monkeypatch.setattr(presentation, "schedule_hp_edit", lambda *a, **k: None)
        async with factory.begin() as session:
            session.add_all([
                User(id=1, first_name="آرش"), User(id=2, first_name="نیلوفر"),
                User(id=3, first_name="سهراب"), User(id=4, first_name="رستم"),
                Group(id=-100, title="گروه"),
                Character(id=10, code="sib_sabz", name_fa="سیب سبز",
                          power=20, speed=20, intelligence=20),
                Inv2Region(code="moon", name_fa="درّه ماه", controlling_faction="فرزندان ماه"),
            ])
            session.add(SystemConfig(key="inv2_events_enabled", value=json.dumps(True)))
        yield factory
    finally:
        for column, original in zip(BIGINT_PKS, originals):
            column.type = original
        await presentation.cancel_pending_hp_edits()
        await engine.dispose()


async def create_inv(factory, *, status="ACTIVE", hp=1000, started_ago=0, window=1800):
    started = NOW - timedelta(seconds=started_ago)
    async with factory.begin() as session:
        inv = Inv2Invasion(
            group_id=-100, region_code="moon", enemy_type="soldier",
            enemy_name_fa="شیاسیب پیاده", enemy_hp=hp, enemy_hp_remaining=hp,
            difficulty_multiplier=2.0, status=status, scheduled_at=started,
            started_at=started if status == "ACTIVE" else None,
            ends_at=started + timedelta(seconds=window) if status == "ACTIVE" else None,
            message_id=123, message_chat_id=-100,
            occurrence_key=f"events:{status}:{hp}:{started_ago}:{window}",
        )
        session.add(inv)
        await session.flush()
        return inv.id


async def add_event(factory, invasion_id, code, *, status=APPLIED, offset=0, duration=None,
                    message_id=777, applied_amount=None, loot_claimed=0, template=None):
    template = template or catalog.BY_CODE[code]
    async with factory.begin() as session:
        used = set((await session.scalars(
            select(Inv2RandomEvent.slot_no).where(
                Inv2RandomEvent.invasion_id == invasion_id
            )
        )).all())
        slot_no = next(slot for slot in range(1, 50) if slot not in used)
        event = Inv2RandomEvent(
            invasion_id=invasion_id, group_id=-100, slot_no=slot_no,
            template_code=code, category=template.category, status=status,
            scheduled_at=NOW + timedelta(seconds=offset),
            starts_at=NOW - timedelta(seconds=5) if status == "ACTIVE" else None,
            ends_at=(NOW + timedelta(seconds=duration if duration is not None
                                        else (template.duration_seconds or 120)))
            if status == "ACTIVE" else None,
            message_id=message_id, message_chat_id=-100 if message_id else None,
            narrative=template.narratives[0],
            applied_amount=applied_amount,
            duration_seconds=template.duration_seconds,
            loot_item_key=template.loot.reward_type if template.loot else None,
            loot_amount=(template.loot.default_amount if template.loot else None),
            loot_max_claims=3 if template.loot else None,
            loot_claimed=loot_claimed,
        )
        session.add(event)
        await session.flush()
        for kind, value in template.effects:
            session.add(Inv2EventEffect(event_id=event.id, kind=kind, value=value))
        return event.id


async def run_tick(factory, *, now=NOW, rng=None, stats=None):
    async with factory() as session:
        counts = await InvasionEventScheduler.tick(
            session, now=now, rng=rng or Random(7), stats=stats
        )
        await session.commit()
        return counts


def fake_bot(*, photo=None, text=None):
    """A bot whose send_photo/send_message behave as configured."""
    return SimpleNamespace(
        send_photo=AsyncMock(side_effect=photo) if isinstance(photo, Exception)
        else AsyncMock(return_value=photo or SimpleNamespace(
            chat=SimpleNamespace(id=-100), message_id=900)),
        send_message=AsyncMock(side_effect=text) if isinstance(text, Exception)
        else AsyncMock(return_value=text or SimpleNamespace(
            chat=SimpleNamespace(id=-100), message_id=901)),
        edit_message_caption=AsyncMock(return_value=True),
    )


# ---------------------------------------------------------------------------
# 1-3: planning
# ---------------------------------------------------------------------------
def test_plan_offsets_follow_the_configured_cadence():
    config = PlanConfig()
    thirty = plan_offsets(1800, config, Random(1))
    forty_five = plan_offsets(2700, config, Random(1))
    assert len(thirty) == 5 and len(forty_five) == 7
    for offsets, window in ((thirty, 1800), (forty_five, 2700)):
        assert offsets[0] == config.guard_head
        assert offsets[-1] == window - config.guard_tail
        gaps = [b - a for a, b in zip(offsets, offsets[1:])]
        assert all(config.min_gap <= gap <= config.max_gap for gap in gaps), gaps


def test_plan_offsets_degrade_instead_of_breaking_the_gap_floor():
    config = PlanConfig()
    assert plan_offsets(300, config, Random(1)) == [config.guard_head]
    assert plan_offsets(200, config, Random(1)) == []       # usable <= 60 s
    short = plan_offsets(420, config, Random(1))            # usable = 240 s
    assert len(short) == 1


def test_template_selection_never_repeats_and_always_offers_a_chest():
    config = PlanConfig()
    for seed in range(25):
        plan = build_plan(window_seconds=1800, hp_remaining=900, total_hp=1000,
                          config=config, rng=Random(seed))
        templates = [template for _, template in plan]
        assert len(templates) == 5
        assert len({t.code for t in templates}) == len(templates)
        assert any(t.category == catalog.LOOT for t in templates)
        assert templates[0].category != catalog.BOSS_BUFF
        offsets = [offset for offset, _ in plan]
        assert offsets == sorted(offsets)


def test_template_selection_respects_disabled_weights_and_full_hp_heal():
    config = PlanConfig(weights={template.code: 0 for template in catalog.EVENTS})
    chosen = choose_templates(5, config, hp_remaining=1000, total_hp=1000, rng=Random(2))
    assert chosen == []
    config = PlanConfig()
    heal = catalog.BY_CODE["boss_shadow_heal"]
    assert heal.eligible(hp_remaining=1000, total_hp=1000) is False
    assert heal.eligible(hp_remaining=999, total_hp=1000) is True


# ---------------------------------------------------------------------------
# 4: the tick plans once, and only under its own flag
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tick_plans_once_and_respects_the_kill_switch(db):
    inv_id = await create_inv(db)
    async with db.begin() as session:
        row = await session.get(SystemConfig, "inv2_events_enabled")
        row.value = json.dumps(False)
    assert await run_tick(db) == {"planned": 0, "applied": 0, "skipped": 0, "closed": 0}
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2RandomEvent)) == 0

    async with db.begin() as session:
        row = await session.get(SystemConfig, "inv2_events_enabled")
        row.value = json.dumps(True)
    stats = {}
    counts = await run_tick(db, stats=stats)
    assert counts["planned"] == 5 and stats["events_planned"] == 5
    await run_tick(db)
    async with db() as session:
        events = list((await session.scalars(
            select(Inv2RandomEvent).order_by(Inv2RandomEvent.slot_no)
        )).all())
        assert len(events) == 5
        assert [event.slot_no for event in events] == [1, 2, 3, 4, 5]
        assert all(event.status in (PLANNED, APPLIED) for event in events)
        assert all(event.narrative for event in events)
        assert await session.scalar(select(func.count()).select_from(Inv2EventEffect)) > 0


@pytest.mark.asyncio
async def test_pending_invasion_is_never_planned(db):
    await create_inv(db, status="PENDING")
    await run_tick(db)
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2RandomEvent)) == 0


# ---------------------------------------------------------------------------
# 5-6: instant effects are one-shot, clamped and non-lethal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_meteor_applies_once_and_never_kills(db):
    inv_id = await create_inv(db, hp=100)
    event_id = await add_event(db, inv_id, "meteor", status=PLANNED, offset=-1)
    await run_tick(db)
    async with db() as session:
        event = await session.get(Inv2RandomEvent, event_id)
        invasion = await session.get(Inv2Invasion, inv_id)
        assert event.status == APPLIED
        assert event.applied_amount == 8            # 8% of 100
        assert invasion.enemy_hp_remaining == 92
        assert invasion.status == InvasionStatus.ACTIVE.value
    await run_tick(db, now=NOW + timedelta(seconds=30))
    async with db() as session:
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 92


@pytest.mark.asyncio
async def test_meteor_leaves_at_least_one_hp(db):
    inv_id = await create_inv(db, hp=100)
    async with db.begin() as session:
        invasion = await session.get(Inv2Invasion, inv_id)
        invasion.enemy_hp_remaining = 3
    await add_event(db, inv_id, "meteor", status=PLANNED, offset=-1)
    await run_tick(db)
    async with db() as session:
        invasion = await session.get(Inv2Invasion, inv_id)
        event = await session.scalar(select(Inv2RandomEvent))
        assert invasion.enemy_hp_remaining == 1
        assert invasion.status == InvasionStatus.ACTIVE.value
        assert event.applied_amount == 2


@pytest.mark.asyncio
async def test_heal_is_capped_at_total_hp(db):
    inv_id = await create_inv(db, hp=1000)
    async with db.begin() as session:
        (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining = 995
    event_id = await add_event(db, inv_id, "boss_shadow_heal", status=PLANNED, offset=-1)
    await run_tick(db)
    async with db() as session:
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 1000
        assert (await session.get(Inv2RandomEvent, event_id)).applied_amount == 5


@pytest.mark.asyncio
async def test_events_are_skipped_when_the_battle_or_group_is_gone(db, monkeypatch):
    inv_id = await create_inv(db)
    await add_event(db, inv_id, "meteor", status=PLANNED, offset=-1)
    monkeypatch.setattr(event_scheduler, "is_group_available", AsyncMock(return_value=False))
    await run_tick(db)
    async with db() as session:
        assert (await session.get(Inv2RandomEvent, 1)).status == SKIPPED
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 1000


# ---------------------------------------------------------------------------
# 7-8: modifiers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_modifiers_fold_and_respect_their_window(db):
    inv_id = await create_inv(db)
    await add_event(db, inv_id, "blessing_apple", status="ACTIVE", duration=180)
    await add_event(db, inv_id, "boss_cold_breath", status="ACTIVE", duration=150)
    await add_event(db, inv_id, "boss_ward", status="ACTIVE", duration=120)
    async with db() as session:
        mods = await load_active_modifiers(session, inv_id, NOW)
        assert mods.active
        assert mods.damage_multiplier == pytest.approx(1.5)
        assert mods.crit_bonus == pytest.approx(-0.10)
        assert mods.crit_chance() == pytest.approx(0.0)
        assert mods.damage_cap_pct == pytest.approx(0.15)
        # One second before the window and after it: inactive.
        early = await load_active_modifiers(session, inv_id, NOW - timedelta(seconds=10))
        assert not early.active
        late = await load_active_modifiers(session, inv_id, NOW + timedelta(seconds=400))
        assert not late.active


@pytest.mark.asyncio
async def test_no_event_means_the_historical_arithmetic(db):
    inv_id = await create_inv(db)
    async with db() as session:
        assert await load_active_modifiers(session, inv_id, NOW) == Modifiers()
        # A pending (not yet announced) event never leaks into a join.
        await add_event(db, inv_id, "blessing_dawn", status=APPLIED)
    async with db() as session:
        assert await load_active_modifiers(session, inv_id, NOW) == Modifiers()


def test_crit_chance_default_is_unchanged_rng_consumption():
    from sib.services.invasion_v2.damage import compute_damage
    baseline = compute_damage(100, Random(11))
    explicit = compute_damage(100, Random(11), crit_chance=0.10)
    assert baseline == explicit


# ---------------------------------------------------------------------------
# 9-12: the chest
# ---------------------------------------------------------------------------
async def _chest(factory, *, code="loot_apple_sack", invasion_status="ACTIVE", duration=120):
    inv_id = await create_inv(factory, status=invasion_status)
    return inv_id, await add_event(factory, inv_id, code, status="ACTIVE", duration=duration)


@pytest.mark.asyncio
async def test_chest_first_three_then_empty_and_never_twice(db):
    _, event_id = await _chest(db)
    for user_id in (1, 2, 3, 4):
        async with db() as session:
            result = await claim_loot(session, event_id, user_id,
                                      group_id=-100, message_id=777, now=NOW)
            await session.commit()
        if user_id <= 3:
            assert result.ok and result.claimed == user_id
        else:
            assert result.status == "empty"
    async with db() as session:
        again = await claim_loot(session, event_id, 1, group_id=-100,
                                 message_id=777, now=NOW)
        assert again.status == "already"
        claims = list((await session.scalars(select(Inv2EventClaim))).all())
        assert sorted(claim.slot_no for claim in claims) == [1, 2, 3]
        ledger = list((await session.scalars(select(AppleTransaction))).all())
        assert len(ledger) == 3 and all(row.amount == 40 for row in ledger)
        wallet = await session.scalar(select(func.sum(AppleWallet.balance)))
        assert wallet == 120


@pytest.mark.asyncio
async def test_item_chest_grants_through_the_item_service(db):
    _, event_id = await _chest(db, code="loot_golden_chest")
    async with db() as session:
        result = await claim_loot(session, event_id, 1, group_id=-100,
                                  message_id=777, now=NOW)
        await session.commit()
        assert result.ok
        assert (await session.scalar(select(UserItem.quantity))) == 1
        assert (await session.scalar(select(UserItem.item_key))) == "apple_gold"
        duplicate = await claim_loot(session, event_id, 1, group_id=-100,
                                     message_id=777, now=NOW)
        assert duplicate.status == "already"


@pytest.mark.asyncio
async def test_chest_is_closed_after_its_window_or_its_battle(db):
    _, event_id = await _chest(db, duration=60)
    async with db() as session:
        expired = await claim_loot(session, event_id, 1, group_id=-100,
                                   message_id=777, now=NOW + timedelta(seconds=61))
        assert expired.status == "closed"
    inv_id, event_id = await _chest(db, invasion_status="VICTORY")
    async with db() as session:
        ended = await claim_loot(session, event_id, 1, group_id=-100,
                                 message_id=777, now=NOW)
        assert ended.status == "closed"


@pytest.mark.asyncio
async def test_chest_rejects_a_foreign_or_edited_message(db):
    _, event_id = await _chest(db)
    async with db() as session:
        assert (await claim_loot(session, event_id, 1, group_id=-999,
                                 message_id=777, now=NOW)).status == "wrong_message"
        assert (await claim_loot(session, event_id, 1, group_id=-100,
                                 message_id=999, now=NOW)).status == "wrong_message"
        assert (await claim_loot(session, 9999, 1, group_id=-100,
                                 message_id=777, now=NOW)).status == "missing"


# ---------------------------------------------------------------------------
# 13: join integration (multiplier, cap, crit bonus)
# ---------------------------------------------------------------------------
@pytest.fixture
def join_env(monkeypatch, db):
    ack = AsyncMock()
    monkeypatch.setattr(inv2_join, "_answer", ack)
    monkeypatch.setattr(presentation.InvasionPresentation, "on_join", AsyncMock())
    monkeypatch.setattr(presentation.InvasionPresentation, "finalize", AsyncMock())
    monkeypatch.setattr(inv2_join, "get_session", _session_provider(db))
    return ack


def _session_provider(factory):
    @asynccontextmanager
    async def session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    return session


def join_callback(inv_id, *, user_id=1, message_id=123):
    msg = Message(message_id=message_id, date=NOW,
                  chat=Chat(id=-100, type=ChatType.GROUP),
                  from_user=TgUser(id=user_id, is_bot=False, first_name="آرش"))
    return CallbackQuery(id=f"cb-{user_id}", from_user=msg.from_user,
                         chat_instance="group", message=msg,
                         data=f"inv2:join:{inv_id}")


async def _arm(factory, monkeypatch, *, damage=100, crit=False):
    async with factory.begin() as session:
        session.add(PlayerCard(id=101, user_id=1, character_id=10, level=1, is_favorite=True))
    monkeypatch.setattr(inv2_join, "compute_damage",
                        lambda power, rng, **kwargs: (damage, crit))


@pytest.mark.asyncio
async def test_join_without_an_event_keeps_the_historical_damage(db, join_env, monkeypatch):
    inv_id = await create_inv(db)
    await _arm(db, monkeypatch)
    await inv2_join.handle_inv2_join(join_callback(inv_id), SimpleNamespace())
    await asyncio.sleep(0)
    async with db() as session:
        assert (await session.scalar(select(Inv2Attack.damage))) == 100
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 900


@pytest.mark.asyncio
async def test_join_applies_the_event_multiplier(db, join_env, monkeypatch):
    inv_id = await create_inv(db)
    await add_event(db, inv_id, "blessing_apple", status="ACTIVE", duration=180)
    await _arm(db, monkeypatch)
    await inv2_join.handle_inv2_join(join_callback(inv_id), SimpleNamespace())
    await asyncio.sleep(0)
    async with db() as session:
        assert (await session.scalar(select(Inv2Attack.damage))) == 150
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 850


@pytest.mark.asyncio
async def test_join_applies_the_damage_cap_after_the_buff(db, join_env, monkeypatch):
    inv_id = await create_inv(db, hp=1000)
    await add_event(db, inv_id, "boss_ward", status="ACTIVE", duration=120)
    await _arm(db, monkeypatch, damage=400)
    await inv2_join.handle_inv2_join(join_callback(inv_id), SimpleNamespace())
    await asyncio.sleep(0)
    async with db() as session:
        # 15% of 1000 = 150, so the 400-damage hit is capped.
        assert (await session.scalar(select(Inv2Attack.damage))) == 150


@pytest.mark.asyncio
async def test_join_passes_the_event_crit_chance(db, join_env, monkeypatch):
    inv_id = await create_inv(db)
    await add_event(db, inv_id, "blessing_hawk", status="ACTIVE", duration=150)
    await _arm(db, monkeypatch)
    seen = {}

    def spy(power, rng, **kwargs):
        seen.update(kwargs)
        return 100, True

    monkeypatch.setattr(inv2_join, "compute_damage", spy)
    await inv2_join.handle_inv2_join(join_callback(inv_id), SimpleNamespace())
    await asyncio.sleep(0)
    assert seen["crit_chance"] == pytest.approx(0.35)


@pytest.mark.asyncio
async def test_chest_claim_does_not_create_an_attack(db, join_env, monkeypatch):
    inv_id, event_id = await _chest(db)
    async with db() as session:
        result = await claim_loot(session, event_id, 1, group_id=-100,
                                  message_id=777, now=NOW)
        await session.commit()
        assert result.ok
        assert await session.scalar(select(func.count()).select_from(Inv2Attack)) == 0
        assert (await session.get(Inv2Invasion, inv_id)).enemy_hp_remaining == 1000


# ---------------------------------------------------------------------------
# 14-16: delivery, fallback and closing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_image_falls_back_to_text(db, monkeypatch):
    inv_id = await create_inv(db)
    event_id = await add_event(db, inv_id, "meteor", status=APPLIED,
                               applied_amount=80, message_id=None)
    monkeypatch.setattr(presentation, "get_session", _session_provider(db))
    monkeypatch.setattr("sib.services.invasion_v2.imaging.templates.event_image_path",
                        lambda code: None)
    bot = fake_bot()
    assert await presentation.publish_event(bot, event_id) is True
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "شهاب‌سنگ" in text and "۸۰" in text
    async with db() as session:
        event = await session.get(Inv2RandomEvent, event_id)
        # One-shot event: shown once, no window to keep open.
        assert event.status == CLOSED and event.message_id == 901
        assert event.starts_at is None and event.resolved_at is not None


@pytest.mark.asyncio
async def test_loot_photo_carries_the_green_button_and_opens_a_window(db, monkeypatch):
    inv_id = await create_inv(db)
    event_id = await add_event(db, inv_id, "loot_golden_chest", status=APPLIED,
                               message_id=None)
    monkeypatch.setattr(presentation, "get_session", _session_provider(db))
    monkeypatch.setattr("sib.services.invasion_v2.imaging.templates.event_image_path",
                        lambda code: Path(__file__))
    bot = fake_bot()
    assert await presentation.publish_event(bot, event_id) is True
    kwargs = bot.send_photo.await_args.kwargs
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "🎁 باز کردن صندوق"
    assert button.callback_data == f"inv2ev:loot:{event_id}"
    assert button.style == "success"
    assert len(kwargs["caption"]) <= 1024
    async with db() as session:
        event = await session.get(Inv2RandomEvent, event_id)
        assert event.status == "ACTIVE"
        assert event.ends_at - event.starts_at == timedelta(seconds=120)


@pytest.mark.asyncio
async def test_failed_delivery_retries_then_skips(db, monkeypatch):
    inv_id = await create_inv(db)
    event_id = await add_event(db, inv_id, "blessing_dawn", status=APPLIED,
                               message_id=None)
    monkeypatch.setattr(presentation, "get_session", _session_provider(db))
    monkeypatch.setattr("sib.services.invasion_v2.imaging.templates.event_image_path",
                        lambda code: Path(__file__))
    bot = fake_bot(photo=RuntimeError("boom"), text=RuntimeError("boom"))
    for _ in range(3):
        assert await presentation.publish_event(bot, event_id) is False
    async with db() as session:
        event = await session.get(Inv2RandomEvent, event_id)
        assert event.status == SKIPPED and event.send_attempts == 3


@pytest.mark.asyncio
async def test_third_claim_removes_the_keyboard(db, monkeypatch):
    _, event_id = await _chest(db)
    for user_id in (1, 2, 3):
        async with db() as session:
            assert (await claim_loot(session, event_id, user_id, group_id=-100,
                                     message_id=777, now=NOW)).ok
            await session.commit()
    monkeypatch.setattr(presentation, "get_session", _session_provider(db))
    bot = fake_bot()
    await presentation.refresh_loot_caption(bot, event_id)
    kwargs = bot.edit_message_caption.await_args.kwargs
    assert kwargs["reply_markup"] is None
    assert "صندوقچه خالی شد" in kwargs["caption"]


@pytest.mark.asyncio
async def test_expired_chest_caption_is_closed_once(db, monkeypatch):
    inv_id = await create_inv(db)
    event_id = await add_event(db, inv_id, "loot_magic_chest", status="ACTIVE", duration=60)
    async with db.begin() as session:
        (await session.get(Inv2RandomEvent, event_id)).message_id = 777
    await run_tick(db, now=NOW + timedelta(seconds=90))
    async with db() as session:
        event = await session.get(Inv2RandomEvent, event_id)
        assert event.status == CLOSED and event.resolved_at is None
    monkeypatch.setattr(presentation, "get_session", _session_provider(db))
    bot = fake_bot()
    assert await presentation.close_expired_events(bot) == 1
    kwargs = bot.edit_message_caption.await_args.kwargs
    assert kwargs["reply_markup"] is None and "صندوقچه بسته شد" in kwargs["caption"]
    async with db() as session:
        assert (await session.get(Inv2RandomEvent, event_id)).resolved_at is not None
    bot.edit_message_caption.reset_mock()
    assert await presentation.close_expired_events(bot) == 0
    bot.edit_message_caption.assert_not_awaited()


@pytest.mark.asyncio
async def test_loot_callback_handler_claims_and_answers(db, monkeypatch):
    from sib.handlers import inv2_events

    inv_id, event_id = await _chest(db)
    monkeypatch.setattr(inv2_events, "get_session", _session_provider(db))
    answers = AsyncMock()
    monkeypatch.setattr(inv2_events, "_answer", answers)

    def loot_callback(event, *, user_id=1, message_id=777):
        message = Message(
            message_id=message_id, date=NOW,
            chat=Chat(id=-100, type=ChatType.GROUP),
            from_user=TgUser(id=user_id, is_bot=False, first_name="آرش"),
        )
        return CallbackQuery(id=f"loot-{user_id}", from_user=message.from_user,
                            chat_instance="group", message=message,
                            data=f"inv2ev:loot:{event}")

    bot = fake_bot()
    await inv2_events.handle_inv2_loot(loot_callback(event_id), bot)
    answers.assert_awaited_once()
    assert answers.await_args.args[1].startswith("🎁")
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Inv2EventClaim)) == 1
    await inv2_events.handle_inv2_loot(loot_callback(event_id), bot)
    assert answers.await_args.args[1] == "قبلاً از این صندوق برداشته‌اید."
    await inv2_events.handle_inv2_loot(loot_callback(event_id, message_id=778), bot)
    assert answers.await_args.args[1] == "این صندوق به این پیام تعلق ندارد."


def test_event_image_path_rejects_anything_but_catalogue_codes():
    from sib.services.invasion_v2.imaging import templates

    assert templates.event_image_path("../secrets") is None
    assert templates.event_image_path("") is None
    assert templates.event_image_path("not a code") is None


# ---------------------------------------------------------------------------
# 17: migration
# ---------------------------------------------------------------------------
def _migration():
    path = ROOT / "database/migrations/versions/055_inv2_random_events.py"
    spec = importlib.util.spec_from_file_location("migration_055", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_055_creates_and_drops_the_three_tables():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    migration = _migration()
    assert migration.down_revision == "054_garden_corruption"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[
            User.__table__, Group.__table__, Inv2Invasion.__table__, Inv2Region.__table__,
            SystemConfig.__table__,
        ])
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        names = set(inspect(conn).get_table_names())
        assert {"inv2_random_events", "inv2_event_effects", "inv2_event_claims"} <= names
        uniques = {
            constraint["name"]
            for constraint in inspect(conn).get_unique_constraints("inv2_event_claims")
        }
        assert {"uq_inv2_event_claims_event_user", "uq_inv2_event_claims_event_slot"} <= uniques
        assert {
            index["name"] for index in inspect(conn).get_indexes("inv2_random_events")
        } == {
            "ix_inv2_random_events_invasion_status",
            "ix_inv2_random_events_status_scheduled",
            "ix_inv2_random_events_group_id",
        }
        assert conn.execute(text(
            "SELECT value FROM system_config WHERE key = 'inv2_events_enabled'"
        )).scalar() == "false"
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        names = set(inspect(conn).get_table_names())
        assert not {"inv2_random_events", "inv2_event_effects", "inv2_event_claims"} & names
    engine.dispose()


# ---------------------------------------------------------------------------
# 18: catalogue copy
# ---------------------------------------------------------------------------
def test_catalogue_has_twelve_events_with_three_variants_and_persian_digits():
    assert len(catalog.EVENTS) == 12
    assert len({template.code for template in catalog.EVENTS}) == 12
    categories = {template.category for template in catalog.EVENTS}
    assert categories == {catalog.BOSS_BUFF, catalog.PLAYER_BUFF,
                          catalog.INSTANT_DAMAGE, catalog.LOOT}
    for template in catalog.EVENTS:
        assert len(template.narratives) == 3
        assert template.narratives[0] != template.narratives[1]
        line = catalog.render_effect_line(
            template, amount=154, loot_amount=template.loot.default_amount
            if template.loot else None, max_claims=3,
        )
        assert line.strip()
        # No Latin digits anywhere in Persian player-facing copy.
        assert not any(char.isascii() and char.isdigit() for char in line)
        assert line.endswith(".")


def test_loot_status_line_reports_winners_and_closure():
    assert "۲ از ۳" in catalog.render_loot_status(2, 3, ["آرش", "نیلوفر"])
    assert "آرش" in catalog.render_loot_status(1, 3, ["آرش"])
    assert "خالی شد" in catalog.render_loot_status(3, 3)
