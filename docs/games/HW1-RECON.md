# HW1-RECON — Halloween Global Boss Event («سیبومورتیس» / Sibomortis)

**Phase:** `HW1-RECON` (Reconnaissance, Architecture, Schema, UI & Asset Plan — **NO CODE**)
**Branch:** `arena/f3725fd5-sib`
**Baseline Commit:** `b14a767` (`main`)
**Current Alembic Head:** `056_garden_wither_grace_hours` → Next revision: `057_halloween_event`

---

## Executive Summary

**Halloween Event: Sibomortis («سیبومورتیس»)** is a 7-day, bot-wide cooperative world-boss raid gated behind `system_config["halloween_enabled"]`. Unlike Invasion v2 (`inv2_invasions`, per-group enemy HP) or Apple Castle (`group_castles`, per-group progression), **Sibomortis has one single global shared HP pool across every Telegram group in the bot**.

Every design element below integrates natively into `sib`'s existing architecture with zero structural hacks:

1. **Global Boss State:** Stored in a dedicated, non-group-bound table `halloween_event_state` (`id`, `boss_hp`, `max_hp`, `current_phase`, `is_active`, `last_day_weakened`, `started_at`, `ends_at`, `defeated_at`, `finisher_user_id`), toggled cleanly via `system_config["halloween_enabled"]` with a 60-second TTL cache mirroring `MaintenanceService._invasion_enabled()`.
2. **Permanent Inventory & Shop Selling (`pumpkin_seed` / «دونه کدو»):** Stored directly in the existing `user_items` table (`services/items.py`). Adding `"pumpkin_seed"` to `ITEM_CATALOG` + `"pumpkin_seed": 15` to `CONFIG_DEFAULTS["item_sell_prices"]` (and merging into any pre-existing `system_config["item_sell_prices"]` JSON row in migration `057`) makes Pumpkin Seeds permanently stored in user inventories and immediately sellable in the existing Shop **Sell Items** (`فروش آیتم‌ها`) menu (`shop_sell` / `shop_sell_one:pumpkin_seed` / `shop_sell_all_ok:pumpkin_seed`) — **with zero edits to `handlers/shop.py` or `services/shop.py`**, and completely unaffected when `halloween_enabled` turns `False` after Day 7.
3. **Exclusive Milestone Card («امبر» / `amber`):** Registered as Character **#18** (`data/characters_seed.json`, code `"amber"`, `release_status: "RELEASED"`, **`shop_enabled: false`**, base attribute sum `== 60`) with bundled artwork at `assets/characters/amber.jpg`. Setting `shop_enabled: false` automatically excludes `amber` from the 300-Apple weekly shop rotation (`ShopService._pool_characters`) and free Daily Chest jackpots (`DailyChestService._eligible_card_characters`) while keeping Wiki (`WikiService.list_wiki`), My Cards (`CollectionService`), Card Upgrade L1→L10 (`CardUpgradeService`), Apple Battle, Apple Clash, and Invasion v2 working out of the box.
4. **Ironclad Idempotent Milestone Rewards (`75%`, `50%`, `25%`, `0%`):** Enforced at the database level via `halloween_reward_claims` (`UNIQUE (event_id, user_id, milestone_pct)`), backed by `AppleWalletService` `idempotency_key` ledger entries and `PlayerCard` unique constraint `uq_player_card_user_character`. No participant can ever receive any milestone reward twice under concurrent double-taps, Telegram update retries, multi-worker races, or end-of-event finalization sweeps.
5. **Deadlock-Free Lock Hierarchy:** Strict global wait-for DAG (`halloween_loot_drops [-1] → users [0] → player_cards [4] → user_items [5] → apple_wallets [6] → player_progressions [7] → halloween_event_state [8]`), preserving `docs/lock-order.md` (`services/lock_order.py`) with sub-millisecond global boss lock duration on the attack hot path.

---

## TASK 1 — Reconnaissance Findings

### 1.1 Global State Storage & Background Lifecycle (Not Group-Bound)

| Component | File & Exact Lines | Existing Pattern Found | How Halloween Uses It |
| --- | --- | --- | --- |
| Global key/value config store | `database/models/system_config.py:11-21` & `services/config_store.py:55-232` | `system_config` table (`key VARCHAR(100) PK`, `value TEXT` JSON-encoded) backed by `ConfigService.get/set` and code fallback `CONFIG_DEFAULTS`. Not tied to any `group_id`. | Stores `halloween_enabled` (`False` default) plus tunable balance keys (`halloween_default_max_hp`, `halloween_loot_cooldown_seconds`, `halloween_day7_hp_retention_ratio`, `halloween_day7_damage_multiplier`, `halloween_drop_*`). |
| Cached global feature flag | `services/maintenance/service.py:21-46` | `_invasion_enabled_cache` caches `bool(ConfigService.get(session, "invasion_enabled"))` for `_INVASION_CACHE_TTL = 60.0` seconds (monotonic clock), invalidated immediately on admin toggle via `invalidate_invasion_config_cache()`. | Exact mirror: `is_halloween_enabled()` with 60s monotonic cache + `invalidate_halloween_config_cache()` called on `/hw_start` and `/hw_stop`. Zero DB queries on ordinary group chat messages when disabled. |
| Single background sweep worker | `bot.py:132-135`, `services/maintenance/service.py:49-125` | Single `asyncio.create_task(MaintenanceService.run_forever(bot, interval_seconds=30))` loop. Each subsystem step in `run_once(bot)` runs inside its own `try/except` block; DB state transitions commit *before* Telegram I/O runs. | Add one isolated step inside `MaintenanceService.run_once(bot)` (guarded by `await is_halloween_enabled()`): handles (1) **Day-7 HP Weakening** (`now >= ends_at - 24h`), (2) **Random Group Loot Drop** spawning & expiry, and (3) **Event expiry / finalization** (`now >= ends_at + grace`). No second background loop needed. |
| Multi-row structured global state | `database/models/shop.py:21-38` (`shop_rotations`) | Global event/window tables have no `group_id` column; active singleton row queried via `is_active == True` and mutated via `SELECT ... FOR UPDATE`. | Dedicated table `halloween_event_state` (`id`, `boss_hp`, `max_hp`, `current_phase`, `is_active`, `last_day_weakened`, timestamps) — strongly typed, indexed, constrained, and row-lockable per attack without touching `system_config`. |

### 1.2 User Inventory Management & Adding `pumpkin_seed` With Shop Sell Support

