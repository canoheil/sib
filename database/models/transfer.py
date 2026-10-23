"""User-to-user apple transfers: audit log + confirmation hand-off.

Phase 1 defines the DATA LAYER only - no Telegram handlers, no Persian text.

Four transferable "currencies" exist and they are stored in two different
places (the transfer service treats them uniformly by ``item_type``):

* ``"apple"``            normal Apples, the game currency -> ``apple_wallets.balance``
* ``"apple_gold"``       Golden Apples    -> ``user_items`` row ``item_key='apple_gold'``
* ``"apple_diamond"``    Diamond Apples   -> ``user_items`` row ``item_key='apple_diamond'``
* ``"apple_obsidian"``   Obsidian Apples  -> ``user_items`` row ``item_key='apple_obsidian'``

``transfer_log`` is the immutable audit trail of every COMPLETED transfer:
one row per transfer, written in the same transaction as the balance change.
It is also the source for the cooldown check (latest row of the sender) and
the daily-limit check (rows of the sender inside the current Tehran day), which
is why both ``(from_user_id, created_at)`` and ``(to_user_id, created_at)``
are indexed.

``pending_transfer`` holds the two-step confirmation introduced in Phase 2:
a command creates a ``pending`` row, the confirmation message remembers
``chat_id``/``message_id``, and the row expires. Nothing in Phase 1 writes it
except the cleanup helper; the table exists now so the migration and the
service contract are final.

Both ``from_user_id``/``to_user_id`` are plain foreign keys without
``ON DELETE CASCADE``: the log is an audit trail that must survive a deleted
account (``users`` deletion is not part of the bot's flow anyway).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base


class PendingTransferState:
    """Lifecycle of a ``pending_transfer`` row (stored in ``state``)."""

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TransferLog(Base):
    """One COMPLETED transfer (immutable audit row)."""

    __tablename__ = "transfer_log"
    __table_args__ = (
        # Daily limit + cooldown: the sender's recent transfers.
        Index("ix_transfer_log_from_user_created_at", "from_user_id", "created_at"),
        # Receiver history.
        Index("ix_transfer_log_to_user_created_at", "to_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    to_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    # One of "apple" | "apple_gold" | "apple_diamond" | "apple_obsidian".
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Requested amount (what the receiver got). The sender paid amount + fee.
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    fee: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<TransferLog {self.id} {self.item_type} {self.amount} "
            f"from={self.from_user_id} to={self.to_user_id}>"
        )


class PendingTransfer(Base):
    """A transfer awaiting the sender's confirmation (Phase 2)."""

    __tablename__ = "pending_transfer"
    __table_args__ = (
        # Expiry sweep (cleanup_expired_pending).
        Index("ix_pending_transfer_state_expires_at", "state", "expires_at"),
        # Duplicate-pending checks for one sender.
        Index("ix_pending_transfer_from_user_state", "from_user_id", "state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    to_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Set once the confirmation message has been sent.
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PendingTransferState.PENDING,
        server_default=PendingTransferState.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PendingTransfer {self.id} {self.item_type} {self.amount} "
            f"state={self.state}>"
        )
