"""Sib Kiss tests: «بوس سیبی» as a reply-based two-user reward.

Mirrors tests/test_sib_hug.py and adds cross-feature independence checks
(Hug, ordinary claim) and a kiss-specific scene key.
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
from sib.services.sib_claim import SibClaimService
from sib.services.sib_hug import HUG_AWARDED as HUG_AWARDED_STATUS
from sib.services.sib_hug import HUG_COOLDOWN as HUG_COOLDOWN_STATUS
from sib.services.sib_hug import SibHugService
from sib.services.sib_kiss import (
    KISS_AWARDED,
    KISS_COOLDOWN,
    KISS_DUPLICATE,
    KISS_MESSAGE_VARIANTS,
    KISS_RECEIVED_REASON,
    KISS_REASON,
    SibKissIntegrityError,
    SibKissInvalidTargetError,
    SibKissSelfTargetError,
    SibKissService,
    format_kiss_cooldown_message,
    kiss_idempotency_keys,
    pick_kiss_message,
)
from sib.tests.conftest import requires_row_locks

KISS_AMOUNT = 10
KISS_COOLDOWN_SECONDS = 600


def _tg_user(uid: int, name: str, is_bot: bool = False) -> TGUser:
    return TGUser(id=uid, is_bot=is_bot, first_name=name, username=f"u{uid}")


def _group_kiss_message(sender: TGUser, text: str, *, target=None, message_id=900, chat_type="supergroup"):
    msg = MagicMock()
    msg.chat = MagicMock()
    msg.chat.type = chat_type
    msg.chat.id = -6200
    msg.chat.title = "گروه بوس سیبی"
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


async def _kiss_ledger(session):
    return list(
        (
            await session.scalars(
                select(AppleTransaction)
                .where(AppleTransaction.reason.in_((KISS_REASON, KISS_RECEIVED_REASON)))
                .order_by(AppleTransaction.id)
            )
        ).all()
    )


async def _balance(session, user_id: int) -> int:
    return await AppleWalletService.get_balance(session, user_id)


async def _backdate_sender_kiss(session, user_id: int, seconds_ago: int) -> None:
    """Move the sender's sib_kiss ledger row into the past, DB-clock based."""
    db_now = await session.scalar(select(func.now()))
    row = await session.scalar(
        select(AppleTransaction)
        .where(AppleTransaction.user_id == user_id)
        .where(AppleTransaction.reason == KISS_REASON)
    )
    assert row is not None, "expected a sib_kiss row to backdate"
    row.created_at = db_now - timedelta(seconds=seconds_ago)
    await session.commit()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_at_least_ten_distinct_kiss_variants():
    assert len(KISS_MESSAGE_VARIANTS) >= 10
    assert len(set(KISS_MESSAGE_VARIANTS)) == len(KISS_MESSAGE_VARIANTS)
    for template in KISS_MESSAGE_VARIANTS:
        assert "**{a}**" in template and "**{b}**" in template
        assert "**{n} سیب**" in template
        assert sum(template.count(mark) for mark in ".!؟") <= 2
        assert "..." not in template and "—" not in template


def test_pick_kiss_message_mentions_both_users_and_amount():
    text = pick_kiss_message("[آلیس](tg://user?id=1)", "[باب](tg://user?id=2)", 10)
    assert "**[آلیس](tg://user?id=1)**" in text
    assert "**[باب](tg://user?id=2)**" in text
    assert "**۱۰ سیب**" in text
    assert "10" not in text


def test_pick_kiss_message_varies_across_variants():
    rng = random.Random(11)
    seen = {pick_kiss_message("A", "B", 10, rng=rng) for _ in range(200)}
    assert len(seen) >= 8


