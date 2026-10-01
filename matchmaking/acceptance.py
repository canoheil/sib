"""Match Acceptance, Rejection, and State Machine management."""

from datetime import datetime, timezone
from typing import Optional, Tuple
from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.errors import (
    sibException,
    InvalidActionException,
    MatchNotFoundException,
)
from sib.core.logging import logger, log_event, Events
from sib.database.models import (
    Match,
    MatchPlayer,
    MatchState,
    Party,
    PartyStatus,
)


class AcceptanceService:
    """Manages match acceptance state transitions and validation."""

    VALID_TRANSITIONS = {
        MatchState.CREATED: {MatchState.WAITING_FOR_OPPONENT, MatchState.CANCELLED},
        MatchState.WAITING_FOR_OPPONENT: {MatchState.OPPONENT_FOUND, MatchState.CANCELLED},
        MatchState.OPPONENT_FOUND: {MatchState.WAITING_FOR_ACCEPTANCE, MatchState.CANCELLED},
        MatchState.WAITING_FOR_ACCEPTANCE: {MatchState.ACTIVE, MatchState.CANCELLED},
        MatchState.ACTIVE: {MatchState.FINISHED, MatchState.CANCELLED},
    }

    @classmethod
    def can_transition(cls, from_state: str, to_state: str) -> bool:
        """Verify whether a state transition is legal in the match state machine."""
        allowed = cls.VALID_TRANSITIONS.get(from_state, set())
        return to_state in allowed

    @classmethod
    async def accept_match(
        cls,
        session: AsyncSession,
        match_id: int,
        user_id: int,
        *,
        group_id: Optional[int] = None,
    ) -> Tuple[Match, bool]:
        """
        Record acceptance from a player for their team.
        Returns (match, both_accepted).
        """
        match_stmt = (
            select(Match)
            .where(Match.id == match_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.game),
                selectinload(Match.group1),
                selectinload(Match.group2),
            )
            .with_for_update()
        )
        match = await session.scalar(match_stmt)
        if not match:
            raise MatchNotFoundException()

        if match.state != MatchState.WAITING_FOR_ACCEPTANCE:
            raise InvalidActionException(f"این مسابقه در وضعیت پذیرش نیست (وضعیت: {match.state}) 🍏")

        # Find user's player record
        player = next((p for p in match.players if p.user_id == user_id), None)
        if not player:
            raise InvalidActionException("شما بازیکن این مسابقه نیستید 🍏")
        if group_id is not None and (
            player.group_id != group_id
            or group_id not in (match.group1_id, match.group2_id)
        ):
            raise InvalidActionException("این پذیرش فقط از گروه ثبت‌شده‌ی شما ممکنه 🍏")

        player.has_accepted = True

        # Exactly one acceptance per side is required
        if player.team == 1:
            match.group1_accepted = True
        elif player.team == 2:
            match.group2_accepted = True

        log_event(
            Events.MATCH_ACCEPTED,
            {
                "match_id": match.id,
                "user_id": user_id,
                "team": player.team,
                "group1_accepted": match.group1_accepted,
                "group2_accepted": match.group2_accepted,
            },
        )

        both_accepted = match.group1_accepted and match.group2_accepted

        if both_accepted:
            if not cls.can_transition(match.state, MatchState.ACTIVE):
                raise InvalidActionException("انتقال به وضعیت شروع بازی غیرمجاز است 🍏")

            match.state = MatchState.ACTIVE
            match.started_at = datetime.now(timezone.utc)

            log_event(
                Events.MATCH_STARTED,
                {"match_id": match.id, "started_at": match.started_at.isoformat()},
            )

        await session.flush()
        return match, both_accepted

    @classmethod
    async def reject_match(
        cls,
        session: AsyncSession,
        match_id: int,
        user_id: int,
        *,
        group_id: Optional[int] = None,
    ) -> Match:
        """
        Reject a match before gameplay starts.
        Cancels the match with NO rating change.
        Reverts parties back to FULL so players can retry or leave.
        """
        match_stmt = (
            select(Match)
            .where(Match.id == match_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.game),
                selectinload(Match.group1),
                selectinload(Match.group2),
            )
            .with_for_update()
        )
        match = await session.scalar(match_stmt)
        if not match:
            raise MatchNotFoundException()

        if match.state != MatchState.WAITING_FOR_ACCEPTANCE:
            raise InvalidActionException("امکان رد این مسابقه در وضعیت کنونی وجود ندارد 🍏")

        player = next((p for p in match.players if p.user_id == user_id), None)
        if not player:
            raise InvalidActionException("شما بازیکن این مسابقه نیستید 🍏")
        if group_id is not None and (
            player.group_id != group_id
            or group_id not in (match.group1_id, match.group2_id)
        ):
            raise InvalidActionException("این پذیرش فقط از گروه ثبت‌شده‌ی شما ممکنه 🍏")

        if not cls.can_transition(match.state, MatchState.CANCELLED):
            raise InvalidActionException("انتقال به وضعیت لغو مسابقه غیرمجاز است 🍏")

        match.state = MatchState.CANCELLED

        # Revert parties back to FULL
        for party_id in (match.party1_id, match.party2_id):
            if party_id:
                party = await session.scalar(
                    select(Party).where(Party.id == party_id).with_for_update()
                )
                if party and party.status == PartyStatus.IN_MATCH:
                    party.status = PartyStatus.FULL

        await session.flush()

        log_event(
            Events.MATCH_REJECTED,
            {"match_id": match.id, "rejected_by": user_id, "team": player.team},
        )
        log_event(
            Events.MATCH_CANCELLED,
            {"match_id": match.id, "reason": "rejected_by_player"},
        )

        return match

    @classmethod
    async def check_and_handle_acceptance_timeout(
        cls,
        session: AsyncSession,
        match_id: int,
    ) -> Optional[Match]:
        """Cancel match if acceptance deadline has expired."""
        now = datetime.now(timezone.utc)
        match_stmt = (
            select(Match)
            .where(Match.id == match_id)
            .with_for_update()
        )
        match = await session.scalar(match_stmt)
        if not match:
            return None

        if match.state == MatchState.WAITING_FOR_ACCEPTANCE:
            deadline = match.acceptance_deadline
            if deadline and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)

            if deadline and now > deadline:
                match.state = MatchState.CANCELLED
                for party_id in (match.party1_id, match.party2_id):
                    if party_id:
                        party = await session.scalar(
                            select(Party).where(Party.id == party_id).with_for_update()
                        )
                        if party and party.status == PartyStatus.IN_MATCH:
                            party.status = PartyStatus.FULL

                await session.flush()
                log_event(
                    Events.MATCH_CANCELLED,
                    {"match_id": match.id, "reason": "acceptance_timeout"},
                )
                return match

        return None
