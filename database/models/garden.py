"""Personal garden models (Phase 7).

Every player has a personal garden of plots. A seedling 🌱 planted in a
plot grows (server-timestamp based) into a mature tree 🌳 which produces
apples 🍎. Produced fruit remains available until collected; after a
harvest the tree enters another production cycle.

Plot states are derived from server timestamps so client time can never
exploit growth.
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class PlotState:
    """Lifecycle states of a garden plot."""

    EMPTY = "EMPTY"
    GROWING = "GROWING"
    MATURE = "MATURE"
    FRUIT_READY = "FRUIT_READY"


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

    garden = relationship("Garden", back_populates="plots")

    def __repr__(self) -> str:
        return (
            f"<GardenPlot garden={self.garden_id} idx={self.plot_index} "
            f"cycles={self.cycle_count}>"
        )
