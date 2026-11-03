"""Persistent GTA-style wanted state, one row per player.

A missing row means a clean player: no stars, no cooldown, not jailed.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base


class UserWantedState(Base):
    """Wanted level (0 = clean, 1-5 stars) plus theft timers."""

    __tablename__ = "user_wanted_states"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    wanted_level: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_stolen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Coarse "sentence" timestamp kept for the /wanted display. Stars decay
    # one per theft_star_decay_hours since last_stolen_at (migration 078);
    # this timestamp only clears legacy rows that have no last_stolen_at.
    wanted_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # While now < jailed_until the thief cannot steal at all.
    jailed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
