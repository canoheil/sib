# FINAL REPORT — Unified Shop Card Pricing (every card = 300 Apples)

Branch: `arena/204c0909-sib` · Baseline commit: `c7334a3` (uncommitted working tree)
Test engine available in this sandbox: SQLite (`sib_TEST_DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test.db`).

Baseline (pre-change, same command): `6 failed / 1868 passed / 31 skipped`
Final (post-change): **`6 failed / 1893 passed / 31 skipped`**
The 6 failures are the same pre-existing, environment-specific ones (4 asset-dependent
`test_character_art` checks, the narration-restart / cooldown timing checks, and the
SQLite-only `test_transaction_regressions::test_E_daily_chest_retry_keeps_the_callers_user_row`
row-lock check). Nothing pricing-related fails.

## 1. The rule and how it is enforced

- **Single definition:** `SHOP_CARD_PRICE_APPLES: int = 300` in `database/models/shop.py`,
  exported through `database/models/__init__.py`.
- **Single accessor:** `ShopService.card_price()` — no arguments, so no entry, character,
  rarity or client value can influence it. Every screen, the balance pre-check, the debit
  and the ledger row use it (`ShopService.entry_price(entry, character)` was deleted;
  `grep -rn "entry_price"` is empty).
- **Server-side only:** the buy button carries `shop_buy:{entry_id}:{nonce}` — no price
  segment; `ShopService.purchase(session, user_id, entry_id, nonce=...)` has no price
  parameter, and malformed callbacks (extra/price-like segments) are refused before any
  database work, as before.
- **Future cards need no configuration:** `CharacterService.create` lost its `price`
  parameter (the legacy column is written with the unified value) and no longer accepts
  `rarity`; `CharacterService.update` refuses both `price` and `rarity`; the `price` key
  was removed from `data/characters_seed.json` and `database/connection.py::seed_characters`.
- **Legacy columns never read:** `characters.price` and `shop_entries.price_override` are
  kept for backward-compatible schema only (never consulted for pricing), and
  `render_character_card` prints the unified constant instead of the column.

## 2. Files changed

| File | Change |
| --- | --- |
| `database/models/shop.py` | + `SHOP_CARD_PRICE_APPLES = 300`; `price_override` marked deprecated |
| `database/models/character.py` | `price` default → unified constant; comments; snapshot docstring |
| `database/models/__init__.py` | export the constant |
| `services/shop.py` | `card_price()` accessor; purchase uses it; `entry_price` removed |
| `services/characters.py` | no `price`/`rarity` in the write API; render uses the constant |
| `database/connection.py` | seed loader no longer reads a `price` key |
| `data/characters_seed.json` | 4 `"price"` keys removed |
| `handlers/shop.py` | card list/detail price from `card_price()`; docstring |
| `handlers/admin_meta.py` | admin character/shop screens show the unified price |
| `services/config_store.py` | stale `character.price` balance note corrected |
| `database/migrations/versions/033_shop_unified_card_price.py` | new migration (data normalization + column default) |
| `TODO/DECISIONS.md` | decision entry #15 |
| `tests/test_shop_card_price.py` | **new**: 21 tests for the whole rule |
| `tests/test_shop_card_price_migration_033.py` | **new**: 4 migration tests |
| `tests/test_shop.py`, `test_transaction_regressions.py`, `test_characters.py`, `test_character_art.py`, `test_player_level_art.py`, `test_shop_chests.py` | updated call sites that used per-card prices |

## 3. Rarity

The game has no rarity system in the code: nothing prices, filters, sorts, badges or
labels by rarity, and the deprecated `characters.rarity` column is never read.
It is deliberately **retained** (with historical values) instead of dropped, because
dropping it would be a destructive schema change on live installations; writing it is now
refused by `CharacterService.update`, so it is inert data. The only remaining "rarity"
wording in the repo belongs to drop-weight ordering of special Apples
(daily chest / garden / item captions) — a separate reward system, explicitly out of scope.

## 4. Migration / data

Migration `033_shop_card_price` (down_revision `032_shop_chests`):

1. `UPDATE characters SET price = 300` (all rows),
2. `UPDATE shop_entries SET price_override = NULL`,
3. `ALTER characters.price SET DEFAULT 300` on MySQL/MariaDB only (SQLite — test engine —
   cannot alter column defaults; the code never relies on it).

No column/table is dropped, no wallet/card/ownership data is touched, and the
pre-migration per-card prices remain readable in `character_balance_versions.snapshot`.
Downgrade reverses the column default only and documents why the data normalization is
not reversed.

## 5. Tests executed (actual results)

- Task-relevant batch (shop, characters, chests, cards, trading, items, migrations,
  models, lock ordering — 21 files):
  **`1 failed / 324 passed / 7 skipped`** — the single failure is the pre-existing
  SQLite-only `test_E_daily_chest_retry_keeps_the_callers_user_row` (it also fails on the
  untouched baseline).
- Full suite, fresh SQLite DB: **`6 failed / 1893 passed / 31 skipped`** vs baseline
  `6 failed / 1868 passed / 31 skipped` (same pre-existing failures, +25 new passing tests).

Command:

```
env sib_TEST_DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
    DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
    DATABASE_SYNC_URL="sqlite:////tmp/sibcom_sib_test.db" \
    PYTHONPATH=/home/user .venv/bin/python -m pytest tests -q
```

New coverage: unified value/definition, no per-card API, legacy `price`/`price_override`
values ignored, exact 300 debit + ledger row, list/detail screens showing 300,
forged callbacks refused, callback carries no price, insufficient-balance and
duplicate/replay rules unchanged, rarity has no effect, cards registered after the rule
are 300, free acquisition and other economy prices untouched, migration normalizes data.

The project has no linter/type-checker configuration (no ruff/flake8/mypy/CI config);
`python -m compileall` over all changed files passes.

## 6. Open items / risks

- Only SQLite could be exercised here (no MariaDB daemon in the sandbox), so the MySQL
  `ALTER ... SET DEFAULT` branch of migration 033 and the row-lock concurrency tests are
  unverified locally; run them on staging before/with the production migrate.
- Unusable Apple prices remain impossible by design: a wallet debit is the final check.
