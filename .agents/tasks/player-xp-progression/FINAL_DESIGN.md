# Sib Account XP & Level Progression — Final Design

**Status:** Design-phase canonical handoff for the next implementation phase
**Inspected:** 2026-10-06
**Scope:** Personal account Level 1–30 and its XP economy only. This document specifies design; it does not implement it.
**Source of truth:** This file is the canonical XP/Level design. If a later design changes a number or rule below, revisit the whole XP economy and update this document before implementation.

---

## A. XP Design Summary

Sib's account Level is a permanent, personal measure of participation across the game—not a currency, card level, group rank, or stat bonus. Players start at **Level 1 with 0 XP**. Playing a real match is the main repeatable source; garden, collection, trade, and social activity add smaller, capped rewards. The first few levels arrive quickly, while the quadratic threshold curve makes the upper levels a long-term goal. This adopts only the broad long-horizon grind philosophy of games such as Warframe; Sib uses its own curve, sources, and caps. Level 30 is a hard cap and a visible achievement, not a gateway to an unbounded XP counter.

The system follows five rules:

1. **Reward completed play, not clicks or money movement.** A completed human match is the primary XP event. Spending or earning Apples, opening a menu, joining a queue, or making a move is not itself XP.
2. **Participation counts; winning adds a modest bonus.** A loss or draw still advances the player, but winning remains better. All human players receive their own award; larger teams do not multiply the award.
3. **The curve is fixed and easy to calculate.** The XP cost for the next level is quadratic. Source values and repeatable-source caps can be balanced independently of the curve.
4. **Repeatable farming has a ceiling.** Each repeatable source has a small eligibility limit, and total XP is capped at 1,000 per player per UTC day. Level 30 has a separate lifetime cap of 87,000 XP.
5. **Level is cosmetic/status progression in this phase.** It does not change card attributes, match outcomes, garden yield, currency rewards, or matchmaking. The profile shows Level, progress to the next level, and a level-aware visual; Level 30 has a distinct MAX presentation.

**Player-facing rule:** “Play Sib activities to earn XP; wins earn a little extra; each higher level takes more XP; Level 30 is the current maximum.” Do not expose source multipliers or internal daily source caps in normal UI. If the global daily limit is reached, a single clear “XP limit reached for today” message is enough.

### Curve rule

For a player currently at Level **L**, the XP needed to advance to **L+1** is:

```text
XP_required(L → L+1) = 50 + 10 × L²       for L = 1…29
```

The player-level function uses integer XP only. Cumulative XP to reach Level **N** is:

```text
XP_to_reach(N) = Σ[50 + 10 × L²] for L = 1…(N−1)
               = 50 × (N−1) + 10 × (N−1) × N × (2N−1) / 6
```

Therefore Level 30 requires **87,000 total XP** from the Level 1 starting point. XP carries across level-ups with no loss. Once the player reaches Level 30, total normal Level XP is clamped at 87,000: no normal XP is awarded, saved, or banked beyond MAX. A later Legendary Rank system must use its own track and must not consume hidden overflow from this one.

---

## B. XP Sources and Existing Sib Activities

### B1. Current project audit

The current codebase has no personal account XP field, account Level, or account-level XP ledger. `User` stores Telegram identity/status; `PlayerStats` stores match counts and outcomes. The current progression-like systems are separate: group win standings, Apple Battle standings, player card levels, per-tree magic levels, garden capacity, seedling inventory, and shared marriage Love. **Player Level 1–30 is new and must remain distinct from all of these.**

