"""Native Telegram copying with a process-wide conservative rate limit."""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from sqlalchemy import select
from sib.database.models import Group, AdminLog
from sib.admin.auth import is_admin
from sib.services.sib_claim import fa_digits

logger = logging.getLogger(__name__)
_send_lock = asyncio.Lock()

@dataclass
class BroadcastResult:
    success: int = 0
    failures: list = field(default_factory=list)

    def summary(self):
        lines = [f"✅ ارسال شد: {self.success}", f"❌ ناموفق: {len(self.failures)}"]
        if self.failures:
            lines.append("دلایل:")
            lines.extend(f"{gid}: {reason}" for gid, reason in self.failures[:10])
        return fa_digits("\n".join(lines))

async def broadcast(bot, session, admin_id, source_chat_id, message_id,
                    target_chat_id=None, media_type="text"):
    if not is_admin(admin_id):
        raise ValueError("فقط مدیران اجازه دسترسی دارند.")
    query = select(Group).where(Group.is_active.is_(True))
    if target_chat_id is not None:
        query = query.where(Group.id == target_chat_id)
    groups = list((await session.scalars(query)).all())
    result = BroadcastResult()
    if target_chat_id is not None and not groups:
        result.failures.append((target_chat_id, "گروه فعال ثبت نشده است"))
    for group in groups:
        try:
            # All broadcasts share the lock, including flood-wait retries.
            async with _send_lock:
                try:
                    for attempt in range(2):
                        try:
                            await bot.copy_message(chat_id=group.id, from_chat_id=source_chat_id,
                                                   message_id=message_id)
                            break
                        except TelegramRetryAfter as exc:
                            if attempt:
                                raise
                            await asyncio.sleep(max(0.05, exc.retry_after))
                finally:
                    # Failed requests count toward Telegram's rate budget too.
                    await asyncio.sleep(0.05)
            result.success += 1
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Broadcast skipped group %s: %s", group.id, exc)
            if isinstance(exc, TelegramForbiddenError) or any(
                word in str(exc).lower() for word in ("kicked", "chat not found", "not a member")
            ):
                group.is_active = False
            result.failures.append((group.id, "ربات دسترسی ندارد یا پیام قابل ارسال نیست"))
        except Exception:
            logger.exception("Broadcast failed for %s", group.id)
            result.failures.append((group.id, "خطای ارسال"))
            await asyncio.sleep(0.05)
    session.add(AdminLog(admin_id=admin_id, action="broadcast", details=json.dumps({
        "target_type": "one" if target_chat_id is not None else "all",
        "target_chat_id": target_chat_id, "media_type": media_type,
        "group_count": len(groups), "success_count": result.success,
        "failure_count": len(result.failures)}, ensure_ascii=False)))
    await session.commit()
    return result
