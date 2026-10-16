# FINAL REPORT — Seventeenth card «طیفان» (`tifun`) added to Siib

Branch: `arena/eb59ddc1-sib` · Baseline commit: `d4508b7` · Data-only delivery:
**no Python code, no schema, no migration.** The artwork (`assets/characters/tifun.jpg`)
was already materialized in an earlier step straight from `origin/main` (blob
`36fac074b56129ffb2c13416ddd28e63732d36c9`, byte-identical).

Test engine in this sandbox: SQLite (`sib_TEST_DATABASE_URL=sqlite+aiosqlite:///…`).

## 1. Supplied content (stored verbatim — no trim, no normalization, no spellcheck)

| Field | Value |
| --- | --- |
| `code` | `tifun` (== artwork filename stem; `services/character_art.artwork_path` builds `assets/characters/{code}.jpg`) |
| `name_fa` | طیفان |
| `name_en` | Tifun (matches the card's own artwork plate «TIFUN», same convention as Mahmoud) |
| `affiliations_fa` | فرزانگان🔮 |
| `lore_fa` | نگهبان فانوس دریایی و فرمانروای طوفان‌ها؛ پیرمردی که باد و رعد را به فرمان خود درمی‌آورد. می‌گویند نور فانوسش رازی باستانی را پنهان کرده که با خاموشی آن، خشم دریا آزاد خواهد شد. |
| power / speed / intelligence | 15 / 15 / 30 (**sum 60** = `CARD_BASE_ATTRIBUTE_BUDGET`) |
| faction / `sync_faction` | `sages` / `true` |
| artwork | `null` → bundled original art, never an `/setasset` override |
| release_status / shop_enabled | `RELEASED` / `true` |

Exact code points verified: «طیفان» = U+0637 U+06CC U+0641 U+0627 U+0646; the orb is
U+1F52E (the same symbol the `sages` faction row and the other sages cards use) and there
is **no** space before it — exactly like `green_knight` / `sage_king` / `treyni` /
`sib_sabz` / `sib_talayi`; the story keeps its three ZWNJ half-spaces («طوفان‌ها»,
«درمی‌آورد»، «می‌گویند»). No visual spacing fix was needed for this card.

**Not invented** (absent from the seed entry, as for the other supplied cards):
`faction_role_fa`, `passive_key`, `passive_data`, `hidden_modifiers`, `strengths`,
`weaknesses`, `price`.

## 2. Files changed

| File | Change |
| --- | --- |
| `data/characters_seed.json` | **+15 lines / −0**: one appended entry; the 16 existing definitions are byte-identical (re-parsed and compared entry-by-entry) |
| `tests/test_card_lore_tifun.py` | **new** — 4 tests (5 items), generated from the stored seed strings so the module can never drift from the data |
| `tests/test_card_roster.py` | `tifun` joined `NEW_CARDS` + `OFFICIAL_NEW_TEXT` (shared roster / art / budget / shop-listing / 300-Apple purchase coverage), roster count 16→17, docstring |
| `tests/test_card_lore.py` | roster count 16 → 17 |
| `TODO/DECISIONS.md` | new decision section «۱۹» |

No migration: head stays `033_shop_unified_card_price`. No service, handler, keyboard,
model or economy file was touched.

## 3. Fresh-DB verification (real production code paths)

```
cards: 17 | created this run: 17
id: 17 | code: tifun | name: طیفان / Tifun
stats: {power: 15, speed: 15, intelligence: 30} sum: 60 | status: RELEASED | shop: True
artwork col: None | resolved art: …/assets/characters/tifun.jpg
faction: sages | role: None | passive: None | strengths: []
second startup created: 0 | total still: 17
```

Wiki screen (identical body on the shop detail screen, plus the price line):

```
*طیفان*

*وابستگی‌ها*:فرزانگان🔮

💪 قدرت: 15
⚡ سرعت: 15
🧠 هوش: 30

*داستان*:نگهبان فانوس دریایی و فرمانروای طوفان‌ها؛ …
```

Wiki index — the card joins its faction group in insertion order (6th of 6 sages):
`**— 🔮 فرزانگان —**` … `6. 🍎 *طیفان* — 💪15 ⚡15 🧠30`.

## 4. What the new card automatically gets (unchanged code paths)

| Surface | Behaviour |
| --- | --- |
| Shop | next weekly rotation offers it at the unified 300 Apples; 17 cards → 4 pages (last page 2 cards); historical rotations stay immutable |
| Wiki | sages group, full story, no price, no role/passive lines |
| My Cards / card detail | derived attributes (60 at L1 → 90 after 1→2), own `tifun.jpg` via `FSInputFile`, no story on this screen |
| Card upgrade | shared `card_upgrade_costs` table (100 Apples + 3 golden for 1→2), +10 per attribute per level, 330 total at L10 |
| Daily chest card jackpot | eligible automatically (active + RELEASED + shop_enabled + not owned) |
| Apple battle / duel / bot loadouts / invasion v2 | usable as Main Card; art resolves to the bundled 912×1136 JPEG |

Deliberately **not** changed: faction shop chests (`sages_chest` weight table is pinned by
tests and totals exactly 100 — adding Tifun there is a separate balance decision).

## 5. Tests run

```sh
# focused
tests/test_card_lore_tifun.py tests/test_card_lore_mahmud.py tests/test_card_roster.py tests/test_card_lore.py
# → 57 passed
# card / economy / faction group (incl. test_character_art.py)
# → 153 passed, 2 skipped, 4 failed (the 4 known baseline art-dimension failures)
```

Full-suite comparison against a pristine copy of `d4508b7` with the identical command is
reported in the turn summary (baseline 22 failed / 2507 passed / 25 skipped).

## 6. Open questions / risks

1. **`name_en`** «Tifun» follows the artwork plate and the `tifun.jpg` filename; the Persian
   name is «طیفان». A different transliteration (e.g. «Teyfan») is a one-field data edit.
2. **The story hints at a mechanic** («فرمانروای طوفان‌ها», the lighthouse secret) but no
   `passive_key` was created: `IMPLEMENTED_PASSIVES` in `services/characters.py` is empty, so a
   passive would render nowhere and fight nothing. A real storm mechanic is a battle-engine +
   balance decision.
3. **MariaDB not exercised here** (SQLite only) — the same limitation documented for the
   previous cards; seed/read/render paths verified here run identically.
4. **Art used as delivered** — not re-framed, consistent with the other supplied cards
   (912×1136 JPEG like `filo`/`mahmud`/`wizard`/`moon_lily`).
