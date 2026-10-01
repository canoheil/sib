# STEP-4 — Friendly service: match start, rewards, render helpers

Status: COMPLETED

## Sub tasks

1. [x] `FriendlyMatchService.start_friendly_match`: full party + member check, IN_MATCH under row lock, ACTIVE friendly Match (`group1_id == group2_id == group`, both sides accepted, `party1_id`), MatchPlayers from party teams, engine state + `apply_friendly_state`, opening `run_bot_turns`, deadline; a bot-only finish applies the result immediately (never leaves an ACTIVE finished match)
2. [x] `FriendlyMatchService.rematch`: new FRIENDLY party with the old lineup (busy humans skipped, bots fill), always starts friendly
3. [x] `FriendlyRewardService`: `usage_today`, `reward_for_player`, `award_match_rewards` — configurable 5/day, 10/match, 50/day cap, humans only, ledger key `friendly_reward:{match_id}:{user_id}`
4. [x] render helpers: `friendly_audiences`, `apply_friendly_state`, `viewer_audience`, `shared_board_viewer`, `state_viewer_id`, `friendly_result_line`, `shape_friendly_final_text`, `FriendlyOutcomeView`

## NOTES

- Team 2 audience is the synthetic positive marker `abs(group_id)*1000 + 2`; DB `group_id`s stay
  real (humans) / None (bots) for FK safety.
- Reward values are `system_config` tunables; tests override them via `ConfigService.set`.
