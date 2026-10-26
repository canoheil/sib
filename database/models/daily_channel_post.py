"""Durable once-per-Tehran-day channel digest delivery record."""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class DailyChannelPost(Base, TimestampMixin):
    """One scheduled channel digest decision per Tehran calendar date.

    ``post_day`` is the idempotency key. ``sending`` is written before the
    Telegram API call so a restart cannot blindly publish the same digest
    again. A failed delivery is visible to admins and can be retried manually.
    ``skipped`` is durable too: a quiet day is checked once, not every
    maintenance tick.
    """

    __tablename__ = "daily_channel_posts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sending', 'sent', 'skipped', 'failed')",
            name="ck_daily_channel_posts_status",
        ),
    )

    post_day: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(100), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    skip_reason: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<DailyChannelPost {self.post_day} {self.status}>"
