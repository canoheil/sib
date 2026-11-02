"""Generic item inventory + data-driven drop tables (spec: extensible).

Items live in the ``user_items`` table (one row per user+key). Item
economy properties are CONFIG-DRIVEN (``ConfigService``):

- ``item_buy_prices``  — key -> price; presence means purchasable.
- ``item_sell_prices`` — key -> price; presence means sellable, except for
  permanently unsellable disease items.
- ``disease_antidote_shop_price`` — dedicated purchase price for the antidote.
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
from sib.services.lock_order import lock_user_row

# Display catalog for known items (emoji/label). Unknown keys still work
# (they fall back to the key itself) so new drops can be added via config
# before a catalog entry exists.
#
# NOTE: normal Apples (🍎) are the game's CURRENCY (apple_wallets) and are
# deliberately NOT an inventory item. Special Apples and Magic Powder are
# real inventory items (user_items). Prices are normally CONFIG-driven;
# the antidote uses its own setting and disease items are never sellable.
ITEM_CATALOG: Dict[str, Dict[str, str]] = {
    "magic_powder": {
        "emoji": "✨",
        "label": "پودر جادویی",
        "description": "پودری جادویی که درخت‌های باغ رو بلندتر و جادویی‌تر می‌کنه و شانس سیب‌های ویژه رو بالا می‌بره.",
    },
    "sib_buff": {
        "emoji": "💥",
        "label": "سیب باف",
        "description": "با خوردن این سیب، ضربه‌ی بعدی تو در تهاجم ۲ برابر آسیب می‌زند.",
    },
    "healing_potion": {
        "emoji": "🧪",
        "label": "معجون شفا",
        "description": "با نوشیدن این معجون، ۵۰ واحد از انرژی جنگی خود را در جنگ‌ها بازیابی می‌کنید.",
    },
    "divine_fertilizer": {
        "emoji": "🧪",
        "label": "کود الهی",
        "description": "با استفاده از این کود، یک قطعه زمین پژمرده دوباره سالم می‌شود.",
    },
    "wedding_ring": {
        "emoji": "💍",
        "label": "حلقه ازدواج",
        "description": "حلقه‌ای برای پیشنهاد ازدواج سیبی؛ تنها هنگام پذیرش پیشنهاد ازدواج مصرف می‌شه.",
    },
    "flower": {
        "emoji": "💐",
        "label": "گل",
        "description": "گلی برای هدیه دادن به همسر سیبی و افزایش میزان عشق (Love).",
    },
    "apple_gold": {
        "emoji": "🌕",
        "label": "سیب طلایی",
        "description": "سیبی کمیاب و درخشان از درخت‌های جادویی؛ تو فروشگاه به سیب معمولی تبدیل می‌شه.",
    },
    "apple_diamond": {
        "emoji": "💎",
        "label": "سیب الماسی",
        "description": "سیبی نایاب با درخشش الماس؛ از سیب طلایی هم ارزشمندتره.",
    },
    "apple_obsidian": {
        "emoji": "🌑",
        "label": "سیب ابسیدینی",
        "description": "افسانه‌ای‌ترین سیب باغ؛ نایاب‌ترین و گران‌بهاترین سیب ویژه.",
    },
    "pumpkin_seed": {
        "emoji": "🎃",
        "label": "دونه کدو",
        "description": "مهمات جادویی نبرد با سیبومورتیس؛ قابل فروش همیشگی در فروشگاه.",
    },
    "disease_plant": {
        "emoji": "🌿",
        "label": "گیاه ویژه",
        "description": "گیاهی کمیاب از معدن که برای ساخت پادزهر استفاده می‌شه.",
    },
    "disease_antidote": {
        "emoji": "💊",
        "label": "پادزهر آماده",
        "description": "پادزهری برای درمان بیماری؛ با `/use_antidote` مصرف می‌شه.",
    },
    "pet_food": {
        "emoji": "🍖",
        "label": "غذای پت",
        "description": "غذای خوشمزه‌ی پت‌های سیبی؛ با هر وعده به پت‌ت تجربه (XP) می‌رسه و کمکش می‌کنه بالغ بشه.",
    },
}

# These items may be transferred, but must never be sold back for Apples,
# even if an operator accidentally adds them to item_sell_prices.
NON_SELLABLE_ITEMS = frozenset({"disease_plant", "disease_antidote"})

# Items whose price comes from their OWN config setting instead of the
# ``item_buy_prices`` map, so they appear in the shop on installations whose
# stored ``item_buy_prices`` row predates the item.
DEDICATED_PRICE_ITEMS = ("disease_antidote", "pet_food")


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
            .execution_options(populate_existing=True)
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
    async def get_total(
        session: AsyncSession, user_id: int, item_key: str, for_update: bool = False
    ) -> int:
        """Owned quantity. ``for_update`` row-locks the inventory row so a
        multi-resource check-then-consume cannot race (card upgrades)."""
        if for_update:
            row = await session.scalar(
                select(UserItem)
                .where(UserItem.user_id == user_id)
                .where(UserItem.item_key == item_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            return int(row.quantity if row else 0)
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
        if item_key == "disease_antidote":
            raw = await ConfigService.get(session, "disease_antidote_shop_price")
            try:
                price = int(raw)
            except (TypeError, ValueError, OverflowError):
                price = int(ConfigService.default("disease_antidote_shop_price"))
            return price if price > 0 else None
        if item_key == "pet_food":
            # Dedicated setting, exactly like the antidote: pet_food arrived
            # after the item_buy_prices row was seeded on existing installs, so
            # the price must not depend on that stored JSON map containing the
            # new key. ``pet_food_price`` lives only in CONFIG_DEFAULTS (no seed
            # migration), and operators can override it at runtime.
            raw = await ConfigService.get(session, "pet_food_price")
            try:
                price = int(raw)
            except (TypeError, ValueError, OverflowError):
                price = int(ConfigService.default("pet_food_price"))
            return price if price > 0 else None
        prices = await ConfigService.get(session, "item_buy_prices") or {}
        price = prices.get(item_key)
        return int(price) if price is not None else None

    @classmethod
    async def buy_prices(cls, session: AsyncSession) -> Dict[str, int]:
        """Shop listing prices, including items with a dedicated setting."""
        prices = dict(await ConfigService.get(session, "item_buy_prices") or {})
        for key in DEDICATED_PRICE_ITEMS:
            price = await cls.buy_price(session, key)
            if price is None:
                prices.pop(key, None)
            else:
                prices[key] = price
        return prices

    @staticmethod
    async def sell_price(session: AsyncSession, item_key: str) -> Optional[int]:
        if item_key in NON_SELLABLE_ITEMS:
            return None
        prices = await ConfigService.get(session, "item_sell_prices") or {}
        price = prices.get(item_key)
        return int(price) if price is not None else None

    @staticmethod
    async def sell_prices(session: AsyncSession) -> Dict[str, int]:
        """All configured sell prices, excluding permanently unsellable items."""
        prices = await ConfigService.get(session, "item_sell_prices") or {}
        return {
            key: int(value) for key, value in prices.items()
            if key not in NON_SELLABLE_ITEMS
        }

    @classmethod
    async def sellable_inventory(
        cls, session: AsyncSession, user_id: int
    ) -> List[Tuple[str, int, int]]:
        """Owned sellable items as ``(item_key, quantity, unit_price)``.

        Sellability uses the same config and permanent exclusions as
        :meth:`sell`, so the Sell screen can never offer something the
        service layer would refuse — and the currency (normal Apples,
        which is a wallet balance and not an inventory row) can never
        appear here. Items with quantity 0 are omitted. Ordering follows
        ``ITEM_CATALOG`` for a stable UI.
        """
        prices = await cls.sell_prices(session)
        owned = await cls.list_items(session, user_id)
        ordered = [k for k in ITEM_CATALOG if k in prices]
        ordered += [k for k in prices if k not in ITEM_CATALOG]
        return [
            (key, owned[key], prices[key])
            for key in ordered
            if owned.get(key, 0) > 0
        ]

    @staticmethod
    async def asset_key(session: AsyncSession, item_key: str) -> str:
        """Media key of an item's existing artwork.

        Reuses the single item -> image mapping the garden celebration
        already uses (``garden_drop_entries[key]["asset"]``); the item key
        itself is the fallback so bundled ``assets/items/<key>.jpg`` or
        ``.png`` art keeps working for items without a drop entry.
        """
        entries = await ConfigService.get(session, "garden_drop_entries") or {}
        entry = entries.get(item_key) or {}
        return entry.get("asset") or item_key

    @staticmethod
    async def _lock_inventory_row(session: AsyncSession, user_id: int, item_key: str) -> None:
        """Lock (rank 5) the user's inventory row for ``item_key``, if it exists.

        Taken after the user row and before the wallet (rank 6).
        """
        await session.scalar(
            select(UserItem.id)
            .where(UserItem.user_id == user_id)
            .where(UserItem.item_key == item_key)
            .with_for_update()
        )

    @classmethod
    async def buy(
        cls,
        session: AsyncSession,
        user_id: int,
        item_key: str,
        quantity: int | str = 1,
    ) -> int:
        """Buy an item with normal Apples (atomic spend + grant).

        Repeatable purchase: every press is a real, paid transaction.
        Returns the new inventory total.

        Everything is validated BEFORE the debit: an unknown or unpriced item
        raises ``ValueError`` with no ledger write. Locks: user row, then the
        inventory row, then the wallet (lock policy ranks 0, 5, 6).
        """
        is_max = isinstance(quantity, str) and quantity.strip().lower() == "max"
        if not is_max:
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ValueError("تعداد باید حداقل ۱ باشد.")
        await lock_user_row(session, user_id)
        if item_key not in ITEM_CATALOG:
            raise ValueError("این آیتم در فروشگاه قابل خرید نیست.")
        price = await cls.buy_price(session, item_key)
        if price is None or price <= 0:
            raise ValueError("این آیتم در فروشگاه قابل خرید نیست.")
        await cls._lock_inventory_row(session, user_id, item_key)
        if is_max:
            user_balance = await AppleWalletService.get_balance(
                session, user_id, for_update=True
            )
            max_qty = user_balance // price
            if max_qty <= 0:
                raise ValueError("موجودی سیب شما کافی نیست!")
            qty = max_qty
        else:
            qty = int(quantity)
        total_cost = price * qty
        try:
            await AppleWalletService.spend(
                session,
                user_id,
                total_cost,
                reason="item_purchase",
                ref_type="user_item",
                ref_id=None,
            )
        except InsufficientFundsException:
            balance = await AppleWalletService.get_balance(session, user_id)
            max_possible = balance // price
            raise ValueError(
                f"سیب کافی نداری! شما فقط توانایی خرید {max_possible} عدد را دارید."
            )
        try:
            total = await cls.grant(session, user_id, item_key, qty)
        except Exception:
            await session.rollback()
            raise
        log_event(
            Events.ITEM_PURCHASED,
            {
                "user_id": user_id,
                "item_key": item_key,
                "quantity": qty,
                "price": total_cost,
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
        await lock_user_row(session, user_id)  # rank 0 before the inventory row
        price = await cls.sell_price(session, item_key)
        if price is None:
            raise ValueError("این آیتم قابل فروش نیست.")
        row = await session.scalar(
            select(UserItem)
            .where(UserItem.user_id == user_id)
            .where(UserItem.item_key == item_key)
            .with_for_update()
            .execution_options(populate_existing=True)
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
