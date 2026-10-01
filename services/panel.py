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
    "select_match_type:",
    "create_party:",
    # Personal garden panel
    "garden_plant:",
    "garden_harvest:",
    "garden_tree:",
    "garden_magic:",
    "garden_unlock",
    "garden_harvest_all",
    "garden_refresh",
    # Shop / cards / trading (spend apples or change ownership)
    "shop_buy:",
    "shop_good:",
    "shop_good_buy:",
    "shop_sell_one:",
    "shop_sell_all:",
    "card_panel:",
    "card_upgrade:",
    "card_upgrade_do:",
    # NOTE: trade confirm/cancel are deliberately NOT owner-scoped: a trade is
    # a SHARED group message that BOTH participants must be able to press.
    # Authorization is enforced server-side by TradeService (participants
    # only, state-guarded, atomic) - the middleware is a UX nicety, not the
    # security boundary.
    # Apple battle
    "ab_join:",
)

REJECT_MESSAGE = (
    "🍏 این پنل مال شما نیست؛ فقط صاحب پنل می‌تواند آن را تغییر دهد یا ببندد."
)

# Short, friendly lock message for personal panels (spec: cross-user
# state must never leak through someone else's panel message).
LOCKED_MESSAGE = "🔒 این پنل برای کاربر دیگری باز شده."

# Panel types that are PERSONAL: the opener is the subject and only the
# owner may press ANY button (including navigation). Everything else
# (party flows, game boards) follows the game's own public rules.
PERSONAL_PANEL_TYPES = frozenset(
    {"profile", "garden", "cards", "shop", "trade", "card_panel", "panel_guard_personal"}
)


def is_personal_panel(panel_type: Optional[str]) -> bool:
    return panel_type in PERSONAL_PANEL_TYPES


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
    async def move_claim(
        session: AsyncSession,
        chat_id: int,
        old_message_id: Optional[int],
        new_message_id: Optional[int],
        user_id: int,
        panel_type: str = "generic",
    ) -> bool:
        """Transfer a panel claim to a REPLACEMENT message.

        When a screen transition replaces the message (Telegram cannot
        convert text↔photo in place), the live panel is the new message —
        its claim must move with it, otherwise the old (deleted) message_id
        would linger in the registry forever and the next press would find
        an unowned panel. Ownership (owner_user_id) is preserved.
        """
        if (
            not old_message_id
            or not new_message_id
            or old_message_id == new_message_id
        ):
            return await PanelOwnershipService.claim(
                session, chat_id, new_message_id or 0, user_id, panel_type
            )
        row = await session.get(PanelOwner, (chat_id, old_message_id))
        if row is None:
            return await PanelOwnershipService.claim(
                session, chat_id, new_message_id, user_id, panel_type
            )
        owner = row.owner_user_id
        moved_type = row.panel_type or panel_type
        await session.delete(row)
        existing = await session.get(PanelOwner, (chat_id, new_message_id))
        if existing is None:
            session.add(
                PanelOwner(
                    chat_id=chat_id,
                    message_id=new_message_id,
                    owner_user_id=owner,
                    panel_type=moved_type,
                )
            )
        await session.flush()
        return owner == user_id

    @staticmethod
    async def get_panel_context(
        session: AsyncSession,
        chat_id: int,
        message_id: int,
        fallback_user_id: int,
    ) -> Tuple[int, Optional[str]]:
        """(subject_user_id, panel_type) for a panel message.

        The SUBJECT of a personal panel is the DB-recorded owner - never
        the pressing user - so a shared message can never switch identity.
        Falls back to the pressing user on unowned (public) messages.
        """
        row = await session.get(PanelOwner, (chat_id, message_id))
        if row is not None:
            return row.owner_user_id, row.panel_type
        return fallback_user_id, None

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
