"""Apple Battle (نبرد سیبی) models (Phase 14).

A separate competitive system with its own queue-based matchmaking and
its own leaderboard. Results are PRECOMPUTED and persisted (with the
balance version used) BEFORE any narration is shown — the outcome never
changes after the fact.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class AppleBattleStatus:
    PENDING = "PENDING"          # matched, result being computed
    RESULT_READY = "RESULT_READY"  # precomputed, awaiting narration/award
    COMPLETED = "COMPLETED"      # narrated + winner awarded
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AppleBattle(Base, TimestampMixin):
    """A single apple battle between two players' cards."""

    __tablename__ = "apple_battles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player1_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    player2_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    # Shared matchmaking link (nullable for legacy rows).
    match_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("matches.id", ondelete="SET NULL"), index=True, nullable=True
    )
    player1_card_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    player2_card_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=AppleBattleStatus.PENDING, nullable=False, index=True)
    winner_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Precomputed result, persisted BEFORE narration.
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    balance_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rng_seed: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    seedlings_awarded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    matched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AppleBattle {self.id} {self.status}>"


class AppleBattleQueueEntry(Base, TimestampMixin):
    """One active queue entry per user."""

    __tablename__ = "apple_battle_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    card_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    level: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AppleBattleQueue user={self.user_id} L{self.level}>"
