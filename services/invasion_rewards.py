"""Outcome-based, retry-safe settlement for completed Invasions.

V1 pays Apples only, using the persisted Invasion outcome and persisted
InvasionParticipant rows. Default amounts live in ``CONFIG_DEFAULTS``:
TOTAL_VICTORY=10, NARROW_DEFENSE=5, and DEFEAT=1 consolation Apple per
participant. Contribution, Brace actions, Main Card state, XP, and Seedlings do
not affect this policy.

Each participant's existing AppleTransaction idempotency key is also the
settlement record. If an event is partially settled, its first ledger amount
is reused for remaining participants even if runtime config changes before a
retry. The caller owns commit/rollback; this service only locks and flushes.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import (
    AppleTransaction,
    Invasion,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.invasion import InvasionError, InvasionService


MAX_APPLE_AMOUNT = (1 << 31) - 1
APPLE_REWARD_REASON = "invasion_reward"
APPLE_REWARD_REF_TYPE = "invasion"
APPLE_REWARD_CONFIG_KEYS = {
    InvasionOutcome.TOTAL_VICTORY: "invasion_reward_total_victory_apples",
    InvasionOutcome.NARROW_DEFENSE: "invasion_reward_narrow_defense_apples",
    InvasionOutcome.DEFEAT: "invasion_reward_defeat_apples",
}


class InvasionSettlementError(InvasionError):
    """Base error for an invalid Invasion reward settlement request."""


class InvasionNotCompletedError(InvasionSettlementError):
    """The Invasion has not reached its persisted FINISHED state."""


class InvasionSettlementOutcomeError(InvasionSettlementError):
    """The finished Invasion does not have an approved persisted outcome."""


class InvasionRewardConfigurationError(InvasionSettlementError):
    """An outcome's configured Apple reward is missing or invalid."""


class InvasionRewardLedgerConflictError(InvasionSettlementError):
    """A deterministic Invasion reward key belongs to another ledger payload."""


@dataclass(frozen=True)
class InvasionParticipantReward:
    """The persisted Apple settlement result for one Invasion participant."""

    participant_id: int
    user_id: int
    apples: int
    already_settled: bool


@dataclass(frozen=True)
class InvasionSettlementResult:
    """Summary of one completed-Invasion settlement call."""

    invasion_id: int
    outcome: str
    participant_rewards: tuple[InvasionParticipantReward, ...]
    duplicate: bool
    newly_granted_apples: int

    @property
    def participant_count(self) -> int:
        return len(self.participant_rewards)

    @property
    def total_apples(self) -> int:
        """Total Apple amount represented by all participant ledger entries."""
        return sum(reward.apples for reward in self.participant_rewards)

    @property
    def newly_settled_participants(self) -> int:
        return sum(not reward.already_settled for reward in self.participant_rewards)


