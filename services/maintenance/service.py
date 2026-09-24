"""Background maintenance worker for sib."""

import asyncio

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.timeout import TimeoutService


class MaintenanceService:
    """Runs periodic deadline and maintenance processing."""

    INTERVAL_SECONDS = 5

    @classmethod
    async def process_expired_queue_entries(cls, session) -> int:
        """Expire matchmaking queue entries whose deadline has passed."""
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sib.database.models import (
            MatchmakingQueue,
            Party,
            PartyStatus,
        )

        now = datetime.now(timezone.utc)

        stmt = (
            select(MatchmakingQueue)
            .where(MatchmakingQueue.is_active == True)
            .where(MatchmakingQueue.expires_at < now)
            .with_for_update()
        )

        entries = list((await session.scalars(stmt)).all())
        processed = 0

        for entry in entries:
            entry.is_active = False

            party = await session.scalar(
                select(Party).where(Party.id == entry.party_id)
            )

            if party and party.status == PartyStatus.IN_QUEUE:
                party.status = PartyStatus.FULL

            processed += 1

        if processed:
            await session.commit()
            logger.info(
                f"Expired {processed} matchmaking queue entries"
            )

        return processed

    @classmethod
    async def run_once(cls) -> None:
        """Run one maintenance cycle."""
        async with get_session() as session:
            try:
                await TimeoutService.process_acceptance_timeouts(session)
                await TimeoutService.process_turn_timeouts(session)
                await cls.process_expired_queue_entries(session)
            except Exception:
                await session.rollback()
                logger.exception("Maintenance cycle failed")

    @classmethod
    async def run_forever(cls, stop_event: asyncio.Event) -> None:
        """Run maintenance cycles until stop_event is set."""
        logger.info(
            f"Maintenance worker started (interval={cls.INTERVAL_SECONDS}s)"
        )

        try:
            while not stop_event.is_set():
                await cls.run_once()

                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=cls.INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("Maintenance worker cancelled")
            raise
        finally:
            logger.info("Maintenance worker stopped")
