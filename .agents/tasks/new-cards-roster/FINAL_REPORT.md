# FINAL REPORT — 11 New Cards (Filo … Zarush) integrated into Siib

Branch: `arena/204c0909-sib` · Baseline commit: `c7334a3` (uncommitted working tree; nothing committed or pushed)
Test engine available in this sandbox: SQLite (`sib_TEST_DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test.db`).

Baseline (pristine worktree of `c7334a3`, fresh DB): `6 failed / 1868 passed / 31 skipped`
Final (this work, fresh DB): **`6 failed / 1939 passed / 31 skipped`**

The 6 failures are the *same six* pre-existing, environment-specific ones on both sides
(4 asset-dependent `test_character_art` checks, the narration-restart timing check in
`test_apple_battle_scene_library`, and the SQLite-only row-lock check
`test_transaction_regressions::test_E_daily_chest_retry_keeps_the_callers_user_row`).
Collection diff: **+71 test items, 0 removed** (test_card_roster 35, test_shop_card_price 21,
test_shop_card_price_migration_033 4, 11 new parametrisations in test_card_progression) —
1976 collected now vs 1905 at baseline.

---

## 1. Files / components changed (Task 2 scope)

| File | Change |
| --- | --- |
| `data/characters_seed.json` | **+121 lines / −0**: the 11 new entries appended; the 4 original entries are byte-identical. New entries carry only `code`, `name_fa`, `name_en`, `artwork: null`, `power/speed/intelligence`, `release_status: "RELEASED"`, `shop_enabled: true` |
| `services/card_progression.py` | + `CARD_BASE_ATTRIBUTE_BUDGET = 60` (the permanent Level‑1 budget, lives with the other progression rules); docstring: the rules are code, not configuration |
| `services/characters.py` | `validate_base_budget` reads the constant instead of the `card_base_attribute_budget` config key (ConfigService import dropped); docstring explains the budget cannot be widened/tightened |
| `services/config_store.py` | the `card_base_attribute_budget` default was **removed** and replaced by an explanatory NOTE, so no config row can override the rule |
| `database/models/character.py` | docstring/comment corrected to the real rule (L1 sum 60; total `60 + 30*(level−1)`; 330 at L10) |
| `database/connection.py` | `seed_characters` rewritten as an **append-only, idempotent** sync + new `CharacterSeedError` (loud failure instead of a silent skip) |
| `database/__init__.py` | re-exports `seed_characters` / `CharacterSeedError` |
| `analysis/invasion_balance_simulation.py` | validation uses the constant; balance profiles restricted to the explicit `AUDITED_SEED_CODES` set so the 11 new cards cannot silently change audited profiles |
| `tests/test_card_roster.py` | **new**: 35 tests covering the whole delivery |
| `tests/test_card_progression.py` | pins the 4 audited cards as an explicit subset (`AUDITED_LEVEL_ONE`) + the new cards' spec totals |
| `tests/test_apple_battle.py` | `test_unimplemented_passives_are_not_shown_as_active` guarded for cards without a passive (the new cards) |

Unchanged on purpose: `services/character_art.py` (art already resolves `assets/characters/{code}.jpg`),
the battle engine, the progression formula, `services/shop.py` pricing, and all other economies.
No migration was added — Task 2 changes no schema (see §7).

## 2. Character IDs and image mapping

`code == artwork filename stem`, which is exactly how the art layer resolves bundled files
(`artwork_path → assets/characters/{code}.jpg`). `artwork` is stored as `NULL`, so nothing can
shadow the bundled original and no fallback can substitute another card's image.
IDs are DB-assigned (auto-increment); on a fresh install the seed produces them in this order
(verified against a freshly seeded DB, ids 5–15 are the new cards):

