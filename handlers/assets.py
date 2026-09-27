"""Admin asset registration (spec #16).

Workflow: send (or reply to) a photo/sticker with

    /setasset <key>

The telegram file_id is stored under that key in ``bot_assets``. Features
call assets by key (e.g. 'golden_apple'); replacing the artwork later is
just another /setasset with the same key - no code change.
"""

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from sib.admin.auth import IsAdminFilter
from sib.core.logging import Events, log_event
from sib.database.connection import get_session
from sib.services.assets import AssetService

router = Router(name="assets")


def _extract_file(message: Message):
    """(type, file_id) from the message itself or its replied-to message."""
    targets = [message, message.reply_to_message]
    for msg in targets:
        if msg is None:
            continue
        if msg.sticker:
            return "sticker", msg.sticker.file_id
        if msg.photo:
            return "photo", msg.photo[-1].file_id
        if msg.animation:
            return "animation", msg.animation.file_id
        if msg.document and msg.document.mime_type and (
            msg.document.mime_type.startswith("image/")
        ):
            return "photo", msg.document.file_id
    return None, None


@router.message(Command("setasset"), IsAdminFilter())
async def handle_setasset(message: Message, bot: Bot):
    parts = (message.text or "").split(maxsplit=1)
    key = parts[1].strip() if len(parts) > 1 else ""
    if not key or not key.replace("_", "").isalnum():
        await message.reply(
            "🍏 فرمت درست: روی عکس/استیکر ریپلای کن و بزن:\n/setasset <key>\n"
            "مثلاً: `/setasset golden_apple`",
            parse_mode="Markdown",
        )
        return

    type_, file_id = _extract_file(message)
    if not file_id:
        await message.reply(
            "🍏 اول یک عکس یا استیکر بفرست (یا رویش ریپلای کن) و بعد /setasset بزن."
        )
        return

    async with get_session() as session:
        asset = await AssetService.register(
            session,
            key=key,
            file_id=file_id,
            type=type_,
            description=f"set by {message.from_user.id if message.from_user else '?'}",
        )
        await session.commit()

    log_event(
        Events.ADMIN_COMMAND_EXECUTED,
        {"user_id": message.from_user.id if message.from_user else None, "command": f"/setasset {key}"},
    )
    await message.reply(
        f"✅ ثبت شد!\n🔑 key: `{asset.key}`\n📎 type: `{asset.type}`\n\n"
        "از این پس هر جا این key استفاده شود، همین فایل نمایش داده می‌شود. "
        "برای تعویض، دوباره با همان key همین کار را بکن 🍏",
        parse_mode="Markdown",
    )
