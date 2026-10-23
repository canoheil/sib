"""System configuration (data-driven game/economy settings).

Timings, prices and capacities must be tunable without code changes.
Rows are looked up by key with safe built-in defaults so the bot keeps
working even when a key is missing from the table.
"""

import json
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import SystemConfig


# Invasion v2 defaults; database/admin overrides remain authoritative.
DEFAULT_INVASION_DIFFICULTY_MULTIPLIER = 2.0
DEFAULT_INVASION_JOIN_WINDOW_SECONDS = 1800
# Legacy "every N seconds since last spawn" cadence. No longer read by the
# scheduler (see invasion_spawn_hours below), kept only for backward
# compatibility with any external tooling/admin display that still reads it.
DEFAULT_INVASION_INTERVAL_SECONDS = 14400
# Fixed daily spawn schedule (Asia/Tehran local hours), comma-separated.
DEFAULT_INVASION_SPAWN_HOURS = "0,12,16,20"
# A tick counts as "in slot H" while local minute < this value.
DEFAULT_INVASION_SLOT_WINDOW_MINUTES = "30"


# Safe built-in defaults. Everything here is overridable via the
# system_config table and the admin panel.
CONFIG_DEFAULTS: dict[str, Any] = {
    "invasion_difficulty_multiplier": DEFAULT_INVASION_DIFFICULTY_MULTIPLIER,
    "invasion_join_window_seconds": DEFAULT_INVASION_JOIN_WINDOW_SECONDS,
    "invasion_interval_seconds": DEFAULT_INVASION_INTERVAL_SECONDS,
    # Fixed daily spawn schedule, Asia/Tehran local time (replaces the
    # interval-based cadence above; see services/invasion_v2/scheduler.py).
    "invasion_spawn_hours": DEFAULT_INVASION_SPAWN_HOURS,
    "invasion_slot_window_minutes": DEFAULT_INVASION_SLOT_WINDOW_MINUTES,
    # IV3 random events inside an active invasion (docs/invasion-rework/
    # IV3-RECON.md). Seeded OFF by migration 055: the master
    # ``invasion_enabled`` flag still gates everything, and an operator turns
    # this on without a deploy once the staging run is clean.
    "inv2_events_enabled": False,
    # Never fire in the first minute after the announcement settles, nor in
    # the last two minutes before the deadline (a 2-minute buff would be
    # wasted there).
    "inv2_event_guard_head_seconds": 60,
    "inv2_event_guard_tail_seconds": 120,
    # Cadence guard rails, honoured by the jitter in event_scheduler.
    "inv2_event_min_gap_seconds": 300,
    "inv2_event_max_gap_seconds": 420,
    "inv2_event_min_per_invasion": 3,
    "inv2_event_max_per_invasion": 7,
    # Chest window and "first N" limit.
    "inv2_event_loot_window_seconds": 120,
    "inv2_event_loot_max_claims": 3,
    "inv2_event_loot_apples": 40,
    # Optional per-template weight overrides (code -> weight); 0 disables.
    "inv2_event_weights": {},
    # Days after which a group member is considered inactive for inv2 power.
    "inv2_member_active_window_days": 30,
    # Minimum number of active group members who must have a main card for an
    # invasion v2 to spawn in that group. Members of the window without a main
    # card are ignored, and the average is taken over the ones who have one.
    "invasion_min_players_with_card": 3,
    # Garden
    "garden_initial_plots": 5,
    "garden_max_plots": 10,
    # Per-plot unlock prices for plots 6..10 (index 0 = plot 6).
    "garden_plot_prices": [100, 250, 500, 1000, 2000],
    # G2 36-hour fix: growth 12h + fruit 12h => the FIRST fruit is ready
    # exactly 24h after planting (was 24h+12h=36h). Repeating harvest cycles
    # stay at garden_fruit_seconds (12h), unchanged.
    "garden_growth_seconds": 12 * 60 * 60,
    "garden_fruit_seconds": 12 * 60 * 60,
    "garden_fruit_amount": 3,
    # G4.5: offline grace period for garden withering (hours), tunable via
    # system_config. Default 4h so sleeping/working players are protected.
    "garden_wither_grace_hours": 4,
    # Magical tree progression (per-tree, Magic Powder driven).
    "garden_max_magic_level": 5,
    # Magic Powder units required for level 1..5 (index = current level).
    "magic_powder_level_costs": [1, 2, 3, 5, 8],
    # Magic Powder shop price (normal Apples).
    "magic_powder_buy_price": 40,
    # NOTE: the Level-1 card attribute budget (Power + Speed + Intelligence
    # == 60 for every character definition) is a PERMANENT GAME RULE, not
    # configuration: it lives in ``services/card_progression.py``
    # (``CARD_BASE_ATTRIBUTE_BUDGET``) together with the +10-per-level step
    # and is enforced by ``CharacterService.validate_base_budget``. The old
    # ``card_base_attribute_budget`` key was removed on purpose - adding it
    # back here or in ``system_config`` has no effect.
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
    # Apple Mine pickaxe progression (M2-RECON, docs/games/M2-RECON.md).
    #
    # Balance basis: one 30-minute shift per player; a dedicated miner works
    # 8 shifts/day. EV per shift counts special apples at their sell value
    # (gold 50 / diamond 100 / obsidian 200): L1 ≈ 25.8, L5 ≈ 79.3 Apples,
    # so the ladder (300/600/1000/1500 = 3400 total, ~55% of the card-
    # upgrade ladder) pays back in ~9-10 days for a dedicated miner and
    # ~2-3 weeks for a casual one. Level 1 is deliberately below the old M1
    # flat table (≈64.5/shift): the pickaxe is the progression hook.
    #
    # ``pickaxe_upgrade_costs``: index i = normal-Apple cost to ENTER level
    # i+1 (index 0 = level 1, always free). Rebalancing = edit this list
    # (or override it in system_config); no code.
    "pickaxe_upgrade_costs": [0, 300, 600, 1000, 1500],
    # ``pickaxe_rewards``: index i = the reward table of level i+1. A shift
    # rolls the special chances first (gold/diamond/obsidian, mutually
    # exclusive, percentages of 100; fixed drop quantities 3/1/1) and
    # otherwise grants a uniform random number of normal Apples between
    # ``min`` and ``max``.
    "pickaxe_rewards": [
        {"min": 10, "max": 15, "gold": 7, "diamond": 2, "obsidian": 1},
        {"min": 15, "max": 22, "gold": 10, "diamond": 3, "obsidian": 1},
        {"min": 21, "max": 30, "gold": 13, "diamond": 4, "obsidian": 2},
        {"min": 28, "max": 40, "gold": 16, "diamond": 5, "obsidian": 2},
        {"min": 45, "max": 60, "gold": 20, "diamond": 6, "obsidian": 3},
    ],
    # Apple Battle
    "apple_battle_winner_seedlings": 1,
    "apple_battle_queue_timeout_seconds": 600,
    # Weekly shop
    "shop_rotation_hours": 168,
    "shop_pool_character_ids": [],
    # Generic item economy: presence in a price map == the property.
    #
    # Marriage & Love pricing (audited 2026-10-04 against the economy below):
    #   * ``wedding_ring`` 1000 - a deliberate GATE, not a first-day buy.
    #     Income baseline: a casual player earns ~85-135 Apples/day (garden
    #     3 x 5 plots x 2 cycles = 30, plus the «سیب» claim at 5 per 5 min),
    #     an invested one ~400-1700/day once special-apple drops are sold at
    #     50 / 100 / 200. So the Ring is roughly 8-12 days of casual play but
    #     only ~1 day for a fully invested garden - it discourages effortless
    #     first-day marriages without walling the feature off.
    #     Anchors: Magic Powder 40, costliest plot 2000, one late card-upgrade
    #     step 1600, whole card-upgrade ladder 6200.
    #   * ``flower`` 30 - RECURRING spend (``flower_daily_limit_per_direction``
    #     is 1 per direction per UTC day), so it must stay payable from a
    #     single day's income for every married player: ~25-35% of a casual
    #     day, ~2% of an invested one. It sits BELOW Magic Powder (40) because
    #     Powder is permanent progression while a Flower is consumed on send.
    #     The Ring is ~33x a Flower, so Love stays affordable to nurture.
    #   * Sell prices are the configured ``item_sell_price_ratio`` (0.5)
    #     applied to the buy price: 1000 -> 500 and 30 -> 15. Rebalancing =
    #     edit these two maps; no code change and no new config key.
    #
    # Adding an item to the shop = one `ITEM_CATALOG` entry + one price here.
    # The marriage foundation's `wedding_ring_buy_price` / `flower_buy_price`
    # keys deliberately stay unset: these two maps are the SINGLE source of
    # truth the shop and `ItemService` read, so a second per-item price would
    # only drift out of sync.
    # Buyable items (shop "Items" section): key -> buy price in Apples.
    "item_buy_prices": {
        "magic_powder": 40,
        "sib_buff": 50,
        "wedding_ring": 1000,
        "flower": 30,
        "healing_potion": 150,
        # G2: restores ONE withered garden plot (buy-only remedy; it is
        # deliberately absent from item_sell_prices like magic_powder).
        "divine_fertilizer": 80,
    },
    # Sellable items (shop "Sell Items" section): key -> sell price in Apples.
    # Magic Powder is deliberately absent: it is consumable progression, not a
    # currency, so it can be bought but never sold back (see test_item_economy).
    "item_sell_prices": {
        "apple_gold": 50,
        "apple_diamond": 100,
        "apple_obsidian": 200,
        "wedding_ring": 500,
        "flower": 15,
        "pumpkin_seed": 15,
    },
    # Halloween global world-boss event («سیبومورتیس» / Sibomortis, HW1-RECON).
    "halloween_enabled": False,
    "halloween_default_max_hp": 500_000,
    "halloween_duration_days": 7,
    "halloween_claim_grace_hours": 48,
    "halloween_loot_cooldown_seconds": 14_400,
    "halloween_day7_hp_retention_ratio": 0.20,
    "halloween_day7_damage_multiplier": 2.0,
    "halloween_group_drop_interval_seconds": 10_800,
    "halloween_group_drop_window_seconds": 300,
    "halloween_group_drop_max_claims": 3,
    # Friendly matches ("بازی با اعضای گروه"): Apple reward instead of the
    # competitive seedling. ALL THREE values are independent limits:
    #   * at most `friendly_reward_max_matches_per_day` reward-eligible
    #     friendly matches per player per day,
    #   * `friendly_reward_apples_per_match` Apples per eligible completed
    #     match,
    #   * at most `friendly_reward_daily_apple_cap` Apples per player per day.
    # The reward is ledger-idempotent (one entry per match+player), so double
    # callbacks/retries can never pay twice. Rebalancing = edit these values.
    "friendly_reward_max_matches_per_day": 5,
    "friendly_reward_apples_per_match": 10,
    "friendly_reward_daily_apple_cap": 50,
    # Invasion settlement: each DEFENDER (a participant with at least one
    # persisted successful defense action in that Invasion) receives the
    # outcome-based amount of real Golden Apple inventory items (apple_gold).
    # Joining alone never qualifies. Amounts are read at settlement time and
    # are paid exactly once per Invasion.
    "invasion_reward_total_victory_golden_apples": 10,
    "invasion_reward_narrow_defense_golden_apples": 5,
    "invasion_reward_defeat_golden_apples": 1,
    # Invasion scheduling. Intervals are UTC-slot aligned: the default 4-hour
    # cadence yields six occurrences per active Group per day (00:00, 04:00,
    # 08:00, 12:00, 16:00, 20:00 UTC). An occurrence may activate only inside
    # its first battle window [slot start, slot start + battle window); it is
    # never announced before activation and missed slots are never backfilled.
    # Battle parameters are snapshotted onto each Invasion when it activates,
    # so a later config change never rewrites a battle already in progress.
    "invasion_occurrence_interval_seconds": 4 * 60 * 60,
    # Superseded by invasion_wave_count x invasion_wave_duration_seconds for
    # new battles. Kept so existing rows stay valid; legacy 2-wave events keep
    # their own snapshot and do not read this key.
    "invasion_event_duration_seconds": 20 * 60,
    # Four-wave battle timing: every wave lasts wave_duration seconds, measured
    # from the battle's actual activation time (a 20-minute battle by default).
    "invasion_wave_count": 4,
    "invasion_wave_duration_seconds": 5 * 60,
    # Enemy strength: base + min(Group matches played, activity cap), scaled
    # by wave_count / 2 so each wave keeps the strength of a legacy wave.
    "invasion_enemy_base_power": 100,
    "invasion_enemy_activity_bonus_cap": 100,
    # Downtime recovery budget per battle. A bot stall longer than the 30 s
    # heartbeat tolerance is compensated by extending the active wave, but the
    # total compensation for one battle may not exceed this many seconds. A
    # stall that would exceed it finalizes the battle as interrupted (no
    # outcome, no reward). Snapshotted per battle at activation.
    "invasion_downtime_compensation_max_seconds": 20 * 60,
    # Trading
    "trades_per_day": 1,
    "trade_expiration_hours": 24,
    # Marriage & Love system (foundation)
    "marriage_proposal_timeout_seconds": 300,
    "marriage_divorce_cooldown_seconds": 86400,
    "love_min": 0,
    "love_max": 100,
    "love_initial": 0,
    "flower_reject_love_penalty": 1,
    "love_daily_decay": 1,
    "flower_daily_limit_per_direction": 1,
    # Economy-dependent Marriage & Love keys: explicitly registered as
    # configurable keys (`None` until finalized in the economy review phase;
    # `item_buy_prices` / `item_sell_prices` remain unchanged for Magic Powder).
    #
    # NOTE: `flower_love_gain` was finalized by the economy review and now
    # lives in the "Sib Love economy" block below with its balance basis.
    "wedding_ring_buy_price": None,
    "flower_buy_price": None,
    "item_sell_price_ratio": 0.5,
    # Sib Love economy (finalized 2026-10-04 for the Flower UI).
    #
    # Love earned per ACCEPTED Flower, on the shared 0..100 Love scale
    # (`love_min` / `love_max`). Chosen against the configured relationship
    # pacing: `love_daily_decay` 1/day, `flower_reject_love_penalty` 1,
    # `flower_daily_limit_per_direction` 1 (A->B and B->A are independent,
    # so an engaged couple resolves up to 2 Flowers per day).
    #
    #   net Love/day = (accepted Flowers/day * gain) - love_daily_decay
    #
    #   gain  1 spouse sends    both spouses send   verdict
    #   ----  ----------------  -----------------   --------------------------
    #     1     +0/day  (never)   +1/day -> 100d    BROKEN: decay cancels a
    #                                               lone sender entirely, so
    #                                               Love could never grow
    #                                               without perfect 2-sided
    #                                               play, and one rejection
    #                                               (-1) would erase a whole
    #                                               accepted Flower.
    #     2     +1/day -> 100d    +3/day -> ~33d    CHOSEN: smallest value
    #                                               that still lets a single
    #                                               active partner make
    #                                               progress; ~1 month of
    #                                               daily play for a couple.
    #     3     +2/day -> 50d     +5/day -> 20d     noticeably faster, and a
    #                                               rejection starts to sting
    #                                               too little.
    #     5     +4/day -> 25d     +9/day -> 11d     too fast to feel earned.
    #    10     +9/day -> 11d    +19/day -> 5d      100 is meaningless.
    #
    # Why 2 specifically:
    #   * It is the floor at which the feature still works one-sided, so a
    #     couple with one keen partner is not hard-blocked.
    #   * Rejection (-1) costs exactly half an accepted Flower and one day
    #     of solo progress: a real but recoverable setback, symmetric with
    #     the daily decay of 1.
    #   * Cost of maxing Love: ~33-100 days x 30-60 Apples/day (Flower buy
    #     price 30) = roughly 1000-3000 Apples for the couple - the same
    #     order as the Wedding Ring (1000). The sustained relationship is
    #     worth about as much as the marriage itself, and the Flower stays
    #     a recurring sink instead of a one-off toll.
    #   * The gain is snapshotted onto each FlowerGift (`love_delta`) at SEND
    #     time, so rebalancing here never rewrites offers already in flight.
    "flower_love_gain": 2,
    # Lifetime of a PENDING FlowerGift. A gift nobody accepts or rejects is
    # expired by MaintenanceService (periodic) and RecoveryService (startup)
    # once `created_at + this many seconds` has passed:
    #
    #   * the gift transitions PENDING -> EXPIRED (CAS-guarded, so a
    #     concurrent accept/reject/invalidate always wins instead of
    #     double-refunding);
    #   * exactly one Flower is refunded to the sender;
    #   * Love is NEVER changed (`love_delta` stays 0) - expiry is a return of
    #     the spent item, not a relationship event;
    #   * already ACCEPTED / REJECTED / INVALIDATED gifts are never touched.
    #
    # 300s matches `marriage_proposal_timeout_seconds`: an unanswered Flower
    # card is the same shape of interaction as an unanswered proposal card (a
    # group card with «قبول / رد» buttons waiting on one person), so both
    # windows are deliberately identical. Because expiry REFUNDS the Flower,
    # a short window is the sender-safe choice: the item comes back instead
    # of staying locked inside a card nobody is looking at any more. The
    # lifetime is derived from `created_at` at sweep time, so rebalancing it
    # never needs a migration and never rewrites offers already in flight.
    "flower_gift_pending_timeout_seconds": 300,
    # "سیب" group wake-word claim (N7 spec: a RANDOM 5–10 apples every 5
    # minutes). The reward is no longer a fixed configured value - see
    # sib_claim.roll_claim_amount; `sib_claim_amount` is kept for stored-
    # config compatibility and is no longer read. The cooldown is fixed.
    "sib_claim_amount": 5,
    "sib_claim_cooldown_seconds": 5 * 60,
    # "بغل سیبی" (Sib Hug): both participants receive the amount; only the
    # sender's successful hugs start the cooldown (see services/sib_hug.py).
    "sib_hug_amount": 10,
    "sib_hug_cooldown_seconds": 10 * 60,
    # Friendship Bonus (SOC1): when the two participants already have an
    # ACCEPTED apple_friendships row, this bonus is ADDED on top of the base
    # amount for BOTH of them - so the default pair 10 + 10 pays 20 each.
    # Additive (not a replacement amount) so rebalancing the base reward keeps
    # the bonus meaningful, and tunable here without a deploy or migration.
    "sib_hug_friend_bonus": 10,
    # "بوس سیبی" (Sib Kiss): independent of hug, claim, and received kisses.
    "sib_kiss_amount": 10,
    "sib_kiss_cooldown_seconds": 10 * 60,
    # Friendship Bonus for «بوس سیبی», same rule as the hug bonus above.
    "sib_kiss_friend_bonus": 10,
    # Daily Sib Treasure Chest («صندوقچه گنج روزانه»): one claim per player
    # per UTC calendar day (the established daily boundary: trades_per_day,
    # flower_daily_limit_per_direction and the XP daily caps all reset at
    # UTC midnight). The claim is a DB row keyed (user_id, claim_day).
    #
    # Reward pool — RELATIVE WEIGHTS, normalized at roll time (validated
    # first: known keys only, finite weights >= 0, total > 0). The default
    # row is in rising rarity order and sums to 100, so each weight is the
    # probability in percent:
    #
    #   apples       70.00 %   common outcome, 15-25 Apples (see below)
    #   seedlings    20.00 %   1-2 seedlings (one match win = 1 seedling)
    #   apple_gold    6.00 %   1-2 Golden Apples
    #   apple_diamond 2.50 %   1 Diamond Apple
    #   apple_obsidian 1.45 %  1 Obsidian Apple
    #   card          0.05 %   1 collectible card — the JACKPOT (1 in 2,000)
    #
    # Balance basis (audited 2026-10-08 against the economy blocks below):
    #   * apples 15-25 (avg 20) is one «سیب» wake-word claim (5 per 5 min)
    #     to a third of a casual day (85-135/day): a welcome bonus, never
    #     the main income. Pays through AppleWalletService with a
    #     per-(user, day) idempotency key and a reason='daily_chest' ledger
    #     entry.
    #   * seedlings 1-2 matches one eligible group-game win (exactly 1).
    #   * Golden 1-2 / Diamond 1 / Obsidian 1 sit just under a single
    #     garden drop unit; sell values 50/100/200 Apples keep the special
    #     apples' per-tier upgrade ladder pacing intact.
    #   * card weight 0.05 (0.05% = 1 in 2,000 chests) is deliberately
    #     EXTREMELY low: cards are the collection jackpot also served by
    #     the weekly shop (one global price: SHOP_CARD_PRICE_APPLES) and
    #     one-trade-per-day trading,
    #     and a free daily card source must not erode that scarcity. It is
    #     ~29x rarer than the chest's own Obsidian Apple and rarer than the
    #     garden's legendary Obsidian lore rate (1 in 1,000 rolls). At
    #     0.05% the expected value is < 0.5 Apple/day (a card is worth
    #     hundreds of Apples) so the jackpot cannot inflate the economy,
    #     while float-weighted selection keeps it a real, hittable outcome.
    #     Rebalancing = edit this map (or override it in system_config).
    "daily_chest_enabled": True,
    "daily_chest_weights": {
        "apples": 70.0,
        "seedlings": 20.0,
        "apple_gold": 6.0,
        "apple_diamond": 2.5,
        "apple_obsidian": 1.45,
        "card": 0.05,
    },
    # Quantity per outcome: a fixed int or an inclusive {"min","max"} range
    # (validated: integers >= 1, min <= max). Cards are always exactly one.
    "daily_chest_quantities": {
        "apples": {"min": 15, "max": 25},
        "seedlings": {"min": 1, "max": 2},
        "apple_gold": {"min": 1, "max": 2},
        "apple_diamond": 1,
        "apple_obsidian": 1,
        "card": 1,
    },
    # Progression hook on the EXISTING Account Level (player_xp): each
    # configured tier adds +apples_per_tier to the apples outcome only.
    # Levels 10/20 -> at most +10 Apples on a 15-25 base; odds and every
    # other outcome are untouched, so the rarity order can never invert and
    # higher levels never see cards/items more often. Disable with
    # "enabled": False or an empty tier list.
    "daily_chest_progression": {
        "enabled": True,
        "apples_per_tier": 5,
        "tier_levels": [10, 20],
    },
    # Group chest: seconds between two posts in one group. The group menu
    # button and «سیب صندوق» share it. Opening is never throttled by it.
    "group_chest_cooldown_seconds": 30,
    # Harvest drop model (spec: special-apple drops).
    # Presentation per outcome key. "asset" references a bot_assets key
    # (or a bundled assets/items/<key>.jpg file) for celebration media.
    # Special-apple drops are a CELEBRATION: the image is sent with an
    # event-like caption that escalates with the drop's rarity
    # (Golden 20% < Diamond 9% < Obsidian 1%). Captions live here so they
    # stay configuration/localization data - never hardcoded in handlers.
    "garden_drop_entries": {
        "apple_normal": {
            "message": "🌳 درختت پاداش داد!",
        },
        "apple_gold": {
            "message": "✨ شانست آورده!",
            "caption": "🌕 **سیب طلایی** برداشت کردی! شانست آورده.",
            "asset": "golden_apple",
        },
        "apple_diamond": {
            "message": "💎 باغت درخشید!",
            "caption": "💎 **سیب الماسی** گیرت اومد! باغت درخشید.",
            "asset": "diamond_apple",
        },
        "apple_obsidian": {
            "message": "🌑 این یه شانس افسانه‌ایه!",
            "caption": "🌑 **سیب ابسیدینی** گیرت اومد! این یه شانس افسانه‌ایه.",
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
        if key == "halloween_enabled":
            from sib.services.halloween import invalidate_halloween_config_cache

            invalidate_halloween_config_cache()

    @classmethod
    def default(cls, key: str) -> Any:
        return CONFIG_DEFAULTS.get(key)
