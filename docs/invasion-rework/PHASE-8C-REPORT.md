# Phase 8C — difficulty tuning and narrative context wiring

Baseline: `c5e78ff9cf90093a2f17177ce5f3f4f88463147b`  
Branch: `arena/0e56f627-sib`  
Date: 2026-10-14 UTC

## Result

Default difficulty is **2.5**, with migration **040_inv2_difficulty_default** to
conditionally update the previous seeded value. Narrative callers now provide
invasion IDs; victory finalization provides the persisted defender count. The
existing 92 variants are unchanged; six new counted victory paragraphs bring the
total to **98**. The full scoped test run passed **421 tests**.

## Default locations: before and after

Line numbers below refer to the baseline; the five fallback expressions remain
at the same line numbers after this change.

| Runtime location | Old default | New default |
|---|---|---|
| `services/invasion_v2/scheduler.py:126` | `4.0` | shared constant → `2.5` |
| `services/invasion_v2/simulation.py:53` | `4.0` | shared constant → `2.5` |
| `services/invasion_v2/admin_controls.py:53` | `4.0` | shared constant → `2.5` |
| `services/invasion_v2/admin_controls.py:107` | `4.0` | shared constant → `2.5` |
| `handlers/inv2_admin.py:100` | `4` | shared constant → `2.5` |

The canonical constant is now
`services/config_store.py:17::DEFAULT_INVASION_DIFFICULTY_MULTIPLIER = 2.5`.
`services/config_store.py:23` registers it in `CONFIG_DEFAULTS` under
`invasion_difficulty_multiplier`. Previously that key had no entry in the central
config defaults; callers supplied the literals listed above. All five fallback
locations now import and use the constant rather than duplicating numeric defaults.
The scheduler formula, validation, timing, selection and transaction logic are
otherwise unchanged. Existing `or` versus `is None` semantics outside the scheduler
were preserved rather than broadened in this tuning phase.

`docs/invasion-rework/PHASE-1-DESIGN.md:64,130` reflects the new default. Explicit
old difficulty snapshots in test fixtures and historical migration 036 still use
4.0 where appropriate: they are existing values, not the current runtime default.
The scheduler test's old default-HP expectation was updated to 2.5.

## Migration 040

- File: `database/migrations/versions/040_inv2_difficulty_default.py`
- Revision: `040_inv2_difficulty_default`
- Parent: `039_archive_inv1_tables`
- No applied migration 036–039 was edited.

Upgrade:

```sql
UPDATE system_config
SET value = '2.5'
WHERE key = 'invasion_difficulty_multiplier'
  AND value = '4.0';
```

Downgrade:

```sql
UPDATE system_config
SET value = '4.0'
WHERE key = 'invasion_difficulty_multiplier'
  AND value = '2.5';
```

These predicates preserve different administrator settings, such as `'3.0'`.
Upgrade is idempotent; an existing `'2.5'` is left alone. Missing rows stay missing
and use the code default at read time. The migration does not update active
invasion snapshots, flags or archived tables, and does not normalize numeric
strings such as `'4'`/`'4.00'` to `'4.0'` first.

An admin explicitly selecting the exact old default `'4.0'` cannot be distinguished
from an untouched seeded row. Similarly, downgrade cannot distinguish a deliberate
admin `'2.5'` from the new default. This is the requested value-conditional behavior,
not an audit-history-based migration.

**Deployment:** migration 040 was exercised on isolated SQLite only, not applied to
production. Until it is deployed, a live stored `'4.0'` remains authoritative over
the new code default. New databases should run the normal migration chain: 036
seeds the historical value and 040 conditionally updates it. No active battle is
retroactively rescaled.

## Backwards-compatible public signatures

Every original argument remains required, keyword-only and in its original order.
New optional arguments are appended; callers omitting them still work.

```python
def render_announcement(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random, invasion_id: int | None = None) -> str: ...
def render_join(*, username: str, card_name: str, damage: int, is_crit: bool, enemy_hp_remaining: int, rng: random.Random, invasion_id: int | None = None) -> str: ...
def render_high_damage(*, username: str, card_name: str, damage: int, enemy_hp_remaining: int, rng: random.Random, invasion_id: int | None = None) -> str: ...
def render_finisher(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random, invasion_id: int | None = None) -> str: ...
def render_victory(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random, invasion_id: int | None = None, participant_count: int | None = None) -> str: ...
def render_defeat(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random, invasion_id: int | None = None) -> str: ...
def render_expired(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random, invasion_id: int | None = None) -> str: ...
```

Explicit ID keys are:

| Renderer / selected event | Key |
|---|---|
| announcement | `announcement:{invasion_id}` |
| positive-HP normal join | `join_normal:{invasion_id}` |
| positive-HP critical or high-damage join | `join_crit:{invasion_id}` |
| nonpositive-HP join/high-damage or explicit finisher | `join_kill:{invasion_id}` |
| victory | `victory:{invasion_id}` |
| defeat | `defeat:{invasion_id}` |
| expired | `expired:{invasion_id}` |

`invasion_id=0` is treated as supplied, not mistaken for absence. If it is `None`,
the exact Phase 8B fallback key construction remains: shared `join` + hash of
(player, card) for attack/finisher functions, `announce` + hash of (region, enemy)
for announcement, and event + that region/enemy hash for the other region events.
No damage, HP or RNG state is used to manufacture IDs.

