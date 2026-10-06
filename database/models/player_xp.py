"""Persistent account-level XP progression and its auditable grant ledger."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class PlayerProgression(Base, TimestampMixin):
    """One cumulative XP counter per Sib account.

    Account Level is deliberately not stored here: it is derived by the one
    authoritative curve in ``services.player_xp``. Keeping only cumulative
    XP prevents level/progress fields from drifting out of sync.
    """

    __tablename__ = "player_progressions"
    __table_args__ = (
        CheckConstraint(
            "total_xp >= 0 AND total_xp <= 87000",
            name="ck_player_progressions_total_xp",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    total_xp: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<PlayerProgression user_id={self.user_id} total_xp={self.total_xp}>"


class PlayerXPGrant(Base, TimestampMixin):
    """An idempotent XP event, including actual amount after caps.

    Level transition columns are an immutable snapshot of the event. They
    also serve as a tiny progression-specific post-commit notification queue:
    only level-crossing grants need a congratulatory message, and a failed
    Telegram delivery can be retried by the existing maintenance worker.
    """

    __tablename__ = "player_xp_grants"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "event_key", name="uq_player_xp_grant_event"
        ),
        CheckConstraint("amount >= 0", name="ck_player_xp_grants_amount_nonnegative"),
        CheckConstraint(
            "level_before >= 1 AND level_before <= 30 "
            "AND level_after >= 1 AND level_after <= 30",
            name="ck_player_xp_grants_levels",
        ),
        Index("ix_player_xp_grants_user_awarded_at", "user_id", "awarded_at"),
        Index(
            "ix_player_xp_grants_user_source_awarded_at",
            "user_id",
            "source",
            "awarded_at",
        ),
        Index(
            "ix_player_xp_grants_notification_pending",
            "notification_sent_at",
            "notification_claimed_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # The actual grant after source, UTC-day, and Level-30 caps (can be 0).
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    level_before: Mapped[int] = mapped_column(Integer, nullable=False)
    level_after: Mapped[int] = mapped_column(Integer, nullable=False)

    # A chat id is saved with the event so deferred delivery can prefer a
    # group/public fallback if the player's private chat is unavailable.
    notification_chat_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    notification_claimed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notification_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<PlayerXPGrant id={self.id} user_id={self.user_id} "
            f"source={self.source!r} amount={self.amount}>"
        )
