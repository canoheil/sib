"""Panel ownership service.

A menu/panel opened by a specific user must only be interactable/closable
by that user. Public actions (joining parties, accepting matches, playing
the active game turn) intentionally bypass this service.

Ownership semantics:
- The first user to interact with an owner-scoped control claims the panel.
- Later presses by other users are rejected with a friendly Persian alert.
- ``release`` makes a message shared again (used by shared group panels).
"""

from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models.panel import PanelOwner

# Callback data prefixes that mutate/close a personal interactive panel.
# Public flows (join_party, view_party, accept_match, game actions, ...)
# must NEVER be added to this tuple.
OWNER_SCOPED_CALLBACKS: Tuple[str, ...] = (
    "close_panel",
    "create_new_party_menu",
    "select_game:",
    "create_party:",
    # Personal garden panel
    "garden_plant:",
    "garden_harvest:",
    "garden_expand",
    "garden_refresh",
    # Shop / cards / trading (spend apples or change ownership)
    "shop_buy:",
    "card_panel:",
    "card_up:",
    "trade_ok:",
    "trade_cancel:",
)

REJECT_MESSAGE = (
    "🍏 این پنل مال شما نیست؛ فقط صاحب پنل می‌تواند آن را تغییر دهد یا ببندد."
)


def is_owner_scoped_callback(data: Optional[str]) -> bool:
    """Return True when the callback mutates/closes a personal panel."""
    if not data:
        return False
    return any(
        data == prefix or (prefix.endswith(":") and data.startswith(prefix))
        for prefix in OWNER_SCOPED_CALLBACKS
    )


class PanelOwnershipService:
    """Server-side registry of interactive panel owners."""

    @staticmethod
    async def get_owner(
        session: AsyncSession,
        chat_id: int,
        message_id: int,
    ) -> Optional[int]:
        row = await session.get(PanelOwner, (chat_id, message_id))
        return row.owner_user_id if row else None

    @staticmethod
    async def claim(
        session: AsyncSession,
        chat_id: int,
        message_id: int,
        user_id: int,
        panel_type: str = "generic",
    ) -> bool:
        """Claim ownership of a panel message.

        First writer wins. Returns True when ``user_id`` owns the panel
        afterwards (freshly claimed or already the owner).
        """
        row = await session.get(PanelOwner, (chat_id, message_id))
        if row is None:
            session.add(
                PanelOwner(
                    chat_id=chat_id,
                    message_id=message_id,
                    owner_user_id=user_id,
                    panel_type=panel_type,
                )
            )
            await session.flush()
            return True
        return row.owner_user_id == user_id

    @staticmethod
    async def release(
        session: AsyncSession,
        chat_id: int,
        message_id: int,
    ) -> None:
        """Make the message shared again (e.g. group games list)."""
        row = await session.get(PanelOwner, (chat_id, message_id))
        if row is not None:
            await session.delete(row)
            await session.flush()

    @staticmethod
    async def ensure_control(
        session: AsyncSession,
        chat_id: int,
        message_id: int,
        user_id: int,
        panel_type: str = "generic",
    ) -> bool:
        """Claim-or-verify helper used by owner-scoped handlers."""
        return await PanelOwnershipService.claim(
            session, chat_id, message_id, user_id, panel_type
        )
