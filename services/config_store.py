"""System configuration (data-driven game/economy settings).

Timings, prices and capacities must be tunable without code changes.
Rows are looked up by key with safe built-in defaults so the bot keeps
working even when a key is missing from the table.
"""

import json
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import SystemConfig


# Safe built-in defaults. Everything here is overridable via the
# system_config table and the admin panel.
CONFIG_DEFAULTS: dict[str, Any] = {
    # Garden
    "garden_initial_plots": 5,
    "garden_max_plots": 25,
    "garden_expansion_step": 5,
    "garden_expansion_cost_apples": 50,
    "garden_growth_seconds": 24 * 60 * 60,
    "garden_fruit_seconds": 12 * 60 * 60,
    "garden_fruit_amount": 3,
    # Card upgrades (base/step for the scaling formula; data-driven)
    "card_upgrade_base_cost": 100,
    "card_upgrade_cost_multiplier": 1.5,
    # Card level attribute budgets (sum of Power+Speed+Intelligence)
    "card_level_budgets": [60, 120, 180, 240, 300],
    # Apple Battle
    "apple_battle_winner_seedlings": 1,
    "apple_battle_queue_timeout_seconds": 600,
    # Weekly shop
    "shop_rotation_hours": 168,
    "shop_pool_character_ids": [],
}


class ConfigService:
    """Read/write access to runtime configuration with defaults."""

    @staticmethod
    async def get_raw(session: AsyncSession, key: str) -> Optional[str]:
        row = await session.get(SystemConfig, key)
        return row.value if row else None

    @classmethod
    async def get(cls, session: AsyncSession, key: str) -> Any:
        raw = await cls.get_raw(session, key)
        if raw is None:
            return CONFIG_DEFAULTS.get(key)
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    @classmethod
    async def get_int(cls, session: AsyncSession, key: str) -> int:
        return int(await cls.get(session, key))

    @classmethod
    async def set(cls, session: AsyncSession, key: str, value: Any) -> None:
        row = await session.get(SystemConfig, key)
        encoded = json.dumps(value, ensure_ascii=False)
        if row is None:
            session.add(SystemConfig(key=key, value=encoded))
        else:
            row.value = encoded
        await session.flush()

    @classmethod
    def default(cls, key: str) -> Any:
        return CONFIG_DEFAULTS.get(key)
