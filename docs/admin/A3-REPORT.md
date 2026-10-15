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

## Stale-text sweep (legacy mini-game / leaderboard references)

Sweep terms: «کارت بالا»، «سنگ، کاغذ، قیچی»، «دوز»، «چهار در یک ردیف»، «کشتی جنگی»، «برترین گروه‌ها». Line numbers are post-A3 (current tree).

| Location (file:line) | Content | Disposition |
|---|---|---|
| `handlers/start.py:49` | New private /start line «🎲 بازی‌های جانبی: کارت بالا، سنگ‌کاغذ‌قیچی، دوز، چهار در یک ردیف، کشتی جنگی.» | **REWRITE (done, MOVE-DOWN)** — replaces the old leading «بازی‌های فعال» bullet list (old `handlers/start.py:78-83`); mini-games are now mentioned once, as secondary content |
| `handlers/start.py:145-146` | HELP_TOPICS «سنگ، کاغذ، قیچی» guide page | **KEEP** — guide page, accurate, reachable via «📖 راهنما» |
| `handlers/start.py:152-153` | HELP_TOPICS «چهار در یک ردیف» guide page | **KEEP** |
| `handlers/start.py:158-159` | HELP_TOPICS «دوز» guide page | **KEEP** |
| `handlers/start.py:164-165` | HELP_TOPICS «کشتی جنگی» guide page | **KEEP** |
| `handlers/start.py:171-173` | HELP_TOPICS «کارت بالا» guide page | **KEEP** |
| `database/connection.py:79-80` | `seed_default_data` row: «کارت بالا (های کارت)» | **KEEP** — canonical games catalog; required for the mini-games to run |
| `database/connection.py:86` | Seed row: «سنگ، کاغذ، قیچی» | **KEEP** |
| `database/connection.py:93` | Seed row: «دوز (تیک‌تاک‌تو)» | **KEEP** |
| `database/connection.py:100` | Seed row: «چهار در یک ردیف» | **KEEP** |
| `database/connection.py:107` | Seed row: «کشتی جنگی (بتل‌شیپ)» | **KEEP** |
| `games/high_card/engine.py:24,259,292` | Engine `name_fa` + match screens «کارت بالا» | **KEEP** — the game itself (removal forbidden) |
| `games/rock_paper_scissors/engine.py:16,255,276` | Engine `name_fa` + match screens «سنگ، کاغذ، قیچی» | **KEEP** |
| `games/tic_tac_toe/engine.py:14,17,209,218` | Engine `name_fa` + match screens «دوز» | **KEEP** |
| `handlers/leaderboard.py:23,26` | Group leaderboard title «🏆 برترین گروه‌های سیب» | **KEEP** — live, accurate feature (group leaderboard exists) |
| `keyboards/leaderboard.py:26,41` | Button «🏆 برترین گروه‌ها» → `show_leaderboard` | **KEEP** — entry point to that live feature |

No handler was deleted or disabled in this sweep. The only rewrite was the private `/start` mini-game list (Task 2); everything else keeps its canonical home (guide pages, seed catalog, engines, leaderboard).

## Private /start — before / after

### Text — BEFORE (old `handlers/start.py:73-86`)

```text
🍏 **سلام {first_name} عزیز! من سیب‌ام!**

من ربات بازی گروهی آنلاین تلگرام هستم.
من رو به گروه‌هات اضافه کن، تیم تشکیل بده و با گروه‌های دیگه در سراسر تلگرام مسابقه بده!

🎮 بازی‌های فعال:
• کارت بالا (High Card)
• سنگ، کاغذ، قیچی (Rock Paper Scissors)
• دوز (Tic-Tac-Toe)
• چهار در یک ردیف (Connect Four؛ ۱v۱، ۲v۲، ۴v۴)
• کشتی جنگی (Battleship؛ ۱v۱، ۲v۲، ۴v۴)

🏆 هر گروه تلگرام با تعداد برده‌هاش تو جدول برترین‌ها قرار می‌گیره!
برای شروع دکمه زیر رو لمس کن:
```

