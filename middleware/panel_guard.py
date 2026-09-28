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
    LOCKED_MESSAGE,
    PanelOwnershipService,
    REJECT_MESSAGE,
    is_owner_scoped_callback,
    is_personal_panel,
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

        message = event.message
        chat = getattr(message, "chat", None) if message is not None else None
        chat_id = getattr(chat, "id", None) if chat is not None else None
        message_id = getattr(message, "message_id", None) if message is not None else None

        # Without a message handle there is no panel to protect.
        if not chat_id or not message_id or not event.from_user:
            return await handler(event, data)

        async def handle_and_move_claim():
            handled = False
            try:
                result = await handler(event, data)
                handled = True
                return result
            finally:
                # Back to the public main menu ends a personal panel, even
                # when PHOTO -> PHOTO kept the same message id. Do not carry
                # its owner lock onto a public menu (or leave a deleted id).
                if (
                    event.data == "main_menu"
                    and panel_type is not None
                    and handled
                    and result is True
                ):
                    async with get_session() as session:
                        await PanelOwnershipService.release(session, chat_id, message_id)
                else:
                    # The central renderer retargets callback.message on a
                    # real TEXT <-> PHOTO replacement. Transfer an EXISTING
                    # claim too, including wiki navigation handlers that do
                    # not claim a new panel themselves. Never claim a public
                    # message on behalf of the person who pressed its button.
                    current = event.message
                    current_id = getattr(current, "message_id", None)
                    current_chat = getattr(current, "chat", None)
                    if (
                        isinstance(current_id, int)
                        and current_id != message_id
                        and getattr(current_chat, "id", None) == chat_id
                    ):
                        async with get_session() as session:
                            claim_owner, claim_type = await PanelOwnershipService.get_panel_context(
                                session, chat_id, message_id, event.from_user.id
                            )
                            if claim_type is not None:
                                await PanelOwnershipService.move_claim(
                                    session, chat_id, message_id, current_id,
                                    claim_owner, claim_type,
                                )

        async with get_session() as session:
            owner_id, panel_type = await PanelOwnershipService.get_panel_context(
                session, chat_id, message_id, event.from_user.id
            )

            if is_personal_panel(panel_type):
                # Personal panel (profile/garden/cards/shop/trade): the
                # OWNER is the subject. Non-owners may SEE the message but
                # press nothing - not even navigation. This is the root
                # fix for cross-user panel state.
                if event.from_user.id != owner_id:
                    logger.info(
                        f"Panel guard rejected {event.data!r} from user "
                        f"{event.from_user.id} on personal panel of {owner_id}"
                    )
                    try:
                        await event.answer(LOCKED_MESSAGE, show_alert=True)
                    except Exception:  # pragma: no cover - defensive
                        pass
                    return None
                return await handle_and_move_claim()

            # Public/game surfaces (party cards, boards, shared menus) and
            # legacy messages: only owner-scoped MUTATIONS are guarded,
            # with first-writer-claims semantics (unchanged behavior).
            if not is_owner_scoped_callback(event.data):
                return await handle_and_move_claim()

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

        return await handle_and_move_claim()
