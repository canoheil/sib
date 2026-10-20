# IV3 — Invasion Random Events: recon & design

**Status: DESIGN ONLY.** This phase changes no code, no migration, no asset and no
configuration. Everything below is a proposal for the implementation phase.

| | |
|---|---|
| Branch | `arena/ac804bde-sib` |
| Base commit | `7ab97e14d81b1473c93f5df434252f5e6d3ac124` |
| Date | 2026-10-20 |
| Migration head | `054_garden_corruption` |
| Scope | random events inside one active Invasion v2 battle (30–45 min) |

**In one paragraph.** The invasion tick is a 30-second, DB-only sweep; it can
therefore materialise a pre-computed per-invasion plan of 5 (30 min) or 7
(45 min) events, anchored to `started_at`/`ends_at`, without any new loop. Each
event is a row in `inv2_random_events`; timed combat modifiers are rows in
`inv2_event_effects` that the existing join handler reads inside its current
transaction, so no process memory or cache is involved. Instant effects use the
same compare-and-swap discipline as the existing HP write, and are deliberately
non-lethal in v1. Interactive chests add `inv2_event_claims` with
`(event_id, user_id)` and `(event_id, slot_no)` unique constraints on top of an
event-row lock, which makes "first three, never twice" a database guarantee
rather than a UI hope. All Telegram work stays post-commit in the presentation
layer, exactly like the invasion announcement.

---

## 0. Goal and non-goals

**Goal.** While an invasion is `ACTIVE`, the group receives 4–7 short, illustrated
"events" that change the battle: Shiyasib adapts (boss buffs), the sky answers the
defenders (player buffs), something falls out of the sky (instant damage), or a
chest appears and the first three people to press a button win something.

**Non-goals for this phase.**

- No new attack per player. The existing rule "one Main Card, one attack per
  invasion" (`uq_inv2_attacks_invasion_user`) stays untouched.
- No change to the spawn cadence (`invasion_interval_seconds = 14400`), the join
  window default (`1800 s`) or the reward table.
- No new background loop. Everything rides the existing 30-second maintenance
  sweep, exactly like the invasion tick does today.
- No event can end the battle by itself (see §3.4.3 for why and for the one
  alternative if the product later wants a lethal meteor).

---

## 1. Reconnaissance

### 1.1 The two clocks that already exist

| Clock | Column | Written where | Meaning |
|---|---|---|---|
| Spawn | `inv2_invasions.scheduled_at` | `scheduler._spawn_if_due` (`services/invasion_v2/scheduler.py:118`) | when the row was created (PENDING) |
| Battle | `inv2_invasions.started_at` | `InvasionPresentation.announce` (`services/invasion_v2/presentation.py:363`) | when the announcement photo was **accepted by Telegram** |
| Deadline | `inv2_invasions.ends_at` | same place (`presentation.py:364`) | `started_at + invasion_join_window_seconds` |
| Finalize | `finalized_at` | `scheduler._finalize_due` (`scheduler.py:65`) | terminal timestamp |

This distinction is the whole answer to "how do we time events". A row can sit in
`PENDING` for minutes (or forever, if the group is muted) before the announcement
is sent. `started_at` is the only timestamp that means "players can see this
battle right now", so **the event plan must be anchored to `started_at`, never to
`scheduled_at`**.

Important consequence: `starts_at` is only written after a successful
`send_photo`. If the send fails, the row stays `PENDING` and the next tick retries
(`publish_pending`, `presentation.py:487`). An event plan created from
`scheduled_at` would fire into a group that never saw an announcement.

### 1.2 How the tick actually works

```
bot.py
  └─ MaintenanceService.run_forever(bot)                 services/maintenance/service.py:683
       └─ every 30 s (INTERVAL_SECONDS = 30, line 42)
            run_once()
              ├─ … unrelated DB work (matches, wars, marriage …), each in its own tx
              ├─ if invasion_enabled:                       service.py:549-582
              │    terminal = InvasionScheduler.tick()      service.py:553
              │      ├─ _finalize_due()                     scheduler.py:65
              │      ├─ _deactivate_stale_mutes()           scheduler.py:101
              │      └─ _spawn_if_due()                     scheduler.py:118
              ├─ (post-commit, only if cls._bot is not None)
              │    for (inv_id, status) in terminal: settle + finalize photo
              │    presenter.publish_pending(bot)           presentation.py:487
              └─ …
```

Facts that matter for this feature:

1. **The tick is DB-only.** Its own docstring says so (`scheduler.py:1`), and
   `_telegram` never appears in that module. Telegram work happens after the
   transaction commits, in `run_once`.
2. **The tick interval is 30 s**, not "every X minutes". The *spawn* interval
   (`invasion_interval_seconds`, default 14400 s) only gates how often a new
   invasion may be created. An active battle is never polled per second; it is
   sampled every 30 s.
3. **`_finalize_due` is the only terminal writer in the tick.** Joins are the
   other terminal writer (killing blow, `handlers/inv2_join.py:107-116`). Any new
   HP writer must follow the same compare-and-swap discipline.
4. **`_spawn_if_due` locks every active `Group` row `FOR UPDATE`** before it
   looks at invasions (`scheduler.py:147-152`). The lock direction in the tick is
   therefore `Group → Inv2Invasion`. New work must add locks **after** that
   direction, never before it.
5. **Presentation runs only when a bot object exists** and only for rows that a
   freshly committed tick produced (or that are still `PENDING`, for the
   announcement retry). Tests and CLI runs have `cls._bot is None`.
6. **Startup recovery is separate.** `InvasionScheduler.recover`
   (`scheduler.py:274`) and `InvasionV2Recovery.run_on_startup`
   (`services/invasion_v2/recovery.py`) expire overdue `ACTIVE` rows without
   touching presentation.

### 1.3 Where damage is computed today

`handlers/inv2_join.py:40-137` is the single join path for both buttons:

| Step | Code | Notes |
|---|---|---|
| Gate | `MaintenanceService._invasion_enabled()` | fail-closed flag, 60 s cache |
| Lock | `SELECT … FROM inv2_invasions WHERE id = ? FOR UPDATE` (`:56`) | serializes joins per invasion |
| Validate | status `ACTIVE`, group id, `message_id`, `ends_at > now` (`:58-64`) | rejects old/stale buttons |
| One attack | `SELECT Inv2Attack.id … (invasion, user)` (`:66`) | friendly refusal |
| Sib-buff lock | ascending user locks (`:71-79`) | lock-order discipline for the killing blow |
| Damage | `compute_damage(power, rng)` (`:92`) | **crit chance is hard-coded 10 %** (`damage.py:6-15`) |
| Castle | `apply_castle_buff(damage, buff)` (`:94`) | `int(damage * (1 + buff))` |
| Consumable | `if use_buff: damage *= 2` (`:97-98`) | applied to the castle-buffed hit |
| HP write | compare-and-swap `UPDATE … WHERE enemy_hp_remaining = :observed` (`:107-116`) | `terminal` if 0 |
| Rewards | `settle_outcome(...)` in the same transaction (`:128-129`) | victory only |
| Post-commit | `presenter.on_join(...)`, then `finalize` if terminal | image + HP caption edit |

This is extremely convenient: **there is exactly one place where an event
modifier has to be inserted**, and it already sits inside a transaction that
holds the invasion row lock.

Two hard facts constrain the design:

- **Crit is not a parameter.** `compute_damage` draws
  `rng.random() < 0.10`. Any crit-modifying event needs
  `compute_damage(power, rng, *, crit_chance=0.10)`; the default must keep the
  same RNG consumption so every seeded test stays valid.
- **A player attacks once.** Therefore a timed "+50 % damage" buff only helps
  players who have not joined yet. This is a balance caveat, not a bug (see
  §3.6). Loot and boss-side events do not have this limitation.

### 1.4 Where can active modifiers live?

| Option | Survives restart | Survives 2 workers | Overlapping effects | Verdict |
|---|---|---|---|---|
| Process memory (`dict[invasion_id]`) | ❌ | ❌ | ✅ | rejected |
| Columns on `inv2_invasions` (`dmg_mult`, `dmg_until`, …) | ✅ | ✅ | ❌ (one slot per kind) | rejected |
| Rows in a new table | ✅ | ✅ | ✅ | **chosen** |

The repository already relies on durable rows for things that must not be lost
on restart (`inv2_invasions.announcement_narrative`, `inv2_group_state`,
`daily_chest_claims`). A 60-second config cache exists for the feature flag, but
that is a boolean, not a timed combat modifier; caching a modifier window would
mis-apply the first and last minute of every event.

**Decision.** Three tables, described in §4:

- `inv2_random_events` — the event instance, its schedule, its lifecycle and its
  Telegram message.
- `inv2_event_effects` — the modifiers the join handler reads
  (`damage_multiplier`, `crit_bonus`, `damage_cap_pct`). Instant effects
  (`boss_heal_pct`, `instant_damage_pct`) are applied once by the scheduler and
  never read at join time.
- `inv2_event_claims` — one row per loot winner; the first-three and
  never-twice guarantees live here.

