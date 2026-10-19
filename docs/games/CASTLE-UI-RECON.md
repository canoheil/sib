# CASTLE-UI-RECON — Apple Castle menu spam bug

**Phase:** investigation only — no code was changed.
**Inspected baseline:** `554d0ce` (`[ref1] implement friend referral system with deep links`; the castle handler itself is unchanged since `c18dd6a`).
**Scope:** why clicking an inline button in the «🏰 قلعه سیبی» menu posts a
*new* castle menu message into the group instead of editing the existing one.

## 1. TL;DR — the exact reason

The castle menu has exactly one renderer, `show_castle()`
(`handlers/castle.py:104-120`), and that renderer can only **send**:
`message.answer_photo(...)` at line 114, or `message.answer(...)` at line 120.
It never edits. It was written for the text entry point «قلعه سیبی»
(`castle_entry`, `handlers/castle.py:123-135`), where a new message is correct.

The callback handler `castle_callback` (`handlers/castle.py:166`) reuses that
same entry renderer at the end of almost every button flow
(`handlers/castle.py:385-388`), passing it `callback.message`
(`handlers/castle.py:168`). In aiogram, `Message.answer*()` **always sends a
new message to the same chat** — it makes no difference that the message in hand
is the menu the user clicked. So every successful click ends with the menu
being sent again as a fresh message, and the previous menu is left in the chat
untouched (nothing ever deletes or edits it).

`safe_edit_callback_message` is not used: it is not even imported
(`handlers/castle.py:18` imports only `safe_answer_callback`; a search for
`safe_edit` / any `edit_*` call in the whole file finds **zero** hits).

## 2. Where things live (correction to the phase brief)

There is **no `keyboards/castle.py`**. The castle keyboard builder is
`keyboard(group_id, donate=False, is_admin=False)` inside
`handlers/castle.py:56-72`, using the shared glass buttons from
`keyboards/common.py`. The caption builder is `format_castle_caption`
(`handlers/castle.py:75-101`). Everything castle-UI lives in this one handler
file; money/war state changes go through `CastleService` / `WarService` and
are not involved in the bug.

Current button set (`handlers/castle.py:59-71`):

| Screen | Button | `callback_data` |
|---|---|---|
| main | 🍏 اهدای سیب | `castle:donate:{group_id}` |
| main | 🎁 دریافت درآمد | `castle:income:{group_id}` |
| main | 🏆 برترین اهداکنندگان | `castle:top:{group_id}` |
| main (admin) | ⚔️ شروع حمله | `castle:war_start:{group_id}` |
| donate | ۱۰۰ 🍏 / ۵۰۰ 🍏 / حداکثر | `castle:100:…` / `castle:500:…` / `castle:max:…` |
| donate / war | 🔙 بازگشت | `castle:view:{group_id}` |

## 3. Click-by-click trace

All of these start at `castle_callback` (`handlers/castle.py:167`) after the
shared validation/membership checks (lines 168-196), which fail with an
alert only — no spam there.

| Action | State change | Telegram delivery today | New message? |
|---|---|---|---|
| `donate` (open submenu) | none (keyboard swap only) | toast «🏰» (375) → `show_castle(…, donation_menu=True)` (386-388) → `answer_photo` (114) | **yes** |
| `100` / `500` / `max` | `CastleService.donate` (350-356) | receipt alert via `safe_answer_callback(..., show_alert=True)` (375) → `show_castle` (386-388) → `answer_photo` (114) | **yes** (alert + duplicate menu) |
| `income` | `CastleService.claim_daily_income` (359-361) | receipt alert (375) → `show_castle` (386-388) — caption is unchanged by an income claim, but it re-sends anyway | **yes** |
| `view` (🔙 بازگشت) | none | toast «🏰» (375) → `show_castle` (386-388) | **yes** — even *Back* spawns a fresh menu |
| `top` | read-only query (363-374) | toast «🏰» (375) → `message.answer(leaders)` (384), with no keyboard / no way back | **yes** |
| `war_start` (shielded) | none | alert (218) → `message.answer(WAR_SHIELD_WARNING, …)` (219) | **yes** |
| `war_start` / `war_break_shield_confirm` | maybe `WarService.break_shield` | `offer_war_targets` → toast (159) → `message.answer(WAR_TARGET_PROMPT, …)` (160-162) | **yes** |
| `war_pick` | none | toast (267) → `message.answer(confirm text, …)` (268) | **yes** |
| `war_confirm` | `WarService.declare_war` | alert (285) → `bot.send_photo`/`send_message` declaration to attacker (305/312) and defender (323/330) | yes — but see §5: an announcement to two chats, plausibly deliberate |
| any `CastleError`/`WarError` | rolled back / refused | `safe_answer_callback(..., show_alert=True)` (390) only | no ✔ |

So the spam is a *successful path* behaviour. Every donate click that should
just update «🍏 سیب اهدا شده» in the caption instead posts a whole new photo
menu into the group; a player donating ۱۰۰ five times leaves five extra castle
photos behind, plus one per submenu open/close in between.

Two aggravating details:

1. **The alert was already right.** Donate/income already show the receipt as
   a proper alert at `handlers/castle.py:375`
   (`show_alert=bool(receipt)`). The new message after it is pure duplicate —
   deleting the "refresh" would already help, but the caption would then go
   stale after a donation, which is why the correct fix is an *edit* (§6).
2. **Orphaned menus stay alive.** Callback validation only checks that the
   `group_id` embedded in `callback_data` matches `message.chat.id`
   (lines 170-180) and that the clicker is a current group member. There is no
   message-id/panel-owner bookkeeping for this shared group screen, so every
   orphaned menu in the scrollback remains fully clickable forever — spam
   compounds instead of self-limiting.

