"""Generic item inventory + data-driven drop tables (spec: extensible).

Items live in the ``user_items`` table (one row per user+key). Item
economy properties are CONFIG-DRIVEN (``ConfigService``):

- ``item_buy_prices``  — key -> price; presence means purchasable.
- ``item_sell_prices`` — key -> price; presence means sellable.
- ``garden_drop_entries``  — per-key presentation (message/asset).
- ``garden_drop_weights``  — per-magic-level relative drop weights.

Adding a new item = one ``ITEM_CATALOG`` display entry + one config price;
no business-logic change. Normal Apples are the CURRENCY (wallet), never
an inventory item.
"""

import random
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event
from sib.database.models import UserItem
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.config_store import ConfigService

# Display catalog for known items (emoji/label). Unknown keys still work
# (they fall back to the key itself) so new drops can be added via config
# before a catalog entry exists.
#
# NOTE: normal Apples (🍎) are the game's CURRENCY (apple_wallets) and are
# deliberately NOT an inventory item. Special Apples and Magic Powder are
# real inventory items (user_items). Buyability/sellability and prices are
# CONFIG-driven (item_buy_prices / item_sell_prices), not catalog flags.
ITEM_CATALOG: Dict[str, Dict[str, str]] = {
    "magic_powder": {
        "emoji": "✨",
        "label": "پودر جادویی",
        "description": "پودری جادویی که درختان باغ را بلندتر و جادویی‌تر می‌کند و شانس سیب‌های ویژه را بالا می‌برد.",
    },
    "apple_gold": {
        "emoji": "🥇",
        "label": "سیب طلایی",
        "description": "سیبی کمیاب و درخشان از درخت‌های جادویی؛ در فروشگاه به سیب معمولی تبدیل می‌شود.",
    },
    "apple_diamond": {
        "emoji": "💎",
        "label": "سیب الماسی",
        "description": "سیبی نایاب با درخشش الماس؛ ارزشمندتر از سیب طلایی است.",
    },
    "apple_obsidian": {
        "emoji": "🖤",
        "label": "سیب ابسیدینی",
        "description": "افسانه‌ای‌ترین سیب باغ؛ نایاب‌ترین و گران‌بهاترین سیب ویژه.",
    },
}


def item_description(key: str) -> str:
    meta = ITEM_CATALOG.get(key)
    return meta.get("description", "") if meta else ""


def item_label(key: str) -> str:
    meta = ITEM_CATALOG.get(key)
    return meta["label"] if meta else key


def item_emoji(key: str) -> str:
    meta = ITEM_CATALOG.get(key)
    return meta["emoji"] if meta else "🔹"