The join handler reads the active effects **inside the existing join
transaction, after the invasion row lock**, with one indexed query on
`(invasion_id, status)` plus a time window. No new lock is taken (plain
`SELECT`), so the existing lock order `Invasion → User` is unchanged.

### 1.5 The interactive chest: what the code already gives us

There is no "first N clickers" primitive in the repo yet, but there are two
patterns we can reuse:

- **Public group button with per-user state**: the group daily chest
  (`handlers/daily_chest.py`, `DailyChestService`) uses one shared message and a
  public button; the `(user_id, claim_day)` unique constraint is the arbiter
  against double clicks and duplicate updates.
- **Serialized counter with a hard ceiling**: the invasion HP write uses
  `UPDATE … WHERE old_value = :observed` and checks `rowcount`.

For "first 3", both are needed: a row lock (or CAS) to decide **who is third**,
and unique constraints to make double clicks and duplicate Telegram updates
harmless even if the process crashes between the callback and the commit.

Callback data is limited to 64 bytes; `inv2ev:loot:<event_id>` is ~25 bytes at
realistic ids. The message-ownership check used by joins
(`inv.message_id == callback.message.message_id`, `handlers/inv2_join.py:63`)
must be repeated for events: `event.message_id == callback.message.message_id`
and `event.group_id == callback.message.chat.id`. That prevents a forwarded
event message from being claimed in another group.

### 1.6 Traps found during recon

These are the things that will silently break if the implementation ignores
them. Each one is a design requirement, not a footnote.

1. **Stale pinned HP after an event heals or damages the boss.** The pinned
   announcement is only rebuilt by `_apply_hp_edit` (`presentation.py:208`),
   which is triggered by joins. A scheduler-side HP change must call
   `schedule_hp_edit(bot, invasion_id)` after commit, otherwise the pinned
   message shows old HP until the next player joins.
2. **`_apply_hp_edit` deliberately refuses `VICTORY`/`DEFEAT`** and re-sends
   `join_keyboard` for live battles. It is safe to reuse, but only for `ACTIVE`
   rows.
3. **Instant effects must not kill the boss.** Transitioning to `VICTORY`
   requires `settle_outcome` (rewards, region flag, counters) in the same
   transaction, and then `finalize`. A meteor that writes 0 HP but leaves
   `ACTIVE` would produce a dead-but-active battle and, later, a defeat
   settlement attempt from `_finalize_due`. **v1 clamps instant damage to leave
   at least 1 HP** (§3.4.3).
4. **Caption limit.** Photos accept 1024 characters; `_caption()` truncates at
   1020 and `_fit_caption()` cuts at a word boundary before appending the status
   line. Event captions must go through the same helpers. Persian text is
   ~2 bytes per character in UTF-8 but Telegram counts characters, not bytes.
5. **`test_imaging.py` pins the asset inventory.** `test_manifest_matches_files`
   asserts exactly 14 manifest entries and `test_all_assets_exist` asserts
   `assets/invasion_v2/*/*` contains exactly the 14 expected files
   (`tests/invasion_v2/test_imaging.py:18-44`). Adding `events/*.jpg` **will
   fail these tests** unless they are updated in the same change.
6. **`verify_assets()` runs at import time** (`imaging/templates.py:92`). Missing
   event art should fail at startup, not when a group is waiting for a message.
7. **Muted groups.** `is_group_available` gates announces and joins. Event
   planning must skip muted groups entirely; otherwise the scheduler applies an
   instant heal in a group that cannot receive the message.
8. **Naive MariaDB datetimes.** Every comparison must pass through `_utc()`
   (`scheduler.py:24`); `ends_at` and `starts_at` come back without tzinfo.
9. **SQLite has no `FOR UPDATE`.** All tests run on SQLite
   (`tests/invasion_v2/test_scheduler.py`). Every critical state change needs a
   CAS or a unique constraint as the real arbiter; the row lock is the
   production fast path, not the correctness proof.
10. **Multi-worker.** Two maintenance workers can overlap (the scheduler's own
    comment at `scheduler.py:143-145` says so). Planning, applying and sending
    must each be idempotent under a double tick.
11. **Config defaults are not seeds.** `ConfigService.get` falls back to
    `CONFIG_DEFAULTS` (`services/config_store.py:432-448`); a migration only
    needs to seed values that an operator must be able to see/edit before the
    first tick. New keys can live in defaults alone.
12. **`InvasionScheduler.tick` returns only terminal transitions.** Do not
    change that contract to return event ids; `run_once` and several tests
    depend on it. Event presentation should read committed rows (the
    `publish_pending` pattern), not receive ids from the tick.

### 1.7 What this phase does not touch

`services/invasion_v2/damage.py` keeps its public behaviour; only an optional
keyword argument is proposed. The spawn cadence, reward table, enemy roster,
narrative pools, pinning, circuit breaker and garden corruption are untouched.

---

## 2. Event catalogue (12 events)

### 2.1 Design rules

- **Percent-of-HP effects scale automatically.** `enemy_hp` is
  `round(avg_group_power × enemy_multiplier × difficulty)` (`enemy.py:43-54`).
  A heal or a meteor expressed as a fraction of `enemy_hp` is therefore fair for
  a 3-player group and for a 20-player group. Flat numbers would not be.
- **Damage multipliers are multiplicative and last.** They combine as a product
  with the castle buff and the sib-buff, never as a sum.
- **Timed effects start when the message is sent**, not when the row is
  processed. This is what the
  `APPLIED → (send) → ACTIVE (starts_at = now)` transition buys us (§3.3).
- **No instant effect can be lethal.** Damage is clamped to `≥ 1` HP.
- **No repeated template inside one invasion**, and at least one loot event per
  invasion whenever the battle lives long enough to reach a second slot
  (§3.2).
- **Every event has a separate effect line** appended after the narrative, with
  Persian digits and the Persian percent sign. The narrative itself stays
  number-free so it never needs templating.
- **Copy style.** Protection and resistance, not graphic injury; the same
  register as `services/invasion_v2/narrative_pool.py`. ZWNJ, Persian
  punctuation, no em dash, no Latin digits inside Persian prose.

Legend: `H = inv2_invasions.enemy_hp`, `h = enemy_hp_remaining`,
`P = card power`, `D = computed damage`.

### 2.2 Summary

| # | Code | Persian name | Category | Effect | Duration | Weight |
|---|---|---|---|---|---|---|
| 1 | `boss_obsidian_armor` | زرهٔ ابسیدینی | boss buff | `D × 0.65` (−35 %) | 180 s | 1.0 |
| 2 | `boss_shadow_heal` | بازسازی سایه | boss buff | `h += 10 % H` (capped at `H`) | instant | 0.8 |
| 3 | `boss_cold_breath` | نفس سرد | boss buff | crit chance 10 % → 0 % | 150 s | 1.0 |
| 4 | `boss_ward` | طلسم سکون | boss buff | no hit > 15 % `H` | 120 s | 0.9 |
| 5 | `blessing_apple` | برکت سیب | player buff | `D × 1.5` | 180 s | 1.2 |
| 6 | `blessing_hawk` | چشم شاهین | player buff | crit +25 pp (10 % → 35 %) | 150 s | 1.0 |
| 7 | `blessing_dawn` | نور سپیده | player buff | `D × 2.0` | 90 s | 0.8 |
| 8 | `meteor` | شهاب‌سنگ | instant damage | `h −= 8 % H` (min 1 HP) | instant | 1.0 |
| 9 | `heavenly_spear` | نیزهٔ آسمانی | instant damage | `h −= 6 % H` (min 1 HP) + crit +10 pp | 90 s | 1.0 |
| 10 | `loot_golden_chest` | صندوقچهٔ زرین | loot | first 3 → 1 × `apple_gold` | 120 s | 1.2 |
| 11 | `loot_apple_sack` | کیسهٔ سیب | loot | first 3 → 40 normal apples | 120 s | 1.2 |
| 12 | `loot_magic_chest` | صندوقچهٔ جادو | loot | first 3 → 1 × `magic_powder` | 120 s | 1.0 |

Category weights for the draw: `boss_buff 0.30`, `player_buff 0.30`,
`instant_damage 0.20`, `loot 0.20`, then uniform inside the category, then the
per-slot phase bias (§3.2).

Exact rows to seed in `inv2_event_effects` (one row per `kind`), plus the
`duration_seconds` the presentation writes when the photo is accepted:

| Code | `duration_seconds` | Effect rows (`kind` = `value`) |
|---|---|---|
| `boss_obsidian_armor` | 180 | `damage_multiplier` = `0.65` |
| `boss_shadow_heal` | 0 (one-shot) | `boss_heal_pct` = `0.10` |
| `boss_cold_breath` | 150 | `crit_bonus` = `-0.10` |
| `boss_ward` | 120 | `damage_cap_pct` = `0.15` |
| `blessing_apple` | 180 | `damage_multiplier` = `1.5` |
| `blessing_hawk` | 150 | `crit_bonus` = `0.25` |
| `blessing_dawn` | 90 | `damage_multiplier` = `2.0` |
| `meteor` | 0 (one-shot) | `instant_damage_pct` = `0.08` |
| `heavenly_spear` | 90 | `instant_damage_pct` = `0.06`, `crit_bonus` = `0.10` |
| `loot_golden_chest` | 120 | none (loot columns: `apple_gold`, 1, max 3) |
| `loot_apple_sack` | 120 | none (loot columns: wallet `apples`, 40, max 3) |
| `loot_magic_chest` | 120 | none (loot columns: `magic_powder`, 1, max 3) |

