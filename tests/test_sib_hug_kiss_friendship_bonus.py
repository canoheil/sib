"""SOC1 — Friendship Bonus for «بغل سیبی» (Sib Hug) and «بوس سیبی» (Sib Kiss).

Two users who already share an ACCEPTED ``apple_friendships`` row receive the
Friendship Bonus: the base amount plus ``sib_*_friend_bonus`` for BOTH of
them (10 + 10 = 20 each by default). Everyone else keeps 10.

These tests pin down the four properties the feature must not break:

1. friends pay 20 each, on both sides;
2. non-friends still pay 10 each;
3. the bonus never weakens the cooldown (no farming by spamming a friend);
4. the Persian narrative always prints the amount the ledger really paid.

The cooldown DURATION itself is untouched: these tests never reconfigure it.
"""

import random
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from sib.database.connection import get_session
from sib.database.models import AppleFriendship, FriendshipStatus
from sib.services import marriage_art
from sib.services.apple_wallet import AppleWalletService
from sib.services.friendship import FriendshipService
from sib.services.sib_claim import fa_digits
from sib.services.sib_hug import (
    HUG_AWARDED,
    HUG_AMOUNT_KEY,
    HUG_COOLDOWN,
    HUG_DUPLICATE,
    HUG_FRIEND_BONUS_KEY,
    HUG_FRIEND_MESSAGE_VARIANTS,
    HUG_MESSAGE_VARIANTS,
    HUG_RECEIVED_REASON,
    HUG_REASON,
    SibHugService,
    pick_hug_message,
)
from sib.services.sib_kiss import (
    KISS_AWARDED,
    KISS_AMOUNT_KEY,
    KISS_COOLDOWN,
    KISS_FRIEND_BONUS_KEY,
    KISS_FRIEND_MESSAGE_VARIANTS,
    KISS_MESSAGE_VARIANTS,
    KISS_RECEIVED_REASON,
    KISS_REASON,
    SibKissService,
    pick_kiss_message,
)

BASE_AMOUNT = 10
FRIEND_BONUS = 10
FRIEND_AMOUNT = BASE_AMOUNT + FRIEND_BONUS  # 20


async def _make_user(session, label: str):
    """A real User row, committed so the service sees it."""
    from sib.tests.test_parties import create_test_user

    return await create_test_user(session, label)


def _make_friends(session, a, b) -> AppleFriendship:
    """Direct row insert: an ACCEPTED friendship in the (a -> b) direction."""
    friendship = AppleFriendship(
        user_id=a.id, friend_id=b.id, status=FriendshipStatus.ACCEPTED
    )
    return friendship


async def _befriend(session, a, b) -> None:
    """Commit an ACCEPTED friendship between the two users."""
    session.add(_make_friends(session, a, b))
    await session.commit()


async def _balance(session, user_id: int) -> int:
    return await AppleWalletService.get_balance(session, user_id)


async def _reward_rows(session, sender_reason: str, received_reason: str):
    from sib.database.models import AppleTransaction

    rows = (
        await session.scalars(
            select(AppleTransaction).where(
                AppleTransaction.reason.in_([sender_reason, received_reason])
            )
        )
    ).all()
    return list(rows)


# ---------------------------------------------------------------------------
# TASK 4 — the four required tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hug_gives_20_if_friends(db_session):
    """Accepted friends: BOTH wallets go up by 20 (10 base + 10 bonus)."""
    alice = await _make_user(db_session, "آلیس دوست")
    bob = await _make_user(db_session, "باب دوست")
    await _befriend(db_session, alice, bob)

    result = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:1"
    )
    await db_session.commit()

    assert result.status == HUG_AWARDED
    assert result.amount == FRIEND_AMOUNT == 20
    assert result.friendship_bonus is True
    assert await _balance(db_session, alice.id) == 20
    assert await _balance(db_session, bob.id) == 20

    rows = await _reward_rows(db_session, HUG_REASON, HUG_RECEIVED_REASON)
    assert len(rows) == 2
    assert {row.user_id for row in rows} == {alice.id, bob.id}
    # Both sides are paid the SAME bonus amount, never 10 + 20.
    assert {row.amount for row in rows} == {20}


