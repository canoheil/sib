# C2-2-REPORT — Apple Clash: Database, Energy System, and Declarations

**Feature:** جدال سیبی (Apple Clash) — Phase C2-2 Implementation  
**Date:** 2026-10-18  
**Branch:** `arena/94815b63-sib`

---

## 1. Migration ID & Database Architecture

* **Migration Revision:** `050_apple_clash`
* **Down Revision:** `049_apple_castles`
* **Migration Script Path:** `database/migrations/versions/050_apple_clash.py`

### Schema Changes Summary

1. **`group_castles` Extension:**
   * `shield_until`: `DateTime(timezone=True)`, nullable (stores the 48-hour Peace Shield expiry).
   * `in_war`: `Boolean`, not null, default `False` (server_default `"0"`, fast gate for matchmaking & locking).
2. **`group_wars` Table:**
   * `id`: `BigInteger` (SQLite `Integer` variant), autoincrement primary key.
   * `attacker_id`: `BigInteger`, foreign key referencing `group_castles(group_id)`.
   * `defender_id`: `BigInteger`, foreign key referencing `group_castles(group_id)`.
   * `status`: `String(20)`, not null, default `"active"` (check constraint: `'active'`, `'finished'`).
   * `attacker_damage`: `Integer`, not null, default `0`.
   * `defender_damage`: `Integer`, not null, default `0`.
   * `started_at`: `DateTime(timezone=True)`, not null.
   * `ends_at`: `DateTime(timezone=True)`, not null (started_at + 24 hours).
   * `winner_id`: `BigInteger`, nullable foreign key referencing `groups(id)`.
   * `looted_apples`: `Integer`, not null, default `0`.
   * Indexes: `ix_group_wars_status_ends_at(status, ends_at)`, `(attacker_id, status)`, `(defender_id, status)`.
3. **`war_participants` Table:**
   * `war_id`: `BigInteger`, primary key component, foreign key referencing `group_wars(id)`.
   * `user_id`: `BigInteger`, primary key component, foreign key referencing `users(id)`.
   * `group_id`: `BigInteger`, not null, foreign key referencing `groups(id)`.
   * `energy`: `Integer`, not null, default `100` (server_default `"100"`).
   * `total_damage`: `Integer`, not null, default `0`.
   * `last_attack_at`: `DateTime(timezone=True)`, nullable (enforces 1-minute cooldown).
   * Index: `ix_war_participants_war_group(war_id, group_id)`.
4. **`war_attacks` Table:**
   * `id`: `BigInteger` (SQLite `Integer` variant), autoincrement primary key.
   * `war_id`: `BigInteger`, foreign key referencing `group_wars(id)`.
   * `user_id`: `BigInteger`, foreign key referencing `users(id)`.
   * `source_group_id`: `BigInteger`, foreign key referencing `groups(id)`.
   * `target_group_id`: `BigInteger`, foreign key referencing `groups(id)`.
   * `damage`: `Integer`, not null.
   * `is_buff_used`: `Boolean`, not null, default `False`.
   * `created_at`: `DateTime(timezone=True)`, not null, server_default `func.now()`.
   * Indexes: `(war_id, user_id)`, `(war_id, source_group_id)`.

Models are declared in `database/models/war.py` and exported through `database/models/__init__.py`.

---

## 2. Healing Potion Item ("معجون شفا")

* **Item Key:** `"healing_potion"`
* **Emoji:** `🧪`
* **Label:** `"معجون شفا"`
* **Description:** `"با نوشیدن این معجون، ۵۰ واحد از انرژی جنگی خود را در جنگ‌ها بازیابی می‌کنید."`
* **Catalog Registration:** `services/items.py` (`ITEM_CATALOG`).
* **Price Configuration:** Added `"healing_potion": 150` to `item_buy_prices` in `services/config_store.py` (`CONFIG_DEFAULTS`).
* **Item Artwork:** Generated and deployed at `assets/items/healing_potion.jpg`.
* **Gameplay Effect:** When consumed in a war (`war:potion:{war_id}:{group_id}`), restores 50 War Energy (capped at maximum 100).

---

## 3. Image Generation Status (1280×720 AI Backgrounds)

All 4 required AI background images were generated and verified at exact `1280×720` dimensions using Lanczos resampling where appropriate:

| File Path | Dimensions | Format | Status | Visual Style |
|---|---|---|---|---|
| `assets/apple_clash/war_bg_declaration.jpg` | 1280×720 | JPEG | Deployed | Two citadels facing each other across a misty mountain gorge under dramatic sunset amber skies with fluttering war banners. |
| `assets/apple_clash/war_bg_attacker.jpg` | 1280×720 | JPEG | Deployed | Offensive siege perspective overlooking an enemy fortress with green apple catapults, drifting smoke, and amber evening sky. |
| `assets/apple_clash/war_bg_defender.jpg` | 1280×720 | JPEG | Deployed | High castle ramparts perspective looking out over parapets into twilight indigo skies with glowing emerald defensive runes. |
| `assets/apple_clash/war_bg_victory.jpg` | 1280×720 | JPEG | Deployed | Sunlit morning celebration over fortress terraces, fluttering banners, crates of glowing apples and gems in triumphant light. |

