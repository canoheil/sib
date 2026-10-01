# PLAN — Friendly Party

1. **Inspection**: skills (`tasks`, `persian-writing`, telegram), architecture audit, baseline suite
   (560 passed, 8 skipped on SQLite).
2. **Data model + migration `023_friendly_parties`**: `MatchType`, `Party.match_type`,
   `PartyMember.team`, `PartyMember.bot_profile`, `Match.match_type`, `Match.winner_team`.
3. **Config**: `friendly_reward_max_matches_per_day=5`, `friendly_reward_apples_per_match=10`,
   `friendly_reward_daily_apple_cap=50` in `CONFIG_DEFAULTS` (no code change to rebalance).
4. **Party/team service** (`services/party/service.py`): `create_friendly_party`,
   friendly-aware `join_party(team=…)` with per-team caps, `enable_friendly` (solo conversion),
   `fill_empty_slots_with_bots` (host only; `BotService.create_party_bots`).
5. **Friendly service** (`services/friendly.py`): `FriendlyMatchService.start_friendly_match`
   (atomic, full party, IN_MATCH, ACTIVO match + players + state + bot turns) and
   `rematch`, plus `FriendlyRewardService.award_match_rewards` (caps + ledger idempotency) and
   `render_friendly_board`/audience helpers for the single shared group board.
6. **Match result integration** (`services/match_result.py`): friendly branch = apples, no group
   stats, no seedlings, no competitive counters; record `Match.winner_team`.
7. **UI**: `select_mode:{code}:{mode}` match-type screen (Find Opponent / Play With Group Members),
   friendly party keyboard + renderer, friendly handlers router (`handlers/friendly.py`), rematch
   button on the final friendly board, navigation consistency (Back never cancels).
8. **Render wiring**: friendly audience + neutral final outcome in `handlers/games/common.py` and
   `services/game_sync.py`; Refresh stays available.
9. **Tests** (`tests/test_friendly_party.py`): creation/join/team limits, match type, 2v2/4v4,
   8-player party, bot filling, start/duplicate start races, apples + daily caps + duplicate
   prevention + no-rank assertions, rematch, navigation.
10. **Verification**: focused tests, then full suite; report honestly (row-lock tests skip on SQLite).

## Result (2026-10-01)

All steps completed. Focused `tests/test_friendly_party.py`: **35 passed**.
Full suite: **595 passed, 8 skipped in 72.34s** (skips = `requires_row_locks`, MySQL-only).
`alembic heads` → single head `023_friendly_parties`.

Main files: `services/friendly.py` (new), `services/party/service.py`, `services/match_result.py`,
`services/game_sync.py`, `services/bot.py`, `services/panel.py`, `services/config_store.py`,
`matchmaking/matcher.py`, `handlers/friendly.py` (new), `handlers/games/common.py`,
`handlers/party/{handlers,renderer}.py`, `keyboards/party.py`, `database/models/{match,party}.py`,
`database/migrations/versions/023_friendly_parties.py`, `core/logging.py`, `tests/test_friendly_party.py`.

