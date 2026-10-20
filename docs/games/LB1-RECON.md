# LB1-RECON — Main Leaderboard Rework &amp; Cleanup

**Phase:** LB1 investigation and design — **NO CODE** · **Date:** 2026-10-20  
**Inspected baseline:** `19274124d546fa5cd18a47aec0d7122bbfd4f783` (`main`, arena branch `arena/58439152-sib`)  
**Scope:** this report only. No application code, migrations, handlers, keyboards, services, or database data are changed. Everything described as "proposed" is future work, not existing behavior.

---

## 0. Executive Summary

The current leaderboard (`/leaderboard`, `/top`, text shortcut `سیب لیدربرد` / `سیب لیدربورد`) is a **legacy matchmaking-only** system: it ranks groups by total wins in `group_stats` (the old PvP mini-game match pipeline). It has **no visibility** into the two dominant subsystems that now drive the game:

* **Invasions V2** (تهاجم شیاسیب) — `inv2_invasions` with `VICTORY`/`DEFEAT` outcomes, `inv2_group_state.total_victories`, and per-player damage in `inv2_attacks`.
* **Castle Wars / Apple Clash** (جدال سیبی) — `group_wars` with `winner_id`/`status='finished'`, castle levels in `group_castles`, and donations in `castle_donations`.

There is a **redundant, isolated sub-leaderboard** inside the Apple Battle (نبرد سیبی) ready-screen (`ab_board`), which ranks *individual players* by `apple_battles` wins. It is the only place `ab_board` exists, it is not reachable from the main menu, and it will be removed so the game exposes exactly **one canonical main leaderboard**.

This report maps every leaderboard touch-point, inspects the database models that will power the new four-tab UI, proposes the exact SQL for each tab, and lists the precise lines to delete for the cleanup.

---

## 1. Reconnaissance — All Current Leaderboard Implementations

All paths below are relative to the repository root. Imports use the `sib.*` namespace.

### 1.1 The Main Leaderboard (the one we keep and rework)

| Layer | File | What it does |
|---|---|---|
| **Service** | `services/leaderboard.py` — `LeaderboardService` | The single source of truth. `_ranked_rows()` joins `Group` ↔ `GroupStats` where `is_active`, sorts in Python by `(-wins, -win_rate, -matches_played, group.id)`. Exposes `get_top_groups(session, limit=10)` (returns `List[(rank, Group, GroupStats)]`), `get_group_stats_and_rank(session, group_id)`, and `get_player_stats(session, user_id)`. Win-based, no ELO — ELO was removed in migration `009_remove_elo`. |
| **Handler** | `handlers/leaderboard.py` — `cmd_leaderboard`, `callback_show_leaderboard`, `callback_group_rating_stats`, `cmd_stats` | `cmd_leaderboard` is bound to `Command("leaderboard", "top")` — both `/leaderboard` and `/top`. Renders Persian text via `format_leaderboard_text()` with 🥇🥈🥉 medals, then `send_screen_reply(..., "leaderboard", ...)`. `callback_show_leaderboard` (`F.data == "show_leaderboard"`) does the same via `render_banner`. `callback_group_rating_stats` (`F.data in ("group_rating_stats", "group_win_stats")`) shows `شناسنامه گروه` with rank/win-rate. `cmd_stats` (`Command("stats")`) shows group stats in groups, personal `PlayerStats` + seedling count in private. |
| **Keyboard** | `keyboards/leaderboard.py` — `get_leaderboard_keyboard(is_group)`, `get_group_stats_keyboard()`, `get_player_stats_keyboard()` | `get_leaderboard_keyboard` has `🔄 بروزرسانی → show_leaderboard`, optional `📊 آمار گروه ما → group_win_stats`, and `↩️ بازگشت به منوی اصلی → main_menu`. `get_group_stats_keyboard` is the inverse: `🏆 برترین گروه‌ها → show_leaderboard` + refresh. `get_player_stats_keyboard` is a single `🏆 برترین گروه‌ها` button. |
| **Entry points** | `handlers/start.py` — `SIB_SHORTCUT_TARGETS`, `SIB_SHORTCUT_PATTERN`, `sib_shortcut_target()`, `_open_leaderboard_entry()` | `SIB_SHORTCUT_TARGETS` maps both spellings `لیدربرد` and `لیدربورد` → `"leaderboard"` (line 1225-1226). `SIB_SHORTCUT_PATTERN` (line 1239) anchors `^\\s*سیب\\s+ل[yeh]در(برد\|بورد)\\s*$`. `sib_shortcut_target()` folds `ي→ی`, `ك→ک`, ZWNJ. `_open_leaderboard_entry` (line 1338-1342) delegates directly to `cmd_leaderboard` — no duplicated rendering. Registered at `F.text.regexp(SIB_SHORTCUT_PATTERN)` → `handle_sib_shortcut_word`. |
| **Group menu** | `keyboards/common.py` — `get_group_welcome_keyboard()` | Second row right button: `🏆 رتبه و آمار گروه → group_rating_stats` — leads to the group-stats screen, which links to the main leaderboard. This is the main-menu path and **must stay**. Verified by `tests/test_leaderboard.py::test_main_leaderboard_button_still_exists` (AB1 guard). |
| **Banner** | `services/banner.py` — `_BANNERS["leaderboard"]` | `leaderboard.jpg` banner art (line 34). `render_banner`/`send_screen_reply` are the two delivery paths for the main leaderboard screen. |
| **Database** | `database/models/group_stats.py` — `GroupStats` | `group_id` PK/FK `groups.id` CASCADE, columns `wins`, `losses`, `draws`, `matches_played` (all `Integer`). `win_rate` property = `wins / matches_played * 100`. Populated by `services/match_result.py` — bot and friendly matches never touch it (lines 7-8, 295). |
| **Help text** | `services/help_content.py` line 323 | Bullet `• سیب لیدربرد: جدول رتبه‌ها (لیدربورد هم پذیرفته می‌شود)` — must be updated when tabs change. |