| Current activity / system | What the project currently implements | Existing reward, currency, or progression |
|---|---|---|
| Group games | Five standard engines—Rock Paper Scissors, High Card, Tic-Tac-Toe, Connect Four, Battleship—use the party/matchmaking/result flow. Modes include Solo, 2v2, and 4v4 where supported. Normal matchmaking is cross-group. | Completed normal non-bot wins add one seedling to each winning human; losses/draws do not. Group wins and individual match stats are recorded. The current competitive leaderboard is group-win based, not an individual Level system. |
| Apple Battle | A sixth registered engine, using cards and the shared Match lifecycle for current matchmaking/completion. The result is precomputed and persisted before narration. | An eligible non-bot winner gets one seedling through the normal result path and +5 Apples from Apple Battle settlement. A bot match does not receive the normal seedling. Apple Battle also has its own battle statistics/leaderboard. |
| Friendly matches | Same-group parties can play Solo/2v2/4v4; bots may fill open slots. Friendly matches do not affect group wins or seedlings. | Each human participant can receive 10 Apples per eligible completed match, up to five matches / 50 Apples per day by default. No personal competitive match counters are added for friendly matches. |
| Bot fallback / practice | Matchmaking can fall back to game bots; bot participants are real `User(is_bot=True)` records. Bot results have separate personal bot-match counters and do not affect group standings or seedlings. | Apple Battle bot completion currently still has its Apple payout path; bot play is not a normal competitive win. |
| Garden | A garden starts with five plots and can expand to ten. A planted tree takes 24 hours to mature; its first fruit is ready after a further 12 hours. Each mature tree produces one harvest roll per 12-hour cycle; uncollected fruit does not stack. | A normal roll gives 3 wallet Apples. Other outcomes are Golden, Diamond, or Obsidian Apples. Base tree drop weights are 70/20/9/1; magic levels 0–5 shift weight toward special Apples. Planting consumes a seedling. Plot unlocks cost 100/250/500/1,000/2,000 Apples; magic upgrades use Magic Powder. |
| Wallet and “سیب” claim | Normal Apples are a wallet currency with an auditable transaction ledger. The group wake word can claim 5 Apples with a 5-minute per-user cooldown. | There is no daily claim cap in the current rule: a continuously claimed theoretical maximum is 288 claims / 1,440 Apples per 24 hours. This must not become an XP proxy. |
| Cards, shop, and card upgrades | Character cards are collected through the rotating weekly shop; the seed data currently contains four characters and is data-driven. Each player can own at most one of each character. Card Level is 1–10, separate from account Level; a paid upgrade consumes Apples plus a special-Apple material and adds 10 to each of the three derived attributes. The former manual-allocation data is archived; it is not the current upgrade loop. | Collection, purchase, and upgrades are existing progression loops, but wallet spending is not a safe repeatable XP measure. Trading a card transfers its card level. |
| Card trading | Cards can be swapped only at equal card levels, with double confirmation, and the default quota is one trade per user per UTC day. | A completed trade changes card ownership; an offer, cancellation, expiry, or failed trade does not. |
| Marriage and Love | A successful marriage consumes a Wedding Ring (default price 1,000 Apples). Love is shared by the married pair on a 0–100 scale. Accepted flowers add 2 Love; the default limit is one flower per direction per UTC day, with daily Love decay. | A flower costs 30 Apples. Proposals can be rejected/expire; marriages can be divorced/remarried. These are social actions, not currencies to convert directly into XP. |
| Factions, wiki, profile, ads | Factions are lore metadata for characters, not player factions or a bonus system. Wiki/profile/menu browsing and post-match ad interactions are presentation/utility flows. | No current account XP. Do not reward page opens, ad impressions/clicks, or faction labels. |

**Currency and reward snapshot (current defaults):** normal Apples are the wallet currency, not an inventory item. Sources include the 3-Apple normal garden drop, the 5-Apple/5-minute group claim, friendly-match rewards, Apple Battle wins (+5 Apples), and selling eligible items. The default character seed prices are 100–300 Apples; a garden plot unlock costs 100–2,000; Magic Powder costs 40; Flowers cost 30; and a Wedding Ring costs 1,000. Golden/Diamond/Obsidian Apples drop as inventory and sell for 50/100/200 Apples; Flowers and Rings are also sellable for 15/500, while Magic Powder is not sellable. Seedlings are a consumable progression resource, awarded for eligible normal non-bot wins and spent to plant trees. These balances are all independent of proposed account XP.

