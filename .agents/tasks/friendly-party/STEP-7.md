# STEP-7 — Render wiring (single shared board, refresh, neutral result)

Status: COMPLETED

## Sub tasks

1. [x] `handlers/games/common.py`: friendly branch in `process_game_action` presents ONE board (group1 == group2) from the current player's team perspective
2. [x] `render_board_for` + `handle_game_refresh` friendly-aware (per-team audience, refresh button, final text, rematch keyboard; group members may refresh the shared board)
3. [x] `services/game_sync.py::sync_match_messages` friendly-aware (timeout/recovery re-render + rematch entry, single edit)
4. [x] final boards strip side-relative lines and append `🏆 برندهٔ بازی رفیقانه: …` / `🤝 نتیجه: مساوی!`
5. [x] `_persist_board_message_id` keeps both stored ids on the single live message

## NOTES

- The normal path is untouched (regression caught by `test_monetization` during development and
  fixed: the friendly outcome view is only used in the friendly branch).
- Friendly game actions edit exactly one message — asserted in tests.
