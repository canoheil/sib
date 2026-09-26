"""Seedling inventory and award ledger models (Phase 6).

Seedlings 🌱 are personal progression items:
- exactly one seedling per eligible normal group-game win,
- zero on losses and zero on bot-match wins,
- awarded idempotently (unique per match+user) and auditable.
"""

from typing import Optional
from sqlalchemy import BigInteger, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class SeedlingInventory(Base, TimestampMixin):
    """Personal seedling counter for a player."""

    __tablename__ = "seedling_inventories"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, autoincrement=False,
    )
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user = relationship("User")

    def __repr__(self) -> str:
        return f"<SeedlingInventory user_id={self.user_id} total={self.total}>"


class SeedlingAward(Base, TimestampMixin):
    """Auditable seedling award ledger entry (idempotency guard)."""

    __tablename__ = "seedling_awards"
    __table_args__ = (
        UniqueConstraint("match_id", "user_id", name="uq_seedling_award_match_user"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("matches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reason: Mapped[str] = mapped_column(String(50), default="match_win", nullable=False)

    def __repr__(self) -> str:
        return f"<SeedlingAward match={self.match_id} user={self.user_id} amount={self.amount}>"
