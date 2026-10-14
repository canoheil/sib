# Phase 1 — Invasion v2 design specification

**Status: IMPLEMENTED — see [PHASE-7-ROLLOUT.md](PHASE-7-ROLLOUT.md).** This document preserves the original Phase 1 design reference (including its historical proposals); Phase 1 itself did not create tables, seed data or enable the flag. Later phases implemented V2 and archived V1. No Phase 7 deployment or live flag enablement is implied. See [region seeds](PHASE-1-REGION-SEEDS.md), [narrative samples](PHASE-1-NARRATIVE.md), [asset plan](PHASE-1-ASSETS.md), and [Phase 7 report](PHASE-7-REPORT.md).

## World

- Land: **اواریا (Ovarya)**.
- Main enemy: **شیاسیب** (Shiyasib) — corrupted apples, the main villain; terrifying and merciless.
- There are **10 regions**, each with a controlling faction.

## Regions

| code | name_fa | controlling_faction |
|---|---|---|
| moonfall_valley | درّه ماه | فرزندان ماه |
| emerald_forest | جنگل زمرد | فرزانگان |
| sunfield_plains | دشت آفتاب | امپراتوری |
| diamond_peaks | کوهستان الماس | امپراتوری |
| lotus_lake | دریاچه نیلوفر | فرزندان ماه |
| ash_desert | بیابان خاکستر | بی‌طرف |
| mirror_city | شهر آینه | امپراتوری |
| whispering_caves | غار زمزمه‌ها | بی‌طرف |
| star_harbor | بندر ستاره | امپراتوری |
| dark_marsh | تالاب تاریک | فرزندان ماه |

### Exact seed data (reference only; do not execute until Phase 2)

The statement assumes the proposed Phase 2 table/column names `regions(code, name_fa, controlling_faction)`; **no table or seed is created in Phase 1**. The exact same statement is preserved separately in [PHASE-1-REGION-SEEDS.md](PHASE-1-REGION-SEEDS.md).

```sql
INSERT INTO regions (code, name_fa, controlling_faction) VALUES
  ('moonfall_valley', 'درّه ماه', 'فرزندان ماه'),
  ('emerald_forest', 'جنگل زمرد', 'فرزانگان'),
  ('sunfield_plains', 'دشت آفتاب', 'امپراتوری'),
  ('diamond_peaks', 'کوهستان الماس', 'امپراتوری'),
  ('lotus_lake', 'دریاچه نیلوفر', 'فرزندان ماه'),
  ('ash_desert', 'بیابان خاکستر', 'بی‌طرف'),
  ('mirror_city', 'شهر آینه', 'امپراتوری'),
  ('whispering_caves', 'غار زمزمه‌ها', 'بی‌طرف'),
  ('star_harbor', 'بندر ستاره', 'امپراتوری'),
  ('dark_marsh', 'تالاب تاریک', 'فرزندان ماه');
```

## Enemy types

| type | name_fa | power_multiplier | spawn_weight |
|---|---|---:|---:|
| soldier | شیاسیب پیاده | 1.0 | 0.40 |
| shadow | شیاسیب سایه | 1.6 | 0.20 |
| commander | شیاسیب سردار | 3.0 | 0.20 |
| brute | شیاسیب سنگین | 5.0 | 0.15 |
| king | شیاسیب پادشاه | 8.0 | 0.05 |

## Enemy HP

```text
enemy_hp = round(
    avg_card_power_of_all_group_members
    × type_multiplier
    × difficulty_multiplier
)
```

`difficulty_multiplier` defaults to **2.5** (Phase 8C; originally 4.0), configurable via `system_config`. The enemy type's `power_multiplier` is the `type_multiplier` in this formula. `avg_card_power_of_all_group_members` must come from `GroupPowerResolver.average_main_card_power(group_id=..., session=...)`. If it returns `None` (or no resolver is configured), scheduling fails closed and does **not** substitute a guessed number. The concrete resolver is implemented in Phase 4.5; its observation-based definition of active group membership and fail-closed semantics are documented below.

## Join and attack mechanics

