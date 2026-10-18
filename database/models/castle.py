"""Shared castle progression and durable economy operation gates."""

from datetime import datetime
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
from sib.database.base import Base

_ID = BigInteger().with_variant(Integer(), "sqlite")


class GroupCastle(Base):
    __tablename__ = "group_castles"
    __table_args__ = (
        CheckConstraint("level BETWEEN 1 AND 10", name="ck_castle_level"),
        CheckConstraint("total_donated >= 0", name="ck_castle_total"),
    )
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id"), primary_key=True, autoincrement=False
    )
    level: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    total_donated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class CastleDonation(Base):
    __tablename__ = "castle_donations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_castle_donation_amount"),
        Index("ix_castle_donations_user_time", "user_id", "donated_at"),
        Index("ix_castle_donations_group_user", "group_id", "user_id"),
    )
    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("group_castles.group_id")
    )
    amount: Mapped[int] = mapped_column(Integer)
    donated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    request_key: Mapped[str] = mapped_column(String(100), unique=True)


class CastleIncomeClaim(Base):
    __tablename__ = "castle_income_claims"
    __table_args__ = (
        UniqueConstraint("user_id", "claim_day", name="uq_castle_income_user_day"),
    )
    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("group_castles.group_id")
    )
    claim_day: Mapped[str] = mapped_column(String(10))
    amount: Mapped[int] = mapped_column(Integer)
