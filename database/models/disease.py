"""Persistent disease, immunity, and per-group daily doctor state."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, false, func
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


class GroupDoctorState(Base):
    """Per-group Doctor of the Day selection and daily healing quota."""

    __tablename__ = "group_doctor_states"

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    doctor_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    heals_today: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )

