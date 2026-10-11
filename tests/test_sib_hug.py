"""Sib Hug tests: «بغل سیبی» as a reply-based two-user reward.

Covers validation (no rewards, no cooldown on refusal), the sender-only
cooldown, atomic/idempotent dual rewards, concurrency, Persian formatting,
safe mention names, and the text fallback while the hug artwork is absent.
"""

import asyncio
import struct
import random
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import User as TGUser
from sqlalchemy import func, select

from sib.database.connection import get_session
from sib.database.models import AppleTransaction
from sib.services import marriage_art
from sib.services.apple_wallet import AppleWalletService
from sib.services.sib_claim import CLAIM_REASON, SibClaimService
from sib.services.sib_hug import (
    HUG_AWARDED,
    HUG_COOLDOWN,
    HUG_DUPLICATE,
    HUG_MESSAGE_VARIANTS,
    HUG_RECEIVED_REASON,
    HUG_REASON,
    SibHugIntegrityError,
    SibHugInvalidTargetError,
    SibHugSelfTargetError,
    SibHugService,
    format_hug_cooldown_message,
    hug_idempotency_keys,
    pick_hug_message,
)
from sib.tests.conftest import requires_row_locks

HUG_AMOUNT = 10
HUG_COOLDOWN_SECONDS = 600


def _tg_user(uid: int, name: str, is_bot: bool = False) -> TGUser:
    return TGUser(id=uid, is_bot=is_bot, first_name=name, username=f"u{uid}")


def _group_hug_message(sender: TGUser, text: str, *, target=None, message_id=900, chat_type="supergroup"):
    """Mock group message in the style of tests/test_sib_claim.py."""
    msg = MagicMock()
    msg.chat = MagicMock()
    msg.chat.type = chat_type
    msg.chat.id = -6100
    msg.chat.title = "گروه بغل سیبی"
    msg.chat.username = None
    msg.message_id = message_id
    msg.from_user = sender
    msg.text = text
    msg.reply = AsyncMock(return_value=MagicMock(message_id=message_id + 1))
    msg.reply_photo = AsyncMock(return_value=MagicMock(message_id=message_id + 2))
    if target is None:
        msg.reply_to_message = None
    else:
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.from_user = target
    return msg


async def _ledger(session, reasons=(HUG_REASON, HUG_RECEIVED_REASON)):
    rows = (
        await session.scalars(
            select(AppleTransaction)
            .where(AppleTransaction.reason.in_(reasons))
            .order_by(AppleTransaction.id)
        )
    ).all()
    return list(rows)


async def _balance(session, user_id: int) -> int:
    return await AppleWalletService.get_balance(session, user_id)


async def _backdate_sender_hug(session, user_id: int, seconds_ago: int) -> None:
    """Move the sender's sib_hug ledger row into the past, DB-clock based."""
    db_now = await session.scalar(select(func.now()))
    row = await session.scalar(
        select(AppleTransaction)
        .where(AppleTransaction.user_id == user_id)
        .where(AppleTransaction.reason == HUG_REASON)
    )
    assert row is not None, "expected a sib_hug row to backdate"
    row.created_at = db_now - timedelta(seconds=seconds_ago)
    await session.commit()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_at_least_ten_distinct_hug_variants():
    assert len(HUG_MESSAGE_VARIANTS) >= 10
    assert len(set(HUG_MESSAGE_VARIANTS)) == len(HUG_MESSAGE_VARIANTS)
    for template in HUG_MESSAGE_VARIANTS:
        assert "{a}" in template and "{b}" in template and "{n}" in template


def test_pick_hug_message_mentions_both_users_and_amount():
    text = pick_hug_message("[آلیس](tg://user?id=1)", "[باب](tg://user?id=2)", 10)
    assert "[آلیس](tg://user?id=1)" in text
    assert "[باب](tg://user?id=2)" in text
    # Persian digits for the amount, never Latin.
    assert "۱۰" in text
    assert "10" not in text


