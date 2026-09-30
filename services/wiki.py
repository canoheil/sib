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

    PAGE_SIZE = 8

    @staticmethod
    async def render_collection(
        session: AsyncSession, user_id: int, page: int = 0
    ) -> Tuple[str, int]:
        """(text, total_pages) for the My Cards panel.

        Structure: summary header (count, favorite, total budget) then one
        compact line per card. Each card is opened directly from the
        panel's buttons for upgrades/reallocation.
        """
        cards = await PlayerCardService.list_cards(session, user_id)
        if not cards:
            return (
                "🎴 **کارت‌های من**\n\n"
                "هنوز کارتی ندارید! از فروشگاه 🍏 یک شخصیت بخرید تا شروع کنید.",
                1,
            )

        total_pages = max(1, (len(cards) + CollectionService.PAGE_SIZE - 1) // CollectionService.PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        page_cards = cards[page * CollectionService.PAGE_SIZE : (page + 1) * CollectionService.PAGE_SIZE]

        # Batch-load characters (no per-card round trips).
        character_ids = sorted({c.character_id for c in cards})
        characters = {
            ch.id: ch
            for ch in (
                await session.scalars(
                    select(Character).where(Character.id.in_(character_ids))
                )
            ).all()
        }

        total_budget = sum(c.power + c.speed + c.intelligence for c in cards)
        favorite = next((c for c in cards if c.is_favorite), None)
        fav_name = None
        if favorite is not None:
            fav_char = characters.get(favorite.character_id)
            fav_name = fav_char.name_fa if fav_char else None

        lines = ["🎴 **کارت‌های من**\n"]
        summary = [f"🗂 تعداد کارت‌ها: `{len(cards)}`", f"📊 مجموع امتیازها: `{total_budget}`"]
        if fav_name:
            summary.insert(1, f"⭐ کارت اصلی: **{fav_name}**")
        lines.append(" | ".join(summary))

        if total_pages > 1:
            lines.append(f"📖 صفحه {page + 1} از {total_pages}")
        lines.append("")

        for card in page_cards:
            character = characters.get(card.character_id)
            name = character.name_fa if character else "نامشخص"
            star = "⭐ " if card.is_favorite else ""
            budget = card.power + card.speed + card.intelligence
            lines.append(
                f"{star}**{name}** — سطح `{card.level}` | "
                f"💪{card.power} ⚡{card.speed} 🧠{card.intelligence} | مجموع `{budget}`"
            )

        lines.append("")
        lines.append("برای ارتقا و تخصیص امتیاز، کارت مورد نظر را انتخاب کن 👇")
        return "\n".join(lines), total_pages

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
