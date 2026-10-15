# Apple Mine — M1 Report

## Task 1: Reconnaissance

- **Apple codes:** normal currency outcome: `apple_normal` (`services/garden.py:42–43`); it is credited to the wallet, not stored as a `user_items` row. Special-apple inventory keys: `apple_gold`, `apple_diamond`, and `apple_obsidian` (`services/items.py:52–65`).
- **Grant helpers:** `ItemService.grant(session, user_id, item_key, quantity)` for special apples (`services/items.py:85–116`); `AppleWalletService.earn(...)` for normal-apple currency (`services/apple_wallet.py:151–203`).
- **Persian digits:** `fa_digits` (`services/sib_claim.py:27, 42–44`).
- **Cooldown helper:** no reusable generic cooldown helper found. `SibClaimService` has feature-specific claim cooldown logic (`services/sib_claim.py:87–140`).
- **Photo panel/edit reference:** shop chest details return text, inline keyboard, and photo (`handlers/shop.py:836–887`); the callback renders/edits the screen (`handlers/shop.py:926–945`). For a caption-only edit that retains the image, use `safe_edit_message_caption` (`services/telegram_ui.py:315–345` after adding the button helper).
- **Card-selection reference:** Apple Battle’s `_select_view` uses `PlayerCardService.list_cards`, renders per-card inline buttons, and handles an empty collection (`handlers/apple_battle.py:97–145`).
- **aiogram / Bot API:** the repository declares `aiogram>=3.30.0,<4.0.0` (`requirements.txt:1`). aiogram was absent from the initial workspace; installing the declared requirements into the ignored local test environment resolved aiogram **3.31.0** with bundled Bot API schema **10.3** (`aiogram.__version__` / `aiogram.__api_version__`). Existing UI code documents Bot API 10.3 button styles and uses `style="success"` (`keyboards/common.py:3–10, 186–191`). A live Telegram server version is not discoverable from this checkout.
- **Migration baseline:** repository head before M1 was `042_inv2_pinned_announcement` (`database/migrations/versions/042_inv2_pinned_announcement.py:17–18`). No local MariaDB connection was configured, so the deployed database’s applied revision could not be queried.

## Schema and service

- Added reversible Alembic revision **`043_apple_mine_shift`**, descending from `042_inv2_pinned_announcement`.
- The migration creates only `apple_mine_shift`; the model includes the requested user/group/card foreign keys, outcome/item/amount, indexed `worked_at`, server-default `created_at`, the two single-column indexes, and `(user_id, worked_at)` composite index.
- The service uses weights 60/25/12/3 and the existing wallet/inventory grants. It locks the user row before cooldown reads so concurrent taps serialize globally; cards are ownership-checked but never consumed or locked.
- Narrative anti-repetition reuses `services/invasion_v2/narrative_pool.py`’s pure `pick` helper with key `mine:{user_id}` and a five-entry lookback. Each pool has six distinct entries so the five-entry exclusion can be honored.

## Handler and button behavior

- Plain-text trigger registered: **`معدن سیبی`** (whitespace-tolerant exact-match regex `^\s*معدن\s+سیبی\s*$`). Also registered **`/mine`** as an alias.
- The group panel is a photo with inline work button. Card selection edits its caption/keyboard; successful work edits the same photo caption and clears the keyboard. The card itself is cosmetic.
- **Green-button decision:** use `style="success"` when the installed aiogram version is at least 3.30, the bundled Bot API schema is at least 9.0, and the inline button model exposes `style`. The resolved environment is aiogram 3.31.0 / Bot API schema 10.3, so the styled button path is selected. Construction is still guarded by `except TypeError`; if an older model rejects it, the user-facing label falls back to `🟢 ⛏ کار کردن در معدن`. The chosen path is logged once at INFO.
- No-card, cooldown, invalid-card, database, and Telegram errors are handled with Persian user-facing replies/alerts; internal failures are logged without exposing traces.

## Sample Persian outputs

**Normal**

> چند سیب معمولی از دل سنگ بیرون آمد؛ دست خالی برنگشتی.
>
> 🍎 +۱۵ سیب معمولی

**Golden**

> نوری طلایی از شکاف سنگ تابید؛ سیب طلایی درخشید.
>
> 🌕 +۳ سیب طلایی

**Diamond**

> سیبی الماسی، سرد و درخشان، از معدن بیرون آمد.
>
> 💎 +۱ سیب الماسی

**Obsidian**

> تاریکی سنگ شکست و سیبی ابسیدینی بیرون افتاد.
>
> 🌑 +۱ سیب ابسیدینی

**Cooldown alert**

> ⛏ دیوارهای معدن هنوز خاموش‌اند. ۲۹ دقیقه و ۵۹ ثانیه دیگر برگرد.

**No cards**

> هنوز کارتی نداری. از فروشگاه یک کارت بگیر و دوباره به معدن بیا.

## Asset

- `assets/apple_mine/mine.jpg` — generated dark-fantasy mine art; JPEG, 1280×720, RGB, saved at quality 95. It is not a placeholder and contains no text or characters.

## Tests

- Apple Mine tests: **18 passed, 0 failed** (`PYTHONPATH=/home/user .venv/bin/python -m pytest -q tests/apple_mine`). Includes 10,000-roll distribution check (±20%), cooldown/second-tap, grants, Persian digits/text, no-card flow, same-panel caption edit, style fallback, schema indexes, and migration upgrade/downgrade.
- Related screen/router/migration checks: **16 passed, 2 deselected**.
- Broader SQLite test run after updating the repository’s migration-head assertion: **2,402 passed, 25 skipped, 7 deselected**. The seven deselected tests are unrelated existing failures observed in the unfiltered SQLite run (Apple Battle scene reproducibility, character-art/profile/navigation checks, and a daily-chest transaction retry); no changes were made to those features. The expected migration head assertion was advanced to `043_apple_mine_shift`.
- Production MariaDB concurrency was not available in this workspace; SQLite does not provide the row-lock semantics used by the production guard.

## Git history

The six M1 chunks, oldest first:

1. `16b254a` — `[m1] recon notes for apple mine`
2. `d0c75d8` — `[m1] apple_mine_shift table and migration`
3. `b063a0a` — `[m1] apple mine service and narrative pool`
4. `58625c2` — `[m1] apple mine asset`
5. `eb6d54d` — `[m1] apple mine handler and green-button helper`
6. `[m1] apple mine tests and implementation report` — this final chunk.

## Rollout, flag, and limitations

- Apply Alembic revision `043_apple_mine_shift` before deploying the bot code.
- `invasion_enabled` was not modified or enabled. The repository migration still seeds it to `false`, and the maintenance reader fails closed when the setting is missing. The live database value could not be queried without a MariaDB connection.
- The exact deployed aiogram version and live Telegram Bot API server version are not pinned by a lockfile/configuration here; the runtime helper checks the installed library’s version, bundled API schema, and button model, then safely falls back if `style=` raises `TypeError`.
- Narrative repetition history is process-local, matching the existing picker’s behavior; it resets when the bot process restarts.
