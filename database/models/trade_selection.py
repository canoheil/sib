"""Short-lived private card selection initiated from a shared trade offer."""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class TradeSelectionStatus:
    PENDING = "PENDING"
    SELECTED = "SELECTED"
    EXPIRED = "EXPIRED"


class TradeSelection(Base, TimestampMixin):
    """Persist the public-offer context while the recipient chooses privately.

    The high-entropy token is the only callback identifier sent in the DM;
    ownership, the offered card and originating group/message stay in this
    row and are revalidated before a Trade is created.
    """

    __tablename__ = "trade_selections"
    __table_args__ = (
        Index("ix_trade_selections_recipient_status", "recipient_id", "status"),
        Index("ix_trade_selections_expires_at", "expires_at"),
    )

    token: Mapped[str] = mapped_column(String(32), primary_key=True, autoincrement=False)
    proposer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_trade_selection_proposer"),
        nullable=False,
        index=True,
    )
    proposer_card_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("player_cards.id", ondelete="CASCADE", name="fk_trade_selection_proposer_card"),
        nullable=False,
    )
    recipient_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_trade_selection_recipient"),
        nullable=False,
        index=True,
    )
    offer_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    offer_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    recipient_card_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("player_cards.id", ondelete="SET NULL", name="fk_trade_selection_recipient_card"),
        nullable=True,
    )
    trade_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("trades.id", ondelete="SET NULL", name="fk_trade_selection_trade"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), default=TradeSelectionStatus.PENDING,
        server_default=TradeSelectionStatus.PENDING, nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