The audit is based on current models/services and game registration, not older prose summaries where descriptions may be stale. The repository has aggregate counters, but no historical cohort dataset from which to claim observed daily player frequency. Pacing below is therefore an explicit activity model grounded in current match and economy cadences, not a claim about measured population behaviour.

### B2. Proposed XP award table

All repeatable daily limits below use **UTC calendar days** and are per real player account. Bot identities never receive XP. Eligibility requires a successfully completed, result-applied activity; queue creation, party creation, unaccepted matches, cancelled/expired sessions, and duplicate callbacks pay nothing.

| Activity / XP source | Proposed award | Eligibility and anti-farm rule |
|---|---:|---|
| **Normal human-vs-human match** (the five standard engines) | **100 XP** to each human participant, plus **25 XP** to each winner. Loss/draw = 100 XP. | `NORMAL`, non-bot match; it entered ACTIVE and has one applied final result. Maximum **8 eligible normal matches per user per UTC day** across all games/modes. Per player, not per team; Solo/2v2/4v4 do not scale the amount. |
| **Apple Battle** | **Same 100 + 25 XP** as a normal match. | It uses the same Match award and counts against the same eight-match cap. Award once per Match ID; do not add a second XP payment from the battle/narration settlement. Its current seedling/Apple awards stay independent. |
| **Bot fallback / practice** | **20 XP** to each human participant; no win bonus. | Maximum **2 completed bot matches per user per UTC day**. Bot players get zero. A friendly session with fewer than two distinct real human participants is classified here rather than as a full friendly reward. |
| **Friendly match with humans** | **35 XP** to each real human participant; no win bonus. | A completed friendly match must include at least **two distinct real human accounts**. Maximum **5 eligible matches per user per UTC day**, aligned with the current friendly reward cadence. Bots receive zero. If a friendly match is bot-filled but has two or more real humans, it remains in this friendly bucket. |
| **Garden harvest** | **5 XP per successfully harvested plot-cycle.** | Maximum the first **5 plot harvests (25 XP)** per user per UTC day. Each unique `(plot, cycle)` can pay once. A batch “harvest all” counts the individual plots, not as a single click. No XP for waiting for a timer or re-opening a ripe-plot screen. |
| **First planting in a garden plot** | **10 XP** | One-time for a successful planting in each plot; planting consumes the existing seedling. Do not re-award if a future feature lets a tree be removed/replanted. |
| **Unlocking a garden plot** | **15 XP** | One-time for each successfully unlocked plot. No XP for a failed purchase or the Apples spent. |
| **Applying a Magic Tree level** | **10 XP per actual level increase** | One-time per plot and target magic level (0→1 through 4→5). No XP for buying/holding Magic Powder or a failed upgrade. |
| **New character collection** | **25 XP per character** | One-time collection milestone per `(user, character)` after XP launch, whether obtained from shop or a completed trade. No reward for repeat purchase/transfer. At launch, existing owned character pairs are marked already claimed; no historical XP backfill. |
| **Paid card upgrade** | **10 XP per successfully paid card-level step** | One-time per `(user, character, target card level)` for a real 1→10 upgrade. No XP for buying an already-owned character, trading a card, or replaying an upgrade callback. This is distinct from account Level. |
| **Completed card trade** | **10 XP to each human trader** | Only after both confirmations and the ownership swap succeeds. Maximum **one XP-eligible completed trade per user per UTC day**, matching the current default trade quota. No XP for creating, accepting only one side, cancelling, expiry, or failure. |
| **Accepted Flower** | **5 XP to sender + 5 XP to recipient** | Only when the gift is accepted. Maximum **10 flower XP per user per UTC day** across all accepted flowers. Rejected, pending, invalidated, expired, and refunded flowers pay zero. |
| **First accepted marriage** | **50 XP to each partner** | One-time for each user whose first-ever successful marriage occurs after XP launch. No XP for proposal, rejection, expiry, divorce, or remarriage; an account with a marriage record from before launch is ineligible. No historical marriage grants retroactive XP. |
| **Apple claim; Apples or items earned/spent/sold; seedling awards/spends; shop browsing; card sale; menu/wiki/profile opens; ads; faction lore; group leaderboard updates** | **0 XP** | These are currencies, utility events, or existing rewards—not independent gameplay actions. A game win's XP is already in its match award; do not add a second “group win” or “seedling awarded” XP payment. |