**Routing summary (what must be preserved):**

```
/leaderboard  ─┐
/top          ─┤→ cmd_leaderboard  → get_top_groups  → format_leaderboard_text
سیب لیدربرد   ─┤   (text shortcut)      (banner screen)
سیب لیدربورد  ─┘
show_leaderboard callback → same rendering via render_banner
group_rating_stats / group_win_stats callback → group stats + rank
/stats (group) → group stats + rank   |  /stats (private) → player stats
```

Nothing else renders `برترین گروه‌های سیب` — the search `grep -rn "برترین گروه‌های سیب"` finds it only in `handlers/leaderboard.py` lines 23/26/141/182 and its tests.

### 1.2 The Redundant Apple Battle Leaderboard (the one we remove)

| Layer | File:Line | Detail |
|---|---|---|
| **Screen 2 button** | `handlers/apple_battle.py:171-174` inside `_ready_view()` | The "card selected — press Find Opponent" screen builds 4 rows: `ab_find` (Find Opponent), `ab_change` (Change Card), **`ab_board` (🏆 لیدربورد نبرد)** at line 173, and the back row. This is the **only** place `ab_board` is emitted in the entire codebase (`grep` finds it only here at line 173 and once more in the test expectation). Screen 1 (`_select_view` at line ~84-155) intentionally has **no** `ab_board` — confirmed by `tests/test_apple_battle_menu.py::test_apple_battle_menu_has_no_battle_leaderboard_button`. |
| **Handler** | `handlers/apple_battle.py:500-520` — `handle_ab_board` | `@router.callback_query(F.data == "ab_board")`. Loads `board = await AppleBattleService.battle_leaderboard(session)` (line 503), formats `🏆 لیدربورد نبرد سیبی` with `"{user_id} — {wins} برد از {total} نبرد"` (line 505-508), builds keyboard with `🔄 بروزرسانی → ab_board` + `↩️ بازگشت به نبرد سیبی → apple_battle`. |
| **Service** | `services/apple_battle.py:1441-1490` — `AppleBattleService.battle_leaderboard(session, limit=20)` | Queries `apple_battles` where `status == COMPLETED` and `winner_id IS NOT NULL`, groups by `winner_id` counting wins, separately sums `player1_id` + `player2_id` appearances for total battles, merges into `List[(user_id, wins, battles)]` sorted by wins desc. **This is the only caller** — `database/migrations/versions/018_apple_battle_match.py` aside. Returns bare user IDs (`f"🏆 `{user_id}` — …"`), never display names — a known UX limitation. |
| **Database** | `database/models/apple_battle.py` — `AppleBattle`, `AppleBattleQueueEntry` | `apple_battles` columns include `player1_id`, `player2_id`, `match_id` (FK `matches.id`, nullable for legacy), `player1_card_id`, `player2_card_id`, `level`, `status` (PENDING/RESULT_READY/COMPLETED/CANCELLED/EXPIRED), `winner_id`, `result_json`, `rng_seed`, `seedlings_awarded`, `matched_at`/`completed_at`/`expires_at`. `AppleBattleQueueEntry` is the one-per-user loadout (`user_id` unique). |
| **Tests** | `tests/test_apple_battle.py:561-572` — `test_battle_leaderboard_counts_wins` | Creates wins/losses, asserts `AppleBattleService.battle_leaderboard` ordering by wins. |
| | `tests/test_apple_battle_menu.py:54-78` — `test_apple_battle_ready_menu_keeps_battle_leaderboard_button` | **Explicitly asserts** the ready screen still has `ab_board` at `keyboard.inline_keyboard[2][0]`. This test **must be deleted or updated** when the button is removed — otherwise it will fail post-cleanup (intentionally). |

**Why it is isolated:** the `ab_board` flow has no shared state with the main leaderboard. It is not linked from the group menu, the start handler, the party games list (`keyboards/party.py` hides `apple_battle` from `HIDDEN_GAME_CODES` but never links a leaderboard), or the daily chest / war / castle flows. Removing it touches nothing outside `handlers/apple_battle.py` + `services/apple_battle.py` + that one test.

### 1.3 Per-Group Castle Top-Donors List (NOT a global leaderboard — keep it)

| File | Line | Detail |
|---|---|---|
| `handlers/castle.py` | `keyboard()` line 84: `primary_button("🏆 برترین اهداکنندگان", f"castle:top:{group_id}")` | Per-group top-5 donors, shown inside the `قلعه سیبی` group-only screen. |
| `handlers/castle.py` | `castle_callback` line 429: `text = "🏆 برترین اهداکنندگان\\n" + …` | Query: `select(User.first_name, sum(CastleDonation.amount)).join(CastleDonation).where(group_id).group_by(User.id).order_by(total desc, user_id).limit(5)`. Rendered as HTML-escaped names with `fa_digits`. This is **not** the global leaderboard — it answers "who funded *this* castle" and must be kept. |
| `tests/test_castle.py` | line 407: `assert "castle:top:-123": ("🏆 برترین اهداکنندگان", "primary")` | Guard for the castle screen — unrelated to `/leaderboard`. |

### 1.4 All Other Leaderboard-Adjacent Surfaces (kept, not redundant)

* `handlers/castle.py:84` (`castle:top`) — local donors list, keep.
* `handlers/war.py` (`war:atk`, `war:potion`, `وضعیت جنگ`) — war status screen with live damage totals, never calls itself a leaderboard.
* `handlers/inv2_join.py` (`inv2:join`, `inv2:joinbuff`) — invasion attack buttons, not a leaderboard.
* `handlers/inv2_dashboard.py` / `handlers/inv2_admin.py` — admin dashboards for invasions, not player-facing leaderboards.
* `handlers/war.py::war_keyboard` and `handlers/castle.py::keyboard` — war/castle keyboards have no cross-links to `show_leaderboard` or `ab_board`.

