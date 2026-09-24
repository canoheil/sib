"""Service for enforcing the One-Active-Session rule."""

from typing import Optional
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sib.database.models import Party, PartyMember, PartyStatus, Match, MatchPlayer, MatchState


class SessionService:
    """Enforces server-side that each user may have only ONE active session at a time."""

    ACTIVE_PARTY_STATUSES = (
        PartyStatus.OPEN,
        PartyStatus.FULL,
        PartyStatus.IN_QUEUE,
        PartyStatus.IN_MATCH,
    )

    ACTIVE_MATCH_STATES = (
        MatchState.WAITING_FOR_OPPONENT,
        MatchState.OPPONENT_FOUND,
        MatchState.WAITING_FOR_ACCEPTANCE,
        MatchState.ACTIVE,
    )

    @classmethod
    async def has_active_session(cls, session: AsyncSession, user_id: int) -> bool:
        """Check if user currently has an active party, queue, or match."""
        # 1. Check active party membership
        party_stmt = (
            select(PartyMember.id)
            .join(Party, PartyMember.party_id == Party.id)
            .where(PartyMember.user_id == user_id)
            .where(Party.status.in_(cls.ACTIVE_PARTY_STATUSES))
            .limit(1)
        )
        party_res = await session.scalar(party_stmt)
        if party_res:
            return True

        # 2. Check active match participation
        match_stmt = (
            select(MatchPlayer.id)
            .join(Match, MatchPlayer.match_id == Match.id)
            .where(MatchPlayer.user_id == user_id)
            .where(MatchPlayer.is_active == True)
            .where(Match.state.in_(cls.ACTIVE_MATCH_STATES))
            .limit(1)
        )
        match_res = await session.scalar(match_stmt)
        if match_res:
            return True

        return False

    @classmethod
    async def get_active_session_info(cls, session: AsyncSession, user_id: int) -> Optional[str]:
        """Get friendly description of the active session if one exists."""
        party_stmt = (
            select(Party)
            .join(PartyMember, PartyMember.party_id == Party.id)
            .where(PartyMember.user_id == user_id)
            .where(Party.status.in_(cls.ACTIVE_PARTY_STATUSES))
            .limit(1)
        )
        party = await session.scalar(party_stmt)
        if party:
            return f"پارتی بازی (شناسه: {party.id})"

        match_stmt = (
            select(Match)
            .join(MatchPlayer, MatchPlayer.match_id == Match.id)
            .where(MatchPlayer.user_id == user_id)
            .where(MatchPlayer.is_active == True)
            .where(Match.state.in_(cls.ACTIVE_MATCH_STATES))
            .limit(1)
        )
        match = await session.scalar(match_stmt)
        if match:
            return f"مسابقه فعال (شناسه: {match.id})"

        return None
