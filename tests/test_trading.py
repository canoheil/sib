"""Tests for card trading (Phase 13).

1 trade/user/day, same-level cards, double confirmation, atomic swap.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sib.database.models import PlayerCard, Trade, TradeStatus
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.services.trading import TradeError, TradeService
from sib.services.player_xp import PlayerXPService
from sib.tests.test_parties import create_test_user


async def _card_for(db_session, user, code):
    character = await CharacterService.create(
        db_session, code=code, name_fa=f"قهرمان {code}", power=20, speed=20, intelligence=20
    )
    await db_session.commit()
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()
    return card


@pytest.mark.asyncio
async def test_trade_requires_same_level(db_session):
    alice = await create_test_user(db_session, "مبادله‌کار الف")
    bob = await create_test_user(db_session, "مبادله‌کار ب")
    card_a = await _card_for(db_session, alice, "trade_a")
    card_b = await _card_for(db_session, bob, "trade_b")

    # Alice's card is at level 2 (Bob's stays at level 1).
    card_a.level = 2
    await db_session.commit()

    with pytest.raises(TradeError):
        await TradeService.create(
            db_session,
            proposer_id=alice.id,
            recipient_id=bob.id,
            proposer_card_id=card_a.id,
            recipient_card_id=card_b.id,
        )


@pytest.mark.asyncio
async def test_double_confirmation_swaps_ownership_atomically(db_session):
    alice = await create_test_user(db_session, "مبادله‌کار ج")
    bob = await create_test_user(db_session, "مبادله‌کار د")
    card_a = await _card_for(db_session, alice, "trade_c")
    card_b = await _card_for(db_session, bob, "trade_d")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    await db_session.commit()
    assert trade.status == TradeStatus.PENDING

    # First confirmation is not enough.
    await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.commit()
    assert trade.status == TradeStatus.PENDING
    await db_session.refresh(card_a)
    assert card_a.user_id == alice.id
    assert (await PlayerXPService.get_progress(db_session, alice.id)).total_xp == 0
    assert (await PlayerXPService.get_progress(db_session, bob.id)).total_xp == 0

    # Second confirmation executes.
    await TradeService.confirm(db_session, trade.id, bob.id)
    await db_session.commit()

    assert trade.status == TradeStatus.COMPLETED
    await db_session.refresh(card_a)
    await db_session.refresh(card_b)
    assert card_a.user_id == bob.id
    assert card_b.user_id == alice.id
    # Each participant receives the trade reward and a one-time collection
    # milestone for the character received in the completed swap.
    assert (await PlayerXPService.get_progress(db_session, alice.id)).total_xp == 35
    assert (await PlayerXPService.get_progress(db_session, bob.id)).total_xp == 35


@pytest.mark.asyncio
async def test_only_participants_can_confirm_or_cancel(db_session):
    alice = await create_test_user(db_session, "مبادله‌کار ه")
    bob = await create_test_user(db_session, "مبادله‌کار و")
    mallory = await create_test_user(db_session, "مزاحم")
    card_a = await _card_for(db_session, alice, "trade_e")
    card_b = await _card_for(db_session, bob, "trade_f")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    await db_session.commit()

    with pytest.raises(TradeError):
        await TradeService.confirm(db_session, trade.id, mallory.id)
    with pytest.raises(TradeError):
        await TradeService.cancel(db_session, trade.id, mallory.id)

    # Either participant may cancel.
    await TradeService.cancel(db_session, trade.id, bob.id)
    await db_session.commit()
    assert trade.status == TradeStatus.CANCELLED

    with pytest.raises(TradeError):
        await TradeService.confirm(db_session, trade.id, alice.id)


@pytest.mark.asyncio
async def test_one_trade_per_user_per_day(db_session):
    alice = await create_test_user(db_session, "سهمیه‌دار الف")
    bob = await create_test_user(db_session, "سهمیه‌دار ب")
    card_a1 = await _card_for(db_session, alice, "trade_g")
    card_a2 = await _card_for(db_session, alice, "trade_h")
    card_b1 = await _card_for(db_session, bob, "trade_i")
    card_b2 = await _card_for(db_session, bob, "trade_j")

    assert await TradeService.can_trade_today(db_session, alice.id)

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a1.id,
        recipient_card_id=card_b1.id,
    )
    await db_session.commit()

    # Alice used her daily trade.
    assert not await TradeService.can_trade_today(db_session, alice.id)
    with pytest.raises(TradeError):
        await TradeService.create(
            db_session,
            proposer_id=alice.id,
            recipient_id=bob.id,
            proposer_card_id=card_a2.id,
            recipient_card_id=card_b2.id,
        )

    # Bob also counts (participant in the first trade).
    assert not await TradeService.can_trade_today(db_session, bob.id)


@pytest.mark.asyncio
async def test_cancelled_trade_frees_the_cards_but_still_counts_for_the_day(db_session):
    alice = await create_test_user(db_session, "لغوکننده الف")
    bob = await create_test_user(db_session, "لغوکننده ب")
    card_a = await _card_for(db_session, alice, "trade_k")
    card_b = await _card_for(db_session, bob, "trade_l")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    await TradeService.cancel(db_session, trade.id, alice.id)
    await db_session.commit()

    # Cards are free again (no pending trade on them) — but the daily
    # quota was consumed (cancelled trades still count).
    assert not await TradeService.can_trade_today(db_session, alice.id)
    assert (await PlayerXPService.get_progress(db_session, alice.id)).total_xp == 0
    assert (await PlayerXPService.get_progress(db_session, bob.id)).total_xp == 0


@pytest.mark.asyncio
async def test_expired_trade_cannot_be_confirmed(db_session):
    alice = await create_test_user(db_session, "منقضی الف")
    bob = await create_test_user(db_session, "منقضی ب")
    card_a = await _card_for(db_session, alice, "trade_m")
    card_b = await _card_for(db_session, bob, "trade_n")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    trade.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(TradeError):
        await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.refresh(trade)
    assert trade.status == TradeStatus.EXPIRED
    assert (await PlayerXPService.get_progress(db_session, alice.id)).total_xp == 0
    assert (await PlayerXPService.get_progress(db_session, bob.id)).total_xp == 0


@pytest.mark.asyncio
async def test_cannot_trade_someone_elses_card(db_session):
    alice = await create_test_user(db_session, "دزدکی الف")
    bob = await create_test_user(db_session, "دزدکی ب")
    card_b = await _card_for(db_session, bob, "trade_o")

    with pytest.raises(TradeError):
        await TradeService.create(
            db_session,
            proposer_id=alice.id,
            recipient_id=bob.id,
            proposer_card_id=card_b.id,  # not Alice's card!
            recipient_card_id=card_b.id,
        )


# ---------------------------------------------------------------------------
# Regressions: second-confirmation IntegrityError crash + quota semantics
# (reported flow: create → join → select cards → A confirms → B confirms
# → bot errors → B's daily quota consumed).
# ---------------------------------------------------------------------------


async def _shared_character(db_session, code):
    """One character that MULTIPLE users can own cards of."""
    character = await CharacterService.create(
        db_session, code=code, name_fa=f"قهرمان {code}", power=20, speed=20, intelligence=20
    )
    await db_session.commit()
    return character


@pytest.mark.asyncio
async def test_same_character_swap_completes_atomically(db_session):
    """Regression: the second confirmation crashed with
    ``IntegrityError: UNIQUE constraint failed: player_cards.user_id,
    player_cards.character_id`` when both cards were of the SAME character —
    the two direct ``user_id`` UPDATEs transiently gave one owner two cards
    of one character. The swap must complete atomically, exactly once."""
    alice = await create_test_user(db_session, "هم‌شخصیت الف")
    bob = await create_test_user(db_session, "هم‌شخصیت ب")
    character = await _shared_character(db_session, "same_char")
    card_a = await PlayerCardService.acquire(db_session, alice.id, character.id)
    card_b = await PlayerCardService.acquire(db_session, bob.id, character.id)
    await db_session.commit()

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    await db_session.commit()

    await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.commit()
    assert trade.status == TradeStatus.PENDING

    # Second confirmation must execute the swap WITHOUT raising.
    await TradeService.confirm(db_session, trade.id, bob.id)
    await db_session.commit()

    assert trade.status == TradeStatus.COMPLETED
    await db_session.refresh(card_a)
    await db_session.refresh(card_b)
    assert card_a.user_id == bob.id
    assert card_b.user_id == alice.id
    # 'One card per character' still holds on both sides.
    assert card_a.character_id == card_b.character_id == character.id

    # A COMPLETED trade legitimately consumes the daily quota.
    assert not await TradeService.can_trade_today(db_session, alice.id)
    assert not await TradeService.can_trade_today(db_session, bob.id)


@pytest.mark.asyncio
async def test_create_rejects_impossible_character_end_state(db_session):
    """A trade that would give a player two cards of ONE character is
    refused up front with a friendly error (not a database blow-up), and the
    rejected attempt consumes no quota."""
    alice = await create_test_user(db_session, "پایان ناممکن الف")
    bob = await create_test_user(db_session, "پایان ناممکن ب")
    char_x = await _shared_character(db_session, "end_x")
    char_y = await _shared_character(db_session, "end_y")
    card_ax = await PlayerCardService.acquire(db_session, alice.id, char_x.id)
    await PlayerCardService.acquire(db_session, alice.id, char_y.id)  # Alice also owns a Y
    card_by = await PlayerCardService.acquire(db_session, bob.id, char_y.id)
    await db_session.commit()

    # A gives X, B gives Y -> Alice would end with TWO Y cards.
    with pytest.raises(TradeError):
        await TradeService.create(
            db_session,
            proposer_id=alice.id,
            recipient_id=bob.id,
            proposer_card_id=card_ax.id,
            recipient_card_id=card_by.id,
        )

    assert await TradeService.can_trade_today(db_session, alice.id)
    assert await TradeService.can_trade_today(db_session, bob.id)


@pytest.mark.asyncio
async def test_failed_finalization_fails_safely_and_releases_quota(db_session):
    """Ownership changed between confirmations -> the trade fails SAFELY
    (FAILED status, nothing moved) and burns NEITHER participant's quota."""
    alice = await create_test_user(db_session, "ناتمام الف")
    bob = await create_test_user(db_session, "ناتمام ب")
    carol = await create_test_user(db_session, "ناتمام ج")
    card_a = await _card_for(db_session, alice, "failfin_a")
    card_b = await _card_for(db_session, bob, "failfin_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.commit()

    card_a.user_id = carol.id
    await db_session.commit()

    with pytest.raises(TradeError):
        await TradeService.confirm(db_session, trade.id, bob.id)
    await db_session.commit()

    await db_session.refresh(trade)
    await db_session.refresh(card_a)
    await db_session.refresh(card_b)
    assert trade.status == TradeStatus.FAILED
    assert card_a.user_id == carol.id
    assert card_b.user_id == bob.id

    # Quota rollback: a failed trade must not consume the daily slot.
    assert await TradeService.can_trade_today(db_session, alice.id)
    assert await TradeService.can_trade_today(db_session, bob.id)


@pytest.mark.asyncio
async def test_expired_trade_releases_quota(db_session):
    """Stale offers must stop blocking cards AND quotas (maintenance sweep)."""
    alice = await create_test_user(db_session, "آزادسازی الف")
    bob = await create_test_user(db_session, "آزادسازی ب")
    card_a = await _card_for(db_session, alice, "freed_a")
    card_b = await _card_for(db_session, bob, "freed_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    trade.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(TradeError):
        await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.commit()
    await db_session.refresh(trade)
    assert trade.status == TradeStatus.EXPIRED

    assert await TradeService.can_trade_today(db_session, alice.id)
    assert await TradeService.can_trade_today(db_session, bob.id)


@pytest.mark.asyncio
async def test_quota_consistency_after_failed_trade_then_new_trade(db_session):
    """End-to-end quota consistency: after a FAILED trade the same users can
    create AND complete a fresh trade the very same day."""
    alice = await create_test_user(db_session, "سازگار الف")
    bob = await create_test_user(db_session, "سازگار ب")
    carol = await create_test_user(db_session, "سازگار ج")
    card_a1 = await _card_for(db_session, alice, "consist_a1")
    card_a2 = await _card_for(db_session, alice, "consist_a2")
    card_b1 = await _card_for(db_session, bob, "consist_b1")
    card_b2 = await _card_for(db_session, bob, "consist_b2")

    first = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a1.id,
        recipient_card_id=card_b1.id,
    )
    await db_session.commit()
    await TradeService.confirm(db_session, first.id, alice.id)
    await db_session.commit()
    # Sabotage the first trade (ownership moved elsewhere).
    card_a1.user_id = carol.id
    await db_session.commit()
    with pytest.raises(TradeError):
        await TradeService.confirm(db_session, first.id, bob.id)
    await db_session.commit()

    # Quota is free again for both: a second trade may run to completion.
    second = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a2.id,
        recipient_card_id=card_b2.id,
    )
    await db_session.commit()
    await TradeService.confirm(db_session, second.id, alice.id)
    await TradeService.confirm(db_session, second.id, bob.id)
    await db_session.commit()
    await db_session.refresh(second)
    assert second.status == TradeStatus.COMPLETED

    # ... and once COMPLETED, the day's slot is really used.
    assert not await TradeService.can_trade_today(db_session, alice.id)
    assert not await TradeService.can_trade_today(db_session, bob.id)


