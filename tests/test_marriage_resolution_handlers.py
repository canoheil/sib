"""Phase 7.2 — Telegram ACCEPT / REJECT callbacks for marriage proposals.

The two handlers under test are thin Telegram adapters: they parse the
Phase 7.1 callback data, hand the proposal id plus ``callback.from_user.id``
to the domain, and render whatever comes back. Every business rule — ring
consumption, marriage creation, Love initialisation, cooldowns, rival-proposal
invalidation and concurrency — belongs to the services, so these tests assert
that the handler *propagates* the domain outcome correctly and never lets a
third party act on someone else's proposal.

Accept/Reject handlers were intentionally absent in Phase 7.1; the buttons
already existed. This file covers their behaviour.
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
    ACCEPT_ANSWER,
    ALREADY_RESOLVED_TEXT,
    EXPIRED_TEXT,
    INVALID_BUTTON_TEXT,
    NOT_ACCEPTABLE_TEXT,
    NOT_YOUR_PROPOSAL_TEXT,
    RACE_LOST_TEXT,
    RING_SOLD_TEXT,
    handle_marriage_accept,
    handle_marriage_reject,
)
from sib.keyboards.marriage import (
    MARRIAGE_ACCEPT,
    MARRIAGE_REJECT,
    marriage_proposal_callback,
)
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.marriage import MarriageService
from sib.services.marriage_proposal import MarriageProposalService

GROUP_ID = -1007801
GROUP_TITLE = "گروه ازدواج سیب"
CARD_MESSAGE_ID = 780

PROPOSER_ID = 7800
TARGET_ID = 7801
THIRD_PARTY_ID = 7802
OTHER_ID = 7803


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_bot() -> MagicMock:
    """A bot whose edit/notify calls are awaitable (see test_trade_group_flow)."""
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.send_message = AsyncMock()
    return bot


def _callback(
    actor_id: int, data: str, *, chat_id: int = GROUP_ID, message_id: int = CARD_MESSAGE_ID
) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(
        id=actor_id, is_bot=False, first_name=f"کاربر{actor_id}"
    )
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type="supergroup", title=GROUP_TITLE)
    cb.message.message_id = message_id
    cb.message.text = "proposal card"
    cb.message.reply_markup = None
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _answer_text(cb: MagicMock) -> str:
    assert cb.answer.await_count >= 1, "the handler never answered the callback"
    call = cb.answer.call_args_list[0]
    text = call.args[0] if call.args else call.kwargs.get("text", "")
    return text or ""


def _edited(bot: MagicMock):
    """(text, reply_markup) of the card rewrite, or (None, None)."""
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


async def _mk_marriage(session, a: int, b: int, *, group_id: int = GROUP_ID) -> Marriage:
    await _mk_user(session, a)
    await _mk_user(session, b)
    await _mk_group(session, group_id)
    marriage = Marriage(
        proposer_id=a,
        accepter_id=b,
        origin_group_id=group_id,
        status=MarriageStatus.ACTIVE,
        love_score=0,
    )
    session.add(marriage)
    await session.flush()
    return marriage


async def _pending_proposal(
    db_session, proposer: int, target: int, *, ring: int = 1
) -> int:
    """Both users + group + (optional) ring, then a PENDING proposal."""
    await _mk_user(db_session, proposer)
    await _mk_user(db_session, target)
    await _mk_group(db_session)
    if ring:
        await ItemService.grant(db_session, proposer, "wedding_ring", ring)
    await db_session.commit()

    proposal = await MarriageProposalService.create_proposal(
        db_session, proposer, target, GROUP_ID
    )
    await db_session.commit()
    return int(proposal.id)


async def _reload(db_session, proposal_id: int):
    db_session.expunge_all()
    return await db_session.get(MarriageProposal, proposal_id)


async def _active_marriage_count(db_session) -> int:
    return int(
        await db_session.scalar(
            select(func.count(Marriage.id)).where(
                Marriage.status == MarriageStatus.ACTIVE
            )
        )
        or 0
    )


# ---------------------------------------------------------------------------
# 1-2. Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_successfully_accepts(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    bot = _new_bot()
    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, bot)

    # Callback UX
    assert _answer_text(cb) == ACCEPT_ANSWER

    # Domain outcome actually landed
    proposal = await _reload(db_session, proposal_id)
    assert proposal.status == ProposalStatus.ACCEPTED
    assert await _active_marriage_count(db_session) == 1
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 0

    marriage = (
        await db_session.scalars(
            select(Marriage).where(Marriage.status == MarriageStatus.ACTIVE)
        )
    ).first()
    assert {marriage.user1_id, marriage.user2_id} == {PROPOSER_ID, TARGET_ID}
    assert marriage.proposer_id == PROPOSER_ID
    assert marriage.accepter_id == TARGET_ID
    assert marriage.origin_group_id == GROUP_ID
    # Love starts at the configured initial value.
    love_initial = int(await ConfigService.get(db_session, "love_initial") or 0)
    assert marriage.love_score == love_initial

    # The card is rewritten with the outcome and its buttons removed.
    text, markup = _edited(bot)
    assert text is not None and "ازدواج انجام شد" in text
    assert f"کاربر{PROPOSER_ID}" in text and f"کاربر{TARGET_ID}" in text
    assert markup is None

    # Both users are told in the group.
    assert f"کاربر{PROPOSER_ID}" in _notified(cb)
    assert f"کاربر{TARGET_ID}" in _notified(cb)


@pytest.mark.asyncio
async def test_target_successfully_rejects(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    bot = _new_bot()
    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id))
    await handle_marriage_reject(cb, bot)

    assert "رد" in _answer_text(cb)

    proposal = await _reload(db_session, proposal_id)
    assert proposal.status == ProposalStatus.REJECTED
    assert proposal.is_terminal
    assert await _active_marriage_count(db_session) == 0
    # The ring is never consumed by a rejection.
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1

    text, markup = _edited(bot)
    assert text is not None and "رد شد" in text
    assert markup is None


@pytest.mark.asyncio
async def test_rejection_is_final_and_cannot_be_repeated(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id))
    await handle_marriage_reject(cb, _new_bot())

    again = _callback(
        TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id)
    )
    await handle_marriage_reject(again, _new_bot())

    assert _answer_text(again) == ALREADY_RESOLVED_TEXT
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.REJECTED


# ---------------------------------------------------------------------------
# 3-6. Authorization: only the target may resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_third_party_cannot_accept(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)
    await _mk_user(db_session, THIRD_PARTY_ID)
    await db_session.commit()

    bot = _new_bot()
    cb = _callback(
        THIRD_PARTY_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id)
    )
    await handle_marriage_accept(cb, bot)

    assert _answer_text(cb) == NOT_YOUR_PROPOSAL_TEXT
    proposal = await _reload(db_session, proposal_id)
    assert proposal.status == ProposalStatus.PENDING
    assert await _active_marriage_count(db_session) == 0
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1
    # A stranger must not be able to touch the card at all.
    assert not bot.edit_message_text.called
    assert not bot.edit_message_reply_markup.called


@pytest.mark.asyncio
async def test_third_party_cannot_reject(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)
    await _mk_user(db_session, THIRD_PARTY_ID)
    await db_session.commit()

    bot = _new_bot()
    cb = _callback(
        THIRD_PARTY_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id)
    )
    await handle_marriage_reject(cb, bot)

    assert _answer_text(cb) == NOT_YOUR_PROPOSAL_TEXT
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.PENDING
    assert not bot.edit_message_text.called
    assert not bot.edit_message_reply_markup.called


@pytest.mark.asyncio
async def test_proposer_cannot_accept(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    bot = _new_bot()
    cb = _callback(PROPOSER_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, bot)

    assert _answer_text(cb) == NOT_YOUR_PROPOSAL_TEXT
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.PENDING
    assert await _active_marriage_count(db_session) == 0
    assert not bot.edit_message_text.called


@pytest.mark.asyncio
async def test_proposer_cannot_reject(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    cb = _callback(PROPOSER_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id))
    await handle_marriage_reject(cb, _new_bot())

    assert _answer_text(cb) == NOT_YOUR_PROPOSAL_TEXT
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.PENDING


# ---------------------------------------------------------------------------
# 7-8. Stale buttons: expired / already resolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_proposal_is_refused_and_buttons_are_cleared(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    proposal = await db_session.get(MarriageProposal, proposal_id)
    proposal.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    bot = _new_bot()
    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, bot)

    assert _answer_text(cb) == EXPIRED_TEXT
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.EXPIRED
    assert await _active_marriage_count(db_session) == 0
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1
    # Stale buttons are stripped; the card text is left alone.
    assert not bot.edit_message_text.called
    assert bot.edit_message_reply_markup.called
    assert bot.edit_message_reply_markup.call_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_reject_after_expiration_is_refused(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    proposal = await db_session.get(MarriageProposal, proposal_id)
    proposal.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id))
    await handle_marriage_reject(cb, _new_bot())

    assert _answer_text(cb) == EXPIRED_TEXT
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.EXPIRED


@pytest.mark.asyncio
async def test_already_accepted_proposal_cannot_be_accepted_again(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    first = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(first, _new_bot())
    assert _answer_text(first) == ACCEPT_ANSWER

    bot = _new_bot()
    second = _callback(
        TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id)
    )
    await handle_marriage_accept(second, bot)

    assert _answer_text(second) == NOT_ACCEPTABLE_TEXT
    # Still exactly one marriage and one consumed ring - no double spend.
    assert await _active_marriage_count(db_session) == 1
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 0
    assert bot.edit_message_reply_markup.called


@pytest.mark.asyncio
async def test_accept_after_rejection_is_refused(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    reject_cb = _callback(
        TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id)
    )
    await handle_marriage_reject(reject_cb, _new_bot())

    accept_cb = _callback(
        TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id)
    )
    await handle_marriage_accept(accept_cb, _new_bot())

    assert _answer_text(accept_cb) == NOT_ACCEPTABLE_TEXT
    assert await _active_marriage_count(db_session) == 0
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_unknown_proposal_id_is_refused_without_a_crash(db_session):
    await _mk_user(db_session, TARGET_ID)
    await db_session.commit()

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, 987654))
    await handle_marriage_accept(cb, _new_bot())

    answered = _answer_text(cb)
    assert answered and "پیدا نشد" in answered
    assert await _active_marriage_count(db_session) == 0


# ---------------------------------------------------------------------------
# 9-11. Domain refusals surfaced by the handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ring_sold_before_acceptance(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    # The proposer sells the ring while the proposal is pending.
    await ItemService.consume(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    bot = _new_bot()
    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, bot)

    assert _answer_text(cb) == RING_SOLD_TEXT
    assert "غیبش زد" in _answer_text(cb)
    # No marriage, nothing consumed, card left editable for a later retry.
    assert await _active_marriage_count(db_session) == 0
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 0
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.PENDING
    assert not bot.edit_message_text.called
    assert not bot.edit_message_reply_markup.called


@pytest.mark.asyncio
async def test_target_already_married(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)
    # The target marries somebody else while the proposal is pending.
    await _mk_marriage(db_session, TARGET_ID, OTHER_ID, group_id=GROUP_ID)
    await db_session.commit()

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, _new_bot())

    assert "متأهل" in _answer_text(cb)
    assert await _active_marriage_count(db_session) == 1
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.PENDING
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_proposer_already_married(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)
    await _mk_marriage(db_session, PROPOSER_ID, OTHER_ID, group_id=GROUP_ID)
    await db_session.commit()

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, _new_bot())

    assert "خواستگار" in _answer_text(cb)
    assert await _active_marriage_count(db_session) == 1
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_cooldown_prevents_acceptance(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)
    # The target is inside the post-divorce cooldown.
    marriage = await _mk_marriage(db_session, TARGET_ID, OTHER_ID, group_id=GROUP_ID)
    marriage.status = MarriageStatus.DIVORCED
    marriage.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=1)
    await db_session.commit()

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, _new_bot())

    text = _answer_text(cb)
    assert "صبر کنی" in text
    assert await _active_marriage_count(db_session) == 0
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_proposer_cooldown_is_reported_in_the_third_person(db_session):
    """The presser is the target, so a proposer cooldown is not 'you wait'."""
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)
    marriage = await _mk_marriage(db_session, PROPOSER_ID, OTHER_ID, group_id=GROUP_ID)
    marriage.status = MarriageStatus.DIVORCED
    marriage.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=1)
    await db_session.commit()

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, _new_bot())

    text = _answer_text(cb)
    assert "خواستگار" in text
    assert "صبر کنی" not in text
    assert await _active_marriage_count(db_session) == 0


# ---------------------------------------------------------------------------
# 12-13. Concurrency — the handler must propagate the domain result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_simultaneous_accepts_create_at_most_one_marriage(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    first = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    second = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))

    await asyncio.gather(
        handle_marriage_accept(first, _new_bot()),
        handle_marriage_accept(second, _new_bot()),
    )

    # Both callbacks were answered with a DOMAIN outcome - never a crash.
    answers = [_answer_text(first), _answer_text(second)]
    assert all(answers)
    for answer in answers:
        assert answer == ACCEPT_ANSWER or answer in {
            RACE_LOST_TEXT,
            NOT_ACCEPTABLE_TEXT,
            ALREADY_RESOLVED_TEXT,
            EXPIRED_TEXT,
        }, answer

    # The invariant: at most one marriage, and the ring is spent at most once.
    assert await _active_marriage_count(db_session) <= 1
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") in (0, 1)
    if await _active_marriage_count(db_session) == 1:
        assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 0


@pytest.mark.asyncio
async def test_accept_versus_reject_settle_the_proposal_once(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    accept_cb = _callback(
        TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id)
    )
    reject_cb = _callback(
        TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id)
    )

    await asyncio.gather(
        handle_marriage_accept(accept_cb, _new_bot()),
        handle_marriage_reject(reject_cb, _new_bot()),
    )

    answers = [_answer_text(accept_cb), _answer_text(reject_cb)]
    assert all(answers)

    proposal = await _reload(db_session, proposal_id)
    assert proposal.status in (ProposalStatus.ACCEPTED, ProposalStatus.REJECTED)
    if proposal.status == ProposalStatus.ACCEPTED:
        assert await _active_marriage_count(db_session) == 1
        assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 0
    else:
        assert await _active_marriage_count(db_session) == 0
        assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_accept_loses_a_race_against_a_concurrent_resolution(db_session):
    """Deterministic race: the proposal is resolved *inside* the domain call.

    ``MarriageAcceptanceRaceLostError`` is what the service raises when a
    concurrent writer resolved or invalidated the proposal first; the
    handler must surface it as Persian copy and not retry or crash.
    """
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)
    original = MarriageService.accept_proposal

    async def _racing_accept(session, pid, actor_id, *, now=None):
        # A concurrent writer rejects the proposal right before the domain
        # re-reads it under lock.
        await MarriageProposalService.reject_proposal(session, pid, actor_id)
        await session.flush()
        return await original(session, pid, actor_id, now=now)

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    monkeypatched = pytest.MonkeyPatch()
    try:
        monkeypatched.setattr(
            MarriageService, "accept_proposal", staticmethod(_racing_accept)
        )
        await handle_marriage_accept(cb, _new_bot())
    finally:
        monkeypatched.undo()

    assert _answer_text(cb) in {RACE_LOST_TEXT, NOT_ACCEPTABLE_TEXT}
    assert await _active_marriage_count(db_session) == 0
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1


# ---------------------------------------------------------------------------
# 14-15. Telegram UX: card cleanup + proposer notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_removes_the_buttons_from_the_card(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    bot = _new_bot()
    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, bot)

    assert bot.edit_message_text.called
    call = bot.edit_message_text.call_args
    assert call.kwargs.get("reply_markup") is None
    # The edit is addressed at the card that was pressed.
    assert call.kwargs.get("chat_id") == GROUP_ID
    assert call.kwargs.get("message_id") == CARD_MESSAGE_ID


@pytest.mark.asyncio
async def test_proposer_is_notified_on_rejection(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id))
    await handle_marriage_reject(cb, _new_bot())

    notification = _notified(cb)
    assert notification
    assert f"کاربر{PROPOSER_ID}" in notification
    assert f"کاربر{TARGET_ID}" in notification


@pytest.mark.asyncio
async def test_a_failed_notification_does_not_break_the_outcome(db_session):
    """Presentation failures must never roll back committed domain state."""
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id))
    cb.message.answer = AsyncMock(side_effect=RuntimeError("chat unreachable"))
    await handle_marriage_reject(cb, _new_bot())

    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.REJECTED


# ---------------------------------------------------------------------------
# Callback-data safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_callback_data_is_rejected(db_session):
    await _mk_user(db_session, TARGET_ID)
    await db_session.commit()

    for data in ("marriage_accept:", "marriage_accept:abc", "marriage_accept:-1", ""):
        cb = _callback(TARGET_ID, data)
        bot = _new_bot()
        await handle_marriage_accept(cb, bot)
        assert _answer_text(cb) == INVALID_BUTTON_TEXT
        assert not bot.edit_message_text.called

    assert await _active_marriage_count(db_session) == 0


@pytest.mark.asyncio
async def test_reject_data_cannot_drive_the_accept_handler(db_session):
    """Even if routing slipped, an accept handler never acts on reject data."""
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_REJECT, proposal_id))
    await handle_marriage_accept(cb, _new_bot())

    assert _answer_text(cb) == INVALID_BUTTON_TEXT
    assert (await _reload(db_session, proposal_id)).status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_no_internal_details_leak_into_callback_answers(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)
    await ItemService.consume(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    await handle_marriage_accept(cb, _new_bot())

    text = _answer_text(cb)
    for leaked in ("Error", "Exception", "Traceback", "wedding_ring", "sib."):
        assert leaked not in text


# ---------------------------------------------------------------------------
# Router wiring
# ---------------------------------------------------------------------------


def test_both_resolution_handlers_are_registered():
    from sib.handlers import marriage_router

    names = {h.callback.__name__ for h in marriage_router.callback_query.handlers}
    assert {"handle_marriage_accept", "handle_marriage_reject"} <= names
    # The divorce confirmation handlers (7.3) live on the same router.
    assert {
        "handle_marriage_divorce_confirm",
        "handle_marriage_divorce_cancel",
    } <= names
    # 7.6 flower buttons share the router too.
    assert {
        "handle_flower_accept",
        "handle_flower_reject",
    } <= names
    # 7.1 proposal trigger + 7.3 divorce trigger + 7.6 flower trigger.
    assert len(marriage_router.message.handlers) == 3


def test_handlers_delegate_to_the_domain_api_without_adding_locks():
    """Guard rail: the resolution path uses the domain, not ad-hoc writes."""
    assert hasattr(MarriageService, "accept_proposal")
    assert hasattr(MarriageProposalService, "reject_proposal")
    source = _module_source()
    for forbidden in ("session.commit()", "for_update", "BEGIN"):
        assert forbidden not in source


def _module_source() -> str:
    import inspect

    import sib.handlers.marriage as marriage_module

    return inspect.getsource(marriage_module)
