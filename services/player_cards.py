"""Player card service.

Ownership, favorites and queries. A new card always starts at Level 1; its
attributes are derived from the character + level (``card_progression``)
and upgrades live in ``card_upgrade.CardUpgradeService``. A player may own
at most one card per character.
"""

from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event
from sib.database.models import Character, PlayerCard, ReleaseStatus, User
from sib.services.card_progression import card_attributes

MAX_SIGNED_BIGINT = (1 << 63) - 1


def is_shop_eligible(character: Optional[Character]) -> bool:
    """A character may be sold or offered as a card reward only when it is
    released, shop-enabled, and active. Shared by shop purchases and chest
    reward validation so the rule cannot drift between them."""
    return (
        character is not None
        and bool(character.is_active)
        and bool(character.shop_enabled)
        and character.release_status == ReleaseStatus.RELEASED
    )


class CardError(ValueError):
    """Raised for invalid card operations."""


class DuplicateCardError(CardError):
    """Raised when a player already owns a card for a character."""


class StaleMainCardSelectionError(CardError):
    """Raised when the Main Card changed after the selection screen was shown."""


class PlayerCardService:
    """Manage player cards: acquire, Main Card selection, and queries."""

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------
    @classmethod
    async def acquire(
        cls, session: AsyncSession, user_id: int, character_id: int
    ) -> PlayerCard:
        character = await session.get(Character, character_id)
        if not character or not character.is_active:
            raise CardError("شخصیت موردنظر در دسترس نیست.")

        existing = await session.scalar(
            select(PlayerCard).where(
                PlayerCard.user_id == user_id, PlayerCard.character_id == character_id
            )
        )
        if existing:
            raise DuplicateCardError("شما قبلاً این شخصیت را دارید! (حداکثر یک کارت از هر شخصیت)")

        card = PlayerCard(
            user_id=user_id,
            character_id=character_id,
            level=1,
        )
        session.add(card)
        await session.flush()
        log_event(
            Events.CHARACTER_EVENT,
            {"action": "card_acquired", "user_id": user_id, "character": character.code},
        )
        return card

    # ------------------------------------------------------------------
    # Favorites / main card
    # ------------------------------------------------------------------
    @classmethod
    async def set_favorite(
        cls,
        session: AsyncSession,
        user_id: int,
        card_id: int,
        favorite: bool = True,
        *,
        expected_main_card_id: Optional[int] = None,
    ) -> PlayerCard:
        """Select or clear a player's Main Card.

        On the configured MySQL/MariaDB databases, the user's row is the
        per-player mutex, so concurrent selections are serialized even when
        the player has no favorite card row to lock. SQLite ignores
        ``FOR UPDATE`` and is not a concurrency guarantee. Cards are then
        locked in ascending id order (the same order used by trades), and the
        current favorite is re-read under those locks.

        ``expected_main_card_id`` is an optional compare-and-set guard for UI
        callbacks. Pass ``0`` when the screen showed no Main Card. A callback
        that is out of date cannot replace a different choice; a duplicate
        callback for the already-selected card remains an idempotent no-op.
        """
        if not 0 < card_id <= MAX_SIGNED_BIGINT:
            raise CardError("شناسه کارت معتبر نیست.")
        if not 0 < user_id <= MAX_SIGNED_BIGINT:
            raise CardError("شناسه کاربر معتبر نیست.")
        if expected_main_card_id is not None and not (
            0 <= expected_main_card_id <= MAX_SIGNED_BIGINT
        ):
            raise CardError("شناسه کارت اصلی معتبر نیست.")

        locked_user_id = await session.scalar(
            select(User.id).where(User.id == user_id).with_for_update()
        )
        if locked_user_id is None:
            raise CardError("حساب کاربری پیدا نشد.")

        # Lock the requested owned card and all current favorites in one
        # deterministic order. Filtering by user_id also makes an unowned
        # target indistinguishable from a missing card at the service boundary.
        cards = list(
            (
                await session.scalars(
                    select(PlayerCard)
                    .where(PlayerCard.user_id == user_id)
                    .where(
                        or_(
                            PlayerCard.id == card_id,
                            PlayerCard.is_favorite.is_(True),
                        )
                    )
                    .order_by(PlayerCard.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        card = next((owned for owned in cards if owned.id == card_id), None)
        if card is None:
            raise CardError("کارت موردنظر متعلق به شما نیست.")

        favorites = [owned for owned in cards if owned.is_favorite]
        main_card = favorites[0] if favorites else None

        if favorite:
            # A replay after the first click is harmless, even though the
            # original expected id no longer matches. Repair any legacy
            # duplicates deterministically while retaining the chosen card.
            if main_card is card:
                duplicates = [owned for owned in favorites if owned.id != card.id]
                if duplicates:
                    for duplicate in duplicates:
                        duplicate.is_favorite = False
                    await session.flush()
                return card

            actual_main_card_id = main_card.id if main_card is not None else 0
            if (
                expected_main_card_id is not None
                and expected_main_card_id != actual_main_card_id
            ):
                raise StaleMainCardSelectionError(
                    "کارت اصلی‌ات از وقتی این صفحه را باز کردی تغییر کرده؛ "
                    "صفحه را دوباره باز کن."
                )

            for current in favorites:
                current.is_favorite = False
            card.is_favorite = True
            await session.flush()
        elif card.is_favorite:
            card.is_favorite = False
            await session.flush()

        return card

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @staticmethod
    async def list_cards(session: AsyncSession, user_id: int) -> List[PlayerCard]:
        stmt = (
            select(PlayerCard)
            .where(PlayerCard.user_id == user_id)
            .order_by(PlayerCard.level.desc(), PlayerCard.id.asc())
        )
        cards = list((await session.scalars(stmt)).all())
        main_card = await PlayerCardService.get_main_card(session, user_id)
        if main_card is not None:
            # Preserve the historical ordering (Main Card first, then level/id)
            # without treating any legacy duplicate flags as separate mains.
            cards.sort(
                key=lambda card: (
                    card.id != main_card.id,
                    -card.level,
                    card.id,
                )
            )
        return cards

    @staticmethod
    async def get_card(session: AsyncSession, card_id: int) -> Optional[PlayerCard]:
        return await session.get(PlayerCard, card_id)

    @staticmethod
    async def get_main_card(session: AsyncSession, user_id: int) -> Optional[PlayerCard]:
        """Return the player's Main Card, or ``None`` when none is selected.

        The id order is a stable compatibility rule for any legacy duplicate
        favorite rows. Normal selections are serialized by ``set_favorite``
        and keep a single favorite.
        """
        return await session.scalar(
            select(PlayerCard)
            .where(
                PlayerCard.user_id == user_id,
                PlayerCard.is_favorite.is_(True),
            )
            .order_by(PlayerCard.id.asc())
            .limit(1)
            .execution_options(populate_existing=True)
        )

    @staticmethod
    async def _owned_card(session: AsyncSession, user_id: int, card_id: int) -> PlayerCard:
        card = await session.get(PlayerCard, card_id)
        if not card or card.user_id != user_id:
            raise CardError("کارت موردنظر متعلق به شما نیست.")
        return card

    @classmethod
    async def summary(cls, session: AsyncSession, user_id: int) -> str:
        cards = await cls.list_cards(session, user_id)
        if not cards:
            return "هنوز کارتی نداری و کارت اصلی‌ای هم انتخاب نشده! از فروشگاه هفتگی شروع کن. 🍏"
        main_card = await cls.get_main_card(session, user_id)
        main_card_id = main_card.id if main_card is not None else None
        lines = [f"🎴 کارت‌های شما ({len(cards)}):\n"]
        if main_card is not None:
            main_character = await session.get(Character, main_card.character_id)
            main_name = main_character.name_fa if main_character else "?"
            lines.append(f"⭐ کارت اصلی: **{main_name}**")
        else:
            lines.append("⭐ کارت اصلی: انتخاب نشده")
        for card in cards:
            character = await session.get(Character, card.character_id)
            name = character.name_fa if character else "?"
            star = "⭐ " if card.id == main_card_id else ""
            stats = card_attributes(card, character) if character else None
            stats_text = (
                f"💪{stats.power} ⚡{stats.speed} 🧠{stats.intelligence}" if stats else ""
            )
            lines.append(f"{star}**{name}** — سطح {card.level} | {stats_text}".rstrip(" |"))
        return "\n".join(lines)
