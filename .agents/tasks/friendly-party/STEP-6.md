# STEP-6 — UI: match-type screen, friendly keyboards/handlers, rematch

Status: COMPLETED

## Sub tasks

1. [x] mode list now leads to `select_mode:{code}:{mode}:{back}` (Game → Mode → Match Type)
2. [x] `get_match_type_keyboard`: «پیدا کردن حریف» + «بازی با اعضای گروه (رفیقانه)», Back to the mode list
3. [x] `get_friendly_party_keyboard`: team joins `join_party:{id}:{team}`, leave, host bot-fill, start, cancel, refresh, Back to Active Games (IN_MATCH → refresh+back only)
4. [x] solo normal party card: «بازی با اعضای گروه (رفیقانه)» → `make_friendly:{id}`
5. [x] `handlers/friendly.py` router: `create_friendly_party:`, `make_friendly:`, `friendly_fill_bots:`, `friendly_start:`, `friendly_rematch:` + shared-board renderer
6. [x] `render_friendly_party_card` (two team rosters, 🤖 bots, friendly note, no-rank/apple note)
7. [x] `friendly_rematch:{id}` button on finished friendly boards (`get_friendly_rematch_keyboard`, `get_rematch_keyboard(match=…)`)
8. [x] panel scoping: `select_mode:`, `create_friendly_party:` added to `OWNER_SCOPED_CALLBACKS` (joins/start remain public, enforced server-side)

## NOTES

- Friendly party keyboard is reached from the party card too (`get_party_action_keyboard`
  delegates when `party.is_friendly`), so the Active Games list shows friendly parties correctly.
- Back is never wired to cancel; `cancel_party:` stays an explicit red button.
