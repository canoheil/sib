# STEP 5 — Rarity de-classification

Status: DONE (`tests/test_characters.py` rewritten; focused 8 suites 71 passed; full regression green).

- `Rarity` class/exports removed; validation/labels/rendering/sorting/admin
  usage removed (`services/characters.py`, `services/wiki.py`,
  `services/player_cards.py`, `handlers/wiki.py`, `handlers/admin_meta.py`,
  `database/connection.py`, `data/characters_seed.json`).
- `characters.rarity` DB column KEPT as deprecated (default "COMMON"; still in
  migration 012) — no destructive migration, historical values survive
  (`test_rarity_is_deprecated_but_data_is_preserved`), and it is out of
  `snapshot()` so it cannot influence gameplay.