def test_kiss_cooldown_message_exact_text():
    prefix = "😘 فقط **"
    suffix = "** صبر کن؛ بعدش می‌تونی دوباره بوس سیبی بدی!"
    assert format_kiss_cooldown_message(600) == prefix + "۱۰ دقیقه" + suffix
    assert format_kiss_cooldown_message(42) == prefix + "۴۲ ثانیه" + suffix
    assert format_kiss_cooldown_message(157) == prefix + "۲ دقیقه و ۳۷ ثانیه" + suffix
    assert format_kiss_cooldown_message(0.2) == prefix + "۱ ثانیه" + suffix


def test_kiss_idempotency_keys_are_exact_distinct_and_not_hug_keys():
    sender_key, recipient_key = kiss_idempotency_keys("-100:5", 1, 2)
    assert sender_key == "sib_kiss:-100:5:1"
    assert recipient_key == "sib_kiss_received:-100:5:2"
    assert sender_key != recipient_key
    # Same Telegram message, different action: keys must not collide with Hug.
    from sib.services.sib_hug import hug_idempotency_keys

    assert set(kiss_idempotency_keys("-100:5", 1, 2)).isdisjoint(hug_idempotency_keys("-100:5", 1, 2))


@pytest.mark.parametrize("text", ["بوس سیبی", "  بوس سیبی  ", "بوس سیبی؟", "بوس سیبی?"])
def test_kiss_trigger_matches_exact_phrase_variants(text):
    from sib.handlers.sib_kiss import KISS_TRIGGER_PATTERN

    assert re.match(KISS_TRIGGER_PATTERN, text)


@pytest.mark.parametrize("text", ["بوس", "سیب", "بوس سیبی کن", "بغل سیبی", "بوس کردن", "بوس سیب"])
def test_kiss_trigger_ignores_unrelated_text(text):
    from sib.handlers.sib_kiss import KISS_TRIGGER_PATTERN

    assert not re.match(KISS_TRIGGER_PATTERN, text)


def test_kiss_scene_is_group_scene_and_marriage_contract_is_unchanged():
    assert "kiss" in marriage_art.GROUP_SCENES
    assert marriage_art.GROUP_SCENES["kiss"] == "sib_kiss/kiss.jpg"
    assert "kiss" not in marriage_art.SCENES
    assert set(marriage_art.SCENES) == {"proposal", "wedding", "divorce", "flower_gift"}


