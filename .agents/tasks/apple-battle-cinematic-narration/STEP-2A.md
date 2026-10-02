# STEP-2A — Apple Battle Cinematic Narration Architecture

Date: 2026-10-02 · Branch: `arena/01a0fd14-sib` · Base: `8896891` (main)

Phase 2A changes ONLY how live narration is delivered. The battle formula,
seed, winner, rewards, matchmaking, bot difficulty and the final report are
untouched.

## 1. What changed

* Live narration used to be appended into the SAME group board message by
  repeatedly editing it (`_edit_boards` on every beat).
* Now every precomputed plan beat (intro + 3 rounds + finale) is sent as its
  OWN temporary Telegram message: **photo + short cinematic caption**.
* When the battle ends the temporary narration messages are deleted, then the
  existing final Apple Battle report is rendered exactly as before.

## 2. Files changed

| File | Change |
|---|---|
| `services/apple_battle_narration.py` **(new)** | Beat delivery, restart-safe message-id bookkeeping, best-effort cleanup, placeholder beat art resolver |
| `services/apple_battle.py` | `_narration_loop` sends separate messages; cursor persisted BEFORE the send; teardown before settlement; `build_match_state` starts narration at `cursor = 0`; `_narration_targets`; `recover_active_matches` returns `finalized_ids` |
| `services/recovery/service.py` | Cleans temporary narration messages of battles finalized during startup recovery (best effort) |
| `services/maintenance/service.py` | Cleans temporary narration messages of Apple Battles resolved out of band by the turn-timeout backstop (best effort) |
| `games/apple_battle/engine.py`, `handlers/apple_battle.py` | Docstrings only (no behavior change) |
| `tests/test_apple_battle.py` | Loop test rewritten to the new contract; engine test updated for `cursor = 0` |
| `tests/test_apple_battle_narration.py` **(new)** | Focused Phase 2A suite (mocked bot, no real Telegram calls) |

## 3. How the narration message flow works now

```
match found  → plan precomputed + persisted (result fixed BEFORE narration)
acceptance   → board message rendered (neutral placeholder, no story)
narration    → for each beat i (cursor i → i+1, persisted FIRST):
                 AppleBattleNarration.send_beat(...)
                   → per target chat: safe_send_screen(photo, caption=plan[i].text)
                   → record (beat, chat_id, message_id, group) in game_state
               (3s between beats; the board is NOT touched)
plan finished→ 1. cleanup: delete every recorded temporary message
               2. settle: finalize_match / apply_match_result / +5 apples
                  (identical to before)
               3. final report: _edit_boards(finished=True) — unchanged render,
                  unchanged winner photo, unchanged final keyboard
```

Delivery is deliberately **at-most-once**: the cursor is durably advanced
before the Telegram send. A crash between claim and send can skip one beat
(logged), but can never re-send one — the requirement to avoid duplicate
narration events after a restart wins over replay.

Targets are deduped per chat (`_narration_targets`), so a match whose two
sides share one group receives each beat exactly once.

## 4. How message IDs are persisted (restart safety)

* No in-memory list is used. The ids live inside
  `match.game_state["narration"]` — the same restart-safe JSON state that
  Phase 1 recovery already reads and re-arms from (no schema migration):

```json
"narration": {
  "version": 1,
  "cleaned": false,
  "messages": [
    {"beat": 0, "chat_id": -100123, "message_id": 4711, "group": 1, "deleted": false}
  ]
}
```

* `record_message` appends idempotently (one record per `beat + chat`) and
  rewrites ONLY the `narration` sub-object, so it can never roll back a
  concurrently persisted cursor or another worker's progress.
* `pending_messages(match_id)` returns the recorded ids that still need to be
  deleted, so cleanup always knows the full message set, even after restarts.
* `AppleBattleNarrationState.delivered_beats(...)` exposes the delivered set
  for observability/tests.

## 5. How cleanup works

* Cleanup runs in the narration loop as soon as the plan is finished — while
  the match is still `ACTIVE`, so a crash during teardown is recovered by the
  restart path (recovery finalizes the battle and deletes whatever is left).
* `AppleBattleNarration.cleanup(bot, match_id)` never raises:
  * a successful delete marks the record deleted;
  * "message to delete not found" / `MESSAGE_ID_INVALID` counts as already
    gone (nothing left to clean);
  * any other failure is logged, the record stays pending for a later pass,
    and `cleaned` stays `false`.
* Cleanup failure can never break settlement or the final report: settlement
  runs only after cleanup and does not depend on its result.
* Extra safety nets for battles that never reached live teardown:
  * startup recovery (`RecoveryService`) cleans the matches it finalizes;
  * the maintenance turn-timeout backstop cleans Apple Battles it resolves.
