"""Phase 7.3 — Telegram divorce flow (confirmation-gated).

«طلاق سیبی» as a REPLY to the spouse must only OPEN a confirmation card —
it must never divorce anybody. The divorce itself happens on «💔 بله، طلاق»,
which calls ``MarriageService.divorce``; the Love reset, the proposal/flower
invalidation and the 24-hour remarriage cooldown are all the domain's job and
are only asserted here as outcomes.

The tests drive the real handlers with mocked Telegram objects against the
real database session, following the existing handler-test architecture.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    User as TelegramUser,
)
from sqlalchemy import func, select

from sib.database import (
    Group,
    Marriage,
    MarriageProposal,
    MarriageStatus,
    ProposalStatus,
    User,
)
from sib.handlers.marriage import (
    DIVORCE_ALREADY_ENDED_TEXT,
    DIVORCE_CANCEL_ANSWER,
    DIVORCE_CANCEL_FORBIDDEN_TEXT,
    DIVORCE_GONE_TEXT,
    DIVORCE_NOT_YOURS_TEXT,
    DIVORCE_RACE_LOST_TEXT,
    DIVORCE_TRIGGER_PATTERN,
    INVALID_BUTTON_TEXT,
    NOT_MARRIED_TEXT,
    NOT_YOUR_SPOUSE_TEXT,
    handle_marriage_divorce_cancel,
    handle_marriage_divorce_confirm,
    handle_marriage_divorce_request,
)
from sib.keyboards.marriage import (
    MARRIAGE_DIVORCE_CANCEL,
    MARRIAGE_DIVORCE_CONFIRM,
    get_divorce_confirmation_keyboard,
    marriage_divorce_callback,
    parse_marriage_divorce_callback,
)
from sib.services.marriage import MarriageService

GROUP_ID = -1007901
GROUP_TITLE = "گروه طلاق سیب"
CARD_MESSAGE_ID = 7900

ACTOR_ID = 7900
SPOUSE_ID = 7901
THIRD_PARTY_ID = 7902
STRANGER_ID = 7903


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.send_message = AsyncMock()
    return bot


def _tg(user_id: int, *, is_bot: bool = False) -> TelegramUser:
    return TelegramUser(
        id=user_id, is_bot=is_bot, first_name=f"کاربر{user_id}"
    )


def _sent(message_id: int = CARD_MESSAGE_ID) -> MagicMock:
    sent = MagicMock(spec=Message)
    sent.message_id = message_id
    sent.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    return sent


def _message(
    actor: TelegramUser,
    target: TelegramUser = None,
    *,
    chat_type: str = "supergroup",
    text: str = "طلاق سیبی",
    reply_side_effect=None,
) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = TelegramChat(id=GROUP_ID, type=chat_type, title=GROUP_TITLE)
    msg.from_user = actor
    msg.text = text
    msg.message_id = 5000
    if reply_side_effect is not None:
        msg.reply = AsyncMock(side_effect=reply_side_effect)
    else:
        msg.reply = AsyncMock(return_value=_sent())
    if target is None:
        msg.reply_to_message = None
    else:
        replied = MagicMock(spec=Message)
        replied.from_user = target
        replied.message_id = 5001
        msg.reply_to_message = replied
    return msg


def _reply_text(msg: MagicMock) -> str:
    assert msg.reply.await_count >= 1, "the handler never replied"
    call = msg.reply.call_args_list[0]
    return call.args[0] if call.args else call.kwargs.get("text", "")


def _reply_keyboard(msg: MagicMock):
    return msg.reply.call_args_list[0].kwargs.get("reply_markup")


def _buttons(kb) -> list:
    return [button for row in kb.inline_keyboard for button in row]


def _callback(
    actor_id: int,
    data: str,
    *,
    chat_id: int = GROUP_ID,
    message_id: int = CARD_MESSAGE_ID,
) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = _tg(actor_id)
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type="supergroup", title=GROUP_TITLE)
    cb.message.message_id = message_id
    cb.message.text = "divorce card"
    cb.message.reply_markup = None
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _answer_text(cb: MagicMock) -> str:
    assert cb.answer.await_count >= 1, "the handler never answered the callback"
    call = cb.answer.call_args_list[0]
    return call.args[0] if call.args else call.kwargs.get("text", "") or ""


def _edited(bot: MagicMock):
    if not bot.edit_message_text.called:
        return None, None
    call = bot.edit_message_text.call_args
    text = call.args[0] if call.args else call.kwargs.get("text")
    return text, call.kwargs.get("reply_markup")


def _notified(cb: MagicMock) -> str:
    if not cb.message.answer.called:
        return ""
    call = cb.message.answer.call_args
    return call.args[0] if call.args else call.kwargs.get("text", "")


async def _mk_user(session, user_id: int, *, is_bot: bool = False) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name=f"کاربر{user_id}",
            language_code="fa",
            is_bot=is_bot,
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


async def _mk_marriage(
    session, a: int, b: int, *, love_score: int = 7, group_id: int = GROUP_ID
) -> Marriage:
    await _mk_user(session, a)
    await _mk_user(session, b)
    await _mk_group(session, group_id)
    marriage = Marriage(
        proposer_id=a,
        accepter_id=b,
        origin_group_id=group_id,
        status=MarriageStatus.ACTIVE,
        love_score=love_score,
    )
    session.add(marriage)
    await session.flush()
    return marriage


async def _active_marriage_count(db_session) -> int:
    return int(
        await db_session.scalar(
            select(func.count(Marriage.id)).where(
                Marriage.status == MarriageStatus.ACTIVE
            )
        )
        or 0
    )


async def _reload_marriage(db_session, marriage_id: int):
    db_session.expunge_all()
    return await db_session.get(Marriage, marriage_id)


async def _open_confirmation(db_session, actor_id: int = ACTOR_ID, spouse_id: int = SPOUSE_ID):
    """Run the initiation handler; returns (marriage, message, bot)."""
    marriage = await _mk_marriage(db_session, actor_id, spouse_id)
    await db_session.commit()
    msg = _message(_tg(actor_id), _tg(spouse_id))
    await handle_marriage_divorce_request(msg)
    return marriage, msg, _new_bot()


# ---------------------------------------------------------------------------
# Trigger phrase (pure)
# ---------------------------------------------------------------------------


def test_trigger_matches_every_documented_phrase():
    import re

    trigger = re.compile(DIVORCE_TRIGGER_PATTERN)
    for phrase in ("طلاق سیبی", "طلاق سیبی؟", "  طلاق سیبی  ", "طلاق سيبي"):
        assert trigger.match(phrase), phrase


def test_trigger_ignores_unrelated_messages():
    import re

    trigger = re.compile(DIVORCE_TRIGGER_PATTERN)
    for phrase in ("طلاق", "می‌خوام طلاق بگیرم", "سلام طلاق سیبی", "ازدواج سیبی", ""):
        assert trigger.match(phrase) is None, phrase


def test_divorce_keyboard_has_confirm_and_cancel():
    kb = get_divorce_confirmation_keyboard(11)
    buttons = _buttons(kb)
    assert len(buttons) == 2
    labels = [b.text for b in buttons]
    assert "بله، طلاق" in labels[0] and "انصراف" in labels[1]
    data = [b.callback_data for b in buttons]
    assert marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, 11) in data
    assert marriage_divorce_callback(MARRIAGE_DIVORCE_CANCEL, 11) in data


def test_divorce_callback_parser_fails_closed():
    for bad in (
        "",
        None,
        "marriage_divorce_confirm",
        "marriage_divorce_confirm:",
        "marriage_divorce_confirm:abc",
        "marriage_divorce_confirm:-3",
        "marriage_accept:5",
        "marriage_divorce_confirm:5:6",
    ):
        assert parse_marriage_divorce_callback(bad) is None, bad
    assert parse_marriage_divorce_callback("marriage_divorce_cancel:5") == (
        MARRIAGE_DIVORCE_CANCEL,
        5,
    )


# ---------------------------------------------------------------------------
# 1. Initiation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_divorce_initiation_by_reply(db_session):
    marriage, msg, _ = await _open_confirmation(db_session)

    assert "درخواست طلاق" in _reply_text(msg)
    # Critically: NOTHING was divorced yet.
    stored = await _reload_marriage(db_session, int(marriage.id))
    assert stored.status == MarriageStatus.ACTIVE
    assert stored.love_score == 7
    assert stored.divorced_at is None
    assert stored.cooldown_until is None
    assert await _active_marriage_count(db_session) == 1


@pytest.mark.asyncio
async def test_confirmation_card_appears_with_both_buttons(db_session):
    marriage, msg, _ = await _open_confirmation(db_session)

    kb = _reply_keyboard(msg)
    assert kb is not None
    data = [b.callback_data for b in _buttons(kb)]
    assert marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id) in data
    assert marriage_divorce_callback(MARRIAGE_DIVORCE_CANCEL, marriage.id) in data
    # The card warns about the consequences.
    assert "عشق" in _reply_text(msg)


@pytest.mark.asyncio
async def test_non_reply_is_rejected(db_session):
    await _mk_marriage(db_session, ACTOR_ID, SPOUSE_ID)
    await db_session.commit()

    msg = _message(_tg(ACTOR_ID))  # no reply
    await handle_marriage_divorce_request(msg)

    assert "ریپلای" in _reply_text(msg)
    assert _reply_keyboard(msg) is None
    assert await _active_marriage_count(db_session) == 1


@pytest.mark.asyncio
async def test_reply_to_non_spouse_is_rejected(db_session):
    await _mk_marriage(db_session, ACTOR_ID, SPOUSE_ID)
    await _mk_user(db_session, STRANGER_ID)
    await db_session.commit()

    msg = _message(_tg(ACTOR_ID), _tg(STRANGER_ID))
    await handle_marriage_divorce_request(msg)

    assert _reply_text(msg) == NOT_YOUR_SPOUSE_TEXT
    assert _reply_keyboard(msg) is None
    assert await _active_marriage_count(db_session) == 1


@pytest.mark.asyncio
async def test_unmarried_actor_is_rejected(db_session):
    await _mk_user(db_session, ACTOR_ID)
    await _mk_user(db_session, STRANGER_ID)
    await db_session.commit()

    msg = _message(_tg(ACTOR_ID), _tg(STRANGER_ID))
    await handle_marriage_divorce_request(msg)

    assert _reply_text(msg) == NOT_MARRIED_TEXT
    assert _reply_keyboard(msg) is None


@pytest.mark.asyncio
async def test_self_reply_is_rejected(db_session):
    await _mk_user(db_session, ACTOR_ID)
    await db_session.commit()

    me = _tg(ACTOR_ID)
    msg = _message(me, me)
    await handle_marriage_divorce_request(msg)

    assert _reply_text(msg) == NOT_YOUR_SPOUSE_TEXT
    assert _reply_keyboard(msg) is None


@pytest.mark.asyncio
async def test_private_chat_is_rejected(db_session):
    await _mk_marriage(db_session, ACTOR_ID, SPOUSE_ID)
    await db_session.commit()

    msg = _message(_tg(ACTOR_ID), _tg(SPOUSE_ID), chat_type="private")
    await handle_marriage_divorce_request(msg)

    assert "گروه" in _reply_text(msg)
    assert _reply_keyboard(msg) is None


@pytest.mark.asyncio
async def test_reply_without_a_user_identity_is_rejected(db_session):
    await _mk_marriage(db_session, ACTOR_ID, SPOUSE_ID)
    await db_session.commit()

    msg = _message(_tg(ACTOR_ID), _tg(SPOUSE_ID))
    msg.reply_to_message.from_user = None
    await handle_marriage_divorce_request(msg)

    assert _reply_text(msg)
    assert _reply_keyboard(msg) is None


# ---------------------------------------------------------------------------
# 2-4. Confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorized_confirm_succeeds(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)
    marriage_id = int(marriage.id)

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage_id)
    )
    await handle_marriage_divorce_confirm(cb, bot)

    assert _answer_text(cb) == "طلاق ثبت شد."

    stored = await _reload_marriage(db_session, marriage_id)
    assert stored.status == MarriageStatus.DIVORCED
    assert stored.love_score == 0  # Love reset is the domain's job
    assert stored.divorced_at is not None
    assert stored.divorced_by_user_id == ACTOR_ID
    # The 24h remarriage cooldown is owned by MarriageService.
    assert stored.cooldown_until is not None
    # SQLite hands back naive datetimes; MariaDB hands back aware ones.
    cooldown = stored.cooldown_until
    if cooldown.tzinfo is None:
        cooldown = cooldown.replace(tzinfo=timezone.utc)
    assert cooldown > datetime.now(timezone.utc)
    assert await _active_marriage_count(db_session) == 0

    # The card says the divorce happened and drops the buttons.
    text, markup = _edited(bot)
    assert text is not None and "طلاق انجام شد" in text
    assert f"کاربر{ACTOR_ID}" in text and f"کاربر{SPOUSE_ID}" in text
    assert markup is None

    # The other spouse is told.
    notification = _notified(cb)
    assert f"کاربر{ACTOR_ID}" in notification and f"کاربر{SPOUSE_ID}" in notification


@pytest.mark.asyncio
async def test_either_spouse_may_confirm(db_session):
    """The initiator does not own the confirm button - both spouses do."""
    marriage, _msg, bot = await _open_confirmation(db_session)

    cb = _callback(
        SPOUSE_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id)
    )
    await handle_marriage_divorce_confirm(cb, bot)

    assert _answer_text(cb) == "طلاق ثبت شد."
    stored = await _reload_marriage(db_session, int(marriage.id))
    assert stored.status == MarriageStatus.DIVORCED
    assert stored.divorced_by_user_id == SPOUSE_ID


@pytest.mark.asyncio
async def test_third_party_confirm_is_rejected(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)
    await _mk_user(db_session, THIRD_PARTY_ID)
    await db_session.commit()

    cb = _callback(
        THIRD_PARTY_ID,
        marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id),
    )
    await handle_marriage_divorce_confirm(cb, bot)

    assert _answer_text(cb) == DIVORCE_NOT_YOURS_TEXT
    stored = await _reload_marriage(db_session, int(marriage.id))
    assert stored.status == MarriageStatus.ACTIVE
    assert stored.love_score == 7
    # A stranger must not be able to deface the card either.
    assert not bot.edit_message_text.called
    assert not bot.edit_message_reply_markup.called


@pytest.mark.asyncio
async def test_domain_side_effects_are_propagated(db_session):
    """Proposals/gifts invalidated by the domain show up as outcomes."""
    marriage, _msg, bot = await _open_confirmation(db_session)

    # A PENDING proposal involving the actor must be invalidated by the
    # domain. It is seeded directly: ``create_proposal`` would refuse
    # outright, since a married user can neither send nor receive one.
    await _mk_user(db_session, STRANGER_ID)
    await _mk_group(db_session)
    await db_session.flush()
    proposal = MarriageProposal(
        proposer_id=STRANGER_ID,
        target_id=ACTOR_ID,
        group_id=GROUP_ID,
        status=ProposalStatus.PENDING,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(proposal)
    await db_session.commit()
    proposal_id = int(proposal.id)

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id)
    )
    await handle_marriage_divorce_confirm(cb, bot)

    db_session.expunge_all()
    stored_proposal = await db_session.get(MarriageProposal, proposal_id)
    assert stored_proposal.status == ProposalStatus.INVALIDATED


# ---------------------------------------------------------------------------
# 5. Cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initiator_can_cancel(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CANCEL, marriage.id)
    )
    await handle_marriage_divorce_cancel(cb, bot)

    assert _answer_text(cb) == DIVORCE_CANCEL_ANSWER
    # The marriage is completely untouched.
    stored = await _reload_marriage(db_session, int(marriage.id))
    assert stored.status == MarriageStatus.ACTIVE
    assert stored.love_score == 7
    assert stored.divorced_at is None
    assert await _active_marriage_count(db_session) == 1

    text, markup = _edited(bot)
    assert text is not None and "لغو شد" in text
    assert markup is None


@pytest.mark.asyncio
async def test_non_initiator_spouse_cannot_cancel(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)

    cb = _callback(
        SPOUSE_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CANCEL, marriage.id)
    )
    await handle_marriage_divorce_cancel(cb, bot)

    assert _answer_text(cb) == DIVORCE_CANCEL_FORBIDDEN_TEXT
    assert not bot.edit_message_text.called
    assert (await _reload_marriage(db_session, int(marriage.id))).status == (
        MarriageStatus.ACTIVE
    )


@pytest.mark.asyncio
async def test_third_party_cannot_cancel(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)
    await _mk_user(db_session, THIRD_PARTY_ID)
    await db_session.commit()

    cb = _callback(
        THIRD_PARTY_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CANCEL, marriage.id)
    )
    await handle_marriage_divorce_cancel(cb, bot)

    assert _answer_text(cb) == DIVORCE_CANCEL_FORBIDDEN_TEXT
    assert not bot.edit_message_text.called


# ---------------------------------------------------------------------------
# 6. Stale / raced / malformed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_confirmation_after_divorce(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)
    marriage_id = int(marriage.id)

    # The spouse already completed the divorce.
    async with _fresh_session() as session:
        await MarriageService.divorce(session, marriage_id, SPOUSE_ID)

    stale = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage_id)
    )
    await handle_marriage_divorce_confirm(stale, bot)

    assert _answer_text(stale) == DIVORCE_ALREADY_ENDED_TEXT
    # Stale buttons are stripped without rewriting the card text.
    assert not bot.edit_message_text.called
    assert bot.edit_message_reply_markup.called
    assert bot.edit_message_reply_markup.call_args.kwargs.get("reply_markup") is None
    assert await _active_marriage_count(db_session) == 0


@pytest.mark.asyncio
async def test_unknown_marriage_id_is_refused(db_session):
    await _mk_user(db_session, ACTOR_ID)
    await db_session.commit()

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, 424242)
    )
    bot = _new_bot()
    await handle_marriage_divorce_confirm(cb, bot)

    assert _answer_text(cb) == DIVORCE_GONE_TEXT
    assert bot.edit_message_reply_markup.called


@pytest.mark.asyncio
async def test_actor_no_longer_a_spouse_is_refused(db_session):
    """A divorce against a marriage the actor was never part of."""
    marriage = await _mk_marriage(db_session, SPOUSE_ID, THIRD_PARTY_ID)
    await _mk_user(db_session, ACTOR_ID)
    await db_session.commit()

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id)
    )
    bot = _new_bot()
    await handle_marriage_divorce_confirm(cb, bot)

    assert _answer_text(cb) == DIVORCE_NOT_YOURS_TEXT
    assert not bot.edit_message_reply_markup.called
    assert await _active_marriage_count(db_session) == 1


@pytest.mark.asyncio
async def test_double_confirmation_race(db_session):
    """Deterministic race: the spouse divorces inside the domain call.

    Mirrors the Phase 7.2 style - a concurrent writer is injected into the
    service so exactly one caller can win, and the loser must surface the
    domain's race-lost result rather than crash or double-apply.
    """
    marriage, _msg, _ = await _open_confirmation(db_session)
    marriage_id = int(marriage.id)

    original = MarriageService.divorce

    async def _racing_divorce(session, mid, actor_id, *, now=None):
        # A concurrent writer completes the divorce first.
        await original(session, mid, SPOUSE_ID, now=now)
        return await original(session, mid, actor_id, now=now)

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage_id)
    )
    monkeypatched = pytest.MonkeyPatch()
    try:
        monkeypatched.setattr(
            MarriageService, "divorce", staticmethod(_racing_divorce)
        )
        await handle_marriage_divorce_confirm(cb, _new_bot())
    finally:
        monkeypatched.undo()

    assert _answer_text(cb) in {DIVORCE_RACE_LOST_TEXT, DIVORCE_ALREADY_ENDED_TEXT}
    stored = await _reload_marriage(db_session, marriage_id)
    assert stored.status == MarriageStatus.DIVORCED
    # Exactly one divorce was applied, and the FIRST writer is recorded.
    assert stored.divorced_by_user_id == SPOUSE_ID
    assert await _active_marriage_count(db_session) == 0


@pytest.mark.asyncio
async def test_concurrent_confirms_produce_one_divorce(db_session):
    marriage, _msg, _ = await _open_confirmation(db_session)
    marriage_id = int(marriage.id)

    first = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage_id)
    )
    second = _callback(
        SPOUSE_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage_id)
    )
    await asyncio.gather(
        handle_marriage_divorce_confirm(first, _new_bot()),
        handle_marriage_divorce_confirm(second, _new_bot()),
    )

    answers = [_answer_text(first), _answer_text(second)]
    assert all(answers)
    for answer in answers:
        assert answer in {
            "طلاق ثبت شد.",
            DIVORCE_RACE_LOST_TEXT,
            DIVORCE_ALREADY_ENDED_TEXT,
        }, answer

    stored = await _reload_marriage(db_session, marriage_id)
    assert stored.status == MarriageStatus.DIVORCED
    assert await _active_marriage_count(db_session) == 0


@pytest.mark.asyncio
async def test_malformed_callback_data_is_rejected(db_session):
    await _mk_user(db_session, ACTOR_ID)
    await db_session.commit()

    for data in (
        "marriage_divorce_confirm:",
        "marriage_divorce_confirm:abc",
        "marriage_divorce_cancel:",
        "",
    ):
        cb = _callback(ACTOR_ID, data)
        bot = _new_bot()
        await handle_marriage_divorce_confirm(cb, bot)
        assert _answer_text(cb) == INVALID_BUTTON_TEXT
        assert not bot.edit_message_text.called

    cb = _callback(ACTOR_ID, "marriage_divorce_cancel:xyz")
    await handle_marriage_divorce_cancel(cb, _new_bot())
    assert _answer_text(cb) == INVALID_BUTTON_TEXT


@pytest.mark.asyncio
async def test_confirm_data_cannot_drive_the_cancel_handler(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id)
    )
    await handle_marriage_divorce_cancel(cb, bot)

    assert _answer_text(cb) == INVALID_BUTTON_TEXT
    assert (await _reload_marriage(db_session, int(marriage.id))).status == (
        MarriageStatus.ACTIVE
    )


# ---------------------------------------------------------------------------
# 7. UX: cleanup, notification resilience, no leaks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_removes_the_confirmation_buttons(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id)
    )
    await handle_marriage_divorce_confirm(cb, bot)

    assert bot.edit_message_text.called
    call = bot.edit_message_text.call_args
    assert call.kwargs.get("reply_markup") is None
    assert call.kwargs.get("chat_id") == GROUP_ID
    assert call.kwargs.get("message_id") == CARD_MESSAGE_ID


@pytest.mark.asyncio
async def test_notification_failure_does_not_roll_back_the_divorce(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id)
    )
    cb.message.answer = AsyncMock(side_effect=RuntimeError("chat unreachable"))
    await handle_marriage_divorce_confirm(cb, bot)

    stored = await _reload_marriage(db_session, int(marriage.id))
    assert stored.status == MarriageStatus.DIVORCED
    assert stored.love_score == 0


@pytest.mark.asyncio
async def test_edit_failure_does_not_roll_back_the_divorce(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)
    bot.edit_message_text = AsyncMock(side_effect=RuntimeError("message gone"))

    cb = _callback(
        ACTOR_ID, marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage.id)
    )
    await handle_marriage_divorce_confirm(cb, bot)

    stored = await _reload_marriage(db_session, int(marriage.id))
    assert stored.status == MarriageStatus.DIVORCED


@pytest.mark.asyncio
async def test_no_internal_details_leak_into_divorce_answers(db_session):
    marriage, _msg, bot = await _open_confirmation(db_session)
    marriage_id = int(marriage.id)

    async with _fresh_session() as session:
        await MarriageService.divorce(session, marriage_id, SPOUSE_ID)

    for handler, data in (
        (handle_marriage_divorce_confirm,
         marriage_divorce_callback(MARRIAGE_DIVORCE_CONFIRM, marriage_id)),
        (handle_marriage_divorce_cancel,
         marriage_divorce_callback(MARRIAGE_DIVORCE_CANCEL, marriage_id)),
    ):
        cb = _callback(ACTOR_ID, data)
        await handler(cb, _new_bot())
        text = _answer_text(cb)
        for leaked in ("Error", "Exception", "Traceback", "DIVORCED", "sib.", "None"):
            assert leaked not in text, (leaked, text)


@pytest.mark.asyncio
async def test_initiation_never_divorces_even_when_the_card_fails(db_session):
    """A failed confirmation send must not leave a half-open flow behind."""
    marriage = await _mk_marriage(db_session, ACTOR_ID, SPOUSE_ID)
    await db_session.commit()

    msg = _message(
        _tg(ACTOR_ID), _tg(SPOUSE_ID), reply_side_effect=RuntimeError("down")
    )
    await handle_marriage_divorce_request(msg)

    stored = await _reload_marriage(db_session, int(marriage.id))
    assert stored.status == MarriageStatus.ACTIVE
    assert await _active_marriage_count(db_session) == 1


# ---------------------------------------------------------------------------
# Scope guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_defers_to_the_domain_api(db_session):
    """The handler has no divorce logic of its own to verify: it delegates."""
    assert hasattr(MarriageService, "divorce")
    assert hasattr(MarriageService, "divorce_marriage")
    assert (
        MarriageService.divorce.__func__ is MarriageService.divorce_marriage.__func__
    )


def test_marriage_domain_is_untouched_by_this_phase():
    """Guard rail: no domain rule was added or edited for the Telegram flow."""
    import inspect

    from sib.services import marriage as marriage_service
    from sib.services import love as love_service

    for module in (marriage_service, love_service):
        source = inspect.getsource(module)
        assert "telegram" not in source.lower()
        assert "callback" not in source.lower()


def _fresh_session():
    from sib.database.connection import get_session

    return get_session()
