# Phase 1 — Invasion v2 design specification

**Status:** design reference only. Phase 0 keeps the old invasion system disabled behind `invasion_enabled`; this phase does not create tables, change code, seed a database, generate images, or turn the flag on. Subsequent phases should implement this specification without silently changing its names, numbers, or probabilities. See [region seeds](PHASE-1-REGION-SEEDS.md), [narrative samples](PHASE-1-NARRATIVE.md), and [asset plan](PHASE-1-ASSETS.md).

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

`difficulty_multiplier` defaults to **4.0**, configurable via `system_config`. The enemy type's `power_multiplier` is the `type_multiplier` in this formula. `avg_card_power_of_all_group_members` must come from `GroupPowerResolver.average_main_card_power(group_id=..., session=...)`. If it returns `None` (or no resolver is configured), scheduling fails closed and does **not** substitute a guessed number. The concrete resolver is deferred to Phase 4.5.

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
| invasion_difficulty_multiplier | 4.0 | float |
| invasion_max_active_per_group | 1 | int |
| invasion_circuit_breaker_threshold | 3 | int |
| invasion_mute_hours | 24 | int |

These are **design defaults**, not a request to insert or change configuration rows in Phase 1.

## Decisions needed before implementation (do not infer defaults)

1. Does joining itself consume the user's single attack and deal damage, or is attack a separate post-join action? The phrase “Join — normal/critical/very high damage” in the narrative request suggests a combined event, but the mechanics do not specify one.
2. What is the defeat timer's duration/start point relative to the 300-second join window, and how should bot downtime be handled (expired with defeat, void, or a separate terminal state)?
3. What is the source of a group's “current region,” and exactly how does “prefer lost regions” affect the 70% current-region / 30% random selection without changing those probabilities?
4. **Open question (Phase 4.5):** how will the concrete `GroupPowerResolver` use the proposed `inv2_group_members` membership record and existing group-interaction hooks to compute `avg_card_power_of_all_group_members` for members without a main card or groups with no eligible cards? Which card-power snapshot applies if a profile changes mid-invasion? Phase 4 intentionally installs no resolver and cannot spawn.
5. Does “all joiners” include a joiner who never attacks? Are the 10% and 2% reward rolls independent **per joiner**, or per invasion? The text specifies each joiner but not roll correlation.
6. Does `now > muted_until + 7 days` apply literally on a later scan even though normal scheduling skips a muted group only until `muted_until`? Define when the seven-day deactivation check runs and whether consecutive failures persist after mute expiry.
7. Is the neutral literal `بی‌طرف` a faction record or a display value? The table is not created until Phase 2; decide the column representation then. Also confirm whether round-robin advances at each interval globally or per group.
