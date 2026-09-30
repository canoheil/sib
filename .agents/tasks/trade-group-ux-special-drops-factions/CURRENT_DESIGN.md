# CURRENT_DESIGN — Trade / Group UX / Special Drops / Active Game / Cards / Factions

Audit performed against `c781d3e` (branch `arena/01a0f19a-sib`). All paths are
relative to the repository root. Package resolution: the repository root itself
is imported as the `sib` namespace package (`sib.core.*`, `sib.database.*`) and
the nested `sib/bots/` directory supplies `sib.bots.*` (see KNOWLEDGE.md).

## 1. Trade

| Concern | Location |
| --- | --- |
| Model | `database/models/trade.py` — `Trade` (`trades`), `TradeStatus` (PENDING/COMPLETED/CANCELLED/EXPIRED). Columns: `proposer_id`, `recipient_id`, `proposer_card_id`, `recipient_card_id`, `status`, `proposer_confirmed`, `recipient_confirmed`, `completed_at`, `expires_at`. |
| Service | `services/trading.py` — `TradeService.create/confirm/cancel/list_pending/can_trade_today`. Daily quota = `ConfigService.trades_per_day`; expiry = `trade_expiration_hours`. `_execute()` swaps `PlayerCard.user_id`, clears `is_favorite`. |
| Handlers | `handlers/trading.py` — only `/trade` + `apple_trade` (personal view) + `trade_refresh` + `trade_ok:<id>` + `trade_cancel:<id>`. **No creation UI exists at all** (nothing calls `TradeService.create`). |
| Entry points | Private keyboard `get_private_welcome_keyboard()` has «🔄 مبادله کارت» → `apple_trade`. Group keyboard (`get_group_welcome_keyboard()`) has **no** trade button. |
| Panel ownership | `apple_trade` claims `panel_type="trade"` (a PERSONAL panel → only the owner may press any button on it). `trade_ok:` / `trade_cancel:` are in `OWNER_SCOPED_CALLBACKS` (`services/panel.py`). |
| Nav | `apple_trade`, `main_menu` ∈ `services/nav.py::KNOWN_SCREENS`. |
| Tests | `tests/test_trading.py` (service-level only). |

## 2. Group menus / Games / Guide

- `keyboards/common.py::get_group_welcome_keyboard()` → «🎮 بازی‌های فعال گروه» (`group_games_panel`), «🏆 رتبه و آمار گروه», «🎴 کارت‌های من», «🛒 فروشگاه», «👤 پروفایل من». **No Games menu entry, no Guide.**
- `handlers/party/handlers.py`: `handle_games_panel` (shared panel, releases ownership), `handle_active_games`, `create_new_party_menu`, `select_game:`, `create_party:`.
- Guide: `handlers/start.py::handle_help_menu` + `help_page:<topic>` (works in any chat; its only Back target is `main_menu`).
- Back/refresh helpers: `keyboards/common.py::nav_back_row/nav_refresh_row`; context-aware back = `services/nav.py::back_label_for/back_target`.

## 3. Harvest / special apple drops

- Weights (locked): `services/config_store.py::CONFIG_DEFAULTS["garden_drop_weights"]["0"]` = 70/20/9/1 (`apple_normal`/`apple_gold`/`apple_diamond`/`apple_obsidian`); higher magic levels shift toward specials.
- Presentation: `CONFIG_DEFAULTS["garden_drop_entries"]` per key: `message`, `asset` (`golden_apple` / `diamond_apple` / `obsidian_apple`).
- Roll: `services/items.py::DropService.roll/weights_for_level`, `roll_drop()` (pure, injectable RNG).
- Harvest: `services/garden.py::_harvest_plot` → `HarvestOutcome(apples, item_key, item_label, message, asset)`.
- UI: `handlers/garden.py::handle_garden_harvest` / `handle_garden_harvest_all` → `_harvest_answer()` (toast) + `_send_asset()` → `AssetService.send(session, bot, chat_id, key, caption=None)`. **The image is sent with no caption.**
- Assets: `services/assets.py::AssetService.resolve_photo/send`; bundled files `assets/items/*.jpg` (**must not be modified**).