### B3. Shared cap and extension budget

- **Global repeatable/earned XP ceiling: 1,000 XP per player per UTC day**, across every source above, including one-time milestones and any future source. If an eligible award would cross the cap, grant only the remaining daily amount. The same rule also clips an award at the Level 30 lifetime ceiling.
- Source-specific limits still apply before the global cap. The global cap is the safety rail for new content and unusual combinations, not a target players should be encouraged to hit.
- At the current 50% match win assumption, eight normal matches average 900 XP; the garden cap adds at most 25. An all-win day reaches the 1,000 global ceiling from matches alone. Bot, friendly, and economy/social loops supplement the core rather than replacing it.
- A future activity can be added by registering one source value, its eligibility event, idempotency key, and repeatable cap. It must draw from the same 1,000-XP daily budget. Do not raise the cap or curve just to accommodate a new feature without a pacing review.

---

## C. Level 1–30 Progression Table

**Interpretation:** “XP needed” is the incremental cost to move from the current level to the next. “Cumulative XP” is lifetime XP from the Level 1 start required to reach the destination level. Level 1 starts at 0 XP. Level 30 has no next-level requirement.

| Current → next level | XP needed for next level | Cumulative XP to reach next level | Milestone / pacing note |
|---:|---:|---:|---|
| 1 → 2 | 60 | 60 | One completed standard match is enough for this first level-up. |
| 2 → 3 | 90 | 150 | |
| 3 → 4 | 140 | 290 | |
| 4 → 5 | 210 | 500 | **Level 5** |
| 5 → 6 | 300 | 800 | |
| 6 → 7 | 410 | 1,210 | |
| 7 → 8 | 540 | 1,750 | |
| 8 → 9 | 690 | 2,440 | |
| 9 → 10 | 860 | 3,300 | **Level 10** |
| 10 → 11 | 1,050 | 4,350 | |
| 11 → 12 | 1,260 | 5,610 | |
| 12 → 13 | 1,490 | 7,100 | |
| 13 → 14 | 1,740 | 8,840 | |
| 14 → 15 | 2,010 | 10,850 | **Level 15** |
| 15 → 16 | 2,300 | 13,150 | |
| 16 → 17 | 2,610 | 15,760 | |
| 17 → 18 | 2,940 | 18,700 | |
| 18 → 19 | 3,290 | 21,990 | |
| 19 → 20 | 3,660 | 25,650 | **Level 20** |
| 20 → 21 | 4,050 | 29,700 | |
| 21 → 22 | 4,460 | 34,160 | |
| 22 → 23 | 4,890 | 39,050 | |
| 23 → 24 | 5,340 | 44,390 | |
| 24 → 25 | 5,810 | 50,200 | **Level 25** |
| 25 → 26 | 6,300 | 56,500 | |
| 26 → 27 | 6,810 | 63,310 | |
| 27 → 28 | 7,340 | 70,650 | |
| 28 → 29 | 7,890 | 78,540 | |
| 29 → 30 | 8,460 | **87,000** | **Level 30 — MAX** |

The last level alone costs 8,460 XP; Levels 25–30 together cost 36,800 XP, about 42% of all XP required. The Level 29→30 cost is 141 times the Level 1→2 cost. A player must earn 87,000 XP for Level 30; even at the full 1,000-XP daily ceiling, the theoretical floor is **87 days**, and that ceiling would have to be reached every day.

### Expected milestone pacing

