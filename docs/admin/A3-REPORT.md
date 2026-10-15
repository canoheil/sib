# Phase A3 — Rewrite /start texts + remove extra buttons

## Recon findings

### Private /start handler

- **Handler:** `handlers/start.py:32-91` — `handle_start` (`@router.message(CommandStart())` at `:32`).
  - Private branch: `:56-90`. Membership gate (not a channel member): `:57-71` (text `:64-67`, keyboard `get_membership_keyboard` at `:68`). Member welcome: `:73-90`.
  - **Old private welcome text:** `handlers/start.py:73-86` — leads with "من ربات بازی گروهی آنلاین تلگرام هستم" and a bulleted list of the legacy mini-games ("بازی‌های فعال"), no mention of اواریا / شیاسیب, no support handle.
  - **Old keyboard:** `get_private_welcome_keyboard(settings.BOT_USERNAME)` at `handlers/start.py:87-89`.
- **Membership-verified welcome (same /start flow):** `handlers/start.py:94-122` — `handle_check_membership`; the gate's "✅ بررسی عضویت" button (only reachable from private `/start`, `keyboards/common.py:32`) edits the gate message into a second welcome at `:112-121`, reusing the same 9-button `get_private_welcome_keyboard` at `:121`. The membership gate itself (`get_membership_keyboard`, `keyboards/common.py:22-36`) is required functionality and is not touched.

### Private /start keyboard (before)

`get_private_welcome_keyboard` — `keyboards/common.py:40-98`. **Nine inline buttons** (the task's "EIGHT to remove" plus the one to keep):

| # | Label | callback_data / url |
|---|---|---|
| 1 | 🌳 باغ من | `apple_garden` |
| 2 | 🎴 کارت‌های من | `apple_cards` |
| 3 | 🛒 فروشگاه | `apple_shop` |
| 4 | 📚 ویکی کاراکترها | `apple_wiki` |
| 5 | 🔄 مبادله کارت | `apple_trade` |
| 6 | 🎁 صندوقچه گنج روزانه | `apple_chest` |
| 7 | 👤 پروفایل من | `apple_profile` |
| 8 | ➕ افزودن سیب به گروه | URL `https://t.me/{BOT_USERNAME}?startgroup=true` |
| 9 | 🎮 بازی‌ها و راهنما | `help_menu` |

Note: `get_private_welcome_keyboard` is ALSO the private **main menu** (`_main_menu_text_and_kb`, `handlers/start.py:618`) — it stays intact so the removed features remain reachable from "سیب منو" / `main_menu`. A new dedicated `/start` keyboard is added instead of editing this one.

### Group /start handler

- **Handler:** `handlers/start.py:45-54` (group/supergroup branch of `handle_start`).
- **Old group text:** `handlers/start.py:47-52` — "سیب وارد شد! سلام به برو بچِ {title}!" + "گروه شما ثبت شد…"; no mention of اواریا / شیاسیب, no support handle.
- **Keyboard:** `get_group_welcome_keyboard()` at `handlers/start.py:53` → `keyboards/common.py:102-160`. Six buttons: 🎮 بازی‌ها (`group_games_panel`), 📖 راهنما (`help_menu`), 🛒 فروشگاه (`apple_shop`), 👤 پروفایل من (`apple_profile`), 🏆 رتبه و آمار گروه (`group_rating_stats`), 🎁 صندوقچه گنج روزانه (`group_chest`). **Unchanged by A3.**

### Legacy mini-games still registered

- Routers imported and included: `handlers/__init__.py:11-15` and `:52-56` (`rps_router`, `high_card_router`, `tic_tac_toe_router`, `connect_four_router`, `battleship_router`).
- Engines: `games/registry.py:12` (`high_card`, `rock_paper_scissors`, `tic_tac_toe`, `connect_four`, `battleship`).
- DB seed rows: `database/connection.py:76-108` (`seed_default_data` seeds all five games). Not touched by A3.

### Bot username / deep link

- `core/config.py:22-26` — `BOT_USERNAME`, default `sib_game_bot` (env override `BOT_USERNAME`/`TELEGRAM_BOT_USERNAME`).
- Deep link built at `keyboards/common.py:41`: `https://t.me/{bot_username}?startgroup=true`.

### Other intro / help / about text (context, not changed)

- `HELP_TOPICS` (per-game guide pages): `handlers/start.py:126-186`; guide index `handle_help_menu` `:188-207`; pages `:209-237`.
- Main-menu text: `_main_menu_text_and_kb` `handlers/start.py:605-619`.
- Group auto-welcome on bot-add (A2, different message): `handlers/group_lifecycle.py:20` `_WELCOME_TEXT`.
- Membership-gate strings: `core/errors.py:66-67` (`membership_check_success/failed`).
- Group leaderboard: `handlers/leaderboard.py:23,26` ("🏆 برترین گروه‌های سیب"); buttons `keyboards/leaderboard.py:26,41` ("🏆 برترین گروه‌ها").

### invasion_enabled

- Seeded by migration `database/migrations/versions/036_invasion_v2.py:131` (`'false'`); toggled only via `services/invasion_v2/admin_controls.py:45,65`. **A3 does not touch it.**

### Baseline tests

- `tests/test_handlers.py` pins the three old `/start` texts (`:46`, `:63`, `:85`) — updated alongside the rewrites. All other suites use `get_private_welcome_keyboard` / `get_group_welcome_keyboard` directly (both unchanged as functions).