@pytest.mark.asyncio
async def test_concurrent_confirmation_swaps_exactly_once(db_session):
    """A and B press confirm at the same instant: the swap happens exactly
    once and the trade ends COMPLETED. The one-shot execution claim
    (conditional UPDATE) keeps this safe even without row locks."""
    import asyncio

    from sib.database.connection import get_async_sessionmaker

    alice = await create_test_user(db_session, "همزمان‌تأیید الف")
    bob = await create_test_user(db_session, "همزمان‌تأیید ب")
    card_a = await _card_for(db_session, alice, "conflict_a")
    card_b = await _card_for(db_session, bob, "conflict_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    await db_session.commit()
    maker = get_async_sessionmaker()

    async def confirm_as(user_id):
        async with maker() as session:
            try:
                await TradeService.confirm(session, trade.id, user_id)
                await session.commit()
                return "ok"
            except TradeError as exc:
                await session.rollback()
                return str(exc)

    await asyncio.gather(confirm_as(alice.id), confirm_as(bob.id))

    db_session.expunge_all()
    done = await TradeService.get_trade(db_session, trade.id)
    assert done.status == TradeStatus.COMPLETED
    assert done.proposer_confirmed is True and done.recipient_confirmed is True

    refreshed_a = await db_session.get(PlayerCard, card_a.id)
    refreshed_b = await db_session.get(PlayerCard, card_b.id)
    await db_session.refresh(refreshed_a)
    await db_session.refresh(refreshed_b)
    # Swapped exactly ONCE (a double swap would return the cards home).
    assert refreshed_a.user_id == bob.id
    assert refreshed_b.user_id == alice.id


@pytest.mark.asyncio
async def test_concurrent_duplicate_second_confirmation_never_double_swaps(db_session):
    """Two duplicate 'second confirm' presses race: never two swaps."""
    import asyncio

    from sib.database.connection import get_async_sessionmaker

    alice = await create_test_user(db_session, "تکراری‌همزمان الف")
    bob = await create_test_user(db_session, "تکراری‌همزمان ب")
    card_a = await _card_for(db_session, alice, "duprace_a")
    card_b = await _card_for(db_session, bob, "duprace_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=card_a.id,
        recipient_card_id=card_b.id,
    )
    await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.commit()
    maker = get_async_sessionmaker()

    async def confirm_bob():
        async with maker() as session:
            try:
                await TradeService.confirm(session, trade.id, bob.id)
                await session.commit()
            except TradeError:
                await session.rollback()

    await asyncio.gather(confirm_bob(), confirm_bob())

    db_session.expunge_all()
    done = await TradeService.get_trade(db_session, trade.id)
    assert done.status == TradeStatus.COMPLETED
    refreshed_a = await db_session.get(PlayerCard, card_a.id)
    refreshed_b = await db_session.get(PlayerCard, card_b.id)
    await db_session.refresh(refreshed_a)
    await db_session.refresh(refreshed_b)
    assert refreshed_a.user_id == bob.id
    assert refreshed_b.user_id == alice.id
