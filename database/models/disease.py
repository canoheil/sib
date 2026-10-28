"""Persistent disease and immunity state, one row per player."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, false
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base


class UserDisease(Base):
    """A missing row represents a healthy player with no immunity."""

    __tablename__ = "user_diseases"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    infection_level: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    has_demon_power: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    infected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    immunity_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