These are calendar-time estimates at steady-state XP rates. They assume a 50% normal-match win rate, an already planted/mature garden, no XP boosts, and consistent activity. The casual/active ranges allow occasional trade, Flower, or collection XP. The highly active range is bounded by the daily cap. One-time milestone XP is not assumed as daily income. A new account may take a few extra days to earn its first seedling and bring garden plots online.

| Milestone | Cumulative XP | Casual: ~80–100 XP/day | Active: ~260–300 XP/day | Highly active: ~925–1,000 XP/day |
|---:|---:|---:|---:|---:|
| **Level 5** | 500 | ~5–7 days | ~2 days | ~1 day / first sessions |
| **Level 10** | 3,300 | ~33–42 days (5–6 weeks) | ~11–13 days (about 2 weeks) | ~4 days |
| **Level 15** | 10,850 | ~109–136 days (3.5–4.5 months) | ~37–42 days (5–6 weeks) | ~11–12 days |
| **Level 20** | 25,650 | ~257–321 days (8.5–10.5 months) | ~86–99 days (about 3 months) | ~26–28 days |
| **Level 25** | 50,200 | ~502–628 days (17–21 months) | ~168–194 days (5.5–6.5 months) | ~51–55 days (7–8 weeks) |
| **Level 30** | 87,000 | ~870–1,088 days (2.4–3 years) | ~290–335 days (9.5–11 months) | ~87–95 days (about 3 months) |

Activity-rate basis (repeatable core sources):

- **Casual:** about four normal PvP matches per week (4 × 112.5 expected XP) plus about twenty garden harvests per week (20 × 5) = 550 XP/week, or about 79 XP/day.
- **Active:** three normal PvP matches on five days per week plus five garden harvests on those days = about 1,813 XP/week, or about 259 XP/day.
- **Highly active:** eight normal PvP matches every day at 112.5 expected XP each plus five garden harvests = about 925 XP/day. A day with eight wins reaches the 1,000-XP global ceiling.

The repository does not contain enough historical user-activity data to make narrower forecasts. After launch, validate these estimates against actual median/p90 daily XP, the share by source, daily-cap hit rate, and time-to-Level distribution. For the highly active cohort, a sustained Level 30 median below about 90 days suggests rewards are too high; a highly active median materially above four months suggests the system is too stingy or too difficult to engage with. Evaluate casual and active cohorts against the ranges above, not the high-activity guardrail.

---

## D. Balance & Exploit Analysis

