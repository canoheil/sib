"""Phase 7.1 — Telegram marriage-proposal CREATION flow.

Scope: the group wake phrase («ازدواج سیبی» / «با من ازدواج می‌کنی؟») typed
as a REPLY to the partner's message, the «قبول / رد» card, the persisted
message id, and the error mapping. Accept/Reject callbacks are a later
phase, so the buttons are asserted as data, never as behaviour.

The tests drive the real handler with mocked Telegram objects (the
project's existing handler-test architecture) against the real database
session, so every domain rule still goes through
``MarriageProposalService``.
"""

import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import (
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
from sib.handlers import marriage as marriage_module
from sib.handlers.marriage import (
    PROPOSAL_TRIGGER_PATTERN,
    handle_marriage_proposal,
)
from sib.keyboards.marriage import (
    MARRIAGE_ACCEPT,
    MARRIAGE_REJECT,
    get_marriage_proposal_keyboard,
    marriage_proposal_callback,
    parse_marriage_proposal_callback,
)
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.marriage_proposal import MarriageProposalService

GROUP_ID = -1007701
GROUP_TITLE = "گروه ازدواج سیب"
SENT_MESSAGE_ID = 5150

PROPOSER_ID = 7700
TARGET_ID = 7701
SPOUSE_ID = 7702
OTHER_ID = 7703


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tg(user_id: int, name: str = "کاربر", *, is_bot: bool = False) -> TelegramUser:
    return TelegramUser(id=user_id, is_bot=is_bot, first_name=f"{name}{user_id}")


def _sent(message_id: int = SENT_MESSAGE_ID) -> MagicMock:
    sent = MagicMock(spec=Message)
    sent.message_id = message_id
    sent.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    return sent


def _message(
    proposer: TelegramUser,
    target: TelegramUser = None,
    *,
    chat_type: str = "supergroup",
    text: str = "ازدواج سیبی",
    reply_side_effect=None,
    reply_result=None,
) -> MagicMock:
    """A group message (optionally a reply) wired for ``message.reply``."""
    msg = MagicMock(spec=Message)
    msg.chat = TelegramChat(id=GROUP_ID, type=chat_type, title=GROUP_TITLE)
    msg.from_user = proposer
    msg.text = text
    msg.message_id = 5000
    if reply_side_effect is not None:
        msg.reply = AsyncMock(side_effect=reply_side_effect)
    else:
        msg.reply = AsyncMock(
            return_value=reply_result if reply_result is not None else _sent()
        )

    if target is None:
        msg.reply_to_message = None
    else:
        replied = MagicMock(spec=Message)
        replied.from_user = target
        replied.message_id = 5001
        msg.reply_to_message = replied
    return msg


def _reply_text(msg: MagicMock) -> str:
    """The text of the FIRST reply the handler sent."""
    assert msg.reply.await_count >= 1, "the handler never replied"
    call = msg.reply.call_args_list[0]
    return call.args[0] if call.args else call.kwargs.get("text", "")


def _reply_keyboard(msg: MagicMock):
    call = msg.reply.call_args_list[0]
    return call.kwargs.get("reply_markup")


def _buttons(kb) -> list:
    return [button for row in kb.inline_keyboard for button in row]


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


async def _proposal_count(session) -> int:
    return int(await session.scalar(select(func.count(MarriageProposal.id))) or 0)


# ---------------------------------------------------------------------------
# 1. Trigger phrase (pure)
# ---------------------------------------------------------------------------


def test_trigger_matches_every_documented_phrase():
    """Spec examples plus the natural Persian spelling variants."""
    trigger = re.compile(PROPOSAL_TRIGGER_PATTERN)
    accepted = [
        "ازدواج سیبی",
        "با من ازدواج می‌کنی؟",
        "با من ازدواج میکنی؟",
        # natural variations
        "با من ازدواج می کنی؟",
        "با من ازدواج می‌کنی",
        "  ازدواج سیبی  ",
        "ازدواج سیبی؟",
        "ازدواج سيبي",  # Arabic Yeh
        "با من ازدواج مي‌كني؟",  # Arabic Yeh/Kaf + ZWNJ
    ]
    for phrase in accepted:
        assert trigger.match(phrase), phrase


def test_trigger_ignores_unrelated_messages():
    trigger = re.compile(PROPOSAL_TRIGGER_PATTERN)
    rejected = [
        "سیب",
        "سیب منو",
        "ازدواج",
        "سلام ازدواج سیبی",
        "ازدواج سیبی با سارا",
        "می‌خوام باهات ازدواج کنم",
        "",
    ]
    for phrase in rejected:
        assert trigger.match(phrase) is None, phrase


# ---------------------------------------------------------------------------
# 2. Callback data contract (pure)
# ---------------------------------------------------------------------------


def test_proposal_keyboard_has_exactly_accept_and_reject():
    kb = get_marriage_proposal_keyboard(42)
    buttons = _buttons(kb)

    assert len(buttons) == 2
    labels = [b.text for b in buttons]
    assert any("قبول" in label for label in labels)
    assert any("رد" in label for label in labels)

    data = [b.callback_data for b in buttons]
    assert marriage_proposal_callback(MARRIAGE_ACCEPT, 42) in data
    assert marriage_proposal_callback(MARRIAGE_REJECT, 42) in data


def test_callback_data_identifies_only_the_proposal():
    """The payload carries the numeric id - nothing user-controlled."""
    for action in (MARRIAGE_ACCEPT, MARRIAGE_REJECT):
        payload = marriage_proposal_callback(action, 42)
        assert payload == f"{action}:42"
        # No name / user id / group id ever leaks into the payload.
        assert "کاربر" not in payload and str(GROUP_ID) not in payload
        assert parse_marriage_proposal_callback(payload) == (action, 42)


def test_callback_parser_fails_closed_on_foreign_payloads():
    assert parse_marriage_proposal_callback("") is None
    assert parse_marriage_proposal_callback(None) is None
    assert parse_marriage_proposal_callback("marriage_accept") is None
    assert parse_marriage_proposal_callback("marriage_accept:") is None
    assert parse_marriage_proposal_callback("marriage_accept:abc") is None
    assert parse_marriage_proposal_callback("marriage_accept:-5") is None
    assert parse_marriage_proposal_callback("marriage_accept:1 2") is None
    assert parse_marriage_proposal_callback("marriage_accept:1:2") is None
    assert parse_marriage_proposal_callback("trade_ok:42") is None
    assert parse_marriage_proposal_callback("marriage_accept:42\ntrade_ok:1") is None


def test_callback_builder_rejects_unknown_actions():
    with pytest.raises(ValueError):
        marriage_proposal_callback("marriage_delete", 1)


# ---------------------------------------------------------------------------
# 3. Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_reply_creates_proposal(db_session):
    async with db_session.begin_nested():
        await _mk_user(db_session, PROPOSER_ID)
        await _mk_user(db_session, TARGET_ID)
        await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    db_session.expunge_all()
    proposals = list(
        (await db_session.scalars(select(MarriageProposal))).all()
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.proposer_id == PROPOSER_ID
    assert proposal.target_id == TARGET_ID
    assert proposal.group_id == GROUP_ID
    assert proposal.status == ProposalStatus.PENDING
    # SQLite hands back naive datetimes; MariaDB hands back aware ones.
    expires_at = (
        proposal.expires_at
        if proposal.expires_at.tzinfo
        else proposal.expires_at.replace(tzinfo=timezone.utc)
    )
    assert expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_proposal_message_contains_both_action_buttons(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    db_session.expunge_all()
    proposal = (
        await db_session.scalars(select(MarriageProposal))
    ).first()

    kb = _reply_keyboard(msg)
    assert kb is not None
    data = [b.callback_data for b in _buttons(kb)]
    assert marriage_proposal_callback(MARRIAGE_ACCEPT, proposal.id) in data
    assert marriage_proposal_callback(MARRIAGE_REJECT, proposal.id) in data

    # The card states WHO proposed, that it is a marriage proposal, that the
    # target may accept or reject, and how long it stays valid.
    text = _reply_text(msg)
    assert "پیشنهاد ازدواج" in text
    assert "کاربر" in text
    assert "قبول" in text and "رد" in text
    assert "دقیقه" in text


@pytest.mark.asyncio
async def test_expiry_text_uses_the_configured_timeout(db_session):
    """300 is never hardcoded: the configured lifetime drives the copy."""
    await ConfigService.set(db_session, "marriage_proposal_timeout_seconds", 120)
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    text = _reply_text(msg)
    assert "۲ دقیقه" in text
    assert "۵ دقیقه" not in text


@pytest.mark.asyncio
async def test_proposal_message_id_is_persisted(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    db_session.expunge_all()
    proposal = (await db_session.scalars(select(MarriageProposal))).first()
    assert proposal.message_id == SENT_MESSAGE_ID


@pytest.mark.asyncio
async def test_ring_is_not_consumed_by_creating_a_proposal(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_target_may_receive_multiple_incoming_proposals(db_session):
    """Only the PROPOSER is limited to one live outgoing proposal."""
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, OTHER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await ItemService.grant(db_session, OTHER_ID, "wedding_ring", 1)
    await db_session.commit()

    await handle_marriage_proposal(_message(_tg(PROPOSER_ID), _tg(TARGET_ID)))
    await handle_marriage_proposal(_message(_tg(OTHER_ID), _tg(TARGET_ID)))

    db_session.expunge_all()
    assert await _proposal_count(db_session) == 2
    rows = list((await db_session.scalars(select(MarriageProposal))).all())
    assert {row.target_id for row in rows} == {TARGET_ID}
    assert all(row.status == ProposalStatus.PENDING for row in rows)


# ---------------------------------------------------------------------------
# 4. Rejections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_reply_is_rejected(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID))  # no reply_to_message
    await handle_marriage_proposal(msg)

    assert "ریپلای" in _reply_text(msg)
    assert await _proposal_count(db_session) == 0


@pytest.mark.asyncio
async def test_self_reply_is_rejected(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    me = _tg(PROPOSER_ID)
    msg = _message(me, me)
    await handle_marriage_proposal(msg)

    assert "خودت" in _reply_text(msg)
    assert await _proposal_count(db_session) == 0


@pytest.mark.asyncio
async def test_bot_target_is_rejected(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(999000, "ربات", is_bot=True))
    await handle_marriage_proposal(msg)

    assert _reply_text(msg)
    assert await _proposal_count(db_session) == 0
    # The bot must not even be registered as a marriage candidate.
    assert await db_session.get(User, 999000) is None


@pytest.mark.asyncio
async def test_proposer_without_ring_is_rejected(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    assert "حلقه" in _reply_text(msg)
    assert await _proposal_count(db_session) == 0


@pytest.mark.asyncio
async def test_married_proposer_is_rejected(db_session):
    await _mk_marriage(db_session, PROPOSER_ID, SPOUSE_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    assert "متأهل" in _reply_text(msg)
    assert await _proposal_count(db_session) == 0


@pytest.mark.asyncio
async def test_married_target_is_rejected(db_session):
    await _mk_marriage(db_session, TARGET_ID, SPOUSE_ID)
    await _mk_user(db_session, PROPOSER_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    assert "متأهل" in _reply_text(msg)
    assert await _proposal_count(db_session) == 0


@pytest.mark.asyncio
async def test_proposer_inside_divorce_cooldown_is_rejected(db_session):
    marriage = await _mk_marriage(db_session, PROPOSER_ID, SPOUSE_ID)
    await _mk_user(db_session, TARGET_ID)
    await _mk_group(db_session)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.flush()
    marriage.status = MarriageStatus.DIVORCED
    marriage.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    text = _reply_text(msg)
    assert "صبر کنی" in text
    assert await _proposal_count(db_session) == 0


@pytest.mark.asyncio
async def test_duplicate_outgoing_proposal_is_rejected(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await _mk_user(db_session, OTHER_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    first = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(first)
    assert await _proposal_count(db_session) == 1

    second = _message(_tg(PROPOSER_ID), _tg(OTHER_ID))
    await handle_marriage_proposal(second)

    assert await _proposal_count(db_session) == 1
    assert "در انتظار" in _reply_text(second)


@pytest.mark.asyncio
async def test_private_chat_is_rejected(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID), chat_type="private")
    await handle_marriage_proposal(msg)

    assert "گروه" in _reply_text(msg)
    assert await _proposal_count(db_session) == 0


@pytest.mark.asyncio
async def test_reply_without_a_user_identity_is_rejected(db_session):
    """Channel posts / anonymous admins have no user to propose to."""
    await _mk_user(db_session, PROPOSER_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    msg.reply_to_message.from_user = None
    await handle_marriage_proposal(msg)

    assert _reply_text(msg)
    assert await _proposal_count(db_session) == 0


@pytest.mark.asyncio
async def test_no_exception_names_or_tracebacks_leak_to_the_group(db_session):
    """Every rejection is a short Persian sentence - never a Python name."""
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    text = _reply_text(msg)
    for leaked in ("Error", "Exception", "Traceback", "sib.", "proposal_error"):
        assert leaked not in text


# ---------------------------------------------------------------------------
# 5. Telegram send failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_does_not_leave_an_active_proposal(db_session):
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    boom = RuntimeError("Telegram is down")
    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID), reply_side_effect=boom)
    await handle_marriage_proposal(msg)

    db_session.expunge_all()
    # The proposal was created and then rolled back: no unusable PENDING
    # row is left behind to block the proposer's next attempt.
    rows = list((await db_session.scalars(select(MarriageProposal))).all())
    assert len(rows) == 1
    assert rows[0].status == ProposalStatus.INVALIDATED
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1


@pytest.mark.asyncio
async def test_proposer_can_retry_after_a_send_failure(db_session):
    """The cleanup really frees the proposer's single outgoing slot."""
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    failed = _message(
        _tg(PROPOSER_ID), _tg(TARGET_ID), reply_side_effect=RuntimeError("down")
    )
    await handle_marriage_proposal(failed)

    retry = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(retry)

    db_session.expunge_all()
    rows = list(
        (
            await db_session.scalars(
                select(MarriageProposal).where(
                    MarriageProposal.status == ProposalStatus.PENDING
                )
            )
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].message_id == SENT_MESSAGE_ID


@pytest.mark.asyncio
async def test_missing_message_id_in_the_send_result_rolls_back(db_session):
    """A delivered-looking send with no message id is treated as a failure."""
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID), reply_result=_sent(message_id=None))
    await handle_marriage_proposal(msg)

    db_session.expunge_all()
    proposal = (await db_session.scalars(select(MarriageProposal))).first()
    assert proposal.status == ProposalStatus.INVALIDATED
    assert proposal.message_id is None
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 1


# ---------------------------------------------------------------------------
# 6. Router wiring
# ---------------------------------------------------------------------------


def test_marriage_router_is_registered_exactly_once():
    """The router reaches production registration - exactly once.

    ``register_all_handlers`` attaches MODULE-LEVEL router singletons and
    aiogram refuses to attach one router to two dispatchers, so this test
    must NOT really attach anything (it would poison any later test that
    builds a dispatcher, e.g. ``test_nav_root_fixes``). Intercept
    ``include_router`` and record the calls instead.
    """
    from aiogram import Dispatcher

    from sib.handlers import marriage_router, register_all_handlers

    included: list = []
    dp = Dispatcher()
    with patch.object(
        Dispatcher, "include_router", side_effect=included.append
    ):
        register_all_handlers(dp)

    assert included.count(marriage_router) == 1
    assert marriage_router.name == "marriage"
    # No router was really attached to the throwaway dispatcher.
    assert dp.sub_routers == []


def test_service_api_used_by_the_handler_is_unchanged():
    """Guard rail: the handler relies on these two service entry points."""
    assert hasattr(MarriageProposalService, "create_proposal")
    assert hasattr(MarriageProposalService, "set_proposal_message_id")
    assert hasattr(MarriageProposalService, "invalidate_proposal")


def test_accept_and_reject_handlers_are_not_implemented_yet():
    """Scope guard: this phase ships the BUTTONS, not their handlers."""
    from sib.handlers import marriage_router

    callback_handlers = list(marriage_router.callback_query.handlers)
    assert callback_handlers == []

    message_handlers = list(marriage_router.message.handlers)
    assert len(message_handlers) == 1
    # ...and the single message handler is the proposal trigger itself.
    assert message_handlers[0].callback is handle_marriage_proposal


@pytest.mark.asyncio
async def test_no_transaction_is_open_during_the_telegram_send(db_session):
    """Row locks must never be held across network I/O.

    The proposal is committed inside ``get_session``; the announcement is
    sent only after that block closed. This instruments the handler's
    ``get_session`` and records how many sessions are in flight at the
    instant the Telegram send runs.
    """
    await _mk_user(db_session, PROPOSER_ID)
    await _mk_user(db_session, TARGET_ID)
    await ItemService.grant(db_session, PROPOSER_ID, "wedding_ring", 1)
    await db_session.commit()

    in_flight = {"count": 0, "max": 0}
    observed: list = []
    original_get_session = marriage_module.get_session

    @asynccontextmanager
    async def tracked_get_session():
        in_flight["count"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["count"])
        try:
            async with original_get_session() as session:
                yield session
        finally:
            in_flight["count"] -= 1

    async def fake_reply(*_args, **_kwargs):
        observed.append(in_flight["count"])
        return _sent()

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    msg.reply = AsyncMock(side_effect=fake_reply)

    with patch.object(marriage_module, "get_session", tracked_get_session):
        await handle_marriage_proposal(msg)

    # The transaction was really used (so the check is not vacuous) ...
    assert in_flight["max"] >= 1
    # ... and it was fully closed before the network call.
    assert observed == [0]
