"""Shop chests: configurable apple-priced chests sold in the shop.

Independent from the daily chest (``daily_chest_claims`` and its
``daily_chest:*`` keys are untouched). A shop chest is:

* ``shop_chests`` - the chest definition (code, name, optional image key,
  price in apples, active flag, display order, duplicate compensation) plus a
  ``version`` that is bumped on every configuration change.
* A nullable ``tier`` selects the rarity odds in ``services.shop_chests``;
  custom chests with no tier keep their existing weighted tables.
* ``shop_chest_pity`` - one per-user counter shared across paid shop chests.
* ``shop_chest_rewards`` - an INDEPENDENT weighted reward table per custom chest.
  Each row is one of: a fixed apple amount, an ``ITEM_CATALOG`` item, or a
  character card.
* ``shop_chest_openings`` - one row per successful opening. It is both the
  idempotency gate ``(user_id, operation_key)`` and a snapshot of what was
  granted, so historical outcomes survive later configuration changes.

Reward rows are replaced wholesale on configuration change; openings never
reference them (no FK), so a table rewrite cannot disturb history.
"""

from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin

# Reward kinds stored in ``shop_chest_rewards.kind``.
REWARD_KIND_APPLES = "apples"
REWARD_KIND_ITEM = "item"
REWARD_KIND_CARD = "card"
REWARD_KINDS = (REWARD_KIND_APPLES, REWARD_KIND_ITEM, REWARD_KIND_CARD)


class ShopChest(Base, TimestampMixin):
    __tablename__ = "shop_chests"
    __table_args__ = (
        UniqueConstraint("code", name="uq_shop_chests_code"),
        CheckConstraint("price_apples >= 1", name="ck_shop_chests_price_positive"),
        CheckConstraint("duplicate_apples >= 0", name="ck_shop_chests_duplicate_nonneg"),
        CheckConstraint("version >= 1", name="ck_shop_chests_version_positive"),
        Index("ix_shop_chests_active_sort", "is_active", "sort_order"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    image_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # None retains the custom reward table; tiered chests draw by rarity.
    tier: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    price_apples: Mapped[int] = mapped_column(Integer, nullable=False)
    # Apples credited instead of a card the user already owns.
    duplicate_apples: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class ShopChestReward(Base, TimestampMixin):
    __tablename__ = "shop_chest_rewards"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('apples', 'item', 'card')", name="ck_shop_chest_rewards_kind"
        ),
        CheckConstraint("weight >= 1", name="ck_shop_chest_rewards_weight_positive"),
        CheckConstraint(
            "(kind = 'apples' AND apples IS NOT NULL AND apples >= 1"
            " AND item_key IS NULL AND character_id IS NULL)"
            " OR (kind = 'item' AND item_key IS NOT NULL AND apples IS NULL"
            " AND character_id IS NULL)"
            " OR (kind = 'card' AND character_id IS NOT NULL AND apples IS NULL"
            " AND item_key IS NULL)",
            name="ck_shop_chest_rewards_shape",
        ),
        Index("ix_shop_chest_rewards_chest", "chest_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chest_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shop_chests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False)
    apples: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    item_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    character_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("characters.id", ondelete="RESTRICT"),
        nullable=True,
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class ShopChestOpening(Base, TimestampMixin):
    __tablename__ = "shop_chest_openings"
    __table_args__ = (
        UniqueConstraint("user_id", "operation_key", name="uq_shop_chest_openings_user_key"),
        CheckConstraint("price_apples >= 1", name="ck_shop_chest_openings_price_positive"),
        CheckConstraint("compensation_apples >= 0", name="ck_shop_chest_openings_comp_nonneg"),
        Index("ix_shop_chest_openings_chest", "chest_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chest_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shop_chests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Client-minted key for one purchase press; unique per user.
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    chest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    price_apples: Mapped[int] = mapped_column(Integer, nullable=False)
    # Snapshot of the reward: "apples" | "item" | "card".
    reward_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reward_apples: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reward_item_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reward_character_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Immutable reveal metadata, including on replay after a card is reclassified.
    reward_rarity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pity_guaranteed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    duplicate_converted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    compensation_apples: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Wallet balance right after this opening (set before commit).
    balance_after: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class ShopChestPity(Base):
    """Consecutive non-Epic/non-Legendary shop openings, shared across tiers."""

    __tablename__ = "shop_chest_pity"
    __table_args__ = (
        CheckConstraint("pity_counter >= 0", name="ck_shop_chest_pity_nonneg"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, autoincrement=False,
    )
    pity_counter: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
