"""Leaderboard and Statistics Service (win-based, no Elo)."""

from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import Group, GroupStats, PlayerStats, User


class LeaderboardService:
    """Global group leaderboard ranked by wins.

    Deterministic ordering (no hidden skill score):
    1. Wins descending
    2. Win rate descending
    3. Matches played descending
    4. Stable group id tie-break (ascending)
    """

    @staticmethod
    def _sort_key(row: Tuple[Group, GroupStats]):
        group, stats = row
        win_rate = (stats.wins / stats.matches_played) if stats.matches_played > 0 else 0.0
        return (-stats.wins, -win_rate, -stats.matches_played, group.id)

    @classmethod
    async def _ranked_rows(
        cls,
        session: AsyncSession,
    ) -> List[Tuple[Group, GroupStats]]:
        stmt = (
            select(Group, GroupStats)
            .join(GroupStats, Group.id == GroupStats.group_id)
            .where(Group.is_active == True)
        )
        results = (await session.execute(stmt)).all()
        return sorted(results, key=cls._sort_key)

    @classmethod
    async def get_top_groups(
        cls,
        session: AsyncSession,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Tuple[int, Group, GroupStats]]:
        """Get global leaderboard sorted by wins with deterministic ties."""
        ranked = await cls._ranked_rows(session)
        return [
            (index, group, stats)
            for index, (group, stats) in enumerate(
                ranked[offset : offset + limit], start=offset + 1
            )
        ]

    @classmethod
    async def get_group_stats_and_rank(
        cls,
        session: AsyncSession,
        group_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Fetch group statistics along with its global rank."""
        ranked = await cls._ranked_rows(session)
        for rank, (group, stats) in enumerate(ranked, start=1):
            if group.id == group_id:
                return {
                    "group_id": group.id,
                    "title": group.title,
                    "rank": rank,
                    "matches_played": stats.matches_played,
                    "wins": stats.wins,
                    "losses": stats.losses,
                    "draws": stats.draws,
                    "win_rate": round(stats.win_rate, 1),
                }

        # Group has no stats row yet: fetch the group itself for the title.
        group = await session.get(Group, group_id)
        if not group:
            return None
        return {
            "group_id": group.id,
            "title": group.title,
            "rank": len(ranked) + 1,
            "matches_played": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "win_rate": 0.0,
        }

    @classmethod
    async def get_player_stats(
        cls,
        session: AsyncSession,
        user_id: int,
    ) -> Dict[str, Any]:
        """Fetch participation statistics for an individual player."""
        stmt = select(PlayerStats).where(PlayerStats.user_id == user_id)
        stats = await session.scalar(stmt)

        matches = stats.matches_played if stats else 0
        wins = stats.wins if stats else 0
        losses = stats.losses if stats else 0
        draws = stats.draws if stats else 0
        win_rate = (wins / matches * 100.0) if matches > 0 else 0.0

        return {
            "user_id": user_id,
            "matches_played": matches,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_rate": round(win_rate, 1),
        }
