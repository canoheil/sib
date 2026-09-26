"""Middleware enforcing ownership of interactive panels.

Owner-scoped callbacks (closing or mutating a personal panel) are only
processed when the pressing user owns the panel message. The first user
to interact with an unclaimed panel claims it. Public flows — joining
parties (2v2/4v4), accepting matches, playing the active game turn — are
never routed through this guard.
"""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.panel import (
    PanelOwnershipService,
    REJECT_MESSAGE,
    is_owner_scoped_callback,
)


class PanelGuardMiddleware(BaseMiddleware):
    """Reject owner-scoped callbacks pressed by non-owners."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        if not is_owner_scoped_callback(event.data):
            return await handler(event, data)

        message = event.message
        chat = getattr(message, "chat", None) if message is not None else None
        chat_id = getattr(chat, "id", None) if chat is not None else None
        message_id = getattr(message, "message_id", None) if message is not None else None

        # Without a message handle there is no panel to protect.
        if not chat_id or not message_id or not event.from_user:
            return await handler(event, data)

        async with get_session() as session:
            allowed = await PanelOwnershipService.ensure_control(
                session,
                chat_id=chat_id,
                message_id=message_id,
                user_id=event.from_user.id,
                panel_type="panel_guard",
            )

        if not allowed:
            logger.info(
                f"Panel guard rejected callback {event.data!r} from user "
                f"{event.from_user.id} on message {message_id} in chat {chat_id}"
            )
            try:
                await event.answer(REJECT_MESSAGE, show_alert=True)
            except Exception:  # pragma: no cover - defensive
                pass
            return None

        return await handler(event, data)