**Balance basis.** For a typical group with average power 120 and a
`king` enemy (×8) at difficulty 2.0, `H ≈ 1920` and one hit is ≈ 120. Then:
−35 % saves ~42 damage per hit; a 10 % heal restores ~192 (≈1.6 hits); a meteor
removes ~154 (≈1.3 hits); the 15 % ward caps a hit at 288, which only bites
critical/sib-buff hits. Nothing here decides a battle on its own; it changes its
rhythm, which is the point.

### 2.3 Event cards

Each card gives the mechanical definition, the three narrative variants that go
into a new pool, the rendered effect line (template + example), and a
copy-paste image prompt. The shared style block for every prompt is in §9.

---

#### 1. `boss_obsidian_armor` — زرهٔ ابسیدینی

**Effect.** `damage_multiplier = 0.65` for 180 s. Modifier kind:
`damage_multiplier`. Category `boss_buff`. No instant component.

**Narratives.**

1. «از شکاف‌های زرهٔ شیاسیب، سنگ سیاه جوشید و صفحه‌ای براق روی پیکرش بست. تیغه‌های مدافعان بر آن پوست تازه لغزیدند و زخمی نگشودند؛ تنها جرقه بود که پرید.»
2. «شیاسیب دست بر زمین کوبید. تاریکی از خاک بالا آمد و مانند پوسته‌ای دوم بر تنش نشست. هر ضربه اکنون بخشی از نیرویش را در این پوست سیاه گم می‌کند.»
3. «سنگ‌های سیاه از دل زمین برخاستند و به هم جوش خوردند. شیاسیب در زرهٔ ابسیدینی فرو رفت و خنده‌اش از پشت آن شنیده شد؛ اما هر زرهی، در جایی درز دارد.»

**Effect line.** `🛡 آسیب مدافعان ۳۵٪ کمتر شد؛ تا ۳ دقیقه.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. A towering corrupted apple-demon silhouetted against a smoky amber battlefield, jagged obsidian plates growing out of its shoulders and chest like black glass, molten orange cracks glowing between the plates, thorned vines wrapped around its arms. Low-angle heroic composition, rim light in sickly green and amber, drifting ash. Rich botanical detail, cinematic jewel-toned lighting. No text, no logo, no watermark, no UI. Centered subject, dark vignette at the edges.

**File.** `assets/invasion_v2/events/boss_obsidian_armor.jpg` (1024×1024, JPEG q92, sRGB).

---

#### 2. `boss_shadow_heal` — بازسازی سایه

**Effect.** Instant `boss_heal_pct = 0.10`: `h = min(H, h + round(0.10 H))`.
Skipped if `h >= H` at apply time. `applied_amount` stores the real heal.

**Narratives.**

1. «شیاسیب عقب کشید و در سایهٔ خودش پنهان شد. وقتی بیرون آمد، زخم‌ها بسته شده بودند و از ضربه‌ها فقط خطی کمرنگ مانده بود. باید از نو بزنید.»
2. «تاریکی از چهار گوشهٔ میدان به سوی او دوید و بر زخم‌هایش نشست. پیکر شیاسیب دوباره درخشید و نفسش بازگشت؛ چیزی از جانش را با خود آورد.»
3. «سایه‌ها مانند دست‌هایی بر پیکر شیاسیب لغزیدند و شکاف‌ها را دوختند. او سر برداشت و به مدافعان نگاه کرد؛ نگاهش می‌گفت هنوز تمام نشده است.»

**Effect line.** `💚 شیاسیب ۱۰٪ از جانش را بازگرداند: {heal} جان.`
Rendered: «💚 شیاسیب ۱۰٪ از جانش را بازگرداند: ۱۹۲ جان.»

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. The wounded corrupted apple-demon kneeling in a ruined orchard while ribbons of living shadow pour out of the ground and stitch its cracked bark-skin, glowing blue-black seams, dim emerald sparks, floating ash and petals. Somber and intimate, cinematic rim light, jewel-toned palette of deep blue, charcoal and sickly green. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/boss_shadow_heal.jpg`.

---

#### 3. `boss_cold_breath` — نفس سرد

**Effect.** `crit_bonus = -0.10` for 150 s: the roll becomes
`max(0, 0.10 - 0.10) = 0`, so no critical hits. Existing critical markers and
stored `is_crit` semantics are unchanged.

**Narratives.**

1. «شیاسیب نفس کشید و باد سردی از سینه‌اش بیرون زد. تیغه‌ها کند شد و دست‌ها در سرما لرزید. تا دو دقیقه و نیم، ضربهٔ بحرانی در کار نیست.»
2. «نفس شیاسیب آسمان را سفید کرد. شبنم روی زره‌ها یخ بست و مدافعان دیدند تیزی شمشیرهایشان در سرما خاکستری شده است.»
3. «سرمای نفس او تا مغز استخوان رفت. میان این باد، هیچ ضربه‌ای نمی‌تواند به بحرانی‌ترین نقطهٔ پیکرش برسد.»

**Effect line.** `🥶 تا ۲ دقیقه و ۳۰ ثانیه، ضربهٔ بحرانی ممکن نیست.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. The corrupted apple-demon exhaling a vast plume of pale blue frost across a moonlit battlefield, defenders' swords dulling under rime, frost crystals on black thorns, thick breath fog, deep indigo and silver palette with a single faint amber ember in the distance. Cinematic depth, no text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/boss_cold_breath.jpg`.

---

#### 4. `boss_ward` — طلسم سکون

**Effect.** `damage_cap_pct = 0.15` for 120 s:
`D = min(D, max(1, ceil(0.15 H)))`, applied **after** the sib-buff doubling.
This is the anti-burst answer: it does nothing to normal hits and blunts
critical + sib-buff spikes.

**Narratives.**

1. «شیاسیب دایره‌ای بر خاک کشید و واژه‌ای خواند. هوا غلیظ شد و ضربه‌ها پیش از رسیدن به پیکرش، در میانهٔ راه کند شدند.»
2. «طلسمی از دهان شیاسیب بیرون آمد و روی میدان نشست. زمان برای تیغه‌ها کندتر گذشت؛ تا دو دقیقه، هیچ ضربه‌ای بیش از سهمی معین از جانش را نمی‌برد.»
3. «سنگ‌ها از جا کنده شدند و دور او چرخیدند. در این دایره، هر ضربه به دیواری از هوا می‌خورد و تنها بخشی از نیرویش را می‌رساند. صبر کنید تا طلسم بشکند.»

**Effect line.** `⛔ تا ۲ دقیقه، هیچ ضربه‌ای بیش از ۱۵٪ جان شیاسیب را نمی‌برد.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. Concentric rings of glowing runes burned into cracked earth around the corrupted apple-demon, shards of stone orbiting it slowly in mid-air, a translucent dome of green-gold force bending the air, arrows and blades halted at its edge. Dust, embers, high contrast, cinematic jewel-toned lighting. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/boss_ward.jpg`.

---

#### 5. `blessing_apple` — برکت سیب

**Effect.** `damage_multiplier = 1.5` for 180 s.

**Narratives.**

1. «نوری از میان ابرها به میدان افتاد و بوی سیب تازه در هوا پیچید. دست‌های مدافعان سبک‌تر شد و تیغه‌ها درخشان‌تر.»
2. «درّه لرزید و صدایی از آسمان آمد که نام مدافعان را می‌خواند. برکت سیب بر زره‌ها نشست و نیروی هر ضربه را افزون کرد.»
3. «قطره‌ای نور روی سر نیزه‌ها چکید. از آن لحظه، شمشیرها سنگین‌تر و ضربه‌ها کارسازتر شدند. این برکت دیر نمی‌پاید؛ همین حالا بزنید.»

**Effect line.** `⚔️ آسیب همهٔ مدافعان ۵۰٪ بیشتر شد؛ تا ۳ دقیقه.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. A shaft of warm gold light breaking through storm clouds onto a line of defenders' raised swords in a ruined orchard, translucent apple-shaped motes of light drifting down, green leaves and red apples catching the glow, dust motes in the beam, hopeful cinematic composition against a dark battlefield. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/blessing_apple.jpg`.

---

#### 6. `blessing_hawk` — چشم شاهین

**Effect.** `crit_bonus = +0.25` for 150 s (10 % → 35 %).

**Narratives.**

