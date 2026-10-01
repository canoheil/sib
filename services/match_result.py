"""Dedicated match result service.

Applies match outcomes idempotently WITHOUT any Elo/MMR calculation.
Group progression is win-based:
- A normal eligible group win awards exactly one group win.
- A loss does NOT subtract a win.
- Bot matches never affect group wins or group leaderboard position.
- FRIENDLY matches never affect rank/ELO/MMR, group wins/leaderboards or
  competitive player counters, and never award seedlings: they pay the
  configurable Apple reward instead (see ``sib.services.friendly``).
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger, log_event, Events
from sib.database.models import (
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
    GroupStats,
    PlayerStats,
    User,
    Party,
    PartyStatus,
)
from sib.games.base import GameResult
from sib.services.seedling import SeedlingService


class MatchResultService:
    """Applies match outcomes exactly once (idempotent)."""

    @classmethod
    async def _get_group_stats(
        cls,
        session: AsyncSession,
        group_id: Optional[int],
    ) -> Optional[GroupStats]:
        if not group_id:
            return None
        stats = await session.scalar(
            select(GroupStats)
            .where(GroupStats.group_id == group_id)
            .with_for_update()
        )
        if not stats:
            stats = GroupStats(group_id=group_id)
            session.add(stats)
            await session.flush()
        return stats

    @classmethod
    async def _record_player_results(
        cls,
        session: AsyncSession,
        match: Match,
        result: GameResult,
    ) -> None:
        # Friendly matches are casual: participants keep their per-match
        # result (the board shows it), but NO competitive profile counters
        # and NO seedlings - their reward is the Apple payout below.
        friendly = match.match_type == MatchType.FRIENDLY

        bot_ids = set()
        if match.is_bot_match or friendly:
            bot_ids = set(
                (
                    await session.scalars(
                        select(User.id).where(
                            User.id.in_([p.user_id for p in match.players]),
                            User.is_bot == True,
                        )
                    )
                ).all()
            )

        for p in match.players:
            is_bot_player = p.user_id in bot_ids
            # Bot entities keep a match result, but only human participants
            # get an aggregate profile record.
            p_stat = None
            if not is_bot_player and not friendly:
                p_stat = await session.scalar(
                    select(PlayerStats)
                    .where(PlayerStats.user_id == p.user_id)
                    .with_for_update()
                )
                if not p_stat:
                    p_stat = PlayerStats(user_id=p.user_id)
                    session.add(p_stat)
                    await session.flush()

            if result.is_draw:
                p.result = "DRAW"
                if p_stat:
                    if match.is_bot_match:
                        p_stat.bot_matches_played += 1
                        p_stat.bot_draws += 1
                    else:
                        p_stat.matches_played += 1
                        p_stat.draws += 1
            elif p.team == result.winner_team:
                p.result = "WIN"
                if p_stat:
                    if match.is_bot_match:
                        p_stat.bot_matches_played += 1
                        p_stat.bot_wins += 1
                    else:
                        p_stat.matches_played += 1
                        p_stat.wins += 1
                # Seedling rewards belong to eligible NORMAL PvP wins only.
                if not match.is_bot_match and not friendly:
                    await SeedlingService.award_match_win(
                        session, match_id=match.id, user_id=p.user_id
                    )
            else:
                p.result = "LOSS"
                if p_stat:
                    if match.is_bot_match:
                        p_stat.bot_matches_played += 1
                        p_stat.bot_losses += 1
                    else:
                        p_stat.matches_played += 1
                        p_stat.losses += 1

    @classmethod
    async def _free_parties(cls, session: AsyncSession, match: Match) -> None:
        for party_id in (match.party1_id, match.party2_id):
            if party_id:
                party = await session.scalar(
                    select(Party)
                    .where(Party.id == party_id)
                    .with_for_update()
                )
                if party and party.status == PartyStatus.IN_MATCH:
                    party.status = PartyStatus.CANCELLED

    @classmethod
    async def apply_match_result(
        cls,
        session: AsyncSession,
        match: Match,
        result: GameResult,
    ) -> Match:
        """Atomically apply a match outcome. GUARANTEED IDEMPOTENT."""
        # 1. Idempotency guard: never apply the same result twice.
        if match.result_applied:
            logger.warning(
                f"Result already applied for match_id={match.id}. Skipping duplicate update."
            )
            return match

        # 2. Resolve winner/draw bookkeeping shared by both branches.
        if result.is_draw:
            match.is_draw = True
            match.winner_group_id = None
            match.winner_team = None
        elif result.winner_team in (1, "1", "A"):
            match.is_draw = False
            match.winner_group_id = match.group1_id
            match.winner_team = 1
        elif result.winner_team in (2, "2", "B"):
            match.is_draw = False
            match.winner_group_id = match.group2_id
            match.winner_team = 2
        else:
            raise ValueError(
                f"Invalid game result: winner_team={result.winner_team}, "
                f"is_draw={result.is_draw}"
            )

        # 3. Individual player statistics always recorded.
        await cls._record_player_results(session, match, result)

        if match.match_type == MatchType.FRIENDLY:
            # 4. FRIENDLY match: never rank/ELO/MMR, never group wins or
            #    seedlings. The Apple reward (daily-capped, ledger-idempotent)
            #    is the only payout.
            from sib.services.friendly import FriendlyRewardService

            await FriendlyRewardService.award_match_rewards(session, match)
            log_event(
                Events.MATCH_FINISHED,
                {
                    "match_id": match.id,
                    "match_type": MatchType.FRIENDLY,
                    "winner_team": match.winner_team,
                    "is_draw": match.is_draw,
                },
            )
        elif match.is_bot_match:
            # 4a. Bot matches MUST NOT affect group wins/leaderboard.
            log_event(
                Events.MATCH_FINISHED,
                {
                    "match_id": match.id,
                    "winner_group_id": match.winner_group_id,
                    "is_draw": match.is_draw,
                    "is_bot_match": True,
                },
            )
        else:
            # 4b. Normal group match: win-based group progression.
            g1_stats = await cls._get_group_stats(session, match.group1_id)
            g2_stats = await cls._get_group_stats(session, match.group2_id)

            for stats, score in (
                (g1_stats, (not result.is_draw and result.winner_team in (1, "1", "A"))),
                (g2_stats, (not result.is_draw and result.winner_team in (2, "2", "B"))),
            ):
                if stats is None:
                    continue
                stats.matches_played += 1
                if result.is_draw:
                    stats.draws += 1
                elif score:
                    # A win ADDS one win; losses never subtract wins.
                    stats.wins += 1
                else:
                    stats.losses += 1

            log_event(
                Events.MATCH_FINISHED,
                {
                    "match_id": match.id,
                    "winner_group_id": match.winner_group_id,
                    "is_draw": match.is_draw,
                },
            )

        # 5. Finalize the match exactly once.
        match.result_applied = True
        match.state = MatchState.FINISHED
        match.finished_at = datetime.now(timezone.utc)
        match.current_turn_player_id = None
        match.turn_deadline = None

        await cls._free_parties(session, match)
        await session.flush()
        return match
