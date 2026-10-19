# M2-RECON — Apple Mine: Pickaxe Progression Rework

**Phase:** design and reconnaissance only — **no code written in this phase** · **Date:** 2026-10-19
**Inspected baseline:** `154307c` (branch `arena/385c6fca-sib`, branched from `main`)
**Scope of this change:** this report only. No application code, migrations, handlers, configuration, generated images, or database data are changed. Everything described as “proposed” is future work, not existing behavior.

## 0. Proposed design at a glance

- Card selection is **removed**. The main mine menu has exactly two buttons: `⛏ کار کردن در معدن` (work) and `🔨 ارتقا کلنگ` (upgrade pickaxe).
- Every player owns exactly one pickaxe, **Level 1 → Level 5**, stored in a new 1:1 table **`user_pickaxes`** (lazy-created). Upgrade costs are **normal wallet apples only**.
- **30-minute cooldown unchanged** (`WORK_COOLDOWN_SECONDS = 1800`, last-shift-based, DB clock).
- Shift rewards become a **per-pickaxe-level table**: normal-apple range rolls (10–15 at L1 … 45–60 at L5) plus a rising special-apple chance (10% … 29%). Special-apple quantities are unchanged (3× gold, 1× diamond, 1× obsidian).
- Economy: upgrade ladder **300 / 600 / 1 000 / 1 500** (3 400 total). A dedicated miner (8 shifts/day, selling special apples) reaches max level in **~9–10 days**; a casual player in ~2–3 weeks.
- All numbers are **config-driven** (`system_config` key `pickaxe_levels`, defaults in `config_store.py`), matching the existing `card_upgrade_costs` rebalancing pattern.
- Five new AI images (1280×720) for the pickaxe levels, same art language as the existing `assets/apple_mine/mine.jpg`.

---

## 1. Task 1 — Reconnaissance

Paths relative to repo root. Python imports use the `sib.*` namespace.

### 1.1 Current flow (end to end)