| id | code | name_fa | name_en | image (exact file) | px / format |
| --- | --- | --- | --- | --- | --- |
| 1–4 | `sib_sorkh`, `sib_sabz`, `sib_talayi`, `sib_mahi` | (unchanged) | (unchanged) | `assets/characters/sib_*.jpg` | unchanged |
| 5 | `filo` | فیلو | Filo | `assets/characters/filo.jpg` | 912×1136 JPEG |
| 6 | `goje` | گوجه | Goje | `assets/characters/goje.jpg` | 1227×1536 PNG |
| 7 | `green_knight` | شوالیه سبز | Green Knight | `assets/characters/green_knight.jpg` | 1121×1403 PNG |
| 8 | `igris` | ایگریس | Igris | `assets/characters/igris.jpg` | 1850×2304 PNG |
| 9 | `mmung` | ممونگ | Mmung | `assets/characters/mmung.jpg` | 1121×1403 PNG |
| 10 | `moon_knight` | شوالیه ماه | Moon Knight | `assets/characters/moon_knight.jpg` | 912×1136 JPEG |
| 11 | `moon_lily` | نیلوفر ماه | Moon Lily | `assets/characters/moon_lily.jpg` | 912×1136 JPEG |
| 12 | `sage_king` | پادشاه فرزانه | Sage King | `assets/characters/sage_king.jpg` | 1121×1403 PNG |
| 13 | `treyni` | ترینی | Treyni | `assets/characters/treyni.jpg` | 1121×1403 PNG |
| 14 | `wizard` | جادوگر | Wizard | `assets/characters/wizard.jpg` | 912×1136 JPEG |
| 15 | `zarush` | زاروش | Zarush | `assets/characters/zarush.jpg` | 1121×1403 PNG |

On an existing installation the ids are whatever the sequence hands out; `code` is the stable
identifier (it is what the seed checks and what art/wiki/shop callbacks resolve through).
The files are used as delivered: they were not re-framed (`scripts/frame_card.py` was deliberately
**not** run — it would re-crop them), not recoloured, not redrawn and not replaced. All 15 files are
git-tracked and shipped by the existing packaging (`Dockerfile` copies the whole tree).

## 3. Final base stats (Level‑1 definitions)

| code | power | speed | intelligence | sum |
| --- | --- | --- | --- | --- |
| filo | 7 | 13 | 40 | **60** |
| goje | 20 | 20 | 20 | **60** |
| green_knight | 27 | 19 | 14 | **60** |
| igris | 32 | 17 | 11 | **60** |
| mmung | 14 | 36 | 10 | **60** |
| moon_knight | 30 | 20 | 10 | **60** |
| moon_lily | 8 | 12 | 40 | **60** |
| sage_king | 20 | 12 | 28 | **60** |
| treyni | 12 | 24 | 24 | **60** |
| wizard | 24 | 10 | 26 | **60** |
| zarush | 22 | 21 | 17 | **60** |

The four existing cards keep exactly `22/20/18`, `16/18/26`, `20/24/16`, `14/16/30` — asserted in
tests and byte-identical in the seed file. Progression is untouched: `base + 10*(level−1)` per
attribute, max level 10, so L1 total 60 and L10 total 330 (`attributes_at_level` / `card_attributes`).

## 4. How the fixed 60 budget is enforced

- `CARD_BASE_ATTRIBUTE_BUDGET = 60` is a module constant in `services/card_progression.py`;
  `CharacterService.validate_base_budget` compares the sum against it and raises
  `CharacterValidationError` otherwise (Persian message). It no longer imports `ConfigService`.
- The config key `card_base_attribute_budget` was deleted from `CONFIG_DEFAULTS`, so a stale
  `system_config` row or a `ConfigService.set(...)` call cannot influence the rule. Tests assert
  60 is accepted while 120 and 30 are refused, and that `CharacterService.update` is validated too.
- Consumers of the old key were audited: `services/characters.py` (the only production reader),
  `analysis/invasion_balance_simulation.py` (now the constant), `tests/test_card_progression.py`
  (unaffected). `grep -rn "card_base_attribute_budget"` finds only the note/docstrings.
- Because the same validation runs for seed data, a mistyped stat block now fails loudly at
  startup (`CharacterSeedError`) instead of registering a card with a broken budget.

## 5. How shop pricing stays exactly 300