**Conclusion of TASK 1:** the codebase has exactly **two** global-facing leaderboards: the main one (`group_stats` wins) and the Apple Battle sub-leaderboard (`apple_battles` wins). The latter is the redundant one to delete. Every other "برترین" surface is a scoped local list inside the castle menu and is **not** a global leaderboard.

---

## 2. Database Design for the New Four-Tab Leaderboard

### 2.1 Model Inventory Relevant to the New Tabs

All models use signed `BigInteger` for Telegram IDs (matches `groups.id` PK). `Group.is_active` gates every global query.

| Proposal Tab | Source Table(s) | Key Columns | Current State |
|---|---|---|---|
| **🏰 برترین قلعه‌ها** (Top Castles) | `group_castles` (`database/models/castle.py`) + `groups` | `group_castles.group_id` (PK/FK `groups.id`), `level` (1-10, `ck_castle_level`), `total_donated` (cumulative apples, threshold `1_300_000` for L10), `shield_until`, `in_war` | `group_castles` is one row per group, created lazily via `CastleService.get_or_create_castle` with upsert (`ON DUPLICATE KEY UPDATE` / `ON CONFLICT DO NOTHING`). Level is a cached derivative of `total_donated` via `level_for_total()` with `THRESHOLDS = (0, 5000, 20000, 50000, 110000, 210000, 360000, 580000, 880000, 1300000)`. Indexed only by PK — no global level index, but a scan of this tiny table (one row per active group) is trivial. |
| **⚔️ برترین جنگاوران** (Top Warriors) | `group_wars` (`database/models/war.py`) + `groups` | `group_wars.id` PK, `attacker_id` FK `group_castles.group_id`, `defender_id`, `status` (`active`/`finished`), `attacker_damage`, `defender_damage`, `winner_id` FK `groups.id` nullable, `looted_apples`, `started_at`, `ends_at` | `status` indexed (`ix_group_wars_status_ends_at`), plus `attacker_id`/`defender_id` partial indexes. `winner_id` is `NULL` for draws — only decisive finishes produce a winner. `finish_war` sets `winner_id` to whichever side has higher total damage. War duration is 1h (Blitz), peace shield 2h for defender. `GroupCastle.in_war` mirrors liveness. |
| **🛡 برترین مدافعان** (Top Defenders) | `inv2_invasions` (`database/models/invasion_v2.py`) + `inv2_group_state` + `groups` | `inv2_invasions.id` PK autoincrement, `group_id` FK `groups.id`, `region_code` FK `inv2_regions.code`, `status` (`PENDING`/`ACTIVE`/`VICTORY`/`DEFEAT`/`EXPIRED` — `InvasionStatus` enum in `services/invasion_v2/enums.py`), `enemy_type`, `enemy_hp`/`enemy_hp_remaining`, `scheduled_at`/`started_at`/`ends_at`/`finalized_at`, `occurrence_key` unique, `message_chat_id`/`message_id` | Indexes: `ix_inv2_invasions_group_status (group_id, status)` and `ix_inv2_invasions_status_ends_at`. `Inv2GroupState` (`database/models/invasion_v2.py:139`) caches `total_victories`/`total_defeats` per `group_id` — recomputed as `COUNT(*) WHERE status=VICTORY/DEFEAT` in `services/invasion_v2/rewards_service.py:settle_outcome` and `services/invasion_v2/recovery.py`. This cached counter is exactly the "total Invasions won/survived" the tab needs, but querying `inv2_invasions` directly is canonical and index-friendly. |
| **🍎 برترین اهداکنندگان** (Top Donors) | `castle_donations` (`database/models/castle.py`) + `groups` | `castle_donations.id` PK, `user_id` FK `users.id`, `group_id` FK `group_castles.group_id`, `amount` (>0), `donated_at` (UTC), `request_key` unique | Indexes: `ix_castle_donations_user_time (user_id, donated_at)` and `ix_castle_donations_group_user (group_id, user_id)`. No dedicated global index on `group_id` alone — add one or rely on the composite prefix. |

**Player-level vs group-level — what exists today:**

* **Group-level attribution is complete** for all four tabs: every invasion has a `group_id`, every war has `attacker_id`/`defender_id` + `winner_id`, every donation has a `group_id`, and every castle belongs to one group. The four new tabs can therefore all be **group leaderboards** with a single consistent join pattern (`JOIN groups WHERE is_active`).
* **Player-level attribution exists but is partial:** `inv2_attacks` (`invasion_id`, `user_id`, `damage`, `is_crit`, `card_id`) records each player's *single* attack per invasion (unique `uq_inv2_attacks_invasion_user`). `war_participants` (`war_id`, `user_id`, `group_id`, `energy`, `total_damage`, `attack_count`) and `war_attacks` (`war_id`, `user_id`, `source_group_id`, `target_group_id`, `damage`) record each war hit. `castle_donations` is per-player. These allow a **Top Players by Damage** view, but:
  * No aggregated `player_total_damage` or `player_total_donations` materialized table exists — it would need SUM + GROUP BY.
  * Damage values are noisy (random factor 0.85-1.15, 10% crit, castle buff, optional `sib_buff` ×2) — ranking by raw damage is less meaningful than by wins.
  * Player identities are bare IDs in the DB (`users.id`, not always `display_name` cached) — cross-group display-name resolution needs a JOIN.
* **Recommendation:** ship the four **group** tabs first (consistent with every existing leaderboard surface, which is group-ranked). Add optional player drill-down later as a second row of tabs inside each group tab, reusing the per-player tables above. This report's SQL section gives both group queries (primary) and the player-level query sketch (future).

### 2.2 Proposed New UI — `/leaderboard` with Four Inline Tabs

**Entry points (unchanged):** `/leaderboard`, `/top`, `سیب لیدربرد`, `سیب لیدربورد` — all route to `cmd_leaderboard`. The group-menu path `🏆 رتبه و آمار گروه → group_rating_stats → show_leaderboard` is also preserved.

**New screen layout:**

