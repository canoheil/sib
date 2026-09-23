"""Membership checker service for Telegram channels."""

from typing import Optional, Tuple
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from goje.core.config import settings
from goje.core.logging import logger, log_event, Events
from goje.database.models import SponsorChannel, ChannelType


class MembershipService:
    """Verifies user membership in required official and sponsor channels."""

    VALID_MEMBERSHIP_STATUSES = {"creator", "administrator", "member", "restricted"}

    @classmethod
    async def check_user_membership(
        cls,
        bot: Bot,
        user_id: int,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[bool, Optional[SponsorChannel]]:
        """
        Check if user is a member of all required channels.
        Returns (is_allowed, missing_channel).
        """
        if not settings.MEMBERSHIP_CHECK_ENABLED:
            return True, None

        required_channels = []

        if session:
            stmt = (
                select(SponsorChannel)
                .where(SponsorChannel.is_active == True)
                .where(SponsorChannel.is_required == True)
                .order_by(SponsorChannel.priority.desc())
            )
            result = await session.scalars(stmt)
            required_channels = list(result.all())

        # Fallback to config if no DB channels found
        if not required_channels and settings.OFFICIAL_CHANNEL_ID:
            fallback_channel = SponsorChannel(
                channel_id=settings.OFFICIAL_CHANNEL_ID,
                title="کانال رسمی گوجه",
                invite_link=settings.OFFICIAL_CHANNEL_LINK,
                channel_type=ChannelType.OFFICIAL,
                is_required=True,
                is_active=True,
            )
            required_channels = [fallback_channel]

        for channel in required_channels:
            is_member = await cls._check_single_channel(bot, user_id, channel.channel_id)
            if not is_member:
                log_event(
                    Events.MEMBERSHIP_REJECTED,
                    {"user_id": user_id, "channel_id": channel.channel_id},
                )
                return False, channel

        log_event(Events.MEMBERSHIP_VERIFIED, {"user_id": user_id})
        return True, None

    @classmethod
    async def _check_single_channel(cls, bot: Bot, user_id: int, channel_id: str) -> bool:
        """Query Telegram Bot API for chat member status."""
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            status = getattr(member, "status", None)
            
            # Special check for restricted status (they might still be a member)
            if status == "restricted":
                is_member = getattr(member, "is_member", True)
                return bool(is_member)
                
            return status in cls.VALID_MEMBERSHIP_STATUSES

        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                f"Cannot verify membership in {channel_id} for user {user_id}: {exc}"
            )
            # If bot is not admin or channel is unreachable, log warning
            return False
        except Exception as exc:
            logger.error(f"Unexpected error checking channel membership: {exc}")
            return False
