"""Group service for Telegram group chats."""

from aiogram.types import Chat as TelegramChat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sib.core.config import settings
from sib.core.logging import log_event, Events
from sib.database.models import Group, Rating


class GroupService:
    """Service for managing sib group chats."""

    @staticmethod
    async def get_or_create_group(session: AsyncSession, tg_chat: TelegramChat) -> Group:
        """Register or update group data and initialize rating if missing."""
        stmt = select(Group).where(Group.id == tg_chat.id)
        group = await session.scalar(stmt)

        title = tg_chat.title or "گروه تلگرام"

        if not group:
            group = Group(
                id=tg_chat.id,
                title=title,
                username=tg_chat.username,
                is_active=True,
            )
            session.add(group)
            await session.flush()

            # Initialize Group Rating
            rating = Rating(
                group_id=group.id,
                rating=settings.INITIAL_RATING,
                matches_played=0,
                wins=0,
                losses=0,
                draws=0,
            )
            session.add(rating)
            await session.flush()

            log_event(
                Events.GROUP_REGISTERED,
                {"group_id": group.id, "title": group.title, "initial_rating": rating.rating},
            )
        else:
            if group.title != title:
                group.title = title
                await session.flush()

            # Ensure rating exists
            rating_stmt = select(Rating).where(Rating.group_id == group.id)
            rating = await session.scalar(rating_stmt)
            if not rating:
                rating = Rating(
                    group_id=group.id,
                    rating=settings.INITIAL_RATING,
                    matches_played=0,
                    wins=0,
                    losses=0,
                    draws=0,
                )
                session.add(rating)
                await session.flush()

        return group