```
🏆 لیدربرد سیب — برترین‌ها
یکی از دسته‌ها را انتخاب کنید 👇

[ 🏰 برترین قلعه‌ها ] [ ⚔️ برترین جنگاوران ]
[ 🛡 برترین مدافعان ] [ 🍎 برترین اهداکنندگان ]

━━━━━━━━━━━━━━━━━━━━━━━━
<active tab content: 10 ranked rows + medals>
━━━━━━━━━━━━━━━━━━━━━━━━

[ 🔄 بروزرسانی ] [ 📊 آمار گروه ما ]   ← group chats only
[ ↩️ بازگشت به منوی اصلی ]
```

**Interaction contract:**

* Each tab button is an inline callback `lb:tab:<key>` where `<key>` ∈ `{castles, warriors, defenders, donors}`. The prefix `lb:` namespaces the new leaderboard and avoids collision with `show_leaderboard`, `ab_board`, `castle:top`, or `group_win_stats` (all kept for backward compatibility or scoped use).
* Pressing a tab edits the **same banner message** in place (via `render_banner` / `safe_edit_callback_message`) — never a new message. `callback.answer()` acknowledges instantly.
* `🔄 بروزرسانی` re-renders the **currently active tab** (`lb:refresh:<key>`) — not a hardcoded `show_leaderboard`. For backward compatibility, `show_leaderboard` continues to render the `castles` tab (the new default).
* `📊 آمار گروه ما` (`group_win_stats`) remains — it shows the viewer's group rank in whichever tab is active (needs an extended `get_group_stats_and_rank` that can rank within that tab's metric).
* Tab state is encoded **only** in callback_data — no FSM, no session storage, no URL. Old pinned `show_leaderboard` buttons keep working (they map to the default tab).

**Tab definitions (one sentence each, for the handler docstring):**

| Key | Button label | What it ranks | Metric | Empty text |
|---|---|---|---|---|
| `castles` | 🏰 برترین قلعه‌ها | Groups by castle progress | `level DESC, total_donated DESC, group_id ASC` | "هنوز هیچ قلعه‌ای ساخته نشده! با «قلعه سیبی» گروه‌تان را آغاز کنید." |
| `warriors` | ⚔️ برترین جنگاوران | Groups by Castle Wars wins | `wins DESC, win_rate DESC, group_id ASC` on `group_wars.winner_id` where `status='finished'` | "هنوز نبرد قلعه‌ای ثبت نشده! از «قلعه سیبی» اعلان جنگ کنید." |
| `defenders` | 🛡 برترین مدافعان | Groups by Invasions survived | `victories DESC, win_rate DESC, group_id ASC` on `inv2_invasions.status='VICTORY'` | "هنوز تهاجم پیروزمندانه‌ای ثبت نشده — مدافعان اواریا منتظر شما هستند!" |
| `donors` | 🍎 برترین اهداکنندگان | Groups by total apples donated across all donations | `SUM(amount) DESC, group_id ASC` on `castle_donations` | "هنوز اهدایی ثبت نشده." |

> **Design choice on "Top Donors":** the tab is **group-ranked by aggregate donated apples**, not a player leaderboard. A player leaderboard would list 10 user IDs without group context, break the consistent group-ranking mental model, and leak donor privacy across groups. If a per-group "top donors inside this castle" is wanted, it already exists at `castle:top` (local top-5). The global `donors` tab complements it by answering "which *groups* have collectively invested the most in their castles" — the group equivalent of individual generosity.

**Persistence note:** each tab's ranking is a **live query** against the durable tables above — never a cached materialized view with its own staleness/rebuild rules. The underlying counters (`GroupStats`, `Inv2GroupState.total_victories`, `GroupCastle.level`) are already idempotently maintained by their domain services; the leaderboard reads them but never writes them.

### 2.3 SQL Queries for Top 10 — Each Tab

All queries assume MariaDB/MySQL (production) and SQLite (tests) via SQLAlchemy. Use `BigInteger().with_variant(Integer(), "sqlite")` for ID columns. `groups.is_active = true` is always present. `limit 10` is the display cap; pagination can be added later with `offset`.

---

#### Tab 1 — 🏰 برترین قلعه‌ها (Top Castles by Level)

**Meaning:** which groups have the most advanced castles. Level is the primary sort; within the same level, further progress toward the next threshold (`total_donated`) breaks ties.

**Raw SQL (MariaDB + SQLite):**

```sql
-- Top 10 castles by level, then progress, then stable id
SELECT g.id            AS group_id,
       g.title         AS group_title,
       gc.level        AS castle_level,
       gc.total_donated
FROM   group_castles  gc
JOIN   groups          g  ON g.id = gc.group_id
WHERE  g.is_active = 1
ORDER BY gc.level DESC,
         gc.total_donated DESC,
         g.id ASC
LIMIT 10;
```

**SQLAlchemy:**

```python
stmt = (
    select(Group, GroupCastle)
    .join(GroupCastle, GroupCastle.group_id == Group.id)
    .where(Group.is_active.is_(True))
    .order_by(GroupCastle.level.desc(),
              GroupCastle.total_donated.desc(),
              Group.id.asc())
    .limit(10)
)
```

**Why not `total_donated` alone:** two groups at the same `level` must not be ordered by a level-10 floor value alone — `total_donated` is monotonic (e.g. L10 starts at 1_300_000, L9 caps at 1_299_999), so `ORDER BY level, total_donated` and `ORDER BY total_donated` would agree for now. But explicit two-key ordering is cheaper to read and survives any future threshold rebalance or prestige reset.

**Index need:** scan is `O(groups)` — each group has at most one castle. For 10k groups this is negligible. If needed, add `INDEX ix_group_castles_level_total (level DESC, total_donated DESC)` to turn it into an index-only sort.

**Empty state:** no `group_castles` row → `RETURN fallback_text` ("هنوز هیچ قلعه‌ای ساخته نشده!").

---

#### Tab 2 — ⚔️ برترین جنگاوران (Top Warriors by Castle War Wins)

