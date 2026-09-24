"""Sponsor and Official Channel model."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sib.database.base import Base, TimestampMixin


class ChannelType:
    OFFICIAL = "OFFICIAL"
    SPONSOR = "SPONSOR"


class SponsorChannel(Base, TimestampMixin):
    """Channels used for membership gate (official channel or sponsored channels)."""
    __tablename__ = "sponsor_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    invite_link: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    channel_type: Mapped[str] = mapped_column(String(20), default=ChannelType.OFFICIAL, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<SponsorChannel channel_id={self.channel_id} type={self.channel_type} required={self.is_required}>"
