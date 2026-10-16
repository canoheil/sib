# Phase IV-1 — Invasion v2: Spawn for All Eligible Groups per Interval

Status: in progress (recon recorded first; implementation, counters/log line
and tests follow in separate `[iv1]` commits on this branch).

## Recon findings (pre-change, commit `9d49e7c`)

All line numbers refer to `services/invasion_v2/scheduler.py` before the
IV-1 change (the only pre-existing working-tree edit at recon time was an
unused `func` import on line 9, which does not shift any line below it).

| Item | Location (pre-change) | Finding |
|---|---|---|
| `_spawn_if_due` method | `services/invasion_v2/scheduler.py:111-214` (signature at 111-112) | Classmethod on `InvasionScheduler`; called from `tick()` at line 234 inside the maintenance session. It first returns early when no power resolver is installed (114-118), reads the interval/difficulty config, locks all active `Group` rows in id order (134-136), reads the most recent invasion (139-143) and applies the global interval gate (144-145). |
| `return` that stops after one spawn | `services/invasion_v2/scheduler.py:214` | `return  # at most one new invasion per global interval` — immediately after `stats["spawned"] += 1` (212-213) and `region.last_invasion_id = invasion.id` (211). This is the single line that caps a tick at one invasion worldwide. |
| Round-robin `next_id` logic | `services/invasion_v2/scheduler.py:151-155` | `next_id = last.group_id …` (151), `ordered = [g for g in groups if … g.id > next_id]` (152), wrap-around `ordered += [g for g in groups if g.id <= next_id]` (153-154), then `for group in ordered:` (155). Groups are visited starting just after the group that received the previous invasion, which is what spreads one spawn per interval across groups over time. |
| Config read for `invasion_interval_seconds` | `services/invasion_v2/scheduler.py:120` | `raw_interval = await ConfigService.get(session, "invasion_interval_seconds")`, defaulting to `3600` on line 121 when the key is missing; non-positive values abort the sweep with an error log (122-124). Seeded as `'3600'` by migration `036_invasion_v2.py:133`. |
| Config read for `invasion_max_active_per_group` | **Not read in `scheduler.py` at all** | The scheduler never reads this key. It is seeded as `'1'` by `database/migrations/versions/036_invasion_v2.py:135` and is only read by the admin force-spawn path at `services/invasion_v2/admin_controls.py:76`. In the scheduler the cap is only *implicit*: lines 160-169 look for any single `PENDING`/`ACTIVE` row for the group (`.limit(1)`) and skip the group if one exists, which happens to equal a cap of 1 but ignores the configured value. IV-1 makes the scheduler read and enforce the configured cap explicitly. |

Other relevant pre-change behaviour in the same loop:

- Circuit breaker: `is_group_available(...)` at line 156; unavailable groups
  increment `skipped_muted` (157-158) and are skipped.
- Live-invasion check: lines 160-169 increment `skipped_active`.
- In-loop resolver loss: lines 170-175 increment `skipped_no_resolver` and
  then **`return`**, abandoning the rest of the sweep.
- Unknown/non-positive power: lines 176-182 `continue` with only a DEBUG log
  and **no counter** — there is no `skipped_insufficient_players` yet.
- Tick stats are initialised at lines 224-226 (`spawned`, `finalized`,
  `expired`, `skipped_muted`, `skipped_active`, `skipped_no_resolver`) and
  emitted as the structured `inv2_tick` log at line 247.
- `invasion_enabled` is only consulted via `_enabled()` /
  `MaintenanceService._invasion_enabled()`; IV-1 does not read, write or
  seed it.

## Old policy vs new policy

_Old policy and new policy paragraphs, the new counter, the new log line,
the before/after spawn-rate estimate, test results and limitations are
added by the later `[iv1]` commits in this file._