**Meaning:** which groups have won the most finished Castle Wars (`جدال سیبی`). Wins are `COUNT(*)` where `group_wars.winner_id == group_id` and `status = 'finished'`. Draws (`winner_id IS NULL`) are ignored. Both attacker-wins and defender-wins count equally — the war is symmetric at settlement.

**Raw SQL (MariaDB + SQLite):**

```sql
-- Wins per group from finished wars (attacker OR defender, no double-count)
SELECT g.id                     AS group_id,
       g.title                  AS group_title,
       COALESCE(wc.wins, 0)     AS war_wins,
       COALESCE(wc.finished, 0) AS wars_played
FROM   groups g
LEFT JOIN (
    SELECT winner_id,
           COUNT(*)                           AS wins,
           COUNT(*)                           AS finished  -- same alias for rate
    FROM   group_wars
    WHERE  status = 'finished'
      AND  winner_id IS NOT NULL
    GROUP BY winner_id
) wc ON wc.winner_id = g.id
WHERE g.is_active = 1
ORDER BY war_wins DESC,
         -- win_rate = wins / wars_participated (participation, not just wins)
         -- wars_participated = COUNT(*) where group was attacker OR defender and finished
         -- computed via correlated subquery below OR secondary JOIN; for v1 a simple
         -- secondary metric is total damage dealt when rate is expensive — but to keep
         -- perfect parity with the existing LeaderboardService tie-break, we add a
         -- second CTE for participation:
         wc.wins / NULLIF(wp.total_wars, 0) DESC NULLS LAST,
         wc.wins DESC,
         g.id ASC
LIMIT 10;

-- Simpler v1 (recommended): rank by wins, then by finished-count, then id.
-- The win-rate tie-break is added when war participation counters exist.
-- This equivalent without the correlated participation JOIN is production-ready for LB1:
SELECT g.id                       AS group_id,
       g.title                    AS group_title,
       COUNT(w.id)               AS war_wins
FROM   groups g
LEFT JOIN group_wars w
       ON w.winner_id = g.id
      AND w.status = 'finished'
WHERE  g.is_active = 1
GROUP BY g.id, g.title
ORDER BY war_wins DESC,
         g.id ASC
LIMIT 10;
```

**Recommended v1 (no extra JOIN, index-friendly, deterministic):**

```sql
SELECT g.id,
       g.title,
       COUNT(w.id) AS war_wins
FROM   groups g
LEFT JOIN group_wars w
       ON w.winner_id = g.id AND w.status = 'finished'
WHERE  g.is_active = 1
GROUP BY g.id, g.title
ORDER BY war_wins DESC, g.id ASC
LIMIT 10;
```

**SQLAlchemy:**

```python
from sqlalchemy import func, select

wins_subq = (
    select(
        GroupWar.winner_id.label("gid"),
        func.count(GroupWar.id).label("wins"),
    )
    .where(GroupWar.status == "finished")
    .where(GroupWar.winner_id.is_not(None))
    .group_by(GroupWar.winner_id)
    .subquery()
)

stmt = (
    select(Group, func.coalesce(wins_subq.c.wins, 0).label("war_wins"))
    .outerjoin(wins_subq, wins_subq.c.gid == Group.id)
    .where(Group.is_active.is_(True))
    .order_by(func.coalesce(wins_subq.c.wins, 0).desc(), Group.id.asc())
    .limit(10)
)
```

**Edge cases:** a war whose `winner_id` points to a deleted/inactive group still has a row but the LEFT JOIN excludes the group — consistent with the current leaderboard's `JOIN … WHERE is_active`. Draws (`winner_id IS NULL`) are excluded by `winner_id IS NOT NULL`. The `in_war` flag on `group_castles` is irrelevant — we count only `finished` wars.

**Future player-level drill-down (not v1, but ready):**

```sql
-- Top 10 players by total damage across all their wars
SELECT u.id,
       u.first_name,
       SUM(wp.total_damage)                      AS total_damage,
       SUM(wp.attack_count)                      AS total_attacks,
       COUNT(DISTINCT wp.war_id)                 AS wars_participated
FROM   war_participants wp
JOIN   users u ON u.id = wp.user_id
GROUP BY u.id, u.first_name
ORDER BY total_damage DESC, u.id ASC
LIMIT 10;
```

---

#### Tab 3 — 🛡 برترین مدافعان (Top Defenders by Invasions Won)

**Meaning:** which groups have survived the most شیاسیب invasions. Each invasion creates one `inv2_invasions` row per group per slot; `status='VICTORY'` means the players' combined damage reached `enemy_hp` before `ends_at`. `EXPIRED` (time-ran-out without being announced/active) must NOT count — only `VICTORY` counts, matching the settlement rule in `rewards_service.py:settle_outcome` where only `VICTORY`/`DEFEAT` move `total_victories`/`total_defeats` and region control.

**Raw SQL (canonical — counts terminal invasions directly):**

```sql
-- Top 10 groups by invasion victories
SELECT g.id,
       g.title,
       COUNT(i.id) AS victories
FROM   groups g
LEFT JOIN inv2_invasions i
       ON i.group_id = g.id AND i.status = 'VICTORY'
WHERE  g.is_active = 1
GROUP BY g.id, g.title
ORDER BY victories DESC, g.id ASC
LIMIT 10;
```

**Alternative using the cached counter (faster when many invasions exist):**

```sql
-- Same ranking via the materialized Inv2GroupState (kept in sync by settle/recovery)
SELECT g.id,
       g.title,
       COALESCE(s.total_victories, 0) AS victories,
       COALESCE(s.total_defeats, 0)   AS defeats
FROM   groups g
LEFT JOIN inv2_group_state s ON s.group_id = g.id
WHERE  g.is_active = 1
ORDER BY victories DESC, g.id ASC
LIMIT 10;
```

**SQLAlchemy (canonical, no cache dependency):**

