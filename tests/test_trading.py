"""Tests for card trading (Phase 13).

1 trade/user/day, same-level cards, double confirmation, atomic swap.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sib.database.models import Trade, TradeStatus
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.services.trading import TradeError, TradeService
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

    # Upgrade Alice's card to level 2.
    from sib.services.apple_wallet import AppleWalletService

    await AppleWalletService.earn(db_session, alice.id, 200, reason="test_grant")
    await db_session.commit()
    await PlayerCardService.upgrade(
        db_session, alice.id, card_a.id,
        boost_power=20, boost_speed=20, boost_intelligence=20,
    )
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

    # Second confirmation executes.
    await TradeService.confirm(db_session, trade.id, bob.id)
    await db_session.commit()

    assert trade.status == TradeStatus.COMPLETED
    await db_session.refresh(card_a)
    await db_session.refresh(card_b)
    assert card_a.user_id == bob.id
    assert card_b.user_id == alice.id


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
