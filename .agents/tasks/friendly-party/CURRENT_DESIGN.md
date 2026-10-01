# CURRENT_DESIGN — Friendly Party (match type, not a game mode)

Repo root IS the `sib` package. Run tests with:

```
PYTHONPATH=/home/user:/home/user/sib \
DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
sib_TEST_DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
.venv/bin/python -m pytest -q
```

(`sib.bots` lives in `sib/sib/bots`, so BOTH `/home/user` and `/home/user/sib` must be on
PYTHONPATH — exactly what `deploy/*.sh` set.)

## Existing flow (audited)

| Piece | Location |
|---|---|
| Party model | `database/models/party.py` — `Party(mode, max_players, current_players_count, status, creator_user_id)`, `PartyMember(party_id, user_id)`, `PartyStatus.OPEN/FULL/IN_QUEUE/IN_MATCH/CANCELLED` |
| Party service | `services/party/service.py` — `MODE_SLOTS = {solo:1, 2v2:2, 4v4:4}` (= slots per TEAM), `create_party` (returns FULL for solo, OPEN for team), `join_party` (user row lock + party `FOR UPDATE`, capacity + duplicate member checks), `leave_party`, `cancel_party`, `get_active_parties_for_group` |
| Party UI | `handlers/party/handlers.py` (`create_party:{code}:{mode}`, `view_party`, `join_party`, `leave_party`, `cancel_party`), `keyboards/party.py` (`get_game_mode_keyboard` → per-mode `create_party:{code}:{mode}`; `get_party_action_keyboard`), `handlers/party/renderer.py` |
| Matchmaking | `matchmaking/matcher.py` `enqueue_party` + `find_and_create_match` (cross-group, same game/mode/party size), `services/bot_match.py` `create_fallback_from_queue` (bot squad on team 2, `is_bot_match=True`) |
| Match model | `database/models/match.py` — `Match(game_id, mode, state, group1_id, group2_id, party1_id, party2_id, group1_message_id, group2_message_id, winner_group_id, is_draw, game_state, ..., is_bot_match, result_applied)` |
| Match result | `services/match_result.py` `apply_match_result` (idempotent via `result_applied`; group wins only for non-bot matches; seedlings only for non-bot wins; `_record_player_results` writes `PlayerStats` + `MatchPlayer.result`) |
| Rewards | `services/seedling.py` (one 🌱 per eligible PvP win, unique `(match_id,user_id)`), `services/apple_wallet.py` (`earn/spend` + ledger with unique `idempotency_key`), `services/config_store.py` `CONFIG_DEFAULTS` (data-driven tunables) |
| Gameplay | `handlers/games/common.py` `process_game_action` (authoritative state, `run_bot_turns` when `match.is_bot_match`), `services/game_sync.py` (out-of-band re-render), engines in `games/` (`create_initial_state`, `render_for_group(state, for_group_id, for_player_id)`, `get_keyboard`) |
| Bots | `services/bot.py` `BotService.create_match_bots` (real `User(is_bot=True)` identities, id namespace `BOT_USER_ID - match_id*8 - ordinal`, JSON `bot_profile`), `sib/bots/` strategies + `run_bot_turns` |
| Session guard | `services/session.py` — ACTIVE party statuses / match states are the single definition of "live" + `reconcile_stale_state` |
| Nav | `services/nav.py` (`KNOWN_SCREENS`, back labels), `keyboards/common.py` (`primary/success/danger_button`, `nav_back_row`, `nav_refresh_row`), `services/panel.py` `OWNER_SCOPED_CALLBACKS` |

Engine render note: engines resolve "my side" from the state players' `group_id`
(`p[...]["group_id"] == for_group_id`, and solo engines take the first player whose
`group_id != for_group_id` as the opponent). A friendly match is played INSIDE one
Telegram group, so both teams share the real group id — the state therefore carries a
per-team **audience id** (team 1 = real group id, team 2 = synthetic negative marker).
DB `group_id`s stay real/None (FK-safe); only the serialized state is rewritten.

## Design (decided)

- **Match type is a party/match property, never a game mode**: new `MatchType.NORMAL/FRIENDLY`
  on `Party.match_type` and `Match.match_type`. Hierarchy stays Game → Mode → Match Type.
- Friendly party capacity = `2 × MODE_SLOTS[mode]` (solo 2, 2v2 4, 4v4 8) with
  `PartyMember.team ∈ {1,2}` and a per-team slot cap of `MODE_SLOTS[mode]`.
- Bot filling belongs to the party/team system (`PartyService.fill_empty_slots_with_bots`) and
  reuses `User(is_bot=True)` + `bot_profile` (copied to `MatchPlayer` at match start).
- Friendly matches start immediately (no queue, no acceptance, both sides auto-accepted) and set
  `is_bot_match=True` only when bot members are actually present, `group1_id == group2_id == group`.
- Rewards: apples instead of seedlings — ≤5 matches/day/player, 10 apples/match, ≤50 apples/day,
  ledger-idempotent (`friendly:{match_id}:{user_id}`). No group wins, no seedlings, no
  competitive player counters, no Elo/MMR (none exists).
- Rematch: `friendly_rematch:{match_id}` recreates the same game/mode as a FRIENDLY match
  (same humans that are still free, bots fill the rest) — never a ranked match.
- Solo: the normal solo party card also offers «بازی با اعضای گروه», which converts the same
  party into a 2-slot friendly party (no second party → one-active-session safe).
