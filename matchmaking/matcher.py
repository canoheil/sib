"""Matchmaking engine with compatibility rules, cross-group enforcement, and concurrency safety.

Elo/MMR has been removed: matching uses ONLY game, mode, team/party size,
queue state and queue time. Incompatible parties are never mixed.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple
from sqlalchemy import select, and_, or_, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.config import settings
from sib.core.errors import (
    sibException,
    InvalidActionException,
    PartyNotFoundException,
)
from sib.core.logging import logger, log_event, Events
from sib.database.models import (
    Party,
    PartyMember,
    PartyStatus,
    Group,
    Game,
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
    MatchmakingQueue,
)


class MatchmakingService:
    """Manages matchmaking queues and atomic match creation.

    Compatibility rules (never violated):
    - same game
    - same mode (solo / 2v2 / 4v4)
    - same party size
    - different Telegram groups
    - active, non-expired queue entries

    Fairness rule: the longest-waiting compatible party is matched first.
    """

    @classmethod
    async def enqueue_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> MatchmakingQueue:
        """Enqueue a full party into matchmaking."""
        # 1. Fetch party with row lock
        party_stmt = (
            select(Party)
            .where(Party.id == party_id)
            .options(selectinload(Party.members), selectinload(Party.game))
            .with_for_update()
        )
        party = await session.scalar(party_stmt)
        if not party:
            raise PartyNotFoundException()

        # 2. Hard requirements
        if party.is_friendly:
            # A friendly party already contains BOTH teams of a group match.
            # It must never enter the ranked queue (no accidental ranked game).
            raise InvalidActionException(
                "این پارتی رفیقانه است و توی صف حریف رقابتی قرار نمی‌گیره؛ "
                "برای شروع، دکمهٔ «شروع بازی رفیقانه» رو بزن 🍏"
            )
        if party.status == PartyStatus.IN_QUEUE:
            raise InvalidActionException("این تیم در حال حاضر در صف جستجوی حریف قرار دارد 🍏")
        if party.status == PartyStatus.IN_MATCH:
            raise InvalidActionException("این تیم در حال حاضر در مسابقه است 🍏")
        if not party.is_full:
            raise InvalidActionException("برای شروع جستجوی حریف، ابتدا باید ظرفیت تیم تکمیل شود 🍏")

        # 3. Check user is a member of this party
        is_member = any(m.user_id == user_id for m in party.members)
        if not is_member:
            raise InvalidActionException("فقط اعضای تیم می‌توانند جستجوی حریف را آغاز کنند 🍏")

        now = datetime.now(timezone.utc)
        timeout_seconds = (
            settings.SOLO_MATCHMAKING_TIMEOUT_SECONDS
            if party.mode == "solo"
            else settings.TEAM_MATCHMAKING_TIMEOUT_SECONDS
        )
        expires_at = now + timedelta(seconds=timeout_seconds)

        # 5. Clean up any stale queue entry for this party
        existing_queue = await session.scalar(
            select(MatchmakingQueue).where(MatchmakingQueue.party_id == party.id)
        )
        if existing_queue:
            existing_queue.is_active = True
            existing_queue.entered_at = now
            existing_queue.expires_at = expires_at
            queue_entry = existing_queue
        else:
            queue_entry = MatchmakingQueue(
                party_id=party.id,
                group_id=party.group_id,
                game_id=party.game_id,
                mode=party.mode,
                entered_at=now,
                expires_at=expires_at,
                is_active=True,
            )
            session.add(queue_entry)

        party.status = PartyStatus.IN_QUEUE
        await session.flush()

        log_event(
            Events.MATCHMAKING_STARTED,
            {
                "party_id": party.id,
                "group_id": party.group_id,
                "game": party.game.code,
                "mode": party.mode,
            },
        )

        return queue_entry

    @classmethod
    async def cancel_matchmaking(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> bool:
        """Cancel matchmaking search and return party to FULL status."""
        # Queue -> party lock order matches the matcher and timeout worker.
        queue_entry = await session.scalar(
            select(MatchmakingQueue)
            .where(MatchmakingQueue.party_id == party_id)
            .where(MatchmakingQueue.is_active == True)
            .with_for_update()
        )
        party_stmt = (
            select(Party)
            .where(Party.id == party_id)
            .options(selectinload(Party.members))
            .with_for_update()
        )
        party = await session.scalar(party_stmt)
        if not party:
            raise PartyNotFoundException()

        is_member = any(m.user_id == user_id for m in party.members)
        if not is_member and party.creator_user_id != user_id:
            raise InvalidActionException("فقط اعضای تیم می‌توانند جستجوی حریف را لغو کنند 🍏")

        if party.status != PartyStatus.IN_QUEUE or not queue_entry:
            raise InvalidActionException("جستجوی حریف تموم شده یا قبلاً لغو شده 🍏")

        queue_entry.is_active = False
        party.status = PartyStatus.FULL
        await session.flush()

        log_event(Events.MATCHMAKING_CANCELLED, {"party_id": party.id, "cancelled_by": user_id})
        return True

    @classmethod
    async def find_and_create_match(
        cls,
        session: AsyncSession,
        party_id: int,
    ) -> Optional[Match]:
        """
        Search for a compatible opponent in queue.
        If found, atomically claim both parties and create a Match.
        """
        now = datetime.now(timezone.utc)

        # 1. Lock current queue row first. Expiration is checked AFTER lock
        # acquisition so a late matcher cannot use a snapshot taken just
        # before the timeout worker claims the same row.
        my_queue_stmt = (
            select(MatchmakingQueue)
            .where(MatchmakingQueue.party_id == party_id)
            .where(MatchmakingQueue.is_active == True)
            .with_for_update()
        )
        my_queue = await session.scalar(my_queue_stmt)
        if not my_queue or not my_queue.is_active:
            return None
        now = datetime.now(timezone.utc)
        my_expiry = my_queue.expires_at
        if my_expiry.tzinfo is None:
            my_expiry = my_expiry.replace(tzinfo=timezone.utc)
        if my_expiry <= now:
            return None

        # 2. Hard compatibility query (NO Elo/MMR involved):
        # - Same game
        # - Same mode (team size)
        # - Same party size
        # - DIFFERENT Telegram group (Group A != Group B is NEVER allowed to match!)
        # - Active & not expired
        my_party_size = await session.scalar(
            select(Party.max_players).where(Party.id == my_queue.party_id)
        )

        candidate_stmt = (
            select(MatchmakingQueue)
            .join(Party, Party.id == MatchmakingQueue.party_id)
            .where(MatchmakingQueue.game_id == my_queue.game_id)
            .where(MatchmakingQueue.mode == my_queue.mode)
            .where(Party.max_players == my_party_size)
            # Friendly parties are group-internal and never ranked opponents.
            .where(Party.match_type == MatchType.NORMAL)
            .where(MatchmakingQueue.group_id != my_queue.group_id)
            .where(MatchmakingQueue.is_active == True)
            .where(MatchmakingQueue.expires_at > now)
            .order_by(MatchmakingQueue.entered_at.asc())
            .with_for_update()
        )
        candidates = list((await session.scalars(candidate_stmt)).all())
        if not candidates:
            return None

        # 3. Fair selection: the longest-waiting compatible party first
        # (deterministic, queue-time based, no hidden skill rating).
        candidates.sort(key=lambda c: c.entered_at)
        chosen_cand = candidates[0]

        # 4. Atomic lock on both parties
        party1_stmt = (
            select(Party)
            .where(Party.id == my_queue.party_id)
            .options(selectinload(Party.members).selectinload(PartyMember.user))
            .with_for_update()
        )
        party1 = await session.scalar(party1_stmt)

        party2_stmt = (
            select(Party)
            .where(Party.id == chosen_cand.party_id)
            .options(selectinload(Party.members).selectinload(PartyMember.user))
            .with_for_update()
        )
        party2 = await session.scalar(party2_stmt)

        if not party1 or not party2:
            return None

        # Verify both queue entries and parties are still claimable after all
        # row locks have been acquired. This closes the timeout-vs-late-join
        # race even if a candidate crossed its deadline while we were waiting.
        now = datetime.now(timezone.utc)
        candidate_expiry = chosen_cand.expires_at
        if candidate_expiry.tzinfo is None:
            candidate_expiry = candidate_expiry.replace(tzinfo=timezone.utc)
        own_expiry = my_queue.expires_at
        if own_expiry.tzinfo is None:
            own_expiry = own_expiry.replace(tzinfo=timezone.utc)
        if (
            not my_queue.is_active
            or not chosen_cand.is_active
            or own_expiry <= now
            or candidate_expiry <= now
            or party1.status != PartyStatus.IN_QUEUE
            or party2.status != PartyStatus.IN_QUEUE
        ):
            return None

        # 5. Deactivate both queue entries
        my_queue.is_active = False
        chosen_cand.is_active = False

        party1.status = PartyStatus.IN_MATCH
        party2.status = PartyStatus.IN_MATCH

        # 6. Create Match in WAITING_FOR_ACCEPTANCE state
        acceptance_deadline = now + timedelta(seconds=settings.ACCEPTANCE_TIMEOUT_SECONDS)
        new_match = Match(
            game_id=my_queue.game_id,
            mode=my_queue.mode,
            state=MatchState.WAITING_FOR_ACCEPTANCE,
            group1_id=party1.group_id,
            group2_id=party2.group_id,
            party1_id=party1.id,
            party2_id=party2.id,
            group1_accepted=False,
            group2_accepted=False,
            acceptance_deadline=acceptance_deadline,
        )
        session.add(new_match)
        await session.flush()

        # 7. Add Match Players for both teams
        turn_order = 1
        for m in party1.members:
            mp = MatchPlayer(
                match_id=new_match.id,
                user_id=m.user_id,
                group_id=party1.group_id,
                team=1,
                turn_order=turn_order,
                is_active=True,
                has_accepted=False,
            )
            session.add(mp)
            turn_order += 1

        turn_order = 1
        for m in party2.members:
            mp = MatchPlayer(
                match_id=new_match.id,
                user_id=m.user_id,
                group_id=party2.group_id,
                team=2,
                turn_order=turn_order,
                is_active=True,
                has_accepted=False,
            )
            session.add(mp)
            turn_order += 1

        await session.flush()

        log_event(
            Events.MATCH_FOUND,
            {
                "match_id": new_match.id,
                "game_id": new_match.game_id,
                "mode": new_match.mode,
                "group1_id": party1.group_id,
                "group2_id": party2.group_id,
                "party1_id": party1.id,
                "party2_id": party2.id,
            },
        )
        log_event(
            Events.MATCH_CREATED,
            {"match_id": new_match.id, "state": new_match.state},
        )

        return await cls.get_match_by_id(session, new_match.id)

    @classmethod
    async def get_match_by_id(cls, session: AsyncSession, match_id: int) -> Optional[Match]:
        """Fetch match with eager-loaded relations."""
        stmt = (
            select(Match)
            .where(Match.id == match_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Match.game),
                selectinload(Match.group1).selectinload(Group.stats),
                selectinload(Match.group2).selectinload(Group.stats),
                selectinload(Match.players).selectinload(MatchPlayer.user),
            )
        )
        return await session.scalar(stmt)
