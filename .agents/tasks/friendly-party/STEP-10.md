# STEP-10 — Shared friendly card: join buttons on the first render (bug fix)

Status: COMPLETED

## Sub tasks

1. [x] Reproduce: `_show_friendly_party` rendered the shared card with `is_member=True`
   (creator-tailored), and `get_friendly_party_keyboard` only emits `join_party:{id}:1|2` for
   NON-members — so the initial party message had no Team join buttons until somebody pressed
   Refresh (`view_party:`, which re-derives membership per presser)
2. [x] `keyboards/party.py`: `get_shared_friendly_party_keyboard(party)` renders the friendly card
   for the GROUP — team joins for every team with a free slot (`friendly_free_team_slots` /
   `friendly_join_row`), leave, host bot filling, start when full, cancel, refresh, back
3. [x] `get_party_action_keyboard` delegates friendly parties to the shared card, so every handler
   render path (create, `view_party`, `join_party`, `leave_party`, `make_friendly`,
   `friendly_fill_bots`) uses it; `handlers/friendly.py::_show_friendly_party` calls it directly
4. [x] Regression tests in `tests/test_friendly_party.py` (3 new): first render carries the join
   buttons without any Refresh + creator keeps their controls; a full team's join button is gone
   (full party -> Start); non-host bot fill still refused (no bots, friendly alert)

## NOTES

- Root cause: a friendly party card is ONE shared group message, so Telegram shows every viewer
  the same keyboard. The initial render was built from the creator's membership; the refresh
  render is built from the presser's membership — hence the difference.
- Fix: friendly cards are rendered viewer-independently. Per-team join buttons; a full team's
  button is not rendered (`PartyFullException` still guards the callback). Visibility is not
  authorization: `friendly_fill_bots:`/`friendly_start:` stay owner-scoped in the panel guard and
  `PartyService.fill_empty_slots_with_bots` / `enable_friendly` re-check the host server-side.
- The legacy per-viewer `get_friendly_party_keyboard` is kept (its contract is pinned by existing
  tests); handlers no longer render it for the shared card. Non-friendly party cards are
  untouched (delegation only triggers for `party.is_friendly`).
- Verification: `tests/test_friendly_party.py` 38 passed (was 35). Full suite: **612 passed,
  8 skipped** (SQLite; baseline before this fix: 609 passed, 8 skipped — the +3 are the new
  regression tests; the 8 skips are the MySQL-only `requires_row_locks` races).
- The pre-fix run of a temporary probe failed the new assertion; after the fix it passes.
