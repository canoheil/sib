"""Leaderboard and Statistics Service."""

from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from goje.core.config import settings
from goje.database.models import Group, Rating, PlayerStats, User


class LeaderboardService:
    """Manages global group leaderboard, group statistics, and player records."""

    @classmethod
    async def get_top_groups(
        cls,
        session: AsyncSession,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Tuple[int, Group, Rating]]:
        """
        Get global leaderboard sorted by Elo rating DESC, then wins DESC.
        Returns list of (rank, Group, Rating).
        """
        stmt = (
            select(Group, Rating)
            .join(Rating, Group.id == Rating.group_id)
            .where(Group.is_active == True)
            .order_by(desc(Rating.rating), desc(Rating.wins), desc(Rating.matches_played))
            .limit(limit)
            .offset(offset)
        )
        results = await session.execute(stmt)
        
        ranked_list = []
        for index, (group, rating) in enumerate(results.all(), start=offset + 1):
            ranked_list.append((index, group, rating))
            
        return ranked_list

    @classmethod
    async def get_group_stats_and_rank(
        cls,
        session: AsyncSession,
        group_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Fetch group statistics along with its global competitive rank."""
        # Fetch group & rating
        stmt = (
            select(Group, Rating)
            .join(Rating, Group.id == Rating.group_id)
            .where(Group.id == group_id)
        )
        res = (await session.execute(stmt)).first()
        if not res:
            return None

        group, rating = res

        # Calculate rank: count groups with strictly higher rating
        higher_count_stmt = (
            select(func.count(Rating.id))
            .where(Rating.rating > rating.rating)
        )
        rank = (await session.scalar(higher_count_stmt)) + 1

        win_rate = (rating.wins / rating.matches_played * 100.0) if rating.matches_played > 0 else 0.0

        return {
            "group_id": group.id,
            "title": group.title,
            "rating": rating.rating,
            "rank": rank,
            "matches_played": rating.matches_played,
            "wins": rating.wins,
            "losses": rating.losses,
            "draws": rating.draws,
            "win_rate": round(win_rate, 1),
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
