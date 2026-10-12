"""Weekly rotating shop models (Phase 11).

A shop rotation is a time-bounded set of ShopEntry items. Rotations are
data-driven: duration (shop_rotation_hours) and the character pool
(shop_pool_character_ids; empty = all released & shop-enabled) live in
system_config.

Card pricing is NOT data-driven and NOT per-card: the shop sells every
card for exactly :data:`SHOP_CARD_PRICE_APPLES` Apples. There is no rarity
tier and no per-card/per-entry price - see ``services/shop.py``
(``ShopService.card_price``), which is the one price the purchase
transaction and every screen use.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin

# THE price of a shop card, in Apples - the single definition in the
# codebase. Every card costs exactly this, regardless of identity, faction,
# stats, release date or any legacy column value, and cards added in future
# updates need no price configuration at all (the price is never read from
# `characters.price` or `shop_entries.price_override`).
#
# It lives with the shop models (rather than in a service) so the character
# model can reuse it as its backward-compatible column default without the
# data layer importing services. Application code reads the price through
# ``ShopService.card_price()``.
SHOP_CARD_PRICE_APPLES: int = 300


class ShopEntryType:
    CHARACTER = "CHARACTER"
    BUNDLE = "BUNDLE"  # reserved for future item types


class ShopRotation(Base, TimestampMixin):
    """One weekly rotation window."""

    __tablename__ = "shop_rotations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    week_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<ShopRotation {self.week_id} active={self.is_active}>"


class ShopEntry(Base, TimestampMixin):
    """An item offered during a specific rotation."""

    __tablename__ = "shop_entries"
    __table_args__ = (
        UniqueConstraint("rotation_id", "character_id", name="uq_shop_entry_rotation_character"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rotation_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    character_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(20), default=ShopEntryType.CHARACTER, nullable=False)
    # DEPRECATED: a per-entry price override. Card prices are unified
    # (SHOP_CARD_PRICE_APPLES) and this column is never read - it is kept
    # (always NULL, migration 033 clears any historical value) so no
    # existing installation loses data and old rows stay loadable.
    price_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    badge: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<ShopEntry rotation={self.rotation_id} char={self.character_id}>"
