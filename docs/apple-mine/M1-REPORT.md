# Apple Mine — M1 Report

## Task 1: Reconnaissance

- **Apple codes:** normal currency outcome: `apple_normal` (`services/garden.py:42–43`); it is credited to the wallet, not stored as a `user_items` row. Special-apple inventory keys: `apple_gold`, `apple_diamond`, and `apple_obsidian` (`services/items.py:52–65`).
- **Grant helpers:** `ItemService.grant(session, user_id, item_key, quantity)` for special apples (`services/items.py:85–116`); `AppleWalletService.earn(...)` for normal-apple currency (`services/apple_wallet.py:151–203`).
- **Persian digits:** `fa_digits` (`services/sib_claim.py:27, 42–44`).
- **Cooldown helper:** no reusable generic cooldown helper found. `SibClaimService` has feature-specific claim cooldown logic (`services/sib_claim.py:87–140`).
- **Photo panel/edit reference:** shop chest details return text, inline keyboard, and photo (`handlers/shop.py:836–887`); the callback edits the displayed screen (`handlers/shop.py:926–945`). For a caption-only edit that retains the image, use `safe_edit_message_caption` (`services/telegram_ui.py:249–278`).
- **Card-selection reference:** Apple Battle’s `_select_view` uses `PlayerCardService.list_cards`, renders per-card inline buttons, and handles an empty collection (`handlers/apple_battle.py:97–145`).
- **aiogram / Bot API:** dependency declaration is `aiogram>=3.30.0,<4.0.0` (`requirements.txt:1`). No aiogram installation or dependency lockfile is present in this workspace, so the exact deployed aiogram version cannot be verified here. The UI helper documentation identifies Bot API 10.3 button styles and existing keyboard code uses `style="success"` (`keyboards/common.py:3–10, 186–191`). The Telegram server’s live Bot API version is not discoverable from this checkout; the feature will use runtime capability checks and a safe label fallback.
- **Migration baseline:** the repository’s current Alembic head is `042_inv2_pinned_announcement` (`database/migrations/versions/042_inv2_pinned_announcement.py:17–18`). There is no configured local database connection to verify the deployed database’s applied revision.

## Implementation and verification

_To be completed after implementation._
