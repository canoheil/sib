"""Player Statistics model."""

from sqlalchemy import BigInteger, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from goje.database.base import Base, TimestampMixin


class PlayerStats(Base, TimestampMixin):
    """Aggregate individual player participation and game stats (non-rated)."""
    __tablename__ = "player_stats"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True, autoincrement=False)
    matches_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    draws: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    user = relationship("User")

    def __repr__(self) -> str:
        return f"<PlayerStats user_id={self.user_id} matches={self.matches_played} W:{self.wins}/L:{self.losses}/D:{self.draws}>"
