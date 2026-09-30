# FINAL REPORT — Trade, Group UX, Special Drops, Ghost Game, Cards, Factions

Branch: `arena/01a0f19a-sib` · Commit: `1d96623` (pushed)
Baseline: `403 passed / 4 skipped / 2 failed` → Final: **`457 passed / 6 skipped / 2 failed`**
(the 2 failures are the pre-existing MySQL-connection artefacts, identical at baseline).

## 1. What changed

### 1.1 Trade — discovered and usable from the Group (Card ↔ Card)
- Group menu gains «🔁 مبادله کارت» (`apple_trade`) next to the existing buttons;
  the private-chat entry still works.
- `services/trading.py` rewritten around a real proposal flow: create →
  shared group offer → join → pick → double confirm → atomic execution.
  Guards: self-trade, same card, daily quota, mixed level, already-PENDING;
  duplicate confirmations are idempotent; `_execute` re-verifies status and
  ownership **inside the executing transaction** and swaps atomically
  (row locks on the trade and both cards; mismatch ⇒ `CANCELLED`, nothing moves).
- `handlers/trading.py` rewritten: hub / proposal / shared offer view, all via
  the existing `render_callback_screen` + nav grammar; `_pending_view` stays
  positional-compatible for existing callers.
- Scheduled cleanup: `TradeService.expire_overdue` now called by
  `MaintenanceService.run_once` (stale offers can no longer block cards/quotas).
- Discoverability check: the router `handlers/trading.py` is registered on the
  real dispatcher (`handlers/__init__.py:45`; `bot.py::register_all_handlers`).

### 1.2 Group menu — 🎮 Games + 📖 Guide
- `get_group_welcome_keyboard()` exposes `group_games_panel` and `help_menu`;
  navigation reuses Back/Home/Refresh.
- Back/Home never emit `cancel_*` / `leave_*` / `forfeit_*` callbacks and never
  change match state (asserted).

### 1.3 Special apple drop captions
- Per-apple `caption` in `CONFIG_DEFAULTS["garden_drop_entries"]` (existing
  ConfigService; no parallel i18n): golden is a lucky event, diamond is rare,
  obsidian escalates to legend scale.
- `HarvestOutcome.caption` is populated and passed to
  `AssetService.send(..., caption=...)` on both single-harvest and harvest-all.
- Drop weights (70/20/9/1) and every image asset are unchanged (pinned by test).

### 1.4 Ghost active game — root cause
Validation counted parties in `OPEN/FULL/IN_QUEUE/IN_MATCH` plus active
`Match`/`MatchPlayer` rows, while the Active Games UI listed only current-group
`OPEN/FULL` parties. `IN_QUEUE`/`IN_MATCH` records were therefore
**invisible but blocking**, and nothing reconciled records that could no longer
be live (zombie `IN_MATCH` without a match, queue row removed, match without a
deadline) — hence “an active game exists” with an empty list.
Fix: `services/session.py` is now the single source of truth (live predicates,
per-group listing, `lock_user`, delegating guards) plus idempotent
`reconcile_stale_state()` used by recovery (startup) and maintenance
(every cycle, 600 s grace); the UI lists every live status with the right
actions. Regression tests cover: active blocks, completed/cancelled don’t,
stale session no false positive, UI = validation, recovery/cleanup, and
concurrency.

### 1.5 Cards — rarity de-classified
- `Rarity` class and all behavioural usage removed (validation, render,
  sorting/filtering, wiki, shop, admin list, seed data, serialization).
- `characters.rarity` column deliberately **kept** (deprecated, default
  `COMMON`) so historical values survive; it is excluded from `snapshot()` and
  cannot influence gameplay. No destructive migration.

### 1.6 Card factions — data-driven lore only
- `Faction` model (`code/name_fa/name_en/lore_fa/territory_fa/symbol/display_order/is_active`),
  `Character.faction_id` (nullable FK, indexed) + `faction_role_fa`.
- `data/factions_seed.json`: Empire 👑, Sages 🔮, Moon Descendants 🌙
  (territory «درّه ماه (Moonfall Valley)»).
- `services/factions.py` is the only faction-aware service: list/get/
  for-character/seed (never overwrites)/backfill (NULL-only). No
  `if faction == ...` in generic card logic; adding Faction 4+ is data-only
  (verified by test).
- Surfaces: shop card view, wiki ordering (by faction `display_order`) and
  faction-grouped index, owned-card detail; lore text only.
- No bonuses/resistances/matchmaking/trade restrictions; no future cards
  (King / Sage King / royal guard); no invasion/faction-war/conquest;
  “Avarion” is not canon; Moon Descendants’ goal is left undefined (all pinned
  by tests).

## 2. Files (48)

- New: `database/models/faction.py`, `services/factions.py`,
  `data/factions_seed.json`, `database/migrations/versions/020_factions.py`,
  `tests/test_trade_group_flow.py`, `tests/test_ghost_active_game.py`,
  `tests/test_group_menu_games_guide.py`, `tests/test_special_apple_captions.py`,
  `tests/test_factions.py`, task bank under `.agents/tasks/…`.