Problems: legacy mini-games led the message, generic "online group-game bot" self-description, English game titles, no اواریا/شیاسیب, no support handle.

### Text — AFTER (`handlers/start.py`, `_private_start_text`)

```text
🍏 سلام {first_name} عزیز!

سرزمین اواریا زیر یورش شیاسیب است. تو برای دفاع از آن فراخوانده شده‌ای.

با کارت‌هایت به نبردها بپیوند، در معدن سیب بکن، صندوق‌ها را بگشا، گل‌هایت را پرورش بده و با هم‌گروهی‌هایت متحد شو.

🎲 بازی‌های جانبی: کارت بالا، سنگ‌کاغذ‌قیچی، دوز، چهار در یک ردیف، کشتی جنگی.

برای شروع، بات را به گروهت اضافه کن.

🔧 پشتیبانی: @sibgame_official
```

352 chars with a short name (well under 1024). Persian only; mini-games demoted to one secondary line.

### Buttons — BEFORE → AFTER

| BEFORE (9 buttons, `get_private_welcome_keyboard`) | AFTER (1 button, new `get_private_start_keyboard`) |
|---|---|
| 🌳 باغ من (`apple_garden`) | ~~removed from /start~~ |
| 🎴 کارت‌های من (`apple_cards`) | ~~removed from /start~~ |
| 🛒 فروشگاه (`apple_shop`) | ~~removed from /start~~ |
| 📚 ویکی کاراکترها (`apple_wiki`) | ~~removed from /start~~ |
| 🔄 مبادله کارت (`apple_trade`) | ~~removed from /start~~ |
| 🎁 صندوقچه گنج روزانه (`apple_chest`) | ~~removed from /start~~ |
| 👤 پروفایل من (`apple_profile`) | ~~removed from /start~~ |
| ➕ افزودن سیب به گروه (URL `https://t.me/{BOT_USERNAME}?startgroup=true`) | **KEPT — the only button** |
| 🎮 بازی‌ها و راهنما (`help_menu`) | ~~removed from /start~~ |

**No handler was deleted.** All removed buttons remain fully reachable:

- `get_private_welcome_keyboard` is **unchanged** and remains the private **main menu** keyboard («سیب منو» / `main_menu`, `handlers/start.py` `_main_menu_text_and_kb`), carrying garden, cards, shop, wiki, trading, chest, profile, add-to-group and the guide.
- The group menu (`get_group_welcome_keyboard`, unchanged) keeps shop, profile, chest, guide and group stats.
- The profile panel keeps cards/trading/garden/shop shortcuts (`get_profile_keyboard`).

**Same-flow consistency:** the membership gate (`check_membership`) is only reachable from private `/start`, so its post-verification welcome (`handle_check_membership`) was switched to the SAME new text and the SAME one-button keyboard — every private `/start` path now ends on exactly one button. The membership gate keyboard itself (join-channel + verify, `get_membership_keyboard`) is required functionality and was not touched.

## Group /start — before / after

### Text — BEFORE (old `handlers/start.py:47-52`)

```text
🍏 **سیب وارد شد! سلام به برو بچِ {group_title}!**

گروه شما ثبت شد و از این پس با هر برد، یک **برد** به افتخارات گروه‌تون اضافه می‌شه.

برای ساخت پارتی بازی و مبارزه با گروه‌های دیگه، دکمه‌های زیر رو لمس کنید 👇
```

### Text — AFTER

```text
🍏 سیب وارد شد! سلام به {group_title}.

گروه شما به جمع مدافعان اواریا پیوست. از این پس با هر یورش شیاسیب، در کنار هم می‌جنگید و با هر پیروزی، افتخاری به نام گروه‌تان ثبت می‌شود.

برای شروع، دکمه‌های زیر را لمس کنید 👇

🔧 پشتیبانی: @sibgame_official
```