## 4. Active game validation vs Active Games UI (ghost bug)

| Concern | Location |
| --- | --- |
| Validation | `services/session.py::SessionService.has_active_session` — blocks on (a) `party_members` rows whose `party.status ∈ {OPEN, FULL, IN_QUEUE, IN_MATCH}` (**any group**), (b) `match_players.is_active` rows whose `match.state ∈ {WAITING_FOR_OPPONENT, OPPONENT_FOUND, WAITING_FOR_ACCEPTANCE, ACTIVE}`. |
| Enforcer | `services/party/service.py::create_party` / `join_party` raise `ActiveSessionExistsException` (`core/errors.py`). |
| UI | `services/party/service.py::get_active_parties_for_group` → **only `OPEN`/`FULL`**, only the current group. Rendered by `handlers/party/handlers.py::build_active_games_menu` + `keyboards/party.py::get_active_parties_keyboard`. |
| Lifecycle | `matchmaking/matcher.py` (enqueue → IN_QUEUE; match → both parties IN_MATCH + `WAITING_FOR_ACCEPTANCE`), `matchmaking/acceptance.py` (accept → ACTIVE, reject/timeout → CANCELLED + parties back to FULL), `services/match_result.py::_free_parties` (IN_MATCH → CANCELLED on finish), `services/timeout/service.py` (acceptance + turn deadlines), `services/maintenance/service.py` (queue expiry; only `is_active == True` entries), `services/recovery/service.py` (startup). |
| Gaps found | parties in `IN_QUEUE`/`IN_MATCH` are invisible in the UI but block validation; a party whose queue row is gone/inactive is never restored; matches in legacy states (`CREATED`/`WAITING_FOR_OPPONENT`/`OPPONENT_FOUND`) and `ACTIVE` matches with a NULL `turn_deadline` are never cleaned; nothing reconciles the two definitions. |

## 5. Cards, rarity, metadata

- `database/models/character.py` — `Character` (data-driven) with `rarity: String(20) NOT NULL server_default "COMMON"` (migration `012_characters.py`), `Rarity` class (COMMON/RARE/EPIC/LEGENDARY), `snapshot()` includes `rarity`.
- `database/models/player_card.py` + `services/player_cards.py` — ownership, levels, allocation budget, single favorite. **No per-card rarity.**
- Rarity readers: `services/characters.py` (`validate_rarity`, `RARITY_LABELS_FA`, `render_character_card`), `handlers/wiki.py` (`RARITY_SECTION_HEADERS`, grouping), `services/wiki.py` (`order_by(Character.rarity.desc())`), `handlers/admin_meta.py` (admin list), `database/connection.py::seed_characters` (`entry.get("rarity")`), `data/characters_seed.json`, `database/__init__.py` + `database/models/__init__.py` (exports), unused import in `services/player_cards.py`.
- Card art: `assets/characters/*.jpg`, `assets/characters_raw/*.jpg`, `scripts/frame_card.py`, `services/character_art.py` — **do not touch** (owner adds Faction symbols manually later).

## 6. Database / migrations

- Alembic versions `001`..`019` (`019_magic_trees` is head), style: additive `op.add_column` + portable `sa.text` data fixes, explicit `downgrade`.
- Seeds run at startup: `database/connection.py::init_db` → `seed_default_data` + `seed_characters` (early-returns when any character row exists — so existing installs never receive new data-layer content).
- Tests: `tests/conftest.py` requires `sib_TEST_DATABASE_URL` containing `sibcom_sib_test`; SQLite is supported via a BigInteger→Integer DDL shim; `requires_row_locks` marks MySQL-only races; every table is cleaned + default-seeded per test.