1. «شاهینی از بالای برج پر کشید و بر شانهٔ مدافعان نشست. چشم‌ها تیز شدند و نقطه‌های ضعف شیاسیب، مانند شکاف‌های نور در تاریکی پیدا شدند.»
2. «آسمان چشمی باز کرد. برای چند لحظه، هر مدافع جای دقیق زخم‌های شیاسیب را دید؛ جایی که زره باز است و تیغه می‌تواند به عمق برسد.»
3. «نور سپیده بر تیغه‌ها افتاد و لبه‌هایشان را روشن کرد. حالا هر ضربه می‌تواند به بحرانی‌ترین نقطه برسد؛ فرصت را از دست ندهید.»

**Effect line.** `🎯 شانس ضربهٔ بحرانی از ۱۰٪ به ۳۵٪ رسید؛ تا ۲ دقیقه و ۳۰ ثانیه.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. A great hawk with gold-edged wings perched on a defender's armored shoulder, its eye glowing amber, faint luminous lines tracing the weak seams of the corrupted apple-demon's armor in the mist beyond. Moonlit teal and gold palette, feathers and ash in the air, cinematic depth. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/blessing_hawk.jpg`.

---

#### 7. `blessing_dawn` — نور سپیده

**Effect.** `damage_multiplier = 2.0` for 90 s. The rarest and shortest player
buff; weight 0.8.

**Narratives.**

1. «سپیده از پشت کوه‌ها سر زد و میدان را پر کرد. سایهٔ شیاسیب کوتاه شد و مدافعان، برای یک نفس، دو برابر نیرو گرفتند.»
2. «خورشید از میان ابرها بیرون آمد و بر سر شیاسیب کوبید. هر ضربه‌ای که در این روشنایی زده شود، دو برابر کار می‌کند؛ اما سپیده به سرعت جای خود را به روز می‌دهد.»
3. «نوری طلایی از دل زمین برخاست. دست مدافعان گرم شد و هر ضربه دو برابر شد. نود ثانیه؛ نه یک نفس بیشتر.»

**Effect line.** `🌟 آسیب مدافعان دو برابر شد؛ تا ۹۰ ثانیه.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. A blinding golden dawn erupting from behind jagged mountains and flooding a battlefield, the corrupted apple-demon's long shadow shrinking, defenders as small backlit silhouettes with raised weapons. Warm gold, coral and pale green sky, long dramatic shadows, soft lens glow, epic scale. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/blessing_dawn.jpg`.

---

#### 8. `meteor` — شهاب‌سنگ

**Effect.** Instant `instant_damage_pct = 0.08`:
`h = max(1, h - max(1, round(0.08 H)))`. Non-lethal by design (§3.4.3).
Skipped if `h <= 1`. `applied_amount` stores the real damage.

**Narratives.**

1. «آسمان ترک خورد و سنگی آتشین از دل آن بیرون آمد. شهاب‌سنگ بر پیکر شیاسیب نشست و زمین تا چند قدم لرزید. گرد و خاک که خوابید، بخشی از جانش رفته بود.»
2. «ستاره‌ای از مدار خود بیرون افتاد و راهی میدان شد. برخورد، سکوت را شکست و شیاسیب زیر آتش و سنگ یک قدم عقب رفت.»
3. «شبی که همه فکر می‌کردند آسمان آرام است، شهاب‌سنگ آمد. آتش بر شانهٔ شیاسیب نشست و زره‌اش را سفید کرد. این هدیهٔ آسمان را با ضربه‌های خود کامل کنید.»

**Effect line.** `☄️ شهاب‌سنگ {damage} آسیب زد؛ ۸٪ از جان شیاسیب کم شد.`
Rendered: «☄️ شهاب‌سنگ ۱۵۴ آسیب زد؛ ۸٪ از جان شیاسیب کم شد.»

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. A blazing meteor striking the shoulder of the towering corrupted apple-demon at night, white-hot impact flash, shattered obsidian shards and embers flying, shockwave rings in the dust, defenders bracing in the foreground. Crimson, orange and charcoal palette, extreme drama, cinematic. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/meteor.jpg`.

---

#### 9. `heavenly_spear` — نیزهٔ آسمانی

**Effect.** Instant `instant_damage_pct = 0.06` (non-lethal) **plus**
`crit_bonus = +0.10` for 90 s. Two effect rows on one event; the caption has one
line per component.

**Narratives.**

1. «نیزه‌ای نور از بالای ابرها فرود آمد و در شکاف زرهٔ شیاسیب نشست. زره ترک برداشت و درزهایش برای مدافعان باز شد.»
2. «دستی از آسمان نیزه‌ای پرتاب کرد. نیزه بر سینهٔ شیاسیب نشست و صدای شکستن در تمام درّه پیچید. از آن شکاف، راهی به قلبش باز شد.»
3. «نیزهٔ آسمانی از میان ابرها گذشت و بر شانهٔ شیاسیب نشست. تیغه‌ها اکنون جای شکستن را می‌بینند؛ از این درز استفاده کنید.»

**Effect line.**
`🗡 نیزهٔ آسمانی {damage} آسیب زد؛ تا ۹۰ ثانیه ضربهٔ بحرانی آسان‌تر شد.`
Rendered: «🗡 نیزهٔ آسمانی ۱۱۵ آسیب زد؛ تا ۹۰ ثانیه ضربهٔ بحرانی آسان‌تر شد.»

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. A spear of pure white-gold light descending from parted storm clouds and piercing the cracked shoulder plate of the corrupted apple-demon, molten cracks spreading from the wound, dark green sap and sparks, defenders looking up. Cool blue clouds with warm gold light, dramatic diagonal composition. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/heavenly_spear.jpg`.

---

#### 10. `loot_golden_chest` — صندوقچهٔ زرین

**Effect.** Loot, `loot_item_key = apple_gold`, `loot_amount = 1`,
`loot_max_claims = 3`, window 120 s.

**Narratives.**

1. «صندوقچه‌ای زرین از دل خاک بیرون آمد و در میدان باز شد. سه مدافع می‌توانند پیش از آنکه سایه آن را ببلعد، سهم خود را بردارند.»
2. «زمین ترک خورد و صندوقچه‌ای از دل آن بالا آمد. قفلش خودبه‌خود باز شد؛ سیب‌های طلایی در آن می‌درخشند و تنها سه دست به آن‌ها می‌رسد.»
3. «صدای فلزی از زیر خاک آمد. صندوقچهٔ زرین، یادگاری از مدافعان قدیمی، در میدان ظاهر شد. عجله کنید؛ سایه در راه است.»

**Effect line.** `🎁 سه جایزه برای نخستین سه نفر؛ یک سیب طلایی برای هر نفر.`
Loot status line under the effect: `🏅 برداشت‌ها: {claimed} از ۳`
and, when full, `🏅 برداشت‌ها: ۳ از ۳ · صندوقچه خالی شد.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. An ornate golden chest half-buried in cracked battlefield earth, its lid opening with warm light, three glowing golden apples inside, thorned vines and ash around it, a dark creeping shadow at the frame edge. Rich gold, emerald and charcoal palette, treasure glow, cinematic. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/loot_golden_chest.jpg`.

---

#### 11. `loot_apple_sack` — کیسهٔ سیب

**Effect.** Loot, wallet reward `apples = 40`, `loot_max_claims = 3`, window
120 s. Uses `AppleWalletService.earn` (ledger), not `ItemService` (normal Apples
are currency, not an inventory item).

**Narratives.**

1. «کیسهٔ آذوقه‌ای از دست سربازی افتاده بود و کنار دیوار ماند. کیسه باز شد و سیب‌های سرخ در خاک پخش شدند. سه مدافع می‌توانند سهم خود را بردارند.»
2. «کاروانی که در تاریکی گم شده بود، کیسه‌ای سیب جا گذاشت. بوی سیب تازه در میدان پیچید و میان دود، جایی برای نفس کشیدن باز کرد.»
3. «کیسهٔ سیب از پشت دیوار پیدا شد؛ یادگاری از کاروانی که هرگز نرسید. سیب‌ها سالم مانده‌اند. نخستین سه نفر، سهم خود را بردارند.»

**Effect line.** `🍎 سه جایزه برای نخستین سه نفر؛ ۴۰ سیب برای هر نفر.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. A torn burlap sack spilling glossy red apples onto a dusty stone street at dusk, a fallen caravan cart and a broken wheel in the background, smoke and low amber sunlight, defenders' boots approaching. Warm red, ochre and brown palette, still-life focus with cinematic depth. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/loot_apple_sack.jpg`.

---

#### 12. `loot_magic_chest` — صندوقچهٔ جادو

**Effect.** Loot, `loot_item_key = magic_powder`, `loot_amount = 1`,
`loot_max_claims = 3`, window 120 s.

**Narratives.**

1. «صندوقچه‌ای کوچک با نوشته‌های نورانی میان سنگ‌ها پیدا شد. درونش پودری می‌درخشد که باغ‌های خفته را بیدار می‌کند؛ سه مدافع می‌توانند آن را بگیرند.»
2. «درخششی آبی از زیر خاک بیرون زد. صندوقچهٔ جادو باز شد و پودری درخشان در هوا چرخید. سه نفر می‌توانند پیش از محو شدنش، سهم خود را بردارند.»
3. «باد پودری درخشان را از دوردست آورد و در صندوقچه‌ای کهنه نشاند. این پودر، باغ‌های پژمرده را زنده می‌کند. نخستین نفرها، سهم خود را بردارند.»