The support-handle line was added per the A3 rule "must appear in both messages". **Keyboard unchanged** (`get_group_welcome_keyboard()` — same six buttons/callbacks; asserted by test).

## Other implementation notes

- New keyboard: `keyboards/common.py` → `get_private_start_keyboard(bot_username)`; exported from `keyboards/__init__.py`.
- Shared welcome builder: `handlers/start.py` → `SUPPORT_HANDLE` + `_private_start_text(first_name)` (one source of truth for both private `/start` paths).
- Both new messages are sent with `parse_mode=None` (plain text): the new copy has no Markdown, and this prevents injected first names / group titles containing `*`, `_` etc. from breaking Markdown parsing (the bot default is Markdown).
- `tests/test_handlers.py` assertions that pinned the three old texts were updated alongside the rewrites so every commit stays green.

## Test results

New suite `tests/test_a3_start_texts.py` — **12 passed, 0 failed** (11 required + 1 supplementary):

| Test | Result |
|---|---|
| `test_private_start_shows_exactly_one_button` | PASSED |
| `test_private_start_button_is_url_add_to_group` | PASSED |
| `test_private_start_does_not_contain_removed_buttons` | PASSED |
| `test_private_start_mentions_ovarya` | PASSED |
| `test_private_start_contains_support_handle` | PASSED |
| `test_private_start_mentions_mini_games` | PASSED |
| `test_group_start_contains_group_title` | PASSED |
| `test_group_start_keeps_existing_keyboard` | PASSED |
| `test_group_start_contains_support_handle` | PASSED |
| `test_all_start_texts_persian_only` (masked injected values) | PASSED |
| `test_all_start_texts_under_1024_chars` (incl. ~120-char injected names) | PASSED |
| `test_membership_gate_welcome_matches_private_start` (supplementary) | PASSED |

Regression runs (SQLite test DB, `sib_TEST_DATABASE_URL`):

- Targeted start/menu/chest/navigation group (`test_handlers`, `test_main_menu_layout`, `test_main_menu_style`, `test_navigation`, `test_phase2_nav_garden`, `test_daily_chest`, `test_daily_chest_group`, `test_group_menu_games_guide`, `test_screen_transitions`, `test_trade_group_flow`, `groups/test_group_lifecycle`): **208 passed, 1 failed, 3 skipped** — the single failure (`test_navigation.py::test_shop_and_battle_and_garden_and_trade_have_back_to_main`) requires a live MariaDB on `127.0.0.1:3306` and fails identically on the pre-A3 base commit `24a148e`.
- **Full repository suite: 2458 passed, 7 failed, 25 skipped.** All 7 failures reproduce identically on the base commit `24a148e` (no MariaDB in this environment; character-art image assets; `test_phase8c.py::test_migration_chain_head`) — i.e., **zero new failures introduced by A3**.

## invasion_enabled

Unchanged. No invasion-v2 file, no `_archive_inv1_*` table, and no config/setting was modified by A3 (`git diff 24a148e..HEAD` touches only `handlers/start.py`, `keyboards/common.py`, `keyboards/__init__.py`, `tests/test_handlers.py`, `tests/test_a3_start_texts.py`, `docs/admin/A3-REPORT.md`).

## Limitations

- No live Telegram delivery test was performed; the handler was exercised with aiogram mocks (same pattern as the existing suite).
- The new texts are plain text (no Markdown bold) because the approved copy contains no formatting; if bold is wanted later, `_private_start_text` and the group builder are the single places to change.
- The private **main menu** still shows the full button set by design — A3 only simplifies the `/start` funnel. The membership gate keeps its two functional buttons (join channel + verify).
- The bot's `@username` in the deep link comes from `settings.BOT_USERNAME` (default `sib_game_bot`, env-overridable); it is not hard-coded.
- The pre-existing 7 environmental test failures (MariaDB/assets/migration-chain) are outside A3 scope and remain unfixed.

