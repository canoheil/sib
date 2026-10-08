"""Daily Sib Treasure Chest claim ledger.

Exactly ONE row per player per UTC daily cycle (``claim_day`` is the
``YYYY-MM-DD`` UTC calendar date, matching the project's established
daily-boundary convention used by trades, Flowers and XP daily caps).

The ``(user_id, claim_day)`` unique constraint is the database-level
arbiter against duplicate daily claims: concurrent button presses,
duplicate Telegram updates, retries and other bot processes all collide
on it. The reward columns snapshot WHAT was granted (the actual value
lives in the canonical wallet/inventory/seedling/card tables), so the row
is both the claim gate and the reward audit record.
"""

from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin

# Reward categories, in rising rarity order (the exact strings stored in
# ``reward_type``). The special apples use their canonical ``user_items``
# item keys so the inventory stays a single source of truth.
REWARD_APPLES = "apples"
REWARD_SEEDLINGS = "seedlings"
REWARD_GOLD = "apple_gold"
REWARD_DIAMOND = "apple_diamond"
REWARD_OBSIDIAN = "apple_obsidian"
REWARD_CARD = "card"


class DailyChestClaim(Base, TimestampMixin):
    """One durable claim of the daily chest: gate + reward audit record."""

    __tablename__ = "daily_chest_claims"
    __table_args__ = (
        UniqueConstraint("user_id", "claim_day", name="uq_daily_chest_claim_user_day"),
        CheckConstraint("quantity >= 1", name="ck_daily_chest_claim_quantity"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # UTC calendar day, "YYYY-MM-DD" (the project's daily-reset boundary).
    claim_day: Mapped[str] = mapped_column(String(10), nullable=False)
    reward_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Snapshot of the granted card's character for card rewards (auditing).
    ref_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<DailyChestClaim user={self.user_id} day={self.claim_day} "
            f"reward={self.reward_type} x{self.quantity}>"
        )