* Cleanup cost: one `delete_message` per narration message (max 10 per battle).

## 6. How recovery is preserved

* `recover_active_matches` still only recomputes nothing: it restores the
  persisted state, re-arms narration for incomplete battles and finalizes
  complete ones (idempotent settlement).
* Restart mid-narration: the loop resumes at the persisted cursor and only
  sends the beats that were not claimed yet → no second narration sequence,
  no duplicate messages, no recomputation.
* Restart during teardown: the match is still `ACTIVE` with a complete plan →
  recovery finalizes it and `cleanup` deletes the remaining temporary
  messages (verified by a test that simulates a half-cleaned state).
* Duplicate workers stay impossible at the scheduling layer
  (`schedule_narration` dedupe + `_NARRATION_RUNNING`), and the existing
  `last_cursor` staleness guard still stops a worker whose battle was
  advanced/finalized by someone else.
* Legacy persisted states without a `narration` block (pre-Phase-2A battles)
  resume safely: nothing is re-sent for beats already presented.

## 7. Temporary artwork (Phase 2A placeholders)

* No new images were generated and no external image service is called.
* `AppleBattleNarrationArt` is the ONE beat→photo mapping point:
  * intro → the viewer group's fighter card art (`assets/characters/*.jpg`,
    via the existing `resolve_character_photo`);
  * rounds → alternating fighter card art;
  * finale → the winner's card art.
* If a card has no bundled art, the existing bundled placeholder
  `assets/banners/games.jpg` is used; if even that is absent the message
  degrades to a text-only send so a narration event is never lost.
* Because the mapping is isolated, the real per-beat artwork library can be
  swapped in later without touching the message flow.

## 8. Tests

Command used (SQLite test database, isolated from production):

```
rm -f /tmp/sibcom_sib_test.db && PYTHONPATH=/home/user \
  DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
  sib_TEST_DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
  .venv/bin/python -m pytest -q
```

| Run | Result |
|---|---|
| Baseline before Phase 2A | 661 passed, 8 skipped, 3 failed (pre-existing `test_character_art.py` image-frame checks) |
| `tests/test_apple_battle.py` | 30 passed |
| `tests/test_apple_battle_narration.py` (new) | 14 passed |
| Full suite after Phase 2A | **675 passed, 8 skipped, 3 failed (same 3 pre-existing art failures)** |

New focused coverage (`tests/test_apple_battle_narration.py`, all mocked —
no real Telegram API calls):

1. narration is sent as separate photo messages (never appended into one
   edited message; the only edits are the two final reports);
2. narration message ids are persisted one per beat+chat (unique, ordered);
3. cleanup deletes every recorded narration message;
4. already-deleted messages are treated as clean;
5. cleanup failure does not break finalization or the final report (settlement
   still applied, report still rendered, records kept for retry);
6. the cleanup helper itself never raises;
7. restart mid-narration resumes at the cursor, sends only unsent beats, keeps
   pre-restart message ids, produces no duplicates and no second worker;
8. concurrent narration workers never duplicate events;
9. crash during teardown → recovery finalizes and finishes the cleanup;
10. legacy state without narration records still resumes safely;
11. the final report appears after cleanup, unchanged, on the existing board
    message ids;
12. art resolution uses existing bundled files only (and the placeholder file
    exists);
13. missing art degrades to text-only narration without losing a beat;
14. recovery-finalized battles still drop their temporary messages.

The rewritten loop test in `tests/test_apple_battle.py` replaces the removed
"edits both messages" assertions with the new contract and keeps every
settlement assertion (seedling +5 apples, idempotent, winner unchanged).

## 9. Explicit confirmations

* **No new images** were generated in this stage; only existing repository
  assets (`assets/characters/*.jpg`, `assets/banners/games.jpg`) are
  referenced, and no external image generation is invoked anywhere.
* **The final report is unchanged**: same `AppleBattleEngine.render_for_group`
  output (all beats + `✅ این نبرد به پایان رسید.`), same winner photo, same
  `final_keyboard`, same board message ids. Only the live presentation path
  changed.
* **Battle mechanics untouched**: formula, seed, winner calculation, rewards,
  card upgrades, matchmaking, bot difficulty and other games were not
  modified; no Alembic migration was added.
* **Production was not touched**: no deploy, no migration, no production
  database access; tests ran against an isolated SQLite test database.
* **No remote push**: commits stay on the local development branch.

## 10. Known limitations / follow-ups

* If a Telegram delete fails permanently, the record stays pending; there is
  currently no periodic retry loop for already-finished battles (only the
  live, recovery and maintenance teardown paths). A bounded "recently
  finished battles" sweep can be added later if needed.
* Beat captions reuse the existing plan texts verbatim; Phase 2B (visual
  library) can enrich art per beat without touching the message flow.
