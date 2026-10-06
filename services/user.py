"""User service for user management."""

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sib.core.logging import log_event, Events
from sib.database.models import PlayerProgression, User


class UserService:
    """Service for managing sib users."""

    @staticmethod
    async def get_or_create_user(session: AsyncSession, tg_user: TelegramUser) -> User:
        """Register or update user data from incoming Telegram update."""
        stmt = select(User).where(User.id == tg_user.id)
        user = await session.scalar(stmt)

        if not user:
            user = User(
                id=tg_user.id,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                username=tg_user.username,
                language_code=tg_user.language_code or "fa",
                is_bot=tg_user.is_bot,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            session.add(PlayerProgression(user_id=user.id, total_xp=0))
            await session.flush()
            log_event(
                Events.USER_REGISTERED,
                {"user_id": user.id, "username": user.username},
            )
        else:
            # Keep profile info fresh
            updated = False
            if user.first_name != tg_user.first_name:
                user.first_name = tg_user.first_name
                updated = True
            if user.last_name != tg_user.last_name:
                user.last_name = tg_user.last_name
                updated = True
            if user.username != tg_user.username:
                user.username = tg_user.username
                updated = True
            if updated:
                await session.flush()

        return user