class InvasionRewardService:
    """Idempotently settle the V1 Apple reward for a finished Invasion."""

    @staticmethod
    def _idempotency_key(invasion_id: int, participant_id: int) -> str:
        # At signed BIGINT limits this remains below AppleTransaction's
        # String(100) key column. The unique wallet-ledger key is the durable
        # per-Invasion/per-participant/per-reward settlement marker.
        return f"invasion:{invasion_id}:participant:{participant_id}:apples"

    @classmethod
    async def _configured_amount(
        cls, session: AsyncSession, outcome: str
    ) -> int:
        config_key = APPLE_REWARD_CONFIG_KEYS[outcome]
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
        if amount < 0 or amount > MAX_APPLE_AMOUNT:
            raise InvasionRewardConfigurationError(
                f"{config_key} must be between 0 and {MAX_APPLE_AMOUNT}"
            )
        return amount

    @staticmethod
    def _validate_existing_ledger(
        ledger: AppleTransaction,
        *,
        invasion: Invasion,
        participant: InvasionParticipant,
    ) -> None:
        expected_key = InvasionRewardService._idempotency_key(
            invasion.id, participant.id
        )
        if (
            ledger.idempotency_key != expected_key
            or ledger.user_id != participant.user_id
            or ledger.reason != APPLE_REWARD_REASON
            or ledger.ref_type != APPLE_REWARD_REF_TYPE
            or ledger.ref_id != invasion.id
            or ledger.amount < 0
        ):
            raise InvasionRewardLedgerConflictError(
                "Invasion Apple reward key is already used by a different ledger payload"
            )

    @classmethod
    async def settle_completed_invasion(
        cls,
        session: AsyncSession,
        invasion_id: int,
    ) -> InvasionSettlementResult:
        """Settle every persisted participant for an already-finished Invasion.

        The Invasion row is the settlement mutex, matching lifecycle/action
        locking. Participant rows are locked in user-id order before wallet
        rows are reached through ``AppleWalletService``. The stored outcome is
        consumed as-is; this method never calculates or changes gameplay state.

        All changes are flushed into the caller's transaction. No commit,
        rollback, notification, Main Card lookup, or Telegram operation occurs
        here. Repeated calls read the ledger entries first and pay only keys
        which are still missing.
        """
        # Reuse the canonical strict ID validation, not-found behavior, and
        # parent-row lock used by the Invasion lifecycle and action services.
        invasion = await InvasionService._locked_invasion(session, invasion_id)
        if invasion.status != InvasionStatus.FINISHED:
            raise InvasionNotCompletedError(
                "only a finished Invasion can be settled"
            )
        if invasion.outcome not in InvasionOutcome.ALL:
            raise InvasionSettlementOutcomeError(
                "finished Invasion has no approved persisted outcome"
            )

        participants = list(
            (
                await session.scalars(
                    select(InvasionParticipant)
                    .where(InvasionParticipant.invasion_id == invasion.id)
                    .order_by(
                        InvasionParticipant.user_id.asc(),
                        InvasionParticipant.id.asc(),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if not participants:
            return InvasionSettlementResult(
                invasion_id=invasion.id,
                outcome=invasion.outcome,
                participant_rewards=(),
                duplicate=True,
                newly_granted_apples=0,
            )

        participants_by_key = {
            cls._idempotency_key(invasion.id, participant.id): participant
            for participant in participants
        }
        existing_ledgers = list(
            (
                await session.scalars(
                    select(AppleTransaction)
                    .where(
                        AppleTransaction.idempotency_key.in_(
                            tuple(participants_by_key)
                        )
                    )
                    .order_by(AppleTransaction.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        ledger_by_key: dict[str, AppleTransaction] = {}
        for ledger in existing_ledgers:
            participant = participants_by_key.get(ledger.idempotency_key)
            if participant is None:
                # Defensive; the query's key filter should make this impossible.
                raise InvasionRewardLedgerConflictError(
                    "Invasion Apple reward key does not identify a participant"
                )
            cls._validate_existing_ledger(
                ledger,
                invasion=invasion,
                participant=participant,
            )
            ledger_by_key[ledger.idempotency_key] = ledger

        # Existing per-player ledgers preserve the event's first policy value
        # through partial failures and live config changes. If no participant
        # has been paid yet, use the currently configured V1 outcome amount.
        existing_amounts = {int(row.amount) for row in existing_ledgers}
        if len(existing_amounts) > 1:
            raise InvasionRewardLedgerConflictError(
                "participants in one Invasion have inconsistent Apple reward amounts"
            )
        if existing_amounts:
            reward_amount = existing_amounts.pop()
        else:
            reward_amount = await cls._configured_amount(session, invasion.outcome)

        rewards: list[InvasionParticipantReward] = []
        newly_granted_apples = 0
        for participant in participants:
            key = cls._idempotency_key(invasion.id, participant.id)
            existing = ledger_by_key.get(key)
            if existing is not None:
                rewards.append(
                    InvasionParticipantReward(
                        participant_id=participant.id,
                        user_id=participant.user_id,
                        apples=int(existing.amount),
                        already_settled=True,
                    )
                )
                continue

            await AppleWalletService.earn(
                session,
                user_id=participant.user_id,
                amount=reward_amount,
                reason=APPLE_REWARD_REASON,
                ref_type=APPLE_REWARD_REF_TYPE,
                ref_id=invasion.id,
                idempotency_key=key,
            )
            rewards.append(
                InvasionParticipantReward(
                    participant_id=participant.id,
                    user_id=participant.user_id,
                    apples=reward_amount,
                    already_settled=False,
                )
            )
            newly_granted_apples += reward_amount

        await session.flush()
        return InvasionSettlementResult(
            invasion_id=invasion.id,
            outcome=invasion.outcome,
            participant_rewards=tuple(rewards),
            duplicate=all(reward.already_settled for reward in rewards),
            newly_granted_apples=newly_granted_apples,
        )