**Effect line.** `✨ سه جایزه برای نخستین سه نفر؛ یک پودر جادویی برای هر نفر.`

**Image prompt.**

> Square 1:1 1024×1024 painterly dark-fantasy game illustration. A small ancient chest covered in glowing turquoise runes, open, with luminous blue-green powder spiraling above it like slow smoke, cracked stone and moss around, a withered garden branch reviving at the edge of the frame. Deep teal, jade and violet palette, magical glow, cinematic. No text, no watermark, no UI. Centered, dark vignette.

**File.** `assets/invasion_v2/events/loot_magic_chest.jpg`.

---

### 2.4 Persian copy notes

- All thirty-six narratives were written with ZWNJ (می‌شود، تیغه‌ها، صندوقچه‌ای،
  پیکرش) and Persian punctuation. No em dash, no Latin digits, and no Arabic
  yeh/kaf codepoints.
- Narrative is number-free on purpose; the effect line carries the numbers.
  Keep it that way: it makes the copy lint-clean and lets the same prose be used
  for any magnitude.
- Effect lines use `fa_number()`/`fa_digits()` (`services/invasion_v2/farsi.py`,
  `services/sib_claim.py`) and `٪`, never `%`.
- Durations use a new helper: `fa_duration(seconds)` → «۳ دقیقه»,
  «۲ دقیقه و ۳۰ ثانیه», «۹۰ ثانیه». Do not hand-format them in the templates.

Lint command for the implementation phase (the pool is Python, so extract the
strings first):

```
python .agents/skills/persian-writing/scripts/fa_lint.py --check <extracted-strings.txt>
```

---

## 3. Scheduling

### 3.1 The event clock

```
started_at                                                       ends_at
 0s       60s       8min       14min      21min      28min        30min
  │         │         │          │          │          │            │
  ├─ head ──┼── e1 ───┼── e2 ────┼── e3 ────┼── e4 ────┼── e5 ──┬───┤
  │  60s    │         │          │          │          │        │   │
  └─────────┴─────────┴──────────┴──────────┴──────────┴────────┴───┘
                                                           tail 120s
```

Let `W = ends_at - started_at` (1800 s by default, 2700 s if the join window is
set to 45 min), `guard_head = 60 s`, `guard_tail = 120 s`.

```
usable    = W - guard_head - guard_tail
K         = clamp(floor(usable / 360) + 1, min_events=3, max_events=7)
base_gap  = usable / (K - 1)
jitter    = max(0, min(30, base_gap - min_gap, max_gap - base_gap))
gap(i)    = base_gap + U(-jitter, +jitter)
offset(i) = guard_head + sum(gap(1..i))          # offset(0) = guard_head
```

The jitter is bounded by the configured gap limits, so the cadence promise holds
even at the edges: for 30 min, `base_gap = 405 s` and `jitter = 15 s`
(390–420 s, i.e. 6.5–7 min); for 45 min, `base_gap = 420 s` and `jitter = 0`
(exactly 7 min). The last gap absorbs the accumulated rounding so that
`offset(K-1) = W - guard_tail`; since the jitter bound already leaves room for
that adjustment, no gap leaves `[min_gap, max_gap]` and no event lands in the
final two minutes.

The supported window is 30–45 minutes. Above 45 minutes the 7-event cap would
force gaps longer than `max_gap`; a longer window needs a higher cap (or a
higher `inv2_event_max_gap_seconds`) and is deliberately out of scope.

Defaults:

| Window | `usable` | `K` | Average gap | Events |
|---|---|---|---|---|
| 30 min (1800 s) | 1620 s | 5 | 405 s = 6.75 min | e1 ≈ 1 min, e5 ≈ 28 min |
| 45 min (2700 s) | 2520 s | 7 (cap) | 420 s = 7.0 min | e1 ≈ 1 min, e7 ≈ 43 min |

That keeps the cadence in the 6.5–7 minute band for both window lengths and
never schedules an event in the last two minutes, where a 2-minute buff would
be wasted.

Config keys (defaults in `services/config_store.py`):

| Key | Default | Meaning |
|---|---|---|
| `inv2_events_enabled` | `false` | kill switch, seeded off for the first deploy (§6); the master `invasion_enabled` still gates everything |
| `inv2_event_min_gap_seconds` | `300` | hard floor between two events |
| `inv2_event_max_gap_seconds` | `420` | soft ceiling used by the jitter clamp |
| `inv2_event_guard_head_seconds` | `60` | never fire before the announcement settles |
| `inv2_event_guard_tail_seconds` | `120` | never fire near the deadline |
| `inv2_event_min_per_invasion` | `3` | guarantee floor |
| `inv2_event_max_per_invasion` | `7` | cap |
| `inv2_event_loot_window_seconds` | `120` | chest claim window |
| `inv2_event_loot_max_claims` | `3` | "first N" |
| `inv2_event_loot_apples` | `40` | apple-sack amount |
| `inv2_event_weights` | `{}` | optional per-template weight overrides; `0` disables a template without a deploy |

### 3.2 Choosing the events

The plan is drawn **once per invasion**, when the first tick sees
`status = ACTIVE AND started_at IS NOT NULL AND ends_at > now`.

1. **Slot order.** Compute K offsets (§3.1). Slot 1 is the earliest.
2. **Eligibility at plan time.**
   - `boss_shadow_heal` only if `h < H`.
   - `boss_obsidian_armor`, `boss_cold_breath`, `boss_ward` only if `h > 0`.
   - Loot templates always eligible (the chest is the engagement hook).
   - A template already used in this invasion is excluded (no repeats).
3. **Weighted draw.** Weight = `catalog_weight × phase_bias(slot)`, where
   `phase_bias` is 1.5 for `player_buff`/`loot` in the first half and 1.5 for
   `boss_buff` in the second half. `inv2_event_weights` overrides the catalog
   weight; `0` removes the template.
4. **Forced variety.**
   - If slot 1 would be a boss buff and a loot/player template is available,
     reroll once with the boss templates excluded.
   - If no loot has been planned by the last slot and the battle still has at
     least 2 minutes, the last slot is forced to the highest-weight eligible
     loot template.
5. **Deterministic replay.** The plan is stored as rows, so a crash between
   "planned" and "committed" is just no plan; the next tick replans from
   scratch. `UNIQUE(invasion_id, slot_no)` makes two overlapping workers
   produce exactly one plan.

The draw uses `random.Random()` in production and an injectable `Random(seed)`
in tests, exactly like `_spawn_if_due`.

### 3.3 Lifecycle

```
PLANNED ──(due, battle ACTIVE, group available)──► APPLIED ──(photo accepted)──► ACTIVE ──(ends_at)──► CLOSED
   │                                                  │                             │
   │ battle ended / muted / ineligible                │ send fails 3×               └─ battle ends ─► CLOSED
   └──────────────────► SKIPPED ◄─────────────────────┘
```

| Status | Meaning | Who writes it |
|---|---|---|
| `PLANNED` | in the plan, not due yet | planner |
| `APPLIED` | due; instant effects committed (if any); waiting for its photo | scheduler |
| `ACTIVE` | photo accepted; `starts_at`/`ends_at` set; buff window or chest window is running | presentation |
| `CLOSED` | timed window elapsed, the battle ended, or a one-shot event was shown (reporting only; the join read already ignores it) | presentation/expiry pass |
| `SKIPPED` | never applied, or the battle ended first | scheduler/expiry pass |

Why `APPLIED` and then `ACTIVE` instead of applying a buff immediately: it makes
the buff window start exactly when the group can see it. The delay between the
two is the same `run_once` cycle (milliseconds to a few seconds), not a full
30 s tick, because `publish_events` is called right after the tick commits.
One-shot events (`duration_seconds` null/0: the two instant-damage events and
the heal) go `APPLIED → (photo accepted) → CLOSED` in the same step, because
they have no window to keep open.

`APPLIED` is also the retry state: a failed photo leaves the row there with
`send_attempts` incremented, and the next `publish_events` retries. After 3
attempts the row is `SKIPPED` and logged. Instant effects are already committed
at that point; that is the same trade-off the invasion announcement already
accepts (the DB commits before Telegram).

### 3.4 Applying effects

Before anything is applied, the event pass re-checks the battle
(`status = 'ACTIVE'`, `h > 0`) and the group (`is_group_available`). A muted or
deactivated group gets no event at all, which is what prevents a "silent heal"
that the group never sees. The presentation pass repeats the check as a
backstop, because a mute can be recorded between the tick commit and the send.

For every due event the pass takes the parent invasion row `FOR UPDATE` first
and then the event row, so the lock direction is `Invasion → Event` — the same
direction as planning and never the reverse. That is also the lock a join takes,
so an instant effect and a killing blow can never interleave halfway.

#### 3.4.1 Boss heal

Under the invasion row lock (the tick takes it), read `h`, compute
`heal = max(1, round(0.10 H))`, `new = min(H, h + heal)`, then:

```sql
UPDATE inv2_invasions
   SET enemy_hp_remaining = :new
 WHERE id = :id
   AND status = 'ACTIVE'
   AND enemy_hp_remaining = :observed
```

`rowcount = 0` means a join or another tick moved the HP; retry once with the
fresh value, otherwise mark the event `SKIPPED`. Record
`applied_amount = new - h`.

#### 3.4.2 Timed modifier

No HP write. Insert the `inv2_event_effects` rows (or use the ones planned with
the event), set `status = 'APPLIED'`, and let presentation set `starts_at` and
`ends_at` when the photo is accepted.

#### 3.4.3 Instant damage

```sql
UPDATE inv2_invasions
   SET enemy_hp_remaining = :new
 WHERE id = :id AND status = 'ACTIVE' AND enemy_hp_remaining = :observed
```

with `new = max(1, observed - max(1, round(pct * H)))`. If `observed = 1` the
event is skipped with no message (nothing to hit).

**Why non-lethal.** Making it lethal means the event becomes a third terminal
writer: it must CAS `status = 'VICTORY'`, call `settle_outcome` (rewards + region
flag + counters) in the same transaction, then trigger `finalize`. That is
implementable, but it duplicates the killing-blow path and changes reward
timing from "a player's hit" to "a random timer". If the product wants it later,
the safe shape is: CAS to `VICTORY`, then return the id in a
`resolved` list that `run_once` settles exactly like a terminal tick result.
Documented as an open question (§8), not implemented in v1.

### 3.5 Where this hooks into the tick

`InvasionScheduler.tick` gains one call after `_spawn_if_due`, in the same
transaction:

```python
terminal = await cls._finalize_due(session, current, stats)
await cls._deactivate_stale_mutes(session, current)
await cls._spawn_if_due(session, current, rng or random.Random(), stats)
await InvasionEventScheduler.tick(session, current, rng or random.Random(), stats)
```

Lock order stays `Group → Invasion → Event`: `_spawn_if_due` already holds the
Group locks, and the event pass only adds Event-row locks after Invasion. It
never locks a Group, so it cannot invert the existing order. (The loot claim
handler locks `Event → User` and never locks the invasion row; see §4.4.)

`run_once`, after the existing terminal settlement and `publish_pending`:

```python
await presenter.publish_pending(cls._bot)
await InvasionEventPresentation.publish_applied(cls._bot)
await InvasionEventPresentation.close_expired(cls._bot)
```

`close_expired` both marks `CLOSED` and removes the chest button/caption from
the group (best effort). It is called every cycle, so a chest whose window ended
while the bot was down is cleaned up on the next start.

`InvasionScheduler.recover` (startup) additionally marks every `PLANNED` /
`APPLIED` event of an expired invasion as `SKIPPED`, so a restart cannot deliver
yesterday's meteor into today's group.

### 3.6 "Guaranteed 3–4" in practice

Two things are worth stating plainly, because they affect the promise:

1. **Events stop when the battle stops.** If a group kills a `soldier` boss in
   two minutes, only the first planned event (≈1 min) fires. This is inherent to
   a one-attack-per-player model, not a defect: `H ≈ avg_power × multiplier ×
   difficulty`, so a group with `≥ difficulty × multiplier` active attackers
   (≈2 × multiplier at the default difficulty 2.0) wins before the second slot.
   The plan front-loads slot 1 for exactly this reason.
2. **Timed buffs mostly serve late joiners.** A player who already attacked
   cannot attack again, so "+50 % damage for 3 minutes" is a call to join *now*,
   not a buff for the already-committed. Loot, boss buffs and instant damage do
   not have this limitation. If the product wants buffs to affect everyone, the
   next phase must introduce a second-attack mechanism (a new `attack_index` and
   a relaxed unique constraint), which is explicitly out of scope here.

For a 30-minute battle with steady participation, the default plan yields
**5 events**; for 45 minutes, **7**. The floor of 3 applies whenever the battle
outlives `guard_head + 2 × gap`.

---

## 4. Database schema

### 4.1 `inv2_random_events` — the event instance

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT / INTEGER (sqlite) | no | PK, autoincrement |
| `invasion_id` | BIGINT | no | FK `inv2_invasions.id`, `fk_inv2_random_events_invasion` |
| `group_id` | BIGINT | no | FK `groups.id`, `fk_inv2_random_events_group`; denormalized for ops queries and the claim check |
| `slot_no` | INT | no | 1..K inside the invasion |
| `template_code` | VARCHAR(48) | no | catalogue key, e.g. `boss_ward` |
| `category` | VARCHAR(16) | no | `boss_buff` / `player_buff` / `instant_damage` / `loot` |
| `status` | VARCHAR(16) | no | `PLANNED` / `APPLIED` / `ACTIVE` / `CLOSED` / `SKIPPED` |
| `scheduled_at` | DATETIME | no | planned offset, UTC |
| `starts_at` | DATETIME | yes | set when the photo is accepted (buff/chest window start) |
| `ends_at` | DATETIME | yes | buff expiry / chest close |
| `announced_at` | DATETIME | yes | Telegram acceptance time |
| `resolved_at` | DATETIME | yes | closed/skipped time |
| `message_chat_id` | BIGINT | yes | Telegram chat |
| `message_id` | BIGINT | yes | Telegram message (also the claim owner check) |
| `narrative` | TEXT | yes | frozen rendered prose; caption edits rebuild from it |
| `applied_amount` | INT | yes | real heal/damage applied (`NULL` for modifiers/loot) |
| `duration_seconds` | INT | yes | effect window (`NULL`/0 = one-shot event, no window) |
| `loot_item_key` | VARCHAR(32) | yes | `apple_gold`, `magic_powder`; `NULL` for wallet apples |
| `loot_amount` | INT | yes | per-winner amount |
| `loot_max_claims` | INT | yes | 3 by default |
| `loot_claimed` | INT | no | default 0, server_default `0` |
| `send_attempts` | INT | no | default 0, server_default `0` |
| `created_at`, `updated_at` | DATETIME | no | `TimestampMixin` |

Constraints and indexes:

```python
UniqueConstraint("invasion_id", "slot_no", name="uq_inv2_random_events_slot")
Index("ix_inv2_random_events_invasion_status", "invasion_id", "status")
Index("ix_inv2_random_events_status_scheduled", "status", "scheduled_at")
Index("ix_inv2_random_events_group_id", "group_id")
```

### 4.2 `inv2_event_effects` — the modifiers the join path reads

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT / INTEGER | no | PK |
| `event_id` | BIGINT | no | FK `inv2_random_events.id`, `fk_inv2_event_effects_event` |
| `kind` | VARCHAR(24) | no | `damage_multiplier` / `crit_bonus` / `damage_cap_pct` / `boss_heal_pct` / `instant_damage_pct` |
| `value` | FLOAT | no | meaning per kind |

```python
UniqueConstraint("event_id", "kind", name="uq_inv2_event_effects_event_kind")
Index("ix_inv2_event_effects_event_id", "event_id")
```

Instant kinds (`boss_heal_pct`, `instant_damage_pct`) document *why* an event
changed HP and are never read by the join path. Timed kinds are read by kind.

### 4.3 `inv2_event_claims` — first-N, exactly once

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | BIGINT / INTEGER | no | PK |
| `event_id` | BIGINT | no | FK `inv2_random_events.id`, `fk_inv2_event_claims_event` |
| `user_id` | BIGINT | no | FK `users.id`, `fk_inv2_event_claims_user` |
| `slot_no` | INT | no | 1..`loot_max_claims` |
| `reward_type` | VARCHAR(24) | no | `apples` / `apple_gold` / `magic_powder` |
| `reward_amount` | INT | no | snapshot of what was granted |
| `claimed_at` | DATETIME | no | UTC |

```python
UniqueConstraint("event_id", "user_id", name="uq_inv2_event_claims_event_user")
UniqueConstraint("event_id", "slot_no", name="uq_inv2_event_claims_event_slot")
Index("ix_inv2_event_claims_user_id", "user_id")
```

The two unique constraints are the final correctness proof:

- `(event_id, user_id)` — a second click by the same user can never insert.
- `(event_id, slot_no)` — a fourth winner can never insert, even if the counter
  logic were wrong.

### 4.4 Join-time read and damage formula

Read (inside the existing join transaction, after the invasion `FOR UPDATE`):

```sql
SELECT e.kind, e.value
  FROM inv2_event_effects e
  JOIN inv2_random_events r ON r.id = e.event_id
 WHERE r.invasion_id = :invasion_id
   AND r.status = 'ACTIVE'
   AND r.starts_at <= :now
   AND r.ends_at > :now
   AND e.kind IN ('damage_multiplier', 'crit_bonus', 'damage_cap_pct')
```

Fold the rows into a small frozen value object:

```
damage_multiplier = product(values)         # 1.0 if none
crit_bonus        = sum(values)             # 0.0 if none
damage_cap_pct    = max(values) or None     # most restrictive cap
```

Then the join pipeline becomes (new parts marked ▸):