---

## 4. Button Style Confirmation (Strict Glass UI Compliance)

All buttons across both `handlers/castle.py` and `handlers/war.py` strictly adhere to the semantic color system (`keyboards/common.py`):

* **Success (Green — `success_button`):**
  * `[ ⚔️ شروع حمله ]` (`castle:war_start:{group_id}`) in Castle Admin menu.
  * `[ 🚀 شروع حمله ]` (`castle:war_confirm:{group_id}:{target_id}`) in War Confirmation dialog.
  * `[ ⚔️ حمله به قلعه ]` (`war:atk:{war_id}:{group_id}:normal`) for Attacker clan.
  * `[ 🛡 دفاع از قلعه ]` (`war:atk:{war_id}:{group_id}:normal`) for Defender clan.
* **Primary (Blue — `primary_button`):**
  * Target selection buttons: `[ 🏰 نام قلعه (سطح N) ]` (`castle:war_pick:{group_id}:{target_id}`).
  * `[ 💥 حمله با سیب باف ]` (`war:atk:{war_id}:{group_id}:buff`) for Attacker clan.
  * `[ 💥 دفاع با سیب باف ]` (`war:atk:{war_id}:{group_id}:buff`) for Defender clan.
  * `[ 🧪 استفاده از معجون شفا ]` (`war:potion:{war_id}:{group_id}`) for both sides.
* **Danger (Red — `danger_button`):**
  * `[ 🔙 بازگشت ]` / `[ 🔙 انصراف ]` (`castle:view:{group_id}`).

---

## 5. Declaration & War Mechanics

### 5.1 Admin-Only Initiation & Matchmaking
* Initiated via `[ ⚔️ شروع حمله ]` from `قلعه سیبی`.
* Admin status verified directly via Telegram `get_chat_member`: `member.status in ("creator", "administrator")`. Non-admins receive an immediate alert.
* Matchmaking queries candidate enemy groups with castle level $\Delta \le \pm 3$, `is_active == True`, `in_war == False`, and active Peace Shield check (`shield_until IS NULL OR shield_until <= UTC_NOW`). Up to 5 groups are presented.

### 5.2 Atomic Declaration & Dual Notifications (NO PIN)
* Both castles are locked in ascending group ID order with `with_for_update()`.
* Creates `group_wars` with a 24-hour expiration (`ends_at = started_at + timedelta(hours=24)`).
* Stamped `in_war = True` on both castles.
* Dispatches `war_bg_declaration.jpg` with loud emojis (🚨) and a red back button to **both** attacker and defender groups.
* **Pinning Conflict Resolved:** In accordance with the prompt specification, declaration and attack messages are **never pinned** to preserve the pinned message slot for the Invasion feature.

### 5.3 War Status Menu (`وضعیت جنگ`) & Combat Rules
* Triggered in group chats by sending `وضعیت جنگ`.
* Displays clan damage scores, remaining countdown, and player's current energy.
* **Energy System:** 100 max energy, 25 energy consumed per attack (4 natural attacks).
* **Cooldown:** 1-minute minimum cooldown between hits (`last_attack_at` timestamp check).
* **Damage Pipeline:**
  1. Base power from player's active Main Card.
  2. Castle buff from `CastleService.get_invasion_buff(group_id)`.
  3. If Sib-Buff selected: damage doubled (`damage *= 2`) and 1 `sib_buff` item consumed.
  4. Damage is credited to the proper clan pool: `attacker_damage` if player's group is attacker, or `defender_damage` if defender.
  5. Audit entry logged in `war_attacks` and aggregated in `war_participants`.

---

## 6. Test Suite & Verification Results

All 9 targeted feature tests pass:

```
tests/test_war.py::test_healing_potion_added_to_shop PASSED
tests/test_war.py::test_declaration_requires_admin PASSED
tests/test_war.py::test_declaration_creates_war_and_sets_in_war_flag PASSED
tests/test_war.py::test_attack_costs_25_energy PASSED
tests/test_war.py::test_attack_fails_without_enough_energy PASSED
tests/test_war.py::test_attack_cooldown_enforced PASSED
tests/test_war.py::test_healing_potion_restores_50_energy PASSED
tests/test_war.py::test_damage_goes_to_correct_pool PASSED
tests/test_war.py::test_war_buttons_have_correct_colors PASSED
```

### Full Regression Run
Running `test_war.py`, `test_castle.py`, `test_shop.py`, `test_assets_items.py`, `test_phase8c.py`, and `test_migrations.py`:
**135 passed in 12.90s.**

---

## 7. Git Log (`git log --oneline -5`)

```
a09d672 [c2-1] design and recon for apple clash
94fc4cc Merge pull request #71 from canoheil/arena/1450aebb-sib
```