## Caller wiring and participant count

- `presentation.announce` supplies `inv.id` to `render_announcement`.
- `presentation.finalize` supplies `inv.id` to either result renderer.
- On victory only, finalization runs exactly one
  `SELECT count(inv2_attacks.id) ... WHERE invasion_id = inv.id` and passes its
  result as `participant_count`. The finalizer did not already load attack rows,
  so there is no duplicate attack query. It does not count reward rows (multiple
  rewards can belong to one defender) or attacks from other invasions.
- The join handler change is **only one added narrative keyword**:
  `invasion_id=inv.id`. This is necessary for Task 4's “any other caller” requirement;
  no join behavior, guards, damage, transactions, rewards or delivery timing changed.
  AST comparison after removing that one keyword matches the baseline exactly.
- There are no external calls to high-damage, finisher or expired in current
  production code; their public APIs support the optional ID for future callers.
  Recovery still uses finalization for existing result delivery and was not edited.
- Caption/suffix/reward-summary assembly, image composition, send order, buttons,
  status transitions and delivered-at semantics are unchanged. The new arguments
  intentionally select scoped/count-aware narrative prose; they do not add sends.

## Victory pools and examples

`render_victory` partitions the current VICTORY list at pick time:

- **VICTORY_WITH_N: 6** (new entries, all contain `{n}`).
- **VICTORY_WITHOUT_N: 12** (all original entries, unchanged).
- **VICTORY total: 18**; all seven narrative pools total **98**.

A non-None count selects only the with-count partition and formats `n` in Persian
digits. Zero is a valid supplied count. Omitting the count selects only the original
without-count partition; no count is invented and no `{n}` can leak. Six new entries
were chosen rather than the minimum four because the existing five-pick exclusion
requires more than five variants to remain strict rather than degrade to fallback.
Existing 92 templates are protected by per-pool SHA-256 fingerprints in tests.

### With participant_count=12

در سایهٔ سپرها، کودکان درّه ماه راه خانه را یافتند؛ ۱۲ مدافع تا واپسین موج کنار دروازه مانده بودند. شیاسیب عقب نشست و آنچه از میدان به یاد ماند، پناهی بود که فرو نریخت.

### Without participant_count

آب دوباره از جوی‌های درّه ماه گذشت. داریوش و سیب سرخ کنار پل ماندند تا آخرین خانواده عبور کند؛ این خاک امروز زیر نگاه پاسدارانش آرام گرفت.

## Validation

Command (isolated test DB, no live Telegram):

```bash
PYTHONPATH="$(dirname "$PWD"):$PWD" \
  sib_TEST_DATABASE_URL="sqlite+aiosqlite:///$PWD/tmp/sibcom_sib_test.db" \
  .venv/bin/pytest tests/invasion_v2 tests/test_invasion_feature_flag.py -q
```

**421 passed, 0 failed in 15.56 seconds.** This runs the full invasion_v2 suite plus
feature-flag tests. The new `test_phase8c.py` contributes **66 cases**, covering:

- Constant/config fallback 2.5 and precedence of stored overrides.
- Conditional upgrade/downgrade, missing rows, idempotence, unrelated-key/flag
  preservation, numeric-string distinctions and Alembic revision linkage/head.
- All seven render APIs plus attack subtypes: exact ID keys, separate invasion
  histories, recent-five exclusion, zero IDs and exact legacy fallback keys.
- Persian participant numbers including zero, with/without-count routing, wide
  inputs without placeholder leaks, and all 92 original strings unchanged.
- Announcement and join call wiring; finalization counting only this invasion's
  attacks exactly once, not reward rows; defeat performing no participant query.

Existing narrative tests now cover optional signatures and correctly supply counts
when forcing a counted template. The six appended templates are also included in
all existing length/script/placeholder checks. Initial old-contract failures were
updated rather than changing production logic to preserve obsolete expectations.
No dependencies were added. No claim is made about the entire unrelated repository
suite, or live MariaDB/Telegram validation.

## Safety and limitations

- `invasion_enabled` remains untouched at the supplied **false** production state.
  No live read was performed to re-audit that value. Tests use isolated test flags;
  migration tests explicitly assert the stored flag stays `'false'`.
- No production or archived `_archive_inv1_*` tables were accessed. Migration 040
  targets only one config key. Existing archive tests run solely on disposable DBs.
- Imaging/compose and assets, circuit breaker, all power resolvers, recovery, and
  applied migrations 036–039 are unchanged from the Phase 8C baseline.
- Scheduler edits are limited to the imported shared constant and replacement of
  its old fallback literal. Scheduler timing and all other logic are unchanged.
- Anti-repetition remains process-local, resets on restart and is not coordinated
  across workers. Supplied IDs scope it by event/invasion; legacy no-ID callers
  retain the documented broader context fallback.
- Participant count is the persisted attack-row count at finalization; it is not
  inferred from rewards or current group membership. Retries read it again.
- Pending historical narratives are not rewritten. Existing battle HP snapshots
  are not rescaled by the new default. Migration deployment is still required.
- Changes are on the session branch only; no push, merge, feature activation or
  production migration execution was performed.