## 4. Why it sends instead of editing

- `show_castle()` takes a bare `Message` and unconditionally answers it:
  `message.answer_photo(FSInputFile(image), caption=…, reply_markup=…)`
  (`handlers/castle.py:114-116`), falling back to `message.answer(...)`
  (line 120) only when the photo send itself throws or no level art exists.
  It has no `CallbackQuery`/`Bot` parameter and no edit branch at all.
- The handler holds everything an edit would need — `callback`,
  `callback.message`, `callback.bot` (already used for
  `get_chat_member` at line 189 and the `war_confirm` sends) — it just never
  calls an edit API.
- The castle screen is a **photo** message whenever level art is deployed
  (`castle_image(level)`, `services/castle_art.py`), and Telegram cannot edit
  a photo with `edit_message_text`. The project already solved this generally:
  `safe_edit_callback_message` → `render_callback_screen` → `render_screen`
  (`services/telegram_ui.py:739/664/556`) edits photo→photo in place via
  `edit_message_media`, and 17 other handler modules use it. The castle
  handler (and, see §7, the war handler) predates/bypasses that engine.

## 5. Fallback investigation ("does a failed edit fall back to a new message?")

**In castle code: there is no edit, hence no edit fallback involved.** The only
fallbacks present are send-side degradations, and both still produce a *new*
message:

- Photo delivery throws (or no art) → send the caption as a new text message
  (`handlers/castle.py:118-120`).
- War-declaration banner file missing → `bot.send_message` instead of
  `bot.send_photo` (`handlers/castle.py:312/330`).

**In the shared engine the castle *should* be using, the feared behaviour does
not exist** (`services/telegram_ui.py`):

- Unchanged content is a success, not a failure: `NOT_MODIFIED_MARKERS`
  (line 65) makes `_run_edit_attempts` return `True` on
  "message is not modified" (lines 245-249). Clicks that change nothing visible
  (e.g. `income` — castle totals don't move; `view` — same screen) would be
  silent no-ops, and keyboard-only changes (open/close donate submenu) edit
  the markup in place.
- A replacement message is sent **only** on an actual photo↔text type change,
  and `_replace_message` then **deletes the old message**
  (lines 516-549) — the chat still ends up with exactly one menu.
- Transient/other edit failures return failure without sending anything
  (lines 628-635), so a flaky edit degrades to "screen not refreshed",
  never to spam.

`war_confirm` is the one place a *new* message is arguably intended product
behaviour: the declaration is an announcement, and the defender's copy is in a
different chat, so it cannot be an edit. The recon recommendation is to treat
the two declaration sends as out of scope (like other announcements) and only
fix the interactive menu screens.

## 6. How we should fix it (proposal — not implemented in this phase)

1. **Split build from delivery in `handlers/castle.py`.** Extract a builder
   that returns `(caption, markup, photo)` (the current body of `show_castle`,
   lines 105-111). Keep `show_castle`/send-new for the «قلعه سیبی» text entry
   (`castle_entry`) only.
2. **Add an edit path for callbacks** and use it everywhere `castle_callback`
   and `offer_war_targets` currently answer:
   `safe_edit_callback_message(callback, callback.bot, caption,
   reply_markup=markup, photo=photo)` (preserve the current parse-mode
   behaviour). Actions `donate`, `view`, `100`, `500`, `max`, `income` then
   become in-place edits: donation updates the caption numbers (and swaps art
   on level-up via `edit_message_media`), the receipt alert at line 375 stays
   exactly as it is, and no-op renders cost nothing visually.
3. **Route the sub-screens through the same edit path**: war target list
   (160-162), shield warning (219), war-pick confirmation (268), and the
   donors leaderboard (384). These are text screens rendered onto a photo
   message — the transition engine replaces + deletes the old message, so the
   group still sees exactly one live screen. Give the leaderboard a
   `castle:view:…` Back button while doing so (today it is a dead-end plain
   message). Keep the `war_confirm` cross-group declaration sends as new
   messages (announcement semantics); optionally let the attacker's confirm
   screen be replaced by the declaration — product decision, not required to
   kill the spam.
4. **Failures stay alert-only.** Keep the existing
   `CastleError`/`WarError` alerts (lines 389-397) and the "commit before
   Telegram delivery" ordering in the module docstring: if the edit fails after
   a committed donation, the receipt alert + the next click's edit still show
   correct state; never fall back to `message.answer` from a callback.
5. **Tests for the fix phase.** Today's suite cannot see this bug:
   `tests/test_castle.py:197` replaces `show_castle` with an `AsyncMock`, so
   `test_callback_donation_and_replay` only asserts balances/donation-count and
   that `CallbackQuery.answer` ran twice — delivery is mocked away. The fix
   should add assertions that, per callback, an edit
   (`edit_message_media`/edit path) happens and `answer_photo`/`answer` are
   *not* called, covering at least: open donate submenu (keyboard-only edit),
   a ۱۰۰ donation (caption change), income (unchanged caption → no visible
   change, no new message), Back (`view`), and `top`. War-flow tests in
   `tests/test_war.py` that read screens from `message.answer` mocks will need
   their capture point moved to the edit API.

Effort estimate: small and contained — one handler file plus tests. No service,
schema or callback-data changes are needed, so castle menus already posted in
groups keep working; after the fix, each click simply edits its own message.

## 7. Related observation (out of scope)

`handlers/war.py::show_war_status` (lines 86-145) uses the identical
send-only pattern (`message.answer_photo` at 136, `message.answer` at 145) and
is also called from callbacks, so the war-status screen very likely spams the
same way on its refresh/action buttons. It deserves the same treatment in a
follow-up phase; per this phase's scope it was only read, not analysed
click-by-click.
