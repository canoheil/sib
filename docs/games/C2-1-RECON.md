# C2-1-RECON — Apple Clash (جدال سیبی): Design & Architecture

**Feature:** جدال سیبی (Apple Clash) — 24-Hour Group vs Group War System  
**Phase:** C2-1 (Reconnaissance and Design Only — NO APPLICATION CODE)  
**Date:** 2026-10-18  
**Target Environment:** Python 3.12 / aiogram 3 / SQLAlchemy 2 (asyncio) / MariaDB (production) / SQLite (tests)

---

## Executive Summary

"جدال سیبی" (Apple Clash) introduces asynchronous, high-stakes clan warfare between Telegram groups in the Sib bot ecosystem. Built upon the foundation of the Apple Castle system (`قلعه سیبی`) and borrowing battle-tested patterns from `Invasion v2`, Apple Clash pits two balanced groups against each other in a 24-hour siege. 

Group administrators initiate a war from their castle menu, select an evenly matched opponent (within $\pm 3$ castle levels), review the consequences, and declare war. Once declared, both group chats receive real-time notifications and players mobilize by attacking with their Main Cards. Every attack produces distinct, dual-perspective narrative battle dispatches and dynamic visual battle cards composed via Pillow over pre-rendered AI backgrounds. After 24 hours, the clan with the highest cumulative damage claims victory, looting a percentage of the defeated group's donation progress toward their next castle level, while the defeated group gains temporary immunity via a Peace Shield (سپر صلح).

---

## TASK 1 — Codebase Reconnaissance

Detailed file and line references for all relevant systems in the existing repository:

### 1. Castle Service & Progression Storage

* **Castle State Model (`database/models/castle.py:21–34`):**
  * Stored in the `group_castles` table via `GroupCastle`.
  * Primary key is `group_id` (`BigInteger`, foreign key referencing `groups.id`).
  * Current castle level is stored in `level` (`Integer`, default `1`, constrained by `CheckConstraint("level BETWEEN 1 AND 10")`).
  * Cumulative apple contributions are stored in `total_donated` (`Integer`, default `0`, constrained by `CheckConstraint("total_donated >= 0")`).
* **Donations Model (`database/models/castle.py:37–50`):**
  * Stored in the `castle_donations` table via `CastleDonation`.
  * Tracks individual user contributions: `user_id` (`BigInteger`, FK `users.id`), `group_id` (`BigInteger`, FK `group_castles.group_id`), `amount` (`Integer`), `donated_at` (`DateTime(timezone=True)`), and `request_key` (`String(100)`, unique).
* **Daily Income Claim Model (`database/models/castle.py:53–64`):**
  * Stored in `castle_income_claims` (`CastleIncomeClaim`) with unique constraint `(user_id, claim_day)`.
* **Castle Domain Service (`services/castle_service.py`):**
  * `THRESHOLDS` defined at lines `12–23`: `(0, 5_000, 20_000, 50_000, 110_000, 210_000, 360_000, 580_000, 880_000, 1_300_000)`.
  * `level_for_total(total)` at lines `28–29`: calculates level based on cumulative donations.
  * `get_or_create_castle(session, group_id)` at lines `37–62`: handles atomic upsert without locking the `Group` row to avoid lock-ordering deadlocks.
  * `donate(...)` at lines `79–130`: enforces the 500 apples/day user cap, debits the user's wallet via `AppleWalletService.debit_once`, increments `castle.total_donated`, and updates `castle.level`.
  * `get_invasion_buff(session, group_id)` at lines `132–139`: returns `level * 0.02` (+2% damage per level, up to +20%).
  * `claim_daily_income(...)` at lines `141–168`: provides daily income of `level * 2` normal apples.
* **UI & Entry Point (`handlers/castle.py`):**
  * Exact command trigger: `F.text == "قلعه سیبی"` (`handlers/castle.py:76–81`).
  * Menu rendering: `show_castle(...)` (`handlers/castle.py:57–73`), loading level art `assets/castle/level_{level}.jpg` via `services/castle_art.py`.
  * Castle keyboard: `keyboard(group_id, donate=False)` (`handlers/castle.py:23–34`).

### 2. Invasion v2: 24-Hour Lifecycle, Joining, and Pillow Composition

* **Lifecycle Storage (`database/models/invasion_v2.py:44–80`):**
  * `Inv2Invasion` table stores `status` (`String(20)`), `scheduled_at`, `started_at`, `ends_at`, `enemy_hp`, `enemy_hp_remaining`, and `occurrence_key`.
