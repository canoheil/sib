"""SQLAlchemy models for the global Halloween event («سیبومورتیس» / Sibomortis)."""

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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base

# SQLite only autoincrements INTEGER PRIMARY KEY; MariaDB keeps BIGINT AUTO_INCREMENT.
_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class HalloweenEventState(Base):
    """Global non-group-bound world boss state and 7-day lifecycle window."""

    __tablename__ = "halloween_event_state"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    boss_hp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_hp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_phase: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    last_day_weakened: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    hp_before_weaken: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    weakened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    defeated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finisher_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    announced_milestones_mask: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("max_hp > 0", name="ck_hw_state_max_hp_pos"),
        CheckConstraint(
            "boss_hp >= 0 AND boss_hp <= max_hp", name="ck_hw_state_hp_bounds"
        ),
        CheckConstraint(
            "current_phase BETWEEN 1 AND 5", name="ck_hw_state_phase"
        ),
        Index("ix_hw_state_active", "is_active"),
    )


class HalloweenAttack(Base):
    """Immutable log of player attacks against Sibomortis (participation + leaderboard)."""

    __tablename__ = "halloween_attacks"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("halloween_event_state.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    card_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("player_cards.id", ondelete="SET NULL"),
        nullable=True,
    )
    character_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("characters.id", ondelete="SET NULL"),
        nullable=True,
    )
    damage: Mapped[int] = mapped_column(Integer, nullable=False)
    is_crit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    boss_hp_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("damage > 0", name="ck_hw_attacks_damage_pos"),
        Index("ix_hw_attacks_event_user", "event_id", "user_id"),
        Index("ix_hw_attacks_created_at", "created_at"),
    )


class HalloweenRewardClaim(Base):
    """Idempotent milestone reward claims (75%, 50%, 25%, 0%) per participant."""

    __tablename__ = "halloween_reward_claims"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("halloween_event_state.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    milestone_pct: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "user_id",
            "milestone_pct",
            name="uq_hw_reward_claims_event_user_pct",
        ),
        CheckConstraint(
            "milestone_pct IN (75, 50, 25, 0)", name="ck_hw_reward_claims_pct"
        ),
        Index("ix_hw_reward_claims_user", "user_id"),
    )


class HalloweenSeedCooldown(Base):
    """Per-user cooldown state for the Boss Menu «دریافت غنیمت» button."""

    __tablename__ = "halloween_seed_cooldowns"

    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("halloween_event_state.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    total_claims: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    total_seeds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class HalloweenLootDrop(Base):
    """Random Halloween seed chest spawned inside an active group."""

    __tablename__ = "halloween_loot_drops"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("halloween_event_state.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    variant_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default="PENDING"
    )
    max_claims: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    claimed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_hw_drops_status_expires", "status", "expires_at"),
        Index("ix_hw_drops_group_created", "group_id", "created_at"),
    )


class HalloweenLootDropClaim(Base):
    """One winner row per group chest claim (first N players, never twice per user)."""

    __tablename__ = "halloween_loot_drop_claims"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    drop_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("halloween_loot_drops.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    seeds_granted: Mapped[int] = mapped_column(Integer, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("drop_id", "user_id", name="uq_hw_drop_claims_drop_user"),
        UniqueConstraint("drop_id", "slot_no", name="uq_hw_drop_claims_drop_slot"),
        CheckConstraint(
            "seeds_granted BETWEEN 1 AND 3", name="ck_hw_drop_claims_seeds"
        ),
        Index("ix_hw_drop_claims_user", "user_id"),
    )