```
▸ mods  = await load_active_modifiers(session, inv.id, now)
  base, crit = compute_damage(power, rng, ▸ crit_chance=clamp(0.10 + mods.crit_bonus, 0, 1))
  damage = apply_castle_buff(base, castle_buff)          # unchanged
▸ if mods.any_damage_modifier:
▸     damage = max(1, int(damage * mods.damage_multiplier))
  if use_buff: damage *= 2                               # unchanged
▸ if mods.damage_cap_pct is not None:
▸     damage = min(damage, max(1, ceil(inv.enemy_hp * mods.damage_cap_pct)))
```

**No event active ⇒ neither branch runs ⇒ the arithmetic and the RNG
consumption are byte-for-byte what they are today.** That property is what
keeps every existing join test green.

`compute_damage` gains one keyword-only argument:
`crit_chance: float = 0.10`. The two `rng` calls stay in the same order and the
same count; the docstring's "caller supplies all randomness" contract stands.

### 4.5 The loot claim transaction

Callback data: `inv2ev:loot:<event_id>`.

```
1. parse id, load event FOR UPDATE
2. reject unless status = 'ACTIVE'
3. reject unless ends_at is not None and now < ends_at
4. reject unless event.group_id == callback.message.chat.id
        and event.message_id == callback.message.message_id
5. reject if a claim already exists for (event, user)        ← friendly "already"
6. reject unless the parent invasion is still ACTIVE           ← read only, no lock
7. UPDATE inv2_random_events
      SET loot_claimed = loot_claimed + 1
    WHERE id = :id AND status = 'ACTIVE' AND loot_claimed < loot_max_claims
   rowcount != 1  →  "chest is empty" / "chest closed"
8. claimed  = SELECT loot_claimed WHERE id = :id               ← own update is visible
   slot_no  = claimed
9. INSERT inv2_event_claims(...)                               ← unique guards
10. grant the reward through the canonical service:
      apples        → AppleWalletService.earn(reason="inv2_event_loot",
                       ref_type="inv2_random_event", ref_id=event.id,
                       idempotency_key=f"inv2evt:{event.id}:{user_id}")
      apple_gold /
      magic_powder  → ItemService.grant(session, user_id, key, amount)
11. commit, then answer the callback and edit the chest caption
```

Why three guards instead of one:

| Guard | Protects against |
|---|---|
| `FOR UPDATE` on the event row | two workers deciding "you are third" at the same time (MariaDB) |
| CAS `loot_claimed < max` + `rowcount` | SQLite tests, `close_expired` racing the third click, and any writer that did not take the lock |
| unique `(event_id, user_id)` and `(event_id, slot_no)` | duplicate Telegram updates, retries, a crashed-then-replayed request, and any future code path that forgets the lock |

Lock direction: `Event → User` (the wallet/item write). No path locks a user
row and then an event row, and no path locks the invasion row after the event
row, so this cannot deadlock against `Invasion → User` (join) or
`Group → Invasion → Event` (tick).

After the commit the handler answers with a private callback alert built from
the reward label (for example «🎁 یک سیب طلایی گرفتی!») and asks the
presentation layer to refresh the caption (§5.3). A duplicate click is answered
with «قبلاً از این صندوق برداشته‌اید.» and never touches the wallet.

### 4.6 Migration and model files

- `database/models/invasion_v2_events.py` — the three models, using the existing
  `_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")` convention.
- `database/models/__init__.py` — export the three classes (tests create tables
  explicitly from `Base.metadata`; see `tests/invasion_v2/test_scheduler.py:30-40`).
- `database/migrations/versions/055_inv2_random_events.py` —
  `revision = "055_inv2_random_events"`, `down_revision = "054_garden_corruption"`,
  `mysql_charset="utf8mb4"` on every table, named constraints, `batch_alter_table`
  only if an existing table is touched (none is).
- The downgrade drops the three tables only. No config key is deleted (the
  existing convention is that a key may predate or be customized; see
  `037_inv2_group_members.py`).

### 4.7 Alternatives considered

| Alternative | Why not |
|---|---|
| Columns on `inv2_invasions` (`buff_until`, `buff_mult`) | one slot per effect; cannot represent a damage buff and a crit debuff at once; needs a reset job after every battle |
| One table with nullable effect columns | no history of overlapping events; a new effect kind needs a migration; the join read has to branch on columns |
| In-memory registry | lost on restart; wrong with two workers; tests would need a global reset |
| Reusing `inv2_rewards` for chest prizes | that table is tied to `invasion_id` + `user_id` and is the victory completion marker; reusing it would corrupt the "one reward set per attacker" invariant |
| Storing the plan inside an invasion JSON column | no per-slot status, no unique constraint, no message id, no audit |

---

## 5. Presentation layer

### 5.1 Message shape

Every event is a **photo**, not a text message, so it reads like the rest of the
invasion feed and satisfies the "1024×1024 AI image" requirement:

```
[ photo 1024×1024 ]
caption:
<narrative>

<effect line>
[<loot status line>]

  [ reply_markup: «🎁 باز کردن صندوقچه» (loot only) ]
```

- `parse_mode=None` (same as announcements and joins) so a player name can
  never inject markup.
- Caption is built with `_fit_caption(narrative, "\n\n" + effect_line + …)`
  and goes through `_caption()` before `send_photo`; the existing 1020/1024
  budget is reused unchanged.
- The effect line differs per kind; the exact Persian templates are in §2.3.
- For loot, the status line is recomputed from `loot_claimed` and the last three
  winner names (sanitized with `core.text.plain_display_name`), e.g.
  «🏅 برداشت‌ها: ۲ از ۳ · آرش، نیلوفر».

**The chest button.** Only loot events carry a keyboard:

```python
InlineKeyboardMarkup(inline_keyboard=[[
    success_button(text="🎁 باز کردن صندوقچه", callback_data=f"inv2ev:loot:{event.id}")
]])
```

It is a **public** button, not owner-scoped: like the two join buttons on the
announcement, every member of the group must be able to press it. Ownership is
proved by the message, not by the user: the handler requires
`event.message_id == callback.message.message_id` and
`event.group_id == callback.message.chat.id`. The handler answers every press
privately (`callback.answer`, `show_alert=True`) so the group feed is not
spammed with refusals; successes also edit the shared caption to show the
winner's name (§5.3).

### 5.2 Send lifecycle (commit-after-send, like `announce`)

```
publish_applied(bot):
  ids = SELECT id FROM inv2_random_events
        WHERE status = 'APPLIED' AND message_id IS NULL
        ORDER BY id LIMIT 20
  for id in ids:
      # Mirrors announce(): the row is locked for the duration of ONE metered
      # send, so two workers cannot both deliver the same event.
      row = re-read FOR UPDATE; skip if status != 'APPLIED' or message_id set
      inv = session.get(Inv2Invasion, row.invasion_id)
      if inv is None or inv.status != 'ACTIVE':
          row.status = 'SKIPPED'; commit; continue      # battle ended in the gap
      if not await is_group_available(row.group_id):
          row.status = 'SKIPPED'; commit; continue      # muted/deactivated group
      path = EVENT_ASSETS / f"{row.template_code}.jpg"
      caption = build_event_caption(row)
      kb = loot_keyboard(row.id) if row.category == 'loot' else None
      msg = await _telegram(row.group_id, bot.send_photo, chat_id=row.group_id,
                            photo=FSInputFile(path), caption=caption,
                            reply_markup=kb, parse_mode=None)
      if msg is None:
          row.send_attempts += 1
          if row.send_attempts >= 3: row.status = 'SKIPPED'
          commit; continue
      now = datetime.now(timezone.utc)
      row.message_id = msg.message_id
      row.message_chat_id = msg.chat.id
      row.announced_at = now
      if row.duration_seconds:
          row.starts_at = now
          row.ends_at = now + timedelta(seconds=row.duration_seconds)
          row.status = 'ACTIVE'
      else:
          row.status = 'CLOSED'      # one-shot: shown once, no window
          row.resolved_at = now
      commit
      if row.applied_amount is not None:            # this event moved HP
          schedule_hp_edit(bot, row.invasion_id)    # §1.6 trap 1
```

Notes:

- `_telegram` already meters the circuit breaker and never raises
  (`presentation.py:42`), so event sends inherit the per-group mute behaviour.
- Sending before the DB write is the same trade-off as the announcement: a crash
  in the gap can replay a message. The row is still `APPLIED`; the replay is
  harmless because the effect was already committed and the second message is
  only cosmetic. (The alternative — mark first, send second — loses messages on
  crash, which is worse for a timed buff.)
- The announcement, a join photo and an event photo can land in the same
  minute; that is acceptable. Events themselves are at least 5 minutes apart.

### 5.3 Closing the chest

- `close_expired`: select `ACTIVE` events with `ends_at <= now` or whose parent
  invasion is no longer `ACTIVE`. Loot gets a caption rebuild from the frozen
  `narrative` + status line «صندوقچه بسته شد.» and
  `edit_message_caption(..., reply_markup=None)`; buffs are simply marked
  `CLOSED` (their modifier already stopped being read at `ends_at`).
