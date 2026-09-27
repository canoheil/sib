"""Banner presentation layer tests (warm painterly asset set).

Pins: photo+caption sends with graceful text fallback, caption-based edits
for photo screens, and asset integrity for every registered banner.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from sib.services.banner import BANNERS, CAPTION_LIMIT, banner_path, send_screen_reply
from sib.services.telegram_ui import safe_edit_callback_message


def _msg():
    msg = MagicMock(spec=Message)
    msg.reply = AsyncMock()
    msg.reply.return_value = MagicMock(message_id=1, chat=MagicMock(id=5))
    msg.reply_photo = AsyncMock()
    sent = MagicMock()
    sent.photo = [MagicMock(file_id="FILE123")]
    sent.message_id = 2
    sent.chat = MagicMock(id=5)
    msg.reply_photo.return_value = sent
    return msg


@pytest.mark.asyncio
async def test_send_screen_reply_uses_photo_with_caption():
    msg = _msg()
    kb = MagicMock()
    sent = await send_screen_reply(msg, "main_menu", "سلام منو", kb)
    assert msg.reply_photo.called
    kwargs = msg.reply_photo.call_args.kwargs
    assert kwargs["caption"] == "سلام منو"
    assert kwargs["reply_markup"] is kb
    assert sent is msg.reply_photo.return_value
    assert not msg.reply.called


@pytest.mark.asyncio
async def test_send_screen_reply_falls_back_for_long_captions():
    msg = _msg()
    long_text = "x" * (CAPTION_LIMIT + 1)
    await send_screen_reply(msg, "main_menu", long_text, None)
    assert not msg.reply_photo.called
    assert msg.reply.called
    assert msg.reply.call_args.args[0] == long_text


@pytest.mark.asyncio
async def test_send_screen_reply_falls_back_for_unknown_banner():
    msg = _msg()
    await send_screen_reply(msg, "nope", "متن", None)
    assert not msg.reply_photo.called
    assert msg.reply.called


@pytest.mark.asyncio
async def test_send_screen_reply_falls_back_when_photo_send_fails():
    msg = _msg()
    msg.reply_photo = AsyncMock(side_effect=Exception("boom"))
    await send_screen_reply(msg, "main_menu", "متن", None)
    assert msg.reply.called  # graceful text fallback


@pytest.mark.asyncio
async def test_photo_screens_edit_caption_not_text():
    """Photo screens keep their art; only the caption/keyboard are edited."""
    bot = MagicMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()

    cb = MagicMock(spec=CallbackQuery)
    cb.message = MagicMock(spec=Message)
    cb.message.message_id = 7
    cb.message.chat = MagicMock(id=9)
    cb.message.content_type = "photo"

    ok = await safe_edit_callback_message(cb, bot, "متن جدید")
    assert ok is True
    assert bot.edit_message_caption.called
    assert not bot.edit_message_text.called
    kwargs = bot.edit_message_caption.call_args.kwargs
    assert kwargs["caption"] == "متن جدید"
    assert kwargs["chat_id"] == 9
    assert kwargs["message_id"] == 7


@pytest.mark.asyncio
async def test_text_screens_keep_editing_text():
    bot = MagicMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()

    cb = MagicMock(spec=CallbackQuery)
    cb.message = MagicMock(spec=Message)
    cb.message.message_id = 7
    cb.message.chat = MagicMock(id=9)
    cb.message.content_type = "text"

    ok = await safe_edit_callback_message(cb, bot, "متن جدید")
    assert ok is True
    assert bot.edit_message_text.called
    assert not bot.edit_message_caption.called


def test_every_registered_banner_asset_exists():
    for key in BANNERS:
        path = banner_path(key)
        assert path is not None, f"banner asset missing for '{key}'"
        assert path.is_file()
        assert path.stat().st_size > 1000