* **Lifecycle Transitions:**
  * **Spawning:** `InvasionScheduler._spawn_if_due` (`services/invasion_v2/scheduler.py:111–228`) creates an invasion with `status = "pending"`.
  * **Activation & 24h Timer:** `InvasionPresentation.publish_pending` (`services/invasion_v2/presentation.py:326–333`) posts the announcement banner to Telegram, setting `inv.started_at = now`, `inv.ends_at = now + timedelta(seconds=window)` (where window defaults to 86,400s / 24 hours via `invasion_join_window_seconds`), and sets `inv.status = "active"`.
  * **Expiration / Finalization:** `InvasionScheduler._finalize_due` (`services/invasion_v2/scheduler.py:65–90`) queries active invasions where `ends_at <= now` with row locks, marking them `VICTORY` if HP is depleted or `DEFEAT` if time expired.
* **Attack / Joining Flow (`handlers/inv2_join.py:39–128`):**
  * Checks feature flag via `MaintenanceService._invasion_enabled()`.
  * Locks invasion row: `select(Inv2Invasion).where(...).with_for_update()` (line `58`).
  * Rejects duplicate participation: `select(Inv2Attack.id).where(Inv2Attack.invasion_id == inv.id, Inv2Attack.user_id == user_id)` (lines `67–70`).
  * Resolves user's active Main Card: `PlayerCardService.get_main_card` (line `77`) and calculates power via `card_attributes(card, character).power` (line `85`).
  * Computes damage via `compute_damage(power, rng)` (`services/invasion_v2/damage.py`) and buffs it via `apply_castle_buff` (lines `86–88`).
  * Atomically decrements remaining HP via CAS update (`update(Inv2Invasion).where(...)`, lines `99–106`).
  * Records attack row: `Inv2Attack` (`database/models/invasion_v2.py:83–102`).
  * Dispatches delivery task asynchronously: `asyncio.create_task(deliver())` (lines `122–127`).
* **Pillow Image Composition (`services/invasion_v2/imaging/compose.py`):**
  * **Pre-generated backgrounds:** Loads static background JPEGs from disk (`templates.load_layout("join")`, line `247`). Never renders background mountains, skies, or terrain procedurally.
  * **Card Placement (`services/invasion_v2/imaging/compose.py:214–230`):**
    * Opens card art from file bytes, resizes with `ImageOps.contain` into `(420, 580)`.
    * Centers card at `x = (1280 - card.width) // 2`, `y = 340 - card.height // 2`.
    * Applies rounded rectangle mask (radius 16px), dark outer border (radius 20px, `#1A0E06`), and a 16px Gaussian blurred drop shadow.
  * **Persian Text Shaping & Shadow (`compose.py:73–100`):**
    * Shapes text with `libraqm` (if installed) or `arabic_reshaper` + `python-bidi`.
    * Renders drop shadow on a separate alpha layer with `ImageFilter.GaussianBlur(shadow_blur)` before stamping the stroked text.
  * **Damage Number & Glow (`compose.py:102–119`, `267–275`):**
    * Center coordinates: `(640, 648)`, anchor `ms`, font size 160pt.
    * Calls `_glow(...)` (dilated and Gaussian blurred text layer with opacity `0.40–0.60`).
    * Formats numbers in Persian numerals with comma thousands separators via `_digits(damage)`.

### 3. Sib-Buff Damage Mechanics

* **Item Identity:** Item key `"sib_buff"` managed by `services/items.py` (`ItemService`).
* **Verification & Locking (`handlers/inv2_join.py:70–76`):**
  * Locks all relevant user rows using `lock_user_rows_in_order(session, [...])` before inventory checks.
  * Verifies inventory under lock: `ItemService.get_total(session, user_id, "sib_buff", for_update=True) < 1`. If 0, alerts user: `"شما سیب باف ندارید! از فروشگاه تهیه کنید."`
* **Damage Multiplier Pipeline (`handlers/inv2_join.py:86–92`):**
  1. Base damage computed from Main Card power: `damage, crit = compute_damage(power, random.Random())`.
  2. Castle buff applied first: `damage = apply_castle_buff(damage, castle_buff)` (i.e., `int(damage * (1 + level * 0.02))`).
  3. If Sib-Buff is used: `damage *= 2` (doubles the resulting integer damage).
* **Atomic Consumption (`handlers/inv2_join.py:108–109`):**
  * Consumes 1 item only after the database update succeeds: `await ItemService.consume(session, user_id, "sib_buff", 1)`.

### 4. Group Administrator Verification

* **Bot Admin vs Group Admin:**
  * `admin/auth.py:13` (`is_admin`) checks `settings.ADMIN_IDS`, which is strictly for **bot super-administrators**, not Telegram group admins.
* **Telegram Group Admin Verification Pattern (`handlers/castle.py:101–105`):**
  * Queries Telegram API directly: `member = await callback.bot.get_chat_member(chat_id=group_id, user_id=callback.from_user.id)`.
  * Telegram returns a `ChatMember` object. An admin or owner has:
    ```python
    member.status in ("creator", "administrator")
    ```
  * In aiogram 3, these correspond to `ChatMemberOwner` and `ChatMemberAdministrator`. Standard members have `status == "member"`, while users who left or were kicked have `"left"` or `"kicked"`.
  * For Apple Clash war initiation, only callers with `member.status in ("creator", "administrator")` are permitted to select targets and confirm declarations.