| Source | Finding |
|---|---|
| `handlers/apple_mine.py:113–121` | Entry: `/mine` command and the exact Persian trigger `معدن سیبی` (whitespace-tolerant regex) both call `_send_panel`. |
| `handlers/apple_mine.py:80–108` | `_send_panel` registers the user/group (`UserService.get_or_create_user` + `GroupService.get_or_create_group`), then `answer_photo` with `assets/apple_mine/mine.jpg` (1280×720, per M1-REPORT) + `PANEL_CAPTION` + `work_button_kb`. On image failure it degrades to a text panel with the same keyboard. |
| `services/telegram_ui.py:155–185` | `work_button_kb` builds the single green work button: `text="⛏ کار کردن در معدن"`, `callback_data="mine:work"`, Bot API 10.3 `style="success"` with a `🟢 …` label fallback for legacy clients. |
| `handlers/apple_mine.py:196–245` | `mine:work` → `remaining_cooldown(...)`; if > 0 the presser gets an alert (`cooldown_message`); otherwise `PlayerCardService.list_cards` is rendered as one button per owned card (`mine:card:<id>`). No cards → `NO_CARDS_TEXT`. |
| `handlers/apple_mine.py:257–310` | `mine:card:<id>` → `perform_shift(...)` in one `get_session()` (no reads before the service's user-row lock, by design under MariaDB REPEATABLE READ). Result edits the same photo's caption (`_edit_panel`: `safe_edit_message_caption` for photos, `safe_edit_callback_message` for text) with narrative + reward line and clears the keyboard. |
| `handlers/apple_mine.py:247–253` | Result formatting: `fa_digits` Persian digits, `item_emoji`/`item_label` for special apples, `🍎 سیب معمولی` for the wallet outcome. All captions use `parse_mode=None`. |

### 1.2 Cooldown tracking (kept as-is)

- `services/apple_mine.py:17` — `WORK_COOLDOWN_SECONDS = 1800` (30 min).
- `services/apple_mine.py:68–75` — `_last_worked_at` reads the **latest committed** `apple_mine_shift.worked_at` for the user (`ORDER BY worked_at DESC, id DESC LIMIT 1`, covered by the `(user_id, worked_at)` composite index from migration 043).
- `services/apple_mine.py:60–83` — elapsed-time math against the **database clock** (`select(func.now())`), not the app clock.
- `services/apple_mine.py:92–119` — `perform_shift` first takes the `users` row mutex via `lock_user_row` (`services/lock_order.py`, documented in `docs/lock-order.md`), so concurrent taps from any group/worker serialize; the cooldown read happens *inside* the lock.
- Cooldown is **per-user and global across groups** — this is exactly the behavior we keep. No changes needed in M3.

### 1.3 How rewards are granted currently (to be replaced by level-based rolls)

- `services/apple_mine.py:21–25` — one static weighted table for every player:

  | outcome | weight | item_code | amount |
  |---|---|---|---|
  | `normal` | 60 | `apple_normal` | 15 |
  | `golden` | 25 | `apple_gold` | 3 |
  | `diamond` | 12 | `apple_diamond` | 1 |
  | `obsidian` | 3 | `apple_obsidian` | 1 |

- `services/apple_mine.py:120–123` — `random.choices(OUTCOMES, weights=...)`, one roll per shift.
- Grant path (`services/apple_mine.py:139–149`):
  - `apple_normal` → **wallet**: `AppleWalletService.earn(session, user_id, amount, reason="apple_mine", ref_type="apple_mine_shift", ref_id=shift.id, idempotency_key=f"apple_mine:{shift.id}")`.
  - special apples → **inventory**: `ItemService.grant(session, user_id, item_code, quantity=amount)` (`user_items` rows; sellable in the shop).
- The `AppleMineShift` row (user, group, card, outcome, item, amount, `worked_at`) is written **before** granting and is the audit record. `apple_normal` is deliberately **not** an inventory item (`services/items.py:32–34`).
- **The card is cosmetic**: `perform_shift` verifies ownership of `card_id` but never consumes or locks it (`services/apple_mine.py:110–117`). Removing card selection therefore touches no resource flow — only the UI, and `card_id` on new shifts becomes `NULL` (column kept, nullable, for historical rows).
- M1 EV check (sell-equivalent: gold 50 / diamond 100 / obsidian 200 apples per `services/config_store.py` sell-value comments): 0.60×15 + 0.25×3×50 + 0.12×1×100 + 0.03×1×200 = **64.5 apples worth per shift** (~516/day at 8 shifts). The M2 table deliberately starts below this at Level 1 and exceeds it at Level 5 (see §2.4 — a conscious nerf-then-reward arc).

### 1.4 Where to store the Pickaxe level

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. New table `user_pickaxes` (1:1 with `users`)** | Matches repo convention — every feature owns its table (`apple_wallets`, `player_stats`, `player_cards`, `apple_mine_shift`…); `users` stays a lean identity/security table (currently: identity, skin, ban, referral only); lazy row creation mirrors `AppleWalletService._get_or_create` — **zero backfill** for existing players (all default to level 1 on first read); trivially extensible (max level from config, future `total_shifts`, skin slots); no backfill data migration needed | One extra join in the shift path (negligible: single PK read, same transaction, already under the user-row lock) | **Recommended** |
| B. `users.pickaxe_level` column | One-row read, no join | Pollutes the core identity table with game progression; requires a column-adding migration on a hot table touched by every economy op; inconsistent with how every other progression system in this repo is stored (`player_cards.level`, garden plots, castle tables, `player_xp`) | Rejected |
| C. Derive from `apple_mine_shift` history / `system_config` | No new storage | Cooldown logic would become fragile; upgrade state (a mutation, not an event stream) is exactly what belongs in its own row | Rejected |

**Decision: Option A — new table `user_pickaxes`.**

### 1.5 Proposed schema (for the M3 migration, numbered `053_apple_pickaxes` after head `052_war_attack_count`)

```sql
CREATE TABLE user_pickaxes (
    user_id    BIGINT NOT NULL PRIMARY KEY,
    level      INT    NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_user_pickaxes_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) CHARACTER SET utf8mb4;

ALTER TABLE apple_mine_shift
    ADD COLUMN pickaxe_level INT NULL;   -- snapshot of the level used for the shift; NULL for pre-M2 rows
```

Notes for implementation:

- ORM model `UserPickaxe(Base, TimestampMixin)` with `user_id` as FK+PK, `level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)` — mirrors `AppleWallet` (PK = `user_id`, FK `ondelete="CASCADE"`). Level bounds (1..5) are clamped in the service; `system_config` can later raise the max without a schema change.
- **Lazy creation**: `PickaxeService.get_or_create_pickaxe(session, user_id)` inside the same transaction that already holds the `users` row lock (`lock_user_row`), so creation/upgrade/shift never race. The upgrade itself is a `SELECT … FOR UPDATE` on the pickaxe row + `level += 1`.
- **Audit**: add `pickaxe_level` (nullable) to `apple_mine_shift` so every shift records the level it was played with — needed for balance analysis after launch and for any future per-level analytics. Existing rows stay `NULL`.
- `apple_mine_shift.card_id`: **keep the column** (nullable, `ON DELETE SET NULL`); new shifts write `NULL`. Dropping it would rewrite history and break the 043 archive semantics for no gain.
- Test note: `tests/invasion_v2/test_phase8c.py:119` asserts `scripts.get_current_head() == "052_war_attack_count"` — that assertion moves to `053_apple_pickaxes` in M3 (same pattern as the M1 bump to 043).

### 1.6 Service/config seams to reuse

| Existing seam | Reuse in M3 |
|---|---|
| `AppleWalletService.earn / debit_once` (`services/apple_wallet.py`) | Shift income stays on `earn` (keyed `apple_mine:<shift.id>`). Upgrade spends via `debit_once` with a namespaced key, e.g. `pickaxe_upgrade:{user_id}:{to_level}` — a fresh namespace, never reusing `card_upgrade_v2:*` (see `services/card_upgrade.py:30–35` warning about key namespaces). Only `applied=True` may advance the level. |
| `AppleWalletService.get_balance(session, user_id, for_update=True)` | Upgrade screen shows the live balance; the spend is re-checked inside the lock. |
| `ConfigService.get / default` (`services/config_store.py:404–435`) + `system_config` table | New key **`pickaxe_levels`** (see §2.1) with built-in defaults — admins can rebalance costs and reward ranges by editing JSON, no code (exact pattern of `card_upgrade_costs`, `services/config_store.py:72–84`). |
| `lock_user_row` + `docs/lock-order.md` | Upgrade and shift both run under the `users` mutex; pickaxe row is locked second (consistent with existing "wallet/progression after the user mutex" ranking). |
| `services/apple_mine_narrative.py` | Narrative pools + `cooldown_message` reused unchanged; one new pool (`UPGRADE_SUCCESS`) is the only prose addition. |
| `services/telegram_ui.py` (`safe_edit_message_caption`, `safe_edit_callback_message`, `safe_answer_callback`) | All three screens are caption edits on photo/text messages, exactly as today. |
| `keyboards/common.py` style map | success = work/upgrade, primary = navigation (pickaxe menu entry), danger = back (per the documented semantic: danger = back/cancel). |
| `fa_digits` (`services/sib_claim.py:42–44`) | All Persian digits in captions/costs. |

---

## 2. Task 2 — Economy Design

Sell-equivalent values (existing, from `services/config_store.py`): gold 50 · diamond 100 · obsidian 200 apples. One shift = 30 min, 8 shifts/day.

### 2.1 Proposed defaults (proposed `system_config` key `pickaxe_levels`)

```json
[
  {"level": 1, "upgrade_cost": 0,    "apples_min": 10, "apples_max": 15, "gold_pct": 7,  "diamond_pct": 2, "obsidian_pct": 1},
  {"level": 2, "upgrade_cost": 300,  "apples_min": 15, "apples_max": 22, "gold_pct": 10, "diamond_pct": 3, "obsidian_pct": 1},
  {"level": 3, "upgrade_cost": 600,  "apples_min": 21, "apples_max": 30, "gold_pct": 13, "diamond_pct": 4, "obsidian_pct": 2},
  {"level": 4, "upgrade_cost": 1000, "apples_min": 28, "apples_max": 40, "gold_pct": 16, "diamond_pct": 5, "obsidian_pct": 2},
  {"level": 5, "upgrade_cost": 1500, "apples_min": 45, "apples_max": 60, "gold_pct": 20, "diamond_pct": 6, "obsidian_pct": 3}
]
```

`upgrade_cost` = cost to **enter** that level (level 1 is free). Roll rule: draw special percentages first (gold, then diamond, then obsidian; mutually exclusive), otherwise roll `randint(apples_min, apples_max)` normal apples. Quantities unchanged from M1: 3 gold / 1 diamond / 1 obsidian.

### 2.2 The full table (costs + rewards)

| Level | Upgrade cost (normal 🍎) | Min normal 🍎/shift | Max normal 🍎/shift | 🌕 Gold | 💎 Diamond | 🌑 Obsidian | Total special chance | EV 🍎/shift (sell-equiv) | EV 🍎/day (8 shifts) |
|:---:|---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 1 | — (start) | 10 | 15 | 7% | 2% | 1% | **10%** | ≈ 25.8 | ≈ 206 |
| 2 | **300** | 15 | 22 | 10% | 3% | 1% | **14%** | ≈ 35.9 | ≈ 287 |
| 3 | **600** | 21 | 30 | 13% | 4% | 2% | **19%** | ≈ 48.2 | ≈ 385 |
| 4 | **1 000** | 28 | 40 | 16% | 5% | 2% | **23%** | ≈ 59.2 | ≈ 473 |
| 5 | **1 500** | 45 | 60 | 20% | 6% | 3% | **29%** | ≈ 79.3 | ≈ 634 |

EV formula per level: `(1 − special) × mean(min, max) + gold% × 3 × 50 + diamond% × 1 × 100 + obsidian% × 1 × 200`.

Worked examples:
- **L1**: 0.90 × 12.5 = 11.25 · 0.07 × 150 = 10.5 · 0.02 × 100 = 2.0 · 0.01 × 200 = 2.0 → **25.75**
- **L5**: 0.71 × 52.5 = 37.28 · 0.20 × 150 = 30.0 · 0.06 × 100 = 6.0 · 0.03 × 200 = 6.0 → **79.28**

### 2.3 Pacing and payback

Total ladder cost: **3 400 normal apples** (300 + 600 + 1 000 + 1 500), i.e. ~55% of the 6 200-apple card-upgrade ladder — a personal toolchain shorter than a single card's full progression, as intended.

Primary scenario: a dedicated player (8 shifts/day) sells special apples to fund upgrades:

| Step | Cost | Income while at level | Time to pay (shifts ≈ days) |
|:---:|---:|---:|---:|
| 1 → 2 | 300 | 206/day | ~29 shifts ≈ **1.5 days** |
| 2 → 3 | 600 | 287/day | ~49 shifts ≈ **2.1 days** |
| 3 → 4 | 1 000 | 385/day | ~91 shifts ≈ **2.6 days** |
| 4 → 5 | 1 500 | 473/day | ~124 shifts ≈ **3.2 days** |
| **Total** | **3 400** | — | **~9–10 days to max** |

Static payback per step (cost ÷ EV gained per shift): 29 / 49 / 91 / 75 shifts — every step is clearly *worth* doing, and later steps keep absolute EV growth positive (+10.2 / +12.2 / +11.0 / +20.1 apples per shift). Holding scenario (player saves special apples for card upgrades and pays the ladder from normal apples only): first upgrade ~3.3 days, full ladder ~3 weeks — still "meaningful, not impossible."

Anchors against the existing economy (`services/config_store.py` comments, audited 2026-10-04):
- Casual income baseline ≈ 85–135 apples/day (garden + 5-apple wake claim). Mine at L1 (206/day sell-equiv, ~90/day normal-only) already complements rather than replaces it; the mine becomes the *top* income sink-sourcer only after investment.
- The 300-apple first upgrade ≈ 2–3 days of casual income — reachable without special-apple sales for a casual; near-instant for a miner.
- L5 income (≈634/day sell-equiv) deliberately sits above a fully invested garden day, making a maxed pickaxe the natural end-state of the mine loop.

### 2.4 Deliberate nerf of Level 1 (decision to ratify)

The current flat table pays ≈ 64.5 apples/shift to everyone. New **L1 pays ≈ 25.8** (−60%) — on purpose: the rework's hook is that the bare mine is weak and the pickaxe is what makes it great. **L5 pays ≈ 79.3 (+23% vs today)**, so the end state is a real reward, and special apples (which also feed the existing card-upgrade demand) carry most of the L1 value so players still *feel* the mine. If this arc is too steep, the single-knob fix is raising L1's range to 12–18 and costs down 10–20% — all via the `pickaxe_levels` config, no code.

### 2.5 Anti-exploit / consistency rules (for M3)

- Upgrade and shift run under the same `users` row lock; upgrade re-validates level and balance under the pickaxe `FOR UPDATE` read. A stale `mine:pickaxe:upgrade` press (level already changed) re-renders instead of double-charging — same `StaleUpgradeError` pattern as `services/card_upgrade.py:78–83`.
- Idempotency key `pickaxe_upgrade:{user_id}:{to_level}` makes a duplicated press a no-op ledger replay, never a double level-up.
- Reward roll happens **after** the cooldown check, inside the shift transaction; `apple_mine_shift.pickaxe_level` records the level actually used.

---

## 3. Task 3 — UI/UX Flow

All captions are Persian, digits via `fa_digits`, `parse_mode=None` (consistent with the current mine handler). Button semantics per `keyboards/common.py`: success = work/upgrade, primary = navigation, danger = back. Callback data: `mine:work` (existing, now performs the shift directly), `mine:pickaxe`, `mine:pickaxe:upgrade`, `mine:pickaxe:back`. The `mine:card:<id>` callback and the card-list screen are removed.

### 3.1 Screen 1 — Main mine menu

Sent as `answer_photo` (existing `mine.jpg`, 1280×720). The presser's own level and cooldown are rendered per user (caption is personalized on open and on every return from another screen).

**Caption (pickaxe level L, cooldown 0):**

```
⛏ معدن سیبی

داخل معدن تاریک است. صدای چکش از دور می‌آید.

🔨 کلنگ شما: سطح ۲
⛏ هر شیفت: ۱۵ تا ۲۲ سیب معمولی
🍎 شانس سیب ویژه: ۱۴٪

⏳ معدن آماده‌ی کار است!
```

**Caption (cooldown active — reuses the existing `COOLDOWN` narrative pool):**

```
⛏ معدن سیبی

داخل معدن تاریک است. صدای چکش از دور می‌آید.

🔨 کلنگ شما: سطح ۲
⏳ ⛏ دیوارهای معدن هنوز خاموش‌اند. ۲۳ دقیقه و ۱۵ ثانیه دیگر برگرد.
```

**Keyboard (one row, two buttons):**

| Button | Style | Callback |
|---|---|---|
| `⛏ کار کردن در معدن` | success (green) | `mine:work` |
| `🔨 ارتقا کلنگ` | primary (blue) | `mine:pickaxe` |

`mine:work` now performs the shift immediately (no card list). Cooldown press → existing alert via `cooldown_message`.

### 3.2 Screen 2 — Pickaxe upgrade menu

Sent as a **new** `answer_photo` (or caption edit when reached from a photo) using the current level's image `assets/apple_mine/pickaxe_<L>.jpg` (1280×720).

**Caption (level L < 5, balance B, next-level stats):**

```
🔨 ارتقا کلنگ

📊 سطح فعلی: ۲ از ۵
⛏ سیب معمولی هر شیفت: ۱۵ تا ۲۲
🍎 شانس سیب ویژه: ۱۴٪

⬆️ با ارتقا به سطح ۳:
⛏ سیب معمولی هر شیفت: ۲۱ تا ۳۰
🍎 شانس سیب ویژه: ۱۹٪

💰 هزینه ارتقا: ۶۰۰ سیب 🍎
💰 سیب‌های شما: ۳۵۰
```

**Keyboard:**

| Button | Style | Callback |
|---|---|---|
| `🍏 ارتقا (۶۰۰ 🍎)` | success (green) | `mine:pickaxe:upgrade` |
| `🔙 بازگشت` | danger (red) | `mine:pickaxe:back` |

**Max-level variant (L = 5):** the "با ارتقا به سطح بعدی" block and the upgrade button disappear; the caption starts with `🏆 کلنگ شما به بالاترین سطح (۵) رسیده است!` and the keyboard is only `[ 🔙 بازگشت ]`.

**Upgrade outcomes:**

- *Insufficient funds* → alert (no state change): `سیب کافی نداری 🍎 به ۲۵۰ سیب دیگر نیاز داری.` (shortfall = cost − balance, Persian digits). The screen stays unchanged.
- *Success* → the same photo message is edited to the **new level's image** with caption:

```
🎉 کلنگ به سطح ۳ ارتقا یافت!

📊 سطح فعلی: ۳ از ۵
⛏ سیب معمولی هر شیفت: ۲۱ تا ۳۰
🍎 شانس سیب ویژه: ۱۹٪

💰 سیب‌های شما: ۷۵۰
```

  (one of the new `UPGRADE_SUCCESS` narrative lines may prefix the `🎉` line; keyboard as in the normal variant, now showing the *next* cost).
- *Stale press / level changed concurrently* → re-render the screen (no alert), matching `StaleUpgradeError` behavior.
- `mine:pickaxe:back` → re-render Screen 1 (photo `mine.jpg`, two-button keyboard, personal caption).

### 3.3 Screen 3 — Work result (after a shift)

Same message, caption edited, keyboard cleared — exactly the M1 result pattern, enriched with two planning lines.

**Normal outcome (narrative from the existing `NORMAL` pool):**

```
چند سیب معمولی از دل سنگ بیرون آمد؛ دست خالی برنگشتی.

🍎 +۱۸ سیب معمولی
🔨 کلنگ سطح ۲
⏳ شیفت بعدی: ۲۹ دقیقه و ۵۹ ثانیه
```

**Special outcome (narrative from `GOLDEN` / `DIAMOND` / `OBSIDIAN` pool):**

```
نوری طلایی از شکاف سنگ تابید؛ سیب طلایی درخشید.

🌕 +۳ سیب طلایی
🔨 کلنگ سطح ۲
⏳ شیفت بعدی: ۲۹ دقیقه و ۵۹ ثانیه
```

The `+N` line keeps the M1 shape (`item_emoji` + `fa_digits(amount)` + `item_label`), so existing narrative tests stay valid. The two added lines are new.

### 3.4 State/error matrix (all presser-specific, group-safe)

| State | `mine:work` | `mine:pickaxe` | `mine:pickaxe:upgrade` |
|---|---|---|---|
| Cooldown active | alert with remaining mm:ss | allowed (viewing/upgrading never needs the cooldown) | allowed |
| Balance < cost | n/a | n/a | shortfall alert, screen unchanged |
| Max level | n/a | max variant, back only | n/a (button absent) |
| DB/Telegram failure | `MINE_FAILURE_TEXT` (existing) | same failure text | same failure text |
| Unregistered presser | existing registration flow | same | same |

---

## 4. Task 4 — AI Image Plan

### 4.1 Format decision: **1280×720 (16:9 landscape)**

- The existing mine panel asset `assets/apple_mine/mine.jpg` is 1280×720 (M1-REPORT) and the castle project standardized on the same banner size. Telegram scales photos to the chat's content width; 16:9 keeps the photo short in the chat column so the caption and buttons stay visible without a long vertical scroll, whereas a 1024×1024 square would occupy ~1.78× the vertical space for the same width.
- Consistency: the pickaxe screens share the visual grammar of the mine screens (same aspect, same dark-fantasy palette, same art style) so switching between `mine.jpg` and `pickaxe_<L>.jpg` feels like one continuous UI.
- Files (M3 generates; nothing in this phase): `assets/apple_mine/pickaxe_1.jpg` … `pickaxe_5.jpg`, JPEG quality ≈ 95, RGB, no text/watermark in-art.
- Fallback rule (mirrors M1): if a pickaxe image is missing/unreadable at render time, send a text-only panel with the same caption — the screen must never break over an asset.

### 4.2 Style anchor (append to every prompt)

> dark fantasy underground mine, dim warm lantern light, cinematic digital painting, game item showcase, single hero item centered, slightly tilted, no people, no text, no watermark, 1280x720

### 4.3 The five prompts

**Level 1 — Rusty Iron (base)**
> A battered, rusted iron pickaxe with a cracked, splintered wooden handle, dull and heavily worn, chipped head with deep orange-brown rust patches, leaning against rough grey mine rock, scattered pebbles and dust, dim warm lantern light, dark fantasy underground mine, muted brown, rust and grey palette, cinematic digital painting, game item showcase, single hero item centered, slightly tilted, no people, no text, no watermark, 1280x720

**Level 2 — Forged Steel (reliable)**
> A clean, well-forged steel pickaxe with a smooth oiled oak handle, polished silver head with a faint blue-white shine, subtle hand-hammered texture on the metal, a small leather-wrapped grip, leaning against dark mine rock, soft lantern light with gentle reflections, dark fantasy underground mine, cool steel grey and warm wood tones, cinematic digital painting, game item showcase, single hero item centered, slightly tilted, no people, no text, no watermark, 1280x720

**Level 3 — Runed Bronze & Amethyst (enchanted)**
> An ornate bronze-and-silver pickaxe inlaid with small amethyst crystals that glow faintly purple, glowing violet runes engraved along the head, a few thin wisps of violet magic drifting around it, worn but noble craftsmanship, leaning against dark mine rock with faint crystal clusters in the background, dark fantasy underground mine, rich bronze, purple and silver palette, cinematic digital painting, game item showcase, single hero item centered, slightly tilted, no people, no text, no watermark, 1280x720

**Level 4 — Glowing Mythril (rare)**
> A glowing mythril pickaxe, silvery-blue magical metal humming with soft azure energy, veins of cyan light running through the head and down the shaft, floating glowing sparks and faint energy arcs around the tip, ornate silver filigree on the crossguard, leaning against dark mine rock, the rock faintly lit by the cyan glow, dark fantasy underground mine, luminous cyan, silver and deep blue palette, epic game item showcase, single hero item centered, slightly tilted, no people, no text, no watermark, 1280x720

**Level 5 — Cosmic Legend (mythic)**
> A legendary cosmic pickaxe forged from black star-metal and encrusted with large floating cosmic gems, blazing golden-white magic swirling around the head, tiny galaxies, stars and nebulae visible inside the gems, a radiant godlike golden aura and light rays, the surrounding mine rock scorched and glowing from the intense light, dark fantasy underground mine, deep violet, gold and white-hot palette, ultimate epic game item showcase, single hero item centered, slightly tilted, no people, no text, no watermark, 1280x720

Progression read (for the art reviewer): rust → polished steel → purple enchantment → cyan mythril glow → golden cosmic legend. Each level must read as a clear, at-a-glance step above the previous one at Telegram thumbnail size.

---

## 5. M3 implementation sketch (out of scope here — roadmap only)

1. **Migration `053_apple_pickaxes`** (§1.5) + ORM model `database/models/apple_mine.py` (co-located with `AppleMineShift`); bump the migration-head assertion in `tests/invasion_v2/test_phase8c.py`.
2. **`services/apple_mine.py` changes**: replace `OUTCOMES` with a per-level table built from `ConfigService` key `pickaxe_levels` (defaults in `services/config_store.py`); `perform_shift` reads the pickaxe level (lazy `get_or_create`), rolls the level table, stores `pickaxe_level` on the shift; drop the card-ownership branch (keep `card_id=None`); new `upgrade_pickaxe(...)` using `debit_once` + stale-level re-render; new `PickaxeLevel`/`InsufficientFunds`-style errors.
3. **`handlers/apple_mine.py` changes**: two-button main keyboard; `mine:work` performs the shift directly (delete `_card_keyboard`, card constants, `mine:card:<id>` route, `NO_CARDS_TEXT`); add `mine:pickaxe` / `mine:pickaxe:upgrade` / `mine:pickaxe:back` routes and the three screen captions from §3; result caption gains level + next-cooldown lines.
4. **Assets**: generate `pickaxe_1..5.jpg` (§4), register fallback text panel.
5. **Tests**: level roll distribution per level (10k rolls), upgrade cost/debit/idempotency/stale-press, max-level UI, config-override parsing, cooldown unchanged, shift snapshot column, migration upgrade/downgrade, and the usual group-safe/presser-specific checks.

## 6. Git history

```
$ git log --oneline -5
154307c Merge pull request #77 from canoheil/arena/11b32a94-sib
```

(branch `arena/385c6fca-sib` contains a single squashed commit at inspection time; this recon doc is committed as the next commit on that branch.)