@pytest.mark.asyncio
async def test_kiss_gives_20_if_friends(db_session):
    """Accepted friends: BOTH wallets go up by 20 for «بوس سیبی» too."""
    alice = await _make_user(db_session, "آلیس بوس")
    bob = await _make_user(db_session, "باب بوس")
    await _befriend(db_session, alice, bob)

    result = await SibKissService.kiss(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:2"
    )
    await db_session.commit()

    assert result.status == KISS_AWARDED
    assert result.amount == FRIEND_AMOUNT == 20
    assert result.friendship_bonus is True
    assert await _balance(db_session, alice.id) == 20
    assert await _balance(db_session, bob.id) == 20

    rows = await _reward_rows(db_session, KISS_REASON, KISS_RECEIVED_REASON)
    assert len(rows) == 2
    assert {row.amount for row in rows} == {20}


@pytest.mark.asyncio
async def test_hug_gives_10_if_not_friends(db_session):
    """No friendship row at all: both wallets keep the plain 10."""
    alice = await _make_user(db_session, "آلیس غریبه")
    bob = await _make_user(db_session, "باب غریبه")

    result = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:3"
    )
    await db_session.commit()

    assert result.status == HUG_AWARDED
    assert result.amount == BASE_AMOUNT == 10
    assert result.friendship_bonus is False
    assert await _balance(db_session, alice.id) == 10
    assert await _balance(db_session, bob.id) == 10

    rows = await _reward_rows(db_session, HUG_REASON, HUG_RECEIVED_REASON)
    assert {row.amount for row in rows} == {10}


@pytest.mark.asyncio
async def test_hug_friendship_bonus_does_not_break_cooldown(db_session):
    """A bonus-paying hug still starts the normal cooldown: no farming.

    The second hug arrives immediately after the first, from the same sender.
    It must be refused by the cooldown and MUST NOT grant another 20 to
    anyone - not to the same friend, and not to a different one.
    """
    alice = await _make_user(db_session, "آلیس کولدان")
    bob = await _make_user(db_session, "باب کولدان")
    carol = await _make_user(db_session, "کارول کولدان")
    await _befriend(db_session, alice, bob)
    await _befriend(db_session, alice, carol)

    first = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:10"
    )
    await db_session.commit()
    assert first.status == HUG_AWARDED
    assert first.amount == 20

    # Same friend again, right away.
    second = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:11"
    )
    await db_session.commit()

    # The cooldown still blocks it even though the pair are friends.
    assert second.status == HUG_COOLDOWN
    assert second.remaining_seconds > 0
    assert second.amount == 0
    # No extra apples anywhere: Alice keeps her single 20, Bob his single 20,
    # and the ledger still holds exactly the two rows of the first hug.
    assert await _balance(db_session, alice.id) == 20
    assert await _balance(db_session, bob.id) == 20
    rows = await _reward_rows(db_session, HUG_REASON, HUG_RECEIVED_REASON)
    assert len(rows) == 2

    # Nor can the bonus be farmed by switching to ANOTHER friend.
    third = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=carol.id, message_key="-100:12"
    )
    await db_session.commit()
    assert third.status == HUG_COOLDOWN
    assert await _balance(db_session, alice.id) == 20
    assert await _balance(db_session, carol.id) == 0
    assert len(await _reward_rows(db_session, HUG_REASON, HUG_RECEIVED_REASON)) == 2


# ---------------------------------------------------------------------------
# Bonus eligibility rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kiss_friendship_bonus_does_not_break_cooldown(db_session):
    """The kiss cooldown survives the bonus exactly like the hug one."""
    alice = await _make_user(db_session, "آلیس بوس کولدان")
    bob = await _make_user(db_session, "باب بوس کولدان")
    await _befriend(db_session, alice, bob)

    first = await SibKissService.kiss(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:20"
    )
    await db_session.commit()
    assert first.amount == 20

    second = await SibKissService.kiss(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:21"
    )
    await db_session.commit()

    assert second.status == KISS_COOLDOWN
    assert await _balance(db_session, alice.id) == 20
    assert await _balance(db_session, bob.id) == 20


@pytest.mark.asyncio
async def test_pending_friend_request_does_not_pay_the_bonus(db_session):
    """A PENDING request is not a friendship: both sides still get 10."""
    alice = await _make_user(db_session, "آلیس در انتظار")
    bob = await _make_user(db_session, "باب در انتظار")
    session = db_session
    session.add(
        AppleFriendship(
            user_id=alice.id, friend_id=bob.id, status=FriendshipStatus.PENDING
        )
    )
    await session.commit()

    result = await SibHugService.hug(
        session, sender_id=alice.id, target_id=bob.id, message_key="-100:30"
    )
    await session.commit()

    assert result.amount == 10
    assert result.friendship_bonus is False
    assert await _balance(session, alice.id) == 10
    assert await _balance(session, bob.id) == 10


