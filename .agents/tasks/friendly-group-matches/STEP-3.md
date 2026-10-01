# Implement Friendly parties, team slots, bot fill, and direct match start

Status: COMPLETED

## Sub tasks

1. [ ] Extend `PartyService` with Competitive-compatible/Friendly-aware capacity, explicit team joins, group/status guards, creator-only bot filling, and atomic one-group match start.
2. [x] Add reusable Friendly bot identity/profile creation to existing bot infrastructure, with clear Persian bot names and stable unique IDs.
3. [ ] Add the mode → match-type selection screen and Friendly party card/actions (team occupancy, slots, join side, fill bots, start); retain legacy create callbacks as Competitive.
4. [ ] Wire handlers to validate Telegram group context and route Friendly starts directly, never through competitive queue.
5. [x] Add focused party/UI/start/bot tests; run them plus existing party/matchmaking regressions; record outcomes.

Focused verification passed **53 tests, 2 skipped**:
`PYTHONPATH=/home/user DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test_friendly_parties.db sib_TEST_DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test_friendly_parties.db .venv/bin/python -m pytest -q tests/test_friendly_parties.py tests/test_parties.py tests/test_party_handlers.py tests/test_navigation.py tests/test_matchmaking.py tests/test_matchmaking_handlers.py`

The new handler-level start/double-tap regression also passed separately: **12 passed** in `tests/test_friendly_parties.py`. SQLite skips the two row-lock-specific checks; MariaDB locking remains unverified.

## NOTES

- Competitive capacities remain 1/2/4. Friendly capacities are two teams of 1/2/4, total 2/4/8.
- Apple Battle remains behind its dedicated loadout flow; its Friendly card snapshot and precomputed-battle start are implemented in STEP-5.
- `MatchmakingService.enqueue_party` must reject Friendly even if a stale/manually crafted callback bypasses the UI.
- Direct Friendly start creates one ACTIVE Match (`group1_id=party.group_id`, `group2_id=None`) with every MatchPlayer's explicit team and one shared board message.