# ---------------------------------------------------------------------------
# Domain service (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_kiss_rewards_both_with_exact_reasons_and_keys(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس بوس")
    bob = await create_test_user(db_session, "باب بوس")
    await db_session.commit()

    result = await SibKissService.kiss(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:1")
    await db_session.commit()

    assert result.status == KISS_AWARDED
    assert result.amount == KISS_AMOUNT
    assert await _balance(db_session, alice.id) == KISS_AMOUNT
    assert await _balance(db_session, bob.id) == KISS_AMOUNT

    rows = await _kiss_ledger(db_session)
    assert len(rows) == 2
    sender_row = next(r for r in rows if r.reason == KISS_REASON)
    recipient_row = next(r for r in rows if r.reason == KISS_RECEIVED_REASON)
    assert (sender_row.user_id, sender_row.amount, sender_row.ref_id) == (alice.id, KISS_AMOUNT, bob.id)
    assert (recipient_row.user_id, recipient_row.amount, recipient_row.ref_id) == (bob.id, KISS_AMOUNT, alice.id)
    assert sender_row.idempotency_key == f"sib_kiss:-100:1:{alice.id}"
    assert recipient_row.idempotency_key == f"sib_kiss_received:-100:1:{bob.id}"


@pytest.mark.asyncio
async def test_second_kiss_within_cooldown_is_rejected_without_writes(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس کولدان بوس")
    bob = await create_test_user(db_session, "باب کولدان بوس")
    carol = await create_test_user(db_session, "کارول کولدان بوس")
    await db_session.commit()

    await SibKissService.kiss(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:10")
    await db_session.commit()
    before = len(await _kiss_ledger(db_session))

    result = await SibKissService.kiss(db_session, sender_id=alice.id, target_id=carol.id, message_key="-100:11")
    await db_session.commit()

    assert result.status == KISS_COOLDOWN
    assert result.remaining_seconds > 0
    assert len(await _kiss_ledger(db_session)) == before
    assert await _balance(db_session, carol.id) == 0


@pytest.mark.asyncio
async def test_kiss_cooldown_expires_after_600_seconds(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس انقضای بوس")
    bob = await create_test_user(db_session, "باب انقضای بوس")
    carol = await create_test_user(db_session, "کارول انقضای بوس")
    await db_session.commit()

    async with get_session() as session:
        assert (await SibKissService.kiss(session, sender_id=alice.id, target_id=bob.id, message_key="-100:20")).status == KISS_AWARDED

    async with get_session() as session:  # 599 s ago: still inside the window
        await _backdate_sender_kiss(session, alice.id, KISS_COOLDOWN_SECONDS - 1)
    async with get_session() as session:
        blocked = await SibKissService.kiss(session, sender_id=alice.id, target_id=carol.id, message_key="-100:21")
    assert blocked.status == KISS_COOLDOWN

    async with get_session() as session:  # 601 s ago: expired
        await _backdate_sender_kiss(session, alice.id, KISS_COOLDOWN_SECONDS + 1)
    async with get_session() as session:
        allowed = await SibKissService.kiss(session, sender_id=alice.id, target_id=carol.id, message_key="-100:22")
    assert allowed.status == KISS_AWARDED

    async with get_session() as session:
        assert await _balance(session, alice.id) == KISS_AMOUNT * 2
        assert await _balance(session, carol.id) == KISS_AMOUNT
        assert len(await _kiss_ledger(session)) == 4


@pytest.mark.asyncio
async def test_receiving_a_kiss_does_not_consume_recipient_kiss_cooldown(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس دریافت بوس")
    bob = await create_test_user(db_session, "باب دریافت بوس")
    carol = await create_test_user(db_session, "کارول دریافت بوس")
    await db_session.commit()

    assert (await SibKissService.kiss(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:30")).status == KISS_AWARDED
    await db_session.commit()

    second = await SibKissService.kiss(db_session, sender_id=bob.id, target_id=carol.id, message_key="-100:31")
    await db_session.commit()
    assert second.status == KISS_AWARDED
    assert await _balance(db_session, bob.id) == KISS_AMOUNT * 2


@pytest.mark.asyncio
async def test_kiss_cooldown_is_independent_of_hug_and_claim(db_session):
    """Each action is exercised by a sender who has not yet used that action,
    so every assertion isolates exactly one cooldown."""
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس مستقل بوس")
    bob = await create_test_user(db_session, "باب مستقل بوس")
    carol = await create_test_user(db_session, "کارول مستقل بوس")
    await db_session.commit()

    # 1) Alice hugs (starts ALICE'S HUG cooldown only).
    hug = await SibHugService.hug(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:40")
    await db_session.commit()
    assert hug.status == HUG_AWARDED_STATUS

    # 2) Alice kisses: her hug cooldown must not block her kiss.
    kiss = await SibKissService.kiss(db_session, sender_id=alice.id, target_id=carol.id, message_key="-100:41")
    await db_session.commit()
    assert kiss.status == KISS_AWARDED

    # 3) Carol has been kissed but never kissed or hugged: receiving a kiss
    #    must not block her hug.
    carol_hug = await SibHugService.hug(db_session, sender_id=carol.id, target_id=bob.id, message_key="-100:42")
    await db_session.commit()
    assert carol_hug.status == HUG_AWARDED_STATUS

    # 4) Alice's hug cooldown is still enforced (from step 1, not the kiss).
    alice_hug_again = await SibHugService.hug(db_session, sender_id=alice.id, target_id=carol.id, message_key="-100:43")
    await db_session.commit()
    assert alice_hug_again.status == HUG_COOLDOWN_STATUS

    # 5) Ordinary claim is independent of kiss for the same user.
    _, claimed = await SibClaimService.claim(db_session, alice.id)
    await db_session.commit()
    assert claimed is True

    # 6) Bob has been hugged and claimed nothing yet: neither blocks his kiss.
    bob_kiss = await SibKissService.kiss(db_session, sender_id=bob.id, target_id=alice.id, message_key="-100:44")
    await db_session.commit()
    assert bob_kiss.status == KISS_AWARDED

    # 7) Alice's KISS cooldown (from step 2) is enforced.
    alice_kiss_again = await SibKissService.kiss(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:45")
    assert alice_kiss_again.status == KISS_COOLDOWN


@pytest.mark.asyncio
async def test_duplicate_kiss_message_pays_nobody_twice(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس تکرار بوس")
    bob = await create_test_user(db_session, "باب تکرار بوس")
    await db_session.commit()

    first = await SibKissService.kiss(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:50")
    await db_session.commit()
    second = await SibKissService.kiss(db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:50")
    await db_session.commit()

    assert first.status == KISS_AWARDED
    assert second.status == KISS_DUPLICATE
    assert await _balance(db_session, alice.id) == KISS_AMOUNT
    assert await _balance(db_session, bob.id) == KISS_AMOUNT
    assert len(await _kiss_ledger(db_session)) == 2


@pytest.mark.asyncio
async def test_self_kiss_and_bot_target_are_refused_without_writes(db_session):
    from sib.database.models import User
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس خود بوس")
    await db_session.commit()
    alice_id = alice.id  # captured before the rollback below (MissingGreenlet otherwise)
    with pytest.raises(SibKissSelfTargetError):
        await SibKissService.kiss(db_session, sender_id=alice.id, target_id=alice.id, message_key="-100:60")

    bot = User(id=987654322, first_name="ربات", username="bot_kiss", is_bot=True)
    db_session.add(bot)
    await db_session.commit()
    with pytest.raises(SibKissInvalidTargetError):
        await SibKissService.kiss(db_session, sender_id=alice_id, target_id=bot.id, message_key="-100:61")
    await db_session.rollback()

    assert await _kiss_ledger(db_session) == []
    assert await _balance(db_session, alice_id) == 0


@pytest.mark.asyncio
async def test_pre_existing_half_pair_raises_and_commits_nothing_new(db_session):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس نیمه بوس")
    bob = await create_test_user(db_session, "باب نیمه بوس")
    await db_session.commit()

    _sender_key, recipient_key = kiss_idempotency_keys("-100:70", alice.id, bob.id)
    await AppleWalletService.earn(
        db_session,
        user_id=bob.id,
        amount=KISS_AMOUNT,
        reason=KISS_RECEIVED_REASON,
        ref_type="sib_kiss",
        ref_id=alice.id,
        idempotency_key=recipient_key,
    )
    await db_session.commit()

    with pytest.raises(SibKissIntegrityError):
        async with get_session() as session:
            await SibKissService.kiss(session, sender_id=alice.id, target_id=bob.id, message_key="-100:70")

    async with get_session() as session:
        rows = await _kiss_ledger(session)
        assert [r.reason for r in rows] == [KISS_RECEIVED_REASON]
        assert await _balance(session, alice.id) == 0
        assert await _balance(session, bob.id) == KISS_AMOUNT


@pytest.mark.asyncio
async def test_skipped_recipient_reward_rolls_back_sender_reward(db_session, monkeypatch):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس اتمیک بوس")
    bob = await create_test_user(db_session, "باب اتمیک بوس")
    await db_session.commit()

    real_earn = AppleWalletService.earn

    async def skip_recipient(session, **kwargs):
        if kwargs["idempotency_key"].startswith(KISS_RECEIVED_REASON + ":"):
            return 0
        return await real_earn(session, **kwargs)

    monkeypatch.setattr(AppleWalletService, "earn", skip_recipient)
    with pytest.raises(SibKissIntegrityError):
        async with get_session() as session:
            await SibKissService.kiss(session, sender_id=alice.id, target_id=bob.id, message_key="-100:80")

    monkeypatch.setattr(AppleWalletService, "earn", real_earn)
    async with get_session() as session:
        assert await _kiss_ledger(session) == []
        assert await _balance(session, alice.id) == 0
        assert await _balance(session, bob.id) == 0

    # No cooldown was established, so the retry succeeds for both sides.
    async with get_session() as session:
        retry = await SibKissService.kiss(session, sender_id=alice.id, target_id=bob.id, message_key="-100:80")
    assert retry.status == KISS_AWARDED


@pytest.mark.asyncio
async def test_kiss_rewards_execute_in_ascending_user_id_order(db_session, monkeypatch):
    from sib.tests.test_parties import create_test_user

    alice = await create_test_user(db_session, "آلیس ترتیب بوس")
    bob = await create_test_user(db_session, "باب ترتیب بوس")
    await db_session.commit()

    order = []
    real_earn = AppleWalletService.earn

    async def record(session, **kwargs):
        order.append(kwargs["user_id"])
        return await real_earn(session, **kwargs)

    monkeypatch.setattr(AppleWalletService, "earn", record)
    # One kiss in each direction, so the larger id is the sender once.
    async with get_session() as session:
        await SibKissService.kiss(session, sender_id=alice.id, target_id=bob.id, message_key="-100:90")
    assert order == sorted([alice.id, bob.id])

    order.clear()
    async with get_session() as session:
        await SibKissService.kiss(session, sender_id=bob.id, target_id=alice.id, message_key="-100:91")
    assert order == sorted([alice.id, bob.id])


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_kisses_cannot_bypass_sender_cooldown():
    from sib.tests.test_parties import create_test_user

    async with get_session() as session:
        alice = await create_test_user(session, "آلیس همزمان بوس")
        bob = await create_test_user(session, "باب همزمان بوس")
        carol = await create_test_user(session, "کارول همزمان بوس")
        await session.commit()
        alice_id, bob_id, carol_id = alice.id, bob.id, carol.id

    async def kiss_to(target_id, key):
        async with get_session() as session:
            return (await SibKissService.kiss(session, sender_id=alice_id, target_id=target_id, message_key=key)).status

    statuses = await asyncio.gather(kiss_to(bob_id, "-100:100"), kiss_to(carol_id, "-100:101"))
    assert sorted(statuses) == sorted([KISS_AWARDED, KISS_COOLDOWN])

    async with get_session() as session:
        assert await _balance(session, alice_id) == KISS_AMOUNT
        assert await _balance(session, bob_id) + await _balance(session, carol_id) == KISS_AMOUNT
        assert len(await _kiss_ledger(session)) == 2


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_duplicate_kiss_awards_each_side_once():
    from sib.tests.test_parties import create_test_user

    async with get_session() as session:
        alice = await create_test_user(session, "آلیس دوبل بوس")
        bob = await create_test_user(session, "باب دوبل بوس")
        await session.commit()
        alice_id, bob_id = alice.id, bob.id

    async def process():
        async with get_session() as session:
            return (await SibKissService.kiss(session, sender_id=alice_id, target_id=bob_id, message_key="-100:110")).status

    statuses = await asyncio.gather(process(), process())
    assert sorted(statuses) == sorted([KISS_AWARDED, KISS_DUPLICATE])

    async with get_session() as session:
        assert await _balance(session, alice_id) == KISS_AMOUNT
        assert await _balance(session, bob_id) == KISS_AMOUNT
        assert len(await _kiss_ledger(session)) == 2


# ---------------------------------------------------------------------------
# Telegram handler (mocked messages, real DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_success_mentions_both_and_uses_text_fallback(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_kiss import handle_sib_kiss

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)  # no kiss art yet
    sender = _tg_user(910001, "آلیس*_[")
    target = _tg_user(910002, "باب")

    msg = _group_kiss_message(sender, "بوس سیبی", target=target)
    await handle_sib_kiss(msg)

    assert not msg.reply_photo.called
    assert msg.reply.called
    text = msg.reply.call_args[0][0]
    assert "tg://user?id=910001" in text
    assert "tg://user?id=910002" in text
    assert "آلیس\\*\\_\\[" in text  # Markdown-escaped display name
    assert "۱۰" in text
    assert msg.reply.call_args.kwargs.get("parse_mode") == "Markdown"

    async with get_session() as session:
        assert await _balance(session, 910001) == KISS_AMOUNT
        assert await _balance(session, 910002) == KISS_AMOUNT


@pytest.mark.asyncio
async def test_kiss_mentions_are_clickable_and_markdown_escaped(db_session, monkeypatch, tmp_path):
    """P4: kiss mentions are clickable tg://user links whose names are escaped.

    Group context: everyone in the group already sees each other's profiles,
    so the clickable name links are kept. The display names still go through
    ``safe_name`` so Markdown characters cannot break the message.
    """
    from sib.handlers.sib_kiss import handle_sib_kiss

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)  # no kiss art yet
    sender = _tg_user(911001, "علی_رضا*")
    target = _tg_user(911002, "باب")

    msg = _group_kiss_message(sender, "بوس سیبی", target=target)
    await handle_sib_kiss(msg)

    text = msg.reply.call_args[0][0]
    assert "[" in text
    assert "](tg://user?id=911001)" in text
    assert "](tg://user?id=911002)" in text
    assert "علی\\_رضا\\*" in text  # name is Markdown-escaped
    assert "باب" in text
    assert msg.reply.call_args.kwargs.get("parse_mode") == "Markdown"


@pytest.mark.asyncio
async def test_handler_second_kiss_within_cooldown_replies_exact_wait(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_kiss import handle_sib_kiss

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(920001, "فرستنده بوس")
    first_target = _tg_user(920002, "یک")
    second_target = _tg_user(920003, "دو")

    await handle_sib_kiss(_group_kiss_message(sender, "بوس سیبی", target=first_target, message_id=1))
    async with get_session() as session:
        await _backdate_sender_kiss(session, 920001, 120)  # 600 - 120 = 480 s = 8 min

    msg2 = _group_kiss_message(sender, "بوس سیبی", target=second_target, message_id=2)
    await handle_sib_kiss(msg2)

    cooldown_text = msg2.reply.call_args[0][0]
    assert cooldown_text.startswith("😘 فقط **")
    assert "دقیقه" in cooldown_text
    assert cooldown_text.endswith("** صبر کن؛ بعدش می‌تونی دوباره بوس سیبی بدی!")
    assert msg2.reply.call_args.kwargs["parse_mode"] == "Markdown"
    async with get_session() as session:
        assert await _balance(session, 920003) == 0
        assert await _balance(session, 920001) == KISS_AMOUNT


@pytest.mark.asyncio
async def test_handler_refusals_create_no_rewards_and_no_cooldown(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_kiss import (
        KISS_PRIVATE_CHAT_TEXT,
        KISS_REPLY_REQUIRED_TEXT,
        KISS_SELF_TEXT,
        KISS_TARGET_IS_BOT_TEXT,
        KISS_TARGET_NOT_A_USER_TEXT,
        handle_sib_kiss,
    )

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(930001, "کاربر ریپلای بوس")
    bot_target = _tg_user(930002, "ربات", is_bot=True)
    human_target = _tg_user(930003, "هدف بوس")

    msg = _group_kiss_message(sender, "بوس سیبی", target=human_target, chat_type="private")
    await handle_sib_kiss(msg)
    assert msg.reply.call_args[0][0] == KISS_PRIVATE_CHAT_TEXT

    msg = _group_kiss_message(sender, "بوس سیبی", target=None)
    await handle_sib_kiss(msg)
    assert msg.reply.call_args[0][0] == KISS_REPLY_REQUIRED_TEXT

    msg = _group_kiss_message(sender, "بوس سیبی", target=None)
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.from_user = None
    await handle_sib_kiss(msg)
    assert msg.reply.call_args[0][0] == KISS_TARGET_NOT_A_USER_TEXT

    msg = _group_kiss_message(sender, "بوس سیبی", target=bot_target)
    await handle_sib_kiss(msg)
    assert msg.reply.call_args[0][0] == KISS_TARGET_IS_BOT_TEXT

    msg = _group_kiss_message(sender, "بوس سیبی", target=sender)
    await handle_sib_kiss(msg)
    reply = msg.reply.call_args[0][0]
    assert reply == KISS_SELF_TEXT
    assert "خودت" in reply

    async with get_session() as session:
        assert await _kiss_ledger(session) == []
        assert await _balance(session, 930001) == 0
        assert await _balance(session, 930003) == 0

    # Refusals consumed no cooldown: a valid kiss right after succeeds.
    msg = _group_kiss_message(sender, "بوس سیبی", target=human_target, message_id=99)
    await handle_sib_kiss(msg)
    assert "tg://user?id=930003" in msg.reply.call_args[0][0]
    async with get_session() as session:
        assert await _balance(session, 930001) == KISS_AMOUNT


@pytest.mark.asyncio
async def test_handler_duplicate_delivery_is_silent_and_pays_once(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_kiss import handle_sib_kiss

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(940001, "تکرار بوس")
    target = _tg_user(940002, "هدف تکرار بوس")

    msg = _group_kiss_message(sender, "بوس سیبی", target=target, message_id=444)
    await handle_sib_kiss(msg)
    again = _group_kiss_message(sender, "بوس سیبی", target=target, message_id=444)
    await handle_sib_kiss(again)

    assert msg.reply.called
    assert not again.reply.called
    async with get_session() as session:
        assert await _balance(session, 940001) == KISS_AMOUNT
        assert await _balance(session, 940002) == KISS_AMOUNT


@pytest.mark.asyncio
async def test_kiss_scene_missing_falls_back_to_text(monkeypatch, tmp_path):
    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    assert marriage_art.scene_path("kiss") is None

    msg = MagicMock()
    msg.reply = AsyncMock(return_value=MagicMock())
    msg.reply_photo = AsyncMock()
    await marriage_art.send_scene_reply(msg, "kiss", "متن بوس")

    assert not msg.reply_photo.called
    msg.reply.assert_awaited_once()
    assert msg.reply.call_args[0][0] == "متن بوس"


@pytest.mark.asyncio
async def test_kiss_scene_present_is_sent_as_photo(monkeypatch, tmp_path):
    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    (tmp_path / "sib_kiss").mkdir()
    (tmp_path / "sib_kiss" / "kiss.jpg").write_bytes(b"\xff\xd8\xff")
    assert marriage_art.scene_path("kiss") == tmp_path / "sib_kiss" / "kiss.jpg"

    msg = MagicMock()
    msg.reply = AsyncMock(return_value=MagicMock())
    msg.reply_photo = AsyncMock(return_value=MagicMock(photo=None))
    await marriage_art.send_scene_reply(msg, "kiss", "متن بوس")

    assert msg.reply_photo.called
    assert msg.reply_photo.call_args.kwargs.get("caption") == "متن بوس"
    assert not msg.reply.called


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


def test_committed_kiss_artwork_resolves_and_is_a_valid_jpeg():
    expected = Path(marriage_art.__file__).resolve().parent.parent / "assets" / "sib_kiss" / "kiss.jpg"
    assert marriage_art.scene_path("kiss") == expected
    assert expected.is_file()
    assert _jpeg_dimensions(expected) == (1024, 1024)
    assert 0 < expected.stat().st_size <= 5 * 1024 * 1024


def test_hug_and_kiss_resolve_to_distinct_artwork():
    hug_path = marriage_art.scene_path("hug")
    kiss_path = marriage_art.scene_path("kiss")
    assert hug_path is not None and kiss_path is not None
    assert hug_path != kiss_path
    assert hug_path.read_bytes() != kiss_path.read_bytes()
