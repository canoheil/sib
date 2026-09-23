"""Recovery service for handling interrupted state on system restart."""

from datetime import datetime, timezone
from typing import Any, Dict
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from goje.core.logging import logger, log_event, Events
from goje.database.models import Match, MatchState, MatchmakingQueue, Party, PartyStatus
from goje.services.timeout import TimeoutService


class RecoveryService:
    """Service responsible for safe system recovery and orphaned state cleanup on startup."""

    @classmethod
    async def recover_on_startup(cls, session: AsyncSession) -> Dict[str, Any]:
        """
        Execute recovery routines on bot startup:
        1. Expire matches waiting for acceptance that exceeded acceptance deadline.
        2. Clean up stale matchmaking queue entries and restore parties.
        3. Terminate stale/abandoned active matches.
        """
        now = datetime.now(timezone.utc)
        stats = {
            "expired_acceptance_matches": 0,
            "cleared_queue_entries": 0,
            "expired_abandoned_matches": 0,
        }

        # 1. Matches stuck in WAITING_FOR_ACCEPTANCE past deadline
        stmt = (
            select(Match)
            .where(
                Match.state == MatchState.WAITING_FOR_ACCEPTANCE,
                Match.acceptance_deadline < now,
            )
            .with_for_update()
        )
        expired_acceptance = list((await session.scalars(stmt)).all())
        for m in expired_acceptance:
            m.state = MatchState.CANCELLED
            # Return parties to FULL
            if m.party1_id:
                p1 = await session.scalar(select(Party).where(Party.id == m.party1_id))
                if p1 and p1.status == PartyStatus.IN_MATCH:
                    p1.status = PartyStatus.FULL
            if m.party2_id:
                p2 = await session.scalar(select(Party).where(Party.id == m.party2_id))
                if p2 and p2.status == PartyStatus.IN_MATCH:
                    p2.status = PartyStatus.FULL

            stats["expired_acceptance_matches"] += 1
            log_event(
                Events.MATCH_CANCELLED,
                {"match_id": m.id, "reason": "recovery_acceptance_timeout"},
            )

        # 2. Matchmaking queue entries whose party is no longer waiting or invalid
        queue_stmt = select(MatchmakingQueue).with_for_update()
        queue_items = list((await session.scalars(queue_stmt)).all())
        for q in queue_items:
            party = await session.scalar(select(Party).where(Party.id == q.party_id))
            if not party or party.status != PartyStatus.IN_QUEUE:
                await session.delete(q)
                stats["cleared_queue_entries"] += 1

        # 3. Active matches with expired turn deadlines
        active_stmt = (
            select(Match)
            .where(
                Match.state == MatchState.ACTIVE,
                Match.turn_deadline != None,
                Match.turn_deadline < now,
            )
            .with_for_update()
        )
        expired_active = list((await session.scalars(active_stmt)).all())
        for m in expired_active:
            await TimeoutService._resolve_expired_active_match(session, m)
            stats["expired_abandoned_matches"] += 1

        await session.commit()

        log_event(
            Events.SYSTEM_RESTART_RECOVERY,
            {"status": "completed", **stats},
        )
        logger.info(f"System startup recovery complete: {stats}")
        return stats
