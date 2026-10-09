# Invasion: four-wave battles

This document describes the current Invasion behaviour after the four-wave
redesign. Legacy two-wave events created before the redesign keep their
original timeline; see [Legacy compatibility](#legacy-compatibility).

Code map: `services/maintenance/service.py` (scheduling, tick, settlement
step), `services/invasion.py` (timeline, join, defense, resolution),
`services/invasion_rewards.py` (settlement), `services/invasion_telegram.py`
(presentation), `handlers/invasion.py` (callbacks), and migration
`031_invasion_four_wave_battles`.

## 1. Scheduling and activation

- Slots are UTC multiples of `invasion_occurrence_interval_seconds`
  (default 14400: 00:00, 04:00, … 20:00 UTC).
- A slot starting at `S` has a battle window `[S, S + W)`, where
  `W = invasion_wave_count × invasion_wave_duration_seconds` (default
  4 × 300 = 1200 s).
- Boundary behaviour (half-open window):
  - at exactly `S`: the battle activates;
  - at `S + W − 1 s`: the battle still activates;
  - at exactly `S + W`: the slot is closed and nothing is created.
- A battle is **created already ACTIVE** by the first maintenance tick inside
  its window. `started_at` is that tick's time, and it is announced then.
- Nothing is pre-created. Future slots never exist as rows, and no advance
  announcement is ever sent.
- Missed slots are skipped, never backfilled. A restart that finds a window
  closed creates nothing for it.
- Idempotency: the occurrence key `sib-invasion:v1:moon_descendants:group:{g}:slot:{S}`
  is unchanged and unique. Repeated or concurrent ticks create at most one battle.
- Overlap: scheduling for a Group locks its row, then refuses to create a new
  battle while any unfinished battle of that Group exists (counted as
  `invasions_skipped_overlap`).
- Config validation: each value is range-checked and falls back to its
  default with a warning. The battle must fit twice inside one cadence slot
  (`2W ≤ interval`), because a battle may activate as late as the end of its
  window and run a full window longer. If that fails, the default
  wave configuration is used, and the cadence is widened only if the defaults
  still do not fit.

## 2. Four-wave timeline

With activation time `A` and wave length `w` (default 300 s):

| Wave | Starts | Deadline |
|---|---|---|
| 1 | `A` | `A + w` |
| k | `A + (k−1)·w` | `A + k·w` |
| N (final) | `A + (N−1)·w` | `A + N·w` (battle end) |

- A wave boundary is committed when `now ≥ deadline`. The wave ends at its
  **deadline**, and the next wave starts at that same deadline. `ended_at`
  and `started_at` follow the grid, not the tick time, so a tick that runs a
  few seconds late does not drift the timeline.
- Waves are persisted in the existing `invasion_waves` table. No schema
  change is needed for N waves.
- The final wave's end resolves the battle exactly once. Repeated ticks,
  restarts, and concurrent workers all observe `FINISHED` and do nothing.

## 3. Heartbeat and outage recovery

- `invasions.heartbeat_at` is set at activation and refreshed by every
  maintenance pass and every battle action on that battle, in the same
  transaction as the state change.
- Gap `g = now − heartbeat`. When `g > 30 s` (`HEARTBEAT_TOLERANCE_SECONDS`),
  the excess `e = g − 30` is a candidate compensation. Ordinary 5-second cadence
  jitter is never compensated.
- **Downtime budget (cap).** Each battle may receive at most
  `downtime_compensation_max_seconds` of compensation **in total**, across all
  of its outages. The default is **1200 s (20 minutes)**
  (`invasion_downtime_compensation_max_seconds`). It is snapshotted into the
  battle at activation, so a later config change never rewrites a battle in
  progress.
- **Cumulative amount, derived not stored.** Wave `k` is nominally due at
  `started_at + k × wave_seconds`. The compensation already granted is
  `current_deadline − nominal_deadline`. It is computed from persisted fields, so
  it survives restarts, and no counter or migration is needed.
- **Within budget** (`already + e ≤ budget`): `e` is added to the active wave's
  deadline and the battle's next transition. The remaining playable time at the
  last heartbeat is preserved, minus at most 30 seconds per genuine stall.
- **Over budget** (`already + e > budget`): the battle is **finalized as
  interrupted**, not extended (see below). Nothing is added to any deadline.
- A completed wave is never reopened. Only the active wave's deadline changes.
- Idempotent: the heartbeat is set to `now` in the same transaction, so a repeat
  at the same instant adds nothing, and an over-budget battle is terminal.
- Recovery reuses the normal maintenance step (`RecoveryService` →
  `MaintenanceService.run_invasion_cycle`), so startup and periodic maintenance
  are identical.

### Exact cap semantics

| Total compensation after this stall | Result |
|---|---|
| ≤ budget (for example 1198 s, 1199 s, or exactly 1200 s) | Restored; battle continues |
| > budget (for example 1201 s) | Battle interrupted; `ended_at` = last heartbeat |

Example with the default: the heartbeat is at 60 s. A 1230 s gap (1200 s of
compensation) still restores, and the battle resumes with the 210 s that were
left. A 1231 s gap (1201 s) interrupts the battle.

### Interrupted battles (terminal)

An over-budget battle is finalized in one transaction, reusing the existing
voided shape:

- `status = FINISHED`, `outcome = NULL`, `ended_at` = the last heartbeat (the
  last instant the battle was known to be running). Every wave is closed.
- It is **not a defeat**. No outcome is recorded, so combat resolution never
  runs for it.
- It is **non-rewardable**. `rewards_settled_at` is stamped in the same
  transaction, and settlement only selects an approved outcome.
- Telegram shows one final notice that says the battle closed without a result
  because of a long bot outage, with no reward.
- Its Group is released at once. The overlap guard no longer sees it, so the
  Group's current window can open its own battle on the next tick. Missed
  occurrences are never backfilled.
- A join or Brace that arrives after the stall is refused by the normal closed
  responses. That transaction is rolled back, but the next maintenance tick
  derives the same terminal state from the persisted heartbeat.

### Limitations (explicit)

- Each genuine stall loses up to 30 seconds of playable time by design.
- The budget is per battle, so a battle that is interrupted early does not
  affect later battles. A flaky bot can still exhaust one battle's budget and
  lose that battle.
- Legacy two-wave events have no heartbeat and are **not** compensated and
  never interrupted by this rule. A deploy during a legacy battle does not
  restore its remaining time.
- A bot that is alive but whose maintenance loop is stalled is treated the same
  as downtime, because the heartbeat cannot tell the two apart.

## 4. Participation

- **Join**: allowed while the battle is ACTIVE and before its final deadline
  (`A + N·w`). The window is exactly the battle, so participation closes at
  battle end. A join is unique per (battle, player) by a database constraint
  and the row lock.
- Joining is **not** defending. It creates a participant record with zero
  contribution. The join confirmation says so.
- A join arriving after a silent stall first restores the battle (section 3),
  so it can still succeed if the restored window is open.
- Joining before activation is impossible: a battle does not exist until it
  activates.

## 5. Defense

- Action: `brace` (the only action code). A participant may defend **once per
  wave**, so at most `N` actions per participant per battle.
- Enforcement, in layers:
  1. exact-key replay detection (the key is a hash of the Telegram callback id);
  2. a locked pre-check for an existing action in the same wave;
  3. a **database uniqueness rule** on `(participant_id, wave_id, action_code)`
     (migration 031) as the atomic backstop. A race that passes the pre-check
     fails at insert and is reported as "already used".
- Deadline: an action is accepted only while `now < D_k` for the current wave.
  Stale-wave callbacks are rejected with the existing stale-wave feedback.
- Contribution: `1 + floor(power_snapshot / 10)` from the Main Card snapshot
  taken at join. A player without a Main Card contributes `1` per wave. This is
  the existing rule, unchanged.

## 6. Enemy strength and balance

Enemy power for a four-wave battle:

    E = floor( (base + min(matches_played, cap)) × N / 2 )

with defaults `base = 100`, `cap = 100`, `N = 4`, so `E ∈ [200, 400]`. Each wave
receives `E / N` (the remainder goes to the earliest waves). Dividing by the
legacy wave count of 2 means **each wave keeps the strength of a legacy wave**.

Outcome thresholds are unchanged: `TOTAL_VICTORY` if `D ≥ E`,
`NARROW_DEFENSE` if `100·D ≥ 75·E`, otherwise `DEFEAT`.

Representative values (computed from the implemented formulas):

| Group matches played | Enemy power `E` (4 waves) | Legacy `E` (2 waves) |
|---|---|---|
| 0 | 200 | 100 |
| 50 | 300 | 150 |
| 100 or more | 400 | 200 |

Per-player contribution is `c = 1 + floor(P/10)` per wave, where `P` is the
Main Card power snapshot: power 18 (seeded L1 average) gives `c = 2`; power 58
(L5) gives `c = 6`; power 108 (L10) gives `c = 11`. Over four waves, a player
contributes at most `4c`: 12 at the lowest seeded L1 power and 48 at L10.

For the same participation, the four-wave outcome is **identical** to the
legacy two-wave outcome in every scenario checked (5, 10, and 20 participants,
three card tiers, three activity levels). Example: 10 L1 players braced in every wave give
`D = 80` against `E = 200`, or `D = 40` against legacy `E = 100`. Both are
`DEFEAT`.

Important: this preserves the existing balance; it does not judge it. The same
scenarios show that all-L1 groups of up to 10 players lose and all-L10 groups
win. That is the legacy design's property, and whether it is the intended
difficulty is an open product decision (section 10).

## 7. Telegram lifecycle

Each battle sends a small, predictable set of messages, never one per tick:

| Moment | Message | Notifies |
|---|---|---|
| Activation | Battle screen (photo), pinned quietly | Yes |
| Each wave 2..N start | One short notice "موج k از N آغاز شد" | Silent |
| Battle end | One result notice with outcome and reward state | Yes |

- The pinned battle screen is edited in place after each stage. Edits do not
  notify.
- Each delivered stage is recorded by compare-and-set in
  `invasions.presentation_stage`. A retry, restart, or recovery never repeats a
  notice that was delivered. A failed send is retried after a 60-second
  throttle, and the throttle does not delay the next wave.
- Feedback to callbacks is immediate (join, brace, duplicate, expired wave, and
  battle-ended messages).

## 8. Golden Apple rewards (settlement)

- **Currency: real Golden Apples.** Each reward is an inventory grant of
  `apple_gold` (🌕 سیب طلایی) through the canonical `ItemService.grant`, which
  increments the player's `user_items` row under a row lock. The Apple wallet
  (`apple_wallet`) is never touched.
- **Eligibility: defenders only.** A participant is paid for an Invasion only if
  at least one **persisted successful defense action** exists for that
  participant in that Invasion (`invasion_defense_actions`). Joining alone never
  qualifies. Rejected, duplicate, expired, and failed actions persist no such row,
  so they never qualify. The check reads the defense-action records directly and
  never infers defence from aggregates. Legacy events use the same records and
  are never assumed to have defended.
- **Amounts** per eligible defender, from configuration (read at settlement):
  total victory `invasion_reward_total_victory_golden_apples` = **10**, narrow
  defense `invasion_reward_narrow_defense_golden_apples` = **5**, defeat
  `invasion_reward_defeat_golden_apples` = **1**. Contribution, Brace count, and
  Main Card state do not change the amount.
- **Trigger**: the settlement step of each maintenance cycle, after lifecycle
  work in `run_invasion_cycle`. Startup recovery runs it too. It settles each
  FINISHED battle with an approved outcome and no `rewards_settled_at` marker.

Exactly-once guarantees:

- Settlement locks the Invasion row. The `rewards_settled_at` marker is checked
  **under that lock**, so a concurrent worker blocks and then sees a committed
  marker.
- All item grants and the marker are flushed in the caller's transaction and
  commit together or not at all (`get_session` rolls back on error). A failed
  grant leaves the battle pending, with no items granted and no marker. A later
  cycle retries it and pays each defender once.
- A repeat run, a restart, or a concurrent worker finds the marker and grants
  nothing (`duplicate = True`).
- Unresolved battles pay nothing. Interrupted battles (outcome `NULL`) are never
  selected. A battle with no defenders is marked settled and grants nothing.
- Settled history is never paid retroactively. Migration 031 marks already-FINISHED
  historical battles as settled (`rewards_settled_at = ended_at`). No code path
  paid Invasion rewards before this redesign, so this only closes them out.
- **Trade-off**: there is no per-player payout ledger for these items. Idempotency
  rests on the atomic marker-with-grants commit and the Invasion row lock. Clearing
  `rewards_settled_at` by hand would re-grant items. Use the auditable
  `ITEM_GRANTED` log events (user, item, quantity, new total) for accounting.
- The Telegram result shows the configured amount for the battle's outcome, so
  an operator who changes that amount after settlement changes only the displayed
  text, not what was paid.

## 9. Configuration

| Key | Default | Meaning |
|---|---|---|
| `invasion_occurrence_interval_seconds` | 14400 | Slot cadence (UTC) |
| `invasion_wave_count` | 4 | Waves per battle (≥ 1) |
| `invasion_wave_duration_seconds` | 300 | Length of each wave (≥ 60) |
| `invasion_enemy_base_power` | 100 | Base enemy strength per legacy wave |
| `invasion_enemy_activity_bonus_cap` | 100 | Cap on the matches-played bonus |
| `invasion_reward_total_victory_golden_apples` | 10 | 🌕 per defender, total victory |
| `invasion_reward_narrow_defense_golden_apples` | 5 | 🌕 per defender, narrow defense |
| `invasion_reward_defeat_golden_apples` | 1 | 🌕 per defender, defeat |
| `invasion_downtime_compensation_max_seconds` | 1200 | Total downtime compensation per battle; over it, the battle is interrupted (section 3) |
| `invasion_event_duration_seconds` | 1200 | Superseded; kept so old rows stay valid |

Battle parameters are snapshotted when a battle activates. Later changes never
rewrite a battle in progress.

## 10. Legacy compatibility

- Any event whose `event_config` is not schema v2 (`timing_basis: activation`)
  keeps the original two-wave, absolute-deadline timeline, unchanged.
- Legacy SCHEDULED events (created before the deploy) still start on their
  original timeline while their first window is open. If that window has already
  closed, they are **voided** (`FINISHED` with no outcome): players never had an
  opportunity, so no defeat is recorded and no reward is paid.
- Legacy presentation is unchanged (edits only). Legacy SCHEDULED events are never
  announced before they start.
- Legacy events are **not** given the downtime budget or interruption rule and
  keep their original timeline. They are paid by the same defender-only rule,
  judged from their defense-action records. Legacy battles that finished before
  this change are already settled and are not paid.
- Migration 031 is additive: three nullable columns on `invasions`, one index,
  one unique constraint on `invasion_defense_actions`, and the historical
  settled backfill. Apply the migration before deploying the code. This change
  adds **no** further migration.

## 11. Decisions and open items

Resolved by the refinement request:

1. **Currency**: real Golden Apples (`apple_gold`), not Apple wallet currency.
2. **Eligibility**: defenders only (persisted successful defense action).
3. **Outage cap**: 20 minutes of total compensation per battle, configurable;
   over the cap the battle is interrupted without an outcome or reward.

Still open:

4. **Difficulty.** Legacy balance is preserved exactly; the formulas and
   thresholds were not changed. Small groups of low-activity (level-1) players
   can still lose the battle, while high-activity groups win it. See
   `analysis/invasion_balance_simulation.py`. Whether that is intended is a
   balance decision.
5. **Notification volume.** Each battle sends one notifying start message, three
   silent wave notices, and one notifying result message (6 battles per Group
   per day). Confirm this is acceptable.
6. **Legacy battles mid-deploy.** Legacy battles active during the deploy are not
   compensated. Deploy between battles, or accept it.
