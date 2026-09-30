# STEP 6 — Card factions (lore/metadata only)

Status: DONE (full suite 456P/6S/2F; factions suite 9 tests).

- `database/models/faction.py`: `Faction(code unique, name_fa/en, lore_fa,
  territory_fa, symbol, display_order, is_active)`; `Character.faction_id`
  (nullable FK + index) and `Character.faction_role_fa`; `faction` relationship.
- `data/factions_seed.json`: empire 👑, sages 🔮, moon_descendants 🌙
  («درّه ماه (Moonfall Valley)»).
- `services/factions.py`: list/get/for-character/seed (never overwrites)/
  backfill (NULL-only) — the only faction-aware service; no faction code
  branching in generic card logic.
- Migration `020_factions`: inspect-guarded DDL, insert-if-missing factions,
  NULL-only backfill of the 4 seeded characters; downgrade drops only what it
  added. Rarity column untouched.
- Surfaces: `render_character_card(..., faction=None)` (shop view),
  `WikiService` ordering by faction `display_order` (+ page faction block),
  wiki index grouped under faction headers, owned-card detail faction line.
- `database/connection.py::init_db` seeds factions on every boot and
  backfills NULLs for pre-existing installs.
- Tests: `tests/test_factions.py` (9) — canon data, backfill non-overwrite,
  Faction 4 as data-only, unaffiliated rendering, no gameplay coupling,
  no "Avarion" canon, card-detail lore.

## Deliberate non-goals

No bonuses/resistances/matchmaking/trade restrictions; no future cards
(King / Sage King / royal guard); no invasion/faction-war/territory mechanics;
Moon Descendants' goal left undefined; artwork untouched.