class ItemService:
    """Grant/read generic inventory items (auditable, restart-safe)."""

    @staticmethod
    async def grant(
        session: AsyncSession, user_id: int, item_key: str, quantity: int = 1
    ) -> int:
        """Add quantity of an item; returns the new total for that item."""
        if item_key not in ITEM_CATALOG:
            raise ValueError(f"unknown item_key: {item_key}")
        row = await session.scalar(
            select(UserItem)
            .where(UserItem.user_id == user_id)
            .where(UserItem.item_key == item_key)
            .with_for_update()
        )
        if row is None:
            row = UserItem(user_id=user_id, item_key=item_key, quantity=0)
            session.add(row)
        row.quantity += quantity
        await session.flush()
        log_event(
            Events.ITEM_GRANTED,
            {
                "user_id": user_id,
                "item_key": item_key,
                "quantity": quantity,
                "total": row.quantity,
            },
        )
        return row.quantity

    @staticmethod
    async def get_total(session: AsyncSession, user_id: int, item_key: str) -> int:
        row = await session.scalar(
            select(UserItem.quantity)
            .where(UserItem.user_id == user_id)
            .where(UserItem.item_key == item_key)
        )
        return int(row or 0)

    @staticmethod
    async def list_items(session: AsyncSession, user_id: int) -> Dict[str, int]:
        rows = (
            await session.execute(
                select(UserItem.item_key, UserItem.quantity).where(
                    UserItem.user_id == user_id
                )
            )
        ).all()
        return {key: int(qty) for key, qty in rows if qty > 0}

    @staticmethod
    async def summary_line(session: AsyncSession, user_id: int) -> str:
        items = await ItemService.list_items(session, user_id)
        if not items:
            return "—"
        parts = [
            f"{item_emoji(key)}×{items[key]}"
            for key in ITEM_CATALOG
            if key in items
        ]
        parts += [
            f"{item_emoji(key)}×{qty}"
            for key, qty in items.items()
            if key not in ITEM_CATALOG
        ]
        return " · ".join(parts)

    # ------------------------------------------------------------------
    # Consumption (Magic Powder etc.)
    # ------------------------------------------------------------------
    @staticmethod
    async def consume(
        session: AsyncSession, user_id: int, item_key: str, quantity: int = 1
    ) -> int:
        """Remove quantity of an item; returns the remaining total.

        Row-locked so concurrent uses can never overspend the inventory.
        """
        if quantity < 1:
            raise ValueError("تعداد باید حداقل ۱ باشد.")
        row = await session.scalar(
            select(UserItem)
            .where(UserItem.user_id == user_id)
            .where(UserItem.item_key == item_key)
            .with_for_update()
        )
        have = row.quantity if row else 0
        if have < quantity:
            raise ValueError(
                f"موجودی {item_emoji(item_key)} «{item_label(item_key)}» کافی نیست! "
                f"(لازم: {quantity}، موجود: {have})"
            )
        row.quantity -= quantity
        await session.flush()
        log_event(
            Events.ITEM_CONSUMED,
            {
                "user_id": user_id,
                "item_key": item_key,
                "quantity": quantity,
                "total": row.quantity,
            },
        )
        return row.quantity

    # ------------------------------------------------------------------
    # Shop: buy / sell (prices are config-driven)
    # ------------------------------------------------------------------
    @staticmethod
    async def buy_price(session: AsyncSession, item_key: str) -> Optional[int]:
        prices = await ConfigService.get(session, "item_buy_prices") or {}
        price = prices.get(item_key)
        return int(price) if price is not None else None

    @staticmethod
    async def sell_price(session: AsyncSession, item_key: str) -> Optional[int]:
        prices = await ConfigService.get(session, "item_sell_prices") or {}
        price = prices.get(item_key)
        return int(price) if price is not None else None

    @classmethod
    async def buy(cls, session: AsyncSession, user_id: int, item_key: str, quantity: int = 1) -> int:
        """Buy an item with normal Apples (atomic spend + grant).

        Repeatable purchase: every press is a real, paid transaction.
        Returns the new inventory total.
        """
        if quantity < 1:
            raise ValueError("تعداد باید حداقل ۱ باشد.")
        price = await cls.buy_price(session, item_key)
        if price is None:
            raise ValueError("این آیتم در فروشگاه قابل خرید نیست.")
        try:
            await AppleWalletService.spend(
                session,
                user_id,
                price * quantity,
                reason="item_purchase",
                ref_type="user_item",
                ref_id=None,
            )
        except InsufficientFundsException:
            raise ValueError(
                f"سیب کافی نیست! قیمت: {price * quantity} سیب 🍎"
            )
        total = await cls.grant(session, user_id, item_key, quantity)
        log_event(
            Events.ITEM_PURCHASED,
            {
                "user_id": user_id,
                "item_key": item_key,
                "quantity": quantity,
                "price": price * quantity,
                "total": total,
            },
        )
        return total

    @classmethod
    async def sell(cls, session: AsyncSession, user_id: int, item_key: str, quantity: int = 1) -> int:
        """Sell items back to the shop for normal Apples.

        Only items with a configured sell price are sellable. Returns the
        number of Apples credited.
        """
        if quantity < 1:
            raise ValueError("تعداد باید حداقل ۱ باشد.")
        price = await cls.sell_price(session, item_key)
        if price is None:
            raise ValueError("این آیتم قابل فروش نیست.")
        row = await session.scalar(
            select(UserItem)
            .where(UserItem.user_id == user_id)
            .where(UserItem.item_key == item_key)
            .with_for_update()
        )
        have = row.quantity if row else 0
        if have < quantity:
            raise ValueError(
                f"موجودی {item_emoji(item_key)} «{item_label(item_key)}» کافی نیست! "
                f"(لازم: {quantity}، موجود: {have})"
            )
        row.quantity -= quantity
        await session.flush()
        await AppleWalletService.earn(
            session,
            user_id,
            price * quantity,
            reason="item_sold",
            ref_type="user_item",
            ref_id=row.id,
        )
        log_event(
            Events.ITEM_SOLD,
            {
                "user_id": user_id,
                "item_key": item_key,
                "quantity": quantity,
                "apples": price * quantity,
                "remaining": row.quantity,
            },
        )
        return price * quantity


def roll_drop(
    drop_table: Optional[Dict[str, Any]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[Tuple[str, Any]]:
    """Weighted roll over ``{key: weight}`` (weights relative, normalized).

    Pure function - injectable RNG for tests. Returns ``(key, weight)``
    or None for an empty table.
    """
    table = drop_table if drop_table is not None else {}
    if not table:
        return None
    rng = rng or random
    keys = list(table.keys())
    weights = [max(0.0, float(table[k])) for k in keys]
    total = sum(weights)
    if total <= 0:
        return None
    pick = rng.random() * total
    cumulative = 0.0
    for key, weight in zip(keys, weights):
        cumulative += weight
        if pick < cumulative:
            return key, weight
    return keys[-1], weights[-1]


class DropService:
    """Harvest drops backed by config: presentation + per-level weights."""

    @staticmethod
    async def entries(session: AsyncSession) -> Dict[str, Dict[str, Any]]:
        """Presentation per outcome key (message/asset)."""
        return await ConfigService.get(session, "garden_drop_entries") or {}

    @classmethod
    async def weights_for_level(cls, session: AsyncSession, magic_level: int) -> Dict[str, float]:
        """Relative drop weights for a tree's magic level.

        The level is clamped to 0..garden_max_magic_level and unknown keys
        are filtered out, so a partially-configured table can never break
        a harvest.
        """
        max_level = int(
            await ConfigService.get(session, "garden_max_magic_level") or 5
        )
        level = max(0, min(int(magic_level or 0), max_level))
        table = await ConfigService.get(session, "garden_drop_weights") or {}
        row = table.get(str(level)) or table.get(level) or table.get("0") or {}
        known = set(ITEM_CATALOG) | {"apple_normal"}
        return {k: float(v) for k, v in row.items() if k in known and float(v) > 0}

    @classmethod
    async def roll(
        cls,
        session: AsyncSession,
        magic_level: int,
        rng: Optional[random.Random] = None,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Roll the level's table; returns (key, presentation entry)."""
        weights = await cls.weights_for_level(session, magic_level)
        rolled = roll_drop(weights, rng=rng)
        if rolled is None:
            return None
        key = rolled[0]
        entries = await cls.entries(session)
        return key, entries.get(key) or {}
