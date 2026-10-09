# V1 Invasion balance — repository-grounded analysis

> **Status (superseded for new battles).** This analysis describes the legacy
> two-wave V1 battles, which keep their original timeline. New battles are
> four-wave; see `docs/invasion-four-wave-battles.md` for the current timing,
> balance formula (enemy power scaled by waves / 2, so each wave keeps the legacy
> strength), and reward rules. Correction: the default occurrence cadence is 4
> hours (six slots per day), not the 8 hours stated below.

**Scope:** read-only balance analysis as of 2026-10-08. No gameplay, balance, reward, formula, schedule, schema, or production behavior was changed. All runtime conclusions below are based on the checked-in V1 implementation and defaults; conditional scenario results are not claims about current players.

## Bottom line

- Enemy power is driven by the Group's **matches played**, not its headcount or number of Invasion participants, and ranges from 100 to 200. More active participants do not directly make the enemy stronger.
- Player defense comes from the player-facing **Brace** action. Joining alone adds zero. A player may Brace once in each of two Waves, so a participant who is present for both can contribute at most `2 × (1 + floor(snapshot Power / 10))`.
- Under the explicit scenarios below, small/low-Power/low-uptake groups often lose, while sufficient participation or high card levels can make victory certain. For example, 10 Level-1-average participants using both Braces all lose even at enemy power 100; 10 Level-10 participants using both all win through power 200; 50 Level-1-average participants using both all win through power 200. These are scenario results, not forecasts.
- The thresholds do create meaningful Narrow Defense cases in the model. But there is no credible single overall win rate: the repository does not provide live distributions for Group activity, participation, card levels/Main Cards, or Brace uptake.
- Defaults pay every persisted participant 10 / 5 / 1 Apples for Total Victory / Narrow Defense / Defeat, independent of their card or contribution. This makes total issuance depend on both outcome and participant count, and permits non-contributing participants to receive the same reward.

## What the implementation does

### Enemy power and Waves

The maintenance rule is `100 + min(max(int(matches_played or 0), 0), 100)`. The activity snapshot is taken when the Invasion is scheduled; Group size is not an input to this expression. Thus 0, 25, 50, 75, and 100+ matches produce enemy power 100, 125, 150, 175, and 200. `GroupStats` stores a `matches_played` counter, but the inspected repository fixtures are not a live activity distribution. (Sources: `services/maintenance/service.py:303-315,346-354`; `database/models/group_stats.py:23-27`.)

V1 creates two Waves. The default event lasts 20 minutes, divided into two 10-minute Waves; the default occurrence interval is 8 hours (three slots per day per active Group). Total enemy power is split as evenly as possible, with any odd point assigned to Wave 1:

| Enemy power | Wave 1 + Wave 2 | Minimum defense for Narrow Defense | Minimum for Total Victory |
|---:|---:|---:|---:|
| 100 | 50 + 50 | 75 | 100 |
| 125 | 63 + 62 | 94 | 125 |
| 150 | 75 + 75 | 113 | 150 |
| 175 | 88 + 87 | 132 | 175 |
| 200 | 100 + 100 | 150 | 200 |

The Narrow minimum is the smallest integer `D` satisfying `D × 100 >= enemy_power × 75`; Total Victory starts at `D >= enemy_power`. The engine resolves on the **sum of contributions across Waves**, using integer comparisons: below the Narrow minimum is Defeat. (Sources: `services/maintenance/service.py:206-208,318-354`; defaults in `services/config_store.py:135-140`; `services/invasion.py:1616-1667`.)

### Main Card snapshot, progression, and Brace

On joining, the service snapshots the selected Main Card's level and attributes. Later card changes do not alter that event snapshot. Card Power is its Level-1 Power plus 10 for every level above Level 1; levels run from 1 through 10. Speed and Intelligence do not enter the Brace formula. (Sources: `services/invasion.py:678-692,736-780`; `services/card_progression.py:24-27,90-97`.)