@pytest.mark.asyncio
async def test_bonus_is_direction_agnostic(db_session):
    """The friendship row is matched in BOTH column orders."""
    alice = await _make_user(db_session, "آلیس برعکس")
    bob = await _make_user(db_session, "باب برعکس")
    # Bob is the row's owner, Alice the friend; the hug goes Alice -> Bob.
    db_session.add(
        AppleFriendship(
            user_id=bob.id, friend_id=alice.id, status=FriendshipStatus.ACCEPTED
        )
    )
    await db_session.commit()

    result = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:31"
    )
    await db_session.commit()

    assert result.friendship_bonus is True
    assert result.amount == 20
    assert await _balance(db_session, bob.id) == 20


@pytest.mark.asyncio
async def test_bonus_ignores_a_friendship_with_a_third_user(db_session):
    """Being friends with SOMEONE ELSE does not pay the bonus."""
    alice = await _make_user(db_session, "آلیس سه نفره")
    bob = await _make_user(db_session, "باب سه نفره")
    carol = await _make_user(db_session, "کارول سه نفره")
    await _befriend(db_session, alice, carol)

    result = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:32"
    )
    await db_session.commit()

    assert result.friendship_bonus is False
    assert result.amount == 10
    assert await _balance(db_session, bob.id) == 10
    assert await _balance(db_session, carol.id) == 0


@pytest.mark.asyncio
async def test_unfriending_removes_the_bonus(db_session):
    """After ``unfriend`` the pair are ordinary users again (10 each)."""
    alice = await _make_user(db_session, "آلیس جدایی")
    bob = await _make_user(db_session, "باب جدایی")
    await _befriend(db_session, alice, bob)

    assert await FriendshipService.unfriend(db_session, alice.id, bob.id)
    await db_session.commit()

    result = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:33"
    )
    await db_session.commit()

    assert result.friendship_bonus is False
    assert result.amount == 10
    assert await _balance(db_session, alice.id) == 10
    assert await _balance(db_session, bob.id) == 10


@pytest.mark.asyncio
async def test_friendship_bonus_survives_duplicate_message_redelivery(db_session):
    """Replaying the SAME message never pays the bonus twice."""
    alice = await _make_user(db_session, "آلیس تکرار")
    bob = await _make_user(db_session, "باب تکرار")
    await _befriend(db_session, alice, bob)

    first = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:40"
    )
    await db_session.commit()
    replay = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:40"
    )
    await db_session.commit()

    assert first.amount == 20
    assert replay.status == HUG_DUPLICATE
    assert await _balance(db_session, alice.id) == 20
    assert await _balance(db_session, bob.id) == 20


@pytest.mark.asyncio
async def test_bonus_is_configurable_and_additive(db_session):
    """The bonus is base + configured bonus, so a rebalance stays coherent."""
    from sib.services.config_store import ConfigService

    alice = await _make_user(db_session, "آلیس تنظیم")
    bob = await _make_user(db_session, "باب تنظیم")
    await _befriend(db_session, alice, bob)

    await ConfigService.set(db_session, HUG_AMOUNT_KEY, 10)
    await ConfigService.set(db_session, HUG_FRIEND_BONUS_KEY, 5)
    await db_session.commit()

    result = await SibHugService.hug(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:50"
    )
    await db_session.commit()

    assert result.amount == 15
    assert await _balance(db_session, alice.id) == 15

    await ConfigService.set(db_session, HUG_FRIEND_BONUS_KEY, FRIEND_BONUS)
    await ConfigService.set(db_session, HUG_AMOUNT_KEY, BASE_AMOUNT)
    await db_session.commit()


@pytest.mark.asyncio
async def test_kiss_bonus_is_configurable_and_additive(db_session):
    """Same rule for «بوس سیبی»: base + ``sib_kiss_friend_bonus``."""
    from sib.services.config_store import ConfigService

    alice = await _make_user(db_session, "آلیس تنظیم بوس")
    bob = await _make_user(db_session, "باب تنظیم بوس")
    await _befriend(db_session, alice, bob)

    await ConfigService.set(db_session, KISS_AMOUNT_KEY, 10)
    await ConfigService.set(db_session, KISS_FRIEND_BONUS_KEY, 5)
    await db_session.commit()

    result = await SibKissService.kiss(
        db_session, sender_id=alice.id, target_id=bob.id, message_key="-100:51"
    )
    await db_session.commit()

    assert result.amount == 15

    await ConfigService.set(db_session, KISS_FRIEND_BONUS_KEY, FRIEND_BONUS)
    await ConfigService.set(db_session, KISS_AMOUNT_KEY, BASE_AMOUNT)
    await db_session.commit()


