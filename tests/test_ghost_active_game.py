"""Regression suite for the "ghost active game" bug.

Bug: the bot refused a new game ("you already have an active game") while
the Active Games screen showed nothing.

Root cause: TWO definitions of "active". The guard
(``SessionService.has_active_session``) counted parties in
OPEN/FULL/IN_QUEUE/IN_MATCH plus players on live matches, while the UI
listing (``PartyService.get_active_parties_for_group``) returned only
OPEN/FULL parties of the current group - so IN_QUEUE / IN_MATCH parties
blocked players invisibly. On top of that, no reconciliation released
parties whose match was already gone/terminal.

These tests pin the single-source-of-truth contract:
- every live state blocks a new game;
- completed/cancelled games never block;
- stale rows never produce a false positive;
- the UI lists exactly what the guard considers live;
- recovery/maintenance clean stale state up.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser
from sqlalchemy import select

from sib.core.errors import ActiveSessionExistsException
from sib.database.models import (
    Game,
    Match,
    MatchPlayer,
    MatchState,
    Party,
    PartyStatus,
)
from sib.handlers.party.handlers import build_active_games_menu
from sib.keyboards.party import get_active_parties_keyboard
from sib.services.maintenance.service import MaintenanceService
from sib.services.party import PartyService
from sib.services.recovery import RecoveryService
from sib.services.session import SessionService
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_group, create_test_user


async def _game(db_session) -> Game:
    return await db_session.scalar(select(Game).where(Game.code == "rock_paper_scissors"))


async def _party(db_session, group, user, status: str = PartyStatus.OPEN) -> Party:
    """A party row forced into the requested live status (no real queue)."""
    game = await _game(db_session)
    party = Party(
        group_id=group.id,
        game_id=game.id,
        mode="2v2",
        max_players=2,
        current_players_count=2 if status != PartyStatus.OPEN else 1,
        status=status,
        creator_user_id=user.id,
    )
    db_session.add(party)
    await db_session.flush()
    from sib.database.models import PartyMember

    db_session.add(PartyMember(party_id=party.id, user_id=user.id))
    if status != PartyStatus.OPEN:
        other = await create_test_user(db_session, f"هم‌تیمی {uuid.uuid4().hex[:6]}")
        db_session.add(PartyMember(party_id=party.id, user_id=other.id))
    await db_session.commit()
    return party


async def _match_with_player(db_session, group, user, state: str, **kwargs) -> Match:
    game = await _game(db_session)
    match = Match(
        game_id=game.id,
        mode="solo",
        state=state,
        group1_id=group.id,
        **kwargs,
    )
    db_session.add(match)
    await db_session.flush()
    db_session.add(
        MatchPlayer(
            match_id=match.id,
            user_id=user.id,
            group_id=group.id,
            team=1,
            turn_order=1,
            is_active=True,
            has_accepted=True,
        )
    )
    await db_session.commit()
    return match


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [PartyStatus.OPEN, PartyStatus.FULL, PartyStatus.IN_QUEUE, PartyStatus.IN_MATCH],
)
async def test_every_live_party_state_blocks_a_new_game(db_session, status):
    group = await create_test_group(db_session, "گروه بلوکه")
    user = await create_test_user(db_session, "بازیکن بلوکه")
    await _party(db_session, group, user, status=status)

    assert await SessionService.has_active_session(db_session, user.id) is True
    with pytest.raises(ActiveSessionExistsException):
        await PartyService.create_party(
            db_session, group.id, user.id, "rock_paper_scissors", "solo"
        )


@pytest.mark.asyncio
async def test_completed_match_does_not_block(db_session):
    group = await create_test_group(db_session, "گروه پایان‌یافته")
    user = await create_test_user(db_session, "برنده")
    await _match_with_player(db_session, group, user, MatchState.FINISHED)

    assert await SessionService.has_active_session(db_session, user.id) is False
    party = await PartyService.create_party(
        db_session, group.id, user.id, "rock_paper_scissors", "solo"
    )
    assert party.status == PartyStatus.FULL


@pytest.mark.asyncio
async def test_cancelled_match_and_party_do_not_block(db_session):
    group = await create_test_group(db_session, "گروه لغو")
    user = await create_test_user(db_session, "بازیکن لغو")
    await _match_with_player(db_session, group, user, MatchState.CANCELLED)
    assert await SessionService.has_active_session(db_session, user.id) is False

    party = await PartyService.create_party(
        db_session, group.id, user.id, "rock_paper_scissors", "solo"
    )
    party.status = PartyStatus.CANCELLED
    await db_session.commit()
    assert await SessionService.has_active_session(db_session, user.id) is False


# ---------------------------------------------------------------------------
# UI and validation must agree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [PartyStatus.OPEN, PartyStatus.FULL, PartyStatus.IN_QUEUE, PartyStatus.IN_MATCH],
)
async def test_active_games_ui_lists_exactly_what_blocks(db_session, status):
    group = await create_test_group(db_session, "گروه هم‌تعریف")
    user = await create_test_user(db_session, "بازیکن هم‌تعریف")
    party = await _party(db_session, group, user, status=status)

    # The guard's definition ...
    assert await SessionService.has_active_session(db_session, user.id) is True
    live = await SessionService.get_live_parties_for_group(db_session, group.id)
    assert [p.id for p in live] == [party.id]

    # ... and the UI's definition are the same object list.
    listed = await PartyService.get_active_parties_for_group(db_session, group.id)
    assert [p.id for p in listed] == [party.id]

    text, kb = await build_active_games_menu(db_session, group.id)
    assert f"view_party:{party.id}" in [b.callback_data for b in _buttons(kb)]
    assert "هیچ بازی فعالی" not in text

    # ... and the label tells WHICH live state it is.
    labels = {b.callback_data: b.text for b in _buttons(kb)}
    label = labels[f"view_party:{party.id}"]
    if status == PartyStatus.IN_QUEUE:
        assert "جستجوی حریف" in label
    elif status == PartyStatus.IN_MATCH:
        assert "مسابقه" in label


@pytest.mark.asyncio
async def test_other_groups_parties_are_not_listed(db_session):
    g1 = await create_test_group(db_session, "گروه من")
    g2 = await create_test_group(db_session, "گروه دیگران")
    u1 = await create_test_user(db_session, "بازیکن من")
    u2 = await create_test_user(db_session, "بازیکن دیگران")
    await _party(db_session, g2, u2)

    assert await PartyService.get_active_parties_for_group(db_session, g1.id) == []
    assert await PartyService.get_active_parties_for_group(db_session, g2.id) != []


# ---------------------------------------------------------------------------
# Stale state must never be a false positive (cleanup)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zombie_in_match_party_is_released(db_session):
    """Party left IN_MATCH although its match is gone -> cleaned up."""
    group = await create_test_group(db_session, "گروه زامبی")
    user = await create_test_user(db_session, "بازیکن زامبی")
    party = await _party(db_session, group, user, status=PartyStatus.IN_MATCH)

    # The blocking state is at least VISIBLE (no ghost) ...
    live = await PartyService.get_active_parties_for_group(db_session, group.id)
    assert [p.id for p in live] == [party.id]

    # ... and reconciliation releases it.
    stats = await SessionService.reconcile_stale_state(db_session)
    await db_session.commit()
    assert stats["zombie_parties"] == 1
    await db_session.refresh(party)
    # The party itself is real (its members are still in it) - it is
    # restored to an ACTIONABLE status and stays visible in the UI, so
    # nothing blocks the player invisibly.
    assert party.status == PartyStatus.FULL
    info = await SessionService.get_active_session_info(db_session, user.id)
    assert "پارتی" in info
    listed = await PartyService.get_active_parties_for_group(db_session, group.id)
    assert [p.id for p in listed] == [party.id]

    # Cancelling the party releases the guard completely.
    await PartyService.cancel_party(db_session, party.id, user.id)
    await db_session.commit()
    assert await SessionService.has_active_session(db_session, user.id) is False


@pytest.mark.asyncio
async def test_zombie_open_party_returns_to_open_not_full(db_session):
    group = await create_test_group(db_session, "گروه زامبی نیمه")
    user = await create_test_user(db_session, "بازیکن نیمه")
    party = await _party(db_session, group, user, status=PartyStatus.IN_QUEUE)
    # Not full: restoring must make it joinable again, not "FULL".
    party.current_players_count = 1
    await db_session.commit()

    await SessionService.reconcile_stale_state(db_session)
    await db_session.commit()
    await db_session.refresh(party)
    assert party.status == PartyStatus.OPEN


@pytest.mark.asyncio
async def test_stale_legacy_match_is_cancelled_and_party_released(db_session):
    """A legacy live-state match that can never progress is not a ghost."""
    group = await create_test_group(db_session, "گروه متقاضی")
    user = await create_test_user(db_session, "بازیکن متقاضی")
    party = await _party(db_session, group, user, status=PartyStatus.IN_MATCH)
    match = await _match_with_player(db_session, group, user, MatchState.WAITING_FOR_OPPONENT)
    match.party1_id = party.id
    match.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.commit()

    assert await SessionService.has_active_session(db_session, user.id) is True
    stats = await SessionService.reconcile_stale_state(db_session)
    await db_session.commit()

    assert stats["stale_matches"] == 1
    await db_session.refresh(match)
    await db_session.refresh(party)
    assert match.state == MatchState.CANCELLED
    assert party.status == PartyStatus.FULL  # actionable again, still visible
    assert [
        p.id
        for p in await SessionService.get_live_parties_for_group(db_session, group.id)
    ] == [party.id]
    assert await SessionService.live_match_for_user(db_session, user.id) is None
    assert await SessionService.get_active_session_info(db_session, user.id) == (
        f"پارتی بازی (شناسه: {party.id})"
    )


@pytest.mark.asyncio
async def test_active_match_without_deadline_is_reconciled_only_when_old(db_session):
    group = await create_test_group(db_session, "گروه بی‌مهلت")
    user = await create_test_user(db_session, "بازیکن بی‌مهلت")
    match = await _match_with_player(db_session, group, user, MatchState.ACTIVE)
    await db_session.commit()

    # Fresh (just created) -> must NOT be touched.
    await SessionService.reconcile_stale_state(db_session)
    await db_session.commit()
    await db_session.refresh(match)
    assert match.state == MatchState.ACTIVE

    # Old, still without any deadline -> cancelled, player released.
    match.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    await db_session.commit()
    stats = await SessionService.reconcile_stale_state(db_session)
    await db_session.commit()
    await db_session.refresh(match)
    assert match.state == MatchState.CANCELLED
    assert stats["stale_matches"] == 1
    assert await SessionService.has_active_session(db_session, user.id) is False


@pytest.mark.asyncio
async def test_terminal_matches_release_player_flags(db_session):
    group = await create_test_group(db_session, "گروه آزادسازی")
    user = await create_test_user(db_session, "بازیکن آزادسازی")
    match = await _match_with_player(db_session, group, user, MatchState.FINISHED)
    # Simulate an old row that kept is_active=True after finishing.
    match.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()

    stats = await SessionService.reconcile_stale_state(db_session)
    await db_session.commit()
    assert stats["released_players"] >= 1
    players = list(
        (await db_session.scalars(select(MatchPlayer).where(MatchPlayer.match_id == match.id))).all()
    )
    assert all(p.is_active is False for p in players)


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(db_session):
    group = await create_test_group(db_session, "گروه تکراری")
    user = await create_test_user(db_session, "بازیکن تکراری")
    await _party(db_session, group, user, status=PartyStatus.IN_MATCH)

    first = await SessionService.reconcile_stale_state(db_session)
    await db_session.commit()
    assert first["zombie_parties"] == 1

    second = await SessionService.reconcile_stale_state(db_session)
    await db_session.commit()
    assert second == {
        "zombie_parties": 0,
        "orphan_queue_parties": 0,
        "stale_matches": 0,
        "released_players": 0,
    }


# ---------------------------------------------------------------------------
# Recovery / maintenance wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_on_startup_releases_zombie_party(db_session):
    group = await create_test_group(db_session, "گروه ریکاوری زامبی")
    user = await create_test_user(db_session, "بازیکن ریکاوری زامبی")
    party = await _party(db_session, group, user, status=PartyStatus.IN_MATCH)

    stats = await RecoveryService.recover_on_startup(db_session)
    assert stats["zombie_parties"] == 1

    await db_session.refresh(party)
    assert party.status == PartyStatus.FULL
    assert await SessionService.live_match_for_user(db_session, user.id) is None


@pytest.mark.asyncio
async def test_maintenance_cycle_releases_zombie_party(db_session):
    """The periodic worker (not only startup) keeps the state consistent."""
    group = await create_test_group(db_session, "گروه نگهداری")
    user = await create_test_user(db_session, "بازیکن نگهداری")
    party = await _party(db_session, group, user, status=PartyStatus.IN_MATCH)

    await MaintenanceService.run_once()

    db_session.expunge_all()
    refreshed = await db_session.get(Party, party.id)
    assert refreshed.status == PartyStatus.FULL
    assert await SessionService.live_match_for_user(db_session, user.id) is None


# ---------------------------------------------------------------------------
# Race: one user, two concurrent "create party"
# ---------------------------------------------------------------------------


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_party_creation_for_same_user_is_serialized(db_session):
    """Two simultaneous creations by one user: exactly one may succeed."""
    from sib.database.connection import get_async_sessionmaker

    group = await create_test_group(db_session, "گروه همروندی")
    user = await create_test_user(db_session, "بازیکن همروندی")
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def attempt():
        async with maker() as session:
            try:
                await PartyService.create_party(
                    session, group.id, user.id, "rock_paper_scissors", "solo"
                )
                await session.commit()
                return True
            except ActiveSessionExistsException:
                await session.rollback()
                return False

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]

    parties = list(
        (
            await db_session.scalars(
                select(Party).where(Party.creator_user_id == user.id)
            )
        ).all()
    )
    assert len(parties) == 1
