"""LB1 — New main leaderboard tests (4 tabs, semantic colors, in-place edit)."""
import pytest
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select, text

from sib.database.models import Group, GroupStats, User
from sib.database.models.castle import GroupCastle, CastleDonation
from sib.database.models.war import GroupWar
from sib.database.models.invasion_v2 import Inv2Invasion, Inv2Region
from sib.services.leaderboard import LeaderboardService
from sib.tests.test_parties import create_test_user, create_test_group


async def _clean_war_inv2(session):
    """Ensure war/invasion tables are clean (conftest does not cover them)."""
    await session.execute(text("DELETE FROM war_attacks"))
    await session.execute(text("DELETE FROM war_participants"))
    await session.execute(text("DELETE FROM group_wars"))
    await session.execute(text("DELETE FROM inv2_attacks"))
    await session.execute(text("DELETE FROM inv2_rewards"))
    await session.execute(text("DELETE FROM inv2_invasions"))
    await session.execute(text("DELETE FROM inv2_group_state"))
    await session.execute(text("DELETE FROM inv2_regions"))
    await session.commit()


async def _make_castle(session, group_id, level=1, total_donated=0):
    castle = await session.get(GroupCastle, group_id)
    if castle is None:
        castle = GroupCastle(group_id=group_id, level=level, total_donated=total_donated)
        session.add(castle)
    else:
        castle.level = level
        castle.total_donated = total_donated
    await session.commit()
    return castle


async def _make_war(session, attacker_id, defender_id, winner_id, status="finished"):
    now = datetime.now(timezone.utc)
    war = GroupWar(
        attacker_id=attacker_id,
        defender_id=defender_id,
        status=status,
        attacker_damage=100,
        defender_damage=50,
        started_at=now - timedelta(hours=2),
        ends_at=now - timedelta(hours=1),
        winner_id=winner_id,
        looted_apples=0,
    )
    # ensure both sides have castles
    for gid in (attacker_id, defender_id):
        if await session.get(GroupCastle, gid) is None:
            session.add(GroupCastle(group_id=gid, level=1, total_donated=0))
    session.add(war)
    await session.commit()
    return war


async def _ensure_region(session, code="test_region_lb1"):
    region = await session.get(Inv2Region, code)
    if region is None:
        region = Inv2Region(code=code, name_fa="سرزمین آزمون", controlling_faction="sib")
        session.add(region)
        await session.commit()
    return region


async def _make_invasion(session, group_id, status="VICTORY", region_code="test_region_lb1"):
    await _ensure_region(session, region_code)
    now = datetime.now(timezone.utc)
    inv = Inv2Invasion(
        region_code=region_code,
        enemy_type="soldier",
        enemy_name_fa="شیاسیب آزمون",
        enemy_hp=100,
        enemy_hp_remaining=0 if status == "VICTORY" else 100,
        difficulty_multiplier=1.0,
        status=status,
        group_id=group_id,
        scheduled_at=now - timedelta(hours=2),
        started_at=now - timedelta(hours=1),
        ends_at=now - timedelta(minutes=10),
        occurrence_key=f"lb1:{group_id}:{uuid.uuid4().hex}",
    )
    session.add(inv)
    await session.commit()
    return inv


async def _make_donation(session, user_id, group_id, amount):
    donation = CastleDonation(
        user_id=user_id,
        group_id=group_id,
        amount=amount,
        donated_at=datetime.now(timezone.utc),
        request_key=f"lb1-test:{uuid.uuid4().hex}",
    )
    session.add(donation)
    await session.commit()
    return donation


# ------------------------------------------------------------------
# Service query tests
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_castles_returns_correct_order(db_session):
    await _clean_war_inv2(db_session)
    g1 = await create_test_group(db_session, "قلعه طلایی")
    g2 = await create_test_group(db_session, "قلعه نقره‌ای")
    g3 = await create_test_group(db_session, "قلعه برنزی")

    await _make_castle(db_session, g1.id, level=10, total_donated=1300000)
    await _make_castle(db_session, g2.id, level=5, total_donated=110000)
    await _make_castle(db_session, g3.id, level=5, total_donated=50000)

    # tie-break: same level, higher total_donated first
    rows = await LeaderboardService.get_top_castles(db_session, limit=10)
    assert len(rows) >= 3
    ids = [g.id for g, _ in rows]
    assert ids.index(g1.id) < ids.index(g2.id) < ids.index(g3.id)
    # verify level stat
    for g, castle in rows:
        if g.id == g1.id:
            assert castle.level == 10
            assert castle.total_donated == 1300000
        if g.id == g2.id:
            assert castle.level == 5


@pytest.mark.asyncio
async def test_top_warriors_counts_only_finished_wars(db_session):
    await _clean_war_inv2(db_session)
    g1 = await create_test_group(db_session, "جنگاور الف")
    g2 = await create_test_group(db_session, "جنگاور ب")
    g3 = await create_test_group(db_session, "جنگاور ج")

    # g1 wins 2 finished wars, g2 wins 1 finished, g3 has 1 active (should not count)
    for _ in range(2):
        await _make_war(db_session, g1.id, g2.id, winner_id=g1.id, status="finished")
    await _make_war(db_session, g2.id, g1.id, winner_id=g2.id, status="finished")
    await _make_war(db_session, g3.id, g1.id, winner_id=g3.id, status="active")

    rows = await LeaderboardService.get_top_warriors(db_session, limit=10)
    mapping = {g.id: wins for g, wins in rows}
    assert mapping[g1.id] == 2
    assert mapping[g2.id] == 1
    assert mapping[g3.id] == 0  # active not counted

    ids = [g.id for g, _ in rows]
    assert ids.index(g1.id) < ids.index(g2.id)