### 5. Background Tasks & Timers

* **Central Maintenance Worker (`services/maintenance/service.py`):**
  * Periodic worker loop runs every 30 seconds (`MaintenanceService.INTERVAL_SECONDS = 30`, line `44`).
  * `MaintenanceService.run_forever` (line `543`) runs in a persistent background task, calling `run_once()` (line `386`).
* **Invasion Sweep Execution (`services/maintenance/service.py:443–463`):**
  * Calls `InvasionScheduler.tick()`, which scans for expired or completed records using `select(Inv2Invasion).where(Inv2Invasion.ends_at <= now).with_for_update()`.
  * If terminal events are detected, triggers settlement (`settle_outcome`) and message updates (`presenter.finalize`).
* **Timer Architecture Principle:**
  * Timers are **never** implemented as volatile in-memory `asyncio.sleep(86400)` tasks or OS cron jobs.
  * The deadline is stamped durably in the database (`ends_at = started_at + interval`).
  * Periodic sweeps in `MaintenanceService` query overdue rows with `with_for_update()`, guaranteeing recovery across bot restarts, worker crashes, and deployments.

---

## TASK 2 — Database Schema & Concurrency Design

### 2.1 Proposed Schema

#### Table 1: `group_wars`
Represents an active or historical 24-hour clash between two groups.

