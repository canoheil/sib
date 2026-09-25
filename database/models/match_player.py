"""Match Player model."""

from typing import Optional
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class MatchPlayer(Base, TimestampMixin):
    """Association between match, user, and team."""
    __tablename__ = "match_players"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("groups.id"), nullable=True, index=True)
    
    team: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 or 2
    turn_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    has_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    result: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # WIN, LOSS, DRAW

    # Relationships
    match = relationship("Match", back_populates="players")
    user = relationship("User")
    group = relationship("Group")

    def __repr__(self) -> str:
        return f"<MatchPlayer match_id={self.match_id} user_id={self.user_id} team={self.team}>"
