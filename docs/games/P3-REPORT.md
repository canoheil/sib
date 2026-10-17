# P3 — Social privacy and Markdown escaping

Follow-up to P2. It removes the `tg://user?id=` links from the hug and kiss
actions, escapes player names everywhere they are interpolated into a
Markdown message on the profile and social screens, and documents the
«پروفایلش» shortcut in the help guide.

## Reconnaissance

Every `tg://` link and every unescaped name in a Markdown message was listed
with `grep`. Findings:

| Location | What it does | Status |
|---|---|---|
| `handlers/sib_hug.py::_mention` | Builds `[name](tg://user?id=…)` for the hug line | **Fixed** (P3 task 1) |
| `handlers/sib_kiss.py::_mention` | Same, for the kiss line | **Fixed** (P3 task 1) |
| `handlers/start.py` profile headers (×2) | `{user.first_name}` raw in Markdown | **Fixed** (P3 task 2) |
| `handlers/start.py` main-menu greeting | `سلام {name}` raw in Markdown | **Fixed** (found in the sweep) |
| `handlers/apple_battle.py::cmd_profile` header | `{user.full_name}` raw in Markdown | **Fixed** (found in the sweep) |
| `handlers/apple_battle.py` owner's favorite card | `**{name}**` raw | **Fixed** (found in the sweep) |
| `handlers/start.py` favorite card (×2) | `character.name_fa` raw | **Fixed** (found in the sweep) |
| `handlers/admin_center.py::_user_panel` | `name` and `@username` raw; usernames may contain `_` | **Fixed** (found in the sweep) |
| `handlers/daily_chest.py::_mention` | `tg://user?id=` in the group chest notice | Not changed (see *Out of scope*) |
| `services/player_xp.py` level-up fallback | `<a href="tg://user?id=…">` in group HTML | Not changed (see *Out of scope*) |
| `handlers/apple_duel.py` invitation | raw `full_name` | Not a crash: sent with `parse_mode=None`; duel logic not touched |
| `handlers/admin_users.py` dashboard | raw name | Not a crash: sent with `parse_mode=None` |
| `handlers/start.py` `/start` greeting | raw name | Not a crash: sent with `parse_mode=None` |

## Task 1 — `tg://user` removed from social interactions

`handlers/sib_hug.py` and `handlers/sib_kiss.py` now use one helper:

```python
def _display_name(first_name) -> str:
    return plain_display_name(first_name, "یک سیبی")
```

The old `_mention(user_id, first_name)` built the link. It is renamed to
`_display_name(first_name)` because it no longer takes or uses a user ID.
Its call sites are updated.

Before and after, for a sender named `علی_رضا*`:

```
before: 🤗 [علی\_رضا\*](tg://user?id=811001) محکم بغلِ باب کرد! 🍎
after:  🤗 علی\_رضا\* محکم بغلِ باب کرد! 🍎
```

The escaped name renders as `علی_رضا*` in Telegram. It is plain text and is
not clickable. The message contains no `tg://`, no Markdown link syntax, and
no Telegram ID.

**Confirmation:** `grep -rn "tg://" handlers/sib_hug.py handlers/sib_kiss.py services/sib_hug.py services/sib_kiss.py` finds only two docstring lines that describe the rule. No code emits a `tg://` link in the hug or kiss flows.

## Task 2 — Markdown escaping in the profile and social screens

Three helpers are used, each for a different job:

- `safe_name(name, fallback)`: Markdown-escapes a name and falls back when it is empty. Used for the **owner's own** name, where the name should stay as the player typed it.
- `escape_markdown(text)`: escapes `\`, `_`, `*`, `` ` `` and `[` in any free text (character names, usernames).
- `plain_display_name(name, fallback)`: for **other players'** names. Strips `@`, HTML tags and link schemes, collapses whitespace, caps the length at 32 characters, then escapes.

Examples, with the name `Soheil_Dev`:

```
👤 **پروفایل سیبی Soheil\_Dev** 🍏          (own profile header, safe_name)
👤 **پروفایل Soheil\_Dev Ali\*Rad**          (/profile header, safe_name on full_name)
🎖 …  ⭐ کارت اصلی: **قهرمان sib\_sorkh** …   (favorite card, escape_markdown)
کاربر: Soheil\_Dev (@soheil\_dev\_99)        (admin panel, safe_name + escape_markdown)
```

Escaping only turns the characters into literal text. Nothing is removed,
except what `plain_display_name` strips on purpose.

A name that is not escaped and contains a single `_` makes Telegram reject the
whole message with `can't parse entities`. The profile was never delivered
in that case. The photo caption fell back to plain text, which failed in the
same way.

