"""Marriage proposal lifecycle service (Phase 3).

Responsibilities:
- Create group-scoped marriage proposals (``PENDING``) with a configured
  lifetime (``marriage_proposal_timeout_seconds``, default 300s).
- Enforce proposal creation preconditions:
  1. Proposer and target must be distinct, real (non-bot) users.
  2. Proposer must not currently have an ``ACTIVE`` marriage.
  3. Target must not currently have an ``ACTIVE`` marriage.
  4. Proposer must not already have an active (non-expired) ``PENDING``
     outgoing proposal.
  5. Proposer must own at least one ``wedding_ring`` in ``UserItem``
     inventory (the ring is NOT consumed at proposal creation).
  6. Multiple different proposers may have concurrent ``PENDING`` proposals
     targeting the same user.
- Transition proposals safely out of ``PENDING`` using atomic database
  Compare-And-Swap (``UPDATE ... WHERE id = :id AND status = 'PENDING'``):
  - ``reject_proposal``: only the target user may reject a live ``PENDING``
    proposal -> ``REJECTED`` (ring remains with proposer).
  - ``expire_proposal`` / ``expire_overdue_proposals``: overdue ``PENDING``
    proposals (``now >= expires_at``) -> ``EXPIRED`` (ring remains with
    proposer).
  - ``invalidate_proposal`` / ``invalidate_pending_for_target`` /
    ``invalidate_pending_for_users``: ``PENDING`` -> ``INVALIDATED`` with a
    persisted ``invalid_reason`` (ring remains with proposer).

Concurrency model:
- User rows are locked in deterministic ascending ID order
  (``SELECT ... FOR UPDATE``) and the proposer's row is touched with an
  atomic ``UPDATE`` write-intent barrier so concurrent ``create_proposal``
  calls from the same proposer (e.g. ``A -> B`` and ``A -> C``) serialize on
  both MariaDB/MySQL and SQLite. A post-flush check further verifies that
  only one ``PENDING`` outgoing proposal exists for the proposer before the
  transaction can commit.
- Every terminal transition out of ``PENDING`` uses a conditional ``UPDATE``
  checking ``status == ProposalStatus.PENDING`` and verifies ``rowcount == 1``
  so concurrent resolution/expiry races can never produce two winning
  transitions.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.logging import Events, log_event
from sib.database.models import (
    Group,
    Marriage,
    MarriageProposal,
    MarriageStatus,
    ProposalStatus,
    User,
)
from sib.services.config_store import ConfigService
from sib.services.items import ItemService

WEDDING_RING_ITEM_KEY = "wedding_ring"
DEFAULT_PROPOSAL_TIMEOUT_SECONDS = 300


class ProposalInvalidReason:
    """Standard invalidation reason codes persisted in ``invalid_reason``."""

    TARGET_MARRIED = "target_married"
    PROPOSER_MARRIED = "proposer_married"
    OTHER_PROPOSAL_ACCEPTED = "other_proposal_accepted"
    PROPOSER_MISSING_RING = "proposer_missing_ring"
    USER_INELIGIBLE = "user_ineligible"
    CANCELLED = "cancelled"
    USER_DIVORCED = "user_divorced"


# ---------------------------------------------------------------------------
# Domain Error Hierarchy
# ---------------------------------------------------------------------------


class ProposalError(ValueError):
    """Base exception for all marriage proposal domain errors."""

    code: str = "proposal_error"

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        if code is not None:
            self.code = code
        self.message = message
        super().__init__(message)


class SelfProposalError(ProposalError):
    """Raised when a user attempts to propose to themselves."""

    code = "self_proposal"

    def __init__(self, message: str = "نمی‌تونی به خودت پیشنهاد ازدواج بدی!") -> None:
        super().__init__(message)


class InvalidProposalParticipantError(ProposalError):
    """Raised when the proposer, target, or group is missing or is a bot."""

    code = "invalid_participant"

    def __init__(
        self, message: str = "کاربر یا گروه موردنظر برای پیشنهاد ازدواج معتبر نیست."
    ) -> None:
        super().__init__(message)


class ProposerAlreadyMarriedError(ProposalError):
    """Raised when the proposer already has an ACTIVE marriage."""

    code = "proposer_already_married"

    def __init__(self, message: str = "شما در حال حاضر متأهل هستید.") -> None:
        super().__init__(message)


class TargetAlreadyMarriedError(ProposalError):
    """Raised when the target user already has an ACTIVE marriage."""

    code = "target_already_married"

    def __init__(self, message: str = "کاربر موردنظر در حال حاضر متأهل است.") -> None:
        super().__init__(message)


class ProposerHasPendingProposalError(ProposalError):
    """Raised when the proposer already has an active PENDING outgoing proposal."""

    code = "proposer_has_pending_proposal"

    def __init__(
        self,
        existing_proposal_id: Optional[int] = None,
        message: str = "شما یک پیشنهاد ازدواج در انتظار پاسخ دارید.",
    ) -> None:
        self.existing_proposal_id = existing_proposal_id
        super().__init__(message)


class WeddingRingRequiredError(ProposalError):
    """Raised when the proposer does not own a Wedding Ring."""

    code = "wedding_ring_required"

    def __init__(
        self,
        message: str = "برای ثبت پیشنهاد ازدواج باید حداقل یک حلقه ازدواج (💍) داشته باشی.",
    ) -> None:
        super().__init__(message)


class ProposalNotFoundError(ProposalError):
    """Raised when the requested proposal does not exist."""

    code = "proposal_not_found"

    def __init__(self, message: str = "پیشنهاد ازدواج پیدا نشد.") -> None:
        super().__init__(message)


class NotProposalTargetError(ProposalError):
    """Raised when a user other than the proposal target tries to reject/resolve it."""

    code = "not_proposal_target"

    def __init__(
        self, message: str = "فقط مخاطب این پیشنهاد ازدواج می‌تونه به اون پاسخ بده."
    ) -> None:
        super().__init__(message)


class ProposalNotPendingError(ProposalError):
    """Raised when an operation requires a PENDING proposal but it is already terminal."""

    code = "proposal_not_pending"

    def __init__(
        self,
        current_status: Optional[str] = None,
        message: str = "این پیشنهاد ازدواج دیگر در وضعیت انتظار نیست.",
    ) -> None:
        self.current_status = current_status
        super().__init__(message)


class ProposalExpiredError(ProposalNotPendingError):
    """Raised when a proposal has passed its expiration time."""

    code = "proposal_expired"

    def __init__(self, message: str = "مهلت این پیشنهاد ازدواج به پایان رسیده است.") -> None:
        super().__init__(current_status=ProposalStatus.EXPIRED, message=message)


class ProposalNotExpiredYetError(ProposalError):
    """Raised when ``expire_proposal`` is called before ``expires_at``."""

    code = "proposal_not_expired_yet"

    def __init__(
        self, message: str = "مهلت این پیشنهاد ازدواج هنوز به پایان نرسیده است."
    ) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class MarriageProposalService:
    """Domain service for creating, querying, rejecting, expiring, and invalidating marriage proposals."""

    _LOCK_RETRIES = 5
    _LOCK_RETRY_DELAY = 0.03

    # ------------------------------------------------------------------
    # 1. Create Proposal
    # ------------------------------------------------------------------
    @classmethod
    async def create_proposal(
        cls,
        session: AsyncSession,
        proposer_id: int,
        target_id: int,
        group_id: int,
        message_id: Optional[int] = None,
        *,
        now: Optional[datetime] = None,
    ) -> MarriageProposal:
        """Create a new ``PENDING`` marriage proposal in ``group_id``.

        Enforces:
        - ``proposer_id != target_id`` and both are real non-bot users.
        - Neither proposer nor target has an ``ACTIVE`` marriage.
        - Proposer is not inside the post-divorce remarriage cooldown.
        - Proposer has no other live ``PENDING`` outgoing proposal.
        - Proposer owns at least 1 ``wedding_ring`` (not consumed here).
        """
        proposer_id = int(proposer_id)
        target_id = int(target_id)
        group_id = int(group_id)
        if message_id is not None:
            message_id = int(message_id)

        if proposer_id == target_id:
            raise SelfProposalError()

        current_time = _utc_now(now)

        # Acquire deterministic user locks + write-intent lock on the proposer
        # row so concurrent proposals from the same proposer serialize on both
        # MariaDB/MySQL (FOR UPDATE) and SQLite (write lock).
        users = await cls._lock_participants_for_proposal(
            session, proposer_id, target_id
        )
        proposer = users.get(proposer_id)
        target = users.get(target_id)
        if proposer is None or target is None or proposer.is_bot or target.is_bot:
            raise InvalidProposalParticipantError()

        group = await session.get(Group, group_id)
        if group is None:
            raise InvalidProposalParticipantError("گروه موردنظر برای پیشنهاد ازدواج پیدا نشد.")

        # Sweep any overdue PENDING outgoing proposal of this proposer first
        # so an already-timed-out proposal never blocks a fresh one.
        await cls._expire_overdue_for_proposer(session, proposer_id, current_time)

        # 1. Proposer must NOT currently have an active marriage.
        if await cls._has_active_marriage(session, proposer_id):
            raise ProposerAlreadyMarriedError()

        # 2. Target must NOT currently have an active marriage.
        if await cls._has_active_marriage(session, target_id):
            raise TargetAlreadyMarriedError()

        # 2b. Proposer must NOT be inside the post-divorce remarriage cooldown.
        await cls._assert_proposer_not_in_cooldown(session, proposer_id, current_time)

        # 3. Proposer must NOT already have another active outgoing proposal.
        open_outgoing = await cls._lock_pending_outgoing_for_proposer(
            session, proposer_id
        )
        for existing in open_outgoing:
            if _as_utc(existing.expires_at) > current_time:
                raise ProposerHasPendingProposalError(
                    existing_proposal_id=existing.id
                )

        # 4. Proposer must own at least one wedding_ring (NOT consumed here).
        ring_count = await ItemService.get_total(
            session, proposer_id, WEDDING_RING_ITEM_KEY
        )
        if ring_count < 1:
            raise WeddingRingRequiredError()

        timeout_raw = await ConfigService.get(
            session, "marriage_proposal_timeout_seconds"
        )
        timeout_seconds = (
            int(timeout_raw)
            if timeout_raw is not None
            else DEFAULT_PROPOSAL_TIMEOUT_SECONDS
        )
        if timeout_seconds <= 0:
            timeout_seconds = DEFAULT_PROPOSAL_TIMEOUT_SECONDS

        proposal = MarriageProposal(
            proposer_id=proposer_id,
            target_id=target_id,
            group_id=group_id,
            message_id=message_id,
            status=ProposalStatus.PENDING,
            expires_at=current_time + timedelta(seconds=timeout_seconds),
        )
        session.add(proposal)
        await session.flush()

        # Post-flush race guard: verify no other live PENDING outgoing proposal
        # for this proposer exists in the database snapshot.
        other_pending = list(
            (
                await session.scalars(
                    select(MarriageProposal)
                    .where(
                        MarriageProposal.proposer_id == proposer_id,
                        MarriageProposal.status == ProposalStatus.PENDING,
                        MarriageProposal.id != proposal.id,
                    )
                    .order_by(MarriageProposal.id.asc())
                )
            ).all()
        )
        for other in other_pending:
            if _as_utc(other.expires_at) > current_time:
                raise ProposerHasPendingProposalError(
                    existing_proposal_id=int(other.id)
                )

        log_event(
            Events.MARRIAGE_PROPOSAL_EVENT,
            {
                "action": "created",
                "proposal_id": proposal.id,
                "proposer_id": proposer_id,
                "target_id": target_id,
                "group_id": group_id,
                "message_id": message_id,
                "expires_at": proposal.expires_at.isoformat(),
            },
        )
        return await cls._load_proposal(session, proposal.id)

    # ------------------------------------------------------------------
    # 2. Reject Proposal
    # ------------------------------------------------------------------
    @classmethod
    async def reject_proposal(
        cls,
        session: AsyncSession,
        proposal_id: int,
        actor_user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> MarriageProposal:
        """Explicitly reject a ``PENDING`` proposal.

        Rules:
        - Only ``proposal.target_id`` may reject.
        - Only ``PENDING`` proposals that have not expired can be rejected.
        - If the proposal is ``PENDING`` and ``now >= expires_at``, it is
          transitioned to ``EXPIRED`` and ``ProposalExpiredError`` is raised.
        - Uses an atomic conditional ``UPDATE`` so concurrent resolution/expiry
          cannot both succeed.
        - Proposer keeps their Wedding Ring.
        """
        proposal_id = int(proposal_id)
        actor_user_id = int(actor_user_id)
        current_time = _utc_now(now)

        proposal = await cls._lock_proposal_for_transition(session, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError()

        if actor_user_id != proposal.target_id:
            raise NotProposalTargetError()

        if proposal.status != ProposalStatus.PENDING:
            if proposal.status == ProposalStatus.EXPIRED:
                raise ProposalExpiredError()
            raise ProposalNotPendingError(current_status=proposal.status)

        if current_time >= _as_utc(proposal.expires_at):
            # Transition overdue proposal to EXPIRED via CAS if still PENDING.
            await session.execute(
                update(MarriageProposal)
                .where(
                    MarriageProposal.id == proposal_id,
                    MarriageProposal.status == ProposalStatus.PENDING,
                )
                .values(
                    status=ProposalStatus.EXPIRED,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            await session.flush()
            await cls._load_proposal(session, proposal_id)
            raise ProposalExpiredError()

        # Atomic one-shot CAS transition: PENDING -> REJECTED
        claimed = await session.execute(
            update(MarriageProposal)
            .where(
                MarriageProposal.id == proposal_id,
                MarriageProposal.target_id == actor_user_id,
                MarriageProposal.status == ProposalStatus.PENDING,
            )
            .values(
                status=ProposalStatus.REJECTED,
                resolved_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if (claimed.rowcount or 0) != 1:
            refreshed = await cls._load_proposal(session, proposal_id)
            if refreshed is None:
                raise ProposalNotFoundError()
            if refreshed.status == ProposalStatus.EXPIRED:
                raise ProposalExpiredError()
            raise ProposalNotPendingError(current_status=refreshed.status)

        await session.flush()
        updated = await cls._load_proposal(session, proposal_id)
        log_event(
            Events.MARRIAGE_PROPOSAL_EVENT,
            {
                "action": "rejected",
                "proposal_id": proposal_id,
                "proposer_id": updated.proposer_id,
                "target_id": updated.target_id,
                "group_id": updated.group_id,
            },
        )
        return updated

    # ------------------------------------------------------------------
    # 3. Proposal Expiration
    # ------------------------------------------------------------------
    @classmethod
    async def expire_proposal(
        cls,
        session: AsyncSession,
        proposal_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> MarriageProposal:
        """Transition a single overdue ``PENDING`` proposal to ``EXPIRED``.

        Safe against concurrent acceptance/rejection via conditional ``UPDATE``.
        Raises:
        - ``ProposalNotFoundError`` if the proposal does not exist.
        - ``ProposalNotPendingError`` if the proposal is already terminal.
        - ``ProposalNotExpiredYetError`` if ``now < expires_at``.
        """
        proposal_id = int(proposal_id)
        current_time = _utc_now(now)

        proposal = await cls._lock_proposal_for_transition(session, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError()

        if proposal.status != ProposalStatus.PENDING:
            raise ProposalNotPendingError(current_status=proposal.status)

        if current_time < _as_utc(proposal.expires_at):
            raise ProposalNotExpiredYetError()

        claimed = await session.execute(
            update(MarriageProposal)
            .where(
                MarriageProposal.id == proposal_id,
                MarriageProposal.status == ProposalStatus.PENDING,
            )
            .values(
                status=ProposalStatus.EXPIRED,
                resolved_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if (claimed.rowcount or 0) != 1:
            refreshed = await cls._load_proposal(session, proposal_id)
            if refreshed is None:
                raise ProposalNotFoundError()
            raise ProposalNotPendingError(current_status=refreshed.status)

        await session.flush()
        updated = await cls._load_proposal(session, proposal_id)
        log_event(
            Events.MARRIAGE_PROPOSAL_EVENT,
            {
                "action": "expired",
                "proposal_id": proposal_id,
                "proposer_id": updated.proposer_id,
                "target_id": updated.target_id,
                "group_id": updated.group_id,
            },
        )
        return updated

    @classmethod
    async def expire_overdue_proposals(
        cls,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> List[MarriageProposal]:
        """Sweep all overdue ``PENDING`` proposals (``now >= expires_at``) to ``EXPIRED``.

        Each candidate is claimed with a conditional ``UPDATE ... WHERE status = 'PENDING'``
        so a concurrent resolution on any individual proposal is never overwritten.
        Returns the list of proposals transitioned to ``EXPIRED`` in this call.
        """
        current_time = _utc_now(now)
        candidates = list(
            (
                await session.scalars(
                    select(MarriageProposal)
                    .where(MarriageProposal.status == ProposalStatus.PENDING)
                    .order_by(MarriageProposal.id.asc())
                )
            ).all()
        )
        overdue_ids = [
            int(p.id)
            for p in candidates
            if _as_utc(p.expires_at) <= current_time
        ]
        if not overdue_ids:
            return []

        expired_ids: List[int] = []
        for pid in overdue_ids:
            claimed = await session.execute(
                update(MarriageProposal)
                .where(
                    MarriageProposal.id == pid,
                    MarriageProposal.status == ProposalStatus.PENDING,
                )
                .values(
                    status=ProposalStatus.EXPIRED,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if (claimed.rowcount or 0) == 1:
                expired_ids.append(pid)

        if not expired_ids:
            return []

        await session.flush()
        log_event(
            Events.MARRIAGE_PROPOSAL_EVENT,
            {"action": "expired_sweep", "count": len(expired_ids), "proposal_ids": expired_ids},
        )
        return await cls._load_proposals_by_ids(session, expired_ids)

    # ------------------------------------------------------------------
    # 4. Invalidation
    # ------------------------------------------------------------------
    @classmethod
    async def invalidate_proposal(
        cls,
        session: AsyncSession,
        proposal_id: int,
        reason: str = ProposalInvalidReason.USER_INELIGIBLE,
        *,
        now: Optional[datetime] = None,
    ) -> MarriageProposal:
        """Transition a ``PENDING`` proposal to ``INVALIDATED`` with ``reason``.

        Preserves the proposal row as historical data and does not consume
        the proposer's Wedding Ring.
        Raises ``ProposalNotFoundError`` if missing, or
        ``ProposalNotPendingError`` if already in a terminal status.
        """
        proposal_id = int(proposal_id)
        clean_reason = cls._normalize_reason(reason)
        current_time = _utc_now(now)

        proposal = await cls._lock_proposal_for_transition(session, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError()

        if proposal.status != ProposalStatus.PENDING:
            raise ProposalNotPendingError(current_status=proposal.status)

        claimed = await session.execute(
            update(MarriageProposal)
            .where(
                MarriageProposal.id == proposal_id,
                MarriageProposal.status == ProposalStatus.PENDING,
            )
            .values(
                status=ProposalStatus.INVALIDATED,
                invalid_reason=clean_reason,
                resolved_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if (claimed.rowcount or 0) != 1:
            refreshed = await cls._load_proposal(session, proposal_id)
            if refreshed is None:
                raise ProposalNotFoundError()
            raise ProposalNotPendingError(current_status=refreshed.status)

        await session.flush()
        updated = await cls._load_proposal(session, proposal_id)
        log_event(
            Events.MARRIAGE_PROPOSAL_EVENT,
            {
                "action": "invalidated",
                "proposal_id": proposal_id,
                "proposer_id": updated.proposer_id,
                "target_id": updated.target_id,
                "reason": clean_reason,
            },
        )
        return updated

    @classmethod
    async def invalidate_pending_for_target(
        cls,
        session: AsyncSession,
        target_id: int,
        *,
        reason: str = ProposalInvalidReason.TARGET_MARRIED,
        exclude_proposal_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> List[MarriageProposal]:
        """Atomically invalidate all ``PENDING`` proposals targeting ``target_id``
        (except ``exclude_proposal_id``, if provided).

        Returns the list of proposals that were transitioned from ``PENDING``
        to ``INVALIDATED`` by this call.
        """
        target_id = int(target_id)
        clean_reason = cls._normalize_reason(reason)
        current_time = _utc_now(now)

        stmt = select(MarriageProposal.id).where(
            MarriageProposal.target_id == target_id,
            MarriageProposal.status == ProposalStatus.PENDING,
        )
        if exclude_proposal_id is not None:
            stmt = stmt.where(MarriageProposal.id != int(exclude_proposal_id))
        stmt = stmt.order_by(MarriageProposal.id.asc())

        candidate_ids = list((await session.scalars(stmt)).all())
        if not candidate_ids:
            return []

        invalidated_ids: List[int] = []
        for pid in candidate_ids:
            claimed = await session.execute(
                update(MarriageProposal)
                .where(
                    MarriageProposal.id == pid,
                    MarriageProposal.status == ProposalStatus.PENDING,
                )
                .values(
                    status=ProposalStatus.INVALIDATED,
                    invalid_reason=clean_reason,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if (claimed.rowcount or 0) == 1:
                invalidated_ids.append(int(pid))

        if not invalidated_ids:
            return []

        await session.flush()
        log_event(
            Events.MARRIAGE_PROPOSAL_EVENT,
            {
                "action": "invalidated_for_target",
                "target_id": target_id,
                "exclude_proposal_id": exclude_proposal_id,
                "reason": clean_reason,
                "proposal_ids": invalidated_ids,
            },
        )
        return await cls._load_proposals_by_ids(session, invalidated_ids)

    @classmethod
    async def invalidate_pending_for_users(
        cls,
        session: AsyncSession,
        user_ids: Iterable[int],
        *,
        reason: str = ProposalInvalidReason.OTHER_PROPOSAL_ACCEPTED,
        exclude_proposal_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> List[MarriageProposal]:
        """Atomically invalidate all ``PENDING`` proposals where any user in
        ``user_ids`` is either ``proposer_id`` or ``target_id`` (excluding
        ``exclude_proposal_id`` if provided).

        Used when two users marry so all other incoming/outgoing pending
        proposals involving either spouse are safely closed as ``INVALIDATED``.
        """
        normalized_ids = sorted({int(uid) for uid in user_ids})
        if not normalized_ids:
            return []

        clean_reason = cls._normalize_reason(reason)
        current_time = _utc_now(now)

        stmt = select(MarriageProposal.id).where(
            MarriageProposal.status == ProposalStatus.PENDING,
            or_(
                MarriageProposal.proposer_id.in_(normalized_ids),
                MarriageProposal.target_id.in_(normalized_ids),
            ),
        )
        if exclude_proposal_id is not None:
            stmt = stmt.where(MarriageProposal.id != int(exclude_proposal_id))
        stmt = stmt.order_by(MarriageProposal.id.asc())

        candidate_ids = list((await session.scalars(stmt)).all())
        if not candidate_ids:
            return []

        invalidated_ids: List[int] = []
        for pid in candidate_ids:
            claimed = await session.execute(
                update(MarriageProposal)
                .where(
                    MarriageProposal.id == pid,
                    MarriageProposal.status == ProposalStatus.PENDING,
                )
                .values(
                    status=ProposalStatus.INVALIDATED,
                    invalid_reason=clean_reason,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if (claimed.rowcount or 0) == 1:
                invalidated_ids.append(int(pid))

        if not invalidated_ids:
            return []

        await session.flush()
        log_event(
            Events.MARRIAGE_PROPOSAL_EVENT,
            {
                "action": "invalidated_for_users",
                "user_ids": normalized_ids,
                "exclude_proposal_id": exclude_proposal_id,
                "reason": clean_reason,
                "proposal_ids": invalidated_ids,
            },
        )
        return await cls._load_proposals_by_ids(session, invalidated_ids)

    # ------------------------------------------------------------------
    # 5. Queries
    # ------------------------------------------------------------------
    @classmethod
    async def get_proposal(
        cls,
        session: AsyncSession,
        proposal_id: int,
        *,
        for_update: bool = False,
    ) -> Optional[MarriageProposal]:
        """Fetch a proposal by ID (optionally row-locked)."""
        stmt = (
            select(MarriageProposal)
            .where(MarriageProposal.id == int(proposal_id))
            .options(
                selectinload(MarriageProposal.proposer),
                selectinload(MarriageProposal.target),
                selectinload(MarriageProposal.group),
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    @classmethod
    async def get_pending_proposal(
        cls,
        session: AsyncSession,
        proposal_id: int,
        *,
        now: Optional[datetime] = None,
        expire_if_overdue: bool = True,
    ) -> Optional[MarriageProposal]:
        """Return the proposal if and only if it is currently ``PENDING`` and
        not expired.

        If ``expire_if_overdue`` is True and the proposal is ``PENDING`` with
        ``now >= expires_at``, it is transitioned to ``EXPIRED`` and ``None``
        is returned.
        """
        proposal = await cls.get_proposal(session, proposal_id)
        if proposal is None or proposal.status != ProposalStatus.PENDING:
            return None

        current_time = _utc_now(now)
        if current_time >= _as_utc(proposal.expires_at):
            if expire_if_overdue:
                try:
                    await cls.expire_proposal(session, proposal.id, now=current_time)
                except ProposalNotPendingError:
                    pass
            return None
        return proposal

    @classmethod
    async def get_active_outgoing_proposal(
        cls,
        session: AsyncSession,
        proposer_id: int,
        *,
        now: Optional[datetime] = None,
        expire_if_overdue: bool = True,
    ) -> Optional[MarriageProposal]:
        """Return the proposer's currently active ``PENDING`` outgoing proposal, if any."""
        proposer_id = int(proposer_id)
        current_time = _utc_now(now)

        if expire_if_overdue:
            await cls._expire_overdue_for_proposer(session, proposer_id, current_time)

        rows = list(
            (
                await session.scalars(
                    select(MarriageProposal)
                    .where(
                        MarriageProposal.proposer_id == proposer_id,
                        MarriageProposal.status == ProposalStatus.PENDING,
                    )
                    .options(
                        selectinload(MarriageProposal.proposer),
                        selectinload(MarriageProposal.target),
                        selectinload(MarriageProposal.group),
                    )
                    .order_by(MarriageProposal.id.desc())
                )
            ).all()
        )
        for p in rows:
            if _as_utc(p.expires_at) > current_time:
                return p
        return None

    @classmethod
    async def get_pending_proposals_for_target(
        cls,
        session: AsyncSession,
        target_id: int,
        *,
        group_id: Optional[int] = None,
        now: Optional[datetime] = None,
        expire_if_overdue: bool = True,
    ) -> List[MarriageProposal]:
        """Return all active ``PENDING`` proposals targeting ``target_id``."""
        target_id = int(target_id)
        current_time = _utc_now(now)

        if expire_if_overdue:
            await cls._expire_overdue_for_target(session, target_id, current_time)

        stmt = select(MarriageProposal).where(
            MarriageProposal.target_id == target_id,
            MarriageProposal.status == ProposalStatus.PENDING,
        )
        if group_id is not None:
            stmt = stmt.where(MarriageProposal.group_id == int(group_id))
        stmt = stmt.options(
            selectinload(MarriageProposal.proposer),
            selectinload(MarriageProposal.target),
            selectinload(MarriageProposal.group),
        ).order_by(MarriageProposal.id.asc())

        rows = list((await session.scalars(stmt)).all())
        return [p for p in rows if _as_utc(p.expires_at) > current_time]

    @classmethod
    async def set_proposal_message_id(
        cls,
        session: AsyncSession,
        proposal_id: int,
        message_id: int,
    ) -> MarriageProposal:
        """Attach or update the Telegram group ``message_id`` on a proposal."""
        proposal = await cls.get_proposal(session, proposal_id, for_update=True)
        if proposal is None:
            raise ProposalNotFoundError()
        proposal.message_id = int(message_id)
        await session.flush()
        return await cls._load_proposal(session, proposal.id)

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_reason(reason: Optional[str]) -> str:
        cleaned = (reason or "").strip()
        if not cleaned:
            cleaned = ProposalInvalidReason.USER_INELIGIBLE
        return cleaned[:64]

    @staticmethod
    async def _has_active_marriage(session: AsyncSession, user_id: int) -> bool:
        active_id = await session.scalar(
            select(Marriage.id)
            .where(
                Marriage.status == MarriageStatus.ACTIVE,
                or_(Marriage.user1_id == user_id, Marriage.user2_id == user_id),
            )
            .limit(1)
        )
        return active_id is not None

    @classmethod
    async def _expire_overdue_for_proposer(
        cls, session: AsyncSession, proposer_id: int, current_time: datetime
    ) -> int:
        rows = list(
            (
                await session.scalars(
                    select(MarriageProposal).where(
                        MarriageProposal.proposer_id == proposer_id,
                        MarriageProposal.status == ProposalStatus.PENDING,
                    )
                )
            ).all()
        )
        overdue_ids = [
            int(p.id) for p in rows if _as_utc(p.expires_at) <= current_time
        ]
        if not overdue_ids:
            return 0

        expired = 0
        for pid in overdue_ids:
            res = await session.execute(
                update(MarriageProposal)
                .where(
                    MarriageProposal.id == pid,
                    MarriageProposal.status == ProposalStatus.PENDING,
                )
                .values(
                    status=ProposalStatus.EXPIRED,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            expired += int(res.rowcount or 0)
        if expired:
            await session.flush()
            await cls._load_proposals_by_ids(session, overdue_ids)
        return expired

    @classmethod
    async def _expire_overdue_for_target(
        cls, session: AsyncSession, target_id: int, current_time: datetime
    ) -> int:
        rows = list(
            (
                await session.scalars(
                    select(MarriageProposal).where(
                        MarriageProposal.target_id == target_id,
                        MarriageProposal.status == ProposalStatus.PENDING,
                    )
                )
            ).all()
        )
        overdue_ids = [
            int(p.id) for p in rows if _as_utc(p.expires_at) <= current_time
        ]
        if not overdue_ids:
            return 0

        expired = 0
        for pid in overdue_ids:
            res = await session.execute(
                update(MarriageProposal)
                .where(
                    MarriageProposal.id == pid,
                    MarriageProposal.status == ProposalStatus.PENDING,
                )
                .values(
                    status=ProposalStatus.EXPIRED,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            expired += int(res.rowcount or 0)
        if expired:
            await session.flush()
            await cls._load_proposals_by_ids(session, overdue_ids)
        return expired

    @classmethod
    async def _lock_proposal_for_transition(
        cls,
        session: AsyncSession,
        proposal_id: int,
    ) -> Optional[MarriageProposal]:
        """Acquire a write-intent lock and FOR UPDATE row lock on a proposal.

        Prevents SQLite SHARED->RESERVED upgrade deadlocks when two transactions
        race to transition the same proposal concurrently.
        """
        for attempt in range(cls._LOCK_RETRIES):
            try:
                await session.execute(
                    update(MarriageProposal)
                    .where(MarriageProposal.id == int(proposal_id))
                    .values(updated_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                stmt = (
                    select(MarriageProposal)
                    .where(MarriageProposal.id == int(proposal_id))
                    .execution_options(populate_existing=True)
                    .options(
                        selectinload(MarriageProposal.proposer),
                        selectinload(MarriageProposal.target),
                        selectinload(MarriageProposal.group),
                    )
                    .with_for_update()
                )
                return await session.scalar(stmt)
            except OperationalError:
                if attempt + 1 < cls._LOCK_RETRIES:
                    await session.rollback()
                    await asyncio.sleep(cls._LOCK_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        return None

    @classmethod
    async def _assert_proposer_not_in_cooldown(
        cls, session: AsyncSession, proposer_id: int, current_time: datetime
    ) -> None:
        """Refuse proposal creation while the proposer is inside the
        post-divorce remarriage cooldown (``marriage_divorce_cooldown_seconds``).

        Runs *before* any ``MarriageProposal`` row is locked and long before the
        row is inserted, so a rejected proposer costs nothing: no ring is
        consumed and no proposal row is created.

        Lock-order note: the cooldown query is a plain non-locking ``SELECT``
        (``MarriageService.get_active_cooldown_for_user`` takes no row locks),
        so this adds **no** acquisition to the path - ``create_proposal`` keeps
        its ``User -> MarriageProposal`` order and never takes a ``Marriage``
        lock. The read is still race-safe because the caller already holds the
        proposer's ``User`` row lock, which is the same lock a divorce takes
        first, so a divorce and a proposal creation for the same user
        serialize.

        ``MarriageService`` owns both the query and the error but imports THIS
        module, so it is imported lazily to keep the module-level dependency
        graph acyclic - the same pattern ``MarriageService.divorce_marriage``
        uses for ``LoveService``.
        """
        from sib.services.marriage import (  # noqa: PLC0415 - late import: marriage.py imports this module
            MarriageCooldownActiveError,
            MarriageService,
        )

        cooldown_until = await MarriageService.get_active_cooldown_for_user(
            session, int(proposer_id), now=current_time
        )
        if cooldown_until is not None:
            raise MarriageCooldownActiveError(
                user_id=int(proposer_id),
                cooldown_until=cooldown_until,
                current_time=current_time,
                role="proposer",
            )

    @classmethod
    async def _lock_pending_outgoing_for_proposer(
        cls, session: AsyncSession, proposer_id: int
    ) -> List[MarriageProposal]:
        """Row-lock every ``PENDING`` outgoing proposal of ``proposer_id``.

        Named counterpart of ``_lock_participants_for_proposal`` /
        ``_lock_proposal_for_transition``: ``create_proposal`` takes these
        proposal row locks *after* the participant ``User`` locks, which is
        what pins the global ``User -> MarriageProposal`` order. Behaviour is
        unchanged; only the query moved here.
        """
        rows = await session.scalars(
            select(MarriageProposal)
            .where(
                MarriageProposal.proposer_id == int(proposer_id),
                MarriageProposal.status == ProposalStatus.PENDING,
            )
            .order_by(MarriageProposal.id.desc())
            .with_for_update()
        )
        return list(rows.all())

    @classmethod
    async def _lock_participants_for_proposal(
        cls,
        session: AsyncSession,
        proposer_id: int,
        target_id: int,
    ) -> Dict[int, User]:
        """Lock both user rows in ascending ID order and acquire a write lock on
        ``proposer_id`` so concurrent proposal creations by the same proposer
        serialize on both MariaDB/MySQL and SQLite.
        """
        ordered_ids = sorted({proposer_id, target_id})
        for attempt in range(cls._LOCK_RETRIES):
            try:
                # 1. Write-intent touch on proposer_id (acquires RESERVED/IMMEDIATE
                #    write lock on SQLite and row X-lock on MariaDB/MySQL).
                await session.execute(
                    update(User)
                    .where(User.id == proposer_id)
                    .values(updated_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                # 2. Deterministic ascending-order FOR UPDATE lock on both users.
                rows = await session.scalars(
                    select(User)
                    .where(User.id.in_(ordered_ids))
                    .order_by(User.id.asc())
                    .with_for_update()
                )
                return {u.id: u for u in rows.all()}
            except OperationalError:
                if attempt + 1 < cls._LOCK_RETRIES:
                    if not session.new and not session.dirty and not session.deleted:
                        await session.rollback()
                    await asyncio.sleep(cls._LOCK_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        return {}

    @staticmethod
    async def _load_proposal(
        session: AsyncSession, proposal_id: int
    ) -> Optional[MarriageProposal]:
        stmt = (
            select(MarriageProposal)
            .where(MarriageProposal.id == int(proposal_id))
            .execution_options(populate_existing=True)
            .options(
                selectinload(MarriageProposal.proposer),
                selectinload(MarriageProposal.target),
                selectinload(MarriageProposal.group),
            )
        )
        return await session.scalar(stmt)

    @staticmethod
    async def _load_proposals_by_ids(
        session: AsyncSession, proposal_ids: Sequence[int]
    ) -> List[MarriageProposal]:
        if not proposal_ids:
            return []
        stmt = (
            select(MarriageProposal)
            .where(MarriageProposal.id.in_([int(pid) for pid in proposal_ids]))
            .execution_options(populate_existing=True)
            .options(
                selectinload(MarriageProposal.proposer),
                selectinload(MarriageProposal.target),
                selectinload(MarriageProposal.group),
            )
            .order_by(MarriageProposal.id.asc())
        )
        return list((await session.scalars(stmt)).all())
