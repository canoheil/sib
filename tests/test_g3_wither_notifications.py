"""G3 — withered garden notifications and garden menu alert."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from types import SimpleNamespace

from sib.core.text import format_html, plain_display_name
from sib.database.models import GardenPlot, User, Group, Inv2GroupMember, Inv2Region, Inv2Invasion
from sib.services.garden import GardenService
from sib.services.invasion_v2.enums import InvasionStatus
from sib.services.invasion_v2.rewards_service import settle_outcome
from sib.handlers.garden import _render_garden
from sib.services.invasion_v2.presentation import (
    WITHER_GROUP_HEADER,
    WITHER_GROUP_FOOTER,
    announce_withered_in_group,
)
from sib.tests.test_parties import create_test_group, create_test_user

REGION_CODE = "g3-moon"
NOW = lambda: datetime.now(timezone.utc)


async def _make_region(session):
    region = Inv2Region(code=REGION_CODE, name_fa="درّه ماه", controlling_faction="فرزندان ماه")
    session.add(region)
    await session.flush()
    return region


async def _make_member(session, user_id: int, group_id: int):
    session.add(Inv2GroupMember(user_id=user_id, group_id=group_id, last_seen_at=NOW()))
    await session.flush()


async def _make_invasion(session, group_id: int, *, status: str, announced: bool = True):
    now = NOW()
    inv = Inv2Invasion(
        group_id=group_id,
        region_code=REGION_CODE,
        enemy_type="soldier",
        enemy_name_fa="شیاسیب پیاده",
        enemy_hp=100,
        enemy_hp_remaining=100,
        difficulty_multiplier=1.0,
        status=status,
        scheduled_at=now,
        started_at=now if announced else None,
        ends_at=now if announced else None,
        finalized_at=now,
        message_id=555 if announced else None,
        message_chat_id=group_id if announced else None,
        occurrence_key=f"g3:{uuid.uuid4().hex}",
    )
    session.add(inv)
    await session.flush()
    return inv


@pytest.mark.asyncio
async def test_withered_users_are_announced_in_group(db_session):
    """Group follow-up must list withered defenders with Persian header/footer and use plain_display_name."""
    await _make_region(db_session)
    group = await create_test_group(db_session, "گروه یورش گ۳")

    # Users with gardens; names include an @handle to verify plain_display_name sanitization.
    user1 = await create_test_user(db_session, "علی@bad")
    user2 = await create_test_user(db_session, "سارا")
    # Create gardens
    await GardenService.get_or_create(db_session, user1.id)
    await GardenService.get_or_create(db_session, user2.id)
    await _make_member(db_session, user1.id, group.id)
    await _make_member(db_session, user2.id, group.id)

    invasion = await _make_invasion(db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True)
    # settle will wither both zero-damage members
    results = await settle_outcome(db_session, invasion.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()
    assert len(results) == 2

    # Mock bot that records group messages
    sent = []

    async def fake_send_message(chat_id, text, parse_mode=None, **kwargs):
        sent.append((chat_id, text))
        # mimic Telegram message
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=123)

    # Use a simple bot namespace
    bot = SimpleNamespace(send_message=fake_send_message)

    await announce_withered_in_group(bot, results)
    # Should have sent exactly one group message (one invasion)
    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == group.id
    # Persian header/footer exact (delivered as HTML: ** is a <b> tag)
    assert WITHER_GROUP_HEADER in text
    assert format_html(WITHER_GROUP_FOOTER) in text
    assert "**" not in text
    assert text.startswith("⚠️ باغ این بازیکن‌ها به‌خاطر شرکت نکردن در یورش پژمرده شد:")
    assert text.endswith(format_html("برای ترمیم زمین‌ها، **کود الهی** بخرید."))
    # Names are sanitized via plain_display_name: @ should be removed, no IDs exposed
    # user1 first_name is "علی@bad" -> plain_display_name should strip @
    assert "@" not in text
    assert "tg://" not in text
    # Both user names should appear (sanitized)
    n1 = plain_display_name(user1.first_name, "مدافع")
    n2 = plain_display_name(user2.first_name, "مدافع")
    assert n1 in text
    assert n2 in text
    # Names use Persian punctuation and one bold list; service result order is not contractual.
    assert "، " in text
    assert format_html(f"**{n1}، {n2}**") in text or format_html(f"**{n2}، {n1}**") in text

    # No duplicate when no withering
    sent.clear()
    await announce_withered_in_group(bot, [])
    assert len(sent) == 0

    # None bot is no-op, no exception
    await announce_withered_in_group(None, results)
    # After with empty list still nothing
    assert len(sent) == 0

    # Also test via settle that produced empty results does not send
    # Create victory (no wither)
    victory = await _make_invasion(db_session, group.id, status=InvasionStatus.VICTORY.value, announced=True)
    empty_results = await settle_outcome(db_session, victory.id, InvasionStatus.VICTORY.value)
    await db_session.commit()
    assert empty_results == []
    sent.clear()
    await announce_withered_in_group(bot, empty_results)
    assert len(sent) == 0


@pytest.mark.asyncio
async def test_garden_menu_shows_wither_alert_if_present(db_session):
    """Garden menu must keep 🥀 on withered plots and add prominent warning line at TOP."""
    await _make_region(db_session)
    group = await create_test_group(db_session, "گروه باغ گ۳ الف")
    user = await create_test_user(db_session, "باغبان پژمرده")
    await GardenService.get_or_create(db_session, user.id)
    await _make_member(db_session, user.id, group.id)
    invasion = await _make_invasion(db_session, group.id, status=InvasionStatus.DEFEAT.value, announced=True)
    await settle_outcome(db_session, invasion.id, InvasionStatus.DEFEAT.value)
    await db_session.commit()

    text, kb = await _render_garden(user.id, db_session)
    # Must keep 🥀 on withered plots
    assert "🥀" in text
    assert "پژمرده" in text
    # Must add a short, prominent warning at the top with a Persian count.
    assert "🥀 **۱ زمین پژمرده شده!**" in text
    assert "برای ترمیم، **کود الهی** لازمه." in text
    # Warning should be near top, before seedlings line and before plot list.
    idx_warning = text.index("🥀 **۱ زمین")
    idx_seedlings = text.index("🌱 نهال:")
    assert idx_warning < idx_seedlings
    # Also ensure rest of garden menu still renders normally (has باغ سیب, buttons)
    assert "باغ سیبِ" in text
    assert "جزئیات هر قطعه" in text
    # Buttons still include withered plot with correct label
    from sib.database.models import PlotState
    # Verify kb contains garden_tree buttons
    all_cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(cb.startswith("garden_tree:") for cb in all_cbs)


@pytest.mark.asyncio
async def test_garden_menu_does_not_show_alert_if_no_wither(db_session):
    """Garden menu must NOT show alert when no withered plots exist."""
    user = await create_test_user(db_session, "باغبان سالم")
    await GardenService.get_or_create(db_session, user.id)
    # No withering applied
    text, kb = await _render_garden(user.id, db_session)
    assert "زمین پژمرده شده!" not in text
    # Still contains normal garden content
    assert "باغ سیبِ" in text
    assert "🌱 نهال:" in text
    # Ensure no 🥀 appears when no wither (or at least not required)
    # The user has no withered plots, so 🥀 should not be in text
    # (but we check that alert line is absent is primary)
    # For completeness, ensure 🥀 not in text for healthy garden
    assert "🥀" not in text
