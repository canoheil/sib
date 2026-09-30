# STEP-3 — Implementation & Test Record

## Targeted test runs (chronological)

| Stage | Command | Result |
|---|---|---|
| Baseline (before changes) | `pytest -q` (sqlite+aiosqlite test DB) | 459 passed, 6 skipped |
| After Trade fix + tests | `pytest tests/test_trading.py tests/test_trade_group_flow.py -q` | 26 passed, 1 skipped |
| After Trade fix | `pytest -q` | 468 passed, 6 skipped |
| After Guide fix + tests | `pytest tests/test_nav_root_fixes.py tests/test_group_menu_games_guide.py tests/test_navigation.py -q` | 19 passed |
| After Games-button + style tests | `pytest tests/test_main_menu_style.py -q` | 3 passed |
| After سیبام/سیب من routing | `pytest tests/test_sib_claim.py tests/test_nav_root_fixes.py tests/test_phase2_nav_garden.py -q` | 38 passed, 1 skipped |
| After Persian audit | `pytest -q` | **477 passed, 6 skipped** |

Full-suite command used throughout:

```
rm -f /tmp/sibcom_sib_test.db && PYTHONPATH=/home/user \
  DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
  sib_TEST_DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
  .venv/bin/python -m pytest -q
```

## Limitations (honest)

- The 6 skipped tests are the `requires_row_locks` MySQL/MariaDB tests. **MySQL/MariaDB is not available in this environment** — those row-lock tests were NOT executed. The SQLite suite exercises every other path (including the new concurrency regression tests, which do run on SQLite).
- No test was deleted or weakened. Three existing assertions were UPDATED to the audited strings («عضویتت تأیید شد», «هنوز کارتی نداری» ×2) — same assertions, corrected orthography/register; all other test changes are NEW regression tests or strengthened assertions.

## Regression tests added (not weakened)

- `tests/test_trading.py`: same-character swap, impossible end-state rejection, failed-finalization quota release, expired quota release, quota consistency, 2 × concurrent-confirmation (one-shot claim).
- `tests/test_trade_group_flow.py`: same-character flow through the handler (FAILED + friendly alert + quota free), stale ownership.
- `tests/test_nav_root_fixes.py`: Guide→Apple-Battle navigation regression ×4, extended wake-word exclusivity.
- `tests/test_main_menu_style.py` (new): both main-menu Games buttons green; other buttons keep their styles.
- `tests/test_sib_claim.py`: سیبام→balance+basket image, سیب من→profile (not balance), routing invariants, no claim fall-through.

## Files changed (implementation)

Trade: `database/models/trade.py`, `services/trading.py`, `handlers/trading.py`
Guide/nav: `services/nav.py`, `handlers/start.py`
Games button: `keyboards/common.py`
Balance/routing/art: `handlers/start.py`, `assets/items/apple_basket.jpg` (NEW)
Persian audit: `core/errors.py`, `handlers/{admin_center,apple_battle,games/common,garden,leaderboard,matchmaking/*,party/handlers,shop,start,trading,wiki}.py`, `services/{bot_match,characters,garden,items,party/service,player_cards,trading,wiki}.py`, tests pins ×3.

## Protected artifacts — untouched

`assets/characters*`, `assets/frames/card_frame_gold.jpg`,
`assets/items/{diamond_apple,golden_apple,magic_powder,obsidian_apple}.jpg`
(`git diff --name-only assets/` is empty; only the NEW `assets/items/apple_basket.jpg` was added).
