# P4 — Partial revert: clickable hug and kiss mentions

Follow-up to P3. P3 removed the `tg://user?id=` links from the hug and kiss
lines and showed the two player names as plain text. P4 **reverts that part
only**: the hug and kiss lines use clickable name links again. The Markdown
escaping from P3 stays.

## Why the revert

Hug and kiss happen in a group chat, and everyone in the group already sees
each other's profiles. A clickable name adds no new exposure, so the privacy
reason for P3 does not apply to these two lines. The links also look better
in the group.

The rules that still apply:

- Spouse lines and the `پروفایلش` profile view keep plain text, with no
  `@username` and no `tg://` link (P2 rule, unchanged).
- Names are still escaped with `safe_name()`, so a `*`, `_`, or `[` in a
  name cannot break the message.

## What changed

| File | Change |
|---|---|
| `handlers/sib_hug.py` | `_display_name()` replaced by `_mention(user_id, first_name)`, which returns `[safe_name(name)](tg://user?id=ID)`. Both call sites now pass the user IDs. Import changed from `plain_display_name` to `safe_name`. The file is byte-identical to `bb5f95c`. |
| `handlers/sib_kiss.py` | Same change. The file is byte-identical to `bb5f95c`. |
| `tests/test_sib_hug.py`, `tests/test_sib_kiss.py` | Original link expectations restored. The P3 absence-of-link tests are renamed to `test_hug_mentions_are_clickable_and_markdown_escaped` and `test_kiss_mentions_are_clickable_and_markdown_escaped`. They assert the `[` opener, the `](tg://user?id=…)` target for both users, the escaped name, and `parse_mode="Markdown"`. |

Not changed in P4:

- The profile logic and the `پروفایلش` feature (`handlers/start.py`,
  `handlers/marriage.py`, `core/text.py::plain_display_name`).
- The daily chest (`handlers/daily_chest.py`) and the XP level-up notice
  (`services/player_xp.py`). Both are out of scope.
- Invasion v2 and `handlers/apple_duel.py`.

The P3 table in `P3-REPORT.md` describes the state after P3. For hug and kiss,
its "Fixed" status is superseded by this report.

## Test results

- Focused run (hug, kiss, profile Markdown escaping, help, reply view, marriage
  profile, daily chest group, player level art): 271 passed, 5 skipped.
- Full suite on this branch: 2590 passed, 33 failed, 25 skipped.
- Full suite on the `bb5f95c` baseline, run the same way: 2547 passed, 35 failed,
  25 skipped.
- Every failure on this branch also fails on the baseline. The 33 failures are
  environmental (Pillow image modes, a few parties and invasion checks, and
  similar), not caused by P4. The baseline's two extra failures are migration
  tests that fail only because of the baseline harness layout.