# ---------------------------------------------------------------------------
# TASK 3 — the narrative must match the amount actually granted
# ---------------------------------------------------------------------------


def _all_pools():
    return (
        ("hug", HUG_MESSAGE_VARIANTS, False),
        ("hug", HUG_FRIEND_MESSAGE_VARIANTS, True),
        ("kiss", KISS_MESSAGE_VARIANTS, False),
        ("kiss", KISS_FRIEND_MESSAGE_VARIANTS, True),
    )


def test_friend_variant_pools_are_distinct_and_well_formed():
    for label, pool, is_friend in _all_pools():
        assert len(pool) >= 6, label
        assert len(set(pool)) == len(pool), label
        for template in pool:
            assert "**{a}**" in template, template
            assert "**{b}**" in template, template
            # The amount placeholder is mandatory in every pool, so no
            # narrative can ever omit or hardcode the granted amount.
            assert "**{n} سیب**" in template, template
            assert "..." not in template and "—" not in template, template
            # Friend variants carry one extra sentence (the friendship line),
            # so they may end with up to three terminal marks; the plain
            # pools keep the house style of at most two.
            marks = sum(template.count(m) for m in ".!؟")
            assert marks <= (3 if is_friend else 2), template


def test_every_variant_renders_the_granted_amount_in_persian_digits():
    """Render each template in both pools and check the printed amount."""
    rng = random.Random(11)
    for _label, pool, is_friend in _all_pools():
        for template in pool:
            for amount in (10, 20):
                text = template.format(a="آلیس", b="باب", n=fa_digits(amount))
                assert f"**{fa_digits(amount)} سیب**" in text, text
                # Latin digits must never leak into user-facing text.
                assert str(amount) not in text, text


def test_pick_message_matches_amount_for_friends_and_strangers():
    """The rendered line always shows exactly what the ledger paid."""
    rng = random.Random(3)

    friend_text = pick_hug_message("آلیس", "باب", 20, rng=rng, friends=True)
    assert "**۲۰ سیب**" in friend_text
    assert "20" not in friend_text
    assert friend_text in {
        t.format(a="آلیس", b="باب", n=fa_digits(20))
        for t in HUG_FRIEND_MESSAGE_VARIANTS
    }

    stranger_text = pick_hug_message("آلیس", "باب", 10, rng=rng)
    assert "**۱۰ سیب**" in stranger_text
    assert "10" not in stranger_text
    assert stranger_text in {
        t.format(a="آلیس", b="باب", n=fa_digits(10)) for t in HUG_MESSAGE_VARIANTS
    }

    friend_kiss = pick_kiss_message("آلیس", "باب", 20, rng=rng, friends=True)
    assert "**۲۰ سیب**" in friend_kiss
    assert friend_kiss in {
        t.format(a="آلیس", b="باب", n=fa_digits(20))
        for t in KISS_FRIEND_MESSAGE_VARIANTS
    }

    stranger_kiss = pick_kiss_message("آلیس", "باب", 10, rng=rng)
    assert "**۱۰ سیب**" in stranger_kiss


def test_friend_pool_celebrates_the_friendship():
    """Every friend variant says the two are friends (دوست/رفیق)."""
    for _label, pool, is_friend in _all_pools():
        if not is_friend:
            continue
        for template in pool:
            assert any(word in template for word in ("دوست", "رفیق")), template


def test_friend_variants_differ_from_the_plain_pools():
    """The two pools never overlap, so a friend hug reads differently."""
    assert not set(HUG_FRIEND_MESSAGE_VARIANTS) & set(HUG_MESSAGE_VARIANTS)
    assert not set(KISS_FRIEND_MESSAGE_VARIANTS) & set(KISS_MESSAGE_VARIANTS)


def test_pick_messages_vary_across_the_friend_pool():
    rng = random.Random(5)
    seen_hug = {
        pick_hug_message("A", "B", 20, rng=rng, friends=True) for _ in range(200)
    }
    seen_kiss = {
        pick_kiss_message("A", "B", 20, rng=rng, friends=True) for _ in range(200)
    }
    assert len(seen_hug) >= 4
    assert len(seen_kiss) >= 4


