"""G2 — Invasion garden corruption (پژمردگی) and Divine Fertilizer.

Covers the mandatory product rules:
- withering fires only on DEFEAT or *announced* EXPIRED invasions;
- never-announced (PENDING->EXPIRED) and startup-recovery expirations are exempt;
- players with any inv2_attacks row (participants, even at zero rolled damage)
  are exempt;
- at most one wither per player per UTC day and one per invasion (idempotent);
- Divine Fertilizer restores exactly one withered plot and is consumed once;
- the 36-hour growth bug is fixed via the 12-hour growth config.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from sib.database.models import (
    Character,
    GardenPlot,
    GardenWitherEvent,
    Group,
    Inv2Attack,
    Inv2GroupMember,
    Inv2GroupState,
    Inv2Invasion,
    Inv2Region,
    PlayerCard,
    PlotState,
)
from sib.handlers.garden import _render_garden, _tree_view
from sib.services.config_store import CONFIG_DEFAULTS, ConfigService
from sib.services.garden import GardenService
from sib.services.invasion_v2.enums import InvasionStatus
from sib.services.invasion_v2.rewards_service import settle_outcome
from sib.services.invasion_v2.scheduler import InvasionScheduler
from sib.services.items import ItemService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_group, create_test_user

REGION_CODE = "g2-moon"
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 - tz-aware test clock


async def _make_region(session):
    region = Inv2Region(
        code=REGION_CODE, name_fa="درّه ماه", controlling_faction="فرزندان ماه"
    )
    session.add(region)
    await session.flush()
    return region


async def _make_group(session):
    return await create_test_group(session, f"گروه گ۲ {uuid.uuid4().hex[:8]}")


async def _make_member(session, user_id: int, group_id: int, *, active: bool = True):
    last_seen = NOW() if active else NOW() - timedelta(days=120)
    session.add(
        Inv2GroupMember(user_id=user_id, group_id=group_id, last_seen_at=last_seen)
    )
    await session.flush()


async def _make_invasion(
    session,
    group_id: int,
    *,
    status: str,
    announced: bool,
    hp_remaining: int = 100,
):
    now = NOW()
    invasion = Inv2Invasion(
        group_id=group_id,
        region_code=REGION_CODE,
        enemy_type="soldier",
        enemy_name_fa="شیاسیب پیاده",
        enemy_hp=100,
        enemy_hp_remaining=hp_remaining,
        difficulty_multiplier=1.0,
        status=status,
        scheduled_at=now,
        started_at=now if announced else None,
        ends_at=now if announced else None,
        finalized_at=now,
        message_id=555 if announced else None,
        message_chat_id=group_id if announced else None,
        occurrence_key=f"g2:{uuid.uuid4().hex}",
    )
    session.add(invasion)
    await session.flush()
    return invasion


async def _add_attack(session, invasion_id: int, user_id: int, *, damage: int = 0):
    """Persist a participation row (damage may be zero — it still exempts)."""
    character = Character(
        code=f"g2-char-{uuid.uuid4().hex[:8]}",
        name_fa="کاراکتر تست",
        power=10,
        speed=10,
        intelligence=10,
    )
    session.add(character)
    await session.flush()
    card = PlayerCard(
        user_id=user_id, character_id=character.id, level=1, is_favorite=True
    )
    session.add(card)
    await session.flush()
    session.add(
        Inv2Attack(
            invasion_id=invasion_id,
            user_id=user_id,
            card_id=card.id,
            card_power=10,
            damage=damage,
            is_crit=False,
            narrative="ضربه تست",
            joined_at=NOW(),
        )
    )
    await session.flush()


async def _withered_count(session, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(GardenPlot)
            .where(GardenPlot.garden_id == user_id, GardenPlot.is_withered.is_(True))
        )
    )


async def _event_count(session, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(GardenWitherEvent)
            .where(GardenWitherEvent.user_id == user_id)
        )
    )


# ---------------------------------------------------------------------------
# TASK 6 — mandatory tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wither_only_on_defeat_or_announced_expired(db_session):
    await _make_region(db_session)
    group = await _make_group(db_session)

    # --- DEFEAT: a zero-damage member is withered, a participant is exempt ---
    idle_user = await create_test_user(db_session, "بی‌تفاوت")
    fighter_user = await create_test_user(db_session, "مدافع")
    await GardenService.get_or_create(db_session, idle_user.id)
    await GardenService.get_or_create(db_session, fighter_user.id)
    await _make_member(db_session, idle_user.id, group.id)
    await _make_member(db_session, fighter_user.id, group.id)
    defeat = await _make_invasion(
        db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True
    )
    # The fighter dealt even a zero-damage rolled hit: the row exempts them.
    await _add_attack(db_session, defeat.id, fighter_user.id, damage=0)

    results = await settle_outcome(db_session, defeat.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()

    assert [r.user_id for r in results] == [idle_user.id]
    assert await _withered_count(db_session, idle_user.id) == 1
    assert await _withered_count(db_session, fighter_user.id) == 0
    assert await _event_count(db_session, idle_user.id) == 1
    # DEFEAT still moves territory and updates the group counters.
    state = await db_session.scalar(
        select(Inv2GroupState).where(Inv2GroupState.group_id == group.id)
    )
    assert state is not None and state.total_defeats == 1

    # --- Announced EXPIRED: a fresh zero-damage member is withered ---
    expired_user = await create_test_user(db_session, "خواب‌آلود")
    await GardenService.get_or_create(db_session, expired_user.id)
    await _make_member(db_session, expired_user.id, group.id)
    announced_expired = await _make_invasion(
        db_session, group.id, status=InvasionStatus.EXPIRED.value, announced=True
    )
    # The fighter participates again; idle_user is capped by today's wither.
    await _add_attack(db_session, announced_expired.id, fighter_user.id, damage=10)
    results = await settle_outcome(
        db_session, announced_expired.id, InvasionStatus.EXPIRED.value
    )
    await db_session.commit()

    assert [r.user_id for r in results] == [expired_user.id]
    assert await _withered_count(db_session, expired_user.id) == 1
    # The daily cap holds: idle_user still lost exactly one plot, one event.
    assert await _withered_count(db_session, idle_user.id) == 1
    assert await _event_count(db_session, idle_user.id) == 1
    assert await _withered_count(db_session, fighter_user.id) == 0
    # EXPIRED must not touch win/loss counters or territory.
    assert state.total_defeats == 1
    assert state.total_victories == 0


@pytest.mark.asyncio
async def test_wither_only_once_per_day(db_session):
    await _make_region(db_session)
    group = await _make_group(db_session)
    user = await create_test_user(db_session, "قربانی روزانه")
    await GardenService.get_or_create(db_session, user.id)
    await _make_member(db_session, user.id, group.id)

    first = await _make_invasion(
        db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True
    )
    second = await _make_invasion(
        db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True
    )

    first_results = await settle_outcome(
        db_session, first.id, InvasionStatus.DEFEAT.value
    )
    second_results = await settle_outcome(
        db_session, second.id, InvasionStatus.DEFEAT.value
    )
    # Replaying the first settlement must stay idempotent.
    replay_results = await settle_outcome(
        db_session, first.id, InvasionStatus.DEFEAT.value
    )
    await db_session.commit()

    assert len(first_results) == 1
    assert second_results == []
    assert replay_results == []
    assert await _event_count(db_session, user.id) == 1
    assert await _withered_count(db_session, user.id) == 1

    # The ledger row records the UTC day and the causing invasion.
    event = await db_session.scalar(
        select(GardenWitherEvent).where(GardenWitherEvent.user_id == user.id)
    )
    assert event is not None
    assert event.wither_day == NOW().date()
    assert event.invasion_id == first.id


@pytest.mark.asyncio
async def test_unannounced_expired_does_not_wither(db_session):
    await _make_region(db_session)
    group = await _make_group(db_session)
    user = await create_test_user(db_session, "نادیده")
    await GardenService.get_or_create(db_session, user.id)
    await _make_member(db_session, user.id, group.id)

    # An EXPIRED invasion that was never announced (no started_at/message_id).
    unannounced = await _make_invasion(
        db_session, group.id, status=InvasionStatus.EXPIRED.value, announced=False
    )
    results = await settle_outcome(
        db_session, unannounced.id, InvasionStatus.EXPIRED.value
    )
    await db_session.commit()

    assert results == []
    assert await _event_count(db_session, user.id) == 0
    assert await _withered_count(db_session, user.id) == 0

    # Scheduler level: an unannounced PENDING row expires but never enters the
    # settlement transitions; an announced one does (and then settles safely).
    pending_unannounced = await _make_invasion(
        db_session, group.id, status=InvasionStatus.PENDING.value, announced=False
    )
    pending_unannounced.ends_at = NOW() - timedelta(minutes=1)
    pending_announced = await _make_invasion(
        db_session, group.id, status=InvasionStatus.PENDING.value, announced=True
    )
    pending_announced.ends_at = NOW() - timedelta(minutes=1)
    await db_session.flush()

    transitions = await InvasionScheduler._finalize_due(db_session, NOW())
    await db_session.commit()
    transition_ids = {inv_id for inv_id, _status in transitions}

    assert pending_unannounced.id not in transition_ids
    assert pending_announced.id in transition_ids
    # Both rows are marked EXPIRED; only the announced one can corrupt later.
    assert pending_unannounced.status == InvasionStatus.EXPIRED.value
    assert pending_announced.status == InvasionStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_fertilizer_restores_withered_plot(db_session):
    await _make_region(db_session)
    group = await _make_group(db_session)
    user = await create_test_user(db_session, "باغبان احیاگر")
    await GardenService.get_or_create(db_session, user.id)
    await _make_member(db_session, user.id, group.id)
    invasion = await _make_invasion(
        db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True
    )
    await settle_outcome(db_session, invasion.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()

    withered_plot = await db_session.scalar(
        select(GardenPlot).where(
            GardenPlot.garden_id == user.id, GardenPlot.is_withered.is_(True)
        )
    )
    assert withered_plot is not None
    plot_index = withered_plot.plot_index

    # No fertilizer yet: restore refuses and consumes nothing.
    with pytest.raises(ValueError):
        await GardenService.restore_plot(db_session, user.id, plot_index)

    await ItemService.grant(db_session, user.id, "divine_fertilizer", 2)
    await db_session.commit()

    restored = await GardenService.restore_plot(db_session, user.id, plot_index)
    await db_session.commit()

    assert restored.is_withered is False
    assert await ItemService.get_total(db_session, user.id, "divine_fertilizer") == 1
    assert await _withered_count(db_session, user.id) == 0
    views = GardenService.view(await GardenService.get_or_create(db_session, user.id))
    assert views[plot_index].state == PlotState.EMPTY

    # Using it on a healthy plot is refused and consumes nothing (the refusal
    # raises before any write, so no rollback is needed — codebase convention).
    with pytest.raises(ValueError):
        await GardenService.restore_plot(db_session, user.id, plot_index)
    assert await ItemService.get_total(db_session, user.id, "divine_fertilizer") == 1


@pytest.mark.asyncio
async def test_growth_config_is_12_hours(db_session):
    # The 36-hour bug: growth 12h + fruit 12h => first fruit at exactly 24h.
    assert CONFIG_DEFAULTS["garden_growth_seconds"] == 12 * 60 * 60
    assert CONFIG_DEFAULTS["garden_fruit_seconds"] == 12 * 60 * 60
    # A missing system_config row falls back to the code default (no seeding).
    assert await ConfigService.get(db_session, "garden_growth_seconds") == 43200
    assert (
        CONFIG_DEFAULTS["garden_growth_seconds"]
        + CONFIG_DEFAULTS["garden_fruit_seconds"]
        == 24 * 60 * 60
    )

    # End-to-end: a freshly planted tree schedules its first fruit at +24h.
    user = await create_test_user(db_session, "باغبان زمان")
    await SeedlingService.admin_adjust(db_session, user.id, 1, reason="g2-test")
    await db_session.commit()
    before = NOW()
    plot = await GardenService.plant(db_session, user.id, 0)
    await db_session.commit()

    growth_delta = plot.mature_at - plot.planted_at
    fruit_delta = plot.fruit_ready_at - plot.planted_at
    assert growth_delta == timedelta(hours=12)
    assert fruit_delta == timedelta(hours=24)
    assert plot.mature_at > before


# ---------------------------------------------------------------------------
# Extra coverage: UI rendering and semantic button colors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_withered_garden_ui_uses_distinct_emoji_and_semantic_buttons(db_session):
    await _make_region(db_session)
    group = await _make_group(db_session)
    user = await create_test_user(db_session, "باغبان رابط")
    await GardenService.get_or_create(db_session, user.id)
    await _make_member(db_session, user.id, group.id)
    invasion = await _make_invasion(
        db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True
    )
    await settle_outcome(db_session, invasion.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()

    withered_plot = await db_session.scalar(
        select(GardenPlot).where(
            GardenPlot.garden_id == user.id, GardenPlot.is_withered.is_(True)
        )
    )
    idx = withered_plot.plot_index

    text, kb = await _render_garden(user.id, db_session)
    assert "🥀" in text
    assert "پژمرده" in text
    plot_buttons = [
        b
        for row in kb.inline_keyboard
        for b in row
        if b.callback_data == f"garden_tree:{idx}"
    ]
    assert len(plot_buttons) == 1
    assert plot_buttons[0].style == "primary"  # blue navigation cell

    tree_text, tree_kb = await _tree_view(user.id, idx)
    assert "کود الهی" in tree_text
    all_buttons = [b for row in tree_kb.inline_keyboard for b in row]
    by_callback = {b.callback_data: b for b in all_buttons}
    fertilizer_btn = by_callback[f"garden_fertilize:{idx}"]
    assert fertilizer_btn.style == "success"  # green restore action
    assert "کود الهی" in fertilizer_btn.text
    assert by_callback["garden_refresh"].style == "danger"  # red back row
    # No transparent (unstyled) buttons are allowed on this screen.
    assert all(b.style in {"primary", "success", "danger"} for b in all_buttons)


@pytest.mark.asyncio
async def test_withered_plot_blocks_plant_harvest_and_magic(db_session):
    await _make_region(db_session)
    group = await _make_group(db_session)
    user = await create_test_user(db_session, "باغبان قفل‌شده")
    await GardenService.get_or_create(db_session, user.id)
    await _make_member(db_session, user.id, group.id)
    invasion = await _make_invasion(
        db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True
    )
    await settle_outcome(db_session, invasion.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()

    withered = await db_session.scalar(
        select(GardenPlot).where(
            GardenPlot.garden_id == user.id, GardenPlot.is_withered.is_(True)
        )
    )
    idx = withered.plot_index
    # Every guard raises BEFORE any write, so the session stays usable and no
    # explicit rollback is needed (codebase convention, see tests/test_garden.py).
    with pytest.raises(ValueError):
        await GardenService.plant(db_session, user.id, idx)
    with pytest.raises(ValueError):
        await GardenService.harvest(db_session, user.id, idx)
    with pytest.raises(ValueError):
        await GardenService.apply_magic(db_session, user.id, idx)

    # harvest_all skips withered plots entirely (no FRUIT_READY derivation).
    with pytest.raises(ValueError):
        await GardenService.harvest_all(db_session, user.id)


@pytest.mark.asyncio
async def test_wither_dm_uses_exact_product_text_and_tolerates_blocks(db_session):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sib.core.text import format_html
    from sib.services.garden import WitherResult
    from sib.services.invasion_v2.presentation import (
        WITHER_NOTIFICATION_TEXT,
        deliver_wither_notifications,
    )
    from aiogram.exceptions import TelegramForbiddenError

    sent = []

    async def send_message(chat_id, text, parse_mode=None):
        if chat_id == 42:
            raise TelegramForbiddenError(method=SimpleNamespace(), message="blocked")
        sent.append((chat_id, text))
        return True

    bot = SimpleNamespace(send_message=send_message)
    results = [WitherResult(user_id=7, plot_index=2, invasion_id=11),
               WitherResult(user_id=42, plot_index=0, invasion_id=11)]
    # Must never raise, even when the player blocked the bot.
    await deliver_wither_notifications(bot, results)
    # The DM is delivered as HTML, so the ** marker reaches the player as <b>.
    assert sent == [(7, format_html(WITHER_NOTIFICATION_TEXT))]
    assert "**" not in sent[0][1]
    assert "پژمرده" in WITHER_NOTIFICATION_TEXT and "کود الهی" in WITHER_NOTIFICATION_TEXT
    # No bot / no results are no-ops.
    await deliver_wither_notifications(None, results)
    await deliver_wither_notifications(bot, [])
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_divine_fertilizer_shop_registration(db_session):
    from sib.services.items import ITEM_CATALOG, ItemService

    catalog = ITEM_CATALOG["divine_fertilizer"]
    assert catalog["label"] == "کود الهی"
    assert catalog["description"] == "با استفاده از این کود، یک قطعه زمین پژمرده دوباره سالم می‌شود."
    price = await ItemService.buy_price(db_session, "divine_fertilizer")
    assert price == 80
    # Consumable remedy: intentionally not sellable, like magic_powder.
    assert await ItemService.sell_price(db_session, "divine_fertilizer") is None