| Seeded card | Power at L1 / L5 / L10 | Brace per Wave at L1 / L5 / L10 |
|---|---:|---:|
| Red Apple | 22 / 62 / 112 | 3 / 7 / 12 |
| Green Apple | 16 / 56 / 106 | 2 / 6 / 11 |
| Golden Apple | 20 / 60 / 110 | 3 / 7 / 12 |
| Moon Apple | 14 / 54 / 104 | 2 / 6 / 11 |

These four seeded Level-1 Powers span 14–22 (mean 18). The means at Levels 5 and 10 are 58 and 108. Those are seed-definition facts—not card ownership, selection, or player-strength statistics. (Source: `data/characters_seed.json:3-10,23-30,43-50,63-70`.)

The only registered V1 defense action is `brace`; the user-facing handler calls it. Its exact contribution is `1 + floor(persisted power_snapshot / 10)`. A missing Main Card snapshot is treated as Power 0, so that Brace still contributes 1. One Brace is allowed per participant per active Wave: at most two in this two-Wave event, assuming the participant joined in time for both. Joining does not itself contribute. The lower-level `record_contribution` API accepts caller-supplied positive amounts; it is not the player-facing defense action modeled here. (Sources: `services/invasion.py:40-51,1114-1143,1252-1294,1404-1426`; `handlers/invasion.py:415-422`; test fixtures in `tests/test_invasion_defense_actions.py:132-224,230-279`.)

### Outcomes and Apple settlement

Default rewards are 10 Apples for Total Victory, 5 for Narrow Defense, and 1 for Defeat; runtime configuration can override them. Settlement pays each persisted participant the selected outcome amount, without requiring a Main Card or positive contribution. An event with no participants issues no participant rewards. (Sources: `services/config_store.py:128-140`; `services/invasion_rewards.py:35-39,151-201,237-248`; fixture explicitly covering no-card/zero-contribution settlement in `tests/test_invasion_rewards.py:144-180`.)

At 50 participants, defaults therefore represent 500 / 250 / 50 Apples for a Total Victory / Narrow Defense / Defeat. If the same 50 people joined all three daily slots and all were Total Victories, the upper bound would be 30 Apples per person and 1,500 per Group per day. This is arithmetic under the defaults, not observed issuance or a claim that groups fill all slots.

## Scenario matrix: method and uncertainty

`analysis/invasion_balance_simulation.py` generates `analysis/invasion_balance_scenario_matrix.csv` (5,000 rows). It uses the repository's card-progression helper, seeded character data, V1 scheduling constants, Brace constants, and default reward values. It does not open a database, call Telegram, or mutate application state. It exactly enumerates the discrete card/action choices specified below (no Monte Carlo sampling); displayed probabilities are rounded, and are **conditional on these synthetic assumptions**.

- Group sizes: 1, 2, 3, 5, 10, 20, 30, 50. Target participation: 10%, 25%, 50%, 75%, 100%. Because people are whole, the scenario count is rounded half-up; the CSV records the resulting effective rate. The resulting participant counts are:

  | Group size | 10% | 25% | 50% | 75% | 100% |
  |---:|---:|---:|---:|---:|---:|
  | 1 | 0 | 0 | 1 | 1 | 1 |
  | 2 | 0 | 1 | 1 | 2 | 2 |
  | 3 | 0 | 1 | 2 | 2 | 3 |
  | 5 | 1 | 1 | 3 | 4 | 5 |
  | 10 | 1 | 3 | 5 | 8 | 10 |
  | 20 | 2 | 5 | 10 | 15 | 20 |
  | 30 | 3 | 8 | 15 | 23 | 30 |
  | 50 | 5 | 13 | 25 | 38 | 50 |

