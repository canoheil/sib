"""Rating model for Groups."""

from sqlalchemy import BigInteger, Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from goje.database.base import Base, TimestampMixin


class Rating(Base, TimestampMixin):
    """Competitive global rating for Telegram Groups."""
    __tablename__ = "ratings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    rating: Mapped[float] = mapped_column(Float, default=1500.0, nullable=False, index=True)
    matches_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    draws: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    group = relationship("Group", back_populates="rating")

    def __repr__(self) -> str:
        return f"<Rating group_id={self.group_id} rating={self.rating} W:{self.wins}/L:{self.losses}/D:{self.draws}>"