| Component | File & Exact Lines | Existing Pattern Found | Impact for `pumpkin_seed` |
| --- | --- | --- | --- |
| Inventory table | `database/models/asset.py:31-48` | `user_items` table: `(id, user_id FK->users.id CASCADE, item_key VARCHAR(50), quantity INT >= 0)` with `UniqueConstraint("user_id", "item_key", name="uq_user_items_user_key")`. **No column on `users`; no TTL/cleanup ever deletes rows.** | `pumpkin_seed` rows live in `user_items` permanently — before, during, and forever after the Halloween event ends. |
| Item catalog & mutations | `services/items.py:21-60, 74-206` | `ITEM_CATALOG: Dict[str, ItemDef]` defines `(key, name_fa, emoji, asset_key, description_fa)`. `ItemService.grant`, `consume`, `get_total`, `list_items`, `sellable_inventory`, and `sell` are **100% data-driven** over `ITEM_CATALOG`. | Adding `"pumpkin_seed"` to `ITEM_CATALOG` immediately enables `grant()`, `consume()`, `get_total()`, `summary_line()`, and `sell()`. |
| Shop selling flow | `services/config_store.py:109-114`, `services/items.py:113-121, 187-206`, `handlers/shop.py:422-565` | `ItemService.sell_price(session, item_key)` reads `ConfigService.get(session, "item_sell_prices")`. `Shop` → `💰 فروش آیتم‌ها` (`shop_sell`) dynamically lists **every item in `ITEM_CATALOG` that has a positive entry in `item_sell_prices`**, complete with **Sell 1 (`shop_sell_one:<key>`)**, **Sell All with confirmation (`shop_sell_all:<key>` → `shop_sell_all_ok:<key>`)**, `lock_user_row` Rank 0 → `user_items` Rank 5 → `AppleWalletService.earn(..., reason="item_sold")` Rank 6! | Add `"pumpkin_seed": 15` to `CONFIG_DEFAULTS["item_sell_prices"]` **AND** extend any pre-existing `system_config` row `"item_sell_prices"` in migration `057` (same pattern as `047_inv2_rebalance_sib_buff.py:48-67`). Do **NOT** add `"pumpkin_seed"` to `item_buy_prices` (seeds cannot be bought with Apples; they are raid loot only, sellable into Apples). **Zero lines of `handlers/shop.py` or `services/shop.py` need modification!** |
| Item artwork fallback | `services/assets.py:17-59` | `AssetService.resolve_photo(session, key)` checks DB override `bot_assets` (`/setasset`), then falls back to `assets/items/<key>.jpg` or `.png` (`1024×1024`). | Place `assets/items/pumpkin_seed.jpg` (`1024×1024` JPEG) — automatically rendered on the Shop Sell detail screen and overridable live via `/setasset pumpkin_seed`. |

### 1.3 Adding Exclusive Card «امبر» (`amber`) & Roster Audit