- Strength proxies from seed data: **weak/new** = the lowest seed Power (Moon Apple, L1, 14); **average proxy** = equal-weight choice among the four seed cards at L1 (mean Power 18); **strong proxy** = equal-weight seed-card choice at L5 (mean 58); **highly developed proxy** = equal-weight seed-card choice at L10 (mean 108); plus **no Main Card** (null snapshot / Power 0). The equal weighting and independent choice per participant are not observed player distributions. “Average” is a label for this proxy only.
- Brace profiles: no action; each participant independently has a 50% chance to use one Brace total; everyone uses exactly one total; independent 50% uptake per participant in each Wave (0/1/2 Braces with probabilities 25%/50%/25%); everyone uses both Braces. The 50% cases are illustrative uptake scenarios, **not measured realistic rates**. The two-Brace case assumes participants can act in both Waves; late joiners cannot retroactively Brace in a past Wave.
- Enemy power points: 100, 125, 150, 175, 200. Outcomes use the production integer thresholds. Defense is total defense across both Waves; “contribution per participant” is the expected total divided by the fixed participant count. Apple rewards per participant are blank for zero-participant cases because nobody is paid.

### Participation and group size

The table below holds strength to the Level-1 average proxy and assumes both Braces per participant. At enemy power 150, more participants move the same Group from Defeat through Narrow Defense to Total Victory:

| Group size / target rate | Participants (effective rate) | Expected total defense | Expected per participant | Total / Narrow / Defeat at enemy 150 | Expected Apples per participant |
|---|---:|---:|---:|---:|---:|
| 50 / 10% | 5 (10%) | 25 | 5 | 0 / 0 / 100% | 1.00 |
| 50 / 25% | 13 (26%) | 65 | 5 | 0 / 0 / 100% | 1.00 |
| 50 / 50% | 25 (50%) | 125 | 5 | ~0 / 99.27% / 0.73% | 4.97 |
| 50 / 75% | 38 (76%) | 190 | 5 | 100% / 0 / 0 | 10.00 |
| 50 / 100% | 50 (100%) | 250 | 5 | 100% / 0 / 0 | 10.00 |

For the same average Level-1 proxy, full participation, and two Braces each:

| Active participants | Expected defense | Enemy 100: Total / Narrow / Defeat | Enemy 150: Total / Narrow / Defeat | Enemy 200: Total / Narrow / Defeat |
|---:|---:|---:|---:|---:|
| 10 | 50 | 0 / 0 / 100% | 0 / 0 / 100% | 0 / 0 / 100% |
| 20 | 100 | 58.81 / 41.19 / 0% | 0 / 0.13 / 99.87% | 0 / 0 / 100% |
| 30 | 150 | 100 / 0 / 0% | 57.22 / 42.78 / 0% | 0 / 57.22 / 42.78% |
| 50 | 250 | 100 / 0 / 0% | 100 / 0 / 0% | 100 / 0 / 0% |

At full attendance in this average-Level-1/full-Brace proxy, group sizes 1, 2, 3, 5, and 10 yield expected defense 5, 10, 15, 25, and 50 respectively; each is Defeat at all five requested enemy powers. For the smallest Groups, low target rates can round to zero participants, in which case no participant reward is issued.

Intermediate requested points also show the threshold bands: 20 participants at enemy 125 are 94.23% Narrow Defense and 5.77% Defeat; 30 participants at enemy 175 are 99.984% Narrow Defense and about 0.016% Defeat. At the same participant count, group label/unused membership does not change this distribution; only actual participants, their snapshots/actions, and enemy power do. Thus larger Groups help only insofar as they produce more active defenders, while Group match history can raise enemy power independently of the count who show up.

### Player strength

For 10 participants, all eligible for and using both Braces, the conditional result shifts sharply with the seeded-card-level proxy:

| Strength proxy | Expected defense | Enemy 100 | Enemy 150 | Enemy 200 |
|---|---:|---|---|---|
| No Main Card | 20 | Defeat (1 Apple) | Defeat (1) | Defeat (1) |
| Weak/new, Moon Apple L1 | 40 | Defeat (1) | Defeat (1) | Defeat (1) |
| Average proxy, seed mix L1 | 50 | Defeat (1) | Defeat (1) | Defeat (1) |
| Strong proxy, seed mix L5 | 130 | Total Victory (10) | Narrow Defense (5) | Defeat (1) |
| Highly developed proxy, seed mix L10 | 230 | Total Victory (10) | Total Victory (10) | Total Victory (10) |

These are not population bands. The Level-1 average proxy contributes an expected 5 per fully active participant over both Waves (range 4–6); the Level-5 proxy averages 13 (range 12–14); Level 10 averages 23 (range 22–24). Since Brace depends on Power in 10-point steps, small Power changes within a step do not change its amount, while crossing a step does.

### Brace uptake and threshold utility

With 50 Level-1-average participants at enemy power 150, all Brace profiles have the following conditional outcomes:

| Brace profile | Expected total defense | Expected per participant | Total / Narrow / Defeat | Expected Apples per participant |
|---|---:|---:|---:|---:|
| None | 0 | 0 | 0 / 0 / 100% | 1.00 |
| 50% use one Brace total | 62.5 | 1.25 | ~0 / 0.000002 / 99.999998% | 1.00 |
| Everyone uses one Brace total | 125 | 2.5 | ~0 / ~100% / ~0 | ~5.00 |
| Independent 50% uptake per Wave | 125 | 2.5 | 3.31 / 79.41 / 17.28% | 4.47 |
| Everyone uses both Braces | 250 | 5 | 100% / 0 / 0 | 10.00 |

“One Brace each” and “50% uptake per Wave” have the same expected defense here, but different outcome distributions: the latter has more zero- and two-action participants, increasing both Defeat and Total Victory tails. This is a threshold/variance effect, not an implementation random roll. At enemy 200, the same 50%-per-Wave case is 0% Total Victory, 3.31% Narrow Defense, and 96.69% Defeat (about 1.13 Apples per participant).

The three outcome regions are therefore reachable under the modeled V1 rules: e.g. 20 average-proxy participants at enemy 100 split between Total Victory and Narrow Defense; 30 at enemy 150 split between Total Victory and Narrow Defense; and 30 at enemy 200 split between Narrow Defense and Defeat. Conversely, low participation or no Brace action produces Defeat throughout the requested power range in many scenarios. The thresholds are not redundant, but which outcome dominates in live play is unknown.

## Answers, balance verdict, and analytical-only recommendations

- **Can players win?** Yes. Full two-Wave Bracing by 10 L10-proxy players guarantees Total Victory through enemy power 200; 50 L1-average-proxy players also guarantee it at 200. Under these assumptions, 10 L1-average-proxy players cannot reach even the Narrow threshold at enemy 100.
- **Does group size matter?** Not directly in either enemy-power formula or outcome resolution. The contribution is a flat sum, not normalized by headcount. More members matter only when they join and contribute; `matches_played` can independently raise enemy power to its cap.
- **How much participation?** In the Level-1 average/full-Brace proxy at enemy 150, 25 of 50 participants are almost always Narrow Defense; 38 are guaranteed Total Victory. At enemy 200, 38 yield mostly Narrow Defense (about 92.8%) and 7.2% Total Victory; 50 guarantee Total Victory. These are conditional values, not universal cutoffs.
- **How much does strength matter?** A fully braced L10 card provides roughly 4.6 times the expected two-Wave defense of the equal-weight L1 seed mix (23 vs 5 per participant). With 10 participants this moves the example from all Defeat to all Total Victory even at enemy power 200.
- **Are defeat/reward rates excessive?** The scenario matrix cannot answer population frequency. It identifies a sharp conditional gap: 10 Level-1-average participants all lose even at the minimum enemy power, while fully braced 50-person groups or 10 highly developed participants can win at the cap. Actual rates require telemetry.
- **Apple economy:** outcome rewards are per participant, not divided among contributors. At 50 participants, one event issues 500 Apples on Total Victory, 250 on Narrow Defense, or 50 on Defeat; no participants means no payout. The defaults alone do not reveal actual economy pressure without event frequency, attendance, and outcome data.