- Join window: **300 seconds**, configurable.
- Each user joins **once**, using their **main card** from their profile.
- **One attack per user per invasion.**
- **Only users who joined receive rewards.**

### Damage

```text
base_damage   = main_card_power
random_factor = uniform(0.85, 1.15)
is_crit       = rand() < 0.10
damage        = round(base_damage × random_factor × (2 if is_crit else 1))
```

## Victory and defeat

- **Victory:** `enemy_hp_remaining <= 0` → **all joiners** get rewards.
- **Defeat:** timer expires and `enemy_hp_remaining > 0` → region marked **lost**.
- On the next spawn, a lost region has a higher chance to be targeted again.

## Rewards

Each joiner on victory receives:

- **3 × سیب طلایی** (always).
- **10%** chance: **1 × سیب الماسی**.
- **2%** chance: **1 × سیب ابسیدینی**.

On defeat: **no rewards**.

## Spawn policy

- At most **one invasion per group at a time**.
- Spawn interval: `invasion_interval_seconds` (default **3600 seconds = 1 hour**).
- Round-robin across active groups.
- Region selection: **70% current region** (prefer lost regions), **30% random**.

## Circuit breaker

- Per group, track `consecutive_failures` on Telegram send errors.
- On **any successful send**, reset to **0**.
- On **>= 3** failures: set `muted_until = now + 24h`; skip the group until then.
- If muted and `now > muted_until + 7 days`: set `is_active = 0`.

## Narrative direction

- **Epic, serious, cinematic.** No English in user-facing text.
- Example join narrative (Persian):

  > «داریوش به خط حمله ملحق شد و ضربه‌ای به‌شدت محکم به خط مقدم شیاسیب
  > زد! ارتش شیاسیب برای لحظه‌ای کنترل اوضاع را از دست داد.»

- Never use the old dry format: no more «نتیجه نهایی تهاجم» tables.
- Forty varied Persian references (five per event) are in [PHASE-1-NARRATIVE.md](PHASE-1-NARRATIVE.md); runtime text must also insert the actual user and main-card names safely.

## Config keys (`system_config`)

| key | default | type |
|---|---:|---|
| invasion_enabled | false | bool |
| invasion_join_window_seconds | 300 | int |
| invasion_interval_seconds | 3600 | int |
| invasion_difficulty_multiplier | 2.5 | float |
| invasion_max_active_per_group | 1 | int |
| invasion_circuit_breaker_threshold | 3 | int |
| invasion_mute_hours | 24 | int |
| inv2_member_active_window_days | 30 | int (Phase 4.5) |

These were **design defaults** in Phase 1. Migration 037 now inserts `inv2_member_active_window_days = 30` if absent, without touching `invasion_enabled`. Its description is “Days after which a group member is considered inactive.” The existing `system_config` table has only key/value/timestamps, so this description lives in migration/code/docs, not in a DB description column.

## Decisions needed before implementation (do not infer defaults)

1. Does joining itself consume the user's single attack and deal damage, or is attack a separate post-join action? The phrase “Join — normal/critical/very high damage” in the narrative request suggests a combined event, but the mechanics do not specify one.
2. What is the defeat timer's duration/start point relative to the 300-second join window, and how should bot downtime be handled (expired with defeat, void, or a separate terminal state)?
3. What is the source of a group's “current region,” and exactly how does “prefer lost regions” affect the 70% current-region / 30% random selection without changing those probabilities?
4. **Resolved in Phase 4.5:** `DbGroupPowerResolver` requires a valid selected Main Card for *every* recently observed member, otherwise it returns `None` and skips spawn. Power is read at spawn time from the current card definition + level, then snapshotted indirectly into `enemy_hp`; later profile edits do not recompute an invasion already scheduled. See the addendum below. Phase 4 originally installed no resolver.
5. Does “all joiners” include a joiner who never attacks? Are the 10% and 2% reward rolls independent **per joiner**, or per invasion? The text specifies each joiner but not roll correlation.
6. Does `now > muted_until + 7 days` apply literally on a later scan even though normal scheduling skips a muted group only until `muted_until`? Define when the seven-day deactivation check runs and whether consecutive failures persist after mute expiry.
7. Is the neutral literal `بی‌طرف` a faction record or a display value? The table is not created until Phase 2; decide the column representation then. Also confirm whether round-robin advances at each interval globally or per group.

