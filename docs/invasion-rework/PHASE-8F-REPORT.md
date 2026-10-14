# Phase 8F — the join button survived only until the first HP edit

Status: fixed. One production file touched (`services/invasion_v2/presentation.py`),
two regression tests added. No migration, no config key, no feature-flag change:
`invasion_enabled` was not read, written or seeded by this phase.

## Reported symptom

The first player joins normally and the join narrative is produced, but the
inline button «⚔️ پیوستن به نبرد» then disappears from the announcement, so no
other player can join that battle.

## Root cause

`_apply_hp_edit` → `_edit_announcement_caption` edited the announcement caption
with **no `reply_markup` argument**:

```python
await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption)
```

Telegram clears a message's inline keyboard whenever an edit of that message
omits `reply_markup` (that is exactly how `editMessageReplyMarkup` removes a
keyboard). aiogram does not paper over this: `None` is excluded from the
outgoing request, so the request carried no markup at all:

```text
with markup : ['chat_id','message_id','caption','parse_mode','show_caption_above_media','reply_markup']
with None   : ['chat_id','message_id','caption','parse_mode','show_caption_above_media']   # == omitted
```

Sequence in production: join → ack → join photo → `schedule_hp_edit` (1.5 s
debounce) → the debounced edit rewrites the caption **and silently removes the
keyboard**. Every later tap is impossible: the callback data no longer exists in
the group, and any surviving old button would be rejected by
`handle_inv2_join`'s ACTIVE/`message_id` guards.

Why the phase 8D tests did not catch it: they assert caption text only, and
[PHASE-8D](PHASE-8D-REPORT.md) §9 explicitly *claimed* the opposite ("called
without `reply_markup`, which for photo messages keeps the existing inline
keyboard"). That claim is wrong and is corrected in place in that report.

## Fix (minimal, `services/invasion_v2/presentation.py`)

1. New `join_keyboard(invasion_id)` — the single source of truth for the button
   label/callback; `announce()` now uses it instead of an inline construction
   (byte-identical markup, so no behaviour change for the first announcement).
2. `_edit_announcement_caption(..., reply_markup=None)` forwards `reply_markup`
   to `edit_message_caption` (metering, "message is not modified" swallowing and
   circuit-breaker semantics are untouched).
3. `_apply_hp_edit` computes the markup from the row it already reads:
   `join_keyboard(inv.id)` while the battle is `ACTIVE`, `None` otherwise.

Resulting behaviour:

| Edit | Battle state | `reply_markup` | Effect on the button |
|---|---|---|---|
| Announcement (`announce`) | → ACTIVE | join keyboard | shown |
| Live HP edit (debounced) | ACTIVE | join keyboard | **kept** (was: removed) |
| Killing-blow edit (sync) | VICTORY | omitted | cleared — the battle is over |
| `finalize` caption rewrite | VICTORY / DEFEAT | omitted (unchanged) | cleared |

No other call site in the repository had this bug: matchmaking re-sends its
acceptance keyboard, marriage clears stale buttons deliberately via
`edit_message_reply_markup(reply_markup=None)`, and `finalize` omits the markup
on purpose.

## Tests

`tests/invasion_v2/test_announcement_hp.py`:

- `test_hp_edit_keeps_the_join_button` — red before the fix
  (`AssertionError: live HP edit dropped the join button`), green after: the
  debounced live edit re-sends `⚔️ پیوستن به نبرد` with `inv2:join:{id}`.
- `test_kill_edit_removes_the_join_button` — the 0 HP edit carries no markup,
  i.e. the button is cleared when the battle ends.

Verification runs (Python 3.11, SQLite test database):

```text
tests/invasion_v2 + tests/test_invasion_feature_flag.py : 449 passed
full tests/ suite, fresh SQLite database                : 2356 passed, 25 skipped,
                                                          6 failed (the six
                                                          pre-existing failures
                                                          documented in
                                                          PHASE-7-REPORT.md)
```

2356 = the 2354 passing tests of the untouched baseline plus exactly these two
new tests; the failing set is identical to the baseline's. The additional
`test_apple_battle_scene_library::test_restart_during_narration_reuses_the_same_plates`
failure seen in two sandbox runs is the pre-existing intermittent test already
recorded in `docs/apple-duel.md` ("untouched baseline repeated five times
returned 1 failure / 4 successes"); it passes here on both the baseline and this
branch.

## Rollout notes

- Code-only change: no schema, config, or state migration, and it is effective
  for the next battle without any admin action.
- A battle whose announcement *already* lost its button before this deploy cannot
  be repaired retroactively (nobody can join it anymore); an admin can close it
  with `/inv2_force_end <id> victory|defeat` (see the Phase 6 note that a manual
  force-end does not post a group result).
- The circuit breaker, caption truncation, debounce/coalescing and narrative
  storage are unchanged.
