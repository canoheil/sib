# PLAN — Sib engineering task

1. Setup: install `persian-writing` skill (`.agents/skills/persian-writing/`, `skills-lock.json`), task docs.
2. Trade fixes (`services/trading.py`, `database/models/trade.py`, tests):
   - collision-free ownership swap (park-then-assign) so valid same-character swaps never violate
     `uq_player_card_user_character`;
   - end-state uniqueness validation at `create` (friendly `TradeError`) and re-validated in `_execute`
     under lock (fail-safe `FAILED` status, no partial changes);
   - new `TradeStatus.FAILED`; `_execute` failure paths use it (CANCELLED stays user-cancel only);
   - quota counts PENDING/COMPLETED/CANCELLED, excludes EXPIRED/FAILED;
   - deterministic ascending card-id lock order in `create`/`_execute` (deadlock safety);
   - regression tests: reported flow (same-character swap completes atomically), stale trade,
     ownership changed, duplicate confirmation, failed finalization, concurrent confirmation
     (`requires_row_locks`), quota rollback/consistency; update the one assertion that pinned
     failed-finalization → CANCELLED (`test_trade_with_stale_card_fails_safely`) to FAILED.
3. Guide fix (`handlers/start.py`, `services/nav.py`): topic-aware parse for `help_page:<topic>[:origin]`
   so `apple_battle` (a KNOWN_SCREEN name) is never eaten as origin; regression tests for every topic
   with and without origin.
4. Main-menu Games button → green (`success`) in `keyboards/common.py`, per-button only.
5. Apple-basket image: 1024×1024 text-free warm-painterly basket → `assets/items/apple_basket.jpg`,
   loaded through `AssetService.resolve_photo` (asset key `apple_basket`), attached to the «سیبام»
   balance reply with graceful text fallback.
6. «سیبام» / «سیب من» routing: balance regex = `سیبام` only; «سیب من» reuses the existing profile opener
   shared with «سیب پروفایل»; update/extend wake-word tests.
7. Persian UX/writing audit with `persian-writing` skill (orthography + colloquial-written register),
   covering menus, games, Guide, Trade, Cards/Collection, Shop, Profile, Apple/Harvest, special-apple
   messages, errors, confirmations, empty states, navigation. No gameplay/prices/callbacks/Lore changes.
8. Full regression suite; final report.
