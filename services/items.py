"""Generic item inventory + data-driven drop tables (spec: extensible).

Items live in the ``user_items`` table (one row per user+key). The drop
table is CONFIG-DRIVEN (``ConfigService`` key ``garden_harvest_drops``):
adding a new drop is a config edit (or SystemConfig row) - the harvest
logic never changes. Weights are RELATIVE (they are normalized) so they
do not have to sum to exactly 100.
"""

import random
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event
from sib.database.models import UserItem
from sib.services.config_store import ConfigService

# Display catalog for known items (emoji/label). Unknown keys still work
# (they fall back to the key itself) so new drops can be added via config
# before a catalog entry exists.
ITEM_CATALOG: Dict[str, Dict[str, str]] = {
    "apple_normal": {"emoji": "🍎", "label": "سیب معمولی"},
    "apple_gold": {"emoji": "🥇", "label": "سیب طلایی"},
    "apple_obsidian": {"emoji": "🖤", "label": "سیب ابسیدینی"},
}

DEFAULT_DROP_TABLE: List[Dict[str, Any]] = [
    {
        "key": "apple_normal",
        "weight": 94,
        "message": "🍎 یک سیب معمولی از درخت افتاد.",
    },
    {
        "key": "apple_gold",
        "weight": 5,
        "message": "✨ شانست گرفت!\n🥇 یک سیب طلایی پیدا کردی!",
    },
    {
        "key": "apple_obsidian",
        "weight": 1,
        "message": "🖤 باورکردنی نیست...\nیک سیب ابسیدینی پیدا کردی!",
    },
]


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


def roll_drop(
    drop_table: Optional[List[Dict[str, Any]]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """Weighted drop roll. Weights are relative (normalized internally).

    Returns the chosen drop entry or None for an empty table. Pure
    function - injectable RNG for tests.
    """
    table = drop_table if drop_table is not None else DEFAULT_DROP_TABLE
    if not table:
        return None
    rng = rng or random
    weights = [max(0, float(entry.get("weight", 0))) for entry in table]
    total = sum(weights)
    if total <= 0:
        return None
    pick = rng.random() * total
    cumulative = 0.0
    for entry, weight in zip(table, weights):
        cumulative += weight
        if pick < cumulative:
            return entry
    return table[-1]


class DropService:
    """Harvest drop rolling backed by config (spec #9/#12)."""

    @staticmethod
    async def get_table(session: AsyncSession) -> List[Dict[str, Any]]:
        table = await ConfigService.get(session, "garden_harvest_drops")
        if not table:
            return DEFAULT_DROP_TABLE
        return table

    @staticmethod
    async def roll_harvest_drop(
        session: AsyncSession,
        user_id: int,
        rng: Optional[random.Random] = None,
    ) -> Optional[Dict[str, Any]]:
        """Roll the configured drop table and persist the item.

        Returns the chosen drop entry (for messaging) or None.
        """
        table = await DropService.get_table(session)
        entry = roll_drop(table, rng=rng)
        if not entry:
            return None
        await ItemService.grant(session, user_id, entry["key"], quantity=1)
        return entry
