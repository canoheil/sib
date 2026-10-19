"""Apple Mine shift history."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class AppleMineShift(Base):
    """One completed Apple Mine work shift and its single reward."""

    __tablename__ = "apple_mine_shift"
    __table_args__ = (
        Index("ix_apple_mine_shift_user_worked_at", "user_id", "worked_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    card_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("player_cards.id", ondelete="SET NULL"), nullable=True
    )
    # M2-RECON: the pickaxe level this shift was rolled with; NULL for
    # pre-M2 rows (card selection was cosmetic, M1).
    pickaxe_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    worked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<AppleMineShift id={self.id} user={self.user_id} "
            f"outcome={self.outcome} amount={self.amount}>"
        )


class UserPickaxe(Base, TimestampMixin):
    """The player's Apple Mine pickaxe (M2-RECON): one row per user.

    Level 1..5. The row is created lazily on first mine access, so every
    existing player starts at Level 1 with no backfill.
    """

    __tablename__ = "user_pickaxes"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, autoincrement=False,
    )
    level: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )

    def __repr__(self) -> str:
        return f"<UserPickaxe user_id={self.user_id} level={self.level}>"
