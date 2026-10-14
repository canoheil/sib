# Phase 4 — Invasion v2 scheduler and circuit breaker

## Scope and verification

- Added `services/invasion_v2/scheduler.py` (DB-only sweep), `circuit_breaker.py` (per-group send failure state), and `power_resolver.py` (injectable authoritative power interface; no implementation installed).
- `MaintenanceService.run_once` invokes **only** `InvasionScheduler.tick()` when `invasion_enabled` is true. Old `run_invasion_cycle` remains implemented, and the old publisher is retained as `_publish_legacy_invasion_updates` but neither is called from the production loop. Startup recovery invokes **only** `InvasionScheduler.recover()` under the same flag; the old recovery call is retained in `_recover_legacy_invasions` and not invoked.
- Startup logs `inv2 power resolver: none` (or the installed resolver class name) once. A missing resolver logs `inv2 spawn skipped: no power resolver configured` once at INFO; an unknown group average logs at DEBUG.
- With the flag false, `tick()`, `recover()` and circuit-breaker entry points return without opening a database session. No Telegram send is made by the new services; no reward grants are issued by expiry/recovery.
- Tests: `PYTHONPATH=/home/user .venv/bin/python -m pytest -q tests/invasion_v2 tests/test_invasion_feature_flag.py` → **99 passed**, including domain and Phase 0 regression tests. Phase 4 tests use an injected isolated SQLite session provider rather than a live database, plus mocks for the resolver, maintenance routing, recovery routing and startup logs. The 70/30 region preference is checked over 1,000 seeded selections. `compileall` and `git diff --check` passed.
- This run did not connect to the production DB, restart the bot, send Telegram messages, or enable `invasion_enabled`.

## Scheduler choices

1. Each flagged tick finalizes past-due `ACTIVE` v2 rows as `DEFEAT` when HP remains (region becomes lost) or `VICTORY` when HP is zero (region is restored). An overdue `PENDING` row with a deadline becomes `EXPIRED`. No rewards are granted here.
2. Startup recovery marks only overdue `ACTIVE` rows `EXPIRED`, logs a warning if any, and never marks a region lost or grants rewards.
3. At most one new PENDING occurrence is scheduled per global `invasion_interval_seconds` (default 3600). Active groups are considered in ascending-ID round-robin order from the last scheduled group; pending/active invasions and muted/inactive groups are skipped. Ordered group row locks and a unique occurrence key protect MariaDB scheduling from duplicate concurrent workers.
4. The 70% preferred branch chooses uniformly among currently lost regions if any, otherwise the group's latest v2 invasion region; the 30% branch chooses uniformly among all seeded regions. With no prior group region or lost region, both branches choose among all regions. No group-region mapping exists yet.
5. A new PENDING row has no `started_at` or `ends_at`: **Phase 5 must start the 300-second window after a successful announcement**, rather than expiring it before Telegram delivery exists. A PENDING row therefore blocks further spawns for its group until the later phase supplies delivery/activation or cleanup.
6. A successful send resets consecutive failures and clears the stale mute marker; three failed sends mute for the configured 24 hours. An active group with a mute whose end is more than seven days past is deactivated. These methods are callable by a future Telegram adapter but do not send messages.

## Known Limitations

- **Group power resolution is not yet available.** The schema does not authoritatively track all active group members and their main cards. Phase 4.5 will implement `GroupPowerResolver.average_main_card_power` using a new `inv2_group_members` table populated by existing group-interaction hooks.
- **The feature will not spawn until Phase 4.5 provides a resolver. This is intentional and by design.** No fallback power or party-member approximation is used. Finalization/recovery still work if old v2 rows already exist and the flag is enabled.
- The group's “current region” is not stored explicitly; the most recent v2 invasion region is a provisional scheduling reference. When no region is known, selection is uniform; Phase 4.5 should settle the authoritative mapping.
- There are no Telegram sends yet. PENDING rows have no deadline until Phase 5 activates them; production message delivery and callbacks remain future work. This phase is safe to deploy with `invasion_enabled = false`.
- The pre-existing legacy invasion integration tests exercise the old `run_once` behavior and need adaptation when v2 replaces the production loop. They were not run here; new routing tests assert the old cycle/publisher are not invoked.