## Phase 4.5 implementation addendum — observed group power

- Migration `037_inv2_group_members` (after `036_invasion_v2`) adds only
  `inv2_group_members`: BIGINT auto-ID, FKs to `users.id`/`groups.id`, unique
  `(user_id, group_id)`, individual FK indexes and `(group_id, last_seen_at)`
  index, non-null UTC `last_seen_at` plus creation/update timestamps. Downgrade
  removes the table; existing configuration values are deliberately retained.
- A single `GroupMembershipMiddleware` instance is registered on **both**
  message and callback outer middleware in `bot.py`, ahead of rate limiting.
  A group/supergroup update with a real user queues one write per user/group
  per 60 seconds, with at most 256 pending writes. It never awaits the DB in
  an incoming handler; errors (including unregistered FK rows) log at DEBUG
  and allow retry on the next update. The atomic upsert only advances
  `last_seen_at`, never rewinds it. No channel, private-chat, bot or anonymous
  activity is inferred to be a human group member. This is *observed activity*,
  **not** a Telegram member-list API; silent members cannot be discovered.
- `DbGroupPowerResolver.average_main_card_power` reads observations within
  `inv2_member_active_window_days` (default **30**); zero observations means
  unknown (`None`). For **every** active member it calls the same Main Card
  selection logic as the existing profile (`PlayerCard.is_favorite`, first ID
  for legacy duplicate flags) and derives displayed power via
  `card_attributes(card, character).power` (character base + 10 per level above
  1). A missing Main Card, missing character, invalid level/stats, invalid
  window, or DB error returns `None` for the whole group. It does not drop
  incomplete members from the divisor, use party composition, or guess power.
  Only observed members are in scope; no claim of complete Telegram roster.
- At startup `bot.main` registers `DbGroupPowerResolver` before recovery and
  logs `inv2 power resolver: DbGroupPowerResolver`. Registration does **not**
  enable `invasion_enabled`: existing flag checks still govern all inv2 ticks
  and recovery. Phase 5 still owns Telegram announcements/activation.
- An optional, guarded backfill from `match_players` uses the latest historical
  `created_at`/`updated_at` per non-bot user/group, retaining the timestamp's
  age. It never fabricates a Main Card or treats old participation as recent.
  Use `python -m sib.scripts.backfill_inv2_group_members --dry-run` before a
  confirmed write; it is not part of startup.

## Archived tables (Phase 7 implementation)

Migration `039_archive_inv1_tables` renames the five original V1 tables to `_archive_inv1_invasions`, `_archive_inv1_waves`, `_archive_inv1_contributions`, `_archive_inv1_defense_actions`, `_archive_inv1_participants`; no V1 table is dropped. Its downgrade restores the old names. The old ORM mappings have been removed, rather than kept as writable mappings against the archives. The existing V2 tables (`inv2_*`) remain separate. Migration 039 adds nullable `delivered_at` to `inv2_invasions`. Preserve the historical migration chain and read-only balance artifacts in `archive/`; verify row counts/foreign keys on a DB copy before any production migration.

## Crash safety (Phase 7 implementation)

The V2 scheduler is DB-only; after a committed tick the presenter sends photos. Successful VICTORY photo delivery stamps `delivered_at`; at startup the gated recovery expires overdue ACTIVE rows (without rewards/messages), retries VICTORY rows whose `delivered_at` is NULL through the circuit breaker, and logs reward/attack and group counter inconsistencies without changing them. Telegram send and DB commit cannot be atomic: a crash between them can produce a duplicate final photo. DEFEAT photo replay is not included. See the [rollout guide](PHASE-7-ROLLOUT.md) and [phase report](PHASE-7-REPORT.md) for limitations and checks. Do not infer that the production `invasion_enabled` flag is false merely from source defaults; no Phase 7 activation was performed.