def test_pick_hug_message_varies_across_variants():
    rng = random.Random(7)
    seen = {
        pick_hug_message("A", "B", 10, rng=rng) for _ in range(200)
    }
    assert len(seen) >= 8


def test_cooldown_message_exact_text():
    prefix = "🤗 هنوز زوده! "
    suffix = " دیگه می\u200cتونی دوباره بغل سیبی بدی. 🍎"
    assert format_hug_cooldown_message(600) == prefix + "۱۰ دقیقه" + suffix
    assert format_hug_cooldown_message(42) == prefix + "۴۲ ثانیه" + suffix
    assert format_hug_cooldown_message(157) == prefix + "۲ دقیقه و ۳۷ ثانیه" + suffix
    # Fractions round UP, so the user is never told "۰ ثانیه".
    assert format_hug_cooldown_message(0.2) == prefix + "۱ ثانیه" + suffix


def test_idempotency_keys_are_distinct_and_deterministic():
    a1, r1 = hug_idempotency_keys("-100:5", 1, 2)
    a2, r2 = hug_idempotency_keys("-100:5", 1, 2)
    assert (a1, r1) == (a2, r2)
    assert a1 != r1
    # A different message yields different keys.
    assert hug_idempotency_keys("-100:6", 1, 2)[0] != a1


@pytest.mark.parametrize(
    "text",
    ["بغل سیبی", "  بغل سیبی  ", "بغل سیبی؟", "بغل سیبی?", "بغل سیبی"],
)
def test_trigger_matches_exact_phrase_variants(text):
    from sib.handlers.sib_hug import HUG_TRIGGER_PATTERN

    assert re.match(HUG_TRIGGER_PATTERN, text)


@pytest.mark.parametrize(
    "text",
    ["بغل", "سیب", "بغل سیبی کن", "امروز بغل سیبی کردم", "بغل کردن", "بغل سیب"],
)
def test_trigger_ignores_unrelated_text(text):
    from sib.handlers.sib_hug import HUG_TRIGGER_PATTERN

    assert not re.match(HUG_TRIGGER_PATTERN, text)


def test_hug_scene_key_is_registered_for_media_helper():
    assert "hug" in marriage_art.GROUP_SCENES
    # The marriage flow contract stays exactly the four committed scenes.
    assert "hug" not in marriage_art.SCENES


# ---------------------------------------------------------------------------
# Domain service (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_hug_rewards_both_users_with_two_entries(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس بغل")
    bob = await create_test_user(db_session, "باب بغل")
    await db_session.commit()

    result = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:1"
    )
    await db_session.commit()

    assert result.status == HUG_AWARDED
    assert result.amount == HUG_AMOUNT
    assert await _balance(db_session, alice.id) == HUG_AMOUNT
    assert await _balance(db_session, bob.id) == HUG_AMOUNT

    rows = await _ledger(db_session)
    assert len(rows) == 2
    sender_row = next(r for r in rows if r.reason == HUG_REASON)
    recipient_row = next(r for r in rows if r.reason == HUG_RECEIVED_REASON)
    assert sender_row.user_id == alice.id and sender_row.amount == HUG_AMOUNT
    assert recipient_row.user_id == bob.id and recipient_row.amount == HUG_AMOUNT
    assert sender_row.idempotency_key != recipient_row.idempotency_key


