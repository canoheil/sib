"""Weekly group league tables.

The league itself needs **no** instrumentation: points are *derived* from the
``matches`` table (normal, non-bot, finished matches inside the week), exactly
the way the daily streak is derived from the chest ledger. These two tables
exist only for the part that cannot be derived:

* :class:`GroupLeagueWeek` — one row per finalized week. Its primary key is
  the atomic "this week has already been paid" claim, so two maintenance
  workers (or a restart mid-finalize) can never pay the same week twice.
* :class:`GroupLeagueAward` — the frozen podium (rank, points, wins, ...) and
  how many players were paid, so the result stays visible after the matches
  that produced it have aged out of any query window.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sib.database.base import Base, TimestampMixin


class GroupLeagueWeek(Base, TimestampMixin):
    """One finalized league week (``week_key`` = the Tehran Saturday date)."""

    __tablename__ = "group_league_weeks"

    week_key: Mapped[str] = mapped_column(String(10), primary_key=True)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    winner_group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    winner_points: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Total Apples paid for this week (0 when no group reached the minimum).
    apples_paid: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    awards = relationship(
        "GroupLeagueAward",
        back_populates="week",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<GroupLeagueWeek {self.week_key} winner={self.winner_group_id} "
            f"apples={self.apples_paid}>"
        )


class GroupLeagueAward(Base, TimestampMixin):
    """A frozen podium row for one week (rank 1..3)."""

    __tablename__ = "group_league_awards"
    __table_args__ = (
        UniqueConstraint("week_key", "rank", name="uq_group_league_award_rank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    week_key: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("group_league_weeks.week_key", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    draws: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: How many players actually shared this rank's prize.
    players_paid: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    #: Apples per paid player (the config value at finalize time).
    apples_per_player: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    week = relationship("GroupLeagueWeek", back_populates="awards")

    def __repr__(self) -> str:
        return (
            f"<GroupLeagueAward {self.week_key} #{self.rank} group={self.group_id} "
            f"points={self.points}>"
        )
