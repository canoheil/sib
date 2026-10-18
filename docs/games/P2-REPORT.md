# P2 — Profile privacy fix and reply-to-view («پروفایلش»)

## Reconnaissance

- **Profile renderers.** There is no `handlers/profile.py`. The profile screen is built in three places, and each one appends the same marriage block:
  - `handlers/start.py::handle_apple_profile`: the «👤 پروفایل من» panel (callback `apple_profile`).
  - `handlers/start.py::_open_sender_profile`: the «سیب پروفایل» / «سیب من» wake words.
  - `handlers/apple_battle.py::cmd_profile`: the `/profile` command.
- **Where `💍 همسر:` is generated.** `handlers/marriage.py::profile_marriage_lines()` builds the line. Before this change it called `spouse_handle(spouse)`, which returned `@{username}` (Markdown-escaped) whenever a username existed. The first name was used only as a fallback.
- **How the spouse data is fetched.** `MarriageService.get_active_marriage_for_user(session, user_id)` returns the ACTIVE marriage with the two users eagerly loaded. `_spouse_of()` picks the other side. Love comes from the shared `Marriage.love_score`. No extra query is involved.
- **Reply-based triggers.**
  - «بغل سیبی» (`handlers/sib_hug.py`): `@router.message(F.text.regexp(HUG_TRIGGER_PATTERN))`, then checks `message.reply_to_message`, `reply.from_user`, `is_bot`, and self-target. Validation runs before any write.
  - «دوئل سیبی» (`handlers/apple_duel.py`): a custom `DuelCommand(Filter)` plus `validate_reply()`. Duel logic is not touched by this phase.
  - «پروفایلش» follows the same reply-validation pattern and is registered on the existing `start` router, so `handlers/__init__.py` needed no change.
- **Repository note.** The repo tracks a nested `sib/bots/` directory, so `sib.bots` imports fail when the checkout root is imported as the `sib` package. The tests were run through an out-of-repo symlink root (`/tmp/pkgroot`), with `bots` mapped to the nested copy. The repository itself was not changed for this.

## Privacy changes

### 1. Spouse line shows only a sanitized first name

`core/text.py::plain_display_name(name, fallback)` is a new helper. It:

1. removes HTML tags;
2. removes `tg://`, `http(s)://`, `t.me/` and `www.` link tokens;
3. removes every `@` (ASCII and full-width);
4. collapses whitespace to one line and caps the length at 32 characters;
5. falls back when the result is empty;
6. Markdown-escapes the result, so the name cannot break the message.

`handlers/marriage.py::spouse_display_name(user)` uses it on `first_name` only. The old `spouse_handle` is gone. The username is never used, not even as plain text. The fallback when there is no first name is «یک سیبی».

Result for a spouse «نیلوفر» with username `sib_spouse`:

```
💍 همسر: نیلوفر
❤️ عشق سیبی: ██████░░░░ 60%
```

If the user is not married, the line is still omitted.

### 2. Public profile for another player (`پروفایلش`)

- **Trigger.** The whole message must be «پروفایلش», ignoring case and surrounding spaces. ZWNJ and Arabic Yeh are folded, as in the other Persian wake words. `سیب پروفایل` and sentences that merely contain the word do not trigger it.
- **Shown.** The sanitized first name, the account level and XP bar, card count, main card, battle and win counts, and the marriage block (spouse first name and Love gauge).
- **Not shown.** Telegram ID, username, `tg://` links, the wallet (apples and seedlings), and the owner-only skin-change control. The result is a new message with no keyboard, and no panel is claimed.
- **Not auto-registered.** The target is looked up and never created. The viewer is registered as usual.

## Persian reply messages

| Situation | Exact text |
|---|---|
| No reply | برای دیدن پروفایل یک نفر، روی پیامش ریپلای کن و بنویس «پروفایلش». |
| Replied to a bot | ربات‌ها که پروفایل ندارن! 😄 |
| Target not in the database | این کاربر هنوز توی بازی ثبت‌نام نکرده. |
| Replied to a channel post or anonymous admin (no user identity) | این پیام مال یه بازیکن واقعی نیست؛ روی پیام یه بازیکن ریپلای کن. |

The last row is not in the spec. It mirrors the hug handler's existing "not a user" case.

## Tests

- `tests/test_marriage_profile.py` (updated):
  - `test_profile_shows_spouse_first_name_not_username`
  - `test_profile_does_not_expose_spouse_telegram_link`: checks all three renderers, plus a hostile first name (`@sib tg://user?id=… <a href=…>`).
  - `spouse_display_name` unit tests (first name only, never the username, strips `@`/HTML/links, Markdown-safe).
  - The existing `@username` assertions were changed to first-name assertions.
- `tests/test_profile_reply_view.py` (new):
  - `test_reply_profile_view_works`
  - `test_reply_profile_view_no_reply`
  - `test_reply_profile_view_target_bot`
  - Trigger matching (accepts and rejects cases).
  - Unregistered target: refused, and the target row is not created.
  - Privacy: no `@`, `tg://`, `t.me/`, Markdown link, username, or ID in the public view.
  - No wallet lines, no owner keyboard, viewer registered and target not.

## Verification

Run through `/tmp/pkgroot` with a fresh SQLite test DB per run.

- `test_marriage_profile.py` + `test_profile_reply_view.py` + `tests/shortcuts`: all pass.
- Full suite on this branch: **2569 passed, 36 failed, 25 skipped**.
- Full suite on the `main` baseline (`bb5f95c`, exported to `/tmp`): **2546 passed, 36 failed, 25 skipped**.
- The 36 failing tests are identical on both sides. None is related to profiles; the causes are environmental (Pillow image-mode and `icc_profile` differences in the art tests, `sib.sib` and `scripts` import paths, and a few pre-existing invasion and parties checks). The 23 extra passes are the new tests.

**Test-environment caveat:** the shared SQLite file leaks rows between runs, because the `db_session` fixture does not clear `users`. A reused DB file produces spurious `UNIQUE constraint failed: users.id` errors in unrelated tests. Use a fresh DB file per run.

## Known issues and follow-ups (not changed here)

1. **Hug mentions still use `tg://user?id=` links.** `handlers/sib_hug.py::_mention()` builds clickable mention links. The P2 rule covers the spouse and the profile, so the hug message is out of scope here. It should be reviewed against the same privacy principle.
2. **Owner header is not Markdown-escaped.** `👤 **پروفایل سیبی {user.first_name}**` in `handlers/start.py` is raw. A name containing `_` or `*` could break the whole profile message. A one-line `safe_name` fix would address it.
3. **Help text does not list «پروفایلش».** `services/help_content.py` is unchanged, so players will not discover the feature from the guide yet.
