"""Group statistics model (win-based leaderboard).

Elo/MMR has been removed from the game. Groups are ranked by total wins
with deterministic tie-breakers (win rate, matches played, group id).
"""

from sqlalchemy import BigInteger, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class GroupStats(Base, TimestampMixin):
    """Aggregated competitive statistics for a Telegram group."""

    __tablename__ = "group_stats"

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    draws: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matches_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    group = relationship("Group", back_populates="stats")

    @property
    def win_rate(self) -> float:
        if self.matches_played <= 0:
            return 0.0
        return self.wins / self.matches_played * 100.0

    def __repr__(self) -> str:
        return (
            f"<GroupStats group_id={self.group_id} W:{self.wins}/"
            f"L:{self.losses}/D:{self.draws}>"
        )
