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
    "garden_max_plots": 10,
    # Per-plot unlock prices for plots 6..10 (index 0 = plot 6).
    "garden_plot_prices": [100, 250, 500, 1000, 2000],
    "garden_growth_seconds": 24 * 60 * 60,
    "garden_fruit_seconds": 12 * 60 * 60,
    "garden_fruit_amount": 3,
    # Magical tree progression (per-tree, Magic Powder driven).
    "garden_max_magic_level": 5,
    # Magic Powder units required for level 1..5 (index = current level).
    "magic_powder_level_costs": [1, 2, 3, 5, 8],
    # Magic Powder shop price (normal Apples).
    "magic_powder_buy_price": 40,
    # Card attribute budget at Level 1 (Power + Speed + Intelligence of every
    # card definition). Upgrades add +10 to each attribute per level; that rule
    # is global and lives in services/card_progression.py, not here.
    "card_base_attribute_budget": 60,
    # Card upgrade cost table: ONE row per upgrade step, index 0 = Level 1->2,
    # ... index 8 = Level 9->10. Every row costs normal Apples (wallet) plus
    # ONE special-apple material, following the Dark Souls 3 style of tiered
    # upgrade materials: Golden (early) -> Diamond (middle) -> Obsidian (late),
    # with the quantity rising inside each tier. Identical for every card.
    #
    # Balance basis (audited 2026-09-30 from the drop tables above; one harvest
    # roll per plot per 12h cycle, so a 5-plot garden rolls 10x/day, a 10-plot
    # garden 20x/day; a normal roll pays garden_fruit_amount=3 Apples):
    #   tree magic 1, 5 plots : ~2.5 gold, ~1.3 diamond, ~0.4 obsidian / day
    #   tree magic 5, 10 plots: ~6.8 gold, ~6.8 diamond, ~3.4 obsidian / day
    # Totals below (gold 16, diamond 19, obsidian 18, Apples 6200) are roughly
    # 6 + 15 + 45 days for an average 5-plot garden, and about 2-5 days per
    # tier for a fully invested garden - Level 10 is a long-term goal.
    # Sell values (gold 50 / diamond 100 / obsidian 200 Apples) keep the
    # per-step value strictly increasing, so every step feels bigger.
    # Rebalancing = edit this list (or override it in system_config); no code.
    "card_upgrade_costs": [
        {"apples": 100, "item": "apple_gold", "quantity": 3},       # 1 -> 2
        {"apples": 150, "item": "apple_gold", "quantity": 5},       # 2 -> 3
        {"apples": 250, "item": "apple_gold", "quantity": 8},       # 3 -> 4
        {"apples": 400, "item": "apple_diamond", "quantity": 4},    # 4 -> 5
        {"apples": 600, "item": "apple_diamond", "quantity": 6},    # 5 -> 6
        {"apples": 800, "item": "apple_diamond", "quantity": 9},    # 6 -> 7
        {"apples": 1000, "item": "apple_obsidian", "quantity": 4},  # 7 -> 8
        {"apples": 1300, "item": "apple_obsidian", "quantity": 6},  # 8 -> 9
        {"apples": 1600, "item": "apple_obsidian", "quantity": 8},  # 9 -> 10
    ],
    # Apple Battle
    "apple_battle_winner_seedlings": 1,
    "apple_battle_queue_timeout_seconds": 600,
    # Weekly shop
    "shop_rotation_hours": 168,
    "shop_pool_character_ids": [],
    # Generic item economy: presence in a price map == the property.
    # Buyable items (shop "Items" section): key -> buy price in Apples.
    "item_buy_prices": {"magic_powder": 40},
    # Sellable items (shop "Sell Items" section): key -> sell price in Apples.
    "item_sell_prices": {
        "apple_gold": 50,
        "apple_diamond": 100,
        "apple_obsidian": 200,
    },
    # Trading
    "trades_per_day": 1,
    "trade_expiration_hours": 24,
    # "سیب" group wake-word claim (spec: 5 apples every 5 minutes)
    "sib_claim_amount": 5,
    "sib_claim_cooldown_seconds": 5 * 60,
    # Harvest drop model (spec: special-apple drops).
    # Presentation per outcome key. "asset" references a bot_assets key
    # (or a bundled assets/items/<key>.jpg file) for celebration media.
    # Special-apple drops are a CELEBRATION: the image is sent with an
    # event-like caption that escalates with the drop's rarity
    # (Golden 20% < Diamond 9% < Obsidian 1%). Captions live here so they
    # stay configuration/localization data - never hardcoded in handlers.
    "garden_drop_entries": {
        "apple_normal": {
            "message": "🍎 سیب‌های معمولی برداشت شد.",
        },
        "apple_gold": {
            "message": "✨ شانست گرفت!\n🥇 یک سیب طلایی پیدا کردی!",
            "caption": (
                "🥇 **سیب طلایی!**\n"
                "شانس در باغت را زد؛ یک سیب طلاییِ درخشان برداشت کردی. ✨"
            ),
            "asset": "golden_apple",
        },
        "apple_diamond": {
            "message": "💎 درخششی عجیب...\nیک سیب الماسی پیدا کردی!",
            "caption": (
                "💎 **سیب الماسی!**\n"
                "این دیگر شانسِ ساده نیست؛ کمیاب است... از هر صد سیب، یکی الماسی "
                "می‌شود و امروز از باغ تو رویید. ✨"
            ),
            "asset": "diamond_apple",
        },
        "apple_obsidian": {
            "message": "🖤 باورکردنی نیست...\nیک سیب ابسیدینی پیدا کردی!",
            "caption": (
                "🖤 **سیب ابسیدینی!**\n"
                "افسانه می‌گوید از هر هزار سیب، تنها یکی ابسیدینی می‌شود...\n"
                "و امروز همان یکی، از باغ تو سر برآورد. این لحظه را نگه دار. 🌑"
            ),
            "asset": "obsidian_apple",
        },
    },
    # Relative weights per tree magic level ("0".."5"). Level 0 is the
    # base distribution 70/20/9/1; higher levels shift probability from
    # Normal to special Apples. Weights are RELATIVE (normalized) so a
    # row never has to sum to exactly 100.
    "garden_drop_weights": {
        "0": {"apple_normal": 70, "apple_gold": 20, "apple_diamond": 9, "apple_obsidian": 1},
        "1": {"apple_normal": 58, "apple_gold": 25, "apple_diamond": 13, "apple_obsidian": 4},
        "2": {"apple_normal": 46, "apple_gold": 29, "apple_diamond": 17, "apple_obsidian": 8},
        "3": {"apple_normal": 35, "apple_gold": 32, "apple_diamond": 22, "apple_obsidian": 11},
        "4": {"apple_normal": 25, "apple_gold": 33, "apple_diamond": 28, "apple_obsidian": 14},
        "5": {"apple_normal": 15, "apple_gold": 34, "apple_diamond": 34, "apple_obsidian": 17},
    },
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
