# M3-REPORT — Apple Mine: Pickaxe Rework Implementation

**Phase:** implementation (design: `docs/games/M2-RECON.md`) · **Date:** 2026-10-19
**Baseline:** `2b9e907` (M2 recon commit) · **Branch:** `arena/385c6fca-sib`

## What changed

| Area | Change |
|---|---|
| Schema | New migration **`053_apple_pickaxes`**: `user_pickaxes` (1:1, lazy) + nullable `pickaxe_level` snapshot on `apple_mine_shift`. Migration head is now `053_apple_pickaxes`. |
| Config | Two new `system_config` keys with validated built-in defaults in `services/config_store.py`: `pickaxe_upgrade_costs` = `[0, 300, 600, 1000, 1500]` (index i = cost to enter level i+1, level 1 free) and `pickaxe_rewards` = 5 rows of `{"min","max","gold","diamond","obsidian"}` (percentages of 100, mutually exclusive; fixed drop quantities 3/1/1). Parsed with `json.loads` via `ConfigService`; broken overrides fall back to defaults (same policy as `card_upgrade_costs`). |
| Service | `services/apple_mine.py` rewritten around the pickaxe: `get_pickaxe_level` (lazy Level-1 creation, locking read), `upgrade_pickaxe` (under the `users` row lock; `AppleWalletService.debit_once` with fresh key namespace `pickaxe_upgrade:{user_id}:{to_level}`; `PickaxeMaxLevelError` / `PickaxeInsufficientFunds(cost, missing)`), `perform_shift(session, *, user_id, group_id)` — **`card_id` removed from the signature**, rolls the level's reward table, records `pickaxe_level` on the shift. Cooldown logic (30 min, DB clock, last shift) is unchanged. `OUTCOMES` and `InvalidCardError` are gone. |
| Handler | `handlers/apple_mine.py` rewritten: two-button main menu (work + pickaxe), work runs the shift **immediately** (no card screen), pickaxe menu with per-level art, upgrade in place, back in place. All user-facing text is Persian; all digits via `fa_digits`. |
| Keyboards | `services/telegram_ui.py`: new `mine_menu_kb` (work green + pickaxe blue, one row); `work_button_kb` refactored to share `_work_button` (legacy-client fallback preserved). Pickaxe keyboard: `success_button` upgrade + `danger_button` back (max level: back only). |
| Help | `services/help_content.py`: the «معدن سیبی» section now describes the pickaxe ladder (per-level reward table + upgrade costs); the cosmetic-card paragraph is removed. |
| Art | 5 new assets `assets/apple_mine/pickaxe_1..5.jpg`, all **1280×720** JPEG (generated 1376×768, normalized to exactly 1280×720, quality 95): rusty iron → forged steel → runed bronze/amethyst → glowing cyan mythril → cosmic star-metal with golden magic. `assets/apple_mine/mine.jpg` (main menu) unchanged. |
| Tests | `tests/apple_mine/test_apple_mine.py` rewritten (41 tests incl. the five required), `tests/invasion_v2/test_phase8c.py` migration head advanced to 053, `tests/help/test_help_sections.py` mine assertions updated for the pickaxe. |

## In-place UI behavior (no message spam)

Every screen transition edits the **pressed message** via `handlers/apple_mine.py::_edit_screen`:

- **Photo message + new art** → `safe_edit_message_media` (photo→photo swap with new caption): mine panel → pickaxe screen, upgrade success → new level's art, back → `mine.jpg` again.
- **Photo message, same art** (work result) → `safe_edit_message_caption`.
- **Text message** (M1 image-fallback panels) → `safe_edit_callback_message`.
- A missing asset file degrades to a caption-only edit instead of breaking the screen.

Cooldown pre-check in `handle_mine_work` runs in a *separate* session from `perform_shift` — the authoritative gate stays inside the service's user-row lock (a same-session pre-read would pin a REPEATABLE READ snapshot, the M1 note is preserved in a comment).

Upgrade edge cases: insufficient funds → Persian alert with the exact shortfall (screen unchanged); stale press at max level → re-render the max screen without the 🎉 line; a replayed keyed debit (`applied=False`) adopts the winner's level and charges nothing again.

## Tests

- `tests/apple_mine`: **41 passed** (service rolls + distribution, wallet debit/ledger, cooldown, in-place edits, config validation, model shape, migration 053 upgrade/downgrade, Persian copy with no Latin letters).
- `tests/help`, `tests/shortcuts`, `tests/invasion_v2/test_phase8c.py`: **135 passed** combined with the mine suite (mine + help + shortcuts + chain head).
- Full suite (SQLite, 2 948 tests): **2 894 passed, 25 skipped, 29 failed** — the 29 failures are **pre-existing sandbox issues, not regressions**: the identical 29 tests fail on the clean baseline commit with all M3 changes stashed (invasion imaging manifest/dimensions, character art, shop chest art, tic-tac-toe flow, party/ghost/game-navigation, marriage maintenance, daily-chest retry, apple-duel scene order — the same families the M1 report flagged as pre-existing).

## Image generation status

**5/5 created** (batches 3 + 2, no limit hit). All verified 1280×720 RGB:

| File | Level | Look | Size |
|---|:---:|---|---:|
| `assets/apple_mine/pickaxe_1.jpg` | 1 | Rusted iron, cracked wooden handle, lantern-lit rock | 336 KB |
| `assets/apple_mine/pickaxe_2.jpg` | 2 | Forged steel, oiled oak handle, leather grip | 356 KB |
| `assets/apple_mine/pickaxe_3.jpg` | 3 | Bronze/silver with glowing amethyst runes | 237 KB |
| `assets/apple_mine/pickaxe_4.jpg` | 4 | Glowing cyan mythril, energy veins | 328 KB |
| `assets/apple_mine/pickaxe_5.jpg` | 5 | Cosmic star-metal, galaxy gems, golden magic | 327 KB |

## Git history

See the commit log at the end of this phase (`[m3] implement apple mine pickaxe rework and art`).
