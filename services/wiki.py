"""Wiki & collection service (Phase 12).

- Wiki («ویکی شخصیت‌ها»): browse ALL released characters — readable,
  Persian, with stats, passives and sidegrade text.
- Collection («کارت‌های من»): ONLY what the player owns, with levels and
  current allocations. No hidden numbers leak here.
"""

from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import Character, PlayerCard, ReleaseStatus
from sib.services.characters import render_character_card
from sib.services.player_cards import PlayerCardService


class WikiService:
    """Read-only browsing of the character universe."""

    @staticmethod
    async def list_wiki(session: AsyncSession) -> List[Character]:
        stmt = (
            select(Character)
            .where(Character.is_active == True)
            .where(Character.release_status == ReleaseStatus.RELEASED)
            .order_by(Character.rarity.desc(), Character.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def wiki_page(session: AsyncSession, character_id: int) -> Optional[str]:
        character = await session.get(Character, character_id)
        if not character or not character.is_active:
            return None
        if character.release_status != ReleaseStatus.RELEASED:
            return None
        return render_character_card(character)

    @staticmethod
    def wiki_index_entry(character: Character, index: int) -> str:
        return f"{index}. 🍎 **{character.name_fa}** — `{character.code}`"


class CollectionService:
    """The player's owned cards («کارت‌های من»)."""

    @staticmethod
    async def render_collection(session: AsyncSession, user_id: int) -> str:
        cards = await PlayerCardService.list_cards(session, user_id)
        if not cards:
            return (
                "🎴 **کارت‌های من**\n\n"
                "هنوز کارتی ندارید! از فروشگاه هفتگی 🍏 یک شخصیت بخرید تا شروع کنید."
            )

        lines = [f"🎴 **کارت‌های من** ({len(cards)} کارت)\n"]
        for card in cards:
            character = await session.get(Character, card.character_id)
            name = character.name_fa if character else "نامشخص"
            star = "⭐ " if card.is_favorite else ""
            budget = card.power + card.speed + card.intelligence
            lines.append(
                f"{star}**{name}** — سطح `{card.level}` | "
                f"💪{card.power} ⚡{card.speed} 🧠{card.intelligence} (مجموع {budget})"
            )
        lines.append("\nبرای جزئیات کامل، کارت را در ویکی شخصیت‌ها ببینید.")
        return "\n".join(lines)

    @staticmethod
    async def render_card_detail(session: AsyncSession, user_id: int, card_id: int) -> Optional[str]:
        card = await PlayerCardService.get_card(session, card_id)
        if not card or card.user_id != user_id:
            return None
        character = await session.get(Character, card.character_id)
        if not character:
            return None

        budget = card.power + card.speed + card.intelligence
        lines = [
            f"🎴 **{character.name_fa}** — سطح `{card.level}`",
            "",
            f"💪 قدرت: `{card.power}` (+{card.boosted_power} ارتقا)",
            f"⚡ سرعت: `{card.speed}` (+{card.boosted_speed} ارتقا)",
            f"🧠 هوش: `{card.intelligence}` (+{card.boosted_intelligence} ارتقا)",
            f"📊 مجموع ویژگی‌ها: `{budget}` (بودجه سطح {card.level})",
            "",
            "برای مشاهده توانایی و داستان، به ویکی شخصیت‌ها مراجعه کنید.",
        ]
        return "\n".join(lines)