No pricing code was touched in Task 2 — the new cards are ordinary characters, so they inherit
Task 1's single rule: `SHOP_CARD_PRICE_APPLES = 300` (`database/models/shop.py`) read through the
only accessor `ShopService.card_price()`. `ShopService.purchase` takes no price/money argument and
the buy callback carries only `shop_buy:{entry_id}:{nonce}`; the price shown on list/detail screens,
the pre-check, the debit and the ledger row all come from the constant. `CharacterService.create`
has no `price`/`rarity` parameter (the legacy `characters.price` column is written with the unified
value) and `update()` refuses to write either, so no new card can carry an individual price.
Tests assert: every one of the 11 cards is purchasable for exactly 300 (parametrised, per card),
balance `1000 → 700`, ledger row `−300` with `ref_id == entry.id`, insufficient balance refused
with no write, double-purchase/nonce replay unchanged.

## 6. Surfaces updated / verified

| Surface | How it works for the new cards | Verified by |
| --- | --- | --- |
| Shop rotation (`ShopService.list_entries`) | all 11 offered (RELEASED + `shop_enabled`) | `test_all_new_cards_are_offered_in_the_shop_rotation` |
| Shop card list `handlers/shop._cards_view` | each name present, each button labelled `(300 🍎)` | `test_shop_card_list_shows_every_new_card_for_300_apples` |
| Shop detail `handlers/shop._shop_item_view` | name + `300 سیب` + bundled photo + buy callback | `test_new_card_detail_screen_shows_its_art_and_the_300_price` |
| Purchase / ownership / wallet / ledger | exactly 300, one card, one ledger row | `test_every_new_card_is_purchasable_for_exactly_300_apples[...]` + protections test |
| Wiki index + character page (`WikiService`) | listed on some page, all 15 reachable, stats rendered | `test_wiki_lists_and_renders_every_new_card` |
| Wiki card screen (`handlers/wiki.handle_wiki_char`) | sends the card's own original file | `test_wiki_card_screen_sends_the_exact_original_art_file` (fake Telegram, `FSInputFile` path asserted) |
| Owned collection + card detail (`CollectionService`, `handlers/cards._card_view`) | listed once owned, detail text + original art | `test_collection_lists_new_cards_once_owned` |
| Seed loading (`seed_characters`) | 11 rows created, re-run creates 0, existing rows untouched | `test_seed_registers_all_eleven_characters_with_unique_ids`, `test_seed_is_idempotent_and_never_touches_player_data`, `test_new_cards_appear_after_a_restart_on_an_existing_installation` |
| Shop rotation rollover on an existing install | historical rotation immutable, next rotation offers all 15 | `test_new_cards_enter_an_existing_installations_next_shop_rotation` |
| Art resolution / rendering | one distinct original file per card, no fallback | `test_every_new_card_uses_its_exact_original_asset`, `test_art_resolution_returns_the_bundled_file_and_never_a_substitute`, `test_every_new_card_asset_is_a_loadable_image_with_the_original_pixels` |
| Balance analysis script | audited profiles frozen, new cards excluded from them | constant usage + `AUDITED_SEED_CODES` (script compiles; script itself not executed) |

## 7. Seed / DB safety and the migration decision

**No migration was added, deliberately.** Adding characters changes no schema: the rows are created
by the existing seed path, `player_cards` already exists (migration 013) and level-based progression
needs no new columns (migration 022 dropped the old stored-stat columns). `033_shop_unified_card_price`
(Task 1) remains the only head, unchanged in this task.

`seed_characters` is now an append-only sync:

* reads the shipped `data/characters_seed.json` (source of truth for the roster);
* always runs faction seeding/backfill (faction links of the existing four cards survive — asserted);
* creates only codes missing from the database, then returns the number created;
* never modifies an existing row: no stat rewrite, no release-status reset, and it touches nothing
  player-owned (cards, levels, balances, ownership, battle loadouts);
* re-running on a seeded DB returns 0 and creates no duplicates;
* on any failure it logs every failing code with its reason and raises `CharacterSeedError`, so
  startup fails loudly and the transaction rolls back for a clean retry on the next start.

## 8. Tests run and actual results

