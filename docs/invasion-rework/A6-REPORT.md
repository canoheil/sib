# A6 — Relaxed power resolver: minimum player threshold

Status: **shipped on `arena/83234cf1-sib`**. One behaviour module touched
(`services/invasion_v2/power_resolver_impl.py`), one config default
(`services/config_store.py`), one migration (`046_inv2_min_players_with_card`),
one new test module (14 tests) and two existing tests updated. No cadence, HP,
reward, narrative or circuit-breaker code changed, and `invasion_enabled` was
not read, written or seeded by this phase.

## 1. Recon findings

| What | Where |
|---|---|
| `DbGroupPowerResolver` (class) | `services/invasion_v2/power_resolver_impl.py:14` |
| Resolver entry point `average_main_card_power` | `services/invasion_v2/power_resolver_impl.py:17-52` (before A6) |
| **Fail-closed branch** — one member without a Main Card aborted the whole group | `services/invasion_v2/power_resolver_impl.py:36-38` (before A6: `card = await PlayerCardService.get_main_card(...)` → `if card is None ...: return None`) |
| Companion fail-closed branches (invalid level / missing character / bad attributes / bad power) | `services/invasion_v2/power_resolver_impl.py:37-47` (before A6; kept for corrupt cards) |
| DB-error safety net (`except Exception` → `None`) | `services/invasion_v2/power_resolver_impl.py:50-52` (before A6; kept) |
| Scheduler call site (resolver result `None` → group skipped) | `services/invasion_v2/scheduler.py:176-179` — **unchanged by A6** |
| Resolver registration | `bot.py:65-68` (`set_power_resolver(DbGroupPowerResolver())`) |
| "No resolver configured" safety net | `services/invasion_v2/scheduler.py:114-118`, `170-175` — unchanged |
| Other resolver consumers | `handlers/inv2_admin.py:99` (`/inv2_simulate`), `services/invasion_v2/simulation.py:30` |
| Active-member window key `inv2_member_active_window_days` | read at `services/invasion_v2/power_resolver_impl.py:19`; in-code default `services/config_store.py:25`; seeded by `database/migrations/versions/037_inv2_group_members.py:41-55`; also read by `services/invasion_v2/simulation.py:34` |
| Membership table | `inv2_group_members` (`Inv2GroupMember`, migration 037) |

### Tests that asserted the old fail-closed behaviour

| Test | File:line (before A6) | Asserts | A6 action |
|---|---|---|---|
| `test_no_members_missing_main_and_average_of_levelled_main_cards` | `tests/invasion_v2/test_group_power_membership.py:57-73` | one member with a card → `30.0`, two → `40.0`, a member **without** a card → `None` | updated: threshold lowered to `1` in the test, the "member without a card" case now averages over the members who have one |
| `test_window_and_uncertain_power_fail_closed` | `tests/invasion_v2/test_group_power_membership.py:77-103` | member without a card inside the window → `None`; level 99 → `None`; missing character → `None` | updated: threshold lowered to `1`, the no-card branch now returns the average; the **corrupt-card** branches (`None`) are kept |
| `test_simulation_is_readonly_except_required_audit` | `tests/invasion_v2/test_admin_phase6.py:203-217` | `/inv2_simulate` works with a single card-carrying member | updated: seeds three card-carrying members (the new minimum) |
| `test_resolver_db_failure_returns_none` | `tests/invasion_v2/test_group_power_membership.py:107-111` | DB error → `None` | **kept verbatim** (safety net) |
| `test_unknown_power_skips_spawn` | `tests/invasion_v2/test_scheduler.py` | resolver `None` → group skipped | kept verbatim |

### Everything A6 leaves alone

Scheduler cadence (`invasion_interval_seconds`, one spawn per global interval,
round-robin over locked `Group` rows), the HP formula
(`compute_enemy_hp(average, enemy_type, difficulty)`), rewards, narrative,
pinned announcements, the circuit breaker, and the `_archive_inv1_*` tables.
`services/invasion_v2/scheduler.py` was not edited at all.

## 2. Old policy vs new policy

**Before A6 the resolver failed closed on membership:** it loaded every active
member (last seen within `inv2_member_active_window_days`, default 30) and, if
even one of them had no Main Card — or a card it could not score — returned
`None`, so the scheduler skipped the group and no invasion ever spawned; the
average was otherwise the mean over **all** active members. **After A6 the
resolver applies a minimum threshold:** it still loads the same active members,
but partitions them into members with a Main Card and members without one,
silently ignores the second group (logged at DEBUG), and returns the mean over
the first group only — returning `None` (no spawn) solely when fewer than
`invasion_min_players_with_card` (default **3**, overridable in
`system_config`) members have a card, while every other unknown — bad activity
window, an unusable threshold value, a card that exists but cannot be scored,
or any database error — still fails closed with `None`.

## 3. Migration `046_inv2_min_players_with_card`

- **Revision id:** `046_inv2_min_players_with_card`
- **Revises:** `045_group_welcomed_at` (new head; `tests/invasion_v2/test_phase8c.py::test_migration_chain_head` was updated from the stale `043_apple_mine_shift`, which migrations 044/045 had already invalidated).
- **File:** `database/migrations/versions/046_inv2_min_players_with_card.py`
- Key `invasion_min_players_with_card`, value `3`, description: *"Minimum number of active group members who must have a main card for invasion to spawn."* (`system_config` has only key/value/timestamps, so the description lives in the migration docstring, as in migration 037.)

