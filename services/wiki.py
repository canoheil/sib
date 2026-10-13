"""Wiki & collection service (Phase 12).

- Wiki («ویکی شخصیت‌ها»): browse ALL released characters — readable,
  Persian, with stats, passives and sidegrade text.
- Collection («کارت‌های من»): ONLY what the player owns, with levels and
  current allocations. No hidden numbers leak here.
"""

from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.text import escape_markdown
from sib.database.models import Character, Faction, PlayerCard, ReleaseStatus
from sib.services.card_progression import (
    CARD_ATTRIBUTES,
    MAX_CARD_LEVEL,
    CardAttributes,
    card_attributes,
)
from sib.services.characters import render_affiliations_line, render_character_card
from sib.services.factions import FactionService
from sib.services.player_cards import PlayerCardService


def attribute_lines(stats: CardAttributes) -> List[str]:
    """One line per attribute with the project's real icons and Persian names."""
    return [
        f"{emoji} {label}: `{getattr(stats, key)}`" for key, emoji, label in CARD_ATTRIBUTES
    ]


class WikiService:
    """Read-only browsing of the character universe."""

    @staticmethod
    async def list_wiki(session: AsyncSession) -> List[Character]:
        # Faction display order first (unaffiliated characters last),
        # then stable insertion order. Data-driven: no faction is named
        # in code.
        stmt = (
            select(Character)
            .outerjoin(Faction, Character.faction_id == Faction.id)
            .where(Character.is_active == True)  # noqa: E712
            .where(Character.release_status == ReleaseStatus.RELEASED)
            .order_by(
                func.coalesce(Faction.display_order, 999).asc(),
                Character.id.asc(),
            )
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def wiki_page(session: AsyncSession, character_id: int) -> Optional[str]:
        character = await session.get(Character, character_id)
        if not character or not character.is_active:
            return None
        if character.release_status != ReleaseStatus.RELEASED:
            return None
        faction = await FactionService.get_for_character(session, character)
        return render_character_card(character, show_price=False, faction=faction)

    @staticmethod
    def wiki_index_entry(character: Character, index: int) -> str:
        return f"{index}. 🍎 *{escape_markdown(character.name_fa)}* — `{character.code}`"


class CollectionService:
    """The player's owned cards («کارت‌های من»)."""

    PAGE_SIZE = 8

    @staticmethod
    async def render_collection(
        session: AsyncSession, user_id: int, page: int = 0
    ) -> Tuple[str, int]:
        """(text, total_pages) for the My Cards panel.

        Structure: summary header (count, Main Card, total attributes) then
        one compact line per card. Each card is opened from the panel's
        buttons (view screen with the ⬆️ upgrade action).
        """
        cards = await PlayerCardService.list_cards(session, user_id)
        main_card = await PlayerCardService.get_main_card(session, user_id)
        if not cards:
            return (
                "🎴 **کارت‌های من**\n\n"
                "هنوز کارتی نداری و کارت اصلی‌ای هم انتخاب نشده! "
                "از فروشگاه 🍏 یه شخصیت بخر تا شروع کنی.",
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

        stats_by_card = {
            c.id: card_attributes(c, characters[c.character_id])
            for c in cards
            if c.character_id in characters
        }
        total_budget = sum(a.total for a in stats_by_card.values())
        main_character = (
            characters.get(main_card.character_id) if main_card is not None else None
        )
        main_name = main_character.name_fa if main_character else "نامشخص"

        lines = ["🎴 **کارت‌های من**\n"]
        main_summary = (
            f"⭐ کارت اصلی: *{escape_markdown(main_name)}*"
            if main_card is not None
            else "⭐ کارت اصلی: انتخاب نشده"
        )
        summary = [
            f"🗂 تعداد کارت‌ها: `{len(cards)}`",
            main_summary,
            f"📊 مجموع امتیازها: `{total_budget}`",
        ]
        lines.append(" | ".join(summary))

        if total_pages > 1:
            lines.append(f"📖 صفحه {page + 1} از {total_pages}")
        lines.append("")

        for card in page_cards:
            character = characters.get(card.character_id)
            name = character.name_fa if character else "نامشخص"
            star = "⭐ " if main_card is not None and card.id == main_card.id else ""
            stats = stats_by_card.get(card.id)
            stats_text = (
                f"💪{stats.power} ⚡{stats.speed} 🧠{stats.intelligence} | مجموع `{stats.total}`"
                if stats
                else "—"
            )
            lines.append(f"{star}*{escape_markdown(name)}* — سطح `{card.level}` | {stats_text}")

        lines.append("")
        lines.append("برای دیدن کارت و ارتقای آن، یکی از کارت‌ها را انتخاب کن 👇")
        return "\n".join(lines), total_pages

    @staticmethod
    async def render_card_detail(
        session: AsyncSession, user_id: int, card_id: int
    ) -> Optional[str]:
        detail, _main_card_id = await CollectionService.render_card_detail_with_main_card(
            session, user_id, card_id
        )
        return detail

    @staticmethod
    async def render_card_detail_with_main_card(
        session: AsyncSession, user_id: int, card_id: int
    ) -> Tuple[Optional[str], Optional[int]]:
        """Return detail text and the same-snapshot Main Card id for its keyboard."""
        card = await PlayerCardService.get_card(session, card_id)
        if not card or card.user_id != user_id:
            return None, None
        character = await session.get(Character, card.character_id)
        if not character:
            return None, None

        faction = await FactionService.get_for_character(session, character)
        stats = card_attributes(card, character)
        lines = [
            f"🎴 *{escape_markdown(character.name_fa)}* — سطح `{card.level}` از `{MAX_CARD_LEVEL}`",
            "",
            *attribute_lines(stats),
            f"📊 مجموع ویژگی‌ها: `{stats.total}`",
        ]
        main_card = await PlayerCardService.get_main_card(session, user_id)
        main_card_id = main_card.id if main_card is not None else None
        if main_card is None:
            lines.extend(["", "⭐ کارت اصلی هنوز انتخاب نشده است."])
        elif main_card.id == card.id:
            lines.extend(["", "⭐ این کارت، کارت اصلی شماست."])
        else:
            main_character = await session.get(Character, main_card.character_id)
            main_name = main_character.name_fa if main_character else "نامشخص"
            lines.extend(["", f"⭐ کارت اصلی فعلی شما: *{escape_markdown(main_name)}*"])
        lines.extend(["", render_affiliations_line(character, faction)])
        if character.affiliations_fa is None and faction is not None:
            if character.faction_role_fa:
                lines.append(f"نقش: {escape_markdown(character.faction_role_fa)}")
        lines += [
            "",
            "برای دیدن داستان و جزئیات شخصیت، به ویکی شخصیت‌ها سر بزن.",
        ]
        return "\n".join(lines), main_card_id
