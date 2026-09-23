"""Dedicated rating and match result service with idempotent Elo updates."""

from datetime import datetime, timezone
import math
from typing import Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from goje.core.config import settings
from goje.core.logging import logger, log_event, Events
from goje.database.models import (
    Match,
    MatchPlayer,
    MatchState,
    Rating,
    PlayerStats,
    Party,
    PartyStatus,
)
from goje.games.base import GameResult


class RatingService:
    """Calculates Elo updates and guarantees idempotency."""

    @classmethod
    def calculate_elo_change(
        cls,
        rating_a: float,
        rating_b: float,
        score_a: float,  # 1.0 (win), 0.5 (draw), 0.0 (loss)
        k_factor: float = settings.ELO_K_FACTOR,
    ) -> Tuple[float, float]:
        """
        Calculate Elo delta for team A and team B.
        Returns (delta_a, delta_b).
        """
        # Expected scores
        ea = 1.0 / (1.0 + math.pow(10.0, (rating_b - rating_a) / 400.0))
        eb = 1.0 / (1.0 + math.pow(10.0, (rating_a - rating_b) / 400.0))

        score_b = 1.0 - score_a

        delta_a = round(k_factor * (score_a - ea), 1)
        delta_b = round(k_factor * (score_b - eb), 1)

        return delta_a, delta_b

    @classmethod
    async def apply_match_result(
        cls,
        session: AsyncSession,
        match: Match,
        result: GameResult,
    ) -> Match:
        """
        Atomically apply match outcome, calculate Elo, update group ratings,
        record player statistics, and mark match finished.
        GUARANTEED IDEMPOTENT: Will NEVER update ratings twice.
        """
        # 1. Idempotency guard: If rating already applied, exit immediately
        if match.rating_applied:
            logger.warning(f"Rating already applied for match_id={match.id}. Skipping duplicate update.")
            return match

        # 2. Lock group ratings
        g1_rating_stmt = select(Rating).where(Rating.group_id == match.group1_id).with_for_update()
        r1 = await session.scalar(g1_rating_stmt)
        if not r1:
            r1 = Rating(group_id=match.group1_id, rating=settings.INITIAL_RATING)
            session.add(r1)
            await session.flush()

        g2_rating_stmt = select(Rating).where(Rating.group_id == match.group2_id).with_for_update()
        r2 = await session.scalar(g2_rating_stmt)
        if not r2:
            r2 = Rating(group_id=match.group2_id, rating=settings.INITIAL_RATING)
            session.add(r2)
            await session.flush()

        # 3. Determine scores
        if result.is_draw:
            score1, score2 = 0.5, 0.5
            match.is_draw = True
            match.winner_group_id = None
        elif result.winner_team in (1, "1", "A"):
            score1, score2 = 1.0, 0.0
            match.is_draw = False
            match.winner_group_id = match.group1_id
        elif result.winner_team in (2, "2", "B"):
            score1, score2 = 0.0, 1.0
            match.is_draw = False
            match.winner_group_id = match.group2_id
        else:
            raise ValueError(f"Invalid game result: winner_team={result.winner_team}, is_draw={result.is_draw}")

        # 4. Calculate Elo deltas
        delta1, delta2 = cls.calculate_elo_change(r1.rating, r2.rating, score1, settings.ELO_K_FACTOR)

        # 5. Update Group 1 Rating
        r1.rating = max(100.0, r1.rating + delta1)
        r1.matches_played += 1
        if score1 == 1.0:
            r1.wins += 1
        elif score1 == 0.0:
            r1.losses += 1
        else:
            r1.draws += 1

        # 6. Update Group 2 Rating
        r2.rating = max(100.0, r2.rating + delta2)
        r2.matches_played += 1
        if score2 == 1.0:
            r2.wins += 1
        elif score2 == 0.0:
            r2.losses += 1
        else:
            r2.draws += 1

        # 7. Update Individual Player Statistics
        for p in match.players:
            p_stat = await session.scalar(
                select(PlayerStats).where(PlayerStats.user_id == p.user_id).with_for_update()
            )
            if not p_stat:
                p_stat = PlayerStats(user_id=p.user_id, matches_played=0, wins=0, losses=0, draws=0)
                session.add(p_stat)
                await session.flush()

            p_stat.matches_played += 1
            if result.is_draw:
                p.result = "DRAW"
                p_stat.draws += 1
            elif p.team == result.winner_team:
                p.result = "WIN"
                p_stat.wins += 1
            else:
                p.result = "LOSS"
                p_stat.losses += 1

        # 8. Update Match State & Idempotency Flag
        match.rating_applied = True
        match.group1_rating_change = delta1
        match.group2_rating_change = delta2
        match.state = MatchState.FINISHED
        match.finished_at = datetime.now(timezone.utc)

        # 9. Free parties from IN_MATCH
        for party_id in (match.party1_id, match.party2_id):
            if party_id:
                party = await session.scalar(
                    select(Party).where(Party.id == party_id).with_for_update()
                )
                if party and party.status == PartyStatus.IN_MATCH:
                    party.status = PartyStatus.CANCELLED  # Party served its purpose

        await session.flush()

        log_event(
            Events.RATING_UPDATED,
            {
                "match_id": match.id,
                "group1_id": match.group1_id,
                "g1_delta": delta1,
                "g1_new_rating": r1.rating,
                "group2_id": match.group2_id,
                "g2_delta": delta2,
                "g2_new_rating": r2.rating,
            },
        )
        log_event(
            Events.MATCH_FINISHED,
            {
                "match_id": match.id,
                "winner_group_id": match.winner_group_id,
                "is_draw": match.is_draw,
            },
        )

        return match
