"""Party service managing group party creation, joining, and leaving."""

from typing import List, Optional, Tuple
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sib.core.errors import (
    ActiveSessionExistsException,
    PartyFullException,
    PartyNotFoundException,
    InvalidActionException,
)
from sib.core.logging import log_event, Events
from sib.database.models import Party, PartyMember, PartyStatus, Game, User
from sib.services.session import SessionService


class PartyService:
    """Manages party creation, atomic join/leave operations, and lifecycle."""

    MODE_SLOTS = {
        "solo": 1,
        "2v2": 2,
        "4v4": 4,
    }

    @classmethod
    async def create_party(
        cls,
        session: AsyncSession,
        group_id: int,
        creator_user_id: int,
        game_code: str,
        mode: str = "solo",
    ) -> Party:
        """Create a new party in a group with the creator as first member."""
        # One-active-session rule. The per-user row lock serializes two
        # concurrent creations by the same user (double tap / retry), so
        # only one can observe "no active session".
        await SessionService.lock_user(session, creator_user_id)
        if await SessionService.has_active_session(session, creator_user_id):
            raise ActiveSessionExistsException()

        # Validate game and mode
        game_stmt = select(Game).where(Game.code == game_code).where(Game.is_active == True)
        game = await session.scalar(game_stmt)
        if not game:
            raise InvalidActionException("بازی انتخاب شده معتبر نیست یا غیرفعال است.")

        if mode == "solo" and not game.supports_solo:
            raise InvalidActionException("این بازی از حالت انفرادی پشتیبانی نمی‌کنه.")
        if mode == "2v2" and not game.supports_2v2:
            raise InvalidActionException("این بازی از حالت تیمی ۲ به ۲ پشتیبانی نمی‌کنه.")
        if mode == "4v4" and not game.supports_4v4:
            raise InvalidActionException("این بازی از حالت تیمی ۴ به ۴ پشتیبانی نمی‌کنه.")

        max_players = cls.MODE_SLOTS.get(mode, 1)

        # Status: If solo, team is immediately full! If team mode, it starts OPEN
        initial_status = PartyStatus.FULL if max_players == 1 else PartyStatus.OPEN

        party = Party(
            group_id=group_id,
            game_id=game.id,
            mode=mode,
            max_players=max_players,
            current_players_count=1,
            status=initial_status,
            creator_user_id=creator_user_id,
        )
        session.add(party)
        await session.flush()

        member = PartyMember(
            party_id=party.id,
            user_id=creator_user_id,
        )
        session.add(member)
        await session.flush()

        log_event(
            Events.PARTY_CREATED,
            {
                "party_id": party.id,
                "group_id": group_id,
                "game": game_code,
                "mode": mode,
                "creator_id": creator_user_id,
                "max_players": max_players,
                "status": party.status,
            },
        )

        return await cls.get_party_by_id(session, party.id)

    @classmethod
    async def join_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Party:
        """Atomically join an open party using row-level locking."""
        import asyncio
        from sqlalchemy.exc import OperationalError

        # 1. Check one-active-session rule (per-user lock: same user can
        #    never pass this check twice concurrently).
        await SessionService.lock_user(session, user_id)
        if await SessionService.has_active_session(session, user_id):
            raise ActiveSessionExistsException()

        # 2. Lock party row with FOR UPDATE (retry once if concurrent modification occurs)
        party = None
        for attempt in range(2):
            try:
                stmt = (
                    select(Party)
                    .where(Party.id == party_id)
                    .with_for_update()
                )
                party = await session.scalar(stmt)
                break
            except OperationalError:
                if attempt == 0:
                    await asyncio.sleep(0.05)
                    continue
                raise PartyFullException()

        if not party:
            raise PartyNotFoundException()

        if party.status != PartyStatus.OPEN:
            if party.status == PartyStatus.FULL:
                raise PartyFullException()
            raise PartyNotFoundException()

        # Check if user is already in this party
        is_member_stmt = (
            select(PartyMember.id)
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.user_id == user_id)
        )
        if await session.scalar(is_member_stmt):
            raise InvalidActionException("شما در حال حاضر عضو این پارتی هستید 🍏")

        # Check capacity
        if party.current_players_count >= party.max_players:
            party.status = PartyStatus.FULL
            await session.flush()
            raise PartyFullException()

        # Add member
        member = PartyMember(party_id=party.id, user_id=user_id)
        session.add(member)
        party.current_players_count += 1

        if party.current_players_count >= party.max_players:
            party.status = PartyStatus.FULL

        await session.flush()

        log_event(
            Events.PARTY_JOINED,
            {
                "party_id": party.id,
                "user_id": user_id,
                "current_players": party.current_players_count,
                "max_players": party.max_players,
                "status": party.status,
            },
        )

        return await cls.get_party_by_id(session, party.id)

    @classmethod
    async def leave_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Tuple[Optional[Party], bool]:
        """
        User leaves party.
        Returns (party, was_cancelled).
        """
        stmt = (
            select(Party)
            .where(Party.id == party_id)
            .with_for_update()
        )
        party = await session.scalar(stmt)

        if not party or party.status in (PartyStatus.IN_MATCH, PartyStatus.CANCELLED):
            raise PartyNotFoundException()

        # Check user membership
        member_stmt = (
            select(PartyMember)
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.user_id == user_id)
        )
        member = await session.scalar(member_stmt)
        if not member:
            raise InvalidActionException("شما عضو این پارتی نیستید 🍏")

        await session.delete(member)
        party.current_players_count = max(0, party.current_players_count - 1)

        was_cancelled = False
        if party.current_players_count == 0:
            party.status = PartyStatus.CANCELLED
            was_cancelled = True
        else:
            party.status = PartyStatus.OPEN

        await session.flush()

        log_event(
            Events.PARTY_LEFT,
            {
                "party_id": party.id,
                "user_id": user_id,
                "remaining_players": party.current_players_count,
                "was_cancelled": was_cancelled,
            },
        )

        refreshed_party = await cls.get_party_by_id(session, party.id) if not was_cancelled else None
        return refreshed_party, was_cancelled

    @classmethod
    async def cancel_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Party:
        """Cancel a party (allowed by creator or group member)."""
        stmt = (
            select(Party)
            .where(Party.id == party_id)
            .with_for_update()
        )
        party = await session.scalar(stmt)
        if not party:
            raise PartyNotFoundException()

        # Check membership
        member_stmt = (
            select(PartyMember.id)
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.user_id == user_id)
        )
        is_member = await session.scalar(member_stmt)
        if not is_member and party.creator_user_id != user_id:
            raise InvalidActionException("فقط اعضای این پارتی امکان لغو آن را دارند 🍏")

        party.status = PartyStatus.CANCELLED
        await session.flush()

        log_event("PARTY_CANCELLED", {"party_id": party.id, "cancelled_by": user_id})
        return party

    @classmethod
    async def get_party_by_id(cls, session: AsyncSession, party_id: int) -> Optional[Party]:
        """Fetch party with eager-loaded members, users, and game details."""
        stmt = (
            select(Party)
            .where(Party.id == party_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Party.members).selectinload(PartyMember.user),
                selectinload(Party.game),
                selectinload(Party.creator),
            )
        )
        return await session.scalar(stmt)

    @classmethod
    async def get_active_parties_for_group(
        cls,
        session: AsyncSession,
        group_id: int,
    ) -> List[Party]:
        """Live parties of a group (OPEN, FULL, IN_QUEUE, IN_MATCH).

        Delegates to ``SessionService`` so the Active Games screen lists
        exactly the parties that block the One-Active-Session guard - a
        party waiting in the queue or playing a match is live too and must
        never be invisible (the "ghost active game" bug).
        """
        return await SessionService.get_live_parties_for_group(session, group_id)
