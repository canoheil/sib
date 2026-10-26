"""Durable invitation/presentation envelope around the existing AppleBattle."""

from datetime import datetime
from typing import Optional

from sib.database.base import Base, TimestampMixin
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column


class AppleDuel(Base, TimestampMixin):
    __tablename__ = "apple_duels"
    __table_args__ = (
        # Weekly league: "who duelled inside this group this week".
        Index("ix_apple_duels_group_created", "group_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inviter_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    recipient_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING", index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    battle_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("apple_battles.id"), unique=True
    )
    snapshot_json: Mapped[Optional[str]] = mapped_column(Text)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_scene_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    terminal_presented: Mapped[bool] = mapped_column(default=False, nullable=False)


class AppleDuelSlot(Base):
    """Unique durable reservation; two roles can never occupy a user twice."""

    __tablename__ = "apple_duel_slots"
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), primary_key=True, autoincrement=False
    )
    duel_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("apple_duels.id"), nullable=False, index=True
    )
