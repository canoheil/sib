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
async def test_photo_screen_with_photo_target_swaps_media():
    """PHOTO → PHOTO: the media is swapped to the NEW screen's own art.

    (The old behavior only edited the caption, silently keeping the
    previous screen's photo around — the "photos follow the user" bug.)
    """
    bot = MagicMock()
    bot.edit_message_media = AsyncMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()

    cb = MagicMock(spec=CallbackQuery)
    cb.message = MagicMock(spec=Message)
    cb.message.message_id = 7
    cb.message.chat = MagicMock(id=9)
    cb.message.content_type = "photo"

    ok = await safe_edit_callback_message(cb, bot, "متن جدید", photo="ART-NEW")
    assert ok is True
    assert bot.edit_message_media.called
    media = bot.edit_message_media.call_args.kwargs["media"]
    assert media.media == "ART-NEW"
    assert media.caption == "متن جدید"
    assert bot.edit_message_media.call_args.kwargs["message_id"] == 7
    # Same message, no orphans.
    assert not bot.send_photo.called and not bot.send_message.called
    assert not bot.delete_message.called


@pytest.mark.asyncio
async def test_photo_screen_to_text_screen_drops_stale_media():
    """PHOTO → TEXT: the old photo must NOT survive into the text screen.

    Telegram cannot strip media in place, so the message is replaced: the
    text screen is sent fresh, the photo message is deleted, and the live
    reference moves to the new message.
    """
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    bot.delete_message = AsyncMock()
    bot.edit_message_caption = AsyncMock()

    cb = MagicMock(spec=CallbackQuery)
    cb.message = MagicMock(spec=Message)
    cb.message.message_id = 7
    cb.message.chat = MagicMock(id=9)
    cb.message.content_type = "photo"

    ok = await safe_edit_callback_message(cb, bot, "متن جدید")
    assert ok is True
    assert bot.send_message.called  # fresh text screen (no media)
    assert bot.delete_message.called  # old photo message removed
    assert not bot.edit_message_caption.called  # no caption-only half-edit
    # The live reference now points at the replacement.
    assert cb.message.message_id == 42


@pytest.mark.asyncio
async def test_photo_message_degrades_to_caption_edit_only_if_replace_fails():
    """When replacing is impossible the message is never left dead: the
    caption/keyboard still update (degraded, documented fallback)."""
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=Exception("chat is gone"))
    bot.send_photo = AsyncMock(side_effect=Exception("chat is gone"))
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


def test_banner_assets_have_no_empty_margins():
    """Banners must not carry large white/empty borders (regression guard).

    A banner may touch its edges with real artwork; a uniform empty band
    wider than 12px on any edge is a cropping regression.
    """
    from PIL import Image

    for key in BANNERS:
        path = banner_path(key)
        im = Image.open(path).convert("RGB")
        w, h = im.size
        px = im.load()
        bg = px[2, 2]

        def far(c, tol=30):
            return (abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2])) > tol

        rows = [sum(1 for x in range(0, w, 4) if far(px[x, y])) for y in range(h)]
        cols = [sum(1 for y in range(0, h, 4) if far(px[x, y])) for x in range(w)]

        top = next((i for i, v in enumerate(rows) if v >= 8), h)
        bottom = next((i for i in range(h - 1, -1, -1) if rows[i] >= 8), -1)
        left = next((i for i, v in enumerate(cols) if v >= 8), w)
        right = next((i for i in range(w - 1, -1, -1) if cols[i] >= 8), -1)

        assert top < 12, f"{key}: empty top band of {top}px"
        assert (h - 1 - bottom) < 12, f"{key}: empty bottom band of {h - 1 - bottom}px"
        assert left < 12, f"{key}: empty left band of {left}px"
        assert (w - 1 - right) < 12, f"{key}: empty right band of {w - 1 - right}px"