```sql
CREATE TABLE group_wars (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    attacker_id BIGINT NOT NULL,
    defender_id BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    attacker_hp INT NOT NULL DEFAULT 100000,
    defender_hp INT NOT NULL DEFAULT 100000,
    attacker_damage INT NOT NULL DEFAULT 0,
    defender_damage INT NOT NULL DEFAULT 0,
    started_at DATETIME NOT NULL,
    ends_at DATETIME NOT NULL,
    winner_id BIGINT NULL,
    looted_apples INT NOT NULL DEFAULT 0,
    attacker_message_id BIGINT NULL,
    defender_message_id BIGINT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    CONSTRAINT fk_group_wars_attacker FOREIGN KEY (attacker_id) REFERENCES group_castles (group_id),
    CONSTRAINT fk_group_wars_defender FOREIGN KEY (defender_id) REFERENCES group_castles (group_id),
    CONSTRAINT fk_group_wars_winner FOREIGN KEY (winner_id) REFERENCES groups (id),
    CONSTRAINT ck_group_wars_different_groups CHECK (attacker_id != defender_id),
    CONSTRAINT ck_group_wars_status CHECK (status IN ('active', 'finished', 'cancelled')),
    INDEX ix_group_wars_status_ends_at (status, ends_at),
    INDEX ix_group_wars_attacker_status (attacker_id, status),
    INDEX ix_group_wars_defender_status (defender_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

*Note on test dialects:* In SQLite test environments, `id` uses `BigInteger().with_variant(Integer(), "sqlite")`.

#### Table 2: `war_attacks`
Records every attack action performed by individual players during the war.

```sql
CREATE TABLE war_attacks (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    war_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    source_group_id BIGINT NOT NULL,
    target_group_id BIGINT NOT NULL,
    card_id BIGINT NOT NULL,
    damage INT NOT NULL,
    is_crit BOOLEAN NOT NULL DEFAULT FALSE,
    is_buff_used BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT fk_war_attacks_war FOREIGN KEY (war_id) REFERENCES group_wars (id),
    CONSTRAINT fk_war_attacks_user FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT fk_war_attacks_source_group FOREIGN KEY (source_group_id) REFERENCES groups (id),
    CONSTRAINT fk_war_attacks_target_group FOREIGN KEY (target_group_id) REFERENCES groups (id),
    CONSTRAINT fk_war_attacks_card FOREIGN KEY (card_id) REFERENCES player_cards (id),
    CONSTRAINT ck_war_attacks_damage CHECK (damage > 0),
    INDEX ix_war_attacks_war_user (war_id, user_id),
    INDEX ix_war_attacks_war_source_group (war_id, source_group_id),
    INDEX ix_war_attacks_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

#### Table 3: `war_participants`
Aggregates participation, attacks count, and individual leaderboard scoring per war.

```sql
CREATE TABLE war_participants (
    war_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    group_id BIGINT NOT NULL,
    total_damage INT NOT NULL DEFAULT 0,
    attack_count INT NOT NULL DEFAULT 1,
    last_attack_at DATETIME NOT NULL,
    
    PRIMARY KEY (war_id, user_id),
    CONSTRAINT fk_war_participants_war FOREIGN KEY (war_id) REFERENCES group_wars (id),
    CONSTRAINT fk_war_participants_user FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT fk_war_participants_group FOREIGN KEY (group_id) REFERENCES groups (id),
    INDEX ix_war_participants_war_group_damage (war_id, group_id, total_damage DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

#### Castle Schema Extension: Peace Shield (`group_castles`)
To support the Peace Shield mechanic and active war exclusions without complex joins:

```sql
ALTER TABLE group_castles 
    ADD COLUMN shield_until DATETIME NULL,
    ADD COLUMN in_war BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN total_wars INT NOT NULL DEFAULT 0,
    ADD COLUMN total_war_wins INT NOT NULL DEFAULT 0;
```

### 2.2 Concurrency, Row Locking & Anti-Cheat Strategy

To prevent race conditions, double attacks, concurrent war declarations, and balance desynchronizations:

1. **War Declaration Serialization:**
   * Both participating groups must be protected from concurrent declarations.
   * Lock order rule: When declaring war, lock both `group_castles` rows in ascending `group_id` order (`min_id`, `max_id`) using `SELECT ... FOR UPDATE`.
   * Verify that neither group has `in_war == True` and defender's `shield_until` is either `NULL` or `<= now`.
   * Stamped atomically: set `in_war = True` on both castles and insert `group_wars`.
2. **Attack Concurrency & Preventing Double-Attacks:**
   * **Rule:** 1 attack per player per 24-hour war (identical to Invasion v2).
   * **Lock Order:**
     1. Lock user economy mutex: `await lock_user_row(session, user_id)` (Rank 0 in `docs/lock-order.md`).
     2. Lock the active war row: `SELECT * FROM group_wars WHERE id = :war_id FOR UPDATE`.
   * **Validation under Lock:**
     * Verify `war.status == 'active'` and `now < war.ends_at`.
     * Check if user already attacked: `SELECT id FROM war_attacks WHERE war_id = :war_id AND user_id = :user_id`. If exists, refuse with `"شما قبلاً در این جدال ضربه زده‌اید."`
     * Verify player's membership in one of the warring groups via Telegram `get_chat_member`. A user belonging to the attacker group attacks the defender; a user belonging to the defender group attacks the attacker. Cross-group spitter attacks or neutral user attacks are strictly rejected.
   * **Atomic Application:**
     * If Sib-Buff is requested: verify and consume item using `ItemService.consume`.
     * Insert `war_attacks` row. A database-level `UNIQUE (war_id, user_id)` constraint serves as the final barrier against racing requests.
     * Upsert `war_participants` with `total_damage = damage`.
     * Atomically increment `war.attacker_damage` or `war.defender_damage`, and decrement corresponding castle HP (`war.defender_hp = max(0, defender_hp - damage)`).
3. **Settlement Atomic Boundaries:**
   * Handled by the periodic maintenance sweep.
   * Lock `group_wars` row `FOR UPDATE`.
   * Lock both `group_castles` in ascending ID order `FOR UPDATE`.
   * Compute winner, calculate loot, adjust `total_donated`, set loser's `shield_until = now + timedelta(hours=48)`, and clear `in_war = False` on both castles.

---

## TASK 3 — AI Backgrounds & Pillow Layout Plan

### 3.1 Critical Imaging Principle

* **Zero Procedural Painting:** Pillow will **never** draw castles, skies, battlements, landscapes, or terrain with geometric code.
* **Pre-generated AI Assets:** All visual backgrounds are static, high-resolution AI-generated assets produced in advance and stored in `assets/apple_clash/`.
* **Runtime Pillow Role:** Pillow's execution is strictly limited to:
  1. Loading the pre-generated 1280×720 AI background.
  2. Resizing and pasting the player's active Main Card (with drop shadow, rounded corners, and outer border).
  3. Drawing Persian text (player name, clan labels) using `libraqm`/`bidi` shaping with blurred black drop shadows.
  4. Rendering dynamic damage numbers with glowing radiant auras and drop shadows.
  5. Applying pre-rendered visual impact layers (e.g., golden critical flash, fire burst overlay).

### 3.2 Exact AI Background Images Required

All images must be generated at **1280×720** (16:9 ratio, standard Telegram web preview and mobile phone resolution), adhering strictly to the warm, painterly, mythological Ovarya art direction established in Phase 8A and Castle C1.

| File Path | Canvas Size | Visual Style & Theme Description |
|---|---|---|
| `assets/apple_clash/war_bg_attacker.jpg` | 1280×720 | **The Offensive Siege:** Dramatic crimson, amber, and gold sky overlooking a massive green apple valley. In the background across the mist stands an imposing fortified stone fortress with glowing watchtowers. In the foreground, glowing emerald siege catapults, war banners fluttering, drifting smoke, sparks, and embers. Open center area for card placement. |
| `assets/apple_clash/war_bg_defender.jpg` | 1280×720 | **The Citadel Ramparts:** View looking outward from atop high stone castle battlements. Fiery siege boulders fly through a deep indigo and dusky violet twilight sky. Defensive apple ballistas and magical green protective runic barriers flare on the parapets. Drifting cinder sparks. Open center area. |
| `assets/apple_clash/war_bg_declaration.jpg` | 1280×720 | **War Declaration & Clash Announcement:** Two majestic Ovarya castles facing each other across a deep misty ravine. Dramatic sunset sky torn by mystical crimson and emerald lightning bolts. War horns, heraldic banners, intense battlefield anticipation. |
| `assets/apple_clash/war_bg_victory.jpg` | 1280×720 | **Triumphant Spoils:** Radiant golden morning light breaking through dissipated battle smoke over a magnificent sunlit fortress. Banners raised high, celebratory golden aura, apple crates spilling over with shimmering gems, divine glory. |
| `assets/apple_clash/war_bg_defeat.jpg` | 1280×720 | **Peace Shield Sanctuary:** A battered, stoic stone keep bathed in a soft blue-cyan celestial energy dome (سپر صلح / Peace Shield). Fading embers floating quietly in a peaceful misty dawn, calm after the storm, protective sanctuary. |

#### Pre-Rendered Transparent Overlay Assets
* `assets/apple_clash/overlay_fire.png` (1280×720 RGBA): Transparent PNG with dynamic flying sparks, burning embers, and heat distortion around the bottom and center.
* `assets/apple_clash/overlay_impact.png` (1280×720 RGBA): Transparent radial shockwave burst for critical and Sib-Buff hits.

### 3.3 Pillow Compositing & Coordinate Layout

```
+-----------------------------------------------------------------------+ (0,0)
| [🛡 نام کلن / برچسب رویداد]                         [👤 نام بازیکن]  |
| (x=70, y=52, anchor="la")                      (x=1216, y=72, anchor="ra")
|                                                                       |
|                          +-----------------+                          |
|                          |                 |                          |
|                          |   کارت اصلی     |                          |
|                          |  (Player Card)  |                          |
|                          |    (380x520)    |                          |
|                          |                 |                          |
|                          +-----------------+                          |
|                    Card Center: (x=640, y=300)                        |
|                                                                       |
|                     [💥 24,500 آسیب]                                  |
|            (x=640, y=648, anchor="ms", 150pt Font)                    |
|                                                                       |
| [🏰 جان قلعه مدافع: ۷۵,۵۰۰]                 [⏱ زمان باقی‌مانده: ۱۸:۲۴] |
| (x=70, y=662, anchor="ls")                    (x=1216, y=662, anchor="rs")
+-----------------------------------------------------------------------+ (1280, 720)
```

1. **Card Placement & Styling:**
   * **Source:** Loaded from `assets/characters/{character_code}.jpg`.
   * **Scaling:** Resized using `ImageOps.contain` to fit a maximum of `(380, 520)` pixels.
   * **Coordinates:** Centered at `x = (1280 - card_width) // 2`, `y = 300 - (card_height // 2)`.
   * **Frame & Decoration:**
     * Rounded corner mask with radius 16px.
     * 4px outer dark border (`#1A0E06`, radius 20px).
     * Reusable Gaussian-blurred drop shadow (radius 16px, 55% opacity) offset behind the card.
2. **Overlays & Special Effects:**
   * **Sib-Buff Effect:** If `is_buff_used == True`, composites `overlay_fire.png` and draws an expanding radial orange glow (`#FF5722`) behind the card.
   * **Critical Strike Effect:** If `is_crit == True`, composites a radial golden flash layer at 25% opacity and renders "ضربهٔ کاری" badge at `(70, 95)` in gold (`#FFD24A`).
3. **Damage Number Typography:**
   * **Text:** Formatted with Persian numerals and commas (e.g., `"۲۴,۵۰۰"`).
   * **Anchor & Position:** Centered at `(640, 648)`, anchor `ms` (middle-subbaseline).
   * **Font Size:** 150pt (using `Vazirmatn-Bold.ttf`).
   * **Colors:**
     * Standard Attack: `#FCECD8` (cream white) with dark brown stroke `#2A1408`.
     * Critical Strike: `#FFD24A` (vibrant gold) with `#3A1A08` stroke.
     * Sib-Buff Attack: `#FF7043` (blazing fiery orange) with dark crimson stroke `#2E0A0A`.
   * **Glow Layer (`_glow`):** Dilated by 2px, blurred with radius 16px at 60% opacity.
   * **Shadow (`_draw_text_with_shadow`):** Blurred black drop shadow offset `(0, 4)`, blur radius 10px, opacity 85%.
4. **Header and Footer Badges:**
   * **Player Display Name:** Positioned at `(1216, 72)`, anchor `ra` (right-aligned), font size 36pt, white text with dark stroke.
   * **Perspective Badge:** Positioned at `(70, 52)`, anchor `la` (left-aligned), font size 28pt:
     * Attacker view: `"⚔️ یورش به باروهای دشمن"` (`#FF8A80`).
     * Defender view: `"🛡 دفاع از دیوارهای قلعه"` (`#80D8FF`).
   * **Footer Telemetry:** Castle remaining HP / battle score rendered cleanly at bottom corners with 30pt text.

---

## TASK 4 — Narrative Design & Dual-Perspective Engine

Every attack in Apple Clash affects both the attacking and defending communities. To maximize engagement and drama, the engine generates **two distinct perspectives** for the exact same combat event.

### 4.1 Narrative Template Variables
* `{attacker_name}`: Player's Telegram display name.
* `{card_name}`: Persian name of the player's active Main Card (e.g., `شوالیه سیب`, `سایهٔ ایگریس`).
* `{source_group}`: Title of the attacking group.
* `{target_group}`: Title of the defending group.
* `{damage}`: Formatted damage amount in Persian numerals.
* `{buff_tag}`: Dynamic suffix if Sib-Buff was consumed (e.g., `"💥 [با قدرت سیب باف]"`).

### 4.2 Pool 1: `WAR_ATTACK_ATTACKER` (Attacking Group View)
Sent to the chat of the clan initiating the attack. Perspective is heroic, aggressive, and conquest-driven.

* **Sample 1:**
  > ⚔️ **{attacker_name}** با هدایت نیروی اسطوره‌ای **{card_name}**، به قلب خطوط دفاعی **{target_group}** یورش برد! ضربه‌ای سهمگین به دروازهٔ قلعه نشست و **{damage}** خسارت ویرانگر ثبت شد! باروهای حریف زیر آتش دلاوران ما می‌لرزد! {buff_tag}
* **Sample 2:**
  > 🏹 غرش منجنیق‌ها بر فراز دشت اواریا! **{attacker_name}** در رکاب **{card_name}** صفوف مقدم **{target_group}** را در هم کوبید و **{damage}** صدمه به باروهای دشمن وارد آورد! پیشروی با قدرت ادامه دارد! {buff_tag}
* **Sample 3:**
  > 🔥 شکافی عمیق بر دیوارهای مستحکم حریف! قهرمان ما **{attacker_name}** با تیغ بران **{card_name}** سنگرهای **{target_group}** را به آتش کشید و **{damage}** امتیاز جنگی برای قبیله رقم زد! به پیش به سوی پیروزی! {buff_tag}

### 4.3 Pool 2: `WAR_ATTACK_DEFENDER` (Defending Group View)
Sent to the chat of the clan under siege. Perspective is urgent, defensive, and rallying to arms.

* **Sample 1:**
  > 🛡 آژیر خطر در قلعه! **{attacker_name}** از گروه مهاجم **{source_group}** با کارت **{card_name}** به باروهای ما یورش آورد و **{damage}** صدمه به استحکامات دروازه زد! دلاوران قلعه، صفوف دفاعی را محکم کنید و آمادهٔ ضدحمله شوید! {buff_tag}
* **Sample 2:**
  > 💥 باران سنگ‌های آتشین بر سر قلعه! مهاجم **{source_group}** (به فرماندهی **{attacker_name}** و توان **{card_name}**) توانست شکافی به ارزش **{damage}** خسارت در دیوارهای ما ایجاد کند! مدافعان اواریا، پرچم‌ها را افراشته نگه دارید! {buff_tag}
* **Sample 3:**
  > ⚠️ هشدار نفوذ! **{attacker_name}** از قبیلهٔ **{source_group}** با حمایت **{card_name}** برجهای دیده‌بانی ما را هدف گرفت و **{damage}** خسارت به بار آورد! اجازه ندهید غنایم قلعه به تاراج برود؛ هر چه سریع‌تر پاسخ دهید! {buff_tag}

### 4.4 War Declaration & Resolution Narratives

* **War Declaration (Both Groups):**
  > ⚔️ **طبل جنگ در اواریا به صدا درآمد!**  
  > قبیلهٔ **{attacker_group}** به طور رسمی به قلعهٔ **{defender_group}** اعلان جنگ داد!  
  > ⏳ مهلت نبرد: **۲۴ ساعت تمام**  
  > 🍏 غنیمت فتح: تاراج بخشی از پیشرفت ارتقای قلعهٔ مغلوب  
  > دلاوران دو جناح، کارت‌های اصلی خود را بردارید و با دستور `وضعیت جنگ` در میدان نبرد حاضر شوید!
* **War Victory & Settlement (Winner View):**
  > 🏆 **پیروزی درخشان در جدال سیبی!**  
  > ۲۴ ساعت نبرد نفس‌گیر به پایان رسید و قبیلهٔ ما با مجموع **{winner_damage}** خسارت در برابر **{loser_damage}** حریف، پیروز میدان شد!  
  > 💰 غنیمت جنگی: **{looted_apples}** سیب از ذخایر حریف غارت شد و به پیشرفت قلعه افزوده گردید!
* **War Defeat & Peace Shield (Loser View):**
  > 🛡 **پایان نبرد و فعال‌سازی سپر صلح!**  
  > با وجود پایداری دلاورانه، قلعه در برابر تهاجم **{winner_group}** آسیب دید.  
  > ⚠️ میزان **{looted_apples}** سیب از پیشرفت جاری قلعه به تاراج رفت (سطح قلعه تنزل پیدا نمی‌کند).  
  > 🕊 **سپر صلح (Peace Shield)** به مدت ۴۸ ساعت بر فراز قلعه برافراشته شد؛ در این مدت هیچ گروهی نمی‌تواند به شما حمله کند.

---

## TASK 5 — The Exact Flow of a 24-Hour War (Step by Step)

```
[Group Admin in Group A]
           │
           ▼
1. Sends "قلعه سیبی"
           │
           ▼
2. Clicks [ ⚔️ شروع حمله ]
           │
           ├──▶ [Check: Is User Admin? (Telegram get_chat_member)]
           ├──▶ [Check: Is Group A already in war?]
           └──▶ [Check: Does Group A have Castle >= Lv 1?]
           │
           ▼
3. Matchmaking Query: Find up to 5 target groups
   (Level between Lv_A - 3 and Lv_A + 3, not in war, shield_until <= now)
           │
           ▼
4. Admin selects Target Group B from Inline List
           │
           ▼
5. Confirmation Dialog: Shows Group B stats, loot rules, consequences
           │
           ▼
6. Admin clicks [ 🚀 شروع حمله ]
           │
           ├──▶ [Acquire Locks: Group A & Group B in ASC ID order]
           ├──▶ [Insert group_wars row (status='active', 24h ends_at)]
           └──▶ [Set in_war=True on both group_castles]
           │
           ▼
7. War Declaration Broadcast:
   ├─ Post announcement banner + pin in Group A
   └─ Post announcement banner + pin in Group B
           │
           ▼
8. Attacking Phase (Duration: 24 Hours)
   ├─ Players type "وضعیت جنگ" in Group A or B
   ├─ Menu shows current scores, remaining time, card stats
   ├─ Buttons: [ ⚔️ حمله ], [ 💥 حمله با سیب باف ], [ ❌ لغو ]
   ├─ On Click:
   │    ├─ Validate user membership & 1-attack-per-war limit
   │    ├─ Calculate damage (Power + Castle Buff [+ Sib-Buff x2])
   │    ├─ Insert war_attacks & war_participants
   │    ├─ Update group_wars damage & castle HP
   │    └─ Pillow renders attack banner over AI background
   └─ Dual Narrative sent to both Group A and Group B
           │
           ▼
9. 24-Hour Resolution (Maintenance Tick: ends_at <= now)
   ├─ Lock group_wars and both castles
   ├─ Compare total damage: Clan with highest damage wins (or draw)
   ├─ Calculate Loot:
   │    ├─ Compute Loser's progress above current level threshold
   │    ├─ Loot = 15% of progress (Level never downgrades!)
   │    ├─ Transfer apples to Winner's total_donated
   │    └─ Apply 48-Hour Peace Shield to Loser (shield_until = now + 48h)
   ├─ Set status = 'finished', in_war = False on both castles
   └─ Post celebratory victory banner in Winner group & shield notice in Loser group
```

### Detailed Breakdown of Steps:

#### Step 1: Initiation
* A group administrator sends `"قلعه سیبی"` in the group chat.
* Below the castle image and donation buttons, an administrator-only button `[ ⚔️ شروع حمله ]` (`castle:war_start:{group_id}`) is displayed.
* If a non-admin clicks the button, Telegram pops an alert: `"تنها مدیران گروه اجازهٔ اعلان جنگ دارند."`

#### Step 2: Target Selection & Matchmaking
* The system executes a query against `group_castles` joined with `groups`:
  * Match criteria:
    1. `group_castles.group_id != current_group_id`
    2. `groups.is_active == True`
    3. `group_castles.level BETWEEN current_level - 3 AND current_level + 3`
    4. `group_castles.in_war == False`
    5. `group_castles.shield_until IS NULL OR group_castles.shield_until <= UTC_NOW`
  * Ordered randomly with a limit of 5 groups.
* An inline keyboard is presented to the admin listing the candidate target groups along with their castle levels (e.g., `[ 🏰 گروه عقاب‌ها (سطح ۵) ]`).

#### Step 3: Confirmation Dialog
* Upon selecting a target group, the bot edits the message with a detailed briefing:
  * Name and level of the defending group.
  * Estimated rewards: 15% of the opponent's current level donation progress.
  * War duration: 24 hours exact countdown.
  * Rules reminder: Castle levels are immune to demotion; defeated groups receive a 48-hour Peace Shield.
  * Buttons: `[ 🚀 تایید و شروع حمله ]` (`war:confirm:{target_id}`) and `[ 🔙 انصراف ]`.

#### Step 4: War Declaration & 24-Hour Clock Start
* Once confirmed:
  * A transaction locks both castles in ascending ID order.
  * Inserts a record into `group_wars`:
    * `started_at = UTC_NOW`
    * `ends_at = UTC_NOW + timedelta(hours=24)`
    * `status = 'active'`
  * Marks `in_war = True` on both `group_castles`.
  * The bot generates and delivers the declaration banner (`war_bg_declaration.jpg`) to **both** group chats with a pin notification.

#### Step 5: Attacking Phase
* Members of either group invoke `وضعیت جنگ` (or click a link in the announcement).
* The war dashboard displays:
  * Opponent group name, castle levels, and comparative score meter.
  * Remaining war time countdown (e.g., `۱۴ ساعت و ۲۲ دقیقه`).
  * Player's active Main Card details.
  * Inline buttons:
    * `[ ⚔️ حمله ]` (`war:atk:{war_id}:normal`)
    * `[ 💥 حمله با سیب باف ]` (`war:atk:{war_id}:buff`)
    * `[ ❌ لغو ]`
* **Execution & Anti-Cheat:**
  * Only players observed as current chat members of one of the warring groups can attack.
  * Attackers can only hit the opposing group.
  * Each player is strictly limited to **1 attack per war**.
  * Damage calculation incorporates Main Card base power, card level, critical roll (10% chance for 2x), current Castle invasion buff (+2% to +20%), and optional Sib-Buff (2x).
  * Pillow runtime assembles the attack visual: pre-generated background (`war_bg_attacker.jpg` or `war_bg_defender.jpg`), player's card, and glowing damage text.
  * Dispatches dual narrative: `WAR_ATTACK_ATTACKER` to the friendly chat and `WAR_ATTACK_DEFENDER` to the opposing chat.

#### Step 6: 24-Hour Resolution & Settlement
* The central background maintenance cycle (`MaintenanceService.run_once()`) scans for active wars where `ends_at <= UTC_NOW`.
* In a single atomic database transaction:
  * Compares `attacker_damage` against `defender_damage`:
    * Highest cumulative damage is designated the `winner_id`.
    * If equal, designated a draw (`winner_id = NULL`).
  * **Loot Calculation (Protection of Castle Level):**
    * Let $L$ be the loser's current castle level.
    * Let $T_{current} = \text{THRESHOLDS}[L-1]$ (the floor threshold of their current level).
    * Let $\text{Progress} = \max(0, \text{total\_donated} - T_{current})$.
    * The looted amount is $15\%$ of this excess progress: $\text{Loot} = \lfloor \text{Progress} \times 0.15 \rfloor$.
    * Deduct $\text{Loot}$ from loser's `total_donated` (because $\text{Loot} \le \text{Progress}$, the loser's `total_donated` never falls below $T_{current}$, mathematically guaranteeing their castle level never downgrades).
    * Add $\text{Loot}$ to winner's `total_donated` (updating winner's level if a threshold is crossed, capped at Level 10).
  * **Peace Shield Grant:**
    * Loser's castle receives `shield_until = UTC_NOW + timedelta(hours=48)` (48 hours of peace).
  * Sets `in_war = False` for both groups and `status = 'finished'`.
