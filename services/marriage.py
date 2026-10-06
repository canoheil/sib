"""Marriage acceptance and creation service (Phase 4).

Responsibilities:
- Validate and execute marriage proposal acceptance within a single atomic
  transaction boundary.
- Enforce acceptance preconditions under deterministic row locks:
  A) Proposal exists.
  B) Proposal status is ``PENDING``.
  C) Proposal has not expired (if ``now >= expires_at``, transition the
     proposal to ``EXPIRED`` and raise ``ProposalExpiredError`` without
     creating a ``Marriage`` or consuming a Wedding Ring).
  D) ``actor_user_id == proposal.target_id``.
  E) Both proposer and target exist and are valid non-bot users.
  F) Neither participant currently has an ``ACTIVE`` ``Marriage``.
  G) Neither participant is currently blocked by a remarriage cooldown
     (``DIVORCED`` marriage with ``cooldown_until > now``).
- Re-check Wedding Ring ownership at acceptance time:
  - If the proposer no longer owns a ``wedding_ring`` (e.g. sold while the
    proposal was pending), raise ``WeddingRingMissingAtAcceptanceError``
    without creating a ``Marriage``, without marking the proposal accepted,
    and without consuming anything.
- Consume exactly 1 ``wedding_ring`` via ``ItemService.consume`` atomically
  with ``Marriage`` creation, ``PENDING -> ACCEPTED`` proposal transition,
  and invalidation of all other conflicting ``PENDING`` proposals via
  ``MarriageProposalService.invalidate_pending_for_users``.

Global lock-order policy
------------------------
Every transaction in the relationship + inventory domain acquires its row locks
in one deterministic global order::

    User (ascending id)
      -> Marriage (ascending id)
        -> MarriageProposal (ascending id)
          -> FlowerGift (ascending id)
            -> UserItem (ascending id)

Any two paths that touch the same pair of resource types must agree on their
relative order; otherwise two concurrent transactions can each hold the lock
the other needs and InnoDB resolves it by killing one of them (error 1213).
Concretely:

* ``MarriageProposalService.create_proposal`` **must** lock ``User`` before
  ``MarriageProposal`` - it is about to INSERT the proposal and has no id to
  anchor on - so ``MarriageService.accept_proposal`` has to do the same
  instead of locking its proposal first.
* ``LoveService.send_flower`` locks ``User`` then ``Marriage``, so every
  FlowerGift resolution path must also lock ``User`` then ``Marriage`` and
  only lock the ``FlowerGift`` row itself afterwards.

SQLite has no row-level ``FOR UPDATE`` and cannot deadlock this way, which is
why these cycles are invisible to the race tests; the ordering contract is
pinned by ``tests/test_lock_ordering.py`` instead. The write-intent
``UPDATE ... SET updated_at`` barriers stay in place (and are part of what that
test records) because they are what serializes writers on SQLite.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.logging import Events, log_event
from sib.database.models import (
    FlowerGift,
    Marriage,
    MarriageProposal,
    MarriageStatus,
    ProposalStatus,
    User,
    canonical_spouse_ids,
)
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.marriage_proposal import (
    WEDDING_RING_ITEM_KEY,
    InvalidProposalParticipantError,
    MarriageProposalService,
    NotProposalTargetError,
    ProposalError,
    ProposalExpiredError,
    ProposalInvalidReason,
    ProposalNotFoundError,
    ProposalNotPendingError,
    ProposerAlreadyMarriedError,
    TargetAlreadyMarriedError,
    WeddingRingRequiredError,
    _as_utc,
    _utc_now,
)

DEFAULT_INITIAL_LOVE = 0
DEFAULT_DIVORCE_COOLDOWN_SECONDS = 86400


# ---------------------------------------------------------------------------
# Structured Domain Exceptions
# ---------------------------------------------------------------------------


class MarriageError(ProposalError):
    """Base exception for marriage domain errors."""

    code: str = "marriage_error"


class MarriageNotFoundError(MarriageError):
    """Raised when a requested marriage record does not exist."""

    code = "marriage_not_found"

    def __init__(
        self,
        *,
        marriage_id: Optional[int] = None,
        user_id: Optional[int] = None,
        message: str = "Marriage record not found.",
    ) -> None:
        self.marriage_id = marriage_id
        self.user_id = user_id
        super().__init__(message)


class MarriageAlreadyExistsError(MarriageError):
    """Raised when an active marriage already exists for the pair or participant."""

    code = "marriage_already_exists"

    def __init__(
        self,
        *,
        marriage_id: Optional[int] = None,
        user1_id: Optional[int] = None,
        user2_id: Optional[int] = None,
        message: str = "An active marriage already exists.",
    ) -> None:
        self.marriage_id = marriage_id
        self.user1_id = user1_id
        self.user2_id = user2_id
        super().__init__(message)


class MarriageParticipantAlreadyMarriedError(MarriageAlreadyExistsError):
    """Raised when either the proposer or target already has an ACTIVE marriage."""

    code = "participant_already_married"

    def __init__(
        self,
        *,
        user_id: int,
        marriage_id: Optional[int] = None,
        role: str = "participant",
        message: Optional[str] = None,
    ) -> None:
        self.user_id = int(user_id)
        self.role = role
        super().__init__(
            marriage_id=marriage_id,
            message=message or f"Participant {user_id} ({role}) already has an active marriage.",
        )


class ProposerAlreadyMarriedOnAcceptError(
    MarriageParticipantAlreadyMarriedError, ProposerAlreadyMarriedError
):
    """Proposer became married before this proposal was accepted."""

    code = "proposer_already_married"

    def __init__(self, *, user_id: int, marriage_id: Optional[int] = None) -> None:
        MarriageParticipantAlreadyMarriedError.__init__(
            self,
            user_id=user_id,
            marriage_id=marriage_id,
            role="proposer",
            message=f"Proposer {user_id} already has an active marriage.",
        )


class TargetAlreadyMarriedOnAcceptError(
    MarriageParticipantAlreadyMarriedError, TargetAlreadyMarriedError
):
    """Target became married before this proposal was accepted."""

    code = "target_already_married"

    def __init__(self, *, user_id: int, marriage_id: Optional[int] = None) -> None:
        MarriageParticipantAlreadyMarriedError.__init__(
            self,
            user_id=user_id,
            marriage_id=marriage_id,
            role="target",
            message=f"Target {user_id} already has an active marriage.",
        )


class MarriageCooldownActiveError(MarriageError):
    """Raised when a participant is still within their post-divorce remarriage cooldown."""

    code = "marriage_cooldown_active"

    def __init__(
        self,
        *,
        user_id: int,
        cooldown_until: datetime,
        current_time: Optional[datetime] = None,
        role: str = "participant",
        marriage_id: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        self.user_id = int(user_id)
        self.cooldown_until = _as_utc(cooldown_until)
        self.role = role
        self.marriage_id = marriage_id
        ref_now = _utc_now(current_time)
        self.remaining_seconds = max(
            0, int((self.cooldown_until - ref_now).total_seconds())
        )
        super().__init__(
            message
            or (
                f"Participant {user_id} ({role}) is in remarriage cooldown until "
                f"{self.cooldown_until.isoformat()}."
            )
        )


class ProposalNotAcceptableError(MarriageError, ProposalNotPendingError):
    """Raised when a proposal is not in an acceptable PENDING state."""

    code = "proposal_not_acceptable"

    def __init__(
        self,
        *,
        proposal_id: Optional[int] = None,
        current_status: Optional[str] = None,
        reason: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        self.proposal_id = proposal_id
        self.current_status = current_status
        self.reason = reason
        ProposalNotPendingError.__init__(
            self,
            current_status=current_status,
            message=message
            or f"Proposal {proposal_id} is not acceptable (status={current_status}).",
        )


class MarriageAcceptanceRaceLostError(ProposalNotAcceptableError):
    """Raised when a concurrent transaction resolved or invalidated the proposal first."""

    code = "marriage_acceptance_race_lost"

    def __init__(
        self,
        *,
        proposal_id: Optional[int] = None,
        current_status: Optional[str] = None,
        reason: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        super().__init__(
            proposal_id=proposal_id,
            current_status=current_status,
            reason=reason or "race_lost",
            message=message
            or f"Proposal {proposal_id} lost a concurrent acceptance race (status={current_status}).",
        )


class MarriageNotActiveError(MarriageError):
    """Raised when an operation needs an ``ACTIVE`` marriage but it is not."""

    code = "marriage_not_active"

    def __init__(
        self,
        *,
        marriage_id: Optional[int] = None,
        current_status: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        self.marriage_id = marriage_id
        self.current_status = current_status
        super().__init__(
            message
            or (
                f"Marriage {marriage_id} is not ACTIVE "
                f"(status={current_status})."
            )
        )


class NotMarriageSpouseError(MarriageError):
    """Raised when the actor is not one of the two spouses of a marriage."""

    code = "not_marriage_spouse"

    def __init__(
        self,
        *,
        marriage_id: int,
        actor_user_id: int,
        message: Optional[str] = None,
    ) -> None:
        self.marriage_id = int(marriage_id)
        self.actor_user_id = int(actor_user_id)
        super().__init__(
            message
            or (
                f"User {actor_user_id} is not a spouse of marriage {marriage_id}."
            )
        )


class MarriageDivorceRaceLostError(MarriageNotActiveError):
    """Raised when a concurrent transaction ended the marriage first.

    Subclasses :class:`MarriageNotActiveError` so handlers that catch "the
    marriage is no longer ACTIVE" also cover a lost divorce race.
    """

    code = "marriage_divorce_race_lost"

    def __init__(
        self,
        *,
        marriage_id: Optional[int] = None,
        current_status: Optional[str] = None,
    ) -> None:
        super().__init__(
            marriage_id=marriage_id,
            current_status=current_status,
            message=(
                f"Marriage {marriage_id} was already ended by a concurrent "
                f"divorce (status={current_status})."
            ),
        )


class WeddingRingMissingAtAcceptanceError(MarriageError, WeddingRingRequiredError):
    """Raised when the proposer no longer owns a Wedding Ring at acceptance time."""

    code = "wedding_ring_missing_at_acceptance"

    def __init__(
        self,
        *,
        proposal_id: int,
        proposer_id: int,
        target_id: int,
        group_id: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        self.proposal_id = int(proposal_id)
        self.proposer_id = int(proposer_id)
        self.target_id = int(target_id)
        self.group_id = int(group_id) if group_id is not None else None
        MarriageError.__init__(
            self,
            message
            or (
                f"Proposer {proposer_id} no longer owns a wedding_ring for "
                f"proposal {proposal_id}."
            ),
            code=self.code,
        )


# ---------------------------------------------------------------------------
# Structured Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarriageAcceptanceResult:
    """Structured domain result returned on successful proposal acceptance."""

    marriage: Marriage
    proposal: MarriageProposal
    proposer: User
    accepter: User
    invalidated_proposals: Tuple[MarriageProposal, ...] = ()
    other_proposals_invalidated: bool = False

    @property
    def winning_proposal(self) -> MarriageProposal:
        return self.proposal

    @property
    def id(self) -> int:
        return self.marriage.id

    @property
    def marriage_id(self) -> int:
        return self.marriage.id

    @property
    def proposal_id(self) -> int:
        return self.proposal.id

    @property
    def user1_id(self) -> int:
        return self.marriage.user1_id

    @property
    def user2_id(self) -> int:
        return self.marriage.user2_id

    @property
    def proposer_id(self) -> int:
        return self.marriage.proposer_id

    @property
    def accepter_id(self) -> int:
        return self.marriage.accepter_id

    @property
    def origin_group_id(self) -> Optional[int]:
        return self.marriage.origin_group_id

    @property
    def status(self) -> str:
        return self.marriage.status

    @property
    def love_score(self) -> int:
        return self.marriage.love_score

    @property
    def married_at(self) -> datetime:
        return self.marriage.married_at

    @property
    def invalidated_count(self) -> int:
        return len(self.invalidated_proposals)


# ---------------------------------------------------------------------------
# Structured Result — Divorce
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarriageDivorceResult:
    """Structured domain result returned on a successful divorce."""

    marriage: Marriage
    actor: User
    spouse: User
    invalidated_gifts: Tuple[FlowerGift, ...] = ()
    invalidated_proposals: Tuple[MarriageProposal, ...] = ()
    transition_won: bool = True

    @property
    def flower_gifts(self) -> Tuple[FlowerGift, ...]:
        return self.invalidated_gifts

    @property
    def proposals(self) -> Tuple[MarriageProposal, ...]:
        return self.invalidated_proposals

    @property
    def marriage_id(self) -> int:
        return self.marriage.id

    @property
    def actor_id(self) -> int:
        return self.actor.id

    @property
    def spouse_id(self) -> int:
        return self.spouse.id

    @property
    def status(self) -> str:
        return self.marriage.status

    @property
    def divorced_at(self) -> Optional[datetime]:
        return self.marriage.divorced_at

    @property
    def cooldown_until(self) -> Optional[datetime]:
        return self.marriage.cooldown_until

    @property
    def love_score(self) -> int:
        return self.marriage.love_score

    @property
    def invalidated_gift_count(self) -> int:
        return len(self.invalidated_gifts)

    @property
    def invalidated_proposal_count(self) -> int:
        return len(self.invalidated_proposals)

    @property
    def flower_refund_count(self) -> int:
        """Exactly one Flower is refunded per invalidated PENDING gift."""
        return len(self.invalidated_gifts)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MarriageService:
    """Domain service for accepting marriage proposals and managing Marriage records."""

    _LOCK_RETRIES = 5
    _LOCK_RETRY_DELAY = 0.03

    @classmethod
    async def accept_proposal(
        cls,
        session: AsyncSession,
        proposal_id: int,
        actor_user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> MarriageAcceptanceResult:
        """Accept a ``PENDING`` marriage proposal and atomically create a ``Marriage``.

        Locking order (global policy: User -> Marriage -> MarriageProposal -> UserItem):
        1. Participant ``User`` rows locked in ascending ``id`` order.
        2. Proposal write-intent & ``FOR UPDATE`` lock.
        3. Any ``Marriage`` rows of the participants.
        4. Proposer ``UserItem`` (``wedding_ring``) row locked via ``ItemService``.

        The proposal is read once *without* a lock purely to resolve
        ``proposer_id`` / ``target_id`` (both immutable once created); every
        state check is then re-verified against the locked row. This matters
        because ``create_proposal`` cannot lock a proposal before the
        participant users, so both paths must agree on User-before-Proposal.

        All mutations (ring consumption, marriage creation, winning proposal
        ``PENDING -> ACCEPTED`` CAS transition, and invalidation of other
        pending proposals) execute inside a single savepoint/transaction so
        any failure leaves zero partial state.
        """
        proposal_id = int(proposal_id)
        actor_user_id = int(actor_user_id)
        current_time = _utc_now(now)

        # 0. Identity resolution only (no lock, no write): proposer_id and
        #    target_id are immutable, so the actor check below is final and
        #    lets us refuse a non-target before taking any row lock.
        seed = await MarriageProposalService.get_proposal(session, proposal_id)
        if seed is None:
            raise ProposalNotFoundError()

        # Validate actor is the proposal target (D).
        if actor_user_id != seed.target_id:
            raise NotProposalTargetError()

        proposer_id = int(seed.proposer_id)
        target_id = int(seed.target_id)
        if proposer_id == target_id:
            raise InvalidProposalParticipantError()

        # 1. Lock both user rows in deterministic ascending user ID order.
        users = await cls._lock_users_ascending(session, proposer_id, target_id)

        # 2. Lock any ACTIVE marriage of either participant (global lock order:
        #    User -> Marriage -> MarriageProposal). These are only *used* by
        #    precondition F further down, but the rows must be locked before
        #    the proposal row is locked.
        proposer_active = await cls.get_active_marriage_for_user(
            session, proposer_id, for_update=True
        )
        target_active = await cls.get_active_marriage_for_user(
            session, target_id, for_update=True
        )

        # 3. Lock the proposal row (with SQLite write-intent barrier).
        proposal = await MarriageProposalService._lock_proposal_for_transition(
            session, proposal_id
        )
        if proposal is None:
            raise ProposalNotFoundError()

        if actor_user_id != proposal.target_id:
            raise NotProposalTargetError()

        # Validate proposal status is PENDING (B).
        if proposal.status != ProposalStatus.PENDING:
            if proposal.status == ProposalStatus.EXPIRED:
                raise ProposalExpiredError()
            raise ProposalNotAcceptableError(
                proposal_id=proposal.id,
                current_status=proposal.status,
            )

        # Validate proposal has not expired (C).
        if current_time >= _as_utc(proposal.expires_at):
            await cls._mark_proposal_expired(session, proposal.id, current_time)
            raise ProposalExpiredError()

        expired_inside_lock = False
        # 4. Atomic mutation boundary (savepoint): if any step fails, all
        #    in-session changes inside this block are rolled back.
        async with session.begin_nested():
            # Re-read the proposal under the participant locks in case a
            # concurrent transaction resolved or invalidated it.
            locked_proposal = await MarriageProposalService.get_proposal(
                session, proposal_id, for_update=True
            )
            if locked_proposal is None:
                raise ProposalNotFoundError()

            await session.refresh(locked_proposal)
            if locked_proposal.status != ProposalStatus.PENDING:
                if locked_proposal.status == ProposalStatus.EXPIRED:
                    raise ProposalExpiredError()
                raise MarriageAcceptanceRaceLostError(
                    proposal_id=locked_proposal.id,
                    current_status=locked_proposal.status,
                )

            if current_time >= _as_utc(locked_proposal.expires_at):
                expired_inside_lock = True
            else:
                # E) Both users still exist and are valid non-bot participants.
                proposer = users.get(proposer_id)
                accepter = users.get(target_id)
                if (
                    proposer is None
                    or accepter is None
                    or proposer.is_bot
                    or accepter.is_bot
                ):
                    raise InvalidProposalParticipantError()

                # F) Neither user currently has an ACTIVE Marriage.
                # (Both were re-read under lock at the top of this savepoint.)
                if proposer_active is not None:
                    raise ProposerAlreadyMarriedOnAcceptError(
                        user_id=proposer_id,
                        marriage_id=proposer_active.id,
                    )

                if target_active is not None:
                    raise TargetAlreadyMarriedOnAcceptError(
                        user_id=target_id,
                        marriage_id=target_active.id,
                    )

                # G) Neither user is currently blocked by a remarriage cooldown.
                proposer_cd = await cls.get_active_cooldown_for_user(
                    session, proposer_id, now=current_time
                )
                if proposer_cd is not None:
                    raise MarriageCooldownActiveError(
                        user_id=proposer_id,
                        cooldown_until=proposer_cd,
                        current_time=current_time,
                        role="proposer",
                    )

                target_cd = await cls.get_active_cooldown_for_user(
                    session, target_id, now=current_time
                )
                if target_cd is not None:
                    raise MarriageCooldownActiveError(
                        user_id=target_id,
                        cooldown_until=target_cd,
                        current_time=current_time,
                        role="target",
                    )

                # 3. Ring re-check under row lock.
                ring_qty = await ItemService.get_total(
                    session, proposer_id, WEDDING_RING_ITEM_KEY, for_update=True
                )
                if ring_qty < 1:
                    raise WeddingRingMissingAtAcceptanceError(
                        proposal_id=locked_proposal.id,
                        proposer_id=proposer_id,
                        target_id=target_id,
                        group_id=locked_proposal.group_id,
                    )

                # 4. Ring consumption (atomic with marriage creation).
                await cls._consume_wedding_ring(
                    session,
                    proposer_id=proposer_id,
                    proposal_id=locked_proposal.id,
                    target_id=target_id,
                    group_id=locked_proposal.group_id,
                )

                # 5. Marriage creation.
                love_initial_raw = await ConfigService.get(session, "love_initial")
                love_initial = (
                    int(love_initial_raw)
                    if love_initial_raw is not None
                    else DEFAULT_INITIAL_LOVE
                )
                marriage = await cls._create_marriage_record(
                    session,
                    proposer_id=proposer_id,
                    accepter_id=target_id,
                    origin_group_id=locked_proposal.group_id,
                    proposal_id=locked_proposal.id,
                    love_score=love_initial,
                    married_at=current_time,
                )

                # Post-flush race guard: verify no duplicate ACTIVE marriage
                # exists for either spouse.
                duplicate_active = await session.scalar(
                    select(func.count(Marriage.id)).where(
                        Marriage.status == MarriageStatus.ACTIVE,
                        Marriage.id != marriage.id,
                        or_(
                            Marriage.user1_id.in_((proposer_id, target_id)),
                            Marriage.user2_id.in_((proposer_id, target_id)),
                        ),
                    )
                )
                if duplicate_active and int(duplicate_active) > 0:
                    raise MarriageAcceptanceRaceLostError(
                        proposal_id=locked_proposal.id,
                        reason="duplicate_active_marriage",
                    )

                # 6. Winning proposal state transition (PENDING -> ACCEPTED).
                await cls._transition_proposal_to_accepted(
                    session,
                    proposal_id=locked_proposal.id,
                    target_id=actor_user_id,
                    resolved_at=current_time,
                )

                # 7. Invalidate all other PENDING proposals involving either spouse.
                invalidated_list = await cls._invalidate_conflicting_proposals(
                    session,
                    proposer_id=proposer_id,
                    accepter_id=target_id,
                    winning_proposal_id=locked_proposal.id,
                    now=current_time,
                )

        if expired_inside_lock:
            await cls._mark_proposal_expired(session, proposal_id, current_time)
            raise ProposalExpiredError()

        await session.flush()

        loaded_marriage = await cls.get_marriage(session, marriage.id)
        loaded_proposal = await MarriageProposalService._load_proposal(
            session, proposal_id
        )
        assert loaded_marriage is not None
        assert loaded_proposal is not None

        from sib.services.player_xp import PlayerXPService

        for participant_id in sorted((proposer_id, target_id)):
            await PlayerXPService.award(
                session,
                user_id=participant_id,
                source="first_marriage",
                event_key="first_marriage",
                notification_chat_id=loaded_marriage.origin_group_id,
            )

        log_event(
            Events.MARRIAGE_EVENT,
            {
                "action": "marriage_created",
                "marriage_id": loaded_marriage.id,
                "user1_id": loaded_marriage.user1_id,
                "user2_id": loaded_marriage.user2_id,
                "proposer_id": proposer_id,
                "accepter_id": target_id,
                "origin_group_id": loaded_marriage.origin_group_id,
                "proposal_id": loaded_proposal.id,
                "love_score": loaded_marriage.love_score,
                "invalidated_proposals": [p.id for p in invalidated_list],
            },
        )

        return MarriageAcceptanceResult(
            marriage=loaded_marriage,
            proposal=loaded_proposal,
            proposer=users[proposer_id],
            accepter=users[target_id],
            invalidated_proposals=tuple(invalidated_list),
            other_proposals_invalidated=bool(invalidated_list),
        )

    # ------------------------------------------------------------------
    # Divorce
    # ------------------------------------------------------------------
    @classmethod
    async def divorce_marriage(
        cls,
        session: AsyncSession,
        marriage_id: int,
        actor_user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> MarriageDivorceResult:
        """End an ``ACTIVE`` marriage atomically.

        Locking order (global policy: User -> Marriage -> MarriageProposal ->
        FlowerGift -> UserItem):

        1. ``User`` rows of both spouses, ascending id.
        2. The ``Marriage`` row.
        3. ``MarriageProposal`` invalidation (before any FlowerGift work).
        4. ``FlowerGift`` invalidation + refund (which touches ``UserItem``).

        The marriage id is resolved with a plain (non-locking) read first
        because ``user1_id``/``user2_id`` are canonical and immutable, exactly
        like ``accept_proposal`` does; every state check is then re-verified
        against the locked row.

        Who won the divorce is decided by a single CAS ``UPDATE ... WHERE
        status = 'ACTIVE'``: a losing caller raises
        :class:`MarriageDivorceRaceLostError` instead of returning a result.

        The ``Marriage`` row is never deleted - it stays persisted as
        ``DIVORCED`` so relationship history survives.
        """
        mid = int(marriage_id)
        actor_id = int(actor_user_id)
        current_time = _utc_now(now)

        # 0. Identity resolution only (no lock, no write).
        seed = await cls.get_marriage(session, mid)
        if seed is None:
            raise MarriageNotFoundError(marriage_id=mid)

        spouse_id = seed.spouse_of(actor_id)
        if spouse_id is None:
            raise NotMarriageSpouseError(marriage_id=mid, actor_user_id=actor_id)

        # 1. Lock both spouse rows in deterministic ascending user ID order.
        users = await cls._lock_users_ascending(session, actor_id, spouse_id)

        # 2. Lock the Marriage row (with SQLite write-intent barrier).
        marriage = await cls._lock_marriage(session, mid)
        if marriage is None:
            raise MarriageNotFoundError(marriage_id=mid)

        # Re-verify under the row locks: a concurrent divorce may have ended
        # the marriage between the read above and this lock.
        if marriage.status != MarriageStatus.ACTIVE:
            raise MarriageNotActiveError(
                marriage_id=mid, current_status=marriage.status
            )
        if marriage.spouse_of(actor_id) != spouse_id:
            raise NotMarriageSpouseError(marriage_id=mid, actor_user_id=actor_id)

        actor = users.get(actor_id)
        spouse = users.get(spouse_id)
        if actor is None or spouse is None:
            missing_id = actor_id if actor is None else spouse_id
            raise MarriageNotFoundError(
                marriage_id=mid,
                user_id=missing_id,
                message=f"Spouse user {missing_id} of marriage {mid} not found.",
            )

        # Both spouses get the same remarriage cooldown, stored once on the
        # Marriage row (get_active_cooldown_for_user matches either spouse).
        cooldown_seconds = await cls._get_divorce_cooldown_seconds(session)
        cooldown_until = current_time + timedelta(seconds=cooldown_seconds)

        invalidated_proposals: List[MarriageProposal] = []
        invalidated_gifts: List[FlowerGift] = []
        previous_love = int(marriage.love_score)

        async with session.begin_nested():
            # 3. Atomic CAS: ACTIVE -> DIVORCED. rowcount == 1 is the single
            #    "this call won the transition" check.
            claimed = await session.execute(
                update(Marriage)
                .where(
                    Marriage.id == mid,
                    Marriage.status == MarriageStatus.ACTIVE,
                )
                .values(
                    status=MarriageStatus.DIVORCED,
                    divorced_at=current_time,
                    divorced_by_user_id=actor_id,
                    love_score=0,
                    cooldown_until=cooldown_until,
                )
                .execution_options(synchronize_session=False)
            )
            if (claimed.rowcount or 0) != 1:
                refreshed = await cls.get_marriage(session, mid)
                raise MarriageDivorceRaceLostError(
                    marriage_id=mid,
                    current_status=refreshed.status if refreshed else None,
                )

            # 4. MarriageProposal invalidation. Proposals never consume a
            #    Wedding Ring, so nothing is consumed or refunded here.
            invalidated_proposals = (
                await MarriageProposalService.invalidate_pending_for_users(
                    session,
                    (actor_id, spouse_id),
                    reason=ProposalInvalidReason.USER_DIVORCED,
                    now=current_time,
                )
            )

            # 5. FlowerGift invalidation + refund. LoveService owns the gift
            #    semantics and imports this module, so it is imported lazily to
            #    keep the module-level dependency graph acyclic. The call
            #    itself stays inside this transaction: it re-locks User and
            #    Marriage (already held by this transaction, so it cannot
            #    block) and then takes FlowerGift -> UserItem, which keeps the
            #    global lock order intact.
            from sib.services.love import LoveService  # noqa: PLC0415 - late import: love.py imports this module

            gift_results = await LoveService.invalidate_pending_gifts_for_marriage(
                session, mid, now=current_time
            )
            invalidated_gifts = [result.gift for result in gift_results]

            await session.flush()

        await session.flush()

        # The CAS used synchronize_session=False, so re-read the row.
        loaded = await cls.get_marriage(session, mid)
        assert loaded is not None
        loaded_actor = users[actor_id]
        loaded_spouse = users[spouse_id]

        log_event(
            Events.MARRIAGE_EVENT,
            {
                "action": "marriage_divorced",
                "marriage_id": loaded.id,
                "user1_id": loaded.user1_id,
                "user2_id": loaded.user2_id,
                "actor_user_id": actor_id,
                "spouse_user_id": spouse_id,
                "previous_love_score": previous_love,
                "cooldown_until": cooldown_until.isoformat(),
                "invalidated_gift_ids": [g.id for g in invalidated_gifts],
                "invalidated_proposal_ids": [p.id for p in invalidated_proposals],
            },
        )

        return MarriageDivorceResult(
            marriage=loaded,
            actor=loaded_actor,
            spouse=loaded_spouse,
            invalidated_gifts=tuple(invalidated_gifts),
            invalidated_proposals=tuple(invalidated_proposals),
            transition_won=True,
        )

    # Alias for naming flexibility.
    divorce = divorce_marriage

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @classmethod
    async def get_marriage(
        cls,
        session: AsyncSession,
        marriage_id: int,
        *,
        for_update: bool = False,
    ) -> Optional[Marriage]:
        """Fetch a ``Marriage`` by ID with eager-loaded participants."""
        stmt = (
            select(Marriage)
            .where(Marriage.id == int(marriage_id))
            .execution_options(populate_existing=True)
            .options(
                selectinload(Marriage.user1),
                selectinload(Marriage.user2),
                selectinload(Marriage.proposer),
                selectinload(Marriage.accepter),
                selectinload(Marriage.origin_group),
                selectinload(Marriage.proposal),
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    @classmethod
    async def get_active_marriage_for_user(
        cls,
        session: AsyncSession,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> Optional[Marriage]:
        """Return the user's current ``ACTIVE`` marriage, if any."""
        uid = int(user_id)
        stmt = (
            select(Marriage)
            .where(
                Marriage.status == MarriageStatus.ACTIVE,
                or_(Marriage.user1_id == uid, Marriage.user2_id == uid),
            )
            .options(
                selectinload(Marriage.user1),
                selectinload(Marriage.user2),
                selectinload(Marriage.proposer),
                selectinload(Marriage.accepter),
            )
            .order_by(Marriage.id.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    @classmethod
    async def is_user_married(cls, session: AsyncSession, user_id: int) -> bool:
        """True when ``user_id`` currently has an ``ACTIVE`` marriage."""
        return (await cls.get_active_marriage_for_user(session, user_id)) is not None

    @classmethod
    async def get_active_cooldown_for_user(
        cls,
        session: AsyncSession,
        user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[datetime]:
        """Return the latest active ``cooldown_until`` timestamp (UTC) if the user
        is currently in a post-divorce remarriage cooldown, else ``None``.
        """
        uid = int(user_id)
        current_time = _utc_now(now)
        rows = list(
            (
                await session.scalars(
                    select(Marriage)
                    .where(
                        Marriage.status == MarriageStatus.DIVORCED,
                        or_(Marriage.user1_id == uid, Marriage.user2_id == uid),
                        Marriage.cooldown_until.is_not(None),
                    )
                    .order_by(Marriage.id.desc())
                )
            ).all()
        )
        active_cooldowns: List[datetime] = []
        for m in rows:
            if m.cooldown_until is not None:
                cd_utc = _as_utc(m.cooldown_until)
                if cd_utc > current_time:
                    active_cooldowns.append(cd_utc)
        return max(active_cooldowns) if active_cooldowns else None

    # ------------------------------------------------------------------
    # Internal Atomic Steps (Separated for Clean Testing & Atomicity Verification)
    # ------------------------------------------------------------------
    @classmethod
    async def _consume_wedding_ring(
        cls,
        session: AsyncSession,
        *,
        proposer_id: int,
        proposal_id: int,
        target_id: int,
        group_id: Optional[int],
    ) -> int:
        try:
            return await ItemService.consume(
                session, proposer_id, WEDDING_RING_ITEM_KEY, 1
            )
        except ValueError as exc:
            raise WeddingRingMissingAtAcceptanceError(
                proposal_id=proposal_id,
                proposer_id=proposer_id,
                target_id=target_id,
                group_id=group_id,
            ) from exc

    @classmethod
    async def _create_marriage_record(
        cls,
        session: AsyncSession,
        *,
        proposer_id: int,
        accepter_id: int,
        origin_group_id: Optional[int],
        proposal_id: int,
        love_score: int,
        married_at: datetime,
    ) -> Marriage:
        user1_id, user2_id = canonical_spouse_ids(proposer_id, accepter_id)
        marriage = Marriage(
            user1_id=user1_id,
            user2_id=user2_id,
            proposer_id=proposer_id,
            accepter_id=accepter_id,
            origin_group_id=origin_group_id,
            proposal_id=proposal_id,
            status=MarriageStatus.ACTIVE,
            love_score=love_score,
            married_at=married_at,
        )
        session.add(marriage)
        await session.flush()
        return marriage

    @classmethod
    async def _transition_proposal_to_accepted(
        cls,
        session: AsyncSession,
        *,
        proposal_id: int,
        target_id: int,
        resolved_at: datetime,
    ) -> None:
        claimed = await session.execute(
            update(MarriageProposal)
            .where(
                MarriageProposal.id == proposal_id,
                MarriageProposal.target_id == target_id,
                MarriageProposal.status == ProposalStatus.PENDING,
            )
            .values(
                status=ProposalStatus.ACCEPTED,
                resolved_at=resolved_at,
            )
            .execution_options(synchronize_session=False)
        )
        if (claimed.rowcount or 0) != 1:
            raise MarriageAcceptanceRaceLostError(proposal_id=proposal_id)

    @classmethod
    async def _invalidate_conflicting_proposals(
        cls,
        session: AsyncSession,
        *,
        proposer_id: int,
        accepter_id: int,
        winning_proposal_id: int,
        now: datetime,
    ) -> List[MarriageProposal]:
        return await MarriageProposalService.invalidate_pending_for_users(
            session,
            (proposer_id, accepter_id),
            reason=ProposalInvalidReason.OTHER_PROPOSAL_ACCEPTED,
            exclude_proposal_id=winning_proposal_id,
            now=now,
        )

    @staticmethod
    async def _mark_proposal_expired(
        session: AsyncSession, proposal_id: int, current_time: datetime
    ) -> None:
        await session.execute(
            update(MarriageProposal)
            .where(
                MarriageProposal.id == int(proposal_id),
                MarriageProposal.status == ProposalStatus.PENDING,
            )
            .values(
                status=ProposalStatus.EXPIRED,
                resolved_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        await session.flush()
        await MarriageProposalService._load_proposal(session, proposal_id)

    @classmethod
    async def _lock_marriage(
        cls, session: AsyncSession, marriage_id: int
    ) -> Optional[Marriage]:
        """Row-lock a ``Marriage`` by id.

        Single shared implementation used by divorce and by the FlowerGift
        resolution paths in ``LoveService`` (which delegates here), so every
        caller acquires the ``Marriage`` lock the same way: an
        ``UPDATE ... SET updated_at`` write-intent barrier first (this is what
        serializes writers on SQLite, where ``FOR UPDATE`` is a no-op) and then
        ``SELECT ... FOR UPDATE`` (the real row lock on MariaDB/MySQL).
        """
        mid = int(marriage_id)
        for attempt in range(cls._LOCK_RETRIES):
            try:
                await session.execute(
                    update(Marriage)
                    .where(Marriage.id == mid)
                    .values(updated_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                stmt = (
                    select(Marriage)
                    .where(Marriage.id == mid)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                return await session.scalar(stmt)
            except OperationalError:
                if attempt + 1 < cls._LOCK_RETRIES:
                    await asyncio.sleep(cls._LOCK_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        return None

    @staticmethod
    async def _get_divorce_cooldown_seconds(session: AsyncSession) -> int:
        """Configured post-divorce remarriage cooldown (``0`` disables it)."""
        raw = await ConfigService.get(session, "marriage_divorce_cooldown_seconds")
        seconds = int(raw) if raw is not None else DEFAULT_DIVORCE_COOLDOWN_SECONDS
        return max(0, seconds)

    @classmethod
    async def _lock_users_ascending(
        cls, session: AsyncSession, user_a: int, user_b: int
    ) -> Dict[int, User]:
        """Lock both participant ``User`` rows in deterministic ascending ID order."""
        ordered_ids = sorted({int(user_a), int(user_b)})
        for attempt in range(cls._LOCK_RETRIES):
            try:
                for uid in ordered_ids:
                    await session.execute(
                        update(User)
                        .where(User.id == uid)
                        .values(updated_at=func.now())
                        .execution_options(synchronize_session=False)
                    )
                rows = await session.scalars(
                    select(User)
                    .where(User.id.in_(ordered_ids))
                    .order_by(User.id.asc())
                    .with_for_update()
                )
                return {u.id: u for u in rows.all()}
            except OperationalError:
                if attempt + 1 < cls._LOCK_RETRIES:
                    await asyncio.sleep(cls._LOCK_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        return {}
