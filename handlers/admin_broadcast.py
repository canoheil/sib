"""Admin-only broadcast draft, preview and confirmation flow."""
import logging
from dataclasses import dataclass, field
from time import monotonic
from aiogram import Router, F
from aiogram.filters import Command
from sib.admin.auth import is_admin
from sib.database.connection import get_session
from sib.keyboards.common import keyboard_from_specs
from sib.services.broadcast import broadcast

router = Router(name="admin_broadcast")
logger = logging.getLogger(__name__)
CONTENT = "پیام خود را بفرستید. می‌تواند متن، عکس، ویدیو، گیف، فایل، استیکر، ویس یا آهنگ باشد."

@dataclass
class PendingBroadcast:
    chat_id: int
    stage: str = "target"
    target: int | None = None
    source: int | None = None
    media_type: str = "text"
    touched: float = field(default_factory=monotonic)

_pending_broadcast: dict[int, PendingBroadcast] = {}

def pending(uid):
    for key, value in list(_pending_broadcast.items()):
        if monotonic() - value.touched > 600:
            _pending_broadcast.pop(key, None)
    return _pending_broadcast.get(uid)

def keyboard(preview=False):
    """Broadcast controls: send green, edit/targets blue, cancel red."""
    rows = [[("✅ ارسال", "send", "success"), ("✏️ ویرایش", "edit", "primary")]] if preview else [
        [("🌍 همه گروه‌ها", "target:all", "primary"), ("📍 یک گروه خاص", "target:one", "primary")]]
    rows.append([("❌ لغو", "cancel", "danger")])
    return keyboard_from_specs(
        [[(t, "bcast:"+d, style) for t, d, style in row] for row in rows])

async def authorized(event):
    if event.from_user and is_admin(event.from_user.id):
        return True
    await event.answer("⛔ فقط مدیران اجازه دسترسی دارند.")
    return False

@router.message(Command("broadcast"))
async def start(message, state=None):
    if not await authorized(message):
        return
    from sib.handlers.admin_users import clear, _lookup
    clear(message.from_user.id)
    _lookup.pop(message.from_user.id, None)
    if state is not None:
        await state.clear()
    _pending_broadcast[message.from_user.id] = PendingBroadcast(message.chat.id)
    await message.answer("📢 پیام همگانی\nمقصد را انتخاب کنید:", reply_markup=keyboard())

@router.callback_query(F.data == "admin_broadcast")
async def entry(callback, state=None):
    if not await authorized(callback):
        return
    from sib.handlers.admin_users import clear, _lookup
    clear(callback.from_user.id)
    _lookup.pop(callback.from_user.id, None)
    if state is not None:
        await state.clear()
    _pending_broadcast[callback.from_user.id] = PendingBroadcast(callback.message.chat.id)
    await callback.answer()
    await callback.message.answer("📢 پیام همگانی\nمقصد را انتخاب کنید:", reply_markup=keyboard())

@router.callback_query(F.data.startswith("bcast:"))
async def control(callback, bot):
    if not await authorized(callback):
        return
    uid = callback.from_user.id
    draft = pending(uid)
    if not draft or not callback.message or draft.chat_id != callback.message.chat.id:
        await callback.answer("درخواست منقضی شده است؛ دوباره شروع کنید.")
        return
    draft.touched = monotonic()
    action = callback.data[6:]
    try:
        if action == "cancel":
            _pending_broadcast.pop(uid, None)
            await callback.message.answer("❌ لغو شد.")
        elif action.startswith("target:") and draft.stage == "target":
            draft.stage = "id" if action == "target:one" else "content"
            await callback.message.answer("شناسه عددی گروه را وارد کنید." if draft.stage == "id" else CONTENT)
        elif action == "edit" and draft.stage == "preview":
            draft.source = None
            draft.stage = "content"
            await callback.message.answer(CONTENT)
        elif action == "send" and draft.stage == "preview" and draft.source:
            # Consume before the first await: repeated confirms cannot send twice.
            _pending_broadcast.pop(uid, None)
            await callback.answer("ارسال آغاز شد.")
            async with get_session() as session:
                result = await broadcast(bot, session, uid, draft.chat_id, draft.source,
                                         draft.target, draft.media_type)
            await callback.message.answer(result.summary(), parse_mode=None)
            return
        else:
            await callback.answer("ابتدا پیام را برای پیش‌نمایش بفرستید.")
            return
        await callback.answer()
    except Exception:
        logger.exception("Broadcast control failed")
        await callback.message.answer("خطایی رخ داد؛ دوباره شروع کنید.")

@router.message(lambda m: bool(m.from_user and not (m.text or "").startswith("/") and pending(m.from_user.id)))
async def receive(message, bot):
    if not await authorized(message):
        return
    draft = pending(message.from_user.id)
    if draft.chat_id != message.chat.id:
        return
    draft.touched = monotonic()
    try:
        if draft.stage == "id":
            try:
                target = int(message.text or "")
                if target >= 0 or target < -(2**63):
                    raise ValueError
            except ValueError:
                await message.answer("شناسه عددی معتبر گروه را وارد کنید.")
                return
            draft.target = target
            draft.stage = "content"
            await message.answer(CONTENT)
        elif draft.stage == "content":
            await bot.copy_message(chat_id=message.chat.id, from_chat_id=message.chat.id,
                                   message_id=message.message_id, reply_markup=keyboard(True))
            draft.source = message.message_id
            draft.media_type = str(message.content_type)
            draft.stage = "preview"
    except Exception:
        logger.exception("Broadcast preview failed")
        await message.answer("این پیام قابل کپی نیست؛ پیام دیگری بفرستید.")
