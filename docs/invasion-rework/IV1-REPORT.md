# Phase IV-1 — Invasion v2: Spawn for All Eligible Groups per Interval

Status: shipped on this branch. Recon, the spawn-policy change, the new
counter/log line and the tests landed as separate `[iv1]` commits (see
`git log`). `invasion_enabled` was not read differently, written or seeded
by this phase — the scheduler still only consults it through the unchanged
`_enabled()` / `MaintenanceService._invasion_enabled()` gate.

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

**Old policy.** Each maintenance tick first applied a single global
interval gate — if the most recent invasion anywhere was younger than
`invasion_interval_seconds`, nobody spawned. When the gate opened, the
scheduler reordered all active groups round-robin style, starting just
after the group that received the previous invasion, walked that list
skipping muted groups, groups that already had a live invasion and groups
with unknown power, and stopped with a `return` immediately after the
first successful spawn. One interval therefore produced at most one
invasion worldwide, and the round-robin pointer decided which single
group received it; with N eligible groups a given group was served only
once every N intervals, and every other eligible group simply waited,
even though nothing about its own state prevented an invasion.

**New policy.** The global interval gate is unchanged — it still
prevents multiple ticks inside the same interval from spawning — but a
due tick now iterates the active groups once, in stable id order (the
round-robin `next_id` reordering and the early `return` are gone), and
spawns one invasion for *every* group that passes the per-group checks:
not muted by the circuit breaker (`skipped_muted`), fewer than
`invasion_max_active_per_group` (default/configured 1) `PENDING`/`ACTIVE`
invasions already live for that group (`skipped_active`), a power
resolver is installed (`skipped_no_resolver`, now a per-group skip
instead of abandoning the sweep), and the resolved average main-card
power is known and positive (`skipped_insufficient_players`). Multiple
groups can therefore all spawn in the same tick, each with its own
region/enemy/HP computed by the unchanged formulas, while any single
group is still capped at one new invasion per interval and at
`invasion_max_active_per_group` simultaneous live invasions.

## What changed in code

Only `services/invasion_v2/scheduler.py` (`InvasionScheduler._spawn_if_due`
and the `tick` stats/docstring):

- Removed the round-robin `next_id`/`ordered` reordering; groups iterate
  in the id order they were locked in.
- Removed the `return` after the first successful spawn.
- The scheduler now explicitly reads `invasion_max_active_per_group`
  (missing key → 1; non-positive/unparseable → error log and the sweep
  aborts, fail-closed like the interval/difficulty settings) and counts
  the group's live `PENDING`/`ACTIVE` invasions against it instead of
  only probing for the existence of one row.
- A resolver that disappears mid-sweep now skips that group
  (`skipped_no_resolver`) and the sweep continues with the next group.
- HP formula, enemy-type distribution, difficulty multiplier, region
  choice (70% lost / 30% random), occurrence keys, circuit breaker,
  narrative and the join flow are untouched, as are the
  `_archive_inv1_*` tables.

## New counter: `skipped_insufficient_players`

The structured `inv2_tick` log keeps `spawned`, `finalized`, `expired`,
`skipped_muted`, `skipped_active` and `skipped_no_resolver`, and gains
`skipped_insufficient_players`. It is incremented once per group whose
resolved average power is `None`, non-finite or `<= 0` — in production
the resolver returns `None` chiefly when fewer than
`invasion_min_players_with_card` active members own a Main Card, hence
the name; it also covers the resolver's other fail-closed "power
unknown" cases. `spawned` can now be greater than 1 per tick (up to the
number of eligible groups).

## New per-group spawn log line

Every successful spawn emits one INFO line in addition to the
`inv2_tick` summary:

```
inv2 spawned: group=<id> enemy=<type> hp=<hp> region=<code>
```

Example from the test run: `inv2 spawned: group=1 enemy=shadow hp=2000 region=region_1`.

