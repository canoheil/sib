"""Random events inside an active Invasion v2 battle.

Three tables, deliberately separate from the invasion/attack/reward tables:

* ``inv2_random_events`` — one planned event per ``(invasion_id, slot_no)``,
  with its schedule, lifecycle, frozen prose and Telegram message.
* ``inv2_event_effects`` — the timed combat modifiers a join reads
  (``damage_multiplier``, ``crit_bonus``, ``damage_cap_pct``) plus the
  one-shot HP effects (``boss_heal_pct``, ``instant_damage_pct``) kept as an
  audit trail of *why* an event moved the boss HP.
* ``inv2_event_claims`` — one row per loot winner. ``(event_id, user_id)``
  makes a second click by the same player impossible and
  ``(event_id, slot_no)`` makes a fourth winner impossible, independent of
  any application-level counter.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
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


class Inv2RandomEvent(Base, TimestampMixin):
    """One random event inside one invasion.

    Status lifecycle: ``PLANNED`` (in the plan, not due) → ``APPLIED`` (due;
    instant effects committed, waiting for its photo) → ``ACTIVE`` (photo
    accepted, timed window or chest window running) → ``CLOSED``. Anything
    that never applies ends in ``SKIPPED``.
    """

    __tablename__ = "inv2_random_events"
    __table_args__ = (
        UniqueConstraint("invasion_id", "slot_no", name="uq_inv2_random_events_slot"),
        Index("ix_inv2_random_events_invasion_status", "invasion_id", "status"),
        Index("ix_inv2_random_events_status_scheduled", "status", "scheduled_at"),
        Index("ix_inv2_random_events_group_id", "group_id"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    invasion_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("inv2_invasions.id", name="fk_inv2_random_events_invasion"),
        nullable=False,
    )
    # Denormalized on purpose: the chest handler must prove the click came
    # from the owning group without joining the invasion row it no longer
    # needs to lock.
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", name="fk_inv2_random_events_group"),
        nullable=False,
    )
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    template_code: Mapped[str] = mapped_column(String(48), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    announced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    message_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Frozen prose: caption edits (chest status, closing) rebuild the exact
    # message the group saw, without replaying the narrative pool.
    narrative: Mapped[Optional[str]] = mapped_column(Text)
    # Real heal/damage applied. NULL means "this event never moved HP".
    applied_amount: Mapped[Optional[int]] = mapped_column(Integer)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    loot_item_key: Mapped[Optional[str]] = mapped_column(String(32))
    loot_amount: Mapped[Optional[int]] = mapped_column(Integer)
    loot_max_claims: Mapped[Optional[int]] = mapped_column(Integer)
    loot_claimed: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    send_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )


class Inv2EventEffect(Base):
    """One modifier of one event; ``kind`` names the mechanic."""

    __tablename__ = "inv2_event_effects"
    __table_args__ = (
        UniqueConstraint("event_id", "kind", name="uq_inv2_event_effects_event_kind"),
        Index("ix_inv2_event_effects_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("inv2_random_events.id", name="fk_inv2_event_effects_event"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)


class Inv2EventClaim(Base):
    """One loot winner; the two unique constraints are the arbiter."""

    __tablename__ = "inv2_event_claims"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_inv2_event_claims_event_user"),
        UniqueConstraint("event_id", "slot_no", name="uq_inv2_event_claims_event_slot"),
        Index("ix_inv2_event_claims_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("inv2_random_events.id", name="fk_inv2_event_claims_event"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", name="fk_inv2_event_claims_user"),
        nullable=False,
    )
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_type: Mapped[str] = mapped_column(String(24), nullable=False)
    reward_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