All commands from `/home/user/sib` with a fresh SQLite DB
(`rm -f /tmp/sibcom_sib_test.db`; env: `sib_TEST_DATABASE_URL`/`DATABASE_URL`/`DATABASE_SYNC_URL`
= `sqlite+aiosqlite:////tmp/sibcom_sib_test.db` / `sqlite:////tmp/sibcom_sib_test.db`, `PYTHONPATH=/home/user`).

| Run | Result |
| --- | --- |
| `pytest tests/test_card_roster.py` | **35 passed** |
| Focused group: `test_card_roster.py test_characters.py test_factions.py test_card_progression.py test_apple_battle.py test_shop_card_price.py` | **137 passed** |
| Wider group incl. `test_card_upgrade.py test_card_upgrade_ui.py test_ui_navigation_regressions.py test_character_art.py` | **211 passed / 2 skipped / 4 failed** (the 4 baseline art failures only) |
| Full suite `pytest tests` | **6 failed / 1939 passed / 31 skipped** |
| Baseline full suite (worktree of `c7334a3`) | **6 failed / 1868 passed / 31 skipped** — same 6 failure names |
| `--collect-only` diff vs baseline | 1976 vs 1905 items: **+71 new, 0 removed** |
| `python -m compileall` on `database services handlers analysis` + new test files | clean |
| Fresh-DB seed inspection (ids/codes/stats/status/art existence) | 15 characters, ids 1–15, every sum 60, all `RELEASED`/`shop_enabled`, all 15 art files present |

The 6 failures, identical before and after: 4 × `test_character_art` (asserted 896×1200 framed
assets vs the delivered 1121×1403 files, plus a `MagicMock` favourite-art binding) — these pin the
four *old* framed `sib_*` cards only; `test_apple_battle_scene_library::test_restart_during_narration_reuses_the_same_plates`
(timing-sensitive, passes in other runs); and `test_transaction_regressions::test_E_daily_chest_retry_keeps_the_callers_user_row`
(needs MySQL/MariaDB row locks). None involves the new cards.

The repo has no ruff/flake8/mypy/black configuration and no CI workflow, so `compileall` plus the
test suites were the available checks.

## 9. Remaining risks / follow-ups

1. **MySQL/MariaDB not exercised locally** (no server in the sandbox): the seed sync, rotation
   immutability and purchase paths were verified only on SQLite. SQLite cannot exercise
   `SELECT … FOR UPDATE`; row-lock-dependent behaviour must be re-checked on staging.
2. **Art packaging in deployment**: the 11 files are ordinary git-tracked JPEG/PNG files inside
   `assets/characters/` and the Docker build copies the whole repository, so they ship as-is. This
   was verified from the Dockerfile and by file-existence/format tests; an actual container build
   and a real Telegram upload were NOT performed (no network/bot token here). The screens' photos
   are `FSInputFile` objects pointing at those exact files — Telegram upload itself is untested.
3. **Legacy framed-card conventions differ**: the four old cards use the 896×1200 framed pipeline;
   the new ones are the raw originals at their delivered sizes (as required). Anything that assumes
   one uniform aspect ratio for *all* cards (e.g. the failing `test_all_character_cards_exist_with_same_dimensions`,
   which presumably encodes the old pipeline) will keep reporting these as differences — the test
   currently only inspects the four `sib_*` codes, so it is unaffected, but future tooling should not
   assume a single size.
4. **Balance-versioning snapshots**: `CharacterService.create` writes a balance-version snapshot per
   card (price = 300, +10/level policy); no rebalancing is recorded for the existing four. Should
   balance tooling later expect every card to have a "balance version", the new cards have exactly
   one, version 1.
5. **Content is intentionally incomplete**: the 11 cards have no lore, faction, role, passive,
   strengths/weaknesses (deferred by requirement). Any screen or analysis that assumes those fields
   are non-empty for every character would need a follow-up — none was found in the audited surfaces.
6. **Rotation timing**: new cards appear in the next weekly rotation (history is immutable). If an
   operator wants them immediately, that is a manual rotation decision, not a code change.
