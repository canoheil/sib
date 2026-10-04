"""Phase 7.6 — Telegram Flower send / accept / reject flow.

The handlers in ``sib.handlers.marriage`` are a thin adapter over
``LoveService``: every rule (marriage validity, spouse check, self-send,
Flower ownership, the directional daily limit, the configured Love gain,
clamping and the refund on rejection) belongs to the domain. These tests
prove the Telegram layer delegates instead of re-implementing, that
identity comes from ``callback.from_user`` / ``message.reply_to_message``
and never from the payload, and that a Telegram failure after a successful
domain call can never roll the resolution back.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat as TelegramChat, Message, User as TelegramUser

from sib.database import Group, Marriage, MarriageStatus, User
from sib.database.models import FlowerGift, FlowerGiftStatus
from sib.handlers.marriage import (
    FLOWER_ACCEPT_ANSWER,
    FLOWER_DAILY_LIMIT_TEXT,
    FLOWER_GENERIC_FAILURE_TEXT,
    FLOWER_INVALID_BUTTON_TEXT,
    FLOWER_NOT_MARRIED_TEXT,
    FLOWER_NOT_SPOUSE_TEXT,
    FLOWER_NOT_YOUR_GIFT_TEXT,
    FLOWER_NO_FLOWER_TEXT,
    FLOWER_REPLY_REQUIRED_TEXT,
    FLOWER_SELF_TEXT,
    FLOWER_TARGET_IS_BOT_TEXT,
    FLOWER_TRIGGER_PATTERN,
    FLOWER_ALREADY_RESOLVED_TEXT,
    FLOWER_MARRIAGE_INACTIVE_TEXT,
    FLOWER_REJECT_ANSWER,
    handle_flower_accept,
    handle_flower_reject,
    handle_flower_send,
)
from sib.keyboards.marriage import (
    LOVE_FLOWER_ACCEPT,
    LOVE_FLOWER_REJECT,
    get_flower_gift_keyboard,
    love_flower_callback,
    parse_love_flower_callback,
)
from sib.services.items import ItemService
from sib.services.love import LoveService
from sib.services.marriage import MarriageService

GROUP_ID = -1007860
GROUP_TITLE = "گروه گل سیب"

SENDER_ID = 7860
SPOUSE_ID = 7861
STRANGER_ID = 7862
BOT_ID = 7863

CARD_MESSAGE_ID = 786
CARD_CHAT_ID = GROUP_ID

SENT_MESSAGE_ID = 786


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tg(user_id: int, first_name: str = "کاربر", *, is_bot: bool = False):
    return TelegramUser(id=user_id, is_bot=is_bot, first_name=first_name)


def _group_chat() -> TelegramChat:
    return TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)


def _sent(message_id: int = SENT_MESSAGE_ID) -> MagicMock:
    sent = MagicMock(spec=Message)
    sent.message_id = message_id
    sent.chat = _group_chat()
    return sent


def _flower_message(sender: TelegramUser, reply_to=None) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = _group_chat()
    msg.from_user = sender
    msg.text = "گل سیبی"
    msg.message_id = 960
    msg.reply_to_message = reply_to
    msg.reply = AsyncMock(return_value=_sent())
    return msg


def _flower_callback(gift_id: int, actor: TelegramUser, action: str) -> MagicMock:
    cb = MagicMock()
    cb.data = f"{action}:{gift_id}"
    cb.from_user = actor
    cb.message = MagicMock(spec=Message)
    cb.message.chat = _group_chat()
    cb.message.message_id = CARD_MESSAGE_ID
    cb.message.text = "card"
    cb.message.reply_markup = get_flower_gift_keyboard(gift_id)
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.send_message = AsyncMock(return_value=_sent())
    return bot


async def _mk_user(session, user_id: int, **kwargs) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name=kwargs.pop("first_name", f"سیبی{user_id}"),
            language_code="fa",
            **kwargs,
        )
        session.add(user)
        await session.flush()
    return user


async def _mk_group(session, group_id: int = GROUP_ID) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title=GROUP_TITLE)
        session.add(group)
        await session.flush()
    return group


async def _marry(db_session, a: int, b: int, *, love_score: int = 0) -> int:
    await _mk_user(db_session, a)
    await _mk_user(db_session, b)
    await _mk_group(db_session)
    await db_session.flush()
    marriage = Marriage(
        proposer_id=a,
        accepter_id=b,
        origin_group_id=GROUP_ID,
        status=MarriageStatus.ACTIVE,
        love_score=love_score,
    )
    db_session.add(marriage)
    await db_session.commit()
    return int(marriage.id)


async def _grant_flower(db_session, user_id: int, quantity: int = 1) -> None:
    await ItemService.grant(db_session, user_id, "flower", quantity)
    await db_session.commit()


async def _send(db_session, sender: TelegramUser, reply_to) -> MagicMock:
    msg = _flower_message(sender, reply_to=reply_to)
    await handle_flower_send(msg)
    return msg


def _reply_text(msg: MagicMock) -> str:
    return msg.reply.call_args.args[0]


def _edited_text(bot: MagicMock) -> str:
    """The text of the last card edit, whichever call style was used."""
    call = bot.edit_message_text.call_args
    assert call is not None, "the card was never edited"
    return call.args[0] if call.args else call.kwargs["text"]


def _edited_markup(bot: MagicMock):
    call = bot.edit_message_text.call_args
    return None if call is None else call.kwargs.get("reply_markup")


async def _pending_gift_id(db_session, sender_id: int = SENDER_ID) -> int:
    from sqlalchemy import select

    gift = (
        await db_session.scalars(
            select(FlowerGift)
            .where(
                FlowerGift.sender_id == sender_id,
                FlowerGift.status == FlowerGiftStatus.PENDING,
            )
            .order_by(FlowerGift.id.desc())
        )
    ).first()
    assert gift is not None, "expected a PENDING flower gift"
    return int(gift.id)


# ---------------------------------------------------------------------------
# 1. Keyboard / callback contract
# ---------------------------------------------------------------------------


def test_flower_keyboard_shape():
    kb = get_flower_gift_keyboard(7)
    flat = [b for row in kb.inline_keyboard for b in row]
    assert [(b.text, b.callback_data) for b in flat] == [
        ("💐 قبول گل", "love_flower_accept:7"),
        ("❌ رد گل", "love_flower_reject:7"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "love_flower_accept",
        "love_flower_accept:",
        "love_flower_accept:0",
        "love_flower_accept:-3",
        "love_flower_accept:abc",
        "love_flower_accept:1;drop",
        "marriage_accept:5",
        "love_flower_accept:99999999999999999999999",
    ],
)
def test_malformed_flower_callback_fails_closed(payload):
    assert parse_love_flower_callback(payload) is None


def test_flower_callback_builder_validates_the_action():
    assert love_flower_callback(LOVE_FLOWER_ACCEPT, 5) == "love_flower_accept:5"
    with pytest.raises(ValueError):
        love_flower_callback("marriage_accept", 5)


def test_flower_trigger_pattern_is_anchored():
    rx = re.compile(FLOWER_TRIGGER_PATTERN)
    assert rx.match("گل سیبی")
    assert rx.match("  گل سیبی؟ ")
    assert rx.match("گل سيبی")  # Arabic Yeh variant
    assert not rx.match("گل سیبی جان")
    assert not rx.match("یه گل سیبی دادم")


# ---------------------------------------------------------------------------
# 2. SEND
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_flower_valid_reply_creates_a_pending_gift(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _grant_flower(db_session, SENDER_ID)

    msg = await _send(db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا")))

    gift_id = await _pending_gift_id(db_session)
    text = _reply_text(msg)
    assert "💐" in text
    assert "گل" in text
    # The card carries the accept/reject keyboard.
    assert msg.reply.call_args.kwargs["reply_markup"] is not None
    # Exactly one Flower consumed.
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 0

    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.PENDING
    assert gift.sender_id == SENDER_ID
    assert gift.receiver_id == SPOUSE_ID
    assert gift.message_id == SENT_MESSAGE_ID


@pytest.mark.asyncio
async def test_send_flower_without_reply_is_refused(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _grant_flower(db_session, SENDER_ID)

    msg = await _send(db_session, _tg(SENDER_ID, "سارا"), None)

    assert _reply_text(msg) == FLOWER_REPLY_REQUIRED_TEXT
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1


@pytest.mark.asyncio
async def test_send_flower_to_a_non_spouse_is_refused(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _mk_user(db_session, STRANGER_ID)
    await _grant_flower(db_session, SENDER_ID)
    await db_session.commit()

    msg = await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(STRANGER_ID, "غریبه"))
    )

    assert _reply_text(msg) == FLOWER_NOT_SPOUSE_TEXT
    # Nothing consumed, no gift created.
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1
    await _assert_no_gifts(db_session)


@pytest.mark.asyncio
async def test_send_flower_to_self_is_refused(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _grant_flower(db_session, SENDER_ID)

    msg = await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SENDER_ID, "سارا"))
    )

    assert _reply_text(msg) == FLOWER_SELF_TEXT
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1


@pytest.mark.asyncio
async def test_send_flower_to_a_bot_is_refused(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _mk_user(db_session, BOT_ID, is_bot=True)
    await _grant_flower(db_session, SENDER_ID)
    await db_session.commit()

    msg = await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(BOT_ID, "ربات", is_bot=True))
    )

    assert _reply_text(msg) == FLOWER_TARGET_IS_BOT_TEXT
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1


@pytest.mark.asyncio
async def test_send_flower_without_a_flower_is_refused(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    # No Flower granted.

    msg = await _send(db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا")))

    assert _reply_text(msg) == FLOWER_NO_FLOWER_TEXT
    await _assert_no_gifts(db_session)


@pytest.mark.asyncio
async def test_send_flower_requires_an_active_marriage(db_session):
    await _mk_user(db_session, SENDER_ID)
    await _mk_user(db_session, SPOUSE_ID)
    await _mk_group(db_session)
    await _grant_flower(db_session, SENDER_ID)
    await db_session.commit()

    msg = await _send(db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا")))

    assert _reply_text(msg) == FLOWER_NOT_MARRIED_TEXT
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1


@pytest.mark.asyncio
async def test_send_flower_respects_the_directional_daily_limit(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _grant_flower(db_session, SENDER_ID, 3)

    first = await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    assert await _pending_gift_id(db_session) is not None

    second = await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    assert _reply_text(second) == FLOWER_DAILY_LIMIT_TEXT
    # The second attempt consumed nothing: 3 - 1 = 2 left.
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 2
    assert first is not None


@pytest.mark.asyncio
async def test_the_return_direction_has_its_own_daily_allowance(db_session):
    """A->B and B->A are independent daily directions."""
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _grant_flower(db_session, SENDER_ID, 2)
    await _grant_flower(db_session, SPOUSE_ID, 2)

    assert _reply_text(
        await _send(
            db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
        )
    ) != FLOWER_DAILY_LIMIT_TEXT
    reverse = await _send(
        db_session, _tg(SPOUSE_ID, "رضا"), _flower_reply(_tg(SENDER_ID, "سارا"))
    )
    assert _reply_text(reverse) != FLOWER_DAILY_LIMIT_TEXT

    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1
    assert await ItemService.get_total(db_session, SPOUSE_ID, "flower") == 1


@pytest.mark.asyncio
async def test_failed_send_never_reaches_a_second_flower(db_session):
    """Two failed attempts (no Flower) must each be refused identically."""
    await _marry(db_session, SENDER_ID, SPOUSE_ID)

    for _ in range(2):
        msg = await _send(
            db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
        )
        assert _reply_text(msg) == FLOWER_NO_FLOWER_TEXT
    await _assert_no_gifts(db_session)


@pytest.mark.asyncio
async def test_unexpected_send_failure_is_generic_and_never_leaks(db_session, monkeypatch):
    """A non-domain exception must never reach the chat."""
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _grant_flower(db_session, SENDER_ID)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("secret database detail")

    monkeypatch.setattr(LoveService, "send_flower", staticmethod(boom))

    msg = await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )

    assert _reply_text(msg) == FLOWER_GENERIC_FAILURE_TEXT
    assert "secret" not in _reply_text(msg)
    assert "RuntimeError" not in _reply_text(msg)
    # The Flower was not consumed.
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1


@pytest.mark.asyncio
async def test_unexpected_resolve_failure_is_generic(db_session, monkeypatch):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("secret database detail")

    monkeypatch.setattr(LoveService, "accept_flower", staticmethod(boom))

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_GENERIC_FAILURE_TEXT, show_alert=True)
    bot.edit_message_text.assert_not_awaited()
    # Untouched: the domain never ran.
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 10
    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.PENDING


@pytest.mark.asyncio
async def test_undelivered_card_refunds_the_flower(db_session):
    """If the card cannot be posted, the domain invalidates and refunds."""
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _grant_flower(db_session, SENDER_ID)

    msg = _flower_message(
        _tg(SENDER_ID, "سارا"), reply_to=_flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    msg.reply = AsyncMock(return_value=None)  # delivery failed

    await handle_flower_send(msg)

    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1
    db_session.expire_all()
    await _assert_no_pending_gifts(db_session)


# ---------------------------------------------------------------------------
# 3. ACCEPT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recipient_accepts_and_love_increases(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_ACCEPT_ANSWER, show_alert=False)
    # Buttons removed: the edit carries no keyboard.
    assert _edited_markup(bot) is None
    assert "قبول شد" in _edited_text(bot)

    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.ACCEPTED
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 12  # gain = 2

    # The sender is notified in the group.
    cb.message.answer.assert_awaited_once()
    assert "قبول کرد" in cb.message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_sender_cannot_accept_their_own_flower(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SENDER_ID, "سارا"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_NOT_YOUR_GIFT_TEXT, show_alert=True)
    bot.edit_message_text.assert_not_awaited()
    bot.edit_message_reply_markup.assert_not_awaited()  # a stranger must not strip buttons
    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.PENDING


@pytest.mark.asyncio
async def test_third_party_cannot_accept(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)
    await _mk_user(db_session, STRANGER_ID)
    await _grant_flower(db_session, SENDER_ID)
    await db_session.commit()
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(STRANGER_ID, "غریبه"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_NOT_YOUR_GIFT_TEXT, show_alert=True)
    bot.edit_message_text.assert_not_awaited()
    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.PENDING


@pytest.mark.asyncio
async def test_accept_twice_second_is_refused(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=0)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    first = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    await handle_flower_accept(first, _bot())
    second = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    bot2 = _bot()
    await handle_flower_accept(second, bot2)

    second.answer.assert_awaited_once_with(FLOWER_ALREADY_RESOLVED_TEXT, show_alert=True)
    # Love grew exactly once.
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 2


@pytest.mark.asyncio
async def test_accept_clamps_love_at_the_maximum(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=100)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    await handle_flower_accept(cb, _bot())

    cb.answer.assert_awaited_once_with(FLOWER_ACCEPT_ANSWER, show_alert=False)
    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.ACCEPTED
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 100


@pytest.mark.asyncio
async def test_accept_after_divorce_is_refused_and_refunds(db_session):
    """Divorce settles pending gifts itself; the handler just presents it.

    ``MarriageService.divorce`` invalidates (and refunds) every PENDING gift
    of the marriage, so by the time the recipient presses the stale button
    the domain already resolved it. The handler must show that gracefully
    and strip the dead buttons - never crash, never refund twice.
    """
    marriage_id = await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=20)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    await MarriageService.divorce(db_session, marriage_id, SENDER_ID)
    await db_session.commit()

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_ALREADY_RESOLVED_TEXT, show_alert=True)
    # Definitively resolved -> stale buttons are stripped.
    bot.edit_message_reply_markup.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
    # The Flower came back exactly once.
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1
    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.INVALIDATED
    # No active marriage left -> no Love score, and the stale press changed
    # nothing (the divorce already reset it).
    assert await LoveService.get_love_score(db_session, SENDER_ID) is None


@pytest.mark.asyncio
async def test_accept_when_the_marriage_is_inactive_refunds(db_session):
    """A gift whose marriage stopped being ACTIVE is settled by the domain.

    This exercises ``FlowerGiftMarriageInactiveError``: the handler reports
    the domain's decision and never invents a Love value.
    """
    marriage_id = await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=20)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    marriage = await db_session.get(Marriage, marriage_id)
    marriage.status = MarriageStatus.DIVORCED
    await db_session.commit()

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_MARRIAGE_INACTIVE_TEXT, show_alert=True)
    bot.edit_message_reply_markup.assert_awaited_once()
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1
    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status in FlowerGiftStatus.TERMINAL


@pytest.mark.asyncio
async def test_accept_uses_the_configured_gain_not_a_hardcoded_one(db_session):
    """The handler renders whatever the domain applied."""
    from sib.services.config_store import ConfigService

    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=0)
    await ConfigService.set(db_session, "flower_love_gain", 7)
    await _grant_flower(db_session, SENDER_ID)
    await db_session.commit()

    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    await handle_flower_accept(cb, _bot())

    assert await LoveService.get_love_score(db_session, SENDER_ID) == 7


# ---------------------------------------------------------------------------
# 4. REJECT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recipient_rejects_refunds_and_reduces_love(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_REJECT)
    bot = _bot()
    await handle_flower_reject(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_REJECT_ANSWER, show_alert=False)
    assert _edited_markup(bot) is None
    assert "رد شد" in _edited_text(bot)

    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.REJECTED
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 9  # penalty 1
    # Flower refunded exactly once (0 before -> 1 after).
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1

    cb.message.answer.assert_awaited_once()
    notification = cb.message.answer.call_args.args[0]
    assert "رد کرد" in notification
    assert "برگشت" in notification


@pytest.mark.asyncio
async def test_reject_clamps_love_at_zero(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=0)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_REJECT)
    await handle_flower_reject(cb, _bot())

    assert await LoveService.get_love_score(db_session, SENDER_ID) == 0
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1


@pytest.mark.asyncio
async def test_sender_cannot_reject_their_own_flower(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SENDER_ID, "سارا"), LOVE_FLOWER_REJECT)
    bot = _bot()
    await handle_flower_reject(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_NOT_YOUR_GIFT_TEXT, show_alert=True)
    bot.edit_message_text.assert_not_awaited()
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 10
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 0


@pytest.mark.asyncio
async def test_third_party_cannot_reject(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _mk_user(db_session, STRANGER_ID)
    await _grant_flower(db_session, SENDER_ID)
    await db_session.commit()
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(STRANGER_ID, "غریبه"), LOVE_FLOWER_REJECT)
    bot = _bot()
    await handle_flower_reject(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_NOT_YOUR_GIFT_TEXT, show_alert=True)
    bot.edit_message_text.assert_not_awaited()
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 10


# ---------------------------------------------------------------------------
# 5. Stale / malformed / unknown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_callback_is_refused_without_touching_anything(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    cb.data = "love_flower_accept:not-a-number"
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_INVALID_BUTTON_TEXT, show_alert=True)
    bot.edit_message_text.assert_not_awaited()
    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.PENDING


@pytest.mark.asyncio
async def test_unknown_gift_id_is_refused(db_session):
    cb = _flower_callback(999_999, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once()
    # Never surfaces internal text.
    answer = cb.answer.call_args.args[0]
    assert "FlowerGift" not in answer
    assert "Traceback" not in answer
    assert "999" not in answer


@pytest.mark.asyncio
async def test_no_internal_exception_text_ever_reaches_the_user(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID)

    # Not married / no flower / wrong spouse: all friendly Persian.
    msg = await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    text = _reply_text(msg)
    for leaked in ("Error", "Exception", "Traceback", "None", "sib.", "love_score"):
        assert leaked not in text


@pytest.mark.asyncio
async def test_telegram_edit_failure_does_not_rollback_the_accept(db_session):
    """A failed Telegram edit must never undo a committed resolution."""
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    bot.edit_message_text = AsyncMock(side_effect=RuntimeError("boom"))
    bot.edit_message_caption = AsyncMock(side_effect=RuntimeError("boom"))

    await handle_flower_accept(cb, bot)

    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.ACCEPTED
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 12


@pytest.mark.asyncio
async def test_notification_failure_does_not_rollback_the_accept(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    cb.message.answer = AsyncMock(side_effect=RuntimeError("boom"))

    await handle_flower_accept(cb, _bot())

    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.ACCEPTED
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 12


@pytest.mark.asyncio
async def test_accept_after_expiration_refunds(db_session):
    """An expired gift resolves gracefully and the Flower comes back."""
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    await LoveService.expire_gift(db_session, gift_id)
    await db_session.commit()

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    bot = _bot()
    await handle_flower_accept(cb, bot)

    cb.answer.assert_awaited_once_with(FLOWER_ALREADY_RESOLVED_TEXT, show_alert=True)
    bot.edit_message_reply_markup.assert_awaited_once()
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 10


@pytest.mark.asyncio
async def test_reject_after_expiration_is_graceful(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    await LoveService.expire_gift(db_session, gift_id)
    await db_session.commit()

    cb = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_REJECT)
    await handle_flower_reject(cb, _bot())

    cb.answer.assert_awaited_once_with(FLOWER_ALREADY_RESOLVED_TEXT, show_alert=True)
    # Refunded once by the expiry, not twice.
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1


# ---------------------------------------------------------------------------
# 6. Concurrency (the domain owns correctness; the handler adds no locking)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_simultaneous_accepts_only_count_once(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    a = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    b = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    await handle_flower_accept(a, _bot())
    await handle_flower_accept(b, _bot())

    assert await LoveService.get_love_score(db_session, SENDER_ID) == 12
    gift = await db_session.get(FlowerGift, gift_id)
    assert gift.status == FlowerGiftStatus.ACCEPTED


@pytest.mark.asyncio
async def test_accept_then_reject_leaves_the_accept_in_place(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    await handle_flower_accept(
        _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT), _bot()
    )
    late = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_REJECT)
    await handle_flower_reject(late, _bot())

    late.answer.assert_awaited_once_with(FLOWER_ALREADY_RESOLVED_TEXT, show_alert=True)
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 12
    # No refund: the gift was accepted, not rejected.
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 0


@pytest.mark.asyncio
async def test_reject_then_accept_leaves_the_reject_in_place(db_session):
    await _marry(db_session, SENDER_ID, SPOUSE_ID, love_score=10)
    await _grant_flower(db_session, SENDER_ID)
    await _send(
        db_session, _tg(SENDER_ID, "سارا"), _flower_reply(_tg(SPOUSE_ID, "رضا"))
    )
    gift_id = await _pending_gift_id(db_session)

    await handle_flower_reject(
        _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_REJECT), _bot()
    )
    late = _flower_callback(gift_id, _tg(SPOUSE_ID, "رضا"), LOVE_FLOWER_ACCEPT)
    await handle_flower_accept(late, _bot())

    late.answer.assert_awaited_once_with(FLOWER_ALREADY_RESOLVED_TEXT, show_alert=True)
    assert await LoveService.get_love_score(db_session, SENDER_ID) == 9
    # Refunded exactly once.
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 1


# ---------------------------------------------------------------------------
# 7. Scope guards
# ---------------------------------------------------------------------------


def test_flower_handler_adds_no_locking_of_its_own():
    """Concurrency is the domain's job, not the handler's."""
    import inspect

    from sib.handlers import marriage as handlers

    source = inspect.getsource(handlers._resolve_flower_callback)
    for forbidden in ("with_for_update", "SELECT ... FOR UPDATE", "LockService"):
        assert forbidden not in source


def test_flower_handler_never_writes_love_itself():
    """The handler renders the domain's number; it never computes one."""
    import inspect

    from sib.handlers import marriage as handlers

    source = inspect.getsource(handlers._resolve_flower_callback)
    assert "love_score =" not in source
    assert "marriage.love_score" not in source
    assert "LoveService.accept_flower" in source
    assert "LoveService.reject_flower" in source


