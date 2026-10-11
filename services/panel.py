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
    "select_mode:",
    "create_party:",
    "create_friendly_party:",
    "make_friendly:",
    # Personal garden panel
    "garden_plant:",
    "garden_harvest:",
    "garden_tree:",
    "garden_magic:",
    "garden_unlock",
    "garden_harvest_all",
    "garden_refresh",
    # Daily chest panel (consumes the once-per-day claim)
    "daily_chest:open",
    # NOTE: the GROUP chest post (group_chest, group_chest:open:<day>) is
    # deliberately NOT owner-scoped: one shared post serves every member and
    # each press rewards the presser (see handlers/daily_chest.py).
    # Profile preferences (persistent, presentation-only)
    "profile_skin",
    "profile_skin_select:",
    # Shop / cards / trading (spend apples or change ownership)
    "shop_buy:",
    "shop_good:",
    "shop_good_buy:",
    "shop_sell_one:",
    # "shop_sell_all:" only OPENS the confirmation screen; the sale itself
    # happens on "shop_sell_all_ok:" — both stay owner-scoped.
    "shop_sell_all:",
    "shop_sell_all_ok:",
    # Shop chests: the detail page is owner-scoped, and "shop_chest_open:"
    # spends apples. "shop_chests" (the list) is a view and is not scoped.
    "shop_chest:",
    "shop_chest_open:",
    "card_panel:",
    "card_main_set:",
    "card_upgrade:",
    "card_upgrade_do:",
    # Apple-battle (private battle hub)
    "ab_pick:",
    "ab_find",
    "ab_cancel",
    "ab_change",
    # NOTE: trade confirm/cancel are deliberately NOT owner-scoped: a trade is
    # a SHARED group message that BOTH participants must be able to press.
    # Authorization is enforced server-side by TradeService (participants
    # only, state-guarded, atomic) - the middleware is a UX nicety, not the
    # security boundary.
    # NOTE: ab_join is not registered - apple battle uses the real shared
    # matchmaking queue (join_party-style flows are not applicable here).
    # Friendly-party host controls (creator only; service-layer enforced too)
    "friendly_fill_bots:",
    "friendly_start:",
    "make_friendly:",
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
    {
        "profile",
        "garden",
        "cards",
        "shop",
        "trade",
        "card_panel",
        "apple_battle",
        "chest",
        "panel_guard_personal",
    }
)


class PanelAuthorizationError(PermissionError):
    """Raised when a user attempts to act on a panel they do not own.

    Handlers should answer the callback with a friendly alert and return
    without mutating state.
    """


async def resolve_personal_actor(
    callback,
) -> Tuple[int, Optional[int]]:
    """Resolve the authorized actor (subject user_id) for a callback.

    Defense-in-depth helper used at the handler/service boundary:

    * On a PERSONAL panel, returns ``(owner_user_id, message_id)`` ONLY
      when ``callback.from_user.id`` matches the recorded owner.
      Otherwise raises :class:`PanelAuthorizationError`.
    * On a public / unowned message, returns ``(presser_user_id,
      message_id)`` — the presser acts on their own state.
    * When there is no message handle (e.g. inline callbacks), returns
      ``(presser_user_id, None)``.

    This helper is the SINGLE source of truth for "who does this button
    act for?" and MUST be used instead of reading ``callback.from_user.id``
    directly in handlers that mutate personal state (garden, cards, shop,
    apple battle, trade hub, profile). The middleware already drops
    unauthorized presses before they reach handlers; this helper is the
    belt-and-braces check so that a future handler routing change or a
    callback manually forged with another user's id in its payload can
    never cause cross-user state mutation.
    """
    from sib.database.connection import get_session

    user = callback.from_user
    if user is None:
        raise PanelAuthorizationError("No acting user")
    presser_id = user.id
    message = getattr(callback, "message", None)
    chat = getattr(message, "chat", None) if message is not None else None
    chat_id = getattr(chat, "id", None) if chat is not None else None
    msg_id = getattr(message, "message_id", None) if message is not None else None

    if not chat_id or not isinstance(msg_id, int):
        return presser_id, None

    async with get_session() as session:
        owner_id, panel_type = await PanelOwnershipService.get_panel_context(
            session, chat_id, msg_id, presser_id
        )

    if is_personal_panel(panel_type) and owner_id != presser_id:
        raise PanelAuthorizationError(
            f"user {presser_id} attempted action on personal panel owned by {owner_id}"
        )

    return owner_id, msg_id


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
