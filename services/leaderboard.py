"""Leaderboard and Statistics Service (win-based, no Elo)."""

from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import Group, GroupStats, PlayerStats, User
from sib.database.models.castle import GroupCastle, CastleDonation
from sib.database.models.war import GroupWar
from sib.database.models.invasion_v2 import Inv2Invasion


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
            "bot_matches_played": stats.bot_matches_played if stats else 0,
            "bot_wins": stats.bot_wins if stats else 0,
            "bot_losses": stats.bot_losses if stats else 0,
            "bot_draws": stats.bot_draws if stats else 0,
        }

    # ------------------------------------------------------------------
    # New main leaderboard — 4 tabs (LB1)
    # ------------------------------------------------------------------

    @classmethod
    async def get_top_castles(
        cls,
        session: AsyncSession,
        limit: int = 10,
    ) -> List[Tuple[Group, GroupCastle]]:
        """Top 10 groups by castle level (then total_donated), active only.

        Returns list of (Group, GroupCastle) ordered by level DESC,
        total_donated DESC, Group.id ASC.
        """
        stmt = (
            select(Group, GroupCastle)
            .join(GroupCastle, GroupCastle.group_id == Group.id)
            .where(Group.is_active == True)
            .order_by(
                GroupCastle.level.desc(),
                GroupCastle.total_donated.desc(),
                Group.id.asc(),
            )
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        # rows are (Group, GroupCastle) tuples
        return [(group, castle) for group, castle in rows]

    @classmethod
    async def get_top_warriors(
        cls,
        session: AsyncSession,
        limit: int = 10,
    ) -> List[Tuple[Group, int]]:
        """Top 10 groups by Castle Wars wins (finished wars where winner_id matches).

        Only counts wars with status='finished' and winner_id not null.
        Returns list of (Group, wins) ordered by wins DESC, Group.id ASC.
        """
        wins_subq = (
            select(
                GroupWar.winner_id.label("gid"),
                func.count(GroupWar.id).label("wins"),
            )
            .where(GroupWar.status == "finished")
            .where(GroupWar.winner_id.is_not(None))
            .group_by(GroupWar.winner_id)
            .subquery()
        )
        stmt = (
            select(Group, func.coalesce(wins_subq.c.wins, 0).label("wins"))
            .outerjoin(wins_subq, wins_subq.c.gid == Group.id)
            .where(Group.is_active == True)
            .order_by(func.coalesce(wins_subq.c.wins, 0).desc(), Group.id.asc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(group, int(wins)) for group, wins in rows]

    @classmethod
    async def get_top_defenders(
        cls,
        session: AsyncSession,
        limit: int = 10,
    ) -> List[Tuple[Group, int]]:
        """Top 10 groups by invasions won (status='VICTORY'), active only.

        Returns list of (Group, victories) ordered by victories DESC, Group.id ASC.
        """
        vic_subq = (
            select(
                Inv2Invasion.group_id.label("gid"),
                func.count(Inv2Invasion.id).label("victories"),
            )
            .where(Inv2Invasion.status == "VICTORY")
            .group_by(Inv2Invasion.group_id)
            .subquery()
        )
        stmt = (
            select(Group, func.coalesce(vic_subq.c.victories, 0).label("victories"))
            .outerjoin(vic_subq, vic_subq.c.gid == Group.id)
            .where(Group.is_active == True)
            .order_by(func.coalesce(vic_subq.c.victories, 0).desc(), Group.id.asc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(group, int(victories)) for group, victories in rows]

    @classmethod
    async def get_top_donors(
        cls,
        session: AsyncSession,
        limit: int = 10,
    ) -> List[Tuple[Group, int]]:
        """Top 10 groups by SUM(castle_donations.amount), active only.

        Returns list of (Group, total_amount) ordered by total DESC, Group.id ASC.
        """
        sum_subq = (
            select(
                CastleDonation.group_id.label("gid"),
                func.sum(CastleDonation.amount).label("total"),
            )
            .group_by(CastleDonation.group_id)
            .subquery()
        )
        stmt = (
            select(Group, func.coalesce(sum_subq.c.total, 0).label("total"))
            .outerjoin(sum_subq, sum_subq.c.gid == Group.id)
            .where(Group.is_active == True)
            .order_by(func.coalesce(sum_subq.c.total, 0).desc(), Group.id.asc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(group, int(total)) for group, total in rows]
