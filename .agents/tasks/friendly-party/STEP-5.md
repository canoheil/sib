# STEP-5 — Match result integration (no rank, apples)

Status: COMPLETED

## Sub tasks

1. [x] `apply_match_result` records `winner_team` (1/2/None) for every match
2. [x] friendly branch: pays `FriendlyRewardService.award_match_rewards`, logs `MATCH_FINISHED`
3. [x] `_record_player_results`: friendly matches keep the per-match result but write NO `PlayerStats`
4. [x] seedlings only for non-bot NORMAL wins; no `GroupStats`/leaderboard changes for friendly
5. [x] `_free_parties` cancels the friendly party when its match ends

## NOTES

- The `result_applied` guard + wallet idempotency key both prevent double rewards.
- Timeout/forfeit resolution (`services/timeout/service.py`) already sets `winner_team`, so
  friendly matches resolve correctly through the same path.