```python
from sqlalchemy import func, select

stmt = (
    select(Group, func.count(Inv2Invasion.id).label("victories"))
    .outerjoin(
        Inv2Invasion,
        (Inv2Invasion.group_id == Group.id)
        & (Inv2Invasion.status == InvasionStatus.VICTORY.value),
    )
    .where(Group.is_active.is_(True))
    .group_by(Group.id)
    .order_by(func.count(Inv2Invasion.id).desc(), Group.id.asc())
    .limit(10)
)
```

**Why canonical over cached:** `inv2_group_state` is recomputed from terminal counts (`COUNT WHERE status=VICTORY`) — it is *derived*, not authoritative. The two agree when settlement is healthy, but a leaderboard must remain correct even during a recovery window where `total_victories` is stale. Counting `inv2_invasions` directly is always correct and is indexed by `ix_inv2_invasions_group_status (group_id, status)`, so the GROUP BY is an index-only scan per group.

**Win-rate tie-break (optional v2):** when adding a rate, compute `victories / NULLIF(victories + defeats, 0)` from `COUNT WHERE status IN ('VICTORY','DEFEAT')` — never include `EXPIRED` or `PENDING`/`ACTIVE` in the denominator. For LB1 v1, `victories DESC, group_id ASC` matches the simplicity of the castle-level tab and is sufficient.

**Player-level future:**

```sql
-- Top 10 players by number of invasions participated in (distinct invasions)
SELECT a.user_id,
       u.first_name,
       COUNT(DISTINCT a.invasion_id)  AS invasions_joined,
       SUM(a.damage)                  AS total_damage,
       SUM(CASE WHEN a.is_crit THEN 1 ELSE 0 END) AS crits
FROM   inv2_attacks a
JOIN   users u ON u.id = a.user_id
GROUP BY a.user_id, u.first_name
ORDER BY invasions_joined DESC, total_damage DESC, a.user_id ASC
LIMIT 10;
-- For LB1 ship only the group tab — this is ready for a later player toggle.
```

---

#### Tab 4 — 🍎 برترین اهداکنندگان (Top Donors by Total Apples Donated)

**Meaning (group ranking):** which groups have collectively donated the most apples to their castles — `SUM(castle_donations.amount)` per `group_id`. This is the global counterpart to the local `castle:top` screen.

**Raw SQL (group leaderboard):**

```sql
-- Top 10 groups by aggregate donated apples (all time, all members)
SELECT g.id,
       g.title,
       COALESCE(SUM(d.amount), 0) AS total_donated_agg
FROM   groups g
LEFT JOIN castle_donations d ON d.group_id = g.id
WHERE  g.is_active = 1
GROUP BY g.id, g.title
ORDER BY total_donated_agg DESC, g.id ASC
LIMIT 10;
```

**SQLAlchemy:**

```python
from sqlalchemy import func, select

stmt = (
    select(Group, func.coalesce(func.sum(CastleDonation.amount), 0).label("total_donated_agg"))
    .outerjoin(CastleDonation, CastleDonation.group_id == Group.id)
    .where(Group.is_active.is_(True))
    .group_by(Group.id)
    .order_by(func.coalesce(func.sum(CastleDonation.amount), 0).desc(), Group.id.asc())
    .limit(10)
)
```

**Player-level variant (optional toggle inside the same tab, not v1):**

```sql
-- Top 10 individual donors across all castles
SELECT d.user_id,
       u.first_name,
       SUM(d.amount) AS lifetime_donated,
       COUNT(*)      AS donation_count
FROM   castle_donations d
JOIN   users u ON u.id = d.user_id
GROUP BY d.user_id, u.first_name
ORDER BY lifetime_donated DESC, d.user_id ASC
LIMIT 10;
```

**Why group-level ships first:** the existing `castle:top` is already the per-group player view. The global tab should answer the next natural question ("which community is most invested overall") without surfacing individual donor wallets globally — a privacy consideration: showing every user's lifetime apples across groups in a single public list reveals spending that is currently only visible inside each castle. A player global toggle can be added later behind a product decision.

**Index note:** the current indexes `ix_castle_donations_user_time` and `ix_castle_donations_group_user` both have `group_id` as a leading column in one of them, so `WHERE group_id = ?` GROUP BY is covered. For the global SUM across all groups, the query is a full scan of `castle_donations` — acceptable at the expected scale (one row per successful donation, at most 500 apples per user per day). If donations grow large, add `INDEX ix_castle_donations_group_amount (group_id, amount)`.

---

#### Shared formatting note

Each tab renders `f"{medal} **{group.title}** — {metric_line}"` with `medals = {1: "🥇", 2: "🥈", 3: "🥉"}` and `f"**{rank}.**"` for rank ≥4 — the same pattern as `handlers/leaderboard.py:format_leaderboard_text`. Metric line examples:

* Castles: `🏰 سطح {level} | 🍏 اهدا {total_donated}`
* Warriors: `⚔️ برد جنگی: {war_wins} | ⚔️ نبردها: {participated}`
* Defenders: `🛡 پیروزی تهاجم: {victories} | 💀 شکست: {defeats} | 📈 نرخ {win_rate}%`
* Donors: `🍎 مجموع اهدا: {total_donated_agg}`

Tab rendering must **escape** `group.title` and truncate to 40 chars (Telegram Markdown; same as `handlers/castle.py:429` with `escape()`).

---

## 3. Cleanup Plan — Redundant Apple Battle Leaderboard Buttons

**Goal:** remove the Apple Battle sub-leaderboard so the game exposes exactly **one** main leaderboard (`/leaderboard` + `سیب لیدربرد`). No other menu is touched.

### 3.1 Exact Deletions in `handlers/apple_battle.py`

