"""Admin authorization foundation."""

from functools import wraps
from typing import Callable, Any
from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery, TelegramObject
from sib.core.config import settings
from sib.core.errors import UnauthorizedAdminException
from sib.core.logging import logger, log_event, Events


def is_admin(user_id: int) -> bool:
    """Check if the given Telegram user ID is authorized as an administrator."""
    return user_id in settings.ADMIN_IDS


class IsAdminFilter(BaseFilter):
    """Aiogram filter for restricting commands/callbacks to administrators."""

    async def __call__(self, obj: TelegramObject) -> bool:
        user_id = None
        if isinstance(obj, (Message, CallbackQuery)):
            user_id = obj.from_user.id if obj.from_user else None
            
        if user_id is None:
            return False
            
        authorized = is_admin(user_id)
        if not authorized:
            logger.warning(f"Unauthorized admin access attempt by user_id={user_id}")
        return authorized


def admin_required(func: Callable) -> Callable:
    """Decorator ensuring that only authorized administrators can call this async handler."""
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any):
        event = args[0] if args else None
        user_id = None
        
        if isinstance(event, (Message, CallbackQuery)) and event.from_user:
            user_id = event.from_user.id
            
        if user_id is None or not is_admin(user_id):
            log_event("ADMIN_UNAUTHORIZED_ACCESS", {"user_id": user_id})
            raise UnauthorizedAdminException()
            
        return await func(*args, **kwargs)

    return wrapper
