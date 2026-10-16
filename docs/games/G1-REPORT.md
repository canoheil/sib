# G1 — Turn-based board transport recovery

## Reconnaissance (baseline 57c5193; line numbers before edits)

All five interactive games delegate to `handlers/games/common.py:210`.

| Game | Handler | DB write | Render call | Retry? | Reject path |
|---|---|---|---|---|---|
| Connect Four | handlers/games/connect_four.py:11 | common.py:291, commit :341 | common.py:477,499,545,559 | Existing UI attempts | engine.py:85,93 → common.py:274,390 |
| Tic-Tac-Toe | handlers/games/tic_tac_toe.py:11 | same | same | same | engine.py:73,80 → shared rejection |
| Rock Paper Scissors | handlers/games/rps.py:11 | same | same | same | engine.py:83 → shared rejection |
| High Card | handlers/games/high_card.py:11 | same | same | same | engine.py:89 → shared rejection |
| Battleship | handlers/games/battleship.py:11 | same | same | same | engine.py:142,155 → shared rejection |
| Apple Battle | No player move callback; games/apple_battle/engine.py:189 schedules narration | Automatic cinematic progression | Automatic narration / common refresh | Existing UI infrastructure | No duplicate player-move path; engine.py:172 only exposes refresh |
| Apple Duel | handlers/apple_duel.py:102 is invitation acceptance, not a turn | services/apple_duel.py:139 | services/apple_duel_presentation.py:302,315 | Separate cinematic infrastructure | services/apple_duel.py:167–168 returns existing invitation |

Engine paths in the table mean `games/<game>/engine.py`.

### Verified cause

The hypothesis is only partially applicable to this checkout. `safe_edit_message_text`,
`safe_edit_callback_message`, `_run_edit_attempts`, `safe_edit_screen` and refresh
buttons already exist in services/telegram_ui.py. Rejected actions already re-render
authoritative state outside the match lock, but show an error alert. Existing flood
wait handling caps waits at 10 seconds and consumes attempts; exhausted edits do not
send fresh boards. The fix is scoped to the five actual interactive games. Apple
Battle and Apple Duel are automatic/cinematic, not turn-based, and remain unchanged.

## Changes and reject paths

All five interactive games now use the same safe board transport for active,
finished, friendly/shared and callback-local boards. The authoritative transaction
still commits before any Telegram await. Exhausted edits attempt a fresh message;
replacement ids are persisted for group boards, including rejected-move recovery.
The callback reference follows replacements. Failed sends log and return rather
than undoing state. Old messages are left available (no destructive deletion).

The following before/after applies individually to **Connect Four, Tic-Tac-Toe,
Rock Paper Scissors, High Card, and Battleship**, through their shared dispatcher:

```python
# Before (all five): recovery already existed, but acknowledgement was an error.
await safe_answer_callback(callback, reason + "\nآخرین وضعیت بازی بارگذاری شد.", show_alert=True)
await render_board_for(callback, bot, match, state, user.id)

# After (all five): render first; only duplicate/stale-turn feedback is changed.
await render_board_for(callback, bot, match, state, user.id)
feedback = early_exit.get("feedback")
await safe_answer_callback(
    callback,
    feedback or (reason + "\nآخرین وضعیت بازی بارگذاری شد."),
    show_alert=not bool(feedback),
)
```

| Fixed game | Before reject path | After reject path |
|---|---|---|
| Connect Four | Turn ownership/full column → error alert + recovery | Stale turn → informational recovery; genuinely full column retains original error |
| Tic-Tac-Toe | Turn ownership/occupied cell → error alert + recovery | Stale turn/repeated cell → informational recovery |
| Rock Paper Scissors | Locked choice → error alert + recovery | Locked choice → informational recovery |
| High Card | Locked decision or repeated draw → error alert + recovery | Both → informational recovery; card never drawn again |
| Battleship | Turn ownership/repeated shot → error alert + recovery | Stale turn/repeated shot → informational recovery |

Exact duplicate message: **صفحه‌ی بازی تازه شد؛ حرکت شما قبلاً ثبت شده بود.**

Exact stale-turn message: **صفحه‌ی بازی تازه شد؛ اکنون نوبت بازیکن دیگری است.**

Terminal participant clicks retain their existing text but no longer show an error
popup. Genuine invalid-action messages remain unchanged. No boards, keyboards,
rules, win conditions or schemas were changed. Apple Battle and Apple Duel have no
player-turn duplicate path and their handlers/presentation services were not edited.

## Helper and parameters

`safe_edit_board(callback, text, *, reply_markup=None, parse_mode=None,
max_retries=3, base_delay=0.5)` returns a boolean and never raises ordinary
presentation exceptions. Cancellation propagates normally.

* Initial edit plus three retries, sleeping 0.5, 1, 2 seconds: short transient
  outages recover without excessive ordinary-error latency (3.5 seconds total).
* "message is not modified" succeeds immediately.
* RetryAfter sleeps the full requested duration, without consuming retries.
* Other bad requests, forbidden, network and unexpected transport errors retry.
* Exhaustion logs WARNING with message_id and error, and returns False.
* Game adapters then attempt sending the identical text/keyboard to the same chat,
  using the existing Markdown parse mode. They return/persist the new message id.
* Shared non-game UI helpers retain their existing behavior.

## Maintenance sweep

Skipped. A timestamp does not establish that a board is stale; there is no durable
render-pending marker. Blind sweeps would edit healthy boards and increase rate
limits. Adding a marker requires storage design beyond this schema-preserving task.

## Tests

Command (SQLite test database only):

```sh
PYTHONPATH=/home/user:/home/user/sib \
sib_TEST_DATABASE_URL=sqlite+aiosqlite:////home/user/sib/.cache/sibcom_sib_test.db \
.venv/bin/pytest tests/games tests/test_gameplay_sync.py tests/test_battleship_reliability.py -q
```

**32 passed** (15 new G1 cases, 17 existing regression cases). Real DB regressions
cover committed move → edit failure plus failed send → repeated move → exact
current-state render for all five interactive games, plus a separate High Card
duplicate-draw regression. Unit cases cover bad request, forbidden and network
retries; unmodified success; full 42-second RetryAfter (mocked sleep), including
multiple flood waits with zero ordinary retries; exhaustion/warning; informational
wording; outsider feedback preservation; and fresh-message fallback id.

## Limitations and safety

* Telegram cannot be guaranteed reachable. If both edits and sending fail, recovery
  requires a later click/refresh; no background durable transport queue was added.
* Repeated flood waits can keep a request alive indefinitely, as required by the
  non-counting RetryAfter contract. No DB match lock is held during those sleeps.
* No action ids/version tokens exist. Repeated column clicks after an entire turn
  cycle, or RPS/High Card clicks after round rollover, can be legitimate new moves;
  changing that would require a protocol/rule change. Stale-turn feedback therefore
  does not falsely claim a proven duplicate.
* Concurrent presentations can race; this patch does not introduce distributed
  render ordering or a durable outbox. Tests use SQLite, not MariaDB locking or live
  Telegram. Full repository suite was not run.
* Replacement sending is best effort; failed persistence can leave a stale stored
  message id. Old boards are not deleted, so callbacks remain usable for recovery.
* invasion_enabled, invasion v2, archived invasion tables, and DB schema unchanged.