| Risk | Why it exists in Sib | Safeguard in this design |
|---|---|---|
| **Apple-claim spam becomes XP farming** | “سیب” awards 5 wallet Apples every five minutes and currently has no daily cap. The theoretical max is 288 claims/day. | Never grant XP per Apple, per claim, or per wallet transaction. Claiming currency remains zero XP. |
| **Fast games / repeated rematches dominate** | Some registered games resolve quickly; match volume can be increased by repeatedly queuing, especially with coordinated groups. | Pay only after a final result; cap normal match XP to eight awards per user/day; count one award per human per Match ID; do not pay for queueing, moves, or group size. Keep the 1,000 XP global ceiling. |
| **Bot farming** | Bot fallback and bot-filled parties can produce repeatable completed results without a real opponent. | Bot/practice pays 20 XP, has no win bonus, and is limited to two matches/day. Never award bots XP. A solo human friendly-vs-bot session uses this reduced bucket. |
| **Friendly match farming** | Friendly matches are same-group and can be arranged cooperatively; existing Apple rewards already have a five-match daily quota. | Lower 35-XP award, no win bonus, maximum five per player/day, require at least two distinct real humans for the friendly rate, and exclude bots. All sources also share the global cap. |
| **Garden harvest spam / investment snowball** | The garden has 5–10 plots and can produce one harvest roll per plot per 12 hours. Special drops and improved magic levels increase economic value. | Award only 5 XP per successful plot-cycle and at most five harvests/day. Pay no XP for passive timer completion, harvest screen opens, Apples/items dropped, magic powder purchases, or garden spending. Permanent garden milestones are one-time and small. |
| **Currency spending becomes pay-to-Level** | Shop cards, card upgrades, plot unlocks, Flowers, Wedding Rings, and items consume Apples/materials; currency can come from repeatable sources. | Never exchange currency amount/spending for XP. Only the listed modest one-time character-collection and successful-upgrade milestones qualify; repeated purchases and ordinary currency transactions do not create repeatable XP. |
| **Trading items back and forth** | Card trades are useful but can be coordinated between accounts. | 10 XP only on a completed double-confirmed swap, capped at one XP-eligible trade per user/day. Offers, cancellations, and failed trades pay zero. New-character collection is separately unique per user+character. |
| **Social alt-account loops** | Marriage and Flower acceptance require another account and can be repeated through new partners or daily gifts. | First accepted marriage is one-time per user; Flowers pay only after acceptance and cap at 10 XP per user/day. Proposals, divorce, rejected gifts, and refunds pay zero. |
| **Duplicate callbacks / retries** | Sib uses asynchronous handlers and persistent result callbacks; duplicate processing must not mint XP. | Every award has a unique `(user, source, source-event)` key and is applied once. A duplicate event must be a no-op. Enforce caps under a per-user lock/atomic update. |
| **Forfeit / timeout manipulation** | A match may finish through a timeout/forfeit rather than an ordinary move sequence. | Require an ACTIVE match and applied final result; no reward for acceptance timeout or cancellation before play. Apply the same per-match and daily limits to started matches that finish by forfeit. Monitor this outcome separately before adding special-case rules. |
| **A new feature silently inflates the curve** | XP sources will expand as Sib adds gameplay. | Require every future source to declare a stable event, award, repeatability, daily cap, idempotency key, and expected XP/day. It shares the 1,000 daily cap. Add the source by budgeting/replacing XP, not by raising the cap by default. |

The intended mix is that completed normal human PvP remains the dominant repeatable path. Garden, social, trade, and collection rewards make the broader Sib ecosystem feel connected, but none can be converted into an unlimited stream. Group wins and existing rewards (seedlings, Apples, cards, Love) continue to function independently; the XP system must not change those economies.

---

## E. Implementation Handoff

### Authoritative rules for implementation

1. **Initialization:** every new account and every existing account at rollout starts at Account Level 1 / 0 XP. Do not backfill from old match counts, group wins, Apple balances, seedling totals, card levels, garden state, trades, Love, or historical marriages. Mark currently owned character pairs as already collected; accounts with a marriage record predating rollout are ineligible for the first-marriage milestone.
2. **Level computation:** use the exact Level 1–30 table/formula in this document: `required(L) = 50 + 10*L*L`; cumulative Level 30 threshold = 87,000. Integer XP only; preserve remainder on level-up.
3. **Source awards:** use the XP source table and eligibility rules in §B2. Match awards are per human participant; a normal match gets 100 participation plus 25 for a win. Apple Battle uses that same Match award exactly once. Friendly/bot rules and the source-specific caps are not interchangeable.
4. **Daily ceiling:** 1,000 total awarded XP per user per UTC day across all current and future sources. Award no more than the remaining daily amount. Source caps are applied before this ceiling.
5. **MAX:** at total XP 87,000, derive Level 30 and stop all normal Level XP. Do not store overflow or silently accrue it for future Legendary Ranks. Show `MAX`, not a fabricated `0 / 0` bar.
6. **Persistence and idempotency:** keep an integer lifetime `total_xp` counter per account and an auditable XP-grant ledger. The ledger should record user, source, stable event/idempotency key, actual amount granted, and timestamp, with a uniqueness constraint preventing duplicate grants. Suggested event keys include `match:{match_id}`, `garden:{plot_id}:{cycle}`, `card_collection:{character_id}`, `card_upgrade:{character_id}:{to_level}`, `trade:{trade_id}`, `flower:{gift_id}`, and a one-time marriage key per account. Lock/update the per-user progression row atomically when enforcing daily/source caps.
7. **Separation:** do not conflate Account Level with `PlayerCard.level`, garden `magic_level`, Love, seedling count, or group standings. Do not add Level stats/currency payouts in this phase. The level visual is presentation-only.
8. **Future source contract:** new XP sources must be registered centrally (data/config or a single XP service registry), with a default of zero/no award until explicitly balanced. They must be idempotent, capped, and budgeted against the existing 1,000-XP daily ceiling. Rebalance source amounts before changing the curve.
9. **Profile:** show Account Level, current-level XP / XP required for the next level, and a visual selected by exact Level. For Level L, display `(total_xp − XP_to_reach(L)) / (50 + 10*L*L)`. Level 30 displays MAX and its capstone visual. XP display must not leak another player's private progression unless the profile's existing privacy rules allow it.
10. **Historical system boundaries:** existing match completion, currency ledgers, and game result application must remain correct if XP award handling retries or fails; an XP-side error must not mint twice or invalidate a finished game. Prefer awarding through the authoritative completion transaction with a durable idempotent record.