- Notable edits: `services/{trading,session,party/service,recovery/service,maintenance/service,characters,wiki,player_cards,panel,config_store,garden}.py`,
  `handlers/{trading,start,party/handlers,party/renderer,garden,wiki,shop,admin_meta}.py`,
  `keyboards/{common,party}.py`, `database/{connection,models/character,models/__init__,__init__}.py`,
  `data/characters_seed.json`, `tests/{conftest,test_characters}.py`, `.gitignore`.

## 3. DB / migrations

- `020_factions` (down_revision `019_magic_trees`): `sa.inspect`-guarded DDL;
  creates `factions` if missing, adds nullable `characters.faction_id`
  (+ index, FK) and `faction_role_fa`; inserts the 3 factions only when absent;
  backfills faction/role **only where NULL** (sib_sorkh→empire,
  sib_sabz/sib_talayi→sages, sib_mahi→moon_descendants). Downgrade removes only
  what it added. The deprecated `rarity` column is untouched.
- `init_db` seeds factions on every boot and runs the NULL-only backfill, so
  existing installations (whose character seed early-returns) also get valid
  faction state.
- `tests/test_migrations.py`: single head + stamp/current pass with `020`.
- Deployment applies it through the standard `alembic upgrade head`
  (`Dockerfile` CMD, `deploy/deploy.sh`, `RUNBOOK.md`).

## 4. Tests added (55)

| suite | tests | focus |
|---|---|---|
| `test_trade_group_flow.py` | 13 (12 pass + 1 row-lock skip) | group entry, valid trade, ownership/stale/cancel/duplicate/concurrency, maintenance sweep |
| `test_ghost_active_game.py` | 20 | active blocks, completed/cancelled don’t, UI = validation, reconciliation, recovery, maintenance, concurrency |
| `test_group_menu_games_guide.py` | 6 | Games/Guide entries, Back/Home match-safety |
| `test_special_apple_captions.py` | 7 | weights 70/20/9/1, distinct escalating captions, caption sent with the image, images untouched |
| `test_factions.py` | 9 | canon data, NULL-only backfill, Faction 4 data-only, no gameplay coupling, no “Avarion”, card-detail lore |

`tests/test_characters.py` was rewritten for the de-classified contract
(including: historical rarity value still readable, no tier label ever rendered).

## 5. Tests executed

- Full suite (SQLite, canonical env): **457 passed, 6 skipped, 2 failed** in 65 s.
  The 2 failures (`test_navigation.py::…back_to_main`,
  `test_stale_and_duplicate_callbacks.py::test_non_existent_match_callback`)
  fail identically at baseline — they need a live MySQL only.
- Focused: factions + migrations 11P; characters/wiki/shop/shop_sections/
  player_cards/balance_lab/admin_center/character_art/factions = **80P**;
  trade module 12P + 1S.
- 6 skips are the `@requires_row_locks` concurrency tests (SQLite).

## 6. §24 audit checklist

- [x] Trade reachable from the Group UI and functional from the Group (router
      registered; tests call the real handlers).
- [x] Card ↔ Card only; future assets can be added to the same flow without
      re-architecture.
- [x] Atomic, ownership-safe finalization; double-spend/race/stale/duplicate/
      cancellation cases covered (row-lock concurrency tests).
- [x] Group menu exposes Games + Guide; Back/Home cannot cancel a match.
- [x] Special-drop probabilities and images unchanged; captions distinct,
      escalating, event-like; localization stays in the existing config.
- [x] Ghost game root-caused (not condition-patched); one definition shared by
      validation and UI; regression tests for every required case.
- [x] Rarity removed as an active classification everywhere; no destructive DB
      change; user data preserved.
- [x] Factions data-driven and extensible; lore-only; no future cards; no
      invasion/faction-war/territory mechanics; Moonfall Valley kept;
      Moon Descendants’ goal undefined; “Avarion” not canon.
- [x] Migration inspected against schema/history, additive and idempotent;
      single-head migration test green.
- [x] Full regression run, no new failures, existing tests not weakened.
- [x] Artwork untouched — `git status -- assets/` is empty; no frame, item,
      character or banner file was modified, and no image was regenerated.

## 7. Limitations / follow-up

- No live MariaDB in this environment: the suite ran on SQLite and the
  row-lock tests skipped. A MySQL run is recommended before production release
  (the 2 pre-existing failures should be re-checked there).
- `020_factions` is validated by inspection + the migration-chain tests; a
  real `alembic upgrade head` on a copy of production data is the last
  pre-deploy check (as with any migration).
- Trade expiry messages are not pushed proactively to the chat; the sweep
  marks offers `EXPIRED` and the UI reflects it. A notification is a possible
  follow-up, not required by the task.
