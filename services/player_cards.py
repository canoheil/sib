"""Player card service (Phase 10).

Ownership, levels and attribute allocation.

Budget model: the total of (Power + Speed + Intelligence) is fixed by the
LEVEL (60/120/180/240/300 for levels 1-5), never by the character.
Levels beyond 5 extrapolate the last step, so future levels need no code
change. A player may own at most one card per character.

Upgrade cost is data-driven: card_upgrade_base_cost * card_upgrade_cost_multiplier^(level-1).
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import Character, PlayerCard, Rarity
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.config_store import ConfigService


class CardError(ValueError):
    """Raised for invalid card operations."""


class DuplicateCardError(CardError):
    """Raised when a player already owns a card for a character."""


class PlayerCardService:
    """Manage player cards: acquire, upgrade, allocate, equip."""

    # ------------------------------------------------------------------
    # Budgets & costs
    # ------------------------------------------------------------------
    @classmethod
    async def level_budget(cls, session: AsyncSession, level: int) -> int:
        budgets = await ConfigService.get(session, "card_level_budgets")
        if not budgets:
            budgets = [60, 120, 180, 240, 300]
        if level <= len(budgets):
            return int(budgets[level - 1])
        # Future levels: extrapolate the last configured step (e.g. +60).
        last_step = int(budgets[-1]) - int(budgets[-2]) if len(budgets) >= 2 else 60
        return int(budgets[-1]) + (level - len(budgets)) * last_step

    @classmethod
    async def upgrade_cost(cls, session: AsyncSession, current_level: int) -> int:
        base = int(await ConfigService.get(session, "card_upgrade_base_cost"))
        multiplier = float(await ConfigService.get(session, "card_upgrade_cost_multiplier"))
        return int(base * (multiplier ** (current_level - 1)))

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
            # Level-1 budget == 60 == character base allocation.
            power=character.power,
            speed=character.speed,
            intelligence=character.intelligence,
        )
        session.add(card)
        await session.flush()
        log_event(
            Events.CHARACTER_EVENT,
            {"action": "card_acquired", "user_id": user_id, "character": character.code},
        )
        return card

    # ------------------------------------------------------------------
    # Upgrades & allocation
    # ------------------------------------------------------------------
    @classmethod
    async def upgrade(
        cls,
        session: AsyncSession,
        user_id: int,
        card_id: int,
        *,
        boost_power: int = 0,
        boost_speed: int = 0,
        boost_intelligence: int = 0,
    ) -> PlayerCard:
        card = await cls._owned_card(session, user_id, card_id)
        current_budget = await cls.level_budget(session, card.level)
        new_level = card.level + 1
        new_budget = await cls.level_budget(session, new_level)
        gained = new_budget - current_budget

        boost_total = boost_power + boost_speed + boost_intelligence
        if boost_total != gained:
            raise CardError(
                f"برای ارتقای سطح {new_level} باید دقیقاً {gained} امتیاز جدید تخصیص دهید."
            )
        if min(boost_power, boost_speed, boost_intelligence) < 0:
            raise CardError("امتیازها نمی‌توانند منفی باشند.")

        cost = await cls.upgrade_cost(session, card.level)
        try:
            await AppleWalletService.spend(
                session,
                user_id,
                cost,
                reason="card_upgrade",
                ref_type="card",
                ref_id=card.id,
                idempotency_key=f"card_upgrade:{card.id}:{card.level + 1}",
            )
        except InsufficientFundsException:
            raise CardError(
                f"سیب کافی نیست! هزینه ارتقا: {cost} سیب 🍎"
            )

        card.level = new_level
        card.power += boost_power
        card.speed += boost_speed
        card.intelligence += boost_intelligence
        card.boosted_power += boost_power
        card.boosted_speed += boost_speed
        card.boosted_intelligence += boost_intelligence
        await session.flush()

        log_event(
            Events.CHARACTER_EVENT,
            {
                "action": "card_upgraded",
                "user_id": user_id,
                "card_id": card.id,
                "level": card.level,
            },
        )
        return card

    @classmethod
    async def reallocate(
        cls,
        session: AsyncSession,
        user_id: int,
        card_id: int,
        *,
        power: int,
        speed: int,
        intelligence: int,
    ) -> PlayerCard:
        """Reallocate the current total budget (free reallocation)."""
        card = await cls._owned_card(session, user_id, card_id)
        budget = await cls.level_budget(session, card.level)
        if min(power, speed, intelligence) < 0:
            raise CardError("ویژگی‌ها نمی‌توانند منفی باشند.")
        if power + speed + intelligence != budget:
            raise CardError(
                f"مجموع ویژگی‌ها باید دقیقاً {budget} باشد (بودجه سطح {card.level})."
            )
        card.power = power
        card.speed = speed
        card.intelligence = intelligence
        await session.flush()
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
            return "هنوز کارتی ندارید! از فروشگاه هفتگی شروع کنید. 🍏"
        lines = [f"🎴 کارت‌های شما ({len(cards)}):\n"]
        for card in cards:
            character = await session.get(Character, card.character_id)
            name = character.name_fa if character else "?"
            star = "⭐ " if card.is_favorite else ""
            lines.append(
                f"{star}**{name}** — سطح {card.level} | "
                f"💪{card.power} ⚡{card.speed} 🧠{card.intelligence}"
            )
        return "\n".join(lines)
