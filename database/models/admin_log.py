"""Durable audit trail for Phase 6 invasion-v2 admin commands."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sib.database.base import Base

_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class AdminLog(Base):
    __tablename__ = "admin_logs"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
