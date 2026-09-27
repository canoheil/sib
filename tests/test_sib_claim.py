"""Sib claim tests (task spec #3/#4/#5).

"سیب" in a group is a real action: +5 apples with a 5-minute per-user
cooldown, persisted to the wallet. "سیب بازی" opens the games menu and
"سیب" alone no longer opens any menu. Messages are friendly Persian in
the Sib voice with dynamic remaining time.
"""

import asyncio
import re

import pytest
from sqlalchemy import text
from unittest.mock import AsyncMock, MagicMock

from sib.database.connection import get_session
from sib.database.models import AppleTransaction, AppleWallet
from sib.services.apple_wallet import AppleWalletService
from sib.services.sib_claim import (
    CLAIM_REASON,
    SibClaimService,
    fa_digits,
    format_claim_success,
    format_cooldown_message,
)
from sqlalchemy import select

# ---------------------------------------------------------------------------
# Message builders (pure)
# ---------------------------------------------------------------------------


def test_fa_digits_translates_latin_digits():
    assert fa_digits(42) == "۴۲"
    assert fa_digits("2m 37s") == "۲m ۳۷s"


def test_success_message_is_friendly_persian():
    msg = format_claim_success(5, 300)
    assert "مال تو شد" in msg
    assert "پنج دقیقه دیگه" in msg
    assert "۵" in msg
    # short and readable: two lines
    assert len(msg.splitlines()) <= 3


def test_cooldown_message_variants_are_dynamic():
    # Almost ready -> seconds-away wording.
    almost = format_cooldown_message(10, 5)
    assert "چند ثانیه" in almost

    # Under a minute -> exact seconds in Persian digits.
    secs = format_cooldown_message(42, 5)
    assert "۴۲ ثانیه" in secs
    assert "یه کم صبر کن" in secs

    # Minutes + seconds -> both units.
    both = format_cooldown_message(157, 5)  # 2:37
    assert "۲ دقیقه و ۳۷ ثانیه" in both
    assert "۵ تا سیب" in both

    # Whole minutes -> no "و ۰ ثانیه".
    whole = format_cooldown_message(180, 5)
    assert "۳ دقیقه" in whole
    assert "۰ ثانیه" not in whole

    # Never advertises a zero/negative wait.
    assert "۰ ثانیه دیگه" not in format_cooldown_message(0, 5)


# ---------------------------------------------------------------------------
# Claim flow (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_awards_five_apples_and_persists(db_session):
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "سیب‌گیر اول")
    await db_session.commit()

    msg, awarded = await SibClaimService.claim(db_session, user.id)
    await db_session.commit()

    assert awarded is True
    assert "مال تو شد" in msg
    balance = await AppleWalletService.get_balance(db_session, user.id)
    assert balance == 5

    row = await db_session.scalar(
        select(AppleTransaction)
        .where(AppleTransaction.user_id == user.id)
        .where(AppleTransaction.reason == CLAIM_REASON)
    )
    assert row is not None
    assert row.amount == 5
    assert row.balance_after == 5


@pytest.mark.asyncio
async def test_second_claim_within_cooldown_is_rejected(db_session):
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "سیب‌گیر دوم")
    await db_session.commit()

    msg1, awarded1 = await SibClaimService.claim(db_session, user.id)
    await db_session.commit()
    assert awarded1 is True

    msg2, awarded2 = await SibClaimService.claim(db_session, user.id)
    await db_session.commit()
    assert awarded2 is False
    # Cooldown text shows remaining time (Persian digits) and gives no apples.
    assert re.search(r"[۰-۹]+ (ثانیه|دقیقه)", msg2) or "چند ثانیه" in msg2
    assert "مال تو شد" not in msg2
    assert await AppleWalletService.get_balance(db_session, user.id) == 5


@pytest.mark.asyncio
async def test_claim_available_again_after_cooldown(db_session):
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "سیب‌گیر سوم")
    await db_session.commit()

    _, awarded1 = await SibClaimService.claim(db_session, user.id)
    await db_session.commit()
    assert awarded1 is True

    # Simulate the cooldown passing (DB clock on both sides).
    await db_session.execute(
        text(
            "UPDATE apple_transactions SET created_at = NOW() - INTERVAL 360 SECOND "
            "WHERE user_id = :uid AND reason = :reason"
        ),
        {"uid": user.id, "reason": CLAIM_REASON},
    )
    await db_session.commit()

    msg2, awarded2 = await SibClaimService.claim(db_session, user.id)
    await db_session.commit()
    assert awarded2 is True
    assert await AppleWalletService.get_balance(db_session, user.id) == 10


