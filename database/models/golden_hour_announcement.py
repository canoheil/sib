"""Durable once-per-Tehran-day Golden Hour notice for each active group."""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class GoldenHourGroupAnnouncement(Base, TimestampMixin):
    """One delivery decision for a group's Tehran calendar day.

    The composite primary key prevents duplicate notices across maintenance
    retries and restarts. ``sending`` is committed before a standalone Telegram
    send, so a crash cannot blindly create a second message. Invasion notices
    are recorded with the invasion message that carried them.
    """

    __tablename__ = "golden_hour_group_announcements"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sending', 'sent', 'failed', 'skipped')",
            name="ck_golden_hour_group_announcements_status",
        ),
    )

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", name="fk_golden_hour_group_announcements_group"),
        primary_key=True,
        autoincrement=False,
    )
    announcement_day: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    invasion_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
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
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<GoldenHourGroupAnnouncement group={self.group_id} "
            f"day={self.announcement_day} {self.status}>"
        )
