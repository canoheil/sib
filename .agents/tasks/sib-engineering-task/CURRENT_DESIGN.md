# CURRENT_DESIGN — Sib engineering task (trade / guide / menu / balance / profile / Persian)

## Environment
- Repo root IS the `sib` package: run tests with `PYTHONPATH=<parent of repo>` (deploy scripts set `PYTHONPATH=/home/user:...`).
- Tests need `sib_TEST_DATABASE_URL` containing `sibcom_sib_test`. SQLite works for everything
  except `requires_row_locks` tests (MySQL/MariaDB `SELECT ... FOR UPDATE` races) which skip on SQLite.
- Two tests (`test_navigation.py::test_shop_and_battle_...`, `test_stale_and_duplicate_callbacks.py::test_non_existent_match_callback`)
  do NOT use the `db_session` fixture and therefore hit `settings.DATABASE_URL` — point BOTH env vars
  at the same test DB.

## Trade
| Piece | Location |
|---|---|
| Model | `database/models/trade.py` — `Trade` (`trades`), `TradeStatus` (PENDING/COMPLETED/CANCELLED/EXPIRED). `status` is `String(20)` — new status values need NO migration. |
| Service | `services/trading.py` — `TradeService.create/confirm/cancel/list_pending/can_trade_today/expire_overdue`. Quota = `ConfigService.trades_per_day` counted by `_trades_used_today` (COUNT of trades created today, proposer OR recipient, ANY status). `_execute()` swaps `PlayerCard.user_id`, clears `is_favorite`. |
| Handlers | `handlers/trading.py` — offer (`trade_offer`) → join (`trade_join`) → pick (`trade_pick`, CREATES the trade) → `trade_ok:<id>` / `trade_cancel:<id>` double confirmation. Only `TradeError` is caught in handlers. |
| Card rule | `database/models/player_card.py` — `UNIQUE(user_id, character_id)` ("one card per character"). |
| Maintenance | `services/maintenance/service.py` — `expire_overdue` sweep; comment says stale offers "must stop blocking cards/quotas" (quota part currently broken). |

**Root cause (second-confirm "bot errors")**: `_execute` swaps the two `user_id`s with two
independent UPDATEs. When both cards share `character_id` (or a player already owns a card of the
incoming character) `uq_player_card_user_character` rejects the transient/final state with a raw
`IntegrityError`, which handlers do not catch → generic error; trade stays PENDING; quota (consumed
at `create`) is never released.

**Quota semantics (determined)**: repo tests pin that a created (PENDING) trade and a user-CANCELLED
trade consume the daily slot (`test_one_trade_per_user_per_day`,
`test_cancelled_trade_frees_the_cards_but_still_counts_for_the_day`). Stale (EXPIRED) trades must
NOT (maintenance comment). A FAILED trade (failed finalization) must NOT (bug report).
→ count `status IN (PENDING, COMPLETED, CANCELLED)`; exclude `EXPIRED` and new `FAILED`.

## Guide
- `handlers/start.py`: `HELP_TOPICS` (rps, c4, ttt, battleship, high_card, apple_battle, cards, garden),
  `handle_help_menu` (`help_menu[:origin]`), `handle_help_page` (`help_page:<topic>[:origin]`).
- `services/nav.py`: `parse_callback` strips a trailing KNOWN_SCREENS segment as origin.
  **Bug**: `help_page:apple_battle` → topic `apple_battle` is in `KNOWN_SCREENS` → eaten as origin →
  `base.split(":")[1]` → IndexError → global error handler. Only the no-origin path breaks.

## Main menu / buttons
- `keyboards/common.py`: semantic glass styles — `primary_button` (blue), `success_button` (green),
  `danger_button` (red). Main menus: `get_group_welcome_keyboard` ("🎮 بازی‌ها" → `group_games_panel`)
  and `get_private_welcome_keyboard` ("🎮 بازی‌ها و راهنما" → `help_menu`). Both currently blue `primary`.
- Requirement: the Games button only → green (`success` style), nothing else changes.

## سیبام / سیب من / Profile
- `handlers/start.py`: `handle_sib_balance_word` regex `^\\s*(سیبام|سیب\\s+من)\\s*$` → balance text reply.
- Profile: `handle_sib_profile_word` ("سیب پروفایل") and `handle_apple_profile` (`apple_profile` button) —
  same view, panel_type "profile". Requirement: «سیبام» → balance (+ new basket image);
  «سیب من» → existing Profile flow (reuse, no second profile system).

## Assets / images
- `assets/items/<key>.jpg` — 1024×1024 (1:1) bundled item art (special apples).
- `AssetService.resolve_photo(session, key)` → (file_id, fs_path); `photo_arg(file_id, fs_path)`;
  `AssetService.send(...)`; `/setasset` runtime override via `bot_assets`.
- `assets/banners/<screen>.jpg` — screen banners via `services/banner.py` (warm painterly, text-free).
- Art direction (banner.py docstring): honey gold / cream / sage green / apple red, gouache-watercolor.

## Persian
- Strings live inline in handlers; `core/errors.py` MESSAGES; DECISIONS.md §5: fluent, natural,
  slightly playful "sib" character voice, never formal/bureaucratic or childish.
- Skill: `.agents/skills/persian-writing/` (installed from ali2000hos/persian-writing, registered in
  `skills-lock.json`) — orthography (نیم‌فاصله/ZWNJ, Persian digits, ک/ی not ك/ي), colloquial-written
  register for Telegram/game UI.
