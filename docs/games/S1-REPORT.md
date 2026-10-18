# PHASE S1 — Shop Bulk Buy & "Buy Max" Feature

## 1. Reconnaissance Findings

- **Shop item detail rendering logic:** Located in `handlers/shop.py`:
  - `_good_view(user_id: int, item_key: str)` renders the item detail screen (bundled item artwork, Persian label/description, unit price, player's current item count, player's current apple balance, and inline keyboard).
  - `handle_shop_good(callback: CallbackQuery, bot: Bot)` (`@router.callback_query(F.data.startswith("shop_good:"))`) opens the item detail view when a player selects an item from `_goods_view`.
- **Callback handler for buying an item:** Located in `handlers/shop.py`:
  - `handle_shop_good_buy(callback: CallbackQuery, bot: Bot)` previously handled `F.data.startswith("shop_good_buy:")` with a fixed quantity of `1`.
- **How the purchase is executed:**
  - `handle_shop_good_buy` opens an async database transaction (`async with get_session() as session:`) and invokes `ItemService.buy(session, actor, item_key, quantity)` (`services/items.py`).
  - `ItemService.buy` validates the item against `ITEM_CATALOG` and reads the unit price via `ItemService.buy_price(session, item_key)` (`ConfigService.get(session, "item_buy_prices")`).
  - It debits the player's wallet using `AppleWalletService.spend(session, user_id, price * quantity, reason="item_purchase", ref_type="user_item", ref_id=None)` (`services/apple_wallet.py`) and grants the items using `ItemService.grant(session, user_id, item_key, quantity)` (`services/items.py`, which updates or inserts the `UserItem` row in `user_items`).
- **Row lock verification during purchase:**
  - Yes, row locks are acquired in strict canonical lock order (`services/lock_order.py`):
    1. **User row lock (rank 0):** `await lock_user_row(session, user_id)` (`SELECT users.id ... FOR UPDATE`).
    2. **Inventory row lock (rank 5):** `await ItemService._lock_inventory_row(session, user_id, item_key)` (`SELECT user_items.id ... FOR UPDATE`), followed by `with_for_update()` in `ItemService.grant`.
    3. **Wallet row lock (rank 6):** `AppleWalletService.spend` calls `AppleWalletService._get_or_create(session, user_id)`, which executes `SELECT apple_wallets ... WHERE user_id = ... FOR UPDATE` before checking and decrementing `wallet.balance`.

---

## 2. Shop Item View Keyboard & Callback Format

- **Callback data format:** Updated to `shop:buy:<item_code>:<quantity>` where `<quantity>` is `1`, `5`, `10`, or `max` (legacy `shop_good_buy:` callbacks remain supported as an alias). `"shop:buy:"` is also registered in `OWNER_SCOPED_CALLBACKS` (`services/panel.py`).
- **Exact button layout in `_good_view`:**

```text
[ 🛒 خرید ۱ ] [ 🛒 خرید ۵ ]
[ 🛒 خرید ۱۰ ] [ 🛒 خرید حداکثر ]
[ 🔙 بازگشت ]
```

| Row | Button 1 | Button 2 |
|---|---|---|
| 1 | `🛒 خرید ۱` (`shop:buy:<item_code>:1`, style=`success`) | `🛒 خرید ۵` (`shop:buy:<item_code>:5`, style=`success`) |
| 2 | `🛒 خرید ۱۰` (`shop:buy:<item_code>:10`, style=`success`) | `🛒 خرید حداکثر` (`shop:buy:<item_code>:max`, style=`success`) |
| 3 | `🔙 بازگشت` (`shop_goods`, style=`danger`) | — |

---

## 3. "Buy Max" Calculation & Atomic Purchase Flow

1. **When a player clicks "Buy Max" (`shop:buy:<item_code>:max`):**
   - Inside the database transaction, after locking the user row (`rank 0`), validating the item and unit price (`item_price`), and locking the inventory row (`rank 5`), the handler locks and reads the player's wallet balance (`user_balance = await AppleWalletService.get_balance(session, actor, for_update=True)`, `rank 6`).
   - Calculates `max_qty = user_balance // item_price`.
   - If `max_qty == 0`, raises `ValueError("موجودی سیب شما کافی نیست!")`, which rolls back the transaction and answers the callback with an alert (`show_alert=True`):
     `موجودی سیب شما کافی نیست!`
   - Otherwise, proceeds with `quantity = max_qty`.

2. **Fixed-quantity and resolved-max purchase (`ItemService.buy`):**
   - Calculates `total_cost = item_price * quantity`.
   - Checks if the player's wallet has at least `total_cost` apples.
   - If not, calculates `max_possible = user_balance // item_price` and answers with an alert (`show_alert=True`):
     `سیب کافی نداری! شما فقط توانایی خرید {max_possible} عدد را دارید.`
   - If sufficient:
     - Deducts `total_cost` from `apple_wallets` and writes one `AppleTransaction` row (`reason="item_purchase"`).
     - Adds `quantity` to `user_items` via `ItemService.grant`.
     - Both operations execute in the same database transaction (`get_session()`). If the inventory write fails, the session rolls back so the wallet deduction is never persisted.
     - Edits the message to show the success confirmation along with the refreshed item detail view:
       `✅ با موفقیت {quantity} عدد {item_name} خریدید.`

---

## 4. Tests

Added `tests/test_shop_bulk_buy.py` covering all required cases:

- `test_good_view_renders_bulk_buy_keyboard` — PASS
- `test_buy_1_item` — PASS
- `test_buy_5_items` — PASS
- `test_buy_10_items` — PASS
- `test_buy_max_calculates_correctly` — PASS
- `test_buy_max_fails_if_balance_too_low` — PASS
- `test_bulk_buy_fails_if_not_enough_apples_for_full_quantity` — PASS
- `test_bulk_buy_is_atomic` — PASS

Combined shop, item economy, marriage shop items, lock ordering, button styles, panel ownership/authorization, and invasion shop item suite:
**258 passed, 2 skipped, 0 failed**.
