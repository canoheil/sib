"""Service for enforcing the One-Active-Session rule.

This module is the SINGLE SOURCE OF TRUTH for what counts as a live
("blocking") game session:

- ``ACTIVE_PARTY_STATUSES``: party statuses that occupy a player.
- ``ACTIVE_MATCH_STATES``: match states that occupy a player.
- ``live_session_for_user``: the one predicate the guard uses.
- ``get_live_parties_for_group``: the one listing the Active Games UI uses.

Validation (``has_active_session``) and presentation (the group Active
Games screen) are two views of the SAME definition, so a state that blocks
the player can never be invisible in the UI.

``reconcile_stale_state`` repairs rows that block a player without a live
game behind them (the "ghost active game" bug): a party left IN_MATCH
whose match is gone/terminal, a party left IN_QUEUE whose queue entry is
gone, and legacy matches that can never make progress (they are cancelled
so their parties/players are released). It is safe to run repeatedly and
is called on startup and from every maintenance cycle.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.logging import log_event, logger
from sib.database.models import (
    Match,
    MatchPlayer,
    MatchState,
    MatchmakingQueue,
    Party,
    PartyMember,
    PartyStatus,
    User,
)


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

    TERMINAL_MATCH_STATES = (
        MatchState.FINISHED,
        MatchState.CANCELLED,
    )

    # A match in a live state that cannot make progress (no deadline / no
    # acceptance flow behind it) is considered stale after this grace
    # period and is cancelled. Generous enough to never touch a match that
    # is simply being created right now.
    STALE_MATCH_GRACE_SECONDS = 600

    # ------------------------------------------------------------------
    # The single definition
    # ------------------------------------------------------------------
    @classmethod
    async def live_party_for_user(
        cls, session: AsyncSession, user_id: int, *, lock: bool = False
    ) -> Optional[Party]:
        """The live party that blocks this user, if any (same statuses as UI)."""
        stmt = (
            select(Party)
            .join(PartyMember, PartyMember.party_id == Party.id)
            .where(PartyMember.user_id == user_id)
            .where(Party.status.in_(cls.ACTIVE_PARTY_STATUSES))
            .order_by(Party.id.desc())
            .limit(1)
        )
        if lock:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    @classmethod
    async def live_match_for_user(
        cls, session: AsyncSession, user_id: int, *, lock: bool = False
    ) -> Optional[Match]:
        """The live match that blocks this user, if any."""
        stmt = (
            select(Match)
            .join(MatchPlayer, MatchPlayer.match_id == Match.id)
            .where(MatchPlayer.user_id == user_id)
            .where(MatchPlayer.is_active == True)  # noqa: E712
            .where(Match.state.in_(cls.ACTIVE_MATCH_STATES))
            .order_by(Match.id.desc())
            .limit(1)
        )
        if lock:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    @classmethod
    async def live_session_for_user(
        cls, session: AsyncSession, user_id: int
    ) -> Optional[str]:
        """'party' | 'match' | 'duel' | None - THE live-session predicate.

        Both the guard and the informational message are derived from this
        single method, and the UI lists exactly the parties/states this
        method considers live.
        """
        if await cls.live_party_for_user(session, user_id):
            return "party"
        if await cls.live_match_for_user(session, user_id):
            return "match"
        from sib.database.models import AppleDuel

        # Locking current-read: MySQL REPEATABLE READ must not use a snapshot
        # established before the caller waited for the shared User mutex.
        duel_id = await session.scalar(
            select(AppleDuel.id)
            .where((AppleDuel.inviter_id == user_id) | (AppleDuel.recipient_id == user_id))
            .where((AppleDuel.status == "ACCEPTED") | (
                (AppleDuel.status == "PENDING") &
                (AppleDuel.expires_at > datetime.now(timezone.utc))
            ))
            .with_for_update()
        )
        if duel_id:
            return "duel"
        return None

    @classmethod
    async def list_live_parties(
        cls, session: AsyncSession, *, group_id: Optional[int] = None
    ) -> List[Party]:
        """Every live party (optionally of one group) - the UI's source list."""
        stmt = select(Party).where(Party.status.in_(cls.ACTIVE_PARTY_STATUSES))
        if group_id is not None:
            stmt = stmt.where(Party.group_id == group_id)
        stmt = (
            stmt.options(
                selectinload(Party.members).selectinload(PartyMember.user),
                selectinload(Party.game),
            )
            .order_by(Party.created_at.desc())
        )
        return list((await session.scalars(stmt)).all())

    @classmethod
    async def get_live_parties_for_group(
        cls, session: AsyncSession, group_id: int
    ) -> List[Party]:
        """Live parties of one group: same definition as the guard."""
        return await cls.list_live_parties(session, group_id=group_id)

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    @classmethod
    async def lock_user(cls, session: AsyncSession, user_id: int) -> None:
        """Serialize concurrent session-guard checks for one user.

        Two quick taps can otherwise both pass the check before either
        party row exists; the user row lock makes the check-then-create
        sequence atomic on MySQL/MariaDB (a no-op on SQLite).
        """
        await session.execute(
            select(User.id).where(User.id == user_id).with_for_update()
        )

    @classmethod
    async def has_active_session(cls, session: AsyncSession, user_id: int) -> bool:
        """Check if user currently has an active party, queue, or match."""
        return (await cls.live_session_for_user(session, user_id)) is not None

    @classmethod
    async def get_active_session_info(
        cls, session: AsyncSession, user_id: int
    ) -> Optional[str]:
        """Friendly description of the active session, from the same predicate."""
        kind = await cls.live_session_for_user(session, user_id)
        if kind == "party":
            party = await cls.live_party_for_user(session, user_id)
            return f"پارتی بازی (شناسه: {party.id})" if party else None
        if kind == "match":
            match = await cls.live_match_for_user(session, user_id)
            return f"مسابقه فعال (شناسه: {match.id})" if match else None
        if kind == "duel":
            return "دعوت یا دوئل سیبی فعال؛ به پیام دعوت در گروه برگرد."
        return None

    # ------------------------------------------------------------------
    # Consistency repair ("ghost active game" cleanup)
    # ------------------------------------------------------------------
    @staticmethod
    def _ago(moment: Optional[datetime], now: datetime) -> Optional[float]:
        """Seconds since ``moment`` (naive datetimes are treated as UTC)."""
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (now - moment).total_seconds()

    @classmethod
    async def _restore_party(cls, party: Party) -> None:
        """Release an occupied party back to a joinable/waiting status."""
        party.status = PartyStatus.FULL if party.is_full else PartyStatus.OPEN

    @classmethod
    async def reconcile_stale_state(cls, session: AsyncSession) -> Dict[str, int]:
        """Repair blocking rows that have no live game behind them.

        Idempotent; safe to call on startup and on every maintenance pass.

        Rules:
        1. A party in IN_MATCH whose match is missing or terminal is
           restored (FULL when full, else OPEN).
        2. A party in IN_QUEUE without an active queue entry is restored.
        3. A match in a live state that can never progress (no deadline
           and older than the grace period) is CANCELLED and its parties
           restored, so it stops blocking its players.
        4. ``MatchPlayer.is_active`` flags on terminal matches are cleared.

        Returns counters for logging/tests.
        """
        now = datetime.now(timezone.utc)
        stats = {
            "zombie_parties": 0,
            "orphan_queue_parties": 0,
            "stale_matches": 0,
            "released_players": 0,
        }

        # 1. Zombie parties: IN_MATCH without a live match.
        zombie_parties = list(
            (
                await session.scalars(
                    select(Party)
                    .where(Party.status == PartyStatus.IN_MATCH)
                    .with_for_update()
                )
            ).all()
        )
        for party in zombie_parties:
            live_match = await session.scalar(
                select(Match)
                .where(
                    ((Match.party1_id == party.id) | (Match.party2_id == party.id))
                    & (Match.state.in_(cls.ACTIVE_MATCH_STATES))
                )
                .order_by(Match.id.desc())
                .limit(1)
            )
            if live_match is None:
                await cls._restore_party(party)
                stats["zombie_parties"] += 1
                log_event(
                    "SESSION_RECONCILED",
                    {"party_id": party.id, "reason": "party_in_match_without_match"},
                )

        # 2. Orphaned queue parties: IN_QUEUE without an active queue row.
        queue_parties = list(
            (
                await session.scalars(
                    select(Party)
                    .where(Party.status == PartyStatus.IN_QUEUE)
                    .with_for_update()
                )
            ).all()
        )
        for party in queue_parties:
            entry = await session.scalar(
                select(MatchmakingQueue)
                .where(MatchmakingQueue.party_id == party.id)
                .where(MatchmakingQueue.is_active == True)  # noqa: E712
                .limit(1)
            )
            if entry is None:
                await cls._restore_party(party)
                stats["orphan_queue_parties"] += 1
                log_event(
                    "SESSION_RECONCILED",
                    {"party_id": party.id, "reason": "queue_entry_missing"},
                )

        # 3. Matches in live states that can never make progress.
        live_matches = list(
            (
                await session.scalars(
                    select(Match)
                    .where(Match.state.in_(cls.ACTIVE_MATCH_STATES))
                    .with_for_update()
                )
            ).all()
        )
        grace = cls.STALE_MATCH_GRACE_SECONDS
        for match in live_matches:
            age = cls._ago(match.created_at, now)
            stale = False
            if match.state in (MatchState.WAITING_FOR_OPPONENT, MatchState.OPPONENT_FOUND):
                # Legacy states no current flow produces: no deadline, no
                # screen, no way out.
                stale = age is not None and age > grace
            elif match.state == MatchState.WAITING_FOR_ACCEPTANCE:
                # Only stale when the acceptance flow itself is missing.
                stale = (
                    match.acceptance_deadline is None
                    and age is not None
                    and age > grace
                )
            elif match.state == MatchState.ACTIVE:
                # An active match without any deadline can never time out,
                # unless it is an Apple Battle match with a persisted result
                # that can be resumed or finalized.
                stale = (
                    match.turn_deadline is None
                    and age is not None
                    and age > grace
                )
                if stale:
                    from sib.services.apple_battle import AppleBattleService

                    if (
                        await AppleBattleService.is_apple_battle_match(session, match)
                        and await AppleBattleService.has_persisted_result(session, match)
                    ):
                        stale = False
            if not stale:
                continue

            match.state = MatchState.CANCELLED
            stats["stale_matches"] += 1
            for party_id in (match.party1_id, match.party2_id):
                if not party_id:
                    continue
                party = await session.scalar(
                    select(Party).where(Party.id == party_id).with_for_update()
                )
                if party and party.status == PartyStatus.IN_MATCH:
                    await cls._restore_party(party)
            log_event(
                "SESSION_RECONCILED",
                {"match_id": match.id, "reason": "match_cannot_progress"},
            )

        # 4. Active flags on terminal matches.
        result = await session.execute(
            update(MatchPlayer)
            .where(MatchPlayer.is_active == True)  # noqa: E712
            .where(
                MatchPlayer.match_id.in_(
                    select(Match.id).where(Match.state.in_(cls.TERMINAL_MATCH_STATES))
                )
            )
            .values(is_active=False)
        )
        stats["released_players"] = int(result.rowcount or 0)

        if any(stats.values()):
            await session.flush()
            logger.info(f"Session state reconciled: {stats}")
        return stats
