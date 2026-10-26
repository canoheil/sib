# sib (سیب) — Telegram Group Game Bot

A Persian-language, apple-themed multiplayer game bot for Telegram groups, set in the fantasy world of Ovarya (اواریا).

![Python](https://img.shields.io/badge/Python-3.12-blue)
![aiogram](https://img.shields.io/badge/aiogram-%3E%3D3.31%2C%20%3C4.0-blue)
![MariaDB](https://img.shields.io/badge/MariaDB-10.11-blue)
![Tests](https://img.shields.io/badge/tests-1932-brightgreen)

## About

«sib» (سیب, "apple") is a long-running game played inside Telegram
groups. Players collect character cards, grow a garden, work shifts in
the Apple Mine, trade with each other, marry, and — when the shadow
falls — defend the regions of Ovarya (اواریا) together against
«شیاسیب». The bot's interface, narrative text, and error messages are
in Persian; this README is in English for maintainers and reviewers.

What makes the project unusual is the combination of a persistent
group-level game (cards, wallets, gardens, and invasions all survive
restarts and are stored in MariaDB) with group-vs-enemy battles in
which every defender in a chat contributes through their own main
card. Invasion v2 is guarded by the `invasion_enabled` flag in the
`system_config` table, which defaults to `false` (migration
`036_invasion_v2`); scheduled invasions only run when an operator
turns that flag on.

The codebase is a single aiogram 3 application with a strict split
between Telegram handlers (`handlers/`), domain services
(`services/`), game engines (`games/`), and SQLAlchemy models and
Alembic migrations (`database/`).

## Features

### Invasion v2 (group battles vs «شیاسیب»)

- Scheduled, per-group battles against «شیاسیب» in the regions of
  Ovarya, with five enemy types of increasing strength (see
  `services/invasion_v2/enemy.py`).
- Each defender joins once with their main card; group power is the
  average main-card power resolved from observed group members
  (`services/invasion_v2/power_resolver_impl.py`).
- The resolver fails closed: if an active member's main card is
  unknown, no invasion spawns for that group instead of guessing.
- Composed battle images (announcement, join, victory, defeat) are
  rendered locally with Pillow under `services/invasion_v2/imaging/`.
- Admin controls, a dashboard, and audited private commands live in
  `handlers/inv2_controls.py`, `handlers/inv2_dashboard.py`, and
  `handlers/inv2_admin.py`. Sample output:
  `docs/invasion-rework/samples/phase-8a/victory.jpg`.

### Apple Mine

- Group members take timed mining shifts (`/mine`, or the exact text
  «معدن سیبی») and earn normal, golden, diamond, and obsidian apples.
- Outcomes and narrative lines are data-driven
  (`services/apple_mine.py`, `services/apple_mine_narrative.py`).
- Shift state is persisted (migration `043_apple_mine_shift`), so a
  restart does not lose an in-progress shift.

### Come-back loop (streak and reminders)

- The **daily chest streak** is derived from `daily_chest_claims`, so it
  needs no new table: consecutive UTC days up to today (or up to
  yesterday while today is still unclaimed). Milestones pay extra
  Apples through the wallet ledger, exactly once per day
  (`daily_streak:<user>:<day>`), inside the chest transaction
  (`services/daily_streak.py`). The ladder is one config value,
  `daily_streak_rewards` (`{"3": 30, "7": 100, "14": 250}` by default);
  the chest screen and the chest result both name the live streak.
- **Reminders** are the only messages sent without being asked
  (`services/reminders.py`): at most one short DM per player per day,
  only inside the local evening window (Asia/Tehran,
  `reminders_window_start_hour`..`reminders_window_end_hour`), and only
  when there is something real to say — today's chest is still closed
  after `reminders_chest_after_hour`, or a plot has fruit ready. The
  sweep runs from the maintenance cycle, throttled to one candidate scan
  per 10 minutes.
- Safety rails, all enforced in code: `reminders_enabled` (built-in
  default **off**, seeded **on** by migration `063_reminders`),
  `reminders_min_gap_hours` (20), `reminders_batch_size` (25 per sweep),
  banned/bot/inactive accounts filtered in SQL, `TelegramForbiddenError`
  (player blocked the bot) recorded in `user_reminder_states.blocked_at`
  and not retried until the player switches reminders back on (pressing
  the switch proves the bot can reach them again, so the stale block is
  cleared), and any other delivery failure is *not* recorded as sent so
  the player keeps their turn.
- The player can switch reminders off from the profile
  («🔔/🔕 یادآوری‌ها», `handlers/reminders.py`); the label always shows
  the current state and the toggle answers with an alert.

### Weekly group league (the reason to play *this* week)

- The all-time leaderboard ranks by total wins, so a group that arrives
  late can never catch up and stops trying. `/league` («🏆 لیگ هفته»)
  resets every **Saturday 00:00 Asia/Tehran** and ranks groups by the
  week's points: **win = 3, draw = 1, loss = 0**
  (`services/group_league.py`).
- Nothing on the hot path changed: the score is **derived** from
  `matches` (normal, non-bot, finished, inside the week), exactly like
  the streak is derived from the chest ledger. Bot matches and friendly
  matches never count, so the table cannot be farmed.
- Tie-breaks are deterministic — points, wins, draws, then group id —
  and never rely on `GROUP BY` ordering.
- **Multi-source scoring (Phase 1):** a group is scored for the week it
  actually had. Group matches: win 3 / draw 1, cap 45. Group wars: win 10,
  presence 3, plus 1 per 100 damage dealt, cap 30. Apple mine inside a
  group: 1 per 25 normal apples, cap 10. Apple duels inside a group: 1 per
  distinct member, cap 5 (a duel is always inside ONE group, so it can only
  be activity, never a competitive result). Theoretical maximum 90, and the
  panel prints the breakdown: `🎮 بازی ۱۸ + ⚔️ جنگ ۱۹ + ⛏ معدن ۲ + 🥊 دوئل ۴`.
  The whole table is one config row, `league_points`.
- Payout: every player who **actually played** for a top-3 group that
  week is paid (not every member — presence is not participation), with
  one auditable wallet transaction per player keyed
  `league:<week>:<group>:<user>`. The ladder is one config value,
  `league_rewards` (`{"1": 300, "2": 150, "3": 100}` by default), and a
  group must reach `league_min_points` (15) to be paid at all, so a quiet
  week has no champion instead of a 0-point winner.
- `group_league_weeks` (PK `week_key`) is the atomic "this week is paid"
  claim and `group_league_awards` is the frozen podium; both are created
  by migration `064_group_league`. `league_first_week` is seeded with
  the migration's own week, so the first deploy never pays a retroactive
  prize for a week that ended before the league existed.
- The maintenance cycle finalizes the week that just ended, once an hour
  behind its own `try/except`, and announces the result in the podium's
  groups (best effort — being removed from a group is not an error).
- **Champion card:** rank 1 also hands the trophy card
  `league_champion_card_code` (`hero` — «هیرو», drawn with his white wolf
  «سپیدچنگ») to every player who actually played for the winning group.
  The card is `shop_enabled: false`, so the weekly shop, the daily chest
  and the starter pack can never grant it: the league is its only source.
  Winning again never duplicates it (one card per character per player);
  the repeat prize stays the Apples. A missing or disabled card never
  blocks the Apple payout.
- Kill switch: `league_enabled` (built-in default **off**, seeded **on**
  by the migration).
- **Daily channel digest (Phase 3):** one short post at **20:00 Asia/Tehran**
  at most, and only on days with real group activity. It shows daily match/war/
  mine/duel activity and the live top three; quiet days are recorded as
  skipped instead of receiving filler. It never posts one message per game.
  The default destination is `OFFICIAL_CHANNEL_ID` (`@sib_official`), and the
  bot must be an admin with permission to post there. Migration
  `066_daily_channel_digest` enables the feature and creates
  `daily_channel_posts`, the durable per-day idempotency/delivery log. The
  admin menu's **پست روزانهٔ کانال** screen provides a kill switch, preview,
  and manual retry after a known send failure; `/daily_digest` opens the same
  screen. `daily_digest_hour` and `daily_digest_channel_id` can be overridden
  in `system_config` (blank channel uses `OFFICIAL_CHANNEL_ID`).

### First session (starter pack and the private home screen)

- A brand-new account is never created empty. `/start` pays a
  **starter pack** exactly once per account: one released shop card
  (level 1), immediately selected as the player's Main Card, plus
  `STARTER_APPLES` (100) through the auditable wallet ledger
  (`services/onboarding.py`). The ledger row with the fixed key
  `starter_pack:<user id>` *is* the "already granted" marker, so the
  pack needs no new column, no migration and cannot be farmed by
  re-entering a group; a player who already owns the starter character
  gets the Apples only. The pack is paid for group-first `/start`s
  too, so every player ends up with the Main Card that invasion power
  and duels read.
- The private `/start` keyboard is the real first-session menu
  (chest, garden, cards, shop, profile, help) with «➕ افزودن سیب به
  گروه» as the **last** row. It replaces the Phase A3 single-button
  funnel, which left any player who does not administer a group with
  nothing to press.
- That keyboard is the ONLY private menu (`get_private_start_keyboard`).
  Every «🏠 منوی اصلی» button in a private screen (guide, shop,
  garden, cards, wiki, chest) renders it, so «back» lands on the same
  screen `/start` shows. The retired second menu
  (`get_private_welcome_keyboard`, which listed the wiki and card
  trading at the top level and labelled its guide entry «🎮 بازی‌ها و
  راهنما») is deleted; the wiki lives in «🎴 کارت‌های من» and card
  trading in «👤 پروفایل من». Navigation never calls the
  membership API to decide what the menu looks like — the channel row
  belongs to the `/start` join flow only.
- The official-channel membership check is a **soft** gate: the menu
  and the starter pack are delivered first, and a missing membership
  only adds a «📢 عضویت در کانال سیب» row plus a line of copy.
  Verifying membership removes the row; it never unlocks the game.
- The player command menu is published from code at startup
  (`services/bot_commands.py`, `set_my_commands`, 13 commands), so the
  «/» button is no longer empty. Admin-only commands stay out of it.
- Design rationale and the remaining growth work:
  `docs/growth-diagnosis-fa.md`.

### Cards

- Collectible character cards (`/cards`, «سیب کارت») seeded from
  `data/characters_seed.json`, with a chosen main card used in
  battles and invasions.
- Power is derived, not stored per card: each card has a level from
  1 to 10, and its 💪 power, ⚡ speed, and 🧠 intelligence are the
  character base plus +10 per attribute per level above 1
  (`services/card_progression.py`).
- Upgrades cost normal apples plus one special apple — golden for
  low levels, diamond for mid levels, obsidian for high levels —
  charged atomically with row locks (`services/card_upgrade.py`,
  migration `022_card_upgrade_levels`).
- Characters belong to data-driven factions seeded from
  `data/factions_seed.json` (`services/factions.py`, migration
  `034_card_affiliations`); adding a faction needs no code change.
- Profile skins change a card/profile's appearance only, never its
  stats (`services/profile_skin.py`, migration `029_profile_skins`).
- **Card art is delivered, not generated:** the image at
  `assets/characters/{code}.jpg` *is* the card — the artist bakes the
  frame, the name plate and the crown into the picture.
  `services/character_art.py` resolves that path and hands the file to
  Telegram; nothing crops, resizes, re-encodes or frames it at runtime.
  The old golden-frame pipeline (`scripts/frame_card.py`) is retired and
  a test fails if any runtime code starts using it again. The house
  format is a 4:5 portrait (912×1136 … 1850×2304) under Telegram's 10 MB
  photo limit.

### Chests and shop

- `/shop` sells cards and shop chests; chest contents (apples,
  items, cards) are defined in `data/shop_chests/` and
  `services/shop_chests.py` (migration `032_shop_chests`).
- The daily chest (`/chest`, «سیب صندوق») grants one random reward
  per day — normal/golden/diamond/obsidian apples, seedlings, or
  other items (`services/daily_chest.py`, migration
  `030_daily_chest`).

### Daily apple claim

- Typing exactly «سیب» in a group claims apples on a per-user
  cooldown (default amount 5, default cooldown 300 seconds, both
  configurable in `services/config_store.py`).
- The award is written to the wallet ledger with an idempotency key,
  and concurrent claims are serialised by a per-user row lock
  (`services/sib_claim.py`).

### Garden

- `/garden` (or «سیب باغ» / «باغ من») opens the sender's garden:
  plots, seedlings, planting, and harvests that can drop special
  apples (`services/garden.py`, `services/seedling.py`, migrations
  `010_seedlings`, `011_garden_wallet`, `019_magic_trees`).

### Marriage

- In-group proposals («ازدواج سیبی», or replying «با من ازدواج
  می‌کنی»), divorce («طلاق سیبی»), and flower gifts («گل سیبی»)
  handled by `handlers/marriage.py` and `services/marriage.py`
  (migration `024_marriage_and_love`).
- Marriage and shared love are shown on the profile.
- Two lightweight social reply actions also exist: «بغل سیبی» (hug)
  and «بوس سیبی» (kiss), in `handlers/sib_hug.py` and
  `handlers/sib_kiss.py`. Both participants receive `sib_hug_amount`
  / `sib_kiss_amount` (10) apples; when the two players are **accepted
  apple friends**, they get the Friendship Bonus instead and each
  receives base + `sib_hug_friend_bonus` / `sib_kiss_friend_bonus`
  (10 + 10 = 20). The bonus never touches the cooldown or the
  idempotency keys, so it cannot be farmed.

### Party and matchmaking

- Players form parties (solo, 2v2, 4v4) from the games menu
  (`/games`), with one active session per user enforced server-side
  and `with_for_update()` row locks on the last free seat
  (`services/party/service.py`, `services/session.py`).
- Cross-group matchmaking never pairs two parties from the same
  Telegram group; bot opponents fill in automatically when a queue
  deadline passes (`matchmaking/`, `services/bot_match.py`).
- Friendly matches pit members of the same group against each other
  and pay seedlings instead of affecting competitive results
  (`handlers/friendly.py`, `services/friendly.py`).

### Trading

- `/trade` exchanges cards between two players; both cards must be
  the same level, and each user can trade at most once per day
  (`services/trading.py`, migration `015_trades`).

### XP and level progression

- Account XP and levels are tracked separately from card levels
  (`services/player_xp.py`, migration `025_player_xp_progression`)
  and shown on the profile with level artwork from
  `assets/player_levels/`.

### Apple wallets

- One normal-apple wallet per user with a full transaction ledger:
  every change is row-locked, auditable, optionally idempotent, and
  can never drive the balance below zero
  (`services/apple_wallet.py`).
- Special apples are inventory items, not separate wallets:
  `apple_gold` (🌕 golden), `apple_diamond` (💎 diamond), and
  `apple_obsidian` (🖤 obsidian), alongside seedlings and magic
  powder (`services/items.py`).

### Mini-games and duels

Only the games still registered in `games/registry.py` and
`handlers/__init__.py` are listed:

- ⚔️ Apple Battle (`/battle`): your main card fights another
  player's card through matchmaking; the winner earns seedlings.
- «دوئل سیبی» (Apple Duel): reply to a player's message with this
  exact phrase to challenge them; the invited player accepts,
  rejects, or swaps card (`handlers/apple_duel.py`).
- Five classic party games, each an independent engine under
  `games/`: High Card, Rock-Paper-Scissors, Tic-Tac-Toe, Connect
  Four (solo/2v2/4v4), and Battleship (solo/2v2/4v4).

### Admin panel

- `/admin` opens the admin centre (`handlers/admin_center.py`,
  `handlers/admin_meta.py`): user lookup and management (adjust
  apples, special apples, seedlings, XP/level; ban/unban via
  migration `044_user_is_banned`), sponsor/official channel
  management, and system statistics.
- `/broadcast` sends a message to users/groups
  (`handlers/admin_broadcast.py`, `services/broadcast.py`); `/users`
  is the user-admin entry point (`handlers/admin_users.py`).
- `/setasset` (admins only) replaces named artwork assets at runtime
  (`handlers/assets.py`, `services/assets.py`).
- Invasion v2 is operated only from private admin chat:
  `/inv2_status`, `/inv2_toggle`, `/inv2_force_spawn`,
  `/inv2_force_end`, `/inv2_regions`, `/inv2_reset_region`,
  `/inv2_mute`, `/inv2_unmute`, `/inv2_group_info`, `/inv2_invasions`,
  `/inv2_config`, `/inv2_set`, `/inv2_health`, plus `/inv2_dashboard`,
  `/inv2_preview`, and `/inv2_simulate`. Every command writes an
  admin audit log row.
- Admin identity comes only from `TELEGRAM_ADMIN_IDS` in the
  environment; no admin ID is hard-coded (`admin/auth.py`).

### Text shortcuts

Exact whole-message phrases (surrounding whitespace ignored;
Persian/Arabic ی and ک variants accepted). A sentence that merely
contains one of these phrases does not trigger it:

- «سیب» (groups only) — daily apple claim
- «سیب منو» — main menu; «سیب بازی» — games menu
- «سیبام» — your apple balance
- «سیب پروفایل» / «سیب من» — your profile
- «سیب باغ» / «باغ من» — your garden
- «سیب کارت» — your cards; «سیب اسکین» — profile skin picker
- «سیب شاپ» — shop; «سیب لیدربرد» / «سیب لیدربورد» — leaderboard
- «سیب لیگ» / «لیگ سیبی» — weekly group league
- «سیب راهنما» — help index; «سیب صندوق» — daily chest
- «معدن سیبی» — Apple Mine; «دوئل سیبی» (as a reply) — Apple Duel
- «ازدواج سیبی», «طلاق سیبی», «گل سیبی», «بغل سیبی», «بوس سیبی» —
  marriage and social actions (groups only)

### Structured help

- The in-bot guide is a tree of sections (quick start, the world of
  Ovarya, cards, invasion, mine, apples and chests, garden/
  marriage/party/trading, side games, shortcuts, support) defined
  once in `services/help_content.py` and rendered by the «راهنما»
  button and the «سیب راهنما» shortcut.

### Groups

- When the bot is added to a group it registers the group, and when
  it is removed it deactivates it (`handlers/group_lifecycle.py`,
  `router.my_chat_member`).
- Group membership in game groups is observed from activity and
  used by the Invasion v2 power resolver (migration
  `037_inv2_group_members`); `scripts/backfill_inv2_group_members.py`
  backfills historical membership.

## Architecture

### Tech stack

| Layer | Technology |
| --- | --- |
| Language | Python 3.12 (Docker image `python:3.12-slim`) |
| Bot framework | aiogram >=3.31, <4.0 (long polling; ephemeral messages) |
| Database | MariaDB 10.11 (`docker-compose.yml`), `utf8mb4` |
| ORM / drivers | SQLAlchemy 2.0 async (`asyncmy`), `pymysql` for Alembic |
| Migrations | Alembic, 46 revisions (`database/migrations/versions/`) |
| Config | pydantic-settings, `.env` at the repo root |
| Images | Pillow, arabic-reshaper, python-bidi, numpy |
| Tests | pytest, pytest-asyncio (auto mode, `pytest.ini`) |
| Deployment | Docker Compose, systemd unit (`deploy/sib.service`), or `bot.py` directly |

### Project tree (top two levels)

```text
.
├── bot.py                  # entry point: polling, middleware, startup recovery
├── core/                   # config.py, logging.py, errors.py, text.py
├── handlers/               # aiogram routers (see handlers/__init__.py)
├── services/               # domain services (invasion_v2/, party/, ad/, ...)
├── games/                  # game engines: base.py, registry.py, five classic games, apple_battle/
├── database/               # connection.py, models/, migrations/ (Alembic)
├── matchmaking/            # matcher.py, acceptance.py
├── keyboards/              # inline keyboard builders
├── middleware/             # ban, rate limit, panel guard, group membership
├── admin/                  # auth.py — admin filter/decorators
├── assets/                 # artwork (characters, invasion_v2, items, ...)
├── data/                   # characters_seed.json, factions_seed.json, shop_chests/
├── scripts/                # backfill and invasion_v2 imaging benchmark
├── deploy/                 # deploy.sh, backup_db.sh, check_health.sh, sib.service
├── docs/                   # phase reports and design documents
├── tests/                  # pytest suite (see Testing)
├── sib/bots/               # in-game bot opponents (registry, runner, strategies)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── alembic.ini
└── pytest.ini
```

Note: modules import via the `sib.` package prefix (for example
`sib.handlers`), so the checkout directory itself is the `sib`
package and its parent must be on `PYTHONPATH` — the deploy scripts
do this (`deploy/deploy.sh` exports `$(pwd)/..`).

### Design principles

- **Domain vs infrastructure:** handlers only translate Telegram
  updates; business rules live in `services/`; game rules live in
  engine classes under `games/` that know nothing about Telegram.
- **Idempotency:** wallet changes, the daily claim, card upgrades,
  match results, and invasion contributions carry idempotency keys
  or applied-flags so retries and double taps cannot double-pay.
- **Fail-closed:** unknown group power blocks an invasion spawn;
  a failed `invasion_enabled` read is treated as disabled
  (`services/maintenance/service.py`); admin commands reject
  non-admins and group chats.
- **Feature flags in the database:** runtime switches such as
  `invasion_enabled` live in `system_config` and are changeable
  from admin commands without a redeploy (`services/config_store.py`).
- **Reversible migrations:** schema changes ship as Alembic
  revisions with downgrades; destructive history is archived, not
  dropped (migration `039_archive_inv1_tables` renames the v1
  invasion tables to `_archive_inv1_*`).

## Testing

```bash
python -m pytest            # full suite
python -m pytest tests/invasion_v2 tests/shortcuts   # one area
```

The suite currently contains **1,932 test functions in 165
`test_*.py` files** (counted with `grep -rE "^\s*(async )?def test_"
tests/ | wc -l`). Tests use in-memory SQLite where MariaDB is not
required. Structure:

- `tests/` (root) — the bulk of the suite: games, cards, shop,
  garden, marriage, trading, admin, assets, and migrations.
- `tests/invasion_v2/` — Invasion v2 rules, imaging, scheduler,
  recovery, admin, and end-to-end tests (201 tests).
- `tests/apple_mine/`, `tests/apple_duel/`, `tests/admin/`,
  `tests/groups/`, `tests/help/`, `tests/shortcuts/` — focused
  suites for those features.

## Installation

### Prerequisites

- Python 3.12 (the version used by the `Dockerfile`).
- MariaDB 10.11 (the version pinned in `docker-compose.yml`), or
  Docker with Compose.
- A Telegram bot token from BotFather, its username, and the
  numeric Telegram IDs of the bot's admins.

### Setup

```bash
git clone <this-private-repo> sib
cd sib
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env (see below)
```

Create the database (or use `docker compose up -d mariadb`):

```sql
CREATE DATABASE IF NOT EXISTS sib CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'sib'@'localhost' IDENTIFIED BY 'sib_password';
GRANT ALL PRIVILEGES ON sib.* TO 'sib'@'localhost';
FLUSH PRIVILEGES;
```

Apply migrations and run:

```bash
alembic upgrade head
PYTHONPATH=.. python bot.py   # imports use the `sib.` package prefix
```

Or, with Docker (migrations run automatically before polling):

```bash
docker compose up -d --build
```

The environment file is `.env` at the repo root (see
`.env.example`). Required keys: `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_BOT_USERNAME`, `TELEGRAM_ADMIN_IDS`, `DATABASE_URL`
(async, `mysql+asyncmy://...`), and `DATABASE_SYNC_URL` (sync,
`mysql+pymysql://...`, used by Alembic). Optional keys with defaults
in `core/config.py` include `OFFICIAL_CHANNEL_ID`,
`OFFICIAL_CHANNEL_LINK`, `MEMBERSHIP_CHECK_ENABLED`, matchmaking and
game timeouts, rate-limit settings, and `LOG_LEVEL`.

## BotFather setup

- **Group Privacy → OFF**: the bot must see ordinary group messages,
  because exact text shortcuts («سیب», «سیب کارت», ...) are plain
  messages, not commands.
- **Allow Groups → ON**: the game is played in groups.
- **Commands menu**: the public list is registered at startup by
  `services/bot_commands.py`. Admin-only commands (`/admin`,
  `/daily_digest`, `/users`, `/broadcast`, `/setasset`, `/inv2_*`) are
  deliberately absent from the public menu. If Telegram does not accept the
  startup update, the player commands are `/start`, `/games`, `/battle`,
  `/profile`, `/cards`, `/garden`, `/shop`, `/trade`, `/wiki`, `/mine`,
  `/chest`, `/leaderboard` (`/top`), `/league`, `/stats`, and `/help`.

## Performance

Invasion v2 image composition is the only benchmarked component.
`docs/invasion-rework/PHASE-8A-REPORT.md` reports 20 warm
compositions per output (Python 3.11.2, Pillow 12.3.0, Linux x86_64):
announcement 10.85 ms mean, join-normal 104.20 ms, join-crit
117.02 ms, victory 24.77 ms, defeat 21.87 ms, all under the report's
300 ms target. Reproduce with
`python scripts/invasion_v2/benchmark_imaging.py`. Nothing else is
benchmarked in this repository.

## Roadmap

Done:

- [x] Foundation: MariaDB schema, Alembic migrations, admin auth,
  official-channel membership gate.
- [x] Parties, cross-group matchmaking, and the five classic games.
- [x] Cards with levels/factions/skins, shop and chests, garden,
  trading, marriage, XP progression.
- [x] Invasion v2 (46 migrations; v1 tables archived, not dropped),
  Apple Mine, Apple Duel, daily chest, text shortcuts, structured
  help, admin centre with broadcast and ban.

Open (from `docs/`, not invented):

- [ ] Invasion v2 live rollout: per `docs/invasion-rework/
  PHASE-7-ROLLOUT.md`, enable `invasion_enabled` for one approved
  test group for 24 hours before wider rollout; the flag ships off.
- [ ] Shop-chest product decisions listed in `docs/shop-chests.md`:
  repeat-purchase limits, card-pool source, and re-buying a
  traded-away card (currently unlimited purchases, full-price re-buy).

There are no `TODO`/`FIXME` markers in the Python source; longer
design history is in `TODO/DECISIONS.md`.

## Documentation index

- `docs/admin/` — A1–A5 reports: admin centre, user management,
  broadcast, and related admin phases.
- `docs/apple-duel/` and `docs/apple-duel.md` — Apple Duel design
  and the D1 report.
- `docs/apple-mine/` — Apple Mine M1 report.
- `docs/invasion-rework/` — Invasion v2 design, narrative, phase
  reports (0–8H), rollout plan, Persian text audit, archived v1
  balance material (`archive/`), and composed sample images
  (`samples/`).
- `docs/card-lore.md` — card character lore.
- `docs/invasion-four-wave-battles.md` — the earlier four-wave
  invasion design (superseded by Invasion v2).
- `docs/invasion-telegram.md` — invasion Telegram presentation notes.
- `docs/lock-order.md` — database row-lock ordering rules.
- `docs/shop-chests.md` — shop chest design, operations, known limits.
- `docs/growth-diagnosis-fa.md` — honest growth diagnosis of the
  first-session funnel (in Persian), what the starter-pack patch
  changed, and the remaining retention/distribution work.
- `RUNBOOK.md` (repo root) — production operations and deployment.

## Support

Support handle: **@sibgame_official** (the `SUPPORT_HANDLE` constant
in `services/help_content.py`, also shown by `/start`). The official
channel configured by default is `@sib_official`
(`OFFICIAL_CHANNEL_ID` in `core/config.py`).

## License

This project is proprietary and confidential.  
All rights reserved.
