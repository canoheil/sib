# STEP 1 — Trade: group discoverability + proposal flow + atomic execution

Status: DONE (full suite: 414 passed / 5 skipped / 2 MySQL-connection artefacts).

## Sub tasks

1. [x] `services/panel.py`: `trade_ok:` / `trade_cancel:` left the owner-scoped
       list (participant authorization is server-side).
2. [x] `services/trading.py`: locks + in-transaction re-validation; idempotent
       confirmation; `get_trade`, `list_pending`, `expire_overdue`.
3. [x] `handlers/trading.py`: group-aware hub, proposal flow, shared trade
       detail view.
4. [x] `keyboards/common.py`: trade entry in the group main menu.
5. [x] `tests/test_trade_group_flow.py`: 12 tests + 1 row-lock skip.

## Follow-ups closed here

- Trading router reaches the real dispatcher: `handlers/__init__.py:45`
  (`dp.include_router(trading_router)`), `bot.py` calls `register_all_handlers`.
- Scheduled caller for `TradeService.expire_overdue`: `MaintenanceService.run_once`
  (+ `test_maintenance_cycle_sweeps_stale_trade_offers`).
