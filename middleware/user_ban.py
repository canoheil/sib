"""Block new message/callback actions without changing gameplay services."""
import logging
from aiogram import BaseMiddleware
from sqlalchemy import select
from sib.database.connection import get_session
from sib.database.models import User

logger = logging.getLogger(__name__)

class UserBanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        actor = getattr(event, "from_user", None)
        if actor is None:
            return await handler(event, data)
        try:
            async with get_session() as session:
                banned = await session.scalar(select(User.is_banned).where(User.id == actor.id))
        except Exception:
            # No gameplay mutations on an unknown moderation state.
            logger.exception("User ban lookup failed")
            await event.answer("بررسی دسترسی ممکن نشد؛ کمی بعد دوباره تلاش کنید.")
            return None
        if banned:
            await event.answer("🚫 حساب شما مسدود است؛ با مدیر ربات تماس بگیرید.")
            return None
        return await handler(event, data)
