"""Admin authorization foundation."""

import re
from functools import wraps
from typing import Callable, Any
from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery, TelegramObject
from sib.core.config import settings
from sib.core.errors import UnauthorizedAdminException
from sib.core.logging import logger, log_event, Events

#: Message commands handled on the admin routers. Only these count as a
#: "real admin command" typed by a user.
ADMIN_COMMANDS = frozenset(
    {
        "admin",
        "broadcast",
        "daily_digest",
        "disease_start",
        "hw_end",
        "hw_start",
        "hw_status",
        "inv2_dashboard",
        "inv2_preview",
        "inv2_simulate",
        "setasset",
        "users",
    }
)

#: Callback-data prefixes that belong to an admin surface. A callback outside
#: this set (e.g. the friendship buttons ``friend:accept:…`` / ``friend:reject:…``)
#: merely passes through an admin router and is NOT an access attempt.
_ADMIN_CALLBACK_RE = re.compile(r"^(?:admin|adm_|bcast:|uadm:|inv2adm:)")

#: Sent to a user who actually typed/pressed a real admin surface without rights.
NO_ADMIN_ACCESS = "⛔ شما دسترسی ادمین ندارید!"


def is_admin(user_id: int) -> bool:
    """Check if the given Telegram user ID is authorized as an administrator."""
    return user_id in settings.ADMIN_IDS


def _admin_attempt_detail(obj: TelegramObject) -> str | None:
    """The admin-targeting text of an event, or ``None`` when the event does
    not target an admin surface at all.

    Only a real admin command (``/admin`` …) or an admin callback
    (``admin_main``, ``adm_…``, ``bcast:…`` …) qualifies; anything else —
    like a friendship button routed through an admin router — must be
    refused silently so it never looks like an attack in the logs.
    """
    if isinstance(obj, CallbackQuery):
        data = getattr(obj, "data", None) or ""
        return data if _ADMIN_CALLBACK_RE.match(data) else None
    if isinstance(obj, Message):
        text = getattr(obj, "text", None) or getattr(obj, "caption", None) or ""
        if isinstance(text, str) and text.startswith("/"):
            command = text[1:].split(maxsplit=1)[0].split("@", 1)[0]
            if command in ADMIN_COMMANDS:
                return text
    return None


class IsAdminFilter(BaseFilter):
    """Aiogram filter for restricting commands/callbacks to administrators.

    Admin routers apply this filter to EVERY event they observe, so for
    non-admins it must stay silent unless the event actually targets an
    admin surface. A genuine attempt is logged with the real text/callback
    data and answered with «⛔ شما دسترسی ادمین ندارید!»; a wrongly routed
    user callback (friendship buttons) is just refused with no log noise.
    """

    async def __call__(self, obj: TelegramObject) -> bool:
        user_id = None
        if isinstance(obj, (Message, CallbackQuery)):
            user_id = obj.from_user.id if obj.from_user else None

        if user_id is None:
            return False

        if is_admin(user_id):
            return True

        detail = _admin_attempt_detail(obj)
        if detail is None:
            # Not an admin surface: refuse silently (no warning, no alert).
            return False

        logger.warning(
            f"Unauthorized admin access attempt by user_id={user_id} with data: {detail}"
        )
        try:
            if isinstance(obj, CallbackQuery):
                await obj.answer(NO_ADMIN_ACCESS, show_alert=True)
            else:
                await obj.answer(NO_ADMIN_ACCESS)
        except Exception:  # pragma: no cover - defensive
            logger.debug("could not notify user %s about missing admin rights", user_id)
        return False


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
