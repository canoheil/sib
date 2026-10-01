# STEP-2 — Data model + migration 023 + config values

Status: COMPLETED

## Sub tasks

1. [x] `MatchType` (normal/friendly) in `database/models/match.py` + exported via `database/models/__init__.py`
2. [x] `Match.match_type`, `Match.winner_team`
3. [x] `Party.match_type` + `is_friendly`/`team_size` properties, `PartyMember.team`, `PartyMember.bot_profile`
4. [x] migration `database/migrations/versions/023_friendly_parties.py` (reversible, server defaults keep old rows = normal; `alembic heads` → single head `023_friendly_parties`)
5. [x] configurable reward rule in `CONFIG_DEFAULTS` (5 matches/day, 10 apples/match, 50 apples/day)

## NOTES

- Friendly party capacity = `2 x MODE_SLOTS[mode]`, so `Party.team_size == max_players // 2` for
  friendly parties and `== max_players` for normal ones.
- `winner_team` on Match is required for friendly results: `group1_id == group2_id` there.
- Migration was validated offline for MySQL (`alembic upgrade 022:023 --sql` → 5 valid `ALTER TABLE`s).
