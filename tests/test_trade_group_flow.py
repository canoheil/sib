"""Group trade flow, discoverability and safety regression tests.

Covers (task requirements):
- trade is discoverable from the GROUP menu (and Games/Guide are there too);
- a valid Card ↔ Card trade executed through the real handler callbacks;
- ownership is validated, the final transfer is atomic;
- stale/invalid cards fail safely (no corruption);
- cancellation, duplicate confirmations and one-sided confirmation;
- a concurrency race where a card is claimed by two trades at once
  (row-lock test: runs on MySQL/MariaDB, skipped on SQLite).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser
from sqlalchemy import select

from sib.database.models import PlayerCard, ReleaseStatus, Trade, TradeStatus
from sib.handlers.trading import (
    _pending_view,
    handle_trade_cancel,
    handle_trade_join,
    handle_trade_offer,
    handle_trade_ok,
    handle_trade_pick,
)
from sib.keyboards.common import get_group_welcome_keyboard, get_profile_keyboard
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.services.trading import TradeError, TradeService
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _button_data(kb):
    return [b.callback_data for b in _all_buttons(kb)]


async def _card_for(db_session, user, code: str, *, level: int = 1) -> PlayerCard:
    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"قهرمان {code}",
        power=22,
        speed=20,
        intelligence=18,
        passive_key="test_passive",
    )
    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    await db_session.commit()
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()
    return card


def _callback(user, data: str, *, chat_id: int = -100900, message_id: int = 555,
              chat_type: str = "supergroup") -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name or "کاربر")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type=chat_type, title="گروه مبادله")
    cb.message.message_id = message_id
    cb.message.text = "panel"
    cb.message.reply_markup = None
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _edit_payload(cb, bot):
    """Whichever edit path the transition engine used -> (text, reply_markup)."""
    call = cb.message.edit_text.call_args or bot.edit_message_text.call_args
    if call is None:
        return None, None
    text = call.args[0] if call.args else call.kwargs.get("text")
    return text, call.kwargs.get("reply_markup")


def _new_bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.send_message = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# Discoverability
# ---------------------------------------------------------------------------


def test_group_menu_exposes_games_guide_and_profile_keeps_trade():
    kb = get_group_welcome_keyboard()
    data = _button_data(kb)
    labels = " | ".join(b.text for b in _all_buttons(kb))

    # Group features exist in the group menu.
    assert "group_games_panel" in data
    assert "help_menu" in data
    assert "🎮" in labels and "📖" in labels
    # The group menu still keeps the rating / shop / profile entries.
    assert "group_rating_stats" in data
    assert "apple_shop" in data and "apple_profile" in data
    # Card trading is NOT a main-menu shortcut anymore; it is reached from
    # «👤 پروفایل من» (its handler/callback are unchanged).
    assert "apple_trade" not in data
    profile = _button_data(get_profile_keyboard())
    assert any(cb and cb.startswith("apple_trade") for cb in profile)
    assert any(cb and cb.startswith("apple_cards") for cb in profile)  # My Cards stays


@pytest.mark.asyncio
async def test_trade_hub_offers_proposal_and_back(db_session):
    user = await create_test_user(db_session, "صاحب کارت")
    await _card_for(db_session, user, "hub_a")

    text, kb = await _pending_view(user.id)
    data = _button_data(kb)
    assert "trade_new" in data  # there is a real way to propose a trade
    assert "main_menu" in data  # back to the main menu
    assert "هم‌سطح" in text  # the same-level rule is communicated


# ---------------------------------------------------------------------------
# End-to-end group flow through the real handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_trade_flow_completes_and_swaps(db_session):
    alice = await create_test_user(db_session, "آلیس")
    bob = await create_test_user(db_session, "باب")
    alice_card = await _card_for(db_session, alice, "flow_a")
    bob_card = await _card_for(db_session, bob, "flow_b")

    bot = _new_bot()

    # 1. Alice publishes an offer for her card.
    offer_cb = _callback(alice, f"trade_offer:{alice_card.id}")
    await handle_trade_offer(offer_cb, bot)
    assert offer_cb.message.answer.called
    offer_text = offer_cb.message.answer.call_args.args[0]
    offer_kb = offer_cb.message.answer.call_args.kwargs["reply_markup"]
    assert "پیشنهاد مبادله" in offer_text
    assert f"trade_join:{alice.id}:{alice_card.id}" in _button_data(offer_kb)

    # 2. Bob accepts and only same-level cards are offered to him.
    join_cb = _callback(bob, f"trade_join:{alice.id}:{alice_card.id}")
    await handle_trade_join(join_cb, bot)
    join_text, join_kb = _edit_payload(join_cb, bot)
    assert join_kb is not None
    assert f"trade_pick:{alice.id}:{alice_card.id}:{bob_card.id}" in _button_data(join_kb)

    # 3. Bob picks his card -> the trade row is created.
    pick_cb = _callback(bob, f"trade_pick:{alice.id}:{alice_card.id}:{bob_card.id}")
    await handle_trade_pick(pick_cb, bot)
    trade_rows = list((await db_session.scalars(select(Trade))).all())
    assert len(trade_rows) == 1
    trade = trade_rows[0]
    assert trade.status == TradeStatus.PENDING
    assert trade.proposer_id == alice.id and trade.recipient_id == bob.id

    # 4. Both confirmations (one each) execute the swap.
    ok_alice = _callback(alice, f"trade_ok:{trade.id}", message_id=777)
    await handle_trade_ok(ok_alice, bot)
    db_session.expunge_all()  # drop identity-map state; the handler committed on its own session

    awaiting = await TradeService.get_trade(db_session, trade.id)
    assert awaiting.status == TradeStatus.PENDING  # one-sided confirmation is not enough
    assert awaiting.proposer_confirmed is True

    ok_bob = _callback(bob, f"trade_ok:{trade.id}", message_id=777)
    await handle_trade_ok(ok_bob, bot)
    db_session.expunge_all()  # drop identity-map state; the handler committed on its own session

    done = await TradeService.get_trade(db_session, trade.id)
    assert done.status == TradeStatus.COMPLETED
    alice_card_db = await db_session.get(PlayerCard, alice_card.id)
    bob_card_db = await db_session.get(PlayerCard, bob_card.id)
    await db_session.refresh(alice_card_db)
    await db_session.refresh(bob_card_db)
    assert alice_card_db.user_id == bob.id
    assert bob_card_db.user_id == alice.id


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposer_cannot_accept_their_own_offer(db_session):
    alice = await create_test_user(db_session, "خودپذیر")
    card = await _card_for(db_session, alice, "self_a")

    cb = _callback(alice, f"trade_join:{alice.id}:{card.id}")
    await handle_trade_join(cb, _new_bot())
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    assert not cb.message.edit_text.called or "trade_pick" not in str(
        (cb.message.edit_text.call_args or MagicMock()).kwargs
    )


@pytest.mark.asyncio
async def test_trade_with_stale_card_fails_safely(db_session):
    """Card transferred elsewhere before completion -> trade fails, no corruption."""
    alice = await create_test_user(db_session, "مالک الف")
    bob = await create_test_user(db_session, "مالک ب")
    carol = await create_test_user(db_session, "مالک ج")
    alice_card = await _card_for(db_session, alice, "stale_a")
    bob_card = await _card_for(db_session, bob, "stale_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.commit()

    # The card is moved away by ANOTHER feature before the trade completes.
    alice_card.user_id = carol.id
    await db_session.commit()

    with pytest.raises(TradeError):
        await TradeService.confirm(db_session, trade.id, bob.id)
    await db_session.commit()

    await db_session.refresh(trade)
    await db_session.refresh(alice_card)
    await db_session.refresh(bob_card)

    assert trade.status == TradeStatus.FAILED
    assert alice_card.user_id == carol.id  # untouched by the failed trade
    assert bob_card.user_id == bob.id


@pytest.mark.asyncio
async def test_duplicate_confirmations_never_double_swap(db_session):
    alice = await create_test_user(db_session, "تکراری الف")
    bob = await create_test_user(db_session, "تکراری ب")
    alice_card = await _card_for(db_session, alice, "dup_a")
    bob_card = await _card_for(db_session, bob, "dup_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    # Double tap / duplicate callback by the SAME user is idempotent.
    await TradeService.confirm(db_session, trade.id, alice.id)
    await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.commit()
    assert trade.status == TradeStatus.PENDING

    await TradeService.confirm(db_session, trade.id, bob.id)
    await db_session.commit()
    assert trade.status == TradeStatus.COMPLETED

    # Any further confirmation is refused and can never swap a second time.
    with pytest.raises(TradeError):
        await TradeService.confirm(db_session, trade.id, alice.id)
    await db_session.commit()

    await db_session.refresh(alice_card)
    await db_session.refresh(bob_card)
    assert alice_card.user_id == bob.id
    assert bob_card.user_id == alice.id


@pytest.mark.asyncio
async def test_non_participant_handler_press_is_rejected(db_session):
    alice = await create_test_user(db_session, "بازیگر الف")
    bob = await create_test_user(db_session, "بازیگر ب")
    mallory = await create_test_user(db_session, "غریبه")
    alice_card = await _card_for(db_session, alice, "guard_a")
    bob_card = await _card_for(db_session, bob, "guard_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    await db_session.commit()

    cb = _callback(mallory, f"trade_ok:{trade.id}")
    await handle_trade_ok(cb, _new_bot())
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    db_session.expunge_all()
    unchanged = await TradeService.get_trade(db_session, trade.id)
    assert unchanged.status == TradeStatus.PENDING
    assert unchanged.proposer_confirmed is False


@pytest.mark.asyncio
async def test_cancellation_frees_the_cards(db_session):
    alice = await create_test_user(db_session, "لغو الف")
    bob = await create_test_user(db_session, "لغو ب")
    alice_card = await _card_for(db_session, alice, "cancel_a")
    bob_card = await _card_for(db_session, bob, "cancel_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    await db_session.commit()

    cb = _callback(bob, f"trade_cancel:{trade.id}")
    await handle_trade_cancel(cb, _new_bot())
    db_session.expunge_all()  # drop identity-map state; the handler committed on its own session

    cancelled = await TradeService.get_trade(db_session, trade.id)
    assert cancelled.status == TradeStatus.CANCELLED
    # Move the cancelled offer to yesterday so today's quota is free again
    # (the point here is that the CARDS are released, not the daily limit).
    cancelled.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()

    # A different trade on the same cards is possible again.
    new_trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    assert new_trade.status == TradeStatus.PENDING


@pytest.mark.asyncio
async def test_same_card_cannot_be_in_two_open_trades(db_session):
    alice = await create_test_user(db_session, "دوگانه الف")
    bob = await create_test_user(db_session, "دوگانه ب")
    carol = await create_test_user(db_session, "دوگانه ج")
    alice_card = await _card_for(db_session, alice, "twice_a")
    bob_card = await _card_for(db_session, bob, "twice_b")
    carol_card = await _card_for(db_session, carol, "twice_c")

    first = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    # Free today's quota so the ONLY possible blocker is the open trade.
    first.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()

    with pytest.raises(TradeError, match="معامله‌ی باز"):
        await TradeService.create(
            db_session,
            proposer_id=alice.id,
            recipient_id=carol.id,
            proposer_card_id=alice_card.id,
            recipient_card_id=carol_card.id,
        )


@pytest.mark.asyncio
async def test_cannot_trade_a_card_with_itself(db_session):
    alice = await create_test_user(db_session, "خودکارت الف")
    bob = await create_test_user(db_session, "خودکارت ب")
    card = await _card_for(db_session, alice, "selfcard_a")

    with pytest.raises(TradeError):
        await TradeService.create(
            db_session,
            proposer_id=alice.id,
            recipient_id=bob.id,
            proposer_card_id=card.id,
            recipient_card_id=card.id,
        )


@pytest.mark.asyncio
async def test_expire_overdue_sweeps_stale_offers(db_session):
    alice = await create_test_user(db_session, "کهنه الف")
    bob = await create_test_user(db_session, "کهنه ب")
    alice_card = await _card_for(db_session, alice, "staleoff_a")
    bob_card = await _card_for(db_session, bob, "staleoff_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    trade.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    swept = await TradeService.expire_overdue(db_session)
    await db_session.commit()
    assert swept >= 1
    await db_session.refresh(trade)
    assert trade.status == TradeStatus.EXPIRED
    # Free today's quota so the ONLY possible blocker is a stale offer.
    trade.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()

    # An expired trade frees its cards.
    fresh = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    assert fresh.status == TradeStatus.PENDING


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_trades_cannot_double_spend_a_card(db_session):
    """Two simultaneous trades on the same card: exactly one may win.

    ``SELECT ... FOR UPDATE`` serializes the two create() calls on
    MySQL/MariaDB; the pending-trade check then rejects the loser.
    """
    from sib.database.connection import get_async_sessionmaker

    alice = await create_test_user(db_session, "همزمان الف")
    bob = await create_test_user(db_session, "همزمان ب")
    carol = await create_test_user(db_session, "همزمان ج")
    alice_card = await _card_for(db_session, alice, "race_a")
    bob_card = await _card_for(db_session, bob, "race_b")
    carol_card = await _card_for(db_session, carol, "race_c")
    await db_session.commit()

    maker = get_async_sessionmaker()

    async def attempt(recipient, recipient_card):
        async with maker() as session:
            try:
                trade = await TradeService.create(
                    session,
                    proposer_id=alice.id,
                    recipient_id=recipient.id,
                    proposer_card_id=alice_card.id,
                    recipient_card_id=recipient_card.id,
                )
                await session.commit()
                return trade.id
            except TradeError:
                await session.rollback()
                return None

    results = await asyncio.gather(
        attempt(bob, bob_card),
        attempt(carol, carol_card),
        return_exceptions=False,
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1

    pending = list(
        (
            await db_session.scalars(
                select(Trade).where(Trade.status == TradeStatus.PENDING)
            )
        ).all()
    )
    assert len(pending) == 1
    assert pending[0].proposer_card_id == alice_card.id


@pytest.mark.asyncio
async def test_maintenance_cycle_sweeps_stale_trade_offers(db_session):
    """The periodic worker is the scheduled caller of the stale-offer sweep."""
    from sib.services.maintenance.service import MaintenanceService

    alice = await create_test_user(db_session, "چرخه‌کهنه الف")
    bob = await create_test_user(db_session, "چرخه‌کهنه ب")
    alice_card = await _card_for(db_session, alice, "cyclesweep_a")
    bob_card = await _card_for(db_session, bob, "cyclesweep_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    trade.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    await MaintenanceService.run_once()

    db_session.expunge_all()
    refreshed = await db_session.get(Trade, trade.id)
    assert refreshed.status == TradeStatus.EXPIRED


# ---------------------------------------------------------------------------
# Reported-flow regression: second confirmation crashed the bot
# (IntegrityError on uq_player_card_user_character) and burned the daily
# quota. The full handler flow must complete the swap and answer cleanly.
# ---------------------------------------------------------------------------


async def _same_character_cards(db_session, user_a, user_b, code: str):
    """One character, one card per user (both cards of the SAME character)."""
    from sib.database.models import ReleaseStatus

    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"قهرمان {code}",
        power=22,
        speed=20,
        intelligence=18,
    )
    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    await db_session.commit()
    card_a = await PlayerCardService.acquire(db_session, user_a.id, character.id)
    card_b = await PlayerCardService.acquire(db_session, user_b.id, character.id)
    await db_session.commit()
    return card_a, card_b


@pytest.mark.asyncio
async def test_reported_flow_same_character_completes_and_swaps(db_session):
    """A creates Trade → B joins → both select Cards → A confirms → B confirms.

    With two cards of the SAME character the second confirmation used to die
    with a raw IntegrityError (the two direct user_id UPDATEs violated
    uq_player_card_user_character mid-swap). Now the flow must complete the
    atomic swap, answer both confirmers cleanly and keep quota accounting
    consistent (the COMPLETED trade uses the day's slot)."""
    alice = await create_test_user(db_session, "گزارش الف")
    bob = await create_test_user(db_session, "گزارش ب")
    alice_card, bob_card = await _same_character_cards(db_session, alice, bob, "report_char")

    bot = _new_bot()

    # 1. Alice publishes the offer, Bob joins and picks his same-level card.
    await handle_trade_offer(_callback(alice, f"trade_offer:{alice_card.id}"), bot)
    await handle_trade_join(_callback(bob, f"trade_join:{alice.id}:{alice_card.id}"), bot)
    pick_cb = _callback(bob, f"trade_pick:{alice.id}:{alice_card.id}:{bob_card.id}")
    await handle_trade_pick(pick_cb, bot)

    trades = list((await db_session.scalars(select(Trade))).all())
    assert len(trades) == 1
    trade = trades[0]

    # 2. A confirms (first side: no swap yet).
    ok_alice = _callback(alice, f"trade_ok:{trade.id}", message_id=777)
    await handle_trade_ok(ok_alice, bot)  # must NOT raise

    # 3. B confirms — this press used to error with IntegrityError.
    ok_bob = _callback(bob, f"trade_ok:{trade.id}", message_id=777)
    await handle_trade_ok(ok_bob, bot)  # must NOT raise

    # Clean UX: the second confirmer is told the trade went through.
    assert ok_bob.answer.call_args is not None
    assert "مبادله انجام شد" in (ok_bob.answer.call_args.args[0] or "")

    db_session.expunge_all()
    done = await TradeService.get_trade(db_session, trade.id)
    assert done.status == TradeStatus.COMPLETED
    a_after = await db_session.get(PlayerCard, alice_card.id)
    b_after = await db_session.get(PlayerCard, bob_card.id)
    await db_session.refresh(a_after)
    await db_session.refresh(b_after)
    assert a_after.user_id == bob.id
    assert b_after.user_id == alice.id

    # The COMPLETED trade consumes the day's quota (deterministic accounting).
    assert not await TradeService.can_trade_today(db_session, alice.id)
    assert not await TradeService.can_trade_today(db_session, bob.id)


@pytest.mark.asyncio
async def test_failed_confirm_shows_friendly_alert_and_frees_quota(db_session):
    """When finalization fails, the confirmer gets a FRIENDLY alert (never a
    raw exception), nothing moves, and the quota is rolled back."""
    alice = await create_test_user(db_session, "هشدار الف")
    bob = await create_test_user(db_session, "هشدار ب")
    carol = await create_test_user(db_session, "هشدار ج")
    alice_card = await _card_for(db_session, alice, "alert_a")
    bob_card = await _card_for(db_session, bob, "alert_b")

    trade = await TradeService.create(
        db_session,
        proposer_id=alice.id,
        recipient_id=bob.id,
        proposer_card_id=alice_card.id,
        recipient_card_id=bob_card.id,
    )
    await db_session.commit()

    ok_alice = _callback(alice, f"trade_ok:{trade.id}")
    await handle_trade_ok(ok_alice, bot=_new_bot())

    # Ownership changes under the trade's feet before Bob confirms.
    alice_card.user_id = carol.id
    await db_session.commit()

    ok_bob = _callback(bob, f"trade_ok:{trade.id}")
    await handle_trade_ok(ok_bob, bot=_new_bot())  # TradeError -> alert, no raise

    assert ok_bob.answer.call_args.kwargs.get("show_alert") is True
    alert_text = ok_bob.answer.call_args.args[0]
    assert "معامله" in alert_text  # friendly Persian alert, not a crash

    db_session.expunge_all()
    failed = await TradeService.get_trade(db_session, trade.id)
    assert failed.status == TradeStatus.FAILED
    a_after = await db_session.get(PlayerCard, alice_card.id)
    await db_session.refresh(a_after)
    assert a_after.user_id == carol.id  # untouched

    # Quota rollback/consistency: both users keep today's trade slot.
    assert await TradeService.can_trade_today(db_session, alice.id)
    assert await TradeService.can_trade_today(db_session, bob.id)