### Behavior change in an existing test

`tests/test_player_level_art.py` asserted that the raw favorite-card name
`قهرمان sib_sorkh` appears in the profile caption. That assertion pinned the
unsafe output. It now asserts `escape_markdown(name)`. The change is
intentional; it is not a weakening of the test.

## Task 3 — Help guide

`services/help_content.py`, section «⌨️ میان‌برها», has a new bullet:

> • **«پروفایلش»** (با ریپلای روی پیام یک بازیکن): برای دیدن پروفایل آن بازیکن.

The shortcuts body is 691 characters, under the 1024-character page limit
enforced by `test_every_section_body_under_1024_chars`. The bullet keeps it
within that limit.

## Task 4 — Tests

| Test | File | What it checks |
|---|---|---|
| `test_hug_does_not_expose_tg_user_link` | `tests/test_sib_hug.py` | Full handler path. No `tg://`, no `](`, no Telegram ID; the hostile name `tg://user?id=5 باب` is reduced to `باب`; the name `علی_رضا*` is escaped. |
| `test_kiss_does_not_expose_tg_user_link` | `tests/test_sib_kiss.py` | Same contract for the kiss handler. |
| `test_profile_handles_underscores_in_name` | `tests/test_profile_markdown_escape.py` | `Soheil_Dev` renders via the «سیب پروفایل» path with no exception; the output is escaped and balanced. |
| `test_help_contains_profile_reply_shortcut` | `tests/help/test_help_sections.py` | The shortcuts section documents «پروفایلش» with its reply rule. |

Additional tests in the same new file (`tests/test_profile_markdown_escape.py`):

- Underscore names on the other profile screens: the «👤 پروفایل من» panel, `/profile` (with `full_name`), the main-menu greeting, the «پروفایلش» public view, the admin user panel (username), and a married profile with a spouse named `Pari_*Ahmadi`.
- A parametrized set of hostile names: `*bold*`, `` `code ``, `[link](x)`, `a_b_c`, `_`, `\`, `**__``[[`.

`assert_legacy_markdown_balanced` checks the output the way Telegram's legacy
parser does: unescaped `_`, `*` and `` ` `` must come in pairs, and an
unescaped `[` must have a closing `]`. Backslash escapes are skipped. It is a
structural stand-in, not the real parser, which is not available offline.

The hug and kiss tests that pinned `tg://user?id=…` in their expected output
now assert the absence of `tg://` and check the plain name.

## Verification

Run through `/tmp/pkgroot` with a fresh SQLite DB file per run.

- Focused suites (`test_profile_markdown_escape`, `test_sib_hug`, `test_sib_kiss`, `tests/help`, `test_marriage_profile`, `test_profile_reply_view`, `tests/shortcuts`): 162 passed, 4 skipped.
- `test_player_level_art.py`: 38 passed (after the assertion update).
- Full suite on this branch: **2586 passed, 37 failed, 25 skipped**.
- Full suite on the `main` baseline (`bb5f95c`, exported to `/tmp`): **2546 passed, 36 failed, 25 skipped**.
- The 36 baseline failures are the same 36 on this branch. They are environmental (Pillow image modes, asset dimensions, `sib.sib`/`scripts` import paths, and a few pre-existing checks). None are related to profiles or social features.
- The 37th failure is `test_apple_battle_scene_library.py::test_restart_during_narration_reuses_the_same_plates`. **It is flaky on `main` too.** With the baseline code, `PYTHONHASHSEED` 1, 2 and 6 fail and 3, 4 and 5 pass, so the outcome depends on the process hash seed, not on this change. It is pre-existing and was not touched, because apple-battle plate selection is outside this phase.

**Test-isolation note:** the shared SQLite test DB keeps user rows between
tests. The new tests use their own user IDs (`8990`–`8992`), because renaming a
shared row would break other modules' assertions. The DB file name must
contain `sibcom_sib_test`, or the fixture refuses to run.

## Out of scope — recommended follow-ups

1. **`handlers/daily_chest.py::_mention`** still builds a `tg://user?id=` link in the group chest notice. Its existing tests assert that link, so this is a product decision. It should follow the same rule as hug and kiss.
2. **`services/player_xp.py` (level-up notice, around line 649)** sends `<a href="tg://user?id=…">بازیکن</a>` to the group when the direct message fails. This is the same leak, in HTML mode.
3. **Help guide** says nothing about the reply rule for the other social shortcuts (hug, kiss). Consider documenting that they also show plain names.
