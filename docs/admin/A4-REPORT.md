# Phase A4 — Text shortcut aliases for existing menus

Recon notes for the five new «سیب <target>» wake words. All line numbers are
from the tree at commit `25b1d87` (pre-A4) unless stated otherwise.

## 1. Where the existing text shortcuts live (the pattern to follow)

**File:** `handlers/start.py` · **Router:** `Router(name="start")` (`handlers/start.py:30`)
· **Registration:** `handlers/__init__.py:44-100` (`register_all_handlers` → `dp.include_router(start_router)`,
included LAST, after every feature router).

| Wake word | Pattern (anchored) | Handler | Line |
|---|---|---|---|
| «سیب منو» / «سیب بازی» | `r"^\s*سیب\s+(بازی\|منو)\s*$"` | `handle_sib_games_word` | `handlers/start.py:681` |
| «سیبام» | `r"^\s*سیبام\s*$"` | `handle_sib_balance_word` | `:697` |
| «سیب پروفایل» / «سیب من» | `r"^\s*سیب\s+(پروفایل\|من)\s*$"` | `handle_sib_profile_word` | `:841` |
| «سیب باغ» | `r"^\s*سیب\s+باغ\s*$"` | `handle_sib_garden_word` | `:879` |
| «باغ من» | `r"^\s*باغ\s+من\s*$"` | `handle_my_garden_word` | `:885` |
| «سیب» | `r"^\s*سیب\s*$"` | `handle_sib_wake_word` | `:891` |

**Exact registration mechanism:** a plain `@router.message(F.text.regexp(...))`
message handler whose regex is anchored with `^\s*…\s*$` (whole message = the
wake word). There is **no** shared text map today — the closest thing to one is
`handle_sib_games_word`, which folds two phrases into one regex and branches with
`if text_lower.endswith("منو")`. Each handler delegates to a shared opener
(`_reply_main_menu`, `_open_sender_profile`, `_open_sender_garden`), so buttons
and wake words can never drift apart.

**Chat coverage (Task 1 question):** every wake word above works in **both**
private chats and groups/supergroups — none of them gates on `chat.type`. The
only exception in the family is the bare «سیب» claim word (`handle_sib_wake_word`,
`:891`), which returns early outside groups. Covered by
`tests/test_phase2_nav_garden.py::test_private_sib_menu_opens_main_menu` /
`::test_group_sib_menu_opens_main_menu`.

Other text triggers registered outside `handlers/start.py` (same anchored style):

| Trigger | Pattern | Handler |
|---|---|---|
| «معدن سیبی» | `MINE_COMMAND_PATTERN` — `handlers/apple_mine.py:41` | `handlers/apple_mine.py:112` |
| «سیب صندوق» | `GROUP_SHORTCUT_PATTERN` — `handlers/daily_chest.py:127` | `handlers/daily_chest.py:490` |
| «ازدواج سیبی» / «با من ازدواج می‌کنی؟» | `PROPOSAL_TRIGGER_PATTERN` — `handlers/marriage.py:130` | `handlers/marriage.py:1173` |
| «طلاق سیبی» | `DIVORCE_TRIGGER_PATTERN` — `:141` | `handlers/marriage.py:951` |
| «گل سیبی» | `FLOWER_TRIGGER_PATTERN` — `:149` | `handlers/marriage.py:1475` |
| «بغل سیبی» | `HUG_TRIGGER_PATTERN` — `handlers/sib_hug.py:37` | `handlers/sib_hug.py:56` |
| «بوس سیبی» | `KISS_TRIGGER_PATTERN` — `handlers/sib_kiss.py:36` | `handlers/sib_kiss.py:55` |

Those newer patterns (chest, marriage, hug, kiss) accept typed Persian variants
via `_YEH = r"[\u06cc\u064a]"` / `_KEH = r"[\u06a9\u0643]"`; A4 follows that
convention (see the implementation notes).

There is **no** catch-all text handler: the only unanchored text filters in the
bot are `handlers/admin_users.py:212` and `handlers/admin_broadcast.py:110`, both
gated on an in-memory admin-flow state, and no middleware normalizes or rewrites
message text. No `dp.channel_post` handlers exist at all.

## 2. The target each new alias must open

| Shortcut | Target | Exact entry point |
|---|---|---|
| «سیب شاپ» | the `/shop` entry | `handlers/shop.py:110-111` — `@router.message(Command("shop"))`, `cmd_shop(message, bot)` → `_shop_view(user.id)` (`:87`) + `send_screen_reply(message, "shop", …)` + panel claim `panel_type="shop"` |
| «سیب کارت» | «کارت‌های من» panel | `handlers/start.py:339` — `handle_apple_cards` (`F.data.startswith("apple_cards")`); opened from the profile by the button `keyboards/common.py:328` (`callback_data="apple_cards:apple_profile"`), and from the private main menu by `apple_cards` (`keyboards/common.py:52`) |
| «سیب اسکین» | Change Skin panel | `handlers/start.py:534` — `handle_profile_skin` (`F.data == "profile_skin"`), button `keyboards/common.py:340` («🎨 تغییر پوسته»). **Note:** the literal text «تعویض اسکین» does not exist anywhere in the codebase (`grep` = 0 hits); the profile's skin entry is labelled «🎨 تغییر پوسته» / `Change Skin`, so «سیب اسکین» maps to that screen |
| «سیب لیدربرد» | leaderboard | `handlers/leaderboard.py:40-41` — `@router.message(Command("leaderboard", "top"))`, `cmd_leaderboard(message)`; same screen as the `show_leaderboard` callback (`:54`) |
| «سیب راهنما» | Guide index | `handlers/start.py:223` — `handle_help_menu` (`F.data.startswith("help_menu")`); buttons «🎮 بازی‌ها و راهنما» `keyboards/common.py:93-94` (group menu) and «📖 راهنما» `:153-154` (private menu) |

`user_id` requirements: the cards and skin screens are built from the **sender's**
user id (profile panels); the shop and leaderboard entries already take the
sender from the message; the guide index is public.

## 3. Collision check (exact match, not substring)

The five new strings were matched against every exact-match trigger listed in
§1: **no existing pattern matches any of them**, and none of the new strings is a
prefix/extension of an existing trigger (`شاپ`, `کارت`, `اسکین`, `لیدربرد`,
`راهنما` are not used by any current wake word). The check is mechanised in
`tests/shortcuts/test_text_shortcuts.py::test_shortcut_no_collision_with_existing`,
which imports the other modules' pattern constants instead of copying them.

Slash commands are unaffected by construction: the new pattern starts with
`^\s*سی[یي]ب`, so `/shop`, `/leaderboard`, `/help`, `/start` and even
`/سیب شاپ` can never reach it.

## 4. Delivery constraints worth knowing (pre-existing, documented in the report)

* The bot only receives ordinary group text when BotFather *Group Privacy* is
  disabled (or the message replies to the bot / mentions it). This is the same
  precondition the existing wake words («سیب منو»، «سیب باغ»، «سیبام») already
  rely on — A4 adds no new constraint.
* Channels get `channel_post` updates, which have **no** handlers in this bot at
  all → shortcuts do not fire in channels.
* While an automated flow holds a per-user state (admin broadcast/user
  management, `handlers/admin_users.py:212`, `handlers/admin_broadcast.py:110`,
  `handlers/admin_center.py:140/194/503`), those routers (registered before
  `start_router`) consume the text as flow input — again identical for all
  existing wake words.