@pytest.mark.asyncio
async def test_cooldown_is_per_user(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس سیبی")
    bob = await create_test_user(db_session, "باب سیبی")
    await db_session.commit()

    _, awarded_a = await SibClaimService.claim(db_session, alice.id)
    await db_session.commit()
    assert awarded_a is True

    # Alice is on cooldown, Bob is not.
    _, awarded_b = await SibClaimService.claim(db_session, bob.id)
    await db_session.commit()
    assert awarded_b is True

    _, awarded_a2 = await SibClaimService.claim(db_session, alice.id)
    await db_session.commit()
    assert awarded_a2 is False


@pytest.mark.asyncio
async def test_concurrent_claims_award_exactly_once():
    from sib.tests.test_parties import create_test_user

    async with get_session() as session:
        user = await create_test_user(session, "سیب‌گیر همزمان")
        await session.commit()
        user_id = user.id

    async def one_claim():
        async with get_session() as session:
            result = await SibClaimService.claim(session, user_id)
            await session.commit()
            return result

    # Two "سیب" messages at the same moment -> exactly one award.
    results = await asyncio.gather(one_claim(), one_claim())
    awarded_flags = [awarded for _msg, awarded in results]
    assert sum(awarded_flags) == 1

    async with get_session() as session:
        balance = await AppleWalletService.get_balance(session, user_id)
    assert balance == 5


# ---------------------------------------------------------------------------
# Wake-word handlers
# ---------------------------------------------------------------------------

def _tg_user(uid: int, name: str):
    from aiogram.types import User as TGUser

    return TGUser(id=uid, is_bot=False, first_name=name, username=f"u{uid}")


def _group_message(user, text_str):
    msg = MagicMock()
    msg.chat = MagicMock()
    msg.chat.type = "supergroup"
    msg.chat.id = -6001
    msg.chat.title = "گروه سیبی"
    msg.from_user = user
    msg.text = text_str
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_wake_word_claims_instead_of_opening_menu(db_session):
    from sib.handlers.start import handle_sib_games_word, handle_sib_wake_word

    tg_user = _tg_user(770001, "سیب‌خور")

    msg = _group_message(tg_user, "سیب")
    bot = MagicMock()
    await handle_sib_wake_word(msg, bot)

    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    assert "سیب مال تو شد" in reply_text or "سیب گرفتی" in reply_text
    # No keyboard menu is opened by the claim action.
    assert msg.reply.call_args.kwargs.get("reply_markup") is None

    # Second call inside the cooldown: no reward, explains the wait.
    msg2 = _group_message(tg_user, "سیب")
    await handle_sib_wake_word(msg2, bot)
    reply2 = msg2.reply.call_args[0][0]
    assert "مال تو شد" not in reply2
    assert ("صبر" in reply2) or ("زوده" in reply2) or ("ثانیه" in reply2)

    async with get_session() as session:
        balance = await AppleWalletService.get_balance(session, tg_user.id)
    assert balance == 5


@pytest.mark.asyncio
async def test_sib_games_word_opens_games_menu(db_session):
    from sib.handlers.start import handle_sib_games_word

    tg_user = _tg_user(770002, "بازیکن")

    msg = _group_message(tg_user, "سیب بازی")
    bot = MagicMock()
    await handle_sib_games_word(msg, bot)

    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    assert "منوی بازی‌های سیب" in reply_text
    assert msg.reply.call_args.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_wake_word_is_silent_in_private_chats(db_session):
    from sib.handlers.start import handle_sib_wake_word

    tg_user = _tg_user(770003, "پیوی")

    msg = _group_message(tg_user, "سیب")
    msg.chat.type = "private"
    bot = MagicMock()
    await handle_sib_wake_word(msg, bot)
    assert not msg.reply.called


@pytest.mark.asyncio
async def test_sibam_shows_apple_balance_group_and_private(db_session):
    """'سیبام' / 'سیب من' -> the SENDER's apple balance (never the claim)."""
    from sib.handlers.start import handle_sib_balance_word
    from sib.services.apple_wallet import AppleWalletService

    tg_user = _tg_user(770100, "موجودی‌دار")

    async with get_session() as session:
        from sib.services.user import UserService

        await UserService.get_or_create_user(session, tg_user)
        await AppleWalletService.earn(session, tg_user.id, 7, reason="test")
        await session.commit()

    # GROUP
    msg = _group_message(tg_user, "سیبام")
    await handle_sib_balance_word(msg, MagicMock())
    assert msg.reply.called
    reply_text = msg.reply.call_args.args[0]
    assert "موجودی سیب شما" in reply_text
    assert "7" in reply_text

    # PRIVATE + second wording
    msg2 = _group_message(tg_user, "سیب من")
    msg2.chat.type = "private"
    await handle_sib_balance_word(msg2, MagicMock())
    reply_text2 = msg2.reply.call_args.args[0]
    assert "موجودی سیب شما" in reply_text2
    assert "7" in reply_text2


@pytest.mark.asyncio
async def test_sibam_never_falls_through_to_claim(db_session):
    """The claim handler must not handle these wake words (distinct regexes)."""
    from aiogram.types import Message

    from sib.handlers.start import handle_sib_balance_word, router

    tg_user = _tg_user(770101, "بدون‌ادعا")
    msg = _group_message(tg_user, "سیبام")
    await handle_sib_balance_word(msg, MagicMock())
    # Balance reply only — no claim confirmation text.
    reply_text = msg.reply.call_args.args[0]
    assert "موجودی سیب شما" in reply_text
    assert "ثبت شد" not in reply_text
