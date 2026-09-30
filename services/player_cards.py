"""Player card service.

Ownership, favorites and queries. A new card always starts at Level 1; its
attributes are derived from the character + level (``card_progression``)
and upgrades live in ``card_upgrade.CardUpgradeService``. A player may own
at most one card per character.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import Character, PlayerCard
from sib.services.card_progression import card_attributes


class CardError(ValueError):
    """Raised for invalid card operations."""


class DuplicateCardError(CardError):
    """Raised when a player already owns a card for a character."""


class PlayerCardService:
    """Manage player cards: acquire, favorite, list."""

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
        cls, session: AsyncSession, user_id: int, card_id: int, favorite: bool = True
    ) -> PlayerCard:
        card = await cls._owned_card(session, user_id, card_id)
        if favorite:
            # Only one favorite at a time.
            others = (
                await session.scalars(
                    select(PlayerCard).where(
                        PlayerCard.user_id == user_id,
                        PlayerCard.is_favorite == True,
                    )
                )
            ).all()
            for other in others:
                other.is_favorite = False
        card.is_favorite = favorite
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
            .order_by(PlayerCard.is_favorite.desc(), PlayerCard.level.desc(), PlayerCard.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def get_card(session: AsyncSession, card_id: int) -> Optional[PlayerCard]:
        return await session.get(PlayerCard, card_id)

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
            return "هنوز کارتی نداری! از فروشگاه هفتگی شروع کن. 🍏"
        lines = [f"🎴 کارت‌های شما ({len(cards)}):\n"]
        for card in cards:
            character = await session.get(Character, card.character_id)
            name = character.name_fa if character else "?"
            star = "⭐ " if card.is_favorite else ""
            stats = card_attributes(card, character) if character else None
            stats_text = (
                f"💪{stats.power} ⚡{stats.speed} 🧠{stats.intelligence}" if stats else ""
            )
            lines.append(f"{star}**{name}** — سطح {card.level} | {stats_text}".rstrip(" |"))
        return "\n".join(lines)
