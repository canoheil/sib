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

## 4. Delivery constraints worth knowing (pre-existing)

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

---

# Implementation (Task 2 + Task 3)

## 5. How the routing is structured

**One handler, one anchored pattern, one target map** — the existing wake-word
mechanism of `handlers/start.py`, extended, not replaced. **No new router** was
created and `handlers/__init__.py` was not touched: the new handler lives on the
same `Router(name="start")` that owns «سیب منو» / «سیب باغ».

| Piece | Location | Purpose |
|---|---|---|
| `_YEH`, `_KEH` | `handlers/start.py:935-936` | typed-Persian tolerance, same convention as `handlers/daily_chest.py:126` / `handlers/marriage.py:122-124` |
| `SIB_SHORTCUT_TARGETS` | `handlers/start.py:940-947` | the accepted spellings → canonical target (the "text map") |
| `SIB_SHORTCUT_PATTERN` | `handlers/start.py:953-963` | ONE anchored regex: `^\s*سی[یي]ب\s+(شاپ\|کارت\|اسکین\|لیدربرد\|لیدربورد\|راهنما)\s*$` |
| `sib_shortcut_target()` | `handlers/start.py:970-983` | whole-message lookup, folds Arabic Yeh/Kaf + ZWNJ/bidi marks; returns `None` for anything that is not exactly «سیب» + one known target |
| `_SIB_SHORTCUT_OPENERS` | `handlers/start.py:1067-1073` | target → opener function |
| `@router.message(F.text.regexp(SIB_SHORTCUT_PATTERN))` → `handle_sib_shortcut_word` | `handlers/start.py:1076-1092` | the single message handler |

Why this shape: the task asked for the existing text-map pattern if one existed —
`handlers/start.py` only had per-phrase handlers with inline regexes, so the
five aliases were added as **one** family (one pattern, one map, one dispatch)
instead of five near-identical handlers. Each alias still registers nothing new
in the dispatcher: the router already is the one that owns the wake words.

## 6. The five aliases and the exact target each opens

| Shortcut | Opener (`handlers/start.py`) | Reused code path |
|---|---|---|
| «سیب شاپ» | `_open_shop_entry` `:1050-1054` | `handlers/shop.py:110` `cmd_shop` — i.e. `/shop` verbatim (banner + `panel_type="shop"` claim) |
| «سیب کارت» | `_open_sender_cards` `:986-1010` | shared builder `_cards_screen` `:351-396` (used by `handle_apple_cards` `:399`), replied as a screen + `panel_type="cards"` claim |
| «سیب اسکین» | `_open_sender_skin` `:1013-1035` | shared builder `_skin_screen` `:560-583` (used by `handle_profile_skin` `:585`), replied as a screen + `panel_type="profile"` claim (same lock as the profile entry) |
| «سیب لیدربرد» | `_open_leaderboard_entry` `:1057-1061` | `handlers/leaderboard.py:40-41` `cmd_leaderboard` — i.e. `/leaderboard` verbatim |
| «سیب راهنما» | `_open_help_menu` `:1038-1047` | shared builder `_help_menu_screen` `:223-236` (used by `handle_help_menu` `:239`); the guide is public, so no owner claim — exactly like its button |

**Reuse instead of duplication (the one structural change).** Three screens were
previously rendered inline inside their callback handlers. They are now built by
`_cards_screen`, `_skin_screen` and `_help_menu_screen`, and **both** the callback
and the alias call the builder — so the alias cannot drift from the button.
Behaviour is unchanged for the buttons: the same text, the same keyboard, the
same panel claim, the same account-repair call (`_skin_screen(..., telegram_user=user)`).
No service, model, migration or business rule was touched; no handler was removed.

Test evidence for the equivalence: `test_shortcut_sib_kaart_opens_my_cards` and
`test_shortcut_sib_skin_opens_skin_swap` render the screen through the callback
handler **and** through the alias for the same user and assert identical text and
identical card/skin buttons.

## 7. Matching semantics

* Whole message only (`^\s*…\s*$` + `sib_shortcut_target()` requires exactly two
  words): `سلام سیب شاپ`, `سیب شاپ کن`, `سیب شاپ:` and `سیبکارت` do **not** trigger.
* Slash commands never reach the handler (the pattern starts with `سیب`);
  `handle_sib_shortcut_word` additionally returns `None` for any `/…` text.
* Case: Persian is caseless; no `re.IGNORECASE` is needed.
* Typed variants accepted (nothing else): Arabic Yeh `ي` / Kaf `ك`, ZWNJ and bidi
  marks, and the common spelling «لیدربورد» — all folded to the same five targets.
* Chat scope: private **and** group/supergroup, identical code path. Personal
  screens use the **sender** as subject and are claimed to the sender, so inside a
  group only they can press their buttons (`PanelGuardMiddleware`,
  `services/panel.py` `PERSONAL_PANEL_TYPES` / `OWNER_SCOPED_CALLBACKS`) — the same
  rule the profile buttons already follow.

# Tests (Task 4)

`tests/shortcuts/test_text_shortcuts.py` — **14 tests, all passing**:

| Required test | Result |
|---|---|
| `test_shortcut_sib_menu_opens_main_menu` | PASS |
| `test_shortcut_sib_bagh_opens_garden` | PASS |
| `test_shortcut_sib_shop_opens_shop` | PASS |
| `test_shortcut_sib_kaart_opens_my_cards` | PASS |
| `test_shortcut_sib_skin_opens_skin_swap` | PASS |
| `test_shortcut_sib_leaderboard_opens_leaderboard` | PASS |
| `test_shortcut_sib_raahnamaa_opens_help` | PASS |
| `test_shortcut_exact_match_only` | PASS |
| `test_shortcut_does_not_hijack_slash_commands` | PASS |
| `test_shortcut_no_collision_with_existing` | PASS |
| `test_shortcut_exact_match_only_never_replies` (extra) | PASS |
| `test_shortcuts_registered_on_the_start_router` (extra) | PASS |
| `test_shortcut_cards_panel_stays_owner_locked_in_group` (extra) | PASS |
| `test_shortcuts_work_in_private_and_group` (extra) | PASS |