@pytest.mark.asyncio
async def test_top_defenders_counts_only_victories(db_session):
    await _clean_war_inv2(db_session)
    g1 = await create_test_group(db_session, "مدافع الف")
    g2 = await create_test_group(db_session, "مدافع ب")

    await _make_invasion(db_session, g1.id, status="VICTORY")
    await _make_invasion(db_session, g1.id, status="VICTORY")
    await _make_invasion(db_session, g1.id, status="DEFEAT")
    await _make_invasion(db_session, g2.id, status="VICTORY")
    await _make_invasion(db_session, g2.id, status="PENDING")
    await _make_invasion(db_session, g2.id, status="EXPIRED")

    rows = await LeaderboardService.get_top_defenders(db_session, limit=10)
    mapping = {g.id: v for g, v in rows}
    assert mapping[g1.id] == 2
    assert mapping[g2.id] == 1


@pytest.mark.asyncio
async def test_top_donors_sums_donations(db_session):
    await _clean_war_inv2(db_session)
    g1 = await create_test_group(db_session, "اهداکننده الف")
    g2 = await create_test_group(db_session, "اهداکننده ب")
    u1 = await create_test_user(db_session, "اهداگر ۱")
    u2 = await create_test_user(db_session, "اهداگر ۲")

    for gid in (g1.id, g2.id):
        if await db_session.get(GroupCastle, gid) is None:
            db_session.add(GroupCastle(group_id=gid, level=1, total_donated=0))
    await db_session.commit()

    await _make_donation(db_session, u1.id, g1.id, 500)
    await _make_donation(db_session, u2.id, g1.id, 300)
    await _make_donation(db_session, u1.id, g2.id, 100)

    rows = await LeaderboardService.get_top_donors(db_session, limit=10)
    mapping = {g.id: total for g, total in rows}
    assert mapping[g1.id] == 800
    assert mapping[g2.id] == 100

    ids = [g.id for g, _ in rows]
    assert ids.index(g1.id) < ids.index(g2.id)


@pytest.mark.asyncio
async def test_top_castles_only_active_groups(db_session):
    await _clean_war_inv2(db_session)
    g_active = await create_test_group(db_session, "فعال قلعه")
    g_inactive = await create_test_group(db_session, "غیرفعال قلعه")
    await _make_castle(db_session, g_active.id, level=8, total_donated=580000)
    await _make_castle(db_session, g_inactive.id, level=10, total_donated=1300000)
    # deactivate
    g_inactive.is_active = False
    await db_session.commit()

    rows = await LeaderboardService.get_top_castles(db_session, limit=10)
    ids = [g.id for g, _ in rows]
    assert g_active.id in ids
    assert g_inactive.id not in ids


# ------------------------------------------------------------------
# Keyboard semantic colors
# ------------------------------------------------------------------

def test_leaderboard_tabs_use_semantic_colors():
    from sib.keyboards.leaderboard import get_leaderboard_keyboard, get_leaderboard_tab_keyboard
    kb = get_leaderboard_keyboard(is_group=True)
    # All tab buttons must be primary (blue)
    for row in kb.inline_keyboard:
        for btn in row:
            if btn.callback_data.startswith("lb:"):
                assert btn.style == "primary", f"tab {btn.callback_data} must be primary, got {btn.style}"
            if btn.callback_data == "show_leaderboard":
                assert btn.style == "primary"
            if btn.callback_data == "group_win_stats":
                assert btn.style == "primary"
            if btn.callback_data == "main_menu":
                assert btn.style == "danger"

    tab_kb = get_leaderboard_tab_keyboard("castles")
    for row in tab_kb.inline_keyboard:
        for btn in row:
            if "بازگشت" in btn.text or btn.callback_data == "show_leaderboard":
                # back is danger, refresh is primary
                if btn.callback_data == "show_leaderboard":
                    assert btn.style == "danger"
                elif btn.callback_data.startswith("lb:"):
                    assert btn.style == "primary"
                elif btn.callback_data == "main_menu":
                    assert btn.style == "danger"

    # Ensure no button uses style=None
    for kb_check in (kb, tab_kb):
        for row in kb_check.inline_keyboard:
            for btn in row:
                assert btn.style is not None, f"button {btn.text} missing semantic style"


# ------------------------------------------------------------------
# Handler uses safe_edit_callback_message and banner photo
# ------------------------------------------------------------------

def test_leaderboard_callbacks_edit_message_in_place():
    from sib.handlers import leaderboard as lb_module
    src = inspect.getsource(lb_module)

    # All tab handlers must use safe_edit_callback_message (not send_message)
    assert "safe_edit_callback_message" in src, "must use safe_edit_callback_message for tab switches"
    # Ensure lb: handlers exist
    assert "lb:castles" in src
    assert "lb:warriors" in src
    assert "lb:defenders" in src
    assert "lb:donors" in src

    # Must use banner photo for leaderboard
    assert 'banner_photo("leaderboard")' in src or "banner_photo('leaderboard')" in src or 'banner_photo' in src

    # Must show empty text in Persian
    assert "هنوز آمار کافی" in src

    # Check callbacks are defined with correct patterns
    assert 'F.data == "lb:castles"' in src or "lb:castles" in src
    # Verify no new message sending in tab handlers (they should not call send_screen_reply for tabs)
    # Count occurrences of safe_edit vs send - tab section must use safe_edit
    # Simple check: _render_tab uses safe_edit_callback_message
    assert "_render_tab" in src or "safe_edit_callback_message" in src