**CRITICAL RECON DISCOVERY — Current Roster Count Is 17 (Not 16):**
`data/characters_seed.json` already contains **17 characters** (`sib_sorkh`, `sib_sabz`, `sib_talayi`, `sib_mahi`, `filo`, `goje`, `green_knight`, `igris`, `mmung`, `moon_knight`, `moon_lily`, `sage_king`, `treyni`, `wizard`, `zarush`, `mahmud` #16, and `tifun` «طیفان» #17). **«امبر» (`amber`) will be Character #18.**

How `sib` handles character seeding, exclusivity, artwork, and combat:

1. **Startup Seed Sync (`database/connection.py:203-319`):**
   - `init_db()` calls `seed_characters(session)` on every startup, reading `data/characters_seed.json`.
   - Every character **MUST** satisfy `power + speed + intelligence == 60` (`CARD_BASE_ATTRIBUTE_BUDGET = 60` in `services/card_progression.py`, enforced hard in `CharacterService.validate_base_budget` — any violation raises `CharacterSeedError` and aborts bot startup!).
   - On creation (`code not in existing_codes`, line `304`), `seed_characters` sets `character.shop_enabled = bool(entry.get("shop_enabled", True))` and `release_status = entry.get("release_status", ReleaseStatus.DRAFT)`.
   - **Subtle Seed Sync Nuance (`database/connection.py:272-282`):** For already-existing rows, `seed_characters` only syncs `("name_fa", "affiliations_fa", "power", "speed", "intelligence", "lore_fa")` (+ `faction_id` if `sync_faction: true`) and preserves `shop_enabled`. Setting `"release_status": "RELEASED"` and **`"shop_enabled": false`** in `data/characters_seed.json` sets `shop_enabled = False` cleanly on creation everywhere.
2. **Automatic Surface Behavior With `release_status = "RELEASED"` & `shop_enabled = False`:**
   - **Excluded from Weekly Shop (`services/shop.py`):** `_pool_characters()` requires `Character.is_active == True`, `release_status == RELEASED`, **and `shop_enabled == True`**. `amber` **never** enters the 300-Apple weekly rotation.
   - **Excluded from Daily Chest Jackpot (`services/daily_chest.py:667-690`):** `_eligible_card_characters()` requires `shop_enabled == True`. `amber` **never** drops randomly from daily chests.
   - **Excluded from Shop Faction Chests:** Faction chest drop weights are explicit code/config tables (`sages_chest`, `empire_chest`, `moonlight_chest`). `amber` **never** drops there unless explicitly added later.
   - **Included Automatically Everywhere Players Use Cards:**
     - **Wiki (`services/wiki.py:42-55`):** Lists all `is_active == True` + `release_status == RELEASED` characters (does *not* filter `shop_enabled`), showing full card art, stats, affiliations, and story (without price line!).
     - **Card Grant (`services/player_cards.py`):** `PlayerCardService.acquire(session, user_id, amber.id)` creates the real `PlayerCard` row (`level=1`), auto-awards one-time collection XP via `PlayerXPService.award_character_collection`, and respects `uq_player_card_user_character`.
     - **My Cards, Main Card, Upgrade L1→L10, Battles, Invasion v2, Apple Clash, Trading, Halloween Boss Attacks:** Works identically to all other 17 cards (`60` total attributes at L1 → `+10` per stat per level → `330` total at L10).
3. **Bundled Artwork Resolution (`services/character_art.py`):**
   - Resolves `assets/characters/{code}.jpg` directly by character `code` → **`assets/characters/amber.jpg`** (used as-is with zero cropping/alteration, matching `mahmud.jpg` & `tifun.jpg` at `912×1136` portrait JPEG).

### 1.4 Main Card Damage Calculation & Panel Ownership Conventions

- **Canonical Card Damage Formula (`services/invasion_v2/damage.py:6-22` & `services/war_service.py:23`):**
  Both Invasion v2 and Apple Clash compute single-card attack damage using the exact same pure helper:
  ```python
  card_power = card_attributes(main_card, character).power
  # Level 1 power range: 7..40 (avg ~20) | Level 5: 47..80 | Level 10: 97..130
  damage, is_crit = compute_damage(card_power, rng, crit_chance=0.10)
  # random_factor = uniform(0.85, 1.15); 10% crit = 2x -> round(card_power * random_factor * (2 if is_crit else 1))
  ```
  Reusing `compute_damage(card_power, rng, crit_chance=0.10)` keeps Main Card power scaling and critical hits 100% consistent across every PvE/raid mode in `sib`. If the user has not selected a Main Card yet (`PlayerCardService.get_main_card(session, user_id) is None`), the attack aborts **before** consuming `pumpkin_seed` with a friendly prompt directing them to `سیب کارت`.
- **Personal Panel Guard vs Public Group Loot Drops (`middleware/panel_guard.py` & `services/panel.py`):**
  - **Boss Menu (`هالووین`):** Displays the opener's personal `دونه کدو` count, loot cooldown, Main Card stats, and personal milestone claim states. Therefore, the sent message **MUST** be claimed via `PanelOwnershipService.claim(session, chat_id, sent.message_id, user.id, panel_type="halloween")` with `"halloween"` added to `PERSONAL_PANEL_TYPES` in `services/panel.py:92-104`, and all `hw:*` panel callbacks verify the actor via `resolve_personal_actor(callback)`. Other group members cannot click someone else's personal panel (`🔒 این پنل برای کاربر دیگری باز شده.`) — they simply type `هالووین` to open their own.
  - **Random Group Chest Drop (`hw_drop:claim:<drop_id>`):** Public race message posted to a group (first `max_claims` players win `1–3` `pumpkin_seed` each). Deliberately **NOT** in `PERSONAL_PANEL_TYPES` or `OWNER_SCOPED_CALLBACKS` (identical to `handlers/inv2_events.py:1-14` `inv2ev:loot:<id>`), while binding `(group_id, message_id)` in DB so forwarded messages can never be claimed elsewhere.

### 1.5 Existing Test Suite Traps & Pin Assertions Caught During Recon

Every single pin test below was found by reading the test suite line-by-line during HW1-RECON so Phase HW2 will have **zero surprise test failures**:

1. **`tests/invasion_v2/test_phase8c.py:118-145` (`test_migration_chain_head`) — ALREADY BROKEN ON `main` BY `056`!**
   - Line `122` hardcodes `assert scripts.get_current_head() == "055_inv2_random_events"` even though revision `056_garden_wither_grace_hours` already shipped!
   - **HW2 Action:** Update `test_phase8c.py:122` to `"057_halloween_event"` and add `"056_garden_wither_grace_hours": "055_inv2_random_events"` + `"057_halloween_event": "056_garden_wither_grace_hours"` to `chain`.
2. **`tests/test_card_roster.py:194-200` & `586-600`:**
   - Line `195`: `assert len(characters) == 17` → becomes `18` when `amber` is added to `data/characters_seed.json`.
   - Line `407`: `assert len(entries) == 17 and total_pages == 4` → **STAYS `17` and `4` automatically** because `amber.shop_enabled` is `False`! (Proof that `ShopService` isolation works cleanly!)
   - Line `597`: `assert created == len(NEW_CODES)` → update for `amber` alongside `NEW_CODES` (or `EXCLUSIVE_CARDS = {"amber": ...}`).
3. **`tests/test_card_lore.py:92-120` (`test_bold_labels_and_card_names_apply_to_all_cards`):**
   - Line `93`: `assert len(characters) == 17` → becomes `18`.
   - **CRITICAL TRAP AT LINE `118`:** Loops over `for character in characters.values(): assert f"*{escape_markdown(character.name_fa)}*" in listing` assuming *every* character in `characters_seed.json` is shop-enabled! Because `amber` has `shop_enabled = False`, line `118` **MUST** check `if character.shop_enabled:` (and assert `not in listing` when `not character.shop_enabled`!).
4. **`tests/test_lock_ordering.py`:**
   - Pins `LOCK_RANKS` in `services/lock_order.py`. Do **not** alter existing rank integers `0..7` in `LOCK_RANKS`; document Halloween's outer locks (`halloween_loot_drops` before `0`, `halloween_event_state` at `8` strictly after `0..7`) cleanly without shifting `0..7`.

---

## TASK 2 — Exact Database & Catalog Design

### 2.1 Lock-Order Specification (Zero-Deadlock Proof)

```text
Rank -1  halloween_loot_drops     (public group drop row lock — ONLY in claim_group_chest)
Rank  0  users                    (lock_user_row — canonical per-user mutex)
Rank  4  player_cards             (Amber milestone card acquisition)
Rank  5  user_items               (pumpkin_seed consume/grant, powder/special apple grants)
Rank  6  apple_wallets            (Apple milestone awards & Shop item sales)
Rank  7  player_progressions      (XP awards)
Rank  8  halloween_event_state    (global boss row lock — ALWAYS LAST in attack_boss!)
```

- **`attack_boss(session, user_id, group_id)`:** `users (0)` → `user_items (5)` (`consume 1 pumpkin_seed`) → `halloween_event_state (8)` (`FOR UPDATE` for `< 0.5 ms` right before `COMMIT`).
  - *Why `halloween_event_state` at Rank 8 is decisive:* Players with `0` seeds or no Main Card fail fast at Ranks `0..5` **without ever acquiring the global boss lock**; and if two players race on the last `10` HP so the second reaches Rank 8 when `boss_hp == 0`, raising `BossAlreadyDefeatedError` rolls back the entire transaction and **atomically refunds the consumed `pumpkin_seed`**.
- **`claim_seed_loot(session, user_id)`:** Plain read of `halloween_event_state` → `users (0)` → `halloween_seed_cooldowns` upsert → `user_items (5)` (`grant 1..3 pumpkin_seed`). Zero global locks.
- **`claim_milestone(session, user_id, milestone_pct)`:** Plain read of `halloween_event_state` (monotonic: once `boss_hp` crosses a threshold, it never goes back up) → `users (0)` → `INSERT INTO halloween_reward_claims` (`FLUSH` enforces `UNIQUE (event_id, user_id, milestone_pct)`) → `player_cards (4)` / `user_items (5)` / `apple_wallets (6)` / `player_progressions (7)`. Zero global locks.
- **`claim_group_chest(session, drop_id, user_id, group_id, message_id)`:** `halloween_loot_drops (-1)` (`FOR UPDATE`) → `INSERT INTO halloween_loot_drop_claims` → `users (0)` → `user_items (5)`. Nothing else ever locks `halloween_loot_drops` after `users`.
- **`MaintenanceService` Day-7 Weakening & Admin `/hw_*`:** Lock `halloween_event_state (8)` ONLY. Never touch `users..player_progressions`.

**Result:** Every edge in the global wait-for graph is strictly ascending (`-1 < 0 < 4 < 5 < 6 < 7 < 8`). Deadlocks are mathematically impossible.

---

### 2.2 SQLAlchemy Models (`database/models/halloween.py`) & Migration `057_halloween_event.py`

> **SQLite + MariaDB Portability Note:** Every autoincrement primary key uses `_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")` (matching `055_inv2_random_events.py:39`) so SQLite test databases auto-increment `INTEGER PRIMARY KEY` while production MariaDB uses `BIGINT AUTO_INCREMENT` with `mysql_charset="utf8mb4"`.

#### Table 1: `halloween_event_state` — Global Boss & 7-Day Lifecycle State
```sql
CREATE TABLE halloween_event_state (
    id                        BIGINT PRIMARY KEY AUTO_INCREMENT,
    boss_hp                   BIGINT NOT NULL,
    max_hp                    BIGINT NOT NULL,
    current_phase             INT NOT NULL DEFAULT 1,          -- 1 (100-76%), 2 (<=75%), 3 (<=50%), 4 (<=25%), 5 (0% slain)
    is_active                 BOOLEAN NOT NULL DEFAULT 0,
    last_day_weakened         BOOLEAN NOT NULL DEFAULT 0,      -- ensures Day-7 cut runs AT MOST ONCE
    hp_before_weaken          BIGINT NULL,                     -- audit snapshot before Day-7 cut
    weakened_at               DATETIME NULL,
    started_at                DATETIME NOT NULL,
    ends_at                   DATETIME NOT NULL,               -- started_at + 7 days
    defeated_at               DATETIME NULL,
    finisher_user_id          BIGINT NULL,
    announced_milestones_mask INT NOT NULL DEFAULT 0,          -- bitmask: 1=75%, 2=50%, 4=25%, 8=0%
    created_at                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_hw_state_finisher FOREIGN KEY (finisher_user_id) REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT ck_hw_state_max_hp_pos CHECK (max_hp > 0),
    CONSTRAINT ck_hw_state_hp_bounds CHECK (boss_hp >= 0 AND boss_hp <= max_hp),
    CONSTRAINT ck_hw_state_phase CHECK (current_phase BETWEEN 1 AND 5)
) CHARACTER SET utf8mb4;

CREATE INDEX ix_hw_state_active ON halloween_event_state (is_active);
```

#### Table 2: `halloween_attacks` — Global Attack Ledger, Participation Proof & Leaderboard Source
```sql
CREATE TABLE halloween_attacks (
    id             BIGINT PRIMARY KEY AUTO_INCREMENT,
    event_id       BIGINT NOT NULL,
    user_id        BIGINT NOT NULL,
    group_id       BIGINT NULL,                                -- origin chat for group stats; boss itself is global
    card_id        BIGINT NULL,
    character_id   BIGINT NULL,
    damage         INT NOT NULL,
    is_crit        BOOLEAN NOT NULL DEFAULT 0,
    boss_hp_after  BIGINT NOT NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_hw_attacks_event     FOREIGN KEY (event_id)     REFERENCES halloween_event_state (id) ON DELETE CASCADE,
    CONSTRAINT fk_hw_attacks_user      FOREIGN KEY (user_id)      REFERENCES users (id)                 ON DELETE CASCADE,
    CONSTRAINT fk_hw_attacks_group     FOREIGN KEY (group_id)     REFERENCES groups (id)                ON DELETE SET NULL,
    CONSTRAINT fk_hw_attacks_card      FOREIGN KEY (card_id)      REFERENCES player_cards (id)          ON DELETE SET NULL,
    CONSTRAINT fk_hw_attacks_character FOREIGN KEY (character_id) REFERENCES characters (id)            ON DELETE SET NULL,
    CONSTRAINT ck_hw_attacks_damage_pos CHECK (damage > 0)
) CHARACTER SET utf8mb4;

-- Covers BOTH participant check EXISTS(event_id, user_id) AND global leaderboard SUM(damage) GROUP BY user_id:
CREATE INDEX ix_hw_attacks_event_user ON halloween_attacks (event_id, user_id);
CREATE INDEX ix_hw_attacks_created_at ON halloween_attacks (created_at);
```

#### Table 3: `halloween_reward_claims` — Idempotent Global Milestone Claims (`75`, `50`, `25`, `0`)
```sql
CREATE TABLE halloween_reward_claims (
    id                  BIGINT PRIMARY KEY AUTO_INCREMENT,
    event_id            BIGINT NOT NULL,
    user_id             BIGINT NOT NULL,
    milestone_pct       INT NOT NULL,                          -- 75, 50, 25, 0
    reward_summary_json TEXT NOT NULL,                         -- JSON audit payload of granted items/apples/card
    claimed_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_hw_claims_event FOREIGN KEY (event_id) REFERENCES halloween_event_state (id) ON DELETE CASCADE,
    CONSTRAINT fk_hw_claims_user  FOREIGN KEY (user_id)  REFERENCES users (id)                 ON DELETE CASCADE,
    CONSTRAINT uq_hw_reward_claims_event_user_pct UNIQUE (event_id, user_id, milestone_pct),
    CONSTRAINT ck_hw_reward_claims_pct CHECK (milestone_pct IN (75, 50, 25, 0))
) CHARACTER SET utf8mb4;

CREATE INDEX ix_hw_reward_claims_user ON halloween_reward_claims (user_id);
```

#### Table 4: `halloween_seed_cooldowns` — Boss Menu «دریافت غنیمت» Cooldown Tracker
```sql
CREATE TABLE halloween_seed_cooldowns (
    event_id        BIGINT NOT NULL,
    user_id         BIGINT NOT NULL,
    last_claimed_at DATETIME NOT NULL,
    total_claims    INT NOT NULL DEFAULT 1,
    total_seeds     INT NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, user_id),
    CONSTRAINT fk_hw_seed_cd_event FOREIGN KEY (event_id) REFERENCES halloween_event_state (id) ON DELETE CASCADE,
    CONSTRAINT fk_hw_seed_cd_user  FOREIGN KEY (user_id)  REFERENCES users (id)                 ON DELETE CASCADE
) CHARACTER SET utf8mb4;
```

#### Tables 5 & 6: `halloween_loot_drops` & `halloween_loot_drop_claims` — Random Group Chest Drops
```sql
CREATE TABLE halloween_loot_drops (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    event_id      BIGINT NOT NULL,
    group_id      BIGINT NOT NULL,
    variant_code  VARCHAR(32) NOT NULL,                        -- 'pumpkin_chest' | 'lantern_crate' | 'autumn_hollow'
    status        VARCHAR(16) NOT NULL DEFAULT 'PENDING',      -- 'PENDING' | 'ACTIVE' | 'DEPLETED' | 'EXPIRED' | 'SKIPPED'
    max_claims    INT NOT NULL DEFAULT 3,
    claimed_count INT NOT NULL DEFAULT 0,
    message_id    BIGINT NULL,
    expires_at    DATETIME NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_hw_drops_event FOREIGN KEY (event_id) REFERENCES halloween_event_state (id) ON DELETE CASCADE,
    CONSTRAINT fk_hw_drops_group FOREIGN KEY (group_id) REFERENCES groups (id)                ON DELETE CASCADE
) CHARACTER SET utf8mb4;

CREATE INDEX ix_hw_drops_status_expires ON halloween_loot_drops (status, expires_at);
CREATE INDEX ix_hw_drops_group_created  ON halloween_loot_drops (group_id, created_at);

CREATE TABLE halloween_loot_drop_claims (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    drop_id       BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    slot_no       INT NOT NULL,
    seeds_granted INT NOT NULL,
    claimed_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_hw_drop_claims_drop FOREIGN KEY (drop_id) REFERENCES halloween_loot_drops (id) ON DELETE CASCADE,
    CONSTRAINT fk_hw_drop_claims_user FOREIGN KEY (user_id) REFERENCES users (id)                ON DELETE CASCADE,
    CONSTRAINT uq_hw_drop_claims_drop_user UNIQUE (drop_id, user_id),
    CONSTRAINT uq_hw_drop_claims_drop_slot UNIQUE (drop_id, slot_no),
    CONSTRAINT ck_hw_drop_claims_seeds CHECK (seeds_granted BETWEEN 1 AND 3)
) CHARACTER SET utf8mb4;

CREATE INDEX ix_hw_drop_claims_user ON halloween_loot_drop_claims (user_id);
```

---

### 2.3 Seed Item Catalog, System Config Defaults & Exclusive Card `amber`

#### A. `ITEM_CATALOG` Entry (`services/items.py`)
```python
"pumpkin_seed": ItemDef(
    key="pumpkin_seed",
    name_fa="دونه کدو",
    emoji="🎃",
    asset_key="pumpkin_seed",
    description_fa="مهمات جادویی نبرد با سیبومورتیس؛ قابل فروش همیشگی در فروشگاه.",
),
```

#### B. `CONFIG_DEFAULTS` Entries (`services/config_store.py`) + Migration `057` Data Seed
```python
# Feature kill switch (seeded 'false' in migration 057 via ON DUPLICATE KEY / ON CONFLICT DO NOTHING)
"halloween_enabled": False,

# Permanent sell price in Normal Apples — NEVER removed when halloween_enabled becomes False!
"item_sell_prices": {
    "apple_gold": 20,
    "apple_diamond": 75,
    "apple_obsidian": 200,
    "pumpkin_seed": 15,   # <-- NEW: 1 دونه کدو sells for 15 Normal Apples in Shop -> فروش آیتم‌ها
},

# Event balance knobs (all live-tunable via system_config with zero redeploy)
"halloween_default_max_hp": 500_000,
"halloween_duration_days": 7,
"halloween_claim_grace_hours": 48,
"halloween_loot_cooldown_seconds": 14_400,     # 4 hours between menu «دریافت غنیمت» claims (grants 1..3 seeds)
"halloween_day7_hp_retention_ratio": 0.20,     # Day 7 cuts remaining boss_hp by 80% (retains 20%, min 1 HP)
"halloween_day7_damage_multiplier": 2.0,       # Day 7 doubles player attack damage ("سپر تاریک فرو ریخت!")
"halloween_group_drop_interval_seconds": 10_800,  # Min 3h per active group between random chest drops
"halloween_group_drop_window_seconds": 300,    # 5-minute claim window per group chest
"halloween_group_drop_max_claims": 3,          # First 3 players in the group get 1..3 seeds each
```

> **Migration `057` Live-Override Safety Rule:** Exactly like `047_inv2_rebalance_sib_buff.py:48-67`, if a production database already has a custom `system_config` row for `"item_sell_prices"`, `057_halloween_event.upgrade()` parses that JSON dict and inserts `"pumpkin_seed": 15` **only if `"pumpkin_seed"` is absent**, never clobbering customized prices on other items.

#### C. Milestone Reward Schedule & Exclusive Character Card «امبر» (`amber`)

| Milestone Threshold | Global HP Trigger | Reward Granted to **Every Participating Player (`>= 1` Attack)** | Canonical Service Delivery & Idempotency Guard |
| --- | --- | --- | --- |
| **۷۵٪** (`current_phase >= 2`) | `boss_hp <= max_hp * 75 // 100` | `🍎 ۱۵۰ سیب معمولی` + `🎃 ۲ دونه کدو` | `AppleWalletService.earn(..., idempotency_key=f"hw_ms:{event_id}:{user_id}:75")` + `ItemService.grant(session, user_id, "pumpkin_seed", 2)` |
| **۵۰٪** (`current_phase >= 3`) | `boss_hp <= max_hp * 50 // 100` | `✨ ۳ پودر جادویی` + `🌕 ۲ سیب طلایی` | `ItemService.grant(..., "magic_powder", 3)` + `ItemService.grant(..., "apple_gold", 2)` |
| **۲۵٪** (`current_phase >= 4`) | `boss_hp <= max_hp * 25 // 100` | **🎴 کارت انحصاری شخصیت «امبر» (`amber`)** *(+ fallback `🍎 ۳۰۰ سیب` if user already owns `amber`)* | `PlayerCardService.acquire(session, user_id, amber.id)` + `PlayerXPService.award_character_collection(...)` |
| **۰٪** (`current_phase == 5`) | `boss_hp == 0` *(Boss Slain!)* | `🍎 ۵۰۰ سیب معمولی` + `💎 ۱ سیب الماسی` + `🌑 ۱ سیب ابسیدینی` | `AppleWalletService.earn(..., idempotency_key=f"hw_ms:{event_id}:{user_id}:0")` + `ItemService.grant` (`apple_diamond: 1`, `apple_obsidian: 1`) |

**Seed Entry Proposal for Exclusive Card #18 (`data/characters_seed.json`):**
*(Note: If the user supplies verbatim official card copy alongside `amber.jpg` in HW2, replace `affiliations_fa` / `lore_fa` / `faction` / stat split verbatim while preserving `sum == 60` and `"shop_enabled": false`.)*

```json
{
  "code": "amber",
  "name_fa": "امبر",
  "name_en": "Amber",
  "affiliations_fa": "فرزندان ماه🌙",
  "lore_fa": "جادوگر شعله‌های کهربایی خزان که در تاریک‌ترین شب سال از دل مه برمی‌خیزد؛ فانوس او مرز میان جهان زندگان و سایه‌های سیبومورتیس را روشن نگه می‌دارد.",
  "power": 24,
  "speed": 18,
  "intelligence": 18,
  "faction": "moon_descendants",
  "sync_faction": true,
  "release_status": "RELEASED",
  "shop_enabled": false
}
```

---

## TASK 3 — Persian UI Copy & Image / Asset Plan

All player-facing copy below follows `.agents/skills/persian-writing/SKILL.md`: pure Persian `ی`/`ک`, proper ZWNJ (`\u200c`) and `هٔ` ezafe, Persian digits (`۰۱۲۳۴۵۶۷۸۹`), concise mobile-first hierarchy under Telegram's `1024`-character photo caption limit, and semantic glass buttons (`success` green, `primary` blue, `danger` red).

### 3.1 Screen 1 — Boss Main Menu (Triggered by typing `هالووین` in a Group)

**Media:** Photo `assets/halloween/sibomortis.jpg` (resolved via `AssetService.resolve_photo(session, "halloween_boss")` with bundled fallback `assets/halloween/sibomortis.jpg`).

**Caption:**
```markdown
🎃 **رویداد جهانی هالووین: سیبومورتیس**

🕯 سایهٔ مرگبار **سیبومورتیس** بر تمام گروه‌های اواریا افتاده است! همهٔ گروه‌ها با هم به یک جان مشترک ضربه می‌زنند.

❤️ **جان جهانی سیبومورتیس:**
`███████░░░` **۶۸٪** (`۳۴۲٬۱۵۰` از `۵۰۰٬۰۰۰`)
🔥 **فاز نبرد:** فاز ۲ از ۴ | ⏳ **زمان باقی‌مانده:** ۵ روز و ۱۴ ساعت

🎒 **دونه کدوهای شما:** `۳` عدد 🎃
⚔️ **مجموع آسیب شما:** `۴۲۰` (رتبهٔ جهانی: `#۱۸`)
🎁 **جوایز آمادهٔ دریافت:** `۱` مرحله باز شده! ✨
```
*(When Day-7 Weakening is active, an extra banner line appears right under the title:
`⚡ **روز پایانی!** طلسم سیبومورتیس شکسته شد؛ جان باس فرو ریخت و قدرت حمله‌ها ۲ برابر شد!`)*

**Inline Keyboard (Exact 3 Buttons Required by Spec):**
1. Row 1 (`success` — green): `⚔️ حمله به سیبومورتیس (۱ 🎃)` → `callback_data="hw:attack"`
2. Row 2 (`primary` — blue): `🎁 مسیر جوایز` → `callback_data="hw:rewards"` | `🎃 دریافت غنیمت` → `callback_data="hw:loot"`

*(Pressing `🎃 دریافت غنیمت`:*
- *When ready: grants `۱` تا `۳` `pumpkin_seed` immediately, shows callback toast `"🎃 ۲ دونه کدو به کوله‌پشتی‌ات اضافه شد!"`, and refreshes the Boss Menu caption in place with the new `دونه کدو` count!*
- *When on cooldown: shows private callback alert `"⏳ فانوس کدویی هنوز خاموش است! ۲ ساعت و ۱۵ دقیقه دیگر برای دریافت غنیمت برگرد."` — zero chat spam!)*

---

### 3.2 Screen 2 — Attack Result View (Edited In Place on Boss Photo)

**Caption (Normal Hit / Critical Hit Variant):**
```markdown
💥 **ضربهٔ بحرانی به سیبومورتیس!**

🎴 کارت اصلی شما (**ایگریس**، سطح `۵`) با تمام توان به قلب تاریکی کوبید!

⚔️ **آسیب این حمله:** `۱۴۸` *(ضربهٔ بحرانی ×۲!)*
❤️ **جان باقی‌ماندهٔ سیبومورتیس:**
`███████░░░` **۶۸٪** (`۳۴۲٬۰۰۲` از `۵۰۰٬۰۰۰`)

🏆 **مجموع آسیب شما:** `۵۶۸` | **رتبهٔ جهانی:** `#۱۲`
🎃 **دونه کدوهای باقی‌مانده:** `۲` عدد

**برترین شکارچیان جهان:**
🥇 **آرش:** `۴٬۸۲۰` آسیب
🥈 **سارا:** `۴٬۳۱۰` آسیب
🥉 **کیان:** `۳٬۹۵۵` آسیب
```

**Inline Keyboard:**
1. Row 1 (`success` — green): `⚔️ حملهٔ دوباره (۱ 🎃)` → `callback_data="hw:attack"`
2. Row 2 (`primary` — blue): `🎁 مسیر جوایز` → `callback_data="hw:rewards"` | `🏆 جدول کامل شکارچیان` → `callback_data="hw:leaderboard"`
3. Row 3 (`danger` — red): `🔙 بازگشت به منوی هالووین` → `callback_data="hw:menu"`

---

### 3.3 Screen 3 — Milestone Reward Track (`🎁 مسیر جوایز`)

*Note:* If the player just unlocked **«امبر» (`25%`)** on claim, the screen swaps media in place to **`assets/characters/amber.jpg`** so the player gets a full-screen card reveal moment! Otherwise it keeps `sibomortis.jpg`.

**Caption:**
```markdown
🎁 **مسیر جوایز جهانی هالووین**

❤️ **جان فعلی باس:** `۲۱۸٬۴۰۰` از `۵۰۰٬۰۰۰` (**۴۳٪**)
⚔️ **وضعیت مشارکت شما:** `۷` حمله ثبت‌شده *(واجد شرایط دریافت جوایز!)*

✅ **مرز ۷۵٪ جان:** `۱۵۰ سیب 🍎 + ۲ دونه کدو 🎃` *(دریافت شد)*
🟢 **مرز ۵۰٪ جان:** `۳ پودر جادویی ✨ + ۲ سیب طلایی 🌕` *(آمادهٔ دریافت!)*
🔒 **مرز ۲۵٪ جان:** **🎴 کارت انحصاری «امبر»** *(در ۱۲۵٬۰۰۰ جان باز می‌شود)*
🔒 **مرز ۰٪ جان (مرگ باس):** `۵۰۰ سیب 🍎 + ۱ سیب الماسی 💎 + ۱ سیب ابسیدینی 🌑`

💡 هر بازیکن شرکت‌کننده هر مرحلهٔ بازشده را دقیقاً یک بار دریافت می‌کند.
```

**Inline Keyboard:**
1. Row 1 (`success` — green, shown whenever `>= 1` unlocked milestone is unclaimed & player has `>= 1` attack):
   `🎁 دریافت جوایز بازشده (۱ مرحله)` → `callback_data="hw:claim_rewards"`
2. Row 2 (`primary` — blue): `🏆 جدول شکارچیان` → `callback_data="hw:leaderboard"`
3. Row 3 (`danger` — red): `🔙 بازگشت به منوی هالووین` → `callback_data="hw:menu"`

---

### 3.4 Exact Filesystem Placement for All Assets

| Asset Role | Source | Exact Repo Path | Target Format & Resolution | Resolution Helper |
| --- | --- | --- | --- | --- |
| **1. Boss Image («سیبومورتیس» / Sibomortis)** | **User-provided image #1** | **`assets/halloween/sibomortis.jpg`** | JPEG, `1280×720` (`16:9`) or `1024×1024` (`1:1`), used strictly as delivered (`< 600 KB` ideal for fast Telegram upload; cached via Telegram `file_id` or `/setasset halloween_boss`) | `AssetService.resolve_photo(session, "halloween_boss")` with fallback to `assets/halloween/sibomortis.jpg` |
| **2. Exclusive Card («امبر» / Amber)** | **User-provided image #2** | **`assets/characters/amber.jpg`** | Portrait JPEG (`~4:5`, e.g., `912×1136` matching `mahmud.jpg` & `tifun.jpg`), **stored byte-for-byte verbatim with zero recropping** | `services/character_art.py` (`resolve_character_photo` → `assets/characters/{character.code}.jpg`) |
| **3. Item Icon («دونه کدو» / Pumpkin Seed)** | Generated in HW2 asset pass | **`assets/items/pumpkin_seed.jpg`** | Square `1024×1024` (`1:1`) RGB JPEG, `< 200 KB` (matches all 16 existing icons in `assets/items/`) | `ItemService.asset_key("pumpkin_seed")` → `AssetService.resolve_photo(session, "pumpkin_seed")` |
| **4. Group Random Loot Drops (3 Variants)** | Generated from prompts below | **`assets/halloween/loot_pumpkin_chest.jpg`**<br>**`assets/halloween/loot_lantern_crate.jpg`**<br>**`assets/halloween/loot_autumn_hollow.jpg`** | Square `1024×1024` (`1:1`) RGB JPEG, `~200–300 KB` each (matches `assets/invasion_v2/events/loot_*.jpg` & `assets/daily_chest/chest.jpg`) | `resolve_halloween_drop_photo(session, variant_code)` (overridable via `/setasset hw_drop_<variant>`) |

---

### 3.5 Three Image Proposals & Production FLUX Prompts for Group Random Loot Drops

Following `.agents/skills/create-game-assets/SKILL.md` and `.agents/skills/flux-image-best-practices/SKILL.md` (single iconic focal object at `48×48` mobile thumbnail scale, warm rim lighting against dark atmospheric ground, rich painterly stylized fantasy matching `sib`'s `assets/invasion_v2/events/loot_golden_chest.jpg`, **zero embedded text/letters/UI HUD**):

#### Proposal 1 — **صندوقچهٔ کدوی افسانه‌ای** (`assets/halloween/loot_pumpkin_chest.jpg`)
- **Group Caption Concept:** *«🎃 صدای خش‌خش برگ‌های پاییزی پیچید! یک **صندوقچهٔ کدویی جادویی** وسط گروه افتاد؛ ۳ نفر اولی که دکمه را بزنند ۱ تا ۳ دونه کدو می‌گیرند!»*
- **Button:** `🎃 برداشتن دونه کدو (۰/۳)` (`success` green → `hw_drop:claim:<drop_id>`)
- **Production Prompt (`1024×1024`, `1:1`):**
  > Stylized fantasy game illustration of an ornate treasure chest carved from a giant enchanted autumn pumpkin with wrought-iron bands and brass clasps, lid cracked open spilling warm golden-amber light (`#FF9F1C`) and glowing magical pumpkin seeds resting on dark velvet inside. Set on mossy cobblestone ground scattered with fallen crimson leaves and tiny jack-o'-lantern candle stubs in a misty twilight apple orchard. Dramatic warm golden core glow from inside the chest contrasted against cool deep midnight-indigo fog (`#16192B`) in the background, crisp rim light tracing the carved ridges and iron hardware, painterly digital fantasy art matching Supercell/Hearthstone prop illustration, centered composition with generous breathing room, 1:1 square format, clean full-bleed artwork with zero text, zero letters, zero UI frames, and zero watermarks.

#### Proposal 2 — **فانوس شکستهٔ شکارچی سایه** (`assets/halloween/loot_lantern_crate.jpg`)
- **Group Caption Concept:** *«🕯 پیک نگهبانان در مه شبانه محموله‌ای جا گذاشت! **فانوس و جعبهٔ بذرهای نورانی** پیدا شد؛ ۳ شکارچی اول سهمیه می‌گیرند!»*
- **Button:** `🕯 برداشتن غنیمت (۰/۳)` (`success` green → `hw_drop:claim:<drop_id>`)
- **Production Prompt (`1024×1024`, `1:1`):**
  > Stylized fantasy game illustration of a weathered dark-oak supply crate bound with iron corners beside a lit wrought-iron harvest lantern, the crate's top slats pried open revealing a burlap pouch overflowing with radiant golden-orange pumpkin seeds (`#FFB703`) and subtle autumn sparks drifting upward. Resting on damp forest roots and fallen golden orchard leaves under a pale crescent moon seen softly through twisted apple branches. Warm amber lantern light from the left and luminous seed glow from the center contrasting against cool slate-violet night shadows (`#1D1A2F`), bold readable silhouette at small mobile chat size, rich hand-painted stylized fantasy texture, centered 3/4 hero angle, 1:1 square format, pure scene illustration without any typography, numbers, borders, or logos.

#### Proposal 3 — **کندهٔ جادویی باغ خزان** (`assets/halloween/loot_autumn_hollow.jpg`)
- **Group Caption Concept:** *«🍂 باد خزانی در ریشهٔ درخت کهن پیچید! در شکاف **کندهٔ جادویی باغ** چند دونه کدو می‌درخشد؛ زود باش قبل از ناپدید شدن!»*
- **Button:** `🍂 جمع کردن دونه کدو (۰/۳)` (`success` green → `hw_drop:claim:<drop_id>`)
- **Production Prompt (`1024×1024`, `1:1`):**
  > Stylized fantasy game illustration of a gnarled ancient apple-tree hollow nestled with small glowing jack-o'-lanterns and a woven twilight basket filled with shimmering amber pumpkin seeds (`#FB8500`) and tiny floating fireflies. Surrounded by deep purple twilight mist (`#231942`), autumn ferns, and fallen red-gold apples on dark woodland soil. Warm inviting bonfire-toned light radiating outward from the hollow against cool moonlit forest depth, crisp brush-painted edges, strong focal hierarchy readable at thumbnail scale, whimsical dark-autumn fairy-tale fantasy art direction, centered 3/4 view, 1:1 square format, clean edge-to-edge painting with zero text, zero runes, and zero UI overlays.

---

## Step-by-Step 7-Day Event Lifecycle Flow

```text
[T - Pre-Launch: Migration 057 Deployed + Assets Placed]
  • Alembic migration 057 creates tables + seeds system_config['halloween_enabled'] = false
    and merges "pumpkin_seed": 15 into item_sell_prices.
  • Startup seed_characters() registers Card #18 «امبر» (amber, shop_enabled=False, sum=60).
  • Group chat behaves 100% normally; zero Halloween UI exposed yet.

[Day 1 — Hour 0: Operator Starts Event (/hw_start 500000)]
  • Creates active halloween_event_state row:
      boss_hp = 500,000 | max_hp = 500,000 | current_phase = 1 | is_active = True
      started_at = T0 | ends_at = T0 + 7 days | last_day_weakened = False
  • Sets system_config['halloween_enabled'] = True & invalidates 60s flag cache.

[Days 1–6: Global Cooperative Hunt Across All Groups]
  1. Gathering Pumpkin Seeds («دونه کدو»):
     a) Player types «هالووین» in any group -> opens personal Boss Menu (panel_type="halloween")
        showing sibomortis.jpg, live Global HP bar, personal دونه کدو count, and 3 buttons:
        [⚔️ حمله] [🎁 مسیر جوایز] [🎃 دریافت غنیمت].
     b) Pressing [🎃 دریافت غنیمت] checks halloween_seed_cooldowns (4h cooldown) under lock_user_row,
        grants 1–3 pumpkin_seed into user_items, and updates the caption in place.
     c) Every 30s MaintenanceService tick checks eligible active groups (respecting circuit breaker
        & per-group 3h spacing) and posts a Random Loot Drop chest (1 of 3 visual variants).
        First 3 group members who tap [🎃 برداشتن دونه کدو] each receive 1–3 pumpkin_seed.
  2. Attacking Sibomortis & Global Leaderboard:
     • Player taps [⚔️ حمله به سیبومورتیس (۱ 🎃)]:
       - Validates Main Card is selected; consumes 1 pumpkin_seed (Rank 0 -> Rank 5);
       - Computes Main Card power via card_attributes(main_card, character).power and rolls
         compute_damage(power, rng, crit_chance=0.10);
       - Locks halloween_event_state (Rank 8), subtracts damage from global boss_hp, updates
         current_phase if a threshold is crossed, inserts halloween_attacks row, commits.
     • Attack Result screen shows damage dealt, crit badge, new Global HP, remaining seeds,
       player's cumulative damage + live global rank, and Top 3 global hunters.
  3. Unlocking Global Milestones (75% -> 50% -> 25% Amber -> 0%):
     • As all groups collectively push boss_hp past 375k (75%), 250k (50%), and 125k (25%):
       every participating player (attacks >= 1) opens [🎁 مسیر جوایز] and taps
       [🎁 دریافت جوایز بازشده].
     • At 25% HP, every participant acquires Exclusive Card «امبر» (amber) with full card art
       reveal (assets/characters/amber.jpg), usable immediately as Main Card!

[Day 7 — Final 24 Hours (now >= started_at + 6 days): Last-Day Weakening]
  • MaintenanceService.run_once acquires FOR UPDATE on halloween_event_state where
    is_active == True AND boss_hp > 1 AND last_day_weakened == False:
      - Records hp_before_weaken = boss_hp and weakened_at = now;
      - Reduces remaining HP by 80%: boss_hp = max(1, boss_hp * 20 // 100)
        (never 0 — a real player ALWAYS lands the final killing blow!);
      - Updates current_phase for any newly crossed thresholds (unlocking 25% Amber immediately
        if the boss was previously above 25%!);
      - Enables 2.0x Day-7 damage multiplier ("طلسم سیبومورتیس شکسته شد!").
  • Players rally in the final hours and drive boss_hp to 0!
  • Final attacker sets boss_hp = 0, current_phase = 5, defeated_at = now, finisher_user_id = uid,
    unlocking the 0% Victory Milestone (500 Apples + 1 Diamond Apple + 1 Obsidian Apple) for ALL participants!

[Post-Victory Grace Window & Permanent Aftermath]
  • For 48h after victory / ends_at (until auto-finalization or /hw_stop):
    - Attacks & seed drops stop cleanly; typing «هالووین» shows the Victory Monument screen so
      late-checking participants can still click [🎁 دریافت جوایز بازشده].
    - On finalization, a batched idempotent sweep auto-grants any still-unclaimed unlocked
      milestones to every remaining participant (guaranteed zero missed rewards, zero duplicates).
  • FOREVER AFTER THE EVENT ENDS:
    - Any unused «دونه کدو» (pumpkin_seed) remains permanently in user_items.
    - Players can keep them as souvenirs/ammo OR sell 1 / sell all anytime in
      Shop -> 💰 فروش آیتم‌ها for 15 Normal Apples each.
    - Exclusive Card «امبر» remains permanently in players' collections, upgradable L1->L10,
      never sold in the 300-Apple weekly shop rotation.
```

---

## Implementation Roadmap for Next Phase (`HW2` — Code Implementation)

1. **Assets & Seed Data:** Place user-supplied `assets/halloween/sibomortis.jpg` + `assets/characters/amber.jpg`, generate `assets/items/pumpkin_seed.jpg` + 3 `assets/halloween/loot_*.jpg` drops, add `amber` (`shop_enabled: false`) to `data/characters_seed.json`, and register `"pumpkin_seed"` in `services/items.py` + `services/config_store.py`.
2. **Database Layer:** Add `database/models/halloween.py` (`HalloweenEventState`, `HalloweenAttack`, `HalloweenRewardClaim`, `HalloweenSeedCooldown`, `HalloweenLootDrop`, `HalloweenLootDropClaim`), export in `database/models/__init__.py`, and create portable Alembic migration `057_halloween_event.py`.
3. **Domain Service (`services/halloween.py`):** Implement `is_halloween_enabled` (60s cache), `attack_boss`, `claim_seed_loot`, `claim_milestone`, `claim_group_chest`, `apply_day7_weakening_if_due`, `spawn_or_expire_group_drops`, `finalize_expired_events`, and `get_leaderboard`. Wire single isolated step into `MaintenanceService.run_once`.
4. **Handlers & Panel Ownership (`handlers/halloween.py`, `handlers/hw_admin.py`):** Register `"halloween"` in `PERSONAL_PANEL_TYPES`, handle group wake word `هالووین`, personal callbacks `hw:*`, public chest callback `hw_drop:claim:<id>`, and private admin controls `/hw_start`, `/hw_stop`, `/hw_status`, `/hw_set_hp`, `/hw_weaken_now`, `/hw_drop`.
5. **Tests:** Fix `tests/invasion_v2/test_phase8c.py` chain head (`057`), update `tests/test_card_roster.py` & `tests/test_card_lore.py` (`if character.shop_enabled:`), and add full unit/concurrency/idempotency/post-event-sell suite `tests/test_halloween_event.py`.
