# STEP-3 — Party/team service: friendly create/join/convert + bot filling

Status: COMPLETED

## Sub tasks

1. [x] `PartyService.create_friendly_party` (max_players = 2 x team size, creator on team 1, OPEN)
2. [x] friendly-aware `join_party(..., team=None)`: explicit team honoured, auto-balancing (ties → team 1), per-team cap under the existing party row lock
3. [x] `enable_friendly(party_id, user_id)`: creator-only, solo/untouched only, reuses the SAME row (one-active-session stays intact)
4. [x] `fill_empty_slots_with_bots(party_id, user_id)`: host-only, friendly-only, fills team 1 then team 2 via `BotService.create_party_bots` (game-appropriate 🤖 names, `bot_profile`, `is_bot=True`), marks FULL, logs `PARTY_BOTS_FILLED`
5. [x] `MATCHMAKING` guard: `enqueue_party` rejects friendly parties; the opponent query filters `Party.match_type == NORMAL`

## NOTES

- Bots are real `User(is_bot=True)` rows + `PartyMember(team=…, bot_profile=…)`; no per-game code.
- `join_party` on a party the user already belongs to raises `ActiveSessionExistsException`
  (the one-active-session check runs before the duplicate-member check) — tests assert this.
