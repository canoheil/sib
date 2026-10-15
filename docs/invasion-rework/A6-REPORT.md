# A6 — Relaxed power resolver: minimum player threshold

Status: **in progress** (recon recorded, implementation pending).
Flag policy unchanged: `invasion_enabled` is not read, written or seeded by
this phase.

## 1. Recon findings

| What | Where |
|---|---|
| `DbGroupPowerResolver` (class) | `services/invasion_v2/power_resolver_impl.py:14` |
| Resolver entry point `average_main_card_power` | `services/invasion_v2/power_resolver_impl.py:17-52` |
| **Fail-closed branch** — a member without a Main Card aborts the whole group | `services/invasion_v2/power_resolver_impl.py:36-38` (`card = await PlayerCardService.get_main_card(...)` → `if card is None ...: return None`) |
| Companion fail-closed branches (invalid level / missing character / bad attributes / bad power) | `services/invasion_v2/power_resolver_impl.py:37-47` |
| DB-error safety net (`except Exception` → `None`) | `services/invasion_v2/power_resolver_impl.py:50-52` |
| Scheduler call site (resolver result `None` → group skipped) | `services/invasion_v2/scheduler.py:176-179` |
| Resolver registration | `bot.py:65-68` (`set_power_resolver(DbGroupPowerResolver())`) |
| "No resolver configured" safety net in the scheduler | `services/invasion_v2/scheduler.py:114-118`, `170-175` |
| Other resolver consumers | `handlers/inv2_admin.py:99` (`/inv2_simulate`), `services/invasion_v2/simulation.py:30` |
| Active-member window key `inv2_member_active_window_days` | read at `services/invasion_v2/power_resolver_impl.py:19`; in-code default `services/config_store.py:25`; seeded by `database/migrations/versions/037_inv2_group_members.py:41-55`; also read by `services/invasion_v2/simulation.py:34` |
| Membership table | `inv2_group_members` (`Inv2GroupMember`, migration 037) |

### Tests that assert the current fail-closed behaviour

| Test | File:line | Asserts |
|---|---|---|
| `test_no_members_missing_main_and_average_of_levelled_main_cards` | `tests/invasion_v2/test_group_power_membership.py:57-73` | One member with a card averages to `30.0`; two average to `40.0`; a member **without** a card returns `None` (line 69) |
| `test_window_and_uncertain_power_fail_closed` | `tests/invasion_v2/test_group_power_membership.py:77-103` | A member without a card inside the window returns `None` (line 87); level 99 → `None`; missing character → `None` |
| `test_resolver_db_failure_returns_none` | `tests/invasion_v2/test_group_power_membership.py:107-111` | DB error → `None` (safety net, must stay) |
| `test_unknown_power_skips_spawn` | `tests/invasion_v2/test_scheduler.py` | Scheduler skips a group whose resolver returns `None` |

The first two encode the policy A6 replaces and are updated in this phase; the
last two encode safety nets that A6 keeps.

### Everything A6 must leave alone

Scheduler cadence (`invasion_interval_seconds`, one spawn per global
interval, round-robin), HP formula (`compute_enemy_hp` × difficulty), rewards,
narrative, circuit breaker, and the `_archive_inv1_*` tables.

## 2. Old policy vs new policy

*Pending implementation.*

## 3. Migration

*Pending implementation.*

## 4. Sample log lines

*Pending implementation.*

## 5. Tests

*Pending implementation.*

## 6. Limitations

*Pending implementation.*
