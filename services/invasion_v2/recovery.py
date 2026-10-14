"""Read-only reconciliation plus idempotent V2 startup expiration/delivery."""

from datetime import datetime, timezone

from sqlalchemy import and_, func, select

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Inv2Attack, Inv2GroupState, Inv2Invasion, Inv2Reward
from sib.services.maintenance.service import MaintenanceService

from .circuit_breaker import is_group_available
from .enums import InvasionStatus
from .presentation import InvasionPresentation


class InvasionV2Recovery:
    @classmethod
    async def run_on_startup(cls, bot) -> dict:
        """Expire overdue battles, retry VICTORY photos, and warn on bad data.

        No rewards or result counters are granted/changed here. Delivery is
        best-effort: only a successful photo stamps delivered_at. A crash in
        the gap between Telegram acceptance and DB commit can cause a replay.
        """
        stats = {"expired": 0, "delivered": 0, "delivery_failed": 0,
                 "reward_mismatches": 0, "group_mismatches": 0, "disabled": False}
        if not await MaintenanceService._invasion_enabled():
            stats["disabled"] = True
            logger.info("inv2 recovery: %s", stats)
            return stats

        now = datetime.now(timezone.utc)
        async with get_session() as session:
            overdue = list((await session.scalars(
                select(Inv2Invasion).where(
                    Inv2Invasion.status == InvasionStatus.ACTIVE.value,
                    Inv2Invasion.ends_at.is_not(None), Inv2Invasion.ends_at < now)
                .order_by(Inv2Invasion.id).with_for_update()
            )).all())
            for invasion in overdue:
                invasion.status = InvasionStatus.EXPIRED.value
                invasion.finalized_at = now
            stats["expired"] = len(overdue)
            pending = list((await session.execute(
                select(Inv2Invasion.id, Inv2Invasion.group_id).where(
                    Inv2Invasion.status == InvasionStatus.VICTORY.value,
                    Inv2Invasion.delivered_at.is_(None)
                ).order_by(Inv2Invasion.id)
            )).all())
            orphan_rewards = list((await session.execute(
                select(Inv2Reward.id, Inv2Reward.invasion_id, Inv2Reward.user_id)
                .outerjoin(Inv2Attack, and_(
                    Inv2Attack.invasion_id == Inv2Reward.invasion_id,
                    Inv2Attack.user_id == Inv2Reward.user_id))
                .where(Inv2Attack.id.is_(None))
            )).all())
            stats["reward_mismatches"] = len(orphan_rewards)
            for reward_id, invasion_id, user_id in orphan_rewards:
                logger.warning("inv2 reward without attack reward=%s invasion=%s user=%s",
                               reward_id, invasion_id, user_id)
            actual = {(group_id, status): int(count) for group_id, status, count in
                      (await session.execute(
                          select(Inv2Invasion.group_id, Inv2Invasion.status, func.count())
                          .where(Inv2Invasion.status.in_((InvasionStatus.VICTORY.value,
                                                          InvasionStatus.DEFEAT.value)))
                          .group_by(Inv2Invasion.group_id, Inv2Invasion.status)
                      )).all()}
            states = {row.group_id: row for row in (await session.scalars(
                select(Inv2GroupState)
            )).all()}
            group_ids = set(states) | {group_id for group_id, _ in actual}
            for group_id in sorted(group_ids):
                state = states.get(group_id)
                wins = actual.get((group_id, InvasionStatus.VICTORY.value), 0)
                losses = actual.get((group_id, InvasionStatus.DEFEAT.value), 0)
                if (int(state.total_victories) if state else 0) != wins or (
                    int(state.total_defeats) if state else 0
                ) != losses:
                    stats["group_mismatches"] += 1
                    logger.warning("inv2 group totals mismatch group=%s actual=%s/%s stored=%s/%s",
                                   group_id, wins, losses,
                                   state.total_victories if state else 0,
                                   state.total_defeats if state else 0)

        # The first transaction has committed. Do not hold an invasion lock
        # while the existing metered Telegram adapter writes breaker state.
        presenter = InvasionPresentation()
        for invasion_id, group_id in pending:
            if bot is None or not await is_group_available(group_id):
                stats["delivery_failed"] += 1
                logger.warning("inv2 recovery final photo deferred invasion=%s group=%s",
                               invasion_id, group_id)
                continue
            try:
                if await presenter.finalize(bot, invasion_id):
                    stats["delivered"] += 1
                else:
                    stats["delivery_failed"] += 1
                    logger.warning("inv2 recovery final photo failed invasion=%s", invasion_id)
            except Exception:
                stats["delivery_failed"] += 1
                logger.warning("inv2 recovery final photo failed invasion=%s", invasion_id,
                               exc_info=True)
        logger.info("inv2 recovery: %s", stats)
        return stats
