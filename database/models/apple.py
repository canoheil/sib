"""Apple wallet and transaction ledger models (Phase 7/8).

Apples 🍎 are the primary in-game currency. Every balance change MUST
create an auditable ledger entry. Balances can never go negative and
duplicates are prevented through unique idempotency keys.
"""

from typing import Optional
from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class AppleWallet(Base, TimestampMixin):
    """Personal apple wallet for a player."""

    __tablename__ = "apple_wallets"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, autoincrement=False,
    )
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user = relationship("User")

    def __repr__(self) -> str:
        return f"<AppleWallet user_id={self.user_id} balance={self.balance}>"


class AppleTransaction(Base, TimestampMixin):
    """Auditable ledger entry for a single wallet balance change."""

    __tablename__ = "apple_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_apple_tx_idempotency"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # signed
    reason: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    ref_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    ref_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Effective multiplier for a credited gameplay reward; 1 for ordinary
    # credits and all debits. Stored so an idempotent retry after the golden
    # hour keeps validating against the multiplier used by the original event.
    reward_multiplier: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default="1", nullable=False
    )
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<AppleTransaction user={self.user_id} amount={self.amount} "
            f"reason={self.reason} balance_after={self.balance_after}>"
        )
