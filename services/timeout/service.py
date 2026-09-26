"""Timeout service for background and reactive deadline processing."""

from datetime import datetime, timezone
from typing import List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.logging import logger, log_event, Events
from sib.database.models import Match, MatchState, MatchPlayer, Party, PartyStatus, Group
from sib.games.base import GameResult
from sib.services.match_result import MatchResultService


class TimeoutService:
    """Service handling match acceptance timeouts and active gameplay turn timeouts."""

    @classmethod
    async def process_acceptance_timeouts(cls, session: AsyncSession) -> List[int]:
        """Check and cancel matches that exceeded acceptance deadline (default 120s)."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(Match)
            .where(
                Match.state == MatchState.WAITING_FOR_ACCEPTANCE,
                Match.acceptance_deadline != None,
                Match.acceptance_deadline < now,
            )
            .with_for_update()
        )
        timed_out_matches = list((await session.scalars(stmt)).all())
        cancelled_ids = []

        for match in timed_out_matches:
            match.state = MatchState.CANCELLED
            cancelled_ids.append(match.id)

            # Reopen or restore parties
            if match.party1_id:
                p1 = await session.scalar(select(Party).where(Party.id == match.party1_id))
                if p1 and p1.status == PartyStatus.IN_MATCH:
                    p1.status = PartyStatus.FULL
            if match.party2_id:
                p2 = await session.scalar(select(Party).where(Party.id == match.party2_id))
                if p2 and p2.status == PartyStatus.IN_MATCH:
                    p2.status = PartyStatus.FULL

            log_event(
                Events.MATCH_CANCELLED,
                {"match_id": match.id, "reason": "acceptance_timeout"},
            )

        if cancelled_ids:
            await session.commit()
            logger.info(f"Cancelled {len(cancelled_ids)} matches due to acceptance timeout: {cancelled_ids}")

        return cancelled_ids

    @classmethod
    async def _resolve_expired_active_match(
        cls,
        session: AsyncSession,
        match: Match,
    ) -> None:
        """Resolve an expired active match for turn-based and simultaneous games."""
        import json

        if match.current_turn_player_id is not None:
            forfeited_pid = match.current_turn_player_id
            forfeited_player = next(
                (p for p in match.players if p.user_id == forfeited_pid),
                None,
            )
            winner_team = 2 if forfeited_player and forfeited_player.team == 1 else 1
            result = GameResult(
                winner_team=winner_team,
                is_draw=False,
                summary=(
                    "به دلیل پایان زمان نوبت بازیکن حریف، "
                    + ("تیم ۱" if winner_team == 1 else "تیم ۲")
                    + " برنده مسابقه اعلام شد! 🍏⏱️"
                ),
                details={
                    "turn_timeout": True,
                    "forfeited_player_id": forfeited_pid,
                },
            )
        else:
            try:
                state = json.loads(match.game_state or "{}")
            except (TypeError, ValueError):
                state = {}

            players = state.get("players", {})
            locked_teams = {
                data.get("team")
                for data in players.values()
                if data.get("locked") is True and data.get("team") in (1, 2)
            }

            if locked_teams == {1}:
                result = GameResult(
                    winner_team=1,
                    is_draw=False,
                    summary="به دلیل پایان زمان، تیم ۱ برنده مسابقه اعلام شد! 🍏⏱️",
                    details={"simultaneous_timeout": True, "locked_team": 1},
                )
            elif locked_teams == {2}:
                result = GameResult(
                    winner_team=2,
                    is_draw=False,
                    summary="به دلیل پایان زمان، تیم ۲ برنده مسابقه اعلام شد! 🍏⏱️",
                    details={"simultaneous_timeout": True, "locked_team": 2},
                )
            else:
                result = GameResult(
                    winner_team=None,
                    is_draw=True,
                    summary="زمان بازیکنان به پایان رسید؛ مسابقه مساوی شد! 🍏⏱️",
                    details={
                        "simultaneous_timeout": True,
                        "locked_teams": sorted(locked_teams),
                    },
                )

        await MatchResultService.apply_match_result(session, match, result)

    @classmethod
    async def process_turn_timeouts(cls, session: AsyncSession) -> List[int]:
        """
        Check active matches that exceeded player turn deadline.
        Awards victory to opponent team by forfeit due to turn timeout.
        """
        now = datetime.now(timezone.utc)
        stmt = (
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
        timed_out_matches = list((await session.scalars(stmt)).all())
        processed_ids = []

        for match in timed_out_matches:
            await cls._resolve_expired_active_match(session, match)
            processed_ids.append(match.id)


        if processed_ids:
            await session.commit()
            logger.info(f"Processed turn timeout forfeit for matches: {processed_ids}")

        return processed_ids
