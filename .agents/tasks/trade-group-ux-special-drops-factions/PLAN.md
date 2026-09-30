# PLAN — Trade, Group UX, Special Drops, Active Game, Cards, Factions

Goal: extend the existing architecture (no rewrites), keep the diff focused and
keep every currently working behaviour intact.

## Guiding decisions

1. **Reuse, don't fork.** Trade keeps `Trade`/`TradeService`; the group entry
   point reuses `apple_trade` + the existing nav + panel services.
2. **Data over code.** Drop captions, factions and lore live in
   `ConfigService` defaults / JSON data layers + one additive migration, never
   in hardcoded `if faction == ...` chains.
3. **One source of truth for "active game".** `services/session.py` owns the
   definition; both validation and the Active Games UI derive from it, and a
   reconciliation pass repairs records that can no longer be live.
4. **Additive DB changes only.** No column/table is dropped; `characters.rarity`
   stays as a deprecated column, `characters.faction_id` is added nullable and
   backfilled.
5. **No artwork changes** (`assets/**` is off-limits).

## Ordered changes

### P1 — Trade (group discoverability + real proposal flow + atomicity)
- `services/panel.py`: `trade_ok:` / `trade_cancel:` leave
  `OWNER_SCOPED_CALLBACKS` — trade confirmation is authorized server-side and
  BOTH parties must be able to press buttons on the SAME shared group message.
- `services/trading.py`: row-lock the trade + both cards (`with_for_update`),
  re-verify ownership/status inside the executing transaction, make duplicate
  confirmations safe, expose `get_trade`, keep the public API.
- `handlers/trading.py`: group-aware hub, "propose a trade" flow
  (`trade_new` → `trade_offer:<card_id>` → shared offer message →
  `trade_join:<proposer>:<card>` → `trade_pick:<proposer>:<card>:<my_card>`),
  shared trade detail view with confirm/cancel + refresh, all through the
  existing safe-edit/render helpers and nav grammar.
- `keyboards/common.py`: «🔁 مبادله کارت» in the group main menu.

### P2 — Group menu: Games + Guide
- `keyboards/common.py::get_group_welcome_keyboard()` gains «🎮 بازی‌ها»
  (`group_games_panel`) and «📖 راهنما» (`help_menu`), keeping the existing
  buttons. No new navigation logic; back never touches match/party state.

### P3 — Special apple drop captions
- `services/config_store.py`: per-outcome `caption` (Golden / Diamond /
  Obsidian escalate in tone) next to the existing `message`/`asset`.
- `services/garden.py`: `HarvestOutcome.caption` populated from the entry.
- `handlers/garden.py`: pass the caption into `AssetService.send(...)` so the
  special-apple image is sent WITH its caption (single and harvest-all paths).
- Weights untouched (70/20/9/1 at magic level 0).

### P4 — Ghost active game
- `services/session.py`: canonical `ACTIVE_PARTY_STATUSES` /
  `ACTIVE_MATCH_STATES`, per-group live-party listing, per-user active records
  and `reconcile_stale_state()` (queue without a live entry, IN_MATCH party
  whose match is missing/terminal, matches with NULL/expired deadlines, legacy
  match states).
- `services/party/service.py::get_active_parties_for_group` delegates to the
  canonical listing.
- `services/recovery/service.py` + `services/maintenance/service.py` run the
  reconciliation (startup + periodic).
- `handlers/party/handlers.py` + `keyboards/party.py`: Active Games shows every
  live status (searching / in match) with the right actions (cancel search),
  so nothing that blocks a player can be invisible.

### P5 — Rarity removal
- Drop the `Rarity` class and every behavioural use: validation, labels, render,
  balance keys, snapshots, wiki sections/sorting, admin list, seed data,
  exports, unused import. Keep (deprecate) the DB column so no user data is lost.

### P6 — Factions
- `database/models/faction.py`: `Faction` (code, names, lore, territory, symbol,
  order, active) — fully data-driven, no code change to add Faction 4+.
- `Character.faction_id` (nullable FK) + `faction_role_fa` (lore title) +
  relationship, exposed through `Character.faction`/render helpers.
- Migration `020_factions`: create table, add nullable columns, seed the three
  factions, backfill the four existing cards by code — safe on existing data.
- `data/factions_seed.json` + faction keys/updated lore in
  `data/characters_seed.json`; `database/connection.py` seeds factions and
  back-fills only NULL faction fields for existing rows (never overwrites).
- `services/factions.py` (list/get/render/assign) used by wiki, collection and
  card detail; world name stays a configurable `world_name` config key
  (`None` default, "Avarion" is NOT canon).

### P7 — Docs, tests, full regression
- README + TODO/DECISIONS entries, task STEP files, new test modules
  (`test_trade_group_flow.py`, `test_group_menu.py`, `test_special_drop_captions.py`,
  `test_active_game_consistency.py`, updates to `test_characters.py`/`test_wiki.py`),
  full suite run on the available engine, final report.

## Out of scope (explicitly)
Faction gameplay effects, faction relationships mechanics, invasion/warfare,
territory conquest, new tradable asset types, artwork changes, drop-rate changes,
Avarion as canon.
