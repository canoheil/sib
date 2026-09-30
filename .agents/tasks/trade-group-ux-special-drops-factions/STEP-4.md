# STEP 4 — Ghost active game

Status: DONE (root-caused; `tests/test_ghost_active_game.py` 20 tests; full suite 433P/6S/2F at the time).

## Root cause

Validation counted parties in OPEN/FULL/IN_QUEUE/IN_MATCH plus active
match players, while the Active Games UI listed only current-group OPEN/FULL
parties. IN_QUEUE/IN_MATCH records were therefore invisible but blocking, and
nothing ever reconciled records that could no longer be live (zombie IN_MATCH,
queue row removed, matches without a deadline) → "an active game exists" with
an empty Active Games screen.

## Fix

- `services/session.py` is the single source of truth: live predicates,
  per-group live listing, `lock_user`, delegating `has_active_session` /
  `get_active_session_info`, `reconcile_stale_state()` (idempotent, 600s grace,
  restores parties instead of dropping them).
- Party/recovery/maintenance services call the reconciliation;
  keyboards/handlers expose every live status (searching / in match) in the UI.
- Regression tests: active blocks second game, completed does not, cancelled
  does not, stale session no false positive, UI/validation agree, recovery and
  cleanup, plus concurrency tests (`@requires_row_locks`).
