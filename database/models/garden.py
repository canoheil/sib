"""Personal garden models (Phase 7).

Every player has a personal garden of plots. A seedling 🌱 planted in a
plot grows (server-timestamp based) into a mature tree 🌳 which produces
apples 🍎. Produced fruit remains available until collected; after a
harvest the tree enters another production cycle.

Plot states are derived from server timestamps so client time can never
exploit growth.
"""

from datetime import date, datetime
from typing import Optional
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class PlotState:
    """Lifecycle states of a garden plot."""

    EMPTY = "EMPTY"
    GROWING = "GROWING"
    MATURE = "MATURE"
    FRUIT_READY = "FRUIT_READY"
    # Corruption state (Invasion v2 defeat/announced-expiry penalty). It is a
    # stored flag on the plot, not a timestamp-derived state: a withered plot
    # stays locked (no planting, no harvest, no magic) until Divine Fertilizer
    # restores it.
    WITHERED = "WITHERED"


class Garden(Base, TimestampMixin):
    """Personal garden owned by exactly one player."""

    __tablename__ = "gardens"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, autoincrement=False,
    )
    capacity_plots: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    plots = relationship("GardenPlot", back_populates="garden", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Garden user_id={self.user_id} capacity={self.capacity_plots}>"


class GardenPlot(Base, TimestampMixin):
    """A single planting plot inside a garden."""

    __tablename__ = "garden_plots"
    __table_args__ = (
        UniqueConstraint("garden_id", "plot_index", name="uq_garden_plot_index"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    garden_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("gardens.user_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    plot_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # Lifecycle bookkeeping (server timestamps, timezone-aware).
    planted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    mature_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fruit_ready_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Completed harvest cycles of this tree.
    cycle_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Magical progression of THIS tree (0..garden_max_magic_level). Each
    # level was paid for with Magic Powder and improves special-apple
    # drop chances. Per-tree, never account-global.
    magic_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Invasion corruption (G2): when the player's group loses an Invasion and
    # the player dealt zero damage, one random plot is withered. A withered
    # plot cannot be planted, harvested or enchanted until Divine Fertilizer
    # clears this flag. Defaults to healthy for every existing row.
    is_withered: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )

    garden = relationship("Garden", back_populates="plots")

    def __repr__(self) -> str:
        return (
            f"<GardenPlot garden={self.garden_id} idx={self.plot_index} "
            f"cycles={self.cycle_count} magic={self.magic_level} withered={self.is_withered}>"
        )


class GardenWitherEvent(Base, TimestampMixin):
    """Audit + fairness ledger for Invasion-driven garden corruption.

    One row per withered plot. The two unique constraints are the whole
    fairness mechanism, enforced at the database level:

    - ``uq_garden_wither_user_day`` — at most ONE wither per player per UTC
      calendar day, no matter how many groups lose invasions that day
      (protects players against e.g. a 3 AM invasion they could not attend).
    - ``uq_garden_wither_user_invasion`` — re-running settlement for the same
      invasion (retry, admin force-end replay, duplicate maintenance task)
      can never wither twice.

    ``wither_day`` is a UTC ``DATE`` (the project's daily-boundary convention,
    matching ``daily_chest_claims.claim_day``).
    """

    __tablename__ = "garden_wither_events"
    __table_args__ = (
        UniqueConstraint("user_id", "wither_day", name="uq_garden_wither_user_day"),
        UniqueConstraint(
            "user_id", "invasion_id", name="uq_garden_wither_user_invasion"
        ),
        Index("ix_garden_wither_user_id", "user_id"),
        Index("ix_garden_wither_invasion_id", "invasion_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    invasion_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("inv2_invasions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # UTC calendar day of the withering (YYYY-MM-DD).
    wither_day: Mapped[date] = mapped_column(Date, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<GardenWitherEvent user={self.user_id} invasion={self.invasion_id} "
            f"day={self.wither_day.isoformat()}>"
        )