# ---------------------------------------------------------------------------
# End-to-end handler: the group sees the bonus narrative
# ---------------------------------------------------------------------------


def _tg_user(uid: int, name: str):
    from aiogram.types import User as TGUser

    return TGUser(id=uid, is_bot=False, first_name=name, username=f"u{uid}")


def _group_message(sender, text: str, *, target, message_id=900):
    msg = MagicMock()
    msg.chat = MagicMock()
    msg.chat.type = "supergroup"
    msg.chat.id = -6100
    msg.message_id = message_id
    msg.from_user = sender
    msg.text = text
    msg.reply = AsyncMock(return_value=MagicMock(message_id=message_id + 1))
    msg.reply_photo = AsyncMock(return_value=MagicMock(message_id=message_id + 2))
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.from_user = target
    return msg


async def _befriend_by_id(session, a_id: int, b_id: int) -> None:
    session.add(
        AppleFriendship(
            user_id=a_id, friend_id=b_id, status=FriendshipStatus.ACCEPTED
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_handler_hug_reply_shows_the_friendship_bonus_amount(db_session, monkeypatch, tmp_path):
    """The group message prints ۲۰ and the friendship line for friends."""
    from sib.handlers.sib_hug import handle_sib_hug

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(830001, "آلیس")
    target = _tg_user(830002, "باب")
    await _befriend_by_id(db_session, sender.id, target.id)

    msg = _group_message(sender, "بغل سیبی", target=target, message_id=61)
    await handle_sib_hug(msg)

    text = msg.reply.call_args[0][0]
    assert "**۲۰ سیب**" in text
    assert "20" not in text
    assert any(word in text for word in ("دوست", "رفیق"))
    assert msg.reply.call_args.kwargs.get("parse_mode") == "Markdown"

    async with get_session() as session:
        assert await _balance(session, sender.id) == 20
        assert await _balance(session, target.id) == 20


@pytest.mark.asyncio
async def test_handler_hug_reply_shows_10_for_strangers(db_session, monkeypatch, tmp_path):
    """Strangers keep the plain narrative and the plain ۱۰ سیب."""
    from sib.handlers.sib_hug import handle_sib_hug

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(831001, "آلیس")
    target = _tg_user(831002, "باب")

    msg = _group_message(sender, "بغل سیبی", target=target, message_id=62)
    await handle_sib_hug(msg)

    text = msg.reply.call_args[0][0]
    assert "**۱۰ سیب**" in text

    async with get_session() as session:
        assert await _balance(session, sender.id) == 10
        assert await _balance(session, target.id) == 10


@pytest.mark.asyncio
async def test_handler_kiss_reply_shows_the_friendship_bonus_amount(db_session, monkeypatch, tmp_path):
    from sib.handlers.sib_kiss import handle_sib_kiss

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(832001, "آلیس")
    target = _tg_user(832002, "باب")
    await _befriend_by_id(db_session, sender.id, target.id)

    msg = _group_message(sender, "بوس سیبی", target=target, message_id=63)
    await handle_sib_kiss(msg)

    text = msg.reply.call_args[0][0]
    assert "**۲۰ سیب**" in text
    assert any(word in text for word in ("دوست", "رفیق"))

    async with get_session() as session:
        assert await _balance(session, sender.id) == 20
        assert await _balance(session, target.id) == 20


@pytest.mark.asyncio
async def test_bonus_narrative_is_plain_after_strip_bold(db_session, monkeypatch, tmp_path):
    """A bonus line stays readable as a callback-style alert (no ** markers).

    Hug/Kiss answer through ``message.reply`` with Markdown today, but the
    text must survive ``strip_bold`` unchanged apart from the markers, so it
    can be reused for a ``callback.answer`` alert without a rewrite.
    """
    from sib.core.text import strip_bold

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(833001, "آلیس")
    target = _tg_user(833002, "باب")
    await _befriend_by_id(db_session, sender.id, target.id)

    from sib.handlers.sib_hug import handle_sib_hug

    msg = _group_message(sender, "بغل سیبی", target=target, message_id=64)
    await handle_sib_hug(msg)

    plain = strip_bold(msg.reply.call_args[0][0])
    assert "**" not in plain
    assert "۲۰ سیب" in plain
