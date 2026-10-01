# Adapt shared-board engines and gameplay authorization

Status: COMPLETED

## Sub tasks

1. [ ] Require live MatchPlayer membership and exact Telegram group context for game actions and refreshes, before stale-board recovery or state rendering.
2. [ ] Require Competitive acceptance/rejection callbacks to originate in the actor's persisted group; keep participant-based public acceptance.
3. [ ] Make all supported engines render a correct same-group Friendly view based on player/team identity, without changing Competitive opponent privacy.
4. [ ] Make shared-board target selection/synchronization deduplicate Friendly's one group and show both Battleship radar histories; preserve live/replacement message IDs.
5. [x] Add regressions for solo simultaneous actions, team mode views, Battleship shared view, copied/wrong-group/nonparticipant callbacks, stale refresh, and legitimate participants; run focused gameplay tests.

Focused STEP-4 verification passed **100 tests, 1 skipped**:
`PYTHONPATH=/home/user DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test_friendly_step4_final.db sib_TEST_DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test_friendly_step4_final.db .venv/bin/python -m pytest -q tests/test_friendly_engine_views.py tests/test_friendly_parties.py tests/test_rps_handlers.py tests/test_game_handlers.py tests/test_gameplay_sync.py tests/test_battleship_reliability.py tests/test_battleship.py tests/test_connect_four.py tests/test_tic_tac_toe.py tests/test_matchmaking.py tests/test_matchmaking_handlers.py tests/test_stale_and_duplicate_callbacks.py tests/test_party_handlers.py tests/test_navigation.py`

The skip is a row-lock concurrency test unavailable on SQLite. MariaDB behavior remains unverified.

## NOTES

- Friendly matches store `group1_id=party.group_id`, `group2_id=NULL`, and every MatchPlayer's `group_id=party.group_id`; `team=1|2` is the side identity. Competitive rendering must continue selecting each group's opposing team privately.
- Engine renderers that currently pick the first player for `for_group_id` or find an opponent by `group_id != for_group_id` will not work for same-group Friendly matches. Use the viewer's MatchPlayer/team from `for_player_id` and render team/group rosters only in Friendly mode.
- RPS and High Card have simultaneous per-player choices. The shared keyboard must let unlocked participants submit while server-side action validation still ties each choice to the pressing player. Do not display a participant's hidden choice before round resolution.
- Battleship's private radars are indexed by `group_id`; same-group Friendly needs a safe participant/team-specific radar key or a combined public board that does not disclose hidden fleet positions.
- `handlers/games/common.py` currently validates the engine state after loading a match but must not perform stale rendering before checking both player membership and callback chat. Match refresh currently only checks participant identity, not chat equality.
- `AcceptanceService` knows the persisted MatchPlayer; the handler can pass callback `group_id` as an optional expected context so existing service callers/tests stay compatible while production handlers enforce group identity.