* Delivers closing visual announcements:
  * Winner chat: `war_bg_victory.jpg` with celebration text and looted spoils summary.
  * Loser chat: `war_bg_defeat.jpg` with defense summary and Peace Shield confirmation.

---

## Deliverable Summary Check

1. **Recon Findings:** Detailed in Section 2 with file:line citations for Castle service, Invasion v2 lifecycle/joining/Pillow composition, Sib-Buff damage doubling, group admin verification, and maintenance background timers.
2. **Database Schema Proposal:** Exact SQL DDL and SQLAlchemy definitions for `group_wars`, `war_attacks`, `war_participants`, plus `group_castles` extensions, lock ordering, and anti-cheat concurrency.
3. **AI Image List & Pillow Layout Plan:** Complete specification of 5 AI backgrounds (1280x720), transparent overlays, and exact Pillow runtime coordinate geometry (center card at 380x520, damage text at 640x648, 150pt font, drop shadow, glow).
4. **Sample Narrative Texts:** Persian narrative pools for `WAR_ATTACK_ATTACKER` (3 samples) and `WAR_ATTACK_DEFENDER` (3 samples), plus declaration and settlement copy.
5. **Exact 24-Hour War Flow:** Comprehensive 6-phase state machine diagram and narrative.
6. **No Code Modification:** Pure architectural specification; zero runtime code files altered.