- After the third claim, the claim handler (post-commit) does the same edit
  immediately: caption «صندوقچه خالی شد.» + keyboard removed. This is the same
  "edit after commit, swallow `message is not modified`" pattern as
  `_edit_announcement_caption` (`presentation.py:178-206`).
- On `finalize` (victory/defeat), call the same close routine for every open
  loot event of that invasion, right after the result photo, so a chest never
  survives its battle with a live button.

### 5.4 HP line freshness

Any event that changes HP (heal or instant damage) must, after its commit, call
`schedule_hp_edit(bot, invasion_id)`. That rebuilds the pinned announcement from
`announcement_narrative` and the new `enemy_hp_remaining`, re-sending the join
keyboard. Without it the pinned message lies until the next player joins.

### 5.5 Wiring and file map (implementation phase)

| File | Change |
|---|---|
| `database/models/invasion_v2_events.py` | new: `Inv2RandomEvent`, `Inv2EventEffect`, `Inv2EventClaim` |
| `database/models/__init__.py` | export the three models |
| `database/migrations/versions/055_inv2_random_events.py` | new tables |
| `services/invasion_v2/events/catalog.py` | the 12 definitions, Persian pools, effect-line builders, image filenames |
| `services/invasion_v2/events/schedule.py` | pure `plan_offsets()`, `choose_templates()`, `fa_duration()` |
| `services/invasion_v2/events/engine.py` | `plan_if_needed`, `apply_due`, `expire_due`, `mark_invasion_terminal` |
| `services/invasion_v2/events/modifiers.py` | pure `Modifiers` dataclass + `load_active_modifiers()` |
| `services/invasion_v2/events/presentation.py` | `publish_applied`, `close_expired`, `close_for_invasion` |
| `handlers/inv2_event.py` | `inv2ev:loot:<id>` claim handler |
| `handlers/inv2_join.py` | read modifiers, pass `crit_chance`, apply the modifier step |
| `services/invasion_v2/damage.py` | optional `crit_chance=0.10` parameter |
| `services/invasion_v2/scheduler.py` | one call to the event engine in `tick`, one cleanup in `recover` |
| `services/maintenance/service.py` | `publish_applied` + `close_expired` after `publish_pending` |
| `services/config_store.py` | the config defaults from §3.1 |
| `services/invasion_v2/imaging/templates.py` | `EVENT_ASSETS`, `event_asset()`, extend `verify_assets()` |
| `tests/invasion_v2/test_imaging.py` | extend `EXPECTED` and the manifest count (trap 5) |
| `handlers/__init__.py` | register `inv2_event_router` |
| `assets/invasion_v2/events/*.jpg` | 12 images + manifest entries |
| `assets/invasion_v2/manifest.json` | version bump; 12 new entries |

---

## 6. Configuration and rollout

1. Ship migration 055 + models + engine + presentation with
   `inv2_events_enabled = false` in `CONFIG_DEFAULTS`. No event is planned
   until both this key and the master `invasion_enabled` are on.
2. Smoke-test in staging, then enable without a restart:
   `ConfigService.set(session, "inv2_events_enabled", True)`.
   The kill switch returns to `false` the same way if anything misbehaves.
3. Smoke test with a staging group:
   - force an invasion (`/inv2_force_spawn`),
   - confirm the announcement starts the battle,
   - wait for slot 1 (≈60–105 s) and check the photo, caption, effect line and
     (for loot) the button,
   - click with three accounts + a fourth (must be refused),
   - click twice with one account (must be refused),
   - check `inv2_random_events` statuses and `send_attempts`,
   - confirm the pinned HP line refreshes after a heal/damage event.
4. Monitor: `inv2_event_tick` log line with counts
   (`planned`, `applied`, `sent`, `skipped`, `failed`), plus the existing tick
   duration metric.
5. Rollback: set `inv2_events_enabled=false`; the planner stops, rows are
   inert, and the join path behaves exactly as today.

---

## 7. Test plan

| Area | Test |
|---|---|
| Schedule | `plan_offsets` returns K in 3..7, first ≥ 60 s, last ≤ W−120, all gaps within [300, 420] for the default window; a 1800 s window gives 5, a 2700 s window gives 7 |
| Selection | no template repeats inside an invasion; weights 0 remove a template; forced loot appears when the draw would leave none; `boss_shadow_heal` is not planned at full HP |
| Planning | two ticks at the same instant create exactly one plan (unique slot); a tick with `invasion_enabled=false` opens no session |
| Apply | a due instant event writes HP once; a second tick does not repeat it; a non-ACTIVE invasion marks it `SKIPPED` |
| Heal | clamps at `H`; `applied_amount` is the real heal; a join between the read and the CAS retries or skips cleanly |
| Damage | clamps at 1 HP; `applied_amount` is the real damage; a lethal-looking 8 % is never lethal |
| Modifiers | `damage_multiplier` product; `crit_bonus` clamps to [0, 1]; `damage_cap_pct` applies after the sib-buff; **no active event ⇒ identical damage and RNG sequence to today** |
| Windows | effects are inactive one second before `starts_at` and one second after `ends_at` |
| Loot | first three succeed; fourth refused; same user twice refused; two concurrent claims never produce slot 4; reward granted exactly once; wallet ledger has one row with the idempotency key |
| Send | a failed photo leaves the row `APPLIED`; the third failure marks `SKIPPED`; a successful retry sends once |
| Close | third claim removes the keyboard; the timer closes it; `finalize` closes it; a claim after victory is refused |
| Restart | startup recovery marks planned/applied events of expired invasions `SKIPPED`; no message is sent for them |
| Migration | upgrade/downgrade on SQLite; all three tables, FKs and unique constraints exist |
| Assets | 12 files exist, are 1024×1024 JPEG sRGB, and the manifest matches (extended `test_imaging`) |
| Persian | `fa_lint.py --check` clean on the thirty-six narratives + effect lines |

---

## 8. Open questions

1. **Lethal meteor?** v1 says no. If yes, it needs the full terminal path
   (§3.4.3) and a product decision about rewarding a timer kill.
2. **Should loot be limited to players who already attacked?** The current
   design is open to every group member, because the point is to pull people
   into the battle. Limiting it would require an `Inv2Attack` lookup in the claim
   transaction.
3. **Reward tuning.** 40 apples / 1 golden apple / 1 magic powder per winner is
   roughly one daily-chest outcome each. The config keys make this tunable
   without a deploy.
4. **Event art budget.** 12 new 1024×1024 images, ~1–2 MB each after
   optimization. This is a deliberate addition to the repository's asset tree;
   `.tinypng.json` hashes and `manifest.json` must be updated together.
5. **Do we want a second attack mechanism** so timed buffs affect players who
   already joined? That is a separate balance phase, not part of IV3.

---

## 9. Appendix — image production checklist

**Shared style block (append to every prompt).**

> Painterly dark-fantasy game art, cinematic jewel-toned lighting, rich botanical
> detail, corrupt-fruit motifs, thorns and charcoal, subtle grain. Square
> composition, centered focal subject, dark vignette at the edges, no text, no
> logo, no watermark, no UI elements, no human faces.

**Negative prompt.**

> text, letters, captions, watermark, signature, logo, UI, frame, border,
> collage, grid, multiple panels, photorealistic human, cute cartoon, low
> resolution, jpeg artifacts, extra limbs, distorted anatomy.

**Technical checklist.**

- 1024×1024 exactly, JPEG quality 92, sRGB ICC profile, no alpha.
- File name equals `template_code.jpg` under `assets/invasion_v2/events/`.
- Add each file to `assets/invasion_v2/manifest.json`
  (`path`, `width`, `height`, `sha256`) and bump `version` (invalidates the
  composed-image cache, which is harmless).
- Update `tests/invasion_v2/test_imaging.py`: `EXPECTED` grows by
  `events/<code>.jpg` for all 12 codes, and the count assertion changes from 14
  to 26.
- Extend `templates.verify_assets()` so a missing event image fails at import.
- Record `.tinypng.json` hashes if the optimization pipeline is rerun.
- Inspect the final set as a contact sheet at phone scale (the images are shown
  at ~360 px wide in a Telegram group) before merging.

---

## 10. Summary of decisions

| Decision | Choice |
|---|---|
| Event clock | `inv2_invasions.started_at` + `ends_at`, planned lazily on the first ACTIVE tick |
| Cadence | 5 events / 30 min, 7 / 45 min, min gap 5 min, first at ~60 s |
| Modifier storage | durable rows (`inv2_random_events` + `inv2_event_effects`), read inside the join transaction |
| Randomness | one weighted draw at plan time, no repeats, forced loot slot, injectable RNG |
| Instant effects | non-lethal, CAS HP writes, `applied_amount` recorded |
| Loot | `inv2_event_claims` with `(event,user)` and `(event,slot)` uniqueness + row lock + CAS counter |
| Delivery | post-commit photo send, `APPLIED → ACTIVE`, retry ×3, close buttons on expiry/finalize |
| Lock order | `Group → Invasion → Event → User`, unchanged for existing paths |
| Kill switch | `inv2_events_enabled`, master `invasion_enabled` still gates everything |
