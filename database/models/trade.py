"""Card trading model (Phase 13)."""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class TradeStatus:
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    # Terminal state for a trade that could not be finalized (ownership or
    # card-state validation failed at execution). Unlike CANCELLED (a user's
    # deliberate cancel) a FAILED trade never consumes the daily quota.
    # Stored in the existing String(20) column - no migration required.
    FAILED = "FAILED"
    # Transient state used by the one-shot execution claim (PENDING ->
    # COMPLETING -> COMPLETED/FAILED, all inside one transaction).
    COMPLETING = "COMPLETING"


class Trade(Base, TimestampMixin):
    """A card-for-card trade proposal with double confirmation."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    proposer_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    recipient_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    proposer_card_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recipient_card_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=TradeStatus.PENDING, nullable=False, index=True)
    proposer_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recipient_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Trade {self.id} {self.status}>"