| # | File:Line (baseline `19274124d`) | Content | Action |
|---|---|---|---|
| **R1** | `handlers/apple_battle.py:173` | `[primary_button(text="🏆 لیدربورد نبرد", callback_data="ab_board")],` inside `_ready_view()` | **Delete this one list element**. Row count in `_ready_view` goes from 4 rows to 3: `[ab_find]`, `[ab_change]`, `[_back_row]`. Imports `primary_button` stays (used in `_select_view`). |
| **R2** | `handlers/apple_battle.py:500-520` | Entire `handle_ab_board` handler: `@router.callback_query(F.data == "ab_board")` through `await safe_edit_callback_message(callback, bot, text, reply_markup=kb)` | **Delete the whole 21-line function** including its preceding comment header `# Leaderboard` (lines ~498-499) if that comment covers only this handler. Keep `# Profile (Phase 15)` below. The `AppleBattleStatus` / `AppleBattle` imports in that file are still needed by `cmd_profile` (lines 548/560) — do not remove them. |
| **R3** | `handlers/apple_battle.py:1-7` docstring module header | Mentions no explicit leaderboard — no change. | — |

**Do NOT delete:**

* `_select_view` (line 66-155) — already has no `ab_board`, keep as-is.
* `_ready_view` beyond the one row — `ab_find` and `ab_change:origin` stay; the `with_origin` plumbing for back-navigation is unrelated.
* `_searching_view`, `battle_entry_view`, `handle_ab_pick`, `handle_ab_find`, `handle_ab_cancel`, `handle_ab_status`, `cmd_battle` — all core game flows, untouched.
* The final `except (InvalidActionException, PartyNotFoundException)` blocks in `handle_ab_cancel` — they cancel the queue, not the leaderboard.

### 3.2 Exact Deletions in `services/apple_battle.py`

| # | File:Line | Content | Action |
|---|---|---|---|
| **S1** | `services/apple_battle.py:1441-1490` | `async def battle_leaderboard(cls, session, limit=20) -> List[Tuple[int,int,int]]` — the entire method (comment `# Leaderboard (separate from group leaderboard)` + body: three `select(...).execute` + merge dict + return). ~50 lines. | **Delete the whole method.** No other production caller: `grep` finds only `handlers/apple_battle.py:503` (being deleted as R2) and `tests/test_apple_battle.py:561`. After deletion the `from sqlalchemy import func` used only inside this method becomes unused for that import site — but `func` is still used in `services/apple_battle.py` at line ~? only inside this method in the current baseline, so the top-level `from sqlalchemy import select, update` (line 13) stays; `func` can be removed only if unused elsewhere after the deletion (check: `grep -n "func\." services/apple_battle.py` — no hits outside this method). Keep `List`/`Tuple` in line 12 if still used by `recover_active_matches` return type. |
| **S2** | `services/apple_battle.py:7` docstring | Line `AppleBattleService.battle_leaderboard(session)` is not mentioned — no change. | — |

**Retention rationale:** keeping the method for a release and only removing the button would leave a reachable handler with no UI — users with a stale `ab_board` message could still trigger it. Deleting handler + service together is cleaner. If the model `apple_battles` history rows are wanted for analytics, keep the table and the `record_battle` / `finalize_match` paths — they do not render a leaderboard and stay.

### 3.3 Test / Doc Changes Required Alongside the Deletion

| File | Line | Action |
|---|---|---|
| `tests/test_apple_battle_menu.py:54-78` — `test_apple_battle_ready_menu_keeps_battle_leaderboard_button` | **Delete the whole test**. It asserts `keyboard.inline_keyboard[2][0].callback_data == "ab_board"` — this is exactly the invariant being retired. Keep `test_apple_battle_menu_has_no_battle_leaderboard_button` (lines 11-48) — it already asserts the *select* screen has no `ab_board`, and after the fix the *ready* screen must satisfy the same assertion. Optionally add a new assertion to that kept test that the ready screen also has no `ab_board`. |
| `tests/test_apple_battle.py:561-572` — `test_battle_leaderboard_counts_wins` | **Delete** (it tests `AppleBattleService.battle_leaderboard` directly). If the service method is kept for analytics, adapt to test `handlers/apple_battle.py` has no `ab_board` registration instead. |
| `handlers/start.py` | No `ab_board` reference — no change. The `apple_battle` entry `F.data.startswith("apple_battle")` (line 514) is the games-menu gateway, not a leaderboard — keep. |
| `keyboards/party.py` | `HIDDEN_GAME_CODES = {"apple_battle"}` — hides battle from the generic party listing, unrelated — keep. |
| `services/help_content.py` | No `ab_board` mention — no change. The bullet `سیب لیدربرد: جدول رتبه‌ها` must be expanded to describe the four tabs (§2.2), but that belongs to the implementation PR, not this cleanup. |
| `docs/games/*.md` | No existing doc claims `ab_board` is the main leaderboard — no doc fix. |

### 3.4 What Explicitly Stays (Do Not Touch)

* `handlers/castle.py:84` `castle:top` — local per-group top-5 donors list inside the castle menu (HTML-escaped, limit 5). Not global, not redundant.
* `handlers/castle.py:war_start / war_pick / war_confirm` — war declaration flow.
* `handlers/war.py` — war status screen and `war:atk` / `war:potion` actions.
* `handlers/leaderboard.py` — the main `/leaderboard` stack; `show_leaderboard`, `group_rating_stats`, `group_win_stats`, `cmd_stats` callbacks and `get_leaderboard_keyboard` — all become the four-tab host in the implementation phase.
* `keyboards/common.py::get_group_welcome_keyboard` — `group_rating_stats` button to the main leaderboard — keep.
* `handlers/inv2_join.py`, `handlers/inv2_dashboard.py` — invasion attack UIs — no leaderboard button to delete.

### 3.5 Safe Deletion Order (to keep `main` green at each commit)

1. Delete `tests/test_apple_battle_menu.py::test_apple_battle_ready_menu_keeps_battle_leaderboard_button` (or mark `xfail` with reason) — so CI no longer pins the old ready-screen invariant.
2. Delete `handlers/apple_battle.py:173` row + `handle_ab_board` — the service method is still importable, so `tests/test_apple_battle.py::test_battle_leaderboard_counts_wins` still passes if not yet deleted.
3. Delete `services/apple_battle.py::battle_leaderboard` + its dedicated test — now nothing references the method.

