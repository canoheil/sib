"""Sib claim tests (task spec #3/#4/#5).

"سیب" in a group is a real action: +5 apples with a 5-minute per-user
cooldown, persisted to the wallet. "سیب بازی" opens the games menu and
"سیب" alone no longer opens any menu. Messages are friendly Persian in
the Sib voice with dynamic remaining time.
"""

import asyncio
import re

import pytest

from sib.tests.conftest import requires_row_locks
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

    # Simulate the cooldown passing (dialect-neutral: set the timestamp via
    # the ORM instead of dialect-specific SQL date math).
    from datetime import datetime, timedelta, timezone

    rows = (
        (
            await db_session.execute(
                select(AppleTransaction).where(
                    AppleTransaction.user_id == user.id
                ).where(AppleTransaction.reason == CLAIM_REASON)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.created_at = datetime.now(timezone.utc) - timedelta(seconds=360)
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
@requires_row_locks
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
    msg.chat.username = None  # mock hygiene: never leak MagicMock into DB columns
    msg.from_user = user
    msg.text = text_str
    msg.reply = AsyncMock()
    # Real-looking sent message so panel claiming can persist ints.
    msg.reply.return_value = MagicMock(message_id=770, chat=MagicMock(id=-6001))
    msg.reply_photo = AsyncMock()
    msg.reply_photo.return_value = MagicMock(message_id=771, chat=MagicMock(id=-6001))
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

    # The games screen renders as its banner + caption + keyboard (photo
    # path) or falls back to a plain text reply — either way the menu opens.
    assert msg.reply.called or msg.reply_photo.called
    if msg.reply.called:
        reply_text = msg.reply.call_args[0][0]
        reply_markup = msg.reply.call_args.kwargs.get("reply_markup")
    else:
        reply_text = msg.reply_photo.call_args.kwargs.get("caption", "")
        reply_markup = msg.reply_photo.call_args.kwargs.get("reply_markup")
    assert "منوی بازی‌های سیب" in reply_text
    assert reply_markup is not None


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
async def test_sibam_shows_apple_balance_with_basket_image(db_session):
    """'سیبام' -> the SENDER's apple balance (never the claim), shown with the
    NEW reusable apple-basket artwork (assets/items/apple_basket.jpg)."""
    from sib.handlers.start import handle_sib_balance_word
    from sib.services.assets import AssetService

    tg_user = _tg_user(770100, "موجودی‌دار")

    async with get_session() as session:
        from sib.services.user import UserService

        await UserService.get_or_create_user(session, tg_user)
        await AppleWalletService.earn(session, tg_user.id, 7, reason="test")
        await session.commit()
        # The asset/loading system serves the bundled basket art.
        file_id, fs_path = await AssetService.resolve_photo(session, "apple_basket")
    assert fs_path is not None
    assert fs_path.name == "apple_basket.jpg"

    # GROUP
    msg = _group_message(tg_user, "سیبام")
    await handle_sib_balance_word(msg, MagicMock())
    assert msg.reply_photo.called
    photo_arg = msg.reply_photo.call_args.args[0]
    assert "apple_basket" in str(getattr(photo_arg, "path", photo_arg))
    caption = msg.reply_photo.call_args.kwargs.get("caption", "")
    assert "موجودی سیب شما" in caption
    assert "7" in caption

    # PRIVATE — same balance, same artwork
    msg2 = _group_message(tg_user, "سیبام")
    msg2.chat.type = "private"
    await handle_sib_balance_word(msg2, MagicMock())
    assert msg2.reply_photo.called
    caption2 = msg2.reply_photo.call_args.kwargs.get("caption", "")
    assert "موجودی سیب شما" in caption2
    assert "7" in caption2


@pytest.mark.asyncio
async def test_my_sib_word_opens_profile_not_balance(db_session):
    """'سیب من' opens the EXISTING profile flow and is no longer an
    Apple-balance alias ('سیبام' is the balance word)."""
    from sib.handlers.start import handle_sib_profile_word
    from sib.services.panel import PanelOwnershipService

    tg_user = _tg_user(770102, "پروفایلی")

    async with get_session() as session:
        from sib.services.user import UserService

        await UserService.get_or_create_user(session, tg_user)
        await session.commit()

    msg = _group_message(tg_user, "سیب من")
    await handle_sib_profile_word(msg, MagicMock())
    # The profile screen carries the player's Level artwork, so its text
    # normally rides as a PHOTO caption; plain text only when no artwork
    # could be loaded. Either way it is the PROFILE, never the balance.
    assert msg.reply_photo.called or msg.reply.called
    if msg.reply_photo.called:
        profile_text = msg.reply_photo.call_args.kwargs["caption"]
        sent = msg.reply_photo.return_value
    else:
        profile_text = msg.reply.call_args.args[0]
        sent = msg.reply.return_value
    assert "پروفایل سیبی" in profile_text
    # NOT the balance screen.
    assert "موجودی سیب شما" not in profile_text

    # The profile panel is claimed to the sender (the one profile system).
    owner = await PanelOwnershipService.get_owner(db_session, sent.chat.id, sent.message_id)
    assert owner == tg_user.id


def test_sibam_and_my_sib_route_distinctly():
    """Wake-word routing invariants: 'سیبام' -> balance, 'سیب من' -> profile."""
    balance = re.compile(r"^\s*سیبام\s*$")
    profile = re.compile(r"^\s*سیب\s+(پروفایل|من)\s*$")
    claim = re.compile(r"^\s*سیب\s*$")
    words = re.compile(r"^\s*سیب\s+(بازی|منو)\s*$")

    assert balance.match("سیبام")
    assert not profile.match("سیبام")
    assert not claim.match("سیبام")

    assert profile.match("سیب من")
    assert not balance.match("سیب من")  # no longer a balance alias
    assert not claim.match("سیب من")
    assert not words.match("سیب من")

    # 'سیب منو' (menu) and the claim word are untouched by the change.
    assert words.match("سیب منو")
    assert not profile.match("سیب منو")
    assert not balance.match("سیب منو")
    assert claim.match("سیب")


@pytest.mark.asyncio
async def test_sibam_never_falls_through_to_claim(db_session):
    """The claim handler must not handle these wake words (distinct regexes)."""
    from sib.handlers.start import handle_sib_balance_word

    tg_user = _tg_user(770101, "بدون‌ادعا")
    msg = _group_message(tg_user, "سیبام")
    await handle_sib_balance_word(msg, MagicMock())
    # Balance reply only — no claim confirmation text.
    assert msg.reply_photo.called or msg.reply.called
    reply_text = (
        msg.reply_photo.call_args.kwargs.get("caption", "")
        if msg.reply_photo.called
        else msg.reply.call_args.args[0]
    )
    assert "موجودی سیب شما" in reply_text
    assert "ثبت شد" not in reply_text
