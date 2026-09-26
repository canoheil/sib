"""Matchmaking Queue model."""

from datetime import datetime, timezone, timedelta
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class MatchmakingQueue(Base, TimestampMixin):
    """Active queue entries waiting for opponents."""
    __tablename__ = "matchmaking_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    party_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("parties.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), index=True, nullable=False)
    game_id: Mapped[int] = mapped_column(Integer, ForeignKey("games.id"), index=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), index=True, nullable=False)  # solo, 2v2, 4v4
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)

    # Relationships
    party = relationship("Party")
    group = relationship("Group")
    game = relationship("Game")

    def __repr__(self) -> str:
        return f"<MatchmakingQueue id={self.id} party_id={self.party_id} group_id={self.group_id} mode={self.mode}>"
