# STEP-2 — Fix Trade second-confirmation bug + quota semantics + regression tests

Status: IN PROGRESS

## Sub tasks

1. [ ] add `TradeStatus.FAILED`; `_execute` failure paths → FAILED (CANCELLED = user cancel only)
2. [ ] collision-free swap in `_execute` (park both cards under unique non-player ids, then assign)
3. [ ] end-state uniqueness validation at `create` (TradeError) and re-check in `_execute` (FAILED)
4. [ ] `_trades_used_today` counts PENDING/COMPLETED/CANCELLED only (EXPIRED/FAILED release quota)
5. [ ] deterministic ascending card-id lock order in `create`/`_execute`
6. [ ] update `test_trade_with_stale_card_fails_safely` assertion CANCELLED→FAILED
7. [ ] regression tests (reported flow, stale, ownership changed, duplicate confirm, failed
   finalization, concurrent confirm (row-locks), quota rollback/consistency)
8. [ ] run trade tests + full suite

## NOTES

(see CURRENT_DESIGN.md for the root-cause writeup; reproduction: same-character cards →
IntegrityError on B's confirm, quota burned)