@pytest.mark.asyncio
async def test_second_hug_within_cooldown_is_rejected_without_writes(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس کولدان")
    bob = await create_test_user(db_session, "باب کولدان")
    carol = await create_test_user(db_session, "کارول کولدان")
    await db_session.commit()

    await SibHugService.hug(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:10")
    await db_session.commit()
    before = len(await _ledger(db_session))

    result = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=carol.id, message_key="-100:11"
    )
    await db_session.commit()

    assert result.status == HUG_COOLDOWN
    assert result.remaining_seconds > 0
    assert len(await _ledger(db_session)) == before
    assert await _balance(db_session, carol.id) == 0
    assert await _balance(db_session, alice.id) == HUG_AMOUNT


@pytest.mark.asyncio
async def test_receiving_a_hug_does_not_consume_recipient_cooldown(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس دریافت")
    bob = await create_test_user(db_session, "باب دریافت")
    carol = await create_test_user(db_session, "کارول دریافت")
    await db_session.commit()

    first = await SibHugService.hug(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:20")
    await db_session.commit()
    assert first.status == HUG_AWARDED

    # Bob was hugged by Alice; Bob's own hug must still be allowed.
    second = await SibHugService.hug(db_session, sender_id=bob.id, target_id=carol.id, message_key="-100:21")
    await db_session.commit()
    assert second.status == HUG_AWARDED
    assert await _balance(db_session, bob.id) == HUG_AMOUNT * 2


@pytest.mark.asyncio
async def test_hug_cooldown_does_not_block_claim_and_claim_does_not_block_hug(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس مستقل")
    bob = await create_test_user(db_session, "باب مستقل")
    carol = await create_test_user(db_session, "کارول مستقل")
    await db_session.commit()

    # A hug does not block the wake-word claim for the sender or recipient.
    await SibHugService.hug(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:30")
    await db_session.commit()
    _, claimed_alice = await SibClaimService.claim(db_session, alice.id)
    await db_session.commit()
    _, claimed_bob = await SibClaimService.claim(db_session, bob.id)
    await db_session.commit()
    assert claimed_alice is True
    assert claimed_bob is True

    # A claim does not block a hug.
    result = await SibHugService.hug(db_session, sender_id=bob.id, target_id=carol.id, message_key="-100:31")
    await db_session.commit()
    assert result.status == HUG_AWARDED

    # Claim ledger rows are untouched by the hug and keep their own reason.
    claim_rows = (
        await db_session.scalars(
            select(AppleTransaction).where(AppleTransaction.reason == CLAIM_REASON)
        )
    ).all()
    assert len(claim_rows) == 2


@pytest.mark.asyncio
async def test_reward_writes_run_in_ascending_user_id_order(db_session, monkeypatch):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس ترتیب")
    bob = await create_test_user(db_session, "باب ترتیب")
    await db_session.commit()

    order = []
    real_earn = AppleWalletService.earn

    async def record(session, **kwargs):
        order.append(kwargs["user_id"])
        return await real_earn(session, **kwargs)

    monkeypatch.setattr(AppleWalletService, "earn", record)
    # One hug in each direction, so whichever participant has the larger id
    # is the sender once.
    async with get_session() as session:
        await SibHugService.hug(session, sender_id=alice.id, target_id=bob.id, message_key="-100:94")
    assert order == sorted([alice.id, bob.id])

    order.clear()
    async with get_session() as session:
        await SibHugService.hug(session, sender_id=bob.id, target_id=alice.id, message_key="-100:95")
    assert order == sorted([alice.id, bob.id])


@pytest.mark.asyncio
async def test_cooldown_expires_after_600_seconds(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس انقضا")
    bob = await create_test_user(db_session, "باب انقضا")
    carol = await create_test_user(db_session, "کارول انقضا")
    await db_session.commit()

    async with get_session() as session:
        first = await SibHugService.hug(session, sender_id=alice.id, target_id=bob.id, message_key="-100:100")
    assert first.status == HUG_AWARDED

    # 599 seconds ago: still inside the 600-second window.
    async with get_session() as session:
        await _backdate_sender_hug(session, alice.id, HUG_COOLDOWN_SECONDS - 1)
    async with get_session() as session:
        blocked = await SibHugService.hug(session, sender_id=alice.id, target_id=carol.id, message_key="-100:101")
    assert blocked.status == HUG_COOLDOWN

    # 601 seconds ago: the window has expired, so a new hug succeeds.
    async with get_session() as session:
        await _backdate_sender_hug(session, alice.id, HUG_COOLDOWN_SECONDS + 1)
    async with get_session() as session:
        allowed = await SibHugService.hug(session, sender_id=alice.id, target_id=carol.id, message_key="-100:102")
    assert allowed.status == HUG_AWARDED

    async with get_session() as session:
        assert await _balance(session, alice.id) == HUG_AMOUNT * 2
        assert await _balance(session, carol.id) == HUG_AMOUNT
        assert len(await _ledger(session, reasons=(HUG_REASON,))) == 2


@pytest.mark.asyncio
async def test_pre_existing_half_pair_raises_and_commits_nothing_new(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس نیمه")
    bob = await create_test_user(db_session, "باب نیمه")
    await db_session.commit()

    _sender_key, recipient_key = hug_idempotency_keys("-100:110", alice.id, bob.id)
    # Simulate an incomplete earlier write: only the recipient row exists.
    await AppleWalletService.earn(
        db_session,
        user_id=bob.id,
        amount=HUG_AMOUNT,
        reason=HUG_RECEIVED_REASON,
        ref_type="sib_hug",
        ref_id=alice.id,
        idempotency_key=recipient_key,
    )
    await db_session.commit()

    with pytest.raises(SibHugIntegrityError):
        async with get_session() as session:
            await SibHugService.hug(session, sender_id=alice.id, target_id=bob.id, message_key="-100:110")

    async with get_session() as session:
        rows = await _ledger(session)
        assert [r.reason for r in rows] == [HUG_RECEIVED_REASON]
        assert await _balance(session, alice.id) == 0
        assert await _balance(session, bob.id) == HUG_AMOUNT


@pytest.mark.asyncio
async def test_skipped_recipient_reward_rolls_back_sender_reward(db_session, monkeypatch):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس بی‌نقص")
    bob = await create_test_user(db_session, "باب بی‌نقص")
    await db_session.commit()

    real_earn = AppleWalletService.earn

    async def skip_recipient(session, **kwargs):
        # Simulate earn() silently returning for the recipient's key.
        if kwargs["idempotency_key"].startswith(HUG_RECEIVED_REASON + ":"):
            return 0
        return await real_earn(session, **kwargs)

    monkeypatch.setattr(AppleWalletService, "earn", skip_recipient)
    with pytest.raises(SibHugIntegrityError):
        async with get_session() as session:
            await SibHugService.hug(session, sender_id=alice.id, target_id=bob.id, message_key="-100:120")

    # The sender's reward was written in the same transaction and must be gone.
    monkeypatch.setattr(AppleWalletService, "earn", real_earn)
    async with get_session() as session:
        assert await _ledger(session) == []
        assert await _balance(session, alice.id) == 0
        assert await _balance(session, bob.id) == 0

    # No cooldown was established, so the retry succeeds for both sides.
    async with get_session() as session:
        retry = await SibHugService.hug(session, sender_id=alice.id, target_id=bob.id, message_key="-100:120")
    assert retry.status == HUG_AWARDED
    async with get_session() as session:
        assert await _balance(session, alice.id) == HUG_AMOUNT
        assert await _balance(session, bob.id) == HUG_AMOUNT


@pytest.mark.asyncio
async def test_duplicate_message_processing_awards_nobody_twice(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس تکراری")
    bob = await create_test_user(db_session, "باب تکراری")
    await db_session.commit()

    first = await SibHugService.hug(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:40")
    await db_session.commit()
    second = await SibHugService.hug(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:40")
    await db_session.commit()

    assert first.status == HUG_AWARDED
    assert second.status == HUG_DUPLICATE
    assert await _balance(db_session, alice.id) == HUG_AMOUNT
    assert await _balance(db_session, bob.id) == HUG_AMOUNT
    assert len(await _ledger(db_session)) == 2


@pytest.mark.asyncio
async def test_partial_reward_failure_leaves_no_committed_entries(db_session, monkeypatch):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس اتمیک")
    bob = await create_test_user(db_session, "باب اتمیک")
    await db_session.commit()
    # Capture ids BEFORE the rollback: rollback expires loaded ORM objects, and
    # reading them afterwards is a lazy load outside the greenlet (MissingGreenlet).
    alice_id, bob_id = alice.id, bob.id

    real_earn = AppleWalletService.earn
    calls = {"n": 0}

    async def failing_second_earn(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated recipient reward failure")
        return await real_earn(*args, **kwargs)

    monkeypatch.setattr(AppleWalletService, "earn", failing_second_earn)
    with pytest.raises(RuntimeError):
        await SibHugService.hug(db_session, sender_id=alice_id, target_id=bob_id, message_key="-100:50")
    await db_session.rollback()  # what get_session() does on error

    assert await _ledger(db_session) == []
    assert await _balance(db_session, alice_id) == 0
    assert await _balance(db_session, bob_id) == 0

    # The sender's cooldown was never established, so a retry succeeds.
    monkeypatch.setattr(AppleWalletService, "earn", real_earn)
    retry = await SibHugService.hug(db_session, sender_id=alice_id, target_id=bob_id, message_key="-100:50")
    await db_session.commit()
    assert retry.status == HUG_AWARDED


@pytest.mark.asyncio
async def test_self_hug_and_bot_target_are_refused_without_writes(db_session):
    from sib.tests.test_parties import create_test_user
    from sib.database.models import User

    alice = await create_test_user(db_session, "آلیس خود")
    await db_session.commit()
    alice_id = alice.id  # captured before any rollback (see test_partial_reward...)
    with pytest.raises(SibHugSelfTargetError):
        await SibHugService.hug(db_session, sender_id=alice.id, target_id=alice.id, message_key="-100:60")

    bot = User(id=987654321, first_name="ربات", username="bot_x", is_bot=True)
    db_session.add(bot)
    await db_session.commit()
    with pytest.raises(SibHugInvalidTargetError):
        await SibHugService.hug(db_session, sender_id=alice_id, target_id=bot.id, message_key="-100:61")
    await db_session.rollback()

    assert await _ledger(db_session) == []
    assert await _balance(db_session, alice_id) == 0


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_hugs_cannot_bypass_sender_cooldown():
    from sib.tests.test_parties import create_test_user

    async with get_session() as session:
        alice = await create_test_user(session, "آلیس همزمان")
        bob = await create_test_user(session, "باب همزمان")
        carol = await create_test_user(session, "کارول همزمان")
        await session.commit()
        ids = (alice.id, bob.id, carol.id)

    async def hug_to(target_id, key):
        async with get_session() as session:
            result = await SibHugService.hug(
                session, sender_id=ids[0], target_id=target_id, message_key=key
            )
            return result.status

    statuses = await asyncio.gather(hug_to(ids[1], "-100:70"), hug_to(ids[2], "-100:71"))
    assert sorted(statuses) == sorted([HUG_AWARDED, HUG_COOLDOWN])

    async with get_session() as session:
        assert await _balance(session, ids[0]) == HUG_AMOUNT
        awarded_targets = [ids[1], ids[2]]
        total_recipients = 0
        for uid in awarded_targets:
            total_recipients += await _balance(session, uid)
        assert total_recipients == HUG_AMOUNT
        assert len(await _ledger(session, reasons=(HUG_REASON,))) == 1


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_duplicate_message_awards_each_side_once():
    from sib.tests.test_parties import create_test_user

    async with get_session() as session:
        alice = await create_test_user(session, "آلیس دوبل")
        bob = await create_test_user(session, "باب دوبل")
        await session.commit()
        ids = (alice.id, bob.id)

    async def process():
        async with get_session() as session:
            result = await SibHugService.hug(
                session, sender_id=ids[0], target_id=ids[1], message_key="-100:80"
            )
            return result.status

    statuses = await asyncio.gather(process(), process())
    assert sorted(statuses) == sorted([HUG_AWARDED, HUG_DUPLICATE])

    async with get_session() as session:
        assert await _balance(session, ids[0]) == HUG_AMOUNT
        assert await _balance(session, ids[1]) == HUG_AMOUNT
        assert len(await _ledger(session)) == 2


# ---------------------------------------------------------------------------
# Telegram handler (mocked messages, real DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_success_mentions_both_and_uses_text_fallback(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_hug import handle_sib_hug

    # No hug artwork yet: point the scene directory at an empty folder.
    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(810001, "آلیس*_[")
    target = _tg_user(810002, "باب")

    msg = _group_hug_message(sender, "بغل سیبی", target=target)
    await handle_sib_hug(msg)

    assert not msg.reply_photo.called
    assert msg.reply.called
    text = msg.reply.call_args[0][0]
    assert "tg://user?id=810001" in text
    assert "tg://user?id=810002" in text
    assert "۱۰" in text
    # Name is Markdown-escaped, so the bot's Markdown stays valid.
    assert "آلیس\\*\\_\\[" in text
    assert msg.reply.call_args.kwargs.get("parse_mode") == "Markdown"

    async with get_session() as session:
        assert await _balance(session, 810001) == HUG_AMOUNT
        assert await _balance(session, 810002) == HUG_AMOUNT


@pytest.mark.asyncio
async def test_handler_second_hug_within_cooldown_replies_wait_without_reward(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_hug import handle_sib_hug

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(820001, "فرستنده")
    first_target = _tg_user(820002, "یک")
    second_target = _tg_user(820003, "دو")

    await handle_sib_hug(_group_hug_message(sender, "بغل سیبی", target=first_target, message_id=1))
    # Make the first hug exactly 120 seconds old: 600 - 120 = 480 = 8 minutes.
    async with get_session() as session:
        await _backdate_sender_hug(session, 820001, 120)

    msg2 = _group_hug_message(sender, "بغل سیبی", target=second_target, message_id=2)
    await handle_sib_hug(msg2)

    reply2 = msg2.reply.call_args[0][0]
    assert reply2 == (
        "🤗 هنوز زوده! ۸ دقیقه دیگه می\u200cتونی دوباره بغل سیبی بدی. 🍎"
    )
    async with get_session() as session:
        assert await _balance(session, 820003) == 0
        assert await _balance(session, 820001) == HUG_AMOUNT


@pytest.mark.asyncio
async def test_handler_refusals_create_no_rewards_and_no_cooldown(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_hug import (
        HUG_REPLY_REQUIRED_TEXT,
        HUG_SELF_TEXT,
        HUG_TARGET_IS_BOT_TEXT,
        HUG_TARGET_NOT_A_USER_TEXT,
        HUG_PRIVATE_CHAT_TEXT,
        handle_sib_hug,
    )

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(830001, "کاربر ریپلای")
    bot_target = _tg_user(830002, "ربات", is_bot=True)
    human_target = _tg_user(830003, "هدف")

    # Private chat
    msg = _group_hug_message(sender, "بغل سیبی", target=human_target, chat_type="private")
    await handle_sib_hug(msg)
    assert msg.reply.call_args[0][0] == HUG_PRIVATE_CHAT_TEXT

    # Missing reply
    msg = _group_hug_message(sender, "بغل سیبی", target=None)
    await handle_sib_hug(msg)
    assert msg.reply.call_args[0][0] == HUG_REPLY_REQUIRED_TEXT

    # Reply without a user identity (channel post / anonymous admin)
    msg = _group_hug_message(sender, "بغل سیبی", target=None)
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.from_user = None
    await handle_sib_hug(msg)
    assert msg.reply.call_args[0][0] == HUG_TARGET_NOT_A_USER_TEXT

    # Bot target
    msg = _group_hug_message(sender, "بغل سیبی", target=bot_target)
    await handle_sib_hug(msg)
    assert msg.reply.call_args[0][0] == HUG_TARGET_IS_BOT_TEXT

    # Self target: explicitly says the user cannot hug themselves.
    msg = _group_hug_message(sender, "بغل سیبی", target=sender)
    await handle_sib_hug(msg)
    reply = msg.reply.call_args[0][0]
    assert reply == HUG_SELF_TEXT
    assert "خودت" in reply

    async with get_session() as session:
        assert await _ledger(session) == []
        assert await _balance(session, 830001) == 0
        assert await _balance(session, 830003) == 0

    # The refusals consumed no cooldown: a valid hug right after succeeds.
    msg = _group_hug_message(sender, "بغل سیبی", target=human_target, message_id=99)
    await handle_sib_hug(msg)
    assert "tg://user?id=830003" in msg.reply.call_args[0][0]
    async with get_session() as session:
        assert await _balance(session, 830001) == HUG_AMOUNT


@pytest.mark.asyncio
async def test_handler_duplicate_delivery_is_silent_and_pays_once(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_hug import handle_sib_hug

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(840001, "تکرار")
    target = _tg_user(840002, "هدف تکرار")

    msg = _group_hug_message(sender, "بغل سیبی", target=target, message_id=444)
    await handle_sib_hug(msg)
    again = _group_hug_message(sender, "بغل سیبی", target=target, message_id=444)
    await handle_sib_hug(again)

    assert msg.reply.called
    assert not again.reply.called  # redelivery of the same message: no second reply
    async with get_session() as session:
        assert await _balance(session, 840001) == HUG_AMOUNT
        assert await _balance(session, 840002) == HUG_AMOUNT


@pytest.mark.asyncio
async def test_hug_scene_present_is_sent_as_photo_with_caption(monkeypatch, tmp_path):
    """Phase 4 contract: dropping sib_hug/hug.jpg switches the reply to photo."""
    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    (tmp_path / "sib_hug").mkdir()
    (tmp_path / "sib_hug" / "hug.jpg").write_bytes(b"\xff\xd8\xff")
    assert marriage_art.scene_path("hug") == tmp_path / "sib_hug" / "hug.jpg"

    msg = MagicMock()
    msg.reply = AsyncMock(return_value=MagicMock())
    msg.reply_photo = AsyncMock(return_value=MagicMock(photo=None))
    await marriage_art.send_scene_reply(msg, "hug", "متن بغل")

    assert msg.reply_photo.called
    assert msg.reply_photo.call_args.kwargs.get("caption") == "متن بغل"
    assert not msg.reply.called


@pytest.mark.asyncio
async def test_hug_scene_missing_falls_back_to_text(monkeypatch, tmp_path):
    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    assert marriage_art.scene_path("hug") is None

    msg = MagicMock()
    msg.reply = AsyncMock(return_value=MagicMock())
    msg.reply_photo = AsyncMock()
    await marriage_art.send_scene_reply(msg, "hug", "متن بغل")

    assert not msg.reply_photo.called
    msg.reply.assert_awaited_once()
    assert msg.reply.call_args[0][0] == "متن بغل"


# ---------------------------------------------------------------------------
# Committed artwork (Phase 4): real repo files, no monkeypatching
# ---------------------------------------------------------------------------


def _jpeg_dimensions(path):
    """(width, height) from the JPEG frame header, stdlib only."""
    data = path.read_bytes()
    assert data[:3] == b"\xff\xd8\xff", "not a JPEG"
    assert data[-2:] == b"\xff\xd9", "JPEG end-of-image marker missing"
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise AssertionError("no JPEG frame header found")


def test_committed_hug_artwork_resolves_and_is_a_valid_jpeg():
    expected = Path(marriage_art.__file__).resolve().parent.parent / "assets" / "sib_hug" / "hug.jpg"
    assert marriage_art.scene_path("hug") == expected
    assert expected.is_file()
    assert _jpeg_dimensions(expected) == (1024, 1024)
    assert 0 < expected.stat().st_size <= 5 * 1024 * 1024