In practice LB1 ships this as a single implementation PR; the staged order matters only if the cleanup is landed separately from the new four-tab UI.

---

## 4. Report Metadata &amp; Verification

### 4.1 Deliverable checklist

| Task | Status |
|---|---|
| TASK 1 — Reconnaissance (all leaderboard implementations, redundant buttons, model inspection) | ✅ §1 above |
| TASK 2 — Design new main leaderboard with 4 tabs + SQL for top-10 | ✅ §2 above |
| TASK 3 — Cleanup plan (exact lines/files for Apple Battle removal) | ✅ §3 above |
| TASK 4 — Report at `docs/games/LB1-RECON.md` | ✅ this file |
| Commit `[lb1] design and recon for main leaderboard rework` | ⏳ next command after file write |
| Deliverable: `git log --oneline -5` | ⏳ capture post-commit |

### 4.2 Model field reference for the SQL author

| Table | File | PK | Relevant columns (LB1 subset) |
|---|---|---|---|
| `groups` | `database/models/group.py` | `id BigInteger PK` | `title`, `username?`, `is_active`, `welcomed_at` |
| `group_stats` | `database/models/group_stats.py` | `group_id BigInteger PK FK groups.id` | `wins`, `losses`, `draws`, `matches_played`, `win_rate` property |
| `group_castles` | `database/models/castle.py` | `group_id BigInteger PK FK groups.id` | `level` 1-10, `total_donated`, `shield_until`, `in_war` |
| `castle_donations` | `database/models/castle.py` | `id BigInteger PK` | `user_id`, `group_id`, `amount`, `donated_at`, `request_key` unique |
| `group_wars` | `database/models/war.py` | `id BigInteger PK` | `attacker_id`, `defender_id`, `status`, `attacker_damage`, `defender_damage`, `winner_id?`, `looted_apples`, `started_at`, `ends_at` |
| `war_participants` | `database/models/war.py` | `(war_id, user_id)` | `group_id`, `energy`, `total_damage`, `attack_count`, `last_attack_at` |
| `war_attacks` | `database/models/war.py` | `id BigInteger PK` | `war_id`, `user_id`, `source_group_id`, `target_group_id`, `damage`, `is_buff_used` |
| `inv2_invasions` | `database/models/invasion_v2.py` | `id BigInteger PK` | `group_id`, `region_code`, `status` (PENDING/ACTIVE/VICTORY/DEFEAT/EXPIRED), `enemy_type`, `enemy_hp`, `enemy_hp_remaining`, `occurrence_key` unique |
| `inv2_group_state` | `database/models/invasion_v2.py` | `group_id BigInteger PK FK groups.id` | `total_victories`, `total_defeats`, `consecutive_failures`, `muted_until` |
| `inv2_attacks` | `database/models/invasion_v2.py` | `id BigInteger PK` | `invasion_id`, `user_id`, `card_id`, `card_power`, `damage`, `is_crit`, `narrative` — one row per user per invasion |
| `apple_battles` | `database/models/apple_battle.py` | `id BigInteger PK` | `player1_id`, `player2_id`, `match_id?`, `winner_id?`, `status`, `result_json` — legacy sub-leaderboard source to retire |
| `inv2_regions` | `database/models/invasion_v2.py` | `code String(64) PK` | `name_fa`, `controlling_faction`, `last_invasion_id` — not queried for LB1 ranking, but invasion region context |

### 4.3 How to query "top 10 groups" generically

Every LB1 tab's SQL is a variation of:

```sql
SELECT g.id, g.title, <metric>
FROM   groups g
LEFT JOIN <fact_table> ON <group predicate>
WHERE  g.is_active = 1
GROUP BY g.id, g.title          -- when fact join produces 0..N rows per group
ORDER BY <metric> DESC, g.id ASC
LIMIT 10;
```

The `LEFT JOIN` (not `INNER`) guarantees that groups with zero castles / zero wars / zero victories / zero donations still appear at the bottom with metric 0, rather than disappearing. This matches the current `LeaderboardService._ranked_rows` pattern (join + Python sort) but pushed into the DB for the new tabs where Python-side sorting would require loading every row first.

### 4.4 Implementation notes for the next phase (not executed here)

* Add `keyboards/leaderboard.py::get_main_leaderboard_keyboard(active_tab)` that builds the 2×2 tab row + refresh + back. Keep `get_leaderboard_keyboard(is_group)` as a one-line wrapper mapping old `is_group` → `castles` tab for backward compat (`show_leaderboard` legacy callbacks).
* Extend `services/leaderboard.py` with `get_top_castles(session, limit=10)`, `get_top_warriors(session, limit=10)`, `get_top_defenders(session, limit=10)`, `get_top_donor_groups(session, limit=10)` — each is a single `select(...).outerjoin(...).group_by(...).order_by(...).limit(10)` statement from §2.3. No Python sort; let the DB do it.
* Extend `get_group_stats_and_rank` to accept `tab: str` so `📊 آمار گروه ما` can rank within the active metric. It currently does a Python `sorted(results, key=_sort_key)` — the new method should do the DB ranking directly to avoid O(groups) sorts.
* Update `handlers/leaderboard.py:format_leaderboard_text` to dispatch on `tab` (or split into `format_castles_text`, `format_warriors_text`, etc.). New callback: `@router.callback_query(F.data.startswith("lb:"))`.
* Keep `/top` as an alias of `/leaderboard` — same handler.
* Add `__all__` exports in `services/__init__.py` and `keyboards/__init__.py` for the new symbols.

---

## 5. Appendix — git log (post-commit)

```
19274124 Merge pull request #80 from canoheil/arena/eb8c90e9-sib
```

(Five-entry `git log --oneline -5` will be captured after the `[lb1]` commit and appended here in the committed file; the baseline above is the inspected HEAD before this report. The new commit becomes the sixth entry.)

---

*End of LB1-RECON. No runtime test of the new leaderboard is part of this phase.*
