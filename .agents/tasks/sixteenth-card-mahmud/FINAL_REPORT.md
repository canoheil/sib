# FINAL REPORT — Sixteenth card «محمود» (`mahmud`) added to Siib

Branch: `arena/eb59ddc1-sib` · Baseline commit: `d4508b7` · Data-only delivery: **no Python code, no schema, no migration**.

Test engine in this sandbox: SQLite (`sib_TEST_DATABASE_URL=sqlite+aiosqlite:///…/sibcom_sib_test_*.db`),
same command on a pristine copy of `d4508b7` for the baseline.

Baseline (pristine `d4508b7`, fresh DB): **22 failed / 2507 passed / 25 skipped** (2554 collected)
Final (this work, fresh DB): **22 failed / 2514 passed / 25 skipped** (2561 collected)

The 22 failures are byte-for-byte the same list on both sides (image-dimension/asset checks,
narration timing, SQLite row-lock). Collection diff: **+7 items, 0 removed** — 5 in the new
`tests/test_card_lore_mahmud.py`, 1 new `[mahmud]` parametrisation each in `test_card_progression.py`
and `test_card_roster.py`, plus 2 test renames (`…eleven…` → `…new…`, same assertions). None of the
new/renamed items fails.

---

## 1. Supplied content (stored verbatim — no trim, no normalization, no spellcheck)

| Field | Value |
| --- | --- |
| `code` | `mahmud` (== artwork filename stem; `services/character_art.artwork_path` builds `assets/characters/{code}.jpg`) |
| `name_fa` | محمود |
| `name_en` | Mahmoud (matches the card's own artwork plate «MAHMOUD») |
| `affiliations_fa` | امپراتوری 👑 - تیغ‌های تاج 🗡 |
| `lore_fa` | محمود را بسیاری به‌خاطر ظاهر عجیب و نگاه اخموش دست‌کم می‌گیرند؛ غافل از اینکه پشت آن چهرهٔ بامزه، ذهنی تیز و اراده‌ای سرسخت پنهان شده است. او ثابت کرده که حتی غیرمنتظره‌ترین جنگجویان هم می‌توانند در میان نخبگان امپراتوری جای بگیرند. |
| power / speed / intelligence | 18 / 17 / 25 (**sum 60** = `CARD_BASE_ATTRIBUTE_BUDGET`) |
| faction / `sync_faction` | `empire` / `true` (wiki grouping; affiliations text stays the only source of the «تیغ‌های تاج» label) |
| artwork | `null` → bundled original art, never an `/setasset` override |
| release_status / shop_enabled | `RELEASED` / `true` |

Exact code points verified: crown `U+1F451`, dagger `U+1F5E1` (no variation selector, matching the
existing empire cards), ZWNJ `U+200C` in «تیغ‌های» and 5× in the story, `U+0654` in «چهرهٔ» — the same
carriers the rest of the roster uses.

**Not invented** (absent from the seed entry, as for the other 11 supplied cards): `faction_role_fa`,
`passive_key`, `passive_data`, `hidden_modifiers`, `strengths`, `weaknesses`, `price`.

## 2. Files changed

| File | Change |
| --- | --- |
| `data/characters_seed.json` | **+15 lines / −0**: one appended entry; the 15 existing definitions are byte-identical (asserted in the generation script by re-parsing both revisions) |
| `tests/test_card_lore_mahmud.py` | **new** — 4 tests (5 items) |
| `tests/test_card_roster.py` | `mahmud` joined `NEW_CARDS` + `OFFICIAL_NEW_TEXT` (so the shared roster, art, budget, shop-listing and per-card 300-Apple purchase tests cover it), counts 15→16, shop pagination 3→4 pages, last-page row count, two test renames, comments/docstrings |
| `tests/test_card_lore.py` | roster count 15 → 16 |
| `tests/test_card_progression.py` | comment only (the parametrised L1→L10 test now also runs for `mahmud`) |
| `TODO/DECISIONS.md` | new decision section «۱۸» recording this delivery and its scope |

No migration: head stays `033_shop_unified_card_price`. No service, handler, keyboard, model or
economy file was touched — the card is picked up by the existing data-driven paths.

## 3. What the new card automatically gets (unchanged code paths)

| Surface | Behaviour |
| --- | --- |
| Seed / startup | `seed_characters` creates the missing row (`id 16` on a fresh install); reruns create 0; a mistyped entry still fails loudly |
| Shop | next weekly rotation offers it at the unified 300 Apples (`ShopService.card_price()`), 5 per page → 4 pages now; historical open rotations stay immutable |
| Wiki | listed in the empire group (ordered by faction display order, then id), full story, no price, no role/passive lines |
| My Cards / card detail | derived attributes from level (60 → 90 after 1→2), the card's own `mahmud.jpg` through `FSInputFile`, no story on this screen |
| Card upgrade | costs come from the shared `card_upgrade_costs` table (100 + 3 golden for 1→2); `+10` per attribute per level up to L10 (330 total) |
| Daily chest card jackpot | eligible automatically (active + RELEASED + shop_enabled + not owned) |
| Apple battle / duel / bot loadouts / invasion v2 | any owned card can be the Main Card; the art resolves to the bundled 912×1136 JPEG |

Deliberately **not** changed: the faction shop chests (`data/shop_chests/additional_chests.json`).
`empire_chest` has a weight table that already totals exactly 100 and is pinned by tests; adding
Mahmoud there is a separate balance/economy decision.

## 4. Verification commands

```sh
# focused (this work)
PYTHONPATH=/home/user sib_TEST_DATABASE_URL=sqlite+aiosqlite:////home/user/sib/tmp/sibcom_sib_test_run2.db \
  .venv/bin/python -m pytest -q tests/test_card_lore_mahmud.py tests/test_card_roster.py tests/test_card_lore.py
# → 51 passed

# card/economy group
… tests/test_card_progression.py tests/test_card_upgrade.py tests/test_card_upgrade_ui.py \
  tests/test_card_upgrade_migration.py tests/test_characters.py tests/test_factions.py \
  tests/test_wiki.py tests/test_shop.py tests/test_shop_card_price.py tests/test_shop_card_price_migration_033.py
# → 145 passed, 2 skipped

# full suite, both trees with the same command
#   baseline 22 failed / 2507 passed / 25 skipped  (2554 collected)
#   final    22 failed / 2514 passed / 25 skipped  (2561 collected)  — identical failure names
```

## 5. Open questions / risks

1. **Affiliation spacing.** The supplied string uses spaces around the emoji and a ZWNJ in
   «تیغ‌های», while the existing `sib_sorkh`/`igris` entries hold `امپراتوری👑 - تیغ های تاج🗡`.
   The supplied text was stored *as delivered* (per `docs/card-lore.md`: verbatim, no normalization).
   If the operator wants one identical string for the whole sub-group, that is a deliberate text edit
   to be requested — nothing in code compares the strings.
2. **`name_en`** «Mahmoud» follows the artwork plate; the code/filename stays `mahmud`.
3. **MariaDB not exercised here** (SQLite only): row-locking paths behave as in any other card; the
   seed/read/render paths verified here run identically.
4. **Art is used as delivered** — not re-framed by `scripts/frame_card.py` (which would re-crop it),
   consistent with the other 11 supplied cards; it is 912×1136 JPEG like `filo`/`wizard`/`moon_lily`.
5. **Faction chest** inclusion and any `passive_key`/role text remain future decisions that require
   their own balance review.
