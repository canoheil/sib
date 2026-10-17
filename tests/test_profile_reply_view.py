"""P2 — «پروفایلش»: view ANOTHER player's profile by replying to their message.

The trigger must be a reply to a real player's message. The output is the
privacy-safe public view: in-game stats and the sanitized first name only.
It must never show the target's Telegram ID, username or any ``tg://`` link,
and it must never auto-register the target.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message
from aiogram.types import User as TelegramUser

from sib.database import Group, Marriage, MarriageStatus, User
from sib.handlers.start import (
    PROFILE_VIEW_NOT_REGISTERED_TEXT,
    PROFILE_VIEW_REPLY_REQUIRED_TEXT,
    PROFILE_VIEW_TARGET_IS_BOT_TEXT,
    handle_reply_profile_view,
    is_profile_view_command,
)

GROUP_ID = -1007960
GROUP_TITLE = "گروه پروفایلش"

VIEWER_ID = 8960
TARGET_ID = 8961
SPOUSE_ID = 8962
BOT_ID = 8963
UNKNOWN_ID = 8964

TARGET_FIRST_NAME = "نیکا"
TARGET_USERNAME = "nika_handle"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tg(user_id: int, first_name: str = "کاربر", *, username=None, is_bot=False):
    return TelegramUser(
        id=user_id,
        is_bot=is_bot,
        first_name=first_name,
        username=username,
    )


def _sent(message_id: int = 1190) -> MagicMock:
    sent = MagicMock(spec=Message)
    sent.message_id = message_id
    sent.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    return sent


def _command_message(viewer, *, reply_to_user=None, reply=True, text="پروفایلش"):
    """A «پروفایلش» message, optionally replying to ``reply_to_user``'s message."""
    msg = MagicMock(spec=Message)
    msg.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    msg.from_user = viewer
    msg.text = text
    msg.message_id = 1200
    msg.reply = AsyncMock(return_value=_sent())
    msg.reply_photo = AsyncMock(return_value=_sent())
    if not reply:
        msg.reply_to_message = None
    else:
        target_message = MagicMock(spec=Message)
        target_message.message_id = 1100
        target_message.from_user = reply_to_user
        target_message.sender_chat = None
        msg.reply_to_message = target_message
    return msg


def _rendered(msg: MagicMock) -> str:
    """The profile text, from a photo caption or a plain text message."""
    if msg.reply_photo.await_count:
        return msg.reply_photo.await_args.kwargs["caption"]
    return msg.reply.await_args.args[0]


async def _mk_user(session, user_id: int, **kwargs) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name=kwargs.pop("first_name", f"کاربر{user_id}"),
            username=kwargs.pop("username", None),
            language_code="fa",
            **kwargs,
        )
        session.add(user)
        await session.flush()
    return user


async def _marry(db_session, a: int, b: int, *, love_score: int = 0) -> None:
    await _mk_user(db_session, a)
    await _mk_user(db_session, b)
    group = await db_session.get(Group, GROUP_ID)
    if group is None:
        db_session.add(Group(id=GROUP_ID, title=GROUP_TITLE))
        await db_session.flush()
    db_session.add(
        Marriage(
            proposer_id=a,
            accepter_id=b,
            origin_group_id=GROUP_ID,
            status=MarriageStatus.ACTIVE,
            love_score=love_score,
        )
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# Trigger matching (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "پروفایلش",
        "  پروفایلش  ",
        "پروفایل\u200cش",  # ZWNJ typed inside the word
        "پروفایلش",  # Arabic Yeh variant of the same word
    ],
)
def test_reply_profile_view_trigger_accepts_the_exact_word(text):
    assert is_profile_view_command(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "سیب پروفایل",  # the OWNER's profile wake word, not this one
        "پروفایل",
        "پروفایلش من",
        "کسی پروفایلش رو دید؟",
        "/profile",
    ],
)
def test_reply_profile_view_trigger_rejects_other_text(text):
    assert not is_profile_view_command(text)


def test_reply_profile_view_handler_is_on_the_start_router():
    from sib.handlers import start as start_module

    callbacks = [h.callback for h in start_module.router.message.handlers]
    assert handle_reply_profile_view in callbacks


# ---------------------------------------------------------------------------
# Required behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_profile_view_works(db_session):
    """Replying «پروفایلش» to a player sends THEIR profile as a new message."""
    await _mk_user(db_session, TARGET_ID, first_name=TARGET_FIRST_NAME, username=TARGET_USERNAME)
    await db_session.commit()

    viewer = _tg(VIEWER_ID, "سارا")
    target = _tg(TARGET_ID, TARGET_FIRST_NAME, username=TARGET_USERNAME)
    msg = _command_message(viewer, reply_to_user=target)

    await handle_reply_profile_view(msg, MagicMock())

    text = _rendered(msg)
    assert f"👤 **پروفایل سیبی {TARGET_FIRST_NAME}** 🍏" in text
    assert "🎴 تعداد کارت‌ها: `0`" in text
    assert "⭐ کارت اصلی: انتخاب نشده" in text
    assert "⚔️ نبردهای سیبی:" in text
    # It is the TARGET's profile, not the viewer's.
    assert "سارا" not in text
    assert msg.reply_photo.await_count + msg.reply.await_count == 1


