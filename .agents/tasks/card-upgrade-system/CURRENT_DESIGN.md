# CURRENT DESIGN — audit of the card system (before this task)

Skills live in `.agents/skills/` (NOT `agents/skills/`): tasks, telegram (+aiogram-patterns,
keyboard-design), telegram-bot-api-*, persian-writing, flux-image-best-practices.

## Cards
- `Character` (`database/models/character.py`, seed `data/characters_seed.json`): base
  `power` / `speed` / `intelligence` (the exactly-3 attributes; Persian: قدرت 💪 / سرعت ⚡ / هوش 🧠).
  Real Level-1 bases (all sum to 60): sib_sorkh 22/20/18, sib_sabz 16/18/26,
  sib_talayi 20/24/16, sib_mahi 14/16/30. No rarity (deprecated column), factions are lore only.
- `PlayerCard` (`database/models/player_card.py`): `level` (default 1) + STORED `power/speed/
  intelligence` + `boosted_*` counters. One card per (user, character). Acquire copies the base.
- OLD upgrade system (`services/player_cards.py`): `level_budget` (config `card_level_budgets`
  60/120/…), `upgrade_cost` (config base 100 × 1.5^n, paid in wallet apples), `upgrade(boost_*)`,
  `reallocate`. UI in `handlers/shop.py` (`card_panel:` combined detail+upgrade panel, `card_up:` ×4
  allocation buttons). Ledger idempotency key used by old system: `card_upgrade:{card}:{level+1}`
  (!! historical ledger rows with that key exist — new system must use a different key namespace).
- Old system DID persist modified values: stats + boosted_* (+ level) in `player_cards`.

## Consumers of card stats
`services/wiki.py` (collection + detail), `services/apple_battle.py` (`_effective_score`, plan),
`handlers/apple_battle.py` (ready view), `handlers/trading.py` (offer text), `services/bot_match.py`
(bot card), `services/player_cards.py` (summary), tests.
Trading requires equal levels (keeps working; level lives on the row that moves with the card).

## Economy
- Normal Apple 🍎 = WALLET currency (`apple_wallets`, ledger `apple_transactions`, `AppleWalletService`).
  There is no "Red Apple" inventory item; the user's "Red Apple" == the normal wallet apple
  (`apple_normal` drop outcome, label «سیب معمولی»/«سیب»). (A *character* named Red Apple exists: sib_sorkh.)
- Special apples = inventory (`user_items`, `ItemService`): `apple_gold` 🪙 سیب طلایی,
  `apple_diamond` 💎 سیب الماسی, `apple_obsidian` 🌑 سیب ابسیدینی.
- Harvest: 1 roll/plot/cycle, cycle 12h, 5..10 plots, 3 apples per normal roll. Weights by tree magic level
  (normal/gold/diamond/obsidian): L0 70/20/9/1 … L5 15/34/34/17.
- Sell prices: gold 50, diamond 100, obsidian 200 apples. Buy: magic_powder 40. Card prices 100-300,
  plot prices 100..2000, sib_claim 5 apples / 5 min.
- Config is data-driven: `services/config_store.py::CONFIG_DEFAULTS` (+ `system_config` table overrides).

## Conventions
- Locks: `SELECT … FOR UPDATE` via `with_for_update()`; services flush, callers (`get_session()`) commit.
  SQLite tests can't exercise row locks (`requires_row_locks` marker).
- Navigation: `services/nav.py` (`with_origin`, `parse_callback`, `back_label_for`), `nav_back_row`,
  personal panels via `services/panel.py` (`OWNER_SCOPED_CALLBACKS`, `PERSONAL_PANEL_TYPES`).
- Screens render through `safe_edit_callback_message(..., photo=)` / `render_banner`.
- 1:1 art convention: `assets/items/<key>.jpg` (1024²) resolved by `AssetService.resolve_photo`
  (DB `bot_assets` override via /setasset, else bundled file). Art is text-free, warm gouache.
- Migrations: `database/migrations/versions/NNN_name.py`, head = `021_bot_fallback_matchmaking`.
- Tests: `/tmp/runtests.sh` style (see KNOWLEDGE.md); baseline = 2 env-only failures (MySQL).
