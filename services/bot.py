"""Service for managing internal game bot users."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.config import settings
from sib.database.models import User


class BotService:
    """Manages internal bot users used as game opponents."""

    @staticmethod
    async def get_or_create_bot(session: AsyncSession) -> User:
        """Return the shared internal game bot user."""
        stmt = select(User).where(User.id == settings.BOT_USER_ID)
        bot = await session.scalar(stmt)

        if bot:
            return bot

        bot = User(
            id=settings.BOT_USER_ID,
            first_name="بات سیب کوچولو🍏",
            last_name=None,
            username="goje_game_bot",
            language_code="fa",
            is_bot=True,
            is_active=True,
        )
        session.add(bot)
        await session.flush()

        return bot
