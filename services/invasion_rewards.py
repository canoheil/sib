"""Exactly-once Golden Apple settlement for completed Invasions.

Rewards are real inventory items, not wallet currency. Each eligible player
receives ``apple_gold`` through the canonical ``ItemService.grant`` (an
auditable, row-locked increment of ``user_items``). The Apple wallet is never
touched.

Amounts (``CONFIG_DEFAULTS``): total victory 10, narrow defense 5, defeat 1
Golden Apples per eligible player. Contribution, Brace count, Main Card state,
XP, and Seedlings never change the amount.

Eligibility (defenders only): a participant is eligible only when at least one
persisted ``InvasionDefenseAction`` row exists for it in that Invasion. Joining
alone never qualifies. Rejected, duplicate, expired, and failed actions persist
no such row, so they never qualify either. Legacy events are judged by the same
records and are never assumed to have defended.

Idempotency: the Invasion row lock serializes settlement. ``rewards_settled_at``
is checked under that lock and stamped in the same caller transaction as the
item grants. A rollback leaves the event pending and no items granted. A
committed marker makes every later call a no-op, so retries, restarts, and
concurrent maintenance workers never pay twice. Interrupted or voided battles
(outcome NULL) are never settled.

The caller owns commit/rollback; this service only locks and flushes.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import (
    InvasionDefenseAction,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
)
from sib.services.config_store import ConfigService
from sib.services.invasion import InvasionError, InvasionService
from sib.services.items import ItemService


GOLDEN_APPLE_ITEM_KEY = "apple_gold"
MAX_GOLDEN_APPLE_AMOUNT = (1 << 31) - 1
REWARD_CONFIG_KEYS = {
    InvasionOutcome.TOTAL_VICTORY: "invasion_reward_total_victory_golden_apples",
    InvasionOutcome.NARROW_DEFENSE: "invasion_reward_narrow_defense_golden_apples",
    InvasionOutcome.DEFEAT: "invasion_reward_defeat_golden_apples",
}


def _settled_stamp(now: Optional[datetime]) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class InvasionSettlementError(InvasionError):
    """Base error for an invalid Invasion reward settlement request."""


class InvasionNotCompletedError(InvasionSettlementError):
    """The Invasion has not reached its persisted FINISHED state."""


class InvasionSettlementOutcomeError(InvasionSettlementError):
    """The finished Invasion does not have an approved persisted outcome."""


class InvasionRewardConfigurationError(InvasionSettlementError):
    """An outcome's configured Golden Apple reward is missing or invalid."""


@dataclass(frozen=True)
class InvasionParticipantReward:
    """Golden Apples granted in one settlement call to one eligible player."""

    participant_id: int
    user_id: int
    golden_apples: int


@dataclass(frozen=True)
class InvasionSettlementResult:
    """Summary of one completed-Invasion settlement call.

    ``duplicate`` is True when the event was already settled, so this call
    granted nothing. ``participant_rewards`` lists only grants made by this call.
    """

    invasion_id: int
    outcome: Optional[str]
    participant_rewards: tuple[InvasionParticipantReward, ...]
    duplicate: bool

    @property
    def participant_count(self) -> int:
        return len(self.participant_rewards)

    @property
    def newly_granted_golden_apples(self) -> int:
        return sum(reward.golden_apples for reward in self.participant_rewards)


class InvasionRewardService:
    """Idempotently settle the Golden Apple reward for a finished Invasion."""

    @classmethod
    async def configured_amount(cls, session: AsyncSession, outcome: str) -> int:
        """Return the per-defender Golden Apple amount for an approved outcome."""
        config_key = REWARD_CONFIG_KEYS[outcome]
        raw = await ConfigService.get(session, config_key)
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise InvasionRewardConfigurationError(
                f"{config_key} must be a non-negative integer"
            )
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            raise InvasionRewardConfigurationError(
                f"{config_key} must be a non-negative integer"
            ) from None
        if amount < 0 or amount > MAX_GOLDEN_APPLE_AMOUNT:
            raise InvasionRewardConfigurationError(
                f"{config_key} must be between 0 and {MAX_GOLDEN_APPLE_AMOUNT}"
            )
        return amount

    @staticmethod
    async def eligible_participants(
        session: AsyncSession, invasion_id: int
    ) -> list[InvasionParticipant]:
        """Participants with at least one persisted successful defense action.

        Ordered by user id so grants are made in a stable order. The result is
        read from the defense-action table, never from aggregates or joins.
        """
        defended = (
            select(InvasionDefenseAction.id)
            .where(
                InvasionDefenseAction.invasion_id == invasion_id,
                InvasionDefenseAction.participant_id == InvasionParticipant.id,
            )
            .correlate(InvasionParticipant)
        )
        return list(
            (
                await session.scalars(
                    select(InvasionParticipant)
                    .where(
                        InvasionParticipant.invasion_id == invasion_id,
                        exists(defended),
                    )
                    .order_by(
                        InvasionParticipant.user_id.asc(),
                        InvasionParticipant.id.asc(),
                    )
                )
            ).all()
        )

    @classmethod
    async def settle_completed_invasion(
        cls,
        session: AsyncSession,
        invasion_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> InvasionSettlementResult:
        """Grant Golden Apples to every defender of a finished Invasion, once.

        The Invasion row is the settlement mutex, matching lifecycle and action
        locking. The marker is checked under that lock, so a second caller
        blocks and then sees the committed marker. All item grants and the
        marker are flushed into the caller's transaction, which commits them
        together or not at all.
        """
        # Reuse the canonical strict ID validation, not-found behavior, and
        # parent-row lock used by the Invasion lifecycle and action services.
        invasion = await InvasionService._locked_invasion(session, invasion_id)
        if invasion.status != InvasionStatus.FINISHED:
            raise InvasionNotCompletedError("only a finished Invasion can be settled")
        if invasion.outcome not in InvasionOutcome.ALL:
            raise InvasionSettlementOutcomeError(
                "finished Invasion has no approved persisted outcome"
            )
        if invasion.rewards_settled_at is not None:
            return InvasionSettlementResult(
                invasion_id=int(invasion.id),
                outcome=invasion.outcome,
                participant_rewards=(),
                duplicate=True,
            )

        amount = await cls.configured_amount(session, invasion.outcome)
        participants = await cls.eligible_participants(session, invasion.id)
        rewards: list[InvasionParticipantReward] = []
        for participant in participants:
            if amount > 0:
                await ItemService.grant(
                    session,
                    participant.user_id,
                    GOLDEN_APPLE_ITEM_KEY,
                    amount,
                )
            rewards.append(
                InvasionParticipantReward(
                    participant_id=int(participant.id),
                    user_id=int(participant.user_id),
                    golden_apples=amount,
                )
            )

        # Same transaction as every grant above: the marker can never commit
        # without its items, and items never commit without the marker.
        invasion.rewards_settled_at = _settled_stamp(now)
        await session.flush()
        return InvasionSettlementResult(
            invasion_id=int(invasion.id),
            outcome=invasion.outcome,
            participant_rewards=tuple(rewards),
            duplicate=False,
        )