### Balance verdict

**Overall: uncertain—not enough player data to call the live system healthy, too easy, or too difficult.** In the synthetic space, both extremes appear: the model is **too hard** for 10 L1-average participants even with both Braces (all Defeat across enemy 100–200), but **too easy** for 50 such participants using both Braces (all Total Victory across the range) and for 10 L10-proxy participants (all Total Victory through 200). A middle region gives meaningful Total Victory, Narrow Defense, and Defeat cases. These extremes identify what to validate against live distributions; they do not justify changing values now.

There is no overall “how often” for the three reward tiers without weights for actual activity, participation, player Power, and Brace use. The CSV reports the conditional Total/Narrow/Defeat and expected Apples rates in every modeled cell; averaging all cells equally would assign arbitrary population weights.

### Conditional options only—no tuning recommended yet

If later telemetry confirms a persistent live issue, possible experiments (not changes for this phase) include:

1. **High-match Groups with low defense attendance lose disproportionately:** evaluate enemy scaling against actual defending participation, or damp its activity increment, rather than assuming Group headcount equals defenders.
2. **Highly developed/high-uptake Groups nearly always win:** compare a cap/diminishing return on the Power-based Brace component with a targeted enemy-scaling adjustment. The current simulation only shows the conditional possibility; it does not select a value or establish prevalence.
3. **One outcome dominates live telemetry:** re-evaluate the 75% Narrow threshold only if the observed defense distribution leaves that band rarely occupied. The current modeled scenarios do occupy all three regions, so there is no simulation-based case to change thresholds now.
4. **Apple issuance looks high:** conduct a broader economy review using actual event cadence, attendance, wallet inflows/sinks, and per-player issuance before touching rewards. This report does not establish an economy problem.

**Recommended next step, without tuning:** measure/aggregate (1) `matches_played` per Group, (2) eligible Group size and actual participants/join timing, (3) join-time `power_snapshot` and Main Card level, (4) Brace actions per participant per Wave, (5) defense per participant and outcome by enemy power, and (6) Apples paid per event/player/day. Compare observed distributions before proposing any change. Do not use the unweighted 5,000-row grid as an overall win rate.

## Evidence limits, fixtures, and reproduction

Repository facts above are implementation/default/seed facts. The simulation's card-choice weights, participation rounding, and Brace-uptake patterns are explicit synthetic assumptions. The checked-in tests validate behavior using crafted cases—including a Power-24 Brace fixture and a no-Main-Card Brace—not typical player values (`tests/test_invasion_defense_actions.py:132-224`). Reward tests similarly demonstrate that an uncarded, zero-contribution participant is paid according to the persisted outcome (`tests/test_invasion_rewards.py:144-180`). No live player or Group database was queried; the inspected repository model/test data does not provide real card-level, participation, Brace-usage, or Group-activity distributions.

Run from the repository root, with the project dependencies installed:

```bash
python analysis/invasion_balance_simulation.py
```

This validates the currently imported source constants/helpers and rewrites `analysis/invasion_balance_scenario_matrix.csv`. The script contains no database or Telegram calls. Rows include group size, target/effective participation, participant count, enemy power and narrow threshold, strength/Brace assumptions, expected total/per-participant defense, outcome rates, and expected Apples. The report is this file; the CSV and tool are retained for reproducibility. No tuning is implemented.

**Validation:** the focused Invasion and card-progression tests passed (**120 passed, 9 skipped**). The skips are SQLite row-lock/concurrency tests that require MySQL/MariaDB; no MySQL/MariaDB instance was available, so those locking cases were not verified here. `compileall` passed across the project source/test/analysis directories, and `git diff --check` passed. The deterministic script's source-rule checks passed and regenerated all 5,000 rows. The full suite was not run (optional for this analysis-only change).