@pytest.mark.asyncio
async def test_reply_profile_view_no_reply(db_session):
    """«پروفایلش» without replying gives the Persian how-to message."""
    msg = _command_message(_tg(VIEWER_ID, "سارا"), reply=False)

    await handle_reply_profile_view(msg, MagicMock())

    msg.reply.assert_awaited_once_with(
        "برای دیدن پروفایل یک نفر، روی پیامش ریپلای کن و بنویس «پروفایلش»."
    )
    assert PROFILE_VIEW_REPLY_REQUIRED_TEXT == (
        "برای دیدن پروفایل یک نفر، روی پیامش ریپلای کن و بنویس «پروفایلش»."
    )
    msg.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_profile_view_target_bot(db_session):
    """Replying to a bot answers with the fun Persian refusal, no profile."""
    bot_user = _tg(BOT_ID, "ربات", username="some_bot", is_bot=True)
    msg = _command_message(_tg(VIEWER_ID, "سارا"), reply_to_user=bot_user)

    await handle_reply_profile_view(msg, MagicMock())

    msg.reply.assert_awaited_once_with("ربات‌ها که پروفایل ندارن! 😄")
    assert PROFILE_VIEW_TARGET_IS_BOT_TEXT == "ربات‌ها که پروفایل ندارن! 😄"
    msg.reply_photo.assert_not_awaited()


# ---------------------------------------------------------------------------
# Edge cases and privacy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_profile_view_unregistered_target(db_session):
    """A player who never registered has no profile; nothing is created."""
    target = _tg(UNKNOWN_ID, "ناشناس", username="ghost_handle")
    msg = _command_message(_tg(VIEWER_ID, "سارا"), reply_to_user=target)

    await handle_reply_profile_view(msg, MagicMock())

    msg.reply.assert_awaited_once_with("این کاربر هنوز توی بازی ثبت‌نام نکرده.")
    assert PROFILE_VIEW_NOT_REGISTERED_TEXT == "این کاربر هنوز توی بازی ثبت‌نام نکرده."
    # The target must never be auto-registered by a lookup.
    assert await db_session.get(User, UNKNOWN_ID) is None


@pytest.mark.asyncio
async def test_reply_profile_view_exposes_no_identity_or_links(db_session):
    """Privacy: no Telegram ID, username, tg:// link or spouse handle leaks."""
    await _marry(db_session, TARGET_ID, SPOUSE_ID, love_score=40)
    target_row = await db_session.get(User, TARGET_ID)
    target_row.first_name = TARGET_FIRST_NAME
    target_row.username = TARGET_USERNAME
    spouse_row = await db_session.get(User, SPOUSE_ID)
    spouse_row.first_name = "پریسا"
    spouse_row.username = "spouse_secret_handle"
    await db_session.commit()

    target = _tg(TARGET_ID, TARGET_FIRST_NAME, username=TARGET_USERNAME)
    msg = _command_message(_tg(VIEWER_ID, "سارا"), reply_to_user=target)

    await handle_reply_profile_view(msg, MagicMock())

    text = _rendered(msg)
    # The spouse appears as a plain first name in the target's profile.
    assert "💍 همسر: پریسا" in text
    assert "❤️ عشق سیبی:" in text
    for leaked in (
        "@",
        "tg://",
        "t.me/",
        "](",
        TARGET_USERNAME,
        "spouse_secret_handle",
        f"id={TARGET_ID}",
        str(TARGET_ID),
        str(SPOUSE_ID),
    ):
        assert leaked not in text, f"leaked {leaked!r} in the public profile"


@pytest.mark.asyncio
async def test_reply_profile_view_hides_the_private_wallet(db_session):
    """Apple and seedling balances are the owner's private economy, not stats."""
    await _mk_user(db_session, TARGET_ID, first_name=TARGET_FIRST_NAME)
    await db_session.commit()

    msg = _command_message(
        _tg(VIEWER_ID, "سارا"), reply_to_user=_tg(TARGET_ID, TARGET_FIRST_NAME)
    )
    await handle_reply_profile_view(msg, MagicMock())

    text = _rendered(msg)
    assert "🍎 سیب:" not in text
    assert "🌱 نهال:" not in text
    assert "تغییر پوسته" not in text  # no owner-only skin control


@pytest.mark.asyncio
async def test_reply_profile_view_sends_no_owner_keyboard(db_session):
    """The owner's control keyboard must not appear on someone else's profile."""
    await _mk_user(db_session, TARGET_ID, first_name=TARGET_FIRST_NAME)
    await db_session.commit()

    msg = _command_message(
        _tg(VIEWER_ID, "سارا"), reply_to_user=_tg(TARGET_ID, TARGET_FIRST_NAME)
    )
    await handle_reply_profile_view(msg, MagicMock())

    sent_kwargs = (
        msg.reply_photo.await_args.kwargs
        if msg.reply_photo.await_count
        else msg.reply.await_args.kwargs
    )
    assert sent_kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_reply_profile_view_registers_the_viewer_not_the_target(db_session):
    """Looking someone up registers the VIEWER (as every handler does)."""
    await _mk_user(db_session, TARGET_ID, first_name=TARGET_FIRST_NAME)
    await db_session.commit()

    msg = _command_message(
        _tg(VIEWER_ID, "سارا"), reply_to_user=_tg(TARGET_ID, TARGET_FIRST_NAME)
    )
    await handle_reply_profile_view(msg, MagicMock())

    assert await db_session.get(User, VIEWER_ID) is not None
    assert (await db_session.get(User, TARGET_ID)).first_name == TARGET_FIRST_NAME