## Before/after: invasions per hour (7 groups, interval = 7200s)

|  | Before (round-robin, 1/tick) | After (all eligible/tick) |
|---|---|---|
| Invasions per interval (all groups eligible) | 1 | up to 7 |
| Invasions per hour, all groups combined | 0.5 | up to 3.5 |
| Per group | 1 every ~14h (7 × 2h) ≈ 0.071/h | 1 every 2h = 0.5/h |
| Per group per day | ~1.7 | up to 12 |

Actual rates are lower whenever a group is muted, still has a live
invasion from the previous interval, or lacks enough players with cards.

## Test results

New file `tests/invasion_v2/test_spawn_per_group.py` (8 tests, isolated
SQLite, mocked resolver/circuit breaker, no Telegram):

- `test_spawn_all_eligible_groups_in_one_tick` — 3 eligible groups → 3 invasions in one tick.
- `test_skip_groups_with_insufficient_players` — one group with `avg=None` → 2 spawn, `skipped_insufficient_players == 1`.
- `test_skip_muted_groups` — circuit-breaker-muted group skipped, `skipped_muted == 1`.
- `test_skip_groups_with_active_invasion` — group with a live invasion skipped, `skipped_active == 1`.
- `test_no_spawn_before_interval_elapsed` — last invasion 10 min ago (< 3600s interval) → no spawns.
- `test_respects_max_active_per_group` — with the cap set to 2, a group at 2 live spawns nothing, a group at 1 live gains exactly one.
- `test_spawn_count_can_exceed_one` — asserts `stats["spawned"] > 1` (== 3).
- `test_per_group_spawn_log_line` — asserts the exact `inv2 spawned: group=… enemy=… hp=… region=…` line per spawned group.

Updated existing tests that assumed at most one spawn per tick:
`test_resolved_power_spawns_with_formula_and_round_robin` in
`test_scheduler.py` was rewritten as
`test_resolved_power_spawns_for_each_eligible_group_with_formula` (both
active groups now spawn in the first tick; later ticks spawn nothing
because both still hold a live PENDING invasion), and
`test_tick_logging.py` now also expects the
`skipped_insufficient_players` field on the `inv2_tick` record.

Results (`.venv` with `requirements.txt` + `aiosqlite`,
`PYTHONPATH=/home/user:/home/user/sib`):

- Targeted IV-1 files (`test_spawn_per_group.py`, `test_scheduler.py`,
  `test_tick_logging.py`, `test_min_players_threshold.py`, `test_e2e.py`):
  **35 passed**.
- Full `tests/invasion_v2/` + `tests/test_invasion_feature_flag.py`:
  **471 passed, 12 failed**. All 12 failures are in
  `test_imaging.py` (asset manifest hash / image dimension / ICC-profile
  mismatches under `assets/…`); they are pre-existing, unrelated to this
  phase — IV-1 touches no asset, imaging or presentation code — and are
  recorded here as a known limitation of the test environment rather than
  fixed in this phase.

## Limitations

- The interval gate is still **global**: it is measured from the most
  recent invasion of *any* group. A group that becomes eligible
  mid-interval waits for the next due tick, and an admin force-spawn
  (which also stamps `scheduled_at=now`) pushes the next regular spawn
  for every group out by up to one interval.
- Spawning for all groups happens serially inside the single tick
  transaction; if one group's spawn raises, the tick fails and the whole
  transaction (including earlier spawns in the same tick) rolls back,
  to be retried on the next tick. There is no per-group error isolation.
- A tick that spawns for many groups does proportionally more DB work
  (one resolver call and several queries per group) while holding the
  active-Group row locks, so very large group counts lengthen the tick;
  no batching was introduced in this phase.
- `skipped_insufficient_players` counts every "power unknown or
  non-positive" skip, not literally only the player-count threshold —
  the resolver deliberately returns `None` for corrupt-card and DB-error
  cases too, and those remain fail-closed under the same counter.