### Minimum acceptance tests for the implementation phase

- Verify all 29 incremental thresholds and cumulative milestones against §C, Level 1 starts at 0, XP remainder carries across multiple level-ups, and total XP clamps at 87,000 / Level 30.
- Verify normal win/loss/draw awards, the eight-match cap, bot/friendly classification and exclusions, and the 1,000-XP cap at UTC day boundaries.
- Replay a match, trade, flower, harvest-cycle, card milestone, upgrade, or marriage event and prove no duplicate grant; exercise concurrent award attempts against the same user/cap.
- Verify cancelled, unaccepted, expired, or failed activities pay zero; verify valid started timeout/forfeit results follow the specified match rule.
- Verify launch initialization has no historical XP conversion, and the profile bar is correct at a level boundary and at Level 30 MAX.

### Rules that must not change without revisiting the economy

- The 30-level cap and **87,000 XP** Level 30 threshold.
- The quadratic threshold `50 + 10 × L²` and carry-over behavior.
- **100 XP** per eligible normal PvP completion, **+25 XP** for a win; **eight-match daily cap**.
- **1,000 XP per UTC day** global ceiling, and no XP overflow after Level 30.
- Bot, friendly, garden, card, trade, Flower, and marriage eligibility/limits in §B2.
- No XP per Apple, item, seedling, claim, click, move, or currency transaction.
- Account XP stays separate from existing Sib economies and progression tracks.

### Code areas the next phase is likely to touch (reference only)

`database/models/user.py`, `database/models/player_stats.py`, and a new migration/model for account progression and XP grants; `services/match_result.py`, `services/friendly.py`, `services/apple_battle.py`, `services/garden.py`, `services/shop.py`, `services/card_upgrade.py`, `services/trading.py`, `services/love.py`, `services/marriage.py`, and the `/profile` presentation in `handlers/apple_battle.py`. Current tunables live in `services/config_store.py`. This list is a handoff map, not authorization to implement in this design phase.

---

## Project references inspected

- `games/registry.py`, `handlers/party/handlers.py`, `matchmaking/matcher.py`, `services/match_result.py`, `services/seedling.py`, `services/friendly.py`
- `services/apple_battle.py`, `services/garden.py`, `services/apple_wallet.py`, `services/sib_claim.py`, `services/config_store.py`
- `database/models/user.py`, `database/models/player_stats.py`, `database/models/match.py`, `database/models/seedling.py`, `database/models/marriage.py`, `database/models/faction.py`
- `services/card_progression.py`, `services/card_upgrade.py`, `services/shop.py`, `services/trading.py`, `services/love.py`, `services/marriage.py`, `services/factions.py`
- `handlers/apple_battle.py`, `handlers/leaderboard.py`, `data/characters_seed.json`