def test_flower_router_shape():
    """Exactly the three new handlers were added."""
    from sib.handlers.marriage import router

    callbacks = {h.callback.__name__ for h in router.callback_query.handlers}
    assert {"handle_flower_accept", "handle_flower_reject"} <= callbacks
    messages = {h.callback.__name__ for h in router.message.handlers}
    assert "handle_flower_send" in messages
    # The earlier phases are untouched.
    assert {"handle_marriage_accept", "handle_marriage_reject"} <= callbacks
    assert "handle_marriage_proposal" in messages


def test_flower_economy_defaults_are_configured():
    """The economy review set a real Love gain through ConfigService."""
    from sib.services.config_store import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["flower_love_gain"] == 2
    # Untouched by this phase:
    assert CONFIG_DEFAULTS["item_buy_prices"]["wedding_ring"] == 1000
    assert CONFIG_DEFAULTS["item_buy_prices"]["flower"] == 30
    assert CONFIG_DEFAULTS["item_sell_price_ratio"] == 0.5


# ---------------------------------------------------------------------------
# Local assertion helpers
# ---------------------------------------------------------------------------


def _flower_reply(target: TelegramUser) -> MagicMock:
    replied = MagicMock(spec=Message)
    replied.from_user = target
    replied.message_id = 860
    return replied


async def _assert_no_gifts(db_session) -> None:
    from sqlalchemy import func, select

    count = await db_session.scalar(select(func.count(FlowerGift.id)))
    assert count == 0


async def _assert_no_pending_gifts(db_session) -> None:
    from sqlalchemy import func, select

    count = await db_session.scalar(
        select(func.count(FlowerGift.id)).where(
            FlowerGift.status == FlowerGiftStatus.PENDING
        )
    )
    assert count == 0
