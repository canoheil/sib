"""Invasion v2 storage, isolated from the legacy invasion tables."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


# SQLite only autoincrements INTEGER primary keys; production uses BIGINT.
_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class Inv2Region(Base, TimestampMixin):
    """A territory of Ovarya; control is stored as the supplied Persian label."""

    __tablename__ = "inv2_regions"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_fa: Mapped[str] = mapped_column(String(160), nullable=False)
    controlling_faction: Mapped[str] = mapped_column(String(64), nullable=False)
    is_lost_to_enemy: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    # Not a foreign key: the requested schema leaves this optional link
    # nullable, avoiding a cycle between inv2_regions and inv2_invasions.
    last_invasion_id: Mapped[Optional[int]] = mapped_column(BigInteger)


class Inv2Invasion(Base, TimestampMixin):
    """One scheduled invasion for one group and region."""

    __tablename__ = "inv2_invasions"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_inv2_invasions_occurrence_key"),
        Index("ix_inv2_invasions_status_ends_at", "status", "ends_at"),
        Index("ix_inv2_invasions_group_status", "group_id", "status"),
        Index("ix_inv2_invasions_region_code", "region_code"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    region_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("inv2_regions.code", name="fk_inv2_invasions_region"), nullable=False
    )
    enemy_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enemy_name_fa: Mapped[str] = mapped_column(String(160), nullable=False)
    enemy_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    enemy_hp_remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    difficulty_multiplier: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id", name="fk_inv2_invasions_group"), nullable=False
    )
    message_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Stamped when the group announcement is pinned and cleared once a later
    # invasion of the same group unpins it. NOT NULL therefore means "this
    # message is the pin we placed", so the bot never pins twice and never
    # unpins a message an admin pinned themselves.
    pinned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Exact rendered narrative of the sent announcement, so live-HP caption
    # edits rebuild the message with the same prose (pool picks are not
    # replayable once made).
    announcement_narrative: Mapped[Optional[str]] = mapped_column(Text)
    announcement_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    occurrence_key: Mapped[str] = mapped_column(String(255), nullable=False)


class Inv2Attack(Base):
    """One join/attack record per user in an invasion."""

    __tablename__ = "inv2_attacks"
    __table_args__ = (
        UniqueConstraint("invasion_id", "user_id", name="uq_inv2_attacks_invasion_user"),
        Index("ix_inv2_attacks_user_id", "user_id"),
        Index("ix_inv2_attacks_card_id", "card_id"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    invasion_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("inv2_invasions.id", name="fk_inv2_attacks_invasion"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="fk_inv2_attacks_user"), nullable=False
    )
    card_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("player_cards.id", name="fk_inv2_attacks_card"), nullable=False
    )
    card_power: Mapped[int] = mapped_column(Integer, nullable=False)
    damage: Mapped[int] = mapped_column(Integer, nullable=False)
    is_crit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Inv2Reward(Base):
    """A granted item reward for one participating user."""

    __tablename__ = "inv2_rewards"
    __table_args__ = (
        Index("ix_inv2_rewards_invasion_id", "invasion_id"),
        Index("ix_inv2_rewards_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    invasion_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("inv2_invasions.id", name="fk_inv2_rewards_invasion"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="fk_inv2_rewards_user"), nullable=False
    )
    reward_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reward_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Inv2GroupState(Base):
    """Persistent Telegram-send circuit breaker and win/loss counters."""

    __tablename__ = "inv2_group_state"

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", name="fk_inv2_group_state_group"),
        primary_key=True,
        autoincrement=False,
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    muted_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    total_victories: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    total_defeats: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
