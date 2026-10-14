"""Recovery service for handling interrupted state on system restart."""

from datetime import datetime, timezone
from typing import Any, Dict
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.logging import logger, log_event, Events
from sib.database.models import (
    Group,
    Match,
    MatchPlayer,
    MatchState,
    MatchmakingQueue,
    Party,
    PartyStatus,
)
from sib.services.timeout import TimeoutService


class RecoveryService:
    """Service responsible for safe system recovery and orphaned state cleanup on startup."""

    @classmethod
    async def recover_on_startup(
        cls,
        session: AsyncSession,
        *,
        bot: Any = None,
        now: datetime | None = None,
    ) -> Dict[str, Any]:
        """
        Execute recovery routines on bot startup:
        1. Expire matches waiting for acceptance that exceeded acceptance deadline.
        2. Clean up stale matchmaking queue entries and restore parties.
        3. Recover interrupted Apple Battle matches from persisted state/cursor.
        4. Terminate stale/abandoned active matches.
        5. Expire past-due ACTIVE Invasion v2 rows after downtime without
           settlement. The legacy recovery path is retained but not invoked.
        """
        from sib.services.apple_battle import AppleBattleService

        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        stats = {
            "expired_acceptance_matches": 0,
            "cleared_queue_entries": 0,
            "expired_abandoned_matches": 0,
            "resumed_apple_battles": 0,
            "finalized_apple_battles": 0,
            "expired_marriage_proposals": 0,
            "love_decay_marriages": 0,
            "expired_flower_gifts": 0,
            "invasions_scheduled": 0,
            "duplicate_invasion_occurrences": 0,
            "invasions_started": 0,
            "invasion_wave_transitions": 0,
            "invasions_resolved": 0,
            "invasion_failures": 0,
        }

        # 0. Consistency repair FIRST: release parties/matches that block a
        #    player without a live game behind them ("ghost active game").
        #    Single source of truth: SessionService.
        from sib.services.session import SessionService

        stats.update(await SessionService.reconcile_stale_state(session))

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

        # 2b. Active Apple Battle matches with persisted results:
        #     finalize if narration is complete, or keep cursor and re-arm narration.
        apple_recovery = await AppleBattleService.recover_active_matches(
            session, now=now
        )
        stats["resumed_apple_battles"] = len(apple_recovery["resumed"])
        stats["finalized_apple_battles"] = apple_recovery["finalized"]

        # 3. Active matches with expired turn deadlines
        active_stmt = (
            select(Match)
            .where(
                Match.state == MatchState.ACTIVE,
                Match.turn_deadline != None,
                Match.turn_deadline < now,
            )
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.group1).selectinload(Group.stats),
                selectinload(Match.group2).selectinload(Group.stats),
                selectinload(Match.game),
            )
            .with_for_update()
        )
        expired_active = list((await session.scalars(active_stmt)).all())
        for m in expired_active:
            await TimeoutService._resolve_expired_active_match(session, m)
            stats["expired_abandoned_matches"] += 1

        # 4. Marriage & Love: work that a downtime could have skipped.
        #    - proposals that expired while the bot was offline
        #    - daily Love decay for every UTC day that passed offline
        #    - FlowerGifts left PENDING past their configured lifetime
        #    All are pure delegation to the existing idempotent domain
        #    sweeps - no Marriage/Love logic is reproduced here - and they
        #    are called directly on the shared session, exactly like
        #    ``SessionService.reconcile_stale_state`` and
        #    ``AppleBattleService.recover_active_matches`` above. Running
        #    recovery twice therefore changes nothing the second time, and
        #    the periodic MaintenanceService sweep re-tries within seconds
        #    anything this step could not finish, so a partial failure here
        #    is self-healing rather than lost.
        try:
            from sib.services.love import LoveService
            from sib.services.marriage_proposal import MarriageProposalService

            expired_proposals = await MarriageProposalService.expire_overdue_proposals(
                session, now=now
            )
            decayed = await LoveService.apply_daily_decay(session, now=now)
            expired_gifts = await LoveService.expire_overdue_gifts(session, now=now)

            stats["expired_marriage_proposals"] = len(expired_proposals)
            stats["love_decay_marriages"] = decayed
            stats["expired_flower_gifts"] = len(expired_gifts)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Marriage/Love startup recovery failed: {exc}")

        await session.commit()

        # TODO (PHASE 7 Step 4): InvasionV2Recovery.run_on_startup() will be
        # wired here. Until then, preserve the gated V2 scheduler recovery.
        try:
            from sib.services.maintenance.service import MaintenanceService
            from sib.services.invasion_v2.scheduler import InvasionScheduler

            if await MaintenanceService._invasion_enabled():
                # [INVASION-REWORK] guarded — see docs/invasion-rework/
                await InvasionScheduler.recover(now=now)
        except Exception:
            stats["invasion_failures"] += 1
            logger.exception("Invasion v2 startup recovery failed")

        for match_id in apple_recovery["resumed"]:
            AppleBattleService.schedule_narration(match_id, bot=bot)

        # Phase 2A: battles finalized during recovery never reached their live
        # narration teardown, so their temporary narration messages are deleted
        # here. Best effort only - cleanup can never affect settlement or the
        # final report.
        if apple_recovery.get("finalized_ids"):
            from sib.services.apple_battle_narration import AppleBattleNarration

            for match_id in apple_recovery["finalized_ids"]:
                try:
                    await AppleBattleNarration.cleanup(bot, match_id)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        f"Narration cleanup after recovery failed for match "
                        f"{match_id}: {exc}"
                    )

        log_event(
            Events.SYSTEM_RESTART_RECOVERY,
            {"status": "completed", **stats},
        )
        logger.info(f"System startup recovery complete: {stats}")
        return stats
