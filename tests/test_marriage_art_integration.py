"""Phase 9.2 — Marriage & Love ASSET INTEGRATION (presentation only).

Scope: prove that each Telegram Marriage/Love flow selects and sends the
correct committed scene art from ``assets/marriage_love/``, that the
existing text / keyboard / domain behaviour is untouched, and that a failed
image upload never corrupts an already-successful domain transaction.

Deliberately NOT asserted here: any Marriage/Love business rule. The domain
suites own those. These tests only show the art is wired ON and that
Telegram failures degrade to exactly the text the flow already used.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    FSInputFile,
    Message,
    User as TelegramUser,
)
from sqlalchemy import func, select

from sib.database import (
    FlowerGift,
    FlowerGiftStatus,
    Group,
    Marriage,
    MarriageProposal,
    MarriageStatus,
    ProposalStatus,
    User,
)
from sib.handlers.marriage import (
    handle_flower_send,
    handle_marriage_accept,
    handle_marriage_divorce_request,
    handle_marriage_proposal,
)
from sib.keyboards.marriage import (
    LOVE_FLOWER_ACCEPT,
    LOVE_FLOWER_REJECT,
    MARRIAGE_ACCEPT,
    MARRIAGE_DIVORCE_CANCEL,
    MARRIAGE_DIVORCE_CONFIRM,
    MARRIAGE_REJECT,
    marriage_proposal_callback,
)
from sib.services import marriage_art
from sib.services.items import ItemService
from sib.services.marriage_art import (
    SCENES,
    scene_path,
    scene_photo,
    send_scene_answer,
    send_scene_reply,
)
from sib.services.marriage_proposal import MarriageProposalService

# A private ID range so these tests never collide with the existing
# Marriage/Love suites (the ``users`` table is not truncated between tests).
GROUP_ID = -1009750
GROUP_TITLE = "گروه هنر ازدواج"
CARD_MESSAGE_ID = 9750

PROPOSER_ID = 9751
TARGET_ID = 9752
DIVORCE_ACTOR_ID = 9761
DIVORCE_SPOUSE_ID = 9762
SENDER_ID = 9771
RECEIVER_ID = 9772

# The one and only contract: which flow renders which committed asset.
FLOW_SCENES = {
    "proposal": "proposal.jpg",
    "wedding": "wedding.jpg",
    "divorce": "divorce.jpg",
    "flower_gift": "flower_gift.jpg",
}


@pytest.fixture(autouse=True)
def _clear_file_id_cache():
    """Keep photo selection deterministic (no cached file_id bleed)."""
    marriage_art._file_id_cache.clear()
    yield
    marriage_art._file_id_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tg(user_id: int, *, is_bot: bool = False) -> TelegramUser:
    return TelegramUser(
        id=user_id, is_bot=is_bot, first_name=f"کاربر{user_id}"
    )


def _sent(message_id: int = 5000) -> MagicMock:
    sent = MagicMock(spec=Message)
    sent.message_id = message_id
    sent.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    return sent


def _message(
    actor: TelegramUser,
    target: TelegramUser = None,
    *,
    text: str = "ازدواج سیبی",
) -> MagicMock:
    """A group message with every send path wired as an AsyncMock.

    All four are AsyncMocks so assertions on ``await_count`` are meaningful
    (a bare ``MagicMock`` attribute would itself return a Mock). The photo
    path is the production path; the text path is the documented fallback.
    """
    msg = MagicMock(spec=Message)
    msg.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    msg.from_user = actor
    msg.text = text
    msg.message_id = 4000
    msg.reply = AsyncMock(return_value=_sent())
    msg.reply_photo = AsyncMock(return_value=_sent())
    msg.answer = AsyncMock(return_value=_sent())
    msg.answer_photo = AsyncMock(return_value=_sent())
    if target is None:
        msg.reply_to_message = None
    else:
        replied = MagicMock(spec=Message)
        replied.from_user = target
        replied.message_id = 4001
        msg.reply_to_message = replied
    return msg


def _callback(actor_id: int, data: str) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = _tg(actor_id)
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    cb.message.message_id = CARD_MESSAGE_ID
    cb.message.text = "marriage card"
    cb.message.reply_markup = None
    cb.message.answer = AsyncMock()
    cb.message.answer_photo = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.send_message = AsyncMock()
    return bot


def _asset_name(photo_arg) -> str:
    """Filename of an FSInputFile photo argument."""
    assert isinstance(photo_arg, FSInputFile), f"expected a file upload, got {photo_arg!r}"
    return Path(str(photo_arg.path)).name


def _callback_data(kb) -> list:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


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


async def _mk_marriage(session, a: int, b: int, *, love_score: int = 0) -> int:
    await _mk_user(session, a)
    await _mk_user(session, b)
    await _mk_group(session)
    await session.flush()
    marriage = Marriage(
        proposer_id=a,
        accepter_id=b,
        origin_group_id=GROUP_ID,
        status=MarriageStatus.ACTIVE,
        love_score=love_score,
    )
    session.add(marriage)
    await session.commit()
    return int(marriage.id)


async def _seed_proposer(session, proposer: int, target: int) -> None:
    """Users + group + one ring, but NO proposal (the handler creates it)."""
    await _mk_user(session, proposer)
    await _mk_user(session, target)
    await _mk_group(session)
    await ItemService.grant(session, proposer, "wedding_ring", 1)
    await session.commit()


async def _pending_proposal(session, proposer: int, target: int) -> int:
    await _seed_proposer(session, proposer, target)
    proposal = await MarriageProposalService.create_proposal(
        session, proposer, target, GROUP_ID
    )
    await session.commit()
    return int(proposal.id)


async def _active_marriage_count(session) -> int:
    return int(
        await session.scalar(
            select(func.count(Marriage.id)).where(
                Marriage.status == MarriageStatus.ACTIVE
            )
        )
        or 0
    )


# ---------------------------------------------------------------------------
# 1. Asset selection (pure)
# ---------------------------------------------------------------------------


def test_every_flow_scene_asset_is_committed_and_resolves():
    """Each flow key maps to a real file under assets/marriage_love/."""
    assert set(SCENES) == set(FLOW_SCENES)
    for key, filename in FLOW_SCENES.items():
        path = scene_path(key)
        assert path is not None, f"no asset resolved for scene '{key}'"
        assert path.is_file(), f"{path} is missing"
        assert path.name == filename
        # The Phase 9.1 location is the contract - not a new invented path.
        assert path.parent.name == "marriage_love"


def test_scene_path_fails_closed_on_unknown_keys():
    for key in (None, "", "unknown", "Proposal", "wedding.jpg", "../../etc/passwd"):
        assert scene_path(key) is None, key


def test_scene_photo_uploads_the_committed_asset():
    """No file_id cached yet, so every flow selects its own real file."""
    for key, filename in FLOW_SCENES.items():
        photo = scene_photo(key)
        assert isinstance(photo, FSInputFile)
        assert _asset_name(photo) == filename


def test_scene_photo_returns_none_when_there_is_no_art():
    """A missing/unknown scene degrades to text instead of crashing."""
    assert scene_photo(None) is None
    assert scene_photo("not_a_scene") is None


# ---------------------------------------------------------------------------
# 2. Send helpers: photo when possible, the old text when not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_scene_reply_sends_photo_with_caption_and_keyboard():
    msg = _message(_tg(PROPOSER_ID))
    kb = MagicMock()

    sent = await send_scene_reply(msg, "proposal", "متن کارت", reply_markup=kb)

    assert sent is msg.reply_photo.return_value
    call = msg.reply_photo.call_args
    assert _asset_name(call.args[0]) == "proposal.jpg"
    assert call.kwargs["caption"] == "متن کارت"
    assert call.kwargs["reply_markup"] is kb
    assert call.kwargs["parse_mode"] == "Markdown"
    # No duplicate text card when the art was delivered.
    assert msg.reply.await_count == 0


@pytest.mark.asyncio
async def test_send_scene_reply_degrades_to_text_when_upload_fails():
    msg = _message(_tg(PROPOSER_ID))
    msg.reply_photo = AsyncMock(side_effect=RuntimeError("upload exploded"))
    kb = MagicMock()

    sent = await send_scene_reply(msg, "divorce", "متن طلاق", reply_markup=kb)

    assert msg.reply_photo.await_count == 1
    # Falls back to EXACTLY the call the flow already made before Phase 9.2.
    assert sent is msg.reply.return_value
    call = msg.reply.call_args
    assert call.args[0] == "متن طلاق"
    assert call.kwargs["reply_markup"] is kb
    assert call.kwargs["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_send_scene_answer_degrades_to_a_plain_group_message():
    msg = _message(_tg(PROPOSER_ID))
    msg.answer = AsyncMock(return_value=_sent())
    msg.answer_photo = AsyncMock(side_effect=RuntimeError("upload exploded"))

    sent = await send_scene_answer(msg, "wedding", "🔔 تبریک!")

    assert sent is msg.answer.return_value
    call = msg.answer.call_args
    assert call.args[0] == "🔔 تبریک!"


@pytest.mark.asyncio
async def test_send_scene_answer_without_a_scene_is_the_original_call():
    """Reject notifications (and every other non-scene call) are unchanged."""
    msg = _message(_tg(PROPOSER_ID))
    msg.answer = AsyncMock(return_value=_sent())

    await send_scene_answer(msg, None, "🔔 رد شد")

    assert msg.answer_photo.await_count == 0
    assert msg.answer.call_args.args[0] == "🔔 رد شد"


@pytest.mark.asyncio
async def test_send_scene_reply_degrades_for_an_overlong_caption():
    """Telegram caps captions at 1024 units - never drop the text to show art."""
    msg = _message(_tg(PROPOSER_ID))
    long_text = "الف" * 2000  # 6000 UTF-16 units - far past the caption cap

    await send_scene_reply(msg, "proposal", long_text)

    assert msg.reply_photo.await_count == 0
    # The text is never dropped or truncated just to make room for art.
    assert msg.reply.call_args.args[0] == long_text


# ---------------------------------------------------------------------------
# 3. Handler wiring: which flow carries which asset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposal_card_carries_the_proposal_scene(db_session):
    await _seed_proposer(db_session, PROPOSER_ID, TARGET_ID)

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    await handle_marriage_proposal(msg)

    assert msg.reply_photo.await_count == 1, "the proposal card carried no art"
    call = msg.reply_photo.call_args
    assert _asset_name(call.args[0]) == "proposal.jpg"

    # The existing card is intact: same text, same «قبول / رد» buttons.
    assert "ازدواج" in call.kwargs["caption"]
    data = _callback_data(call.kwargs["reply_markup"])
    assert len(data) == 2
    assert any(d.startswith(f"{MARRIAGE_ACCEPT}:") for d in data)
    assert any(d.startswith(f"{MARRIAGE_REJECT}:") for d in data)
    assert msg.reply.await_count == 0  # no duplicate text card


@pytest.mark.asyncio
async def test_divorce_card_carries_the_divorce_scene(db_session):
    await _mk_marriage(db_session, DIVORCE_ACTOR_ID, DIVORCE_SPOUSE_ID)

    msg = _message(
        _tg(DIVORCE_ACTOR_ID), _tg(DIVORCE_SPOUSE_ID), text="طلاق سیبی"
    )
    await handle_marriage_divorce_request(msg)

    assert msg.reply_photo.await_count == 1, "the divorce card carried no art"
    call = msg.reply_photo.call_args
    assert _asset_name(call.args[0]) == "divorce.jpg"

    # The confirmation gate is untouched.
    data = _callback_data(call.kwargs["reply_markup"])
    assert any(d.startswith(f"{MARRIAGE_DIVORCE_CONFIRM}:") for d in data)
    assert any(d.startswith(f"{MARRIAGE_DIVORCE_CANCEL}:") for d in data)
    assert await _active_marriage_count(db_session) == 1  # request never divorces


@pytest.mark.asyncio
async def test_flower_card_carries_the_flower_gift_scene(db_session):
    await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    await ItemService.grant(db_session, SENDER_ID, "flower", 1)
    await db_session.commit()

    msg = _message(_tg(SENDER_ID), _tg(RECEIVER_ID), text="گل سیبی")
    await handle_flower_send(msg)

    assert msg.reply_photo.await_count == 1, "the flower card carried no art"
    call = msg.reply_photo.call_args
    assert _asset_name(call.args[0]) == "flower_gift.jpg"

    data = _callback_data(call.kwargs["reply_markup"])
    assert any(d.startswith(f"{LOVE_FLOWER_ACCEPT}:") for d in data)
    assert any(d.startswith(f"{LOVE_FLOWER_REJECT}:") for d in data)


@pytest.mark.asyncio
async def test_acceptance_posts_the_wedding_scene_to_the_group(db_session):
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    bot = _bot()
    await handle_marriage_accept(cb, bot)

    assert cb.message.answer_photo.await_count == 1, "no wedding art was posted"
    call = cb.message.answer_photo.call_args
    assert _asset_name(call.args[0]) == "wedding.jpg"
    assert "ازدواج" in call.kwargs["caption"]

    # The existing acceptance presentation is untouched...
    assert bot.edit_message_text.called
    # ...and so is the domain outcome.
    assert await _active_marriage_count(db_session) == 1
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 0


# ---------------------------------------------------------------------------
# 4. Telegram failure must never corrupt a domain transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposal_photo_failure_still_persists_the_proposal(db_session):
    """Art is decoration: a failed upload must not discard the proposal."""
    await _seed_proposer(db_session, PROPOSER_ID, TARGET_ID)

    msg = _message(_tg(PROPOSER_ID), _tg(TARGET_ID))
    msg.reply_photo = AsyncMock(side_effect=RuntimeError("upload exploded"))
    await handle_marriage_proposal(msg)

    # The card still reached the group as text...
    assert msg.reply.await_count == 1
    assert "ازدواج" in msg.reply.call_args.args[0]
    # ...and the proposal was neither discarded nor left without its handle.
    db_session.expunge_all()
    proposal = await db_session.scalar(
        select(MarriageProposal).where(
            MarriageProposal.proposer_id == PROPOSER_ID,
            MarriageProposal.status == ProposalStatus.PENDING,
        )
    )
    assert proposal is not None, "the proposal was discarded by a Telegram failure"
    assert proposal.message_id is not None


@pytest.mark.asyncio
async def test_divorce_photo_failure_still_opens_the_confirmation_card(db_session):
    await _mk_marriage(db_session, DIVORCE_ACTOR_ID, DIVORCE_SPOUSE_ID)

    msg = _message(
        _tg(DIVORCE_ACTOR_ID), _tg(DIVORCE_SPOUSE_ID), text="طلاق سیبی"
    )
    msg.reply_photo = AsyncMock(side_effect=RuntimeError("upload exploded"))
    await handle_marriage_divorce_request(msg)

    assert msg.reply.await_count == 1
    data = _callback_data(msg.reply.call_args.kwargs["reply_markup"])
    assert any(d.startswith(f"{MARRIAGE_DIVORCE_CONFIRM}:") for d in data)
    # Still married: only the confirmation card opens.
    assert await _active_marriage_count(db_session) == 1


@pytest.mark.asyncio
async def test_flower_photo_failure_still_creates_the_pending_gift(db_session):
    await _mk_marriage(db_session, SENDER_ID, RECEIVER_ID)
    await ItemService.grant(db_session, SENDER_ID, "flower", 1)
    await db_session.commit()

    msg = _message(_tg(SENDER_ID), _tg(RECEIVER_ID), text="گل سیبی")
    msg.reply_photo = AsyncMock(side_effect=RuntimeError("upload exploded"))
    await handle_flower_send(msg)

    # The flower is consumed and the gift is pending - no refund from a
    # Telegram-only failure, exactly as before the art was added.
    db_session.expunge_all()
    pending = int(
        await db_session.scalar(
            select(func.count(FlowerGift.id)).where(
                FlowerGift.status == FlowerGiftStatus.PENDING
            )
        )
        or 0
    )
    assert pending == 1
    assert await ItemService.get_total(db_session, SENDER_ID, "flower") == 0


@pytest.mark.asyncio
async def test_wedding_photo_failure_leaves_the_marriage_active(db_session):
    """The marriage must survive a failed wedding-photo delivery."""
    proposal_id = await _pending_proposal(db_session, PROPOSER_ID, TARGET_ID)

    cb = _callback(TARGET_ID, marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id))
    cb.message.answer_photo = AsyncMock(side_effect=RuntimeError("upload exploded"))
    bot = _bot()
    await handle_marriage_accept(cb, bot)

    # Degraded to the plain text follow-up...
    assert cb.message.answer.await_count == 1
    assert "ازدواج" in cb.message.answer.call_args.args[0]
    # ...but the marriage stands and the ring was consumed.
    assert await _active_marriage_count(db_session) == 1
    assert await ItemService.get_total(db_session, PROPOSER_ID, "wedding_ring") == 0