Upgrade (MySQL/MariaDB — `key` and `value` are backtick-quoted, the bug that
bit migration 040; `ON DUPLICATE KEY UPDATE` keeps an existing admin value):

```sql
INSERT INTO system_config (`key`, `value`) VALUES
  ('invasion_min_players_with_card', '3')
  ON DUPLICATE KEY UPDATE `key` = `key`;
```

SQLite (isolated test databases only) uses
`INSERT ... ON CONFLICT (\`key\`) DO NOTHING`; any other dialect raises
`RuntimeError` instead of guessing.

Downgrade (reversible, non-destructive — removes only the row this migration
seeded and still holding the untouched default; a customized value survives):

```sql
DELETE FROM system_config
 WHERE `key` = 'invasion_min_players_with_card' AND `value` = '3';
```

After the downgrade the in-code default (`CONFIG_DEFAULTS` and
`DEFAULT_INVASION_MIN_PLAYERS_WITH_CARD`, both `3`) keeps behaviour identical.
`invasion_enabled` is never read, written or seeded by this migration.

Deployment: `alembic upgrade head`.

## 4. Sample log lines

Skip — not enough members with a card (INFO, one line per group per sweep):

```text
2026-10-15 19:50:06 [INFO] [sib] inv2 spawn skipped: group=-100123 players_with_card=2 required=3
```

Members ignored because they have no card (DEBUG, only when at least one is
ignored):

```text
2026-10-15 19:50:06 [DEBUG] [sib] inv2 group power ignoring members without main card group=-100123 ignored=[3, 4, 5, 6, 7, 8]
```

Spawn — the group has enough card owners: the resolver emits **no** skip line,
and the sweep's structured tick record carries `spawned=1`:

```text
2026-10-15 19:50:06 [INFO] [sib] inv2_tick   # extra: spawned=1 finalized=0 expired=0 skipped_muted=0 skipped_active=0 skipped_no_resolver=0 duration_ms=…
```

(The default text formatter renders only the message; the counters travel in
the record's `extra`, as they did before A6.) Unchanged neighbouring lines:
`inv2 spawn skipped: group <id> has no authoritative power` (DEBUG, resolver
returned `None` for any other reason) and
`inv2 spawn skipped: no power resolver configured` (INFO, once).

## 5. Tests

`tests/invasion_v2/test_min_players_threshold.py` — **14 tests, 14 passed**
(10 requested + 4 migration-046 tests):

| Test | Result |
|---|---|
| `test_resolver_below_threshold_returns_none` (2 with cards, threshold 3) | pass |
| `test_resolver_at_threshold_returns_average` (3 with cards → 30.0) | pass |
| `test_resolver_above_threshold_ignores_without_card` (3 with, 5 without → 30.0) | pass |
| `test_resolver_zero_with_card_returns_none` | pass |
| `test_threshold_reads_from_config` (1/2/3/10, plus `0` and `many` → no spawn) | pass |
| `test_missing_config_key_uses_default_3` | pass |
| `test_scheduler_skips_group_with_insufficient_players` | pass |
| `test_scheduler_spawns_group_with_enough_players` (HP == unchanged formula over the 3 owners) | pass |
| `test_ignored_members_are_logged_at_debug` | pass |
| `test_db_error_still_returns_none` (config read and card read) | pass |
| `test_migration_046_seeds_key_and_downgrade_removes_it` | pass |
| `test_migration_046_preserves_a_customized_value` | pass |
| `test_migration_046_quotes_reserved_columns` | pass |
| `test_migration_046_rejects_unsupported_dialect` | pass |

Whole suite on this branch: **24 failed, 2497 passed, 25 skipped**; the same
suite on the base commit `c254404`: **25 failed, 2482 passed, 25 skipped**.
The 24 failures are identical pre-existing environment failures (imaging asset
hashes/dimensions, tests that need the real MariaDB test database) and do not
touch the resolver, the scheduler or `system_config`. The one *fewer* failure
is `test_migration_chain_head`, now green with the real head.

## 6. Limitations

1. **Corrupt card data still fails closed.** A member whose Main Card exists
   but cannot be scored (level outside 1..10, missing `Character`, negative
   attributes, non-integer power) returns `None` for the whole group. A6
   relaxes "no card" only — an unreadable card is treated as an integrity
   error, not as "this member has no card". Relaxing it too is a two-line
   change if you want it.
2. **The threshold also gates `/inv2_simulate` and
   `services/invasion_v2/simulation.py`.** Both read the same resolver, so an
   admin simulating a group with fewer than `invasion_min_players_with_card`
   card owners now gets the Persian "توان کارت‌های اصلی این گروه نامشخص است"
   message. This is the same rule the spawner uses, and the test was updated
   to seed three owners.
3. **The key is not settable from `/inv2_set`.** `admin_controls.set_config`
   whitelists `CONFIG_DEFAULTS` keys starting with `inv2_` only, so
   `invasion_min_players_with_card` must be changed directly in
   `system_config` (or by widening that whitelist in a later phase). It *is*
   visible in `/inv2_config`, which lists `invasion_*` and `inv2_*` rows.
4. **Threshold `0` or a non-numeric value means "no spawn", not "no
   limit"**, and logs a WARNING — the same fail-closed reading used for the
   activity window.
5. **The average is over card owners, so small groups hit harder enemies than
   before.** A group with 3 strong players and 20 idle members used to spawn
   nothing at all; it now spawns an invasion scaled to those 3 players. Watch
   the first days of `inv2_tick` counters after deployment.
