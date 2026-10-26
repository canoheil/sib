"""Apple Clash (Group War) storage models."""

from datetime import datetime
from typing import Optional
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sib.database.base import Base

_ID = BigInteger().with_variant(Integer(), "sqlite")


class GroupWar(Base):
    __tablename__ = "group_wars"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'finished')", name="ck_group_wars_status"),
        Index("ix_group_wars_status_ends_at", "status", "ends_at"),
        Index("ix_group_wars_attacker_status", "attacker_id", "status"),
        Index("ix_group_wars_defender_status", "defender_id", "status"),
    )

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    attacker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("group_castles.group_id"), nullable=False
    )
    defender_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("group_castles.group_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    attacker_damage: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    defender_damage: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    winner_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("groups.id"), nullable=True
    )
    looted_apples: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )


class WarParticipant(Base):
    __tablename__ = "war_participants"
    __table_args__ = (
        Index("ix_war_participants_war_group", "war_id", "group_id"),
    )

    war_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("group_wars.id"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id"), nullable=False
    )
    energy: Mapped[int] = mapped_column(
        Integer, default=100, server_default="100", nullable=False
    )
    total_damage: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # C2-8.5: hard cap counter (max 10 attacks per player per war). It starts
    # at 0 so a fresh participant really gets ten attacks; ``DEFAULT 0`` also
    # backfills rows created before this column existed.
    attack_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_attack_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WarAttack(Base):
    __tablename__ = "war_attacks"
    __table_args__ = (
        Index("ix_war_attacks_war_user", "war_id", "user_id"),
        Index("ix_war_attacks_war_source_group", "war_id", "source_group_id"),
        # Weekly league: damage dealt by each group inside a week.
        Index("ix_war_attacks_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    war_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("group_wars.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    source_group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id"), nullable=False
    )
    target_group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id"), nullable=False
    )
    damage: Mapped[int] = mapped_column(Integer, nullable=False)
    is_buff_used: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