Run (SQLite test database, no Telegram/MariaDB needed):

```bash
PYTHONPATH=/home/user sib_TEST_DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
  .venv/bin/python -m pytest -q tests/shortcuts       # 14 passed
```

## 8. Full-suite result (no regressions)

| Tree | Result |
|---|---|
| baseline `25b1d87` (clean git worktree, same environment) | **8 failed, 2457 passed, 25 skipped** (238 s) |
| A4 tree | **7 failed, 2472 passed, 25 skipped** (226 s) |

Every A4 failure also fails on the untouched baseline (the A4 set is a strict
subset — the 8th baseline failure, `test_apple_battle_scene_library.py::test_restart_during_narration_reuses_the_same_plates`,
is flaky and passed here). All of them are environment/asset issues unrelated to
A4: missing MariaDB (`Can't connect to MySQL server on '127.0.0.1'` — row-lock and
transaction regressions), `tests/test_character_art.py` image-asset assertions,
`invasion_v2/test_phase8c.py::test_migration_chain_head` (alembic head
`045_group_welcomed_at` vs the test's expected `043_apple_mine_shift`), and
`tests/test_navigation.py` (reaches for the same missing MySQL).

Targeted re-runs of the areas the refactor touches:
`tests/test_card_upgrade_ui.py tests/test_nav_root_fixes.py tests/test_group_menu_games_guide.py tests/test_screen_transitions.py tests/test_phase2_nav_garden.py tests/test_handlers.py tests/test_a3_start_texts.py tests/test_banners.py` → **104 passed**;
`tests/test_profile_skins.py tests/test_wiki.py tests/test_player_cards.py tests/test_card_roster.py tests/test_shop_card_price.py tests/test_garden_capacity.py tests/test_garden_state.py tests/test_marriage_profile.py` → **243 passed, 2 skipped**.

## 9. Collisions checked, cleared

* The five strings vs. every exact-match trigger in the bot (§1 table):
  **no collision** — mechanised in `test_shortcut_no_collision_with_existing`,
  which imports the other modules' pattern constants (`MINE_COMMAND_PATTERN`,
  `GROUP_SHORTCUT_PATTERN`, `PROPOSAL/DIVORCE/FLOWER_TRIGGER_PATTERN`,
  `HUG/KISS_TRIGGER_PATTERN`) and asserts each new phrase matches the new pattern
  and **no** existing one (mutual exclusivity).
* The new pattern vs. the existing wake words («سیب»، «سیب منو»، «سیب بازی»،
  «سیبام»، «سیب پروفایل»، «سیب باغ»، «باغ من»): no match.
* Slash commands (`/shop`, `/leaderboard`, `/top`, `/help`, `/start`, `/سیب شاپ`):
  no match and no reply.

## 10. Limitations

* **Channels:** the bot registers no `channel_post` handlers at all, so channel
  posts are not processed — the shortcuts (like every other wake word) do not fire
  in channels. In a channel where the bot is only an admin, nothing changes.
* **Group privacy mode:** in groups Telegram only delivers ordinary text to the bot
  when *Group Privacy* is disabled in BotFather (or the message replies to the bot
  / mentions it). This is the pre-existing precondition of «سیب منو»، «سیب باغ»
  and the claim word «سیب»; A4 adds no new requirement.
* **Admin flows hold the text first:** while a per-user admin flow is active
  (`handlers/admin_users.py:212`, `handlers/admin_broadcast.py:110`,
  `handlers/admin_center.py:140/194/503` — those routers are registered **before**
  `start_router`), the typed text is consumed as flow input. Identical for all
  existing wake words.
* **Missing send permission:** if the bot may not post in the chat, Telegram
  rejects the send, so the alias cannot deliver its screen — the wake-word
  handlers register/claim in the same order as the existing ones and inherit the
  same failure mode.
* **Rate limiting:** `RateLimitMiddleware` (in-memory) can drop repeated messages,
  as it does for every command.
* **Naming:** the profile's skin entry is labelled «🎨 تغییر پوسته» / `Change Skin`;
  the literal string «تعویض اسکین» does not exist in the codebase, so «سیب اسکین»
  opens that screen (the task's own wording: "the 'تعویض اسکین' section currently
  reachable from inside the profile").
* **No banner art** exists for cards/skin/help screens (`services/banner.py` BANNERS
  has `main_menu`, `games`, `garden`, `shop`, `leaderboard` only), so those three
  aliases answer as text+caption screens — same as their buttons; shop and
  leaderboard reuse the existing banner path.

## 11. Guardrails

* `invasion_enabled` is **untouched** (still seeded `false` by
  `database/migrations/versions/036_invasion_v2.py:131`; the only writer stays
  `services/invasion_v2/admin_controls.py:65`). No file under
  `services/invasion_v2/` was modified.
* No invasion v2 logic, no business rules, no handler deletion, no migration.
* Diff scope: `handlers/start.py`, `tests/shortcuts/test_text_shortcuts.py`,
  `docs/admin/A4-REPORT.md` only.

## 12. Commits

```
[a4] recon for text shortcuts      — recon + this report's findings
[a4] add five new text shortcuts   — handlers/start.py (pattern, map, openers, shared builders)
[a4] tests and report              — tests/shortcuts/ + report completion
```
