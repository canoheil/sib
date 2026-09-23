"""Sponsor Channel Management Service."""

from typing import List, Optional
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from goje.database.models import SponsorChannel, ChannelType


class SponsorService:
    """Service managing official channels and sponsor channels."""

    @classmethod
    async def get_all_channels(cls, session: AsyncSession) -> List[SponsorChannel]:
        """Fetch all configured channels ordered by priority."""
        stmt = select(SponsorChannel).order_by(SponsorChannel.priority.desc(), SponsorChannel.id.asc())
        return list((await session.scalars(stmt)).all())

    @classmethod
    async def get_channel(cls, session: AsyncSession, channel_id: str) -> Optional[SponsorChannel]:
        """Fetch channel by Telegram channel_id."""
        return await session.scalar(select(SponsorChannel).where(SponsorChannel.channel_id == channel_id))

    @classmethod
    async def add_or_update_channel(
        cls,
        session: AsyncSession,
        channel_id: str,
        title: str,
        invite_link: str,
        channel_type: str = ChannelType.SPONSOR,
        is_required: bool = True,
        is_active: bool = True,
        priority: int = 10,
    ) -> SponsorChannel:
        """Add or update a channel."""
        ch = await cls.get_channel(session, channel_id)
        if not ch:
            ch = SponsorChannel(
                channel_id=channel_id,
                title=title,
                invite_link=invite_link,
                channel_type=channel_type,
                is_required=is_required,
                is_active=is_active,
                priority=priority,
            )
            session.add(ch)
        else:
            ch.title = title
            ch.invite_link = invite_link
            ch.channel_type = channel_type
            ch.is_required = is_required
            ch.is_active = is_active
            ch.priority = priority

        await session.flush()
        return ch

    @classmethod
    async def remove_channel(cls, session: AsyncSession, channel_id: str) -> bool:
        """Remove a channel by ID."""
        stmt = delete(SponsorChannel).where(SponsorChannel.channel_id == channel_id)
        res = await session.execute(stmt)
        return (res.rowcount > 0)

    @classmethod
    async def toggle_required(cls, session: AsyncSession, channel_id: str) -> Optional[SponsorChannel]:
        """Toggle whether membership in this channel is required."""
        ch = await cls.get_channel(session, channel_id)
        if ch:
            ch.is_required = not ch.is_required
            await session.flush()
        return ch

    @classmethod
    async def toggle_active(cls, session: AsyncSession, channel_id: str) -> Optional[SponsorChannel]:
        """Toggle active status of channel."""
        ch = await cls.get_channel(session, channel_id)
        if ch:
            ch.is_active = not ch.is_active
            await session.flush()
        return ch
