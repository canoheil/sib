# Apple Duel D1 — self-driving scene presentation

## Reconnaissance

- `AppleDuelPresentation.tick` is at `services/apple_duel_presentation.py:169`. It calls `claim_scene` once and sends at most one indexed scene per invocation; it is unchanged by D1.
- `AppleDuelPresentation.recover_and_publish` is at `services/apple_duel_presentation.py:338`; maintenance invokes it at `services/maintenance/service.py:445`.
- `MaintenanceService.INTERVAL_SECONDS` is **30 seconds** (`services/maintenance/service.py:39`). Before D1, each recovery pass advanced only one scene, so subsequent scenes could wait for the next 30-second sweep.
- Apple Battle’s background narration pattern is `AppleBattleService._narration_loop` at `services/apple_battle.py:1015` (background scheduling at lines 907–917; beat sleep at line 1033). Apple Battle was not changed.
- Acceptance sets `AppleDuel.next_scene_at = now` in `services/apple_duel.py:325`. `claim_scene` sets it to `now + settings.APPLE_DUEL_SCENE_SECONDS` at lines 330–360; the default is 4 seconds (`core/config.py:81`). The new player makes the next scene immediately eligible after a successful delivery, then applies its own 1.5-second presentation delay.
- `scene_text` defines six indexed scenes, **0–5** (`services/apple_duel_presentation.py:48–70`); `_photo` uses the same indices (`:140–163`). The last claim transitions the duel to `COMPLETED`.

## Before and after flow

**Before:** acceptance awaited one `tick`; maintenance also awaited one `tick` per recovery scan. With a 30-second sweep interval, each later scene could be delayed by up to 30 seconds (plus the durable 4-second eligibility deadline). For the six-scene presentation, five gaps meant an estimated worst-case delay of roughly 150 seconds after the first scene, depending on maintenance timing.

**After:** acceptance and recovery schedule a retained background task. `play` serializes per duel, calls the unchanged single-scene `tick`, checks persisted state, makes the next scene immediately eligible after successful progress, then sleeps **1.5 seconds**. It stops when there is no progress, no pending scene, or a terminal state is reached. The database cursor and deadline remain the restart-recovery mechanism.

## Player and task lifecycle

- `DUEL_SCENE_DELAY_SECONDS = 1.5` is declared in `services/apple_duel_presentation.py`.
- `AppleDuelPresentation.play(bot, duel_id)` takes a module-level `asyncio.Lock` keyed by duel ID. It ticks once per iteration, rereads the duel, exits after a failed/no-progress tick or terminal/no-scenes state, and sleeps between successful scenes. Unexpected exceptions are logged at WARNING; normal exits are logged at INFO.
- `schedule_play` stores each `asyncio.Task` in `_play_tasks`, deduplicates an already-active task for the duel, and removes the reference in a done callback. This strong reference prevents background tasks from being garbage-collected while active.
- If a delivery fails, the existing `tick`/`release_scene` path rewinds the cursor and keeps the retry eligible later; `play` exits. `recover_and_publish` remains in maintenance and schedules a fresh player for due scenes or unpresented terminal boards.

## Timing and tests

- A three-scene presentation has two inter-scene waits: **3.0 seconds** of configured delay, excluding Telegram and database I/O.
- Five sent scenes have four waits: **6.0 seconds**. The full existing six-scene flow has five waits: **7.5 seconds**, excluding transport/database overhead. The delay assertions use mocked sleeps and verify the configured wait sequence; they do not claim a wall-clock Telegram latency guarantee.
- Tests were run with the duel suite (`tests/test_apple_duel.py` plus `tests/apple_duel/test_presentation_play.py`) against isolated SQLite: **71 passed, 3 skipped**. The skips are row-lock-specific concurrency cases (SQLite does not implement `SELECT ... FOR UPDATE`). Added coverage includes ordered delivery, terminal stop, per-duel serialization, failed-send release/exit, acceptance scheduling, recovery scheduling, task retention/deduplication, and the five-scene timing budget.

## Limitations and scope

- The lock is process-local; it does not serialize player loops across multiple application workers. Durable `claim_scene` remains the database-level claim guard.
- Claims are committed before Telegram I/O. Transport exceptions are explicitly released, but a hard process crash after a claim and before a successful send can leave that claimed scene skipped; the cursor design is at-most-once rather than exactly-once delivery.
- Telegram/API and database latency are outside the 1.5-second sleep budget, so wall-clock duration may exceed the delay-only estimates.
- No engine, damage, reward, narrative string, Apple Battle, invasion-v2, migration, or `_archive_inv1_*` changes were made. The `invasion_enabled` setting was not modified; its live database value was not queried.
