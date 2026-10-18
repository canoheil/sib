# C1-RECON — قلعه سیبی (Apple Castle)

**Phase:** investigation and design only · **Date:** 2026-10-18  
**Inspected baseline:** `109b4e200fe638df373116aac1f0bc202d783077`  
**Scope of this change:** this report only. No application code, migrations, handlers, configuration, generated images, or database data are changed. Everything described as “proposed” is future work, not existing behavior.

## 1. Recommended design at a glance

- One permanent, shared castle per Telegram group; level 1–10, starting as a free ruined level-1 castle. Members spend **normal wallet apples**, not special apple inventory items.
- Automatic upgrades at cumulative donation thresholds; no leader ownership, withdrawal, upkeep, decay, or transferable castle balance.
- **500 apples per user per UTC day across all castles**, plus **10,000 apples per group per UTC day**. The second cap is an explicit addition to protect the months-long goal in very large groups.
- **1,300,000 apples** to reach level 10. Even an extremely wealthy/large group needs at least **130 donation days**. Smaller or less wealthy groups take longer.
- Apple Spring: automatic daily credits of **2 × level apples per eligible member**, with one castle payout per user per UTC day across the ecosystem. No donation requirement for receiving income.
- Invasion: **+2% damage per castle level**, capped at +20%, snapshotted per invasion. Use a multiplier, not an autonomous cannon.
- Entry is the exact group message **`قلعه سیبی`**. Shared banner/menu, public buttons, actor-specific donation confirmation.
- Five illustration milestones: **1, 3, 5, 8, 10**, all 1280×720, using the same castle silhouette and Ovarya visual language.

The group cap, global user cap, active-member definition, and one-spring-per-day restriction are proposed product decisions. They intentionally make the economic and anti-abuse requirements precise; obtain approval before implementation.

## 2. Reconnaissance: current code and integration seams

Paths below are relative to the repository root. Python imports use the `sib.*` namespace.

### 2.1 Groups and membership

| Existing source | Finding | Future castle hook |
|---|---|---|
| `database/models/group.py` — `Group` | `groups.id` is a signed Telegram `BigInteger`, PK, not autoincremented. Fields: title, optional username, active flag, optional welcome time, and `TimestampMixin`. Only declared domain relationship here is one-to-one `GroupStats`. | Use this exact ID as castle PK/FK. A group is the owner, never an admin/user or `GroupStats`. |
| `database/models/group_stats.py` | Match/leaderboard statistics, not a treasury or membership roster. | Leave castle progress separate from competitive win statistics. |
| `handlers/group_lifecycle.py` — `handle_my_chat_member`, `_auto_register`, `_auto_deactivate` | Registers/reactivates the group when the bot joins; marks it inactive when removed. Registration/welcome is durable independently of Telegram delivery. | Lazy-create a castle on first valid menu access. Preserve progress while inactive; stop contributions, buffs for new encounters, and new spring cycles until reactivated. |
| `database/models/invasion_v2_group_member.py` — `Inv2GroupMember` | Unique `(user_id, group_id)` observations with `last_seen_at`; FK to both registered users and groups. | Useful active-player candidate list, **not** proof of present membership. |
| `middleware/group_membership.py` — `GroupMembershipMiddleware`, `record_group_member` | Observes non-bot group messages/callbacks asynchronously, throttled to once per pair per minute, with bounded pending tasks and tolerated failures. | Do not assume the current button press has already produced a membership row. Validate the actor directly and record castle activity transactionally if needed. |
| `services/invasion_v2/power_resolver_impl.py` — `DbGroupPowerResolver` | Uses recently observed users, configurable activity window (default 30 days), selected Main Cards; minimum card-bearing players defaults to 3. | Reuse the membership concept, not card eligibility, for spring candidates. A Main Card must not be required for castle use. |

**Important limit:** this checkout has no authoritative full Telegram member roster. A bot cannot infer all silent/unregistered members from the observation table. “All members” must mean all eligible known active members for automatic payment, or require a separate membership/onboarding project. Never advertise automatic payment to every account in the Telegram group without this qualification. No donor-only eligibility gate is proposed.

Group → supergroup ID migration needs explicit future handling: remap castle and child FKs atomically as part of a shared group-migration policy. Do not copy progress into a second castle. The inspected lifecycle handler is not a castle/group-ID migration facility.

### 2.2 Economy: canonical apples and transaction boundaries

- `database/models/apple.py`: `AppleWallet` stores one global normal-apple balance per user (`Integer`, currently). `AppleTransaction` is the signed audit ledger with reason, reference, balance snapshot, and globally unique nullable `idempotency_key` (`String(100)`). Special apples belong to the item economy, not this wallet.
- `services/apple_wallet.py` — `AppleWalletService`: wallet mutations lock the wallet, flush ledger entries, and do not own the caller's final commit. `earn` supports strict keyed credit replay. `debit_once` returns `LedgerDebit(applied, balance_after)`; **only `applied=True` can authorize a new castle contribution**. Reuse with a different user/amount/reason/reference is a conflict, not success. Insufficient funds must roll back the domain operation.
- MariaDB/MySQL keyed wallet operations use savepoints for duplicate-key races. SQLite is explicitly a test engine with different locking/savepoint behavior; SQLite-only tests cannot prove production concurrency safety.
- `database/connection.py` — `get_session`: commits on successful exit, rolls back on exceptions. Donation, wallet debit, contribution record, and castle upgrade must share that one transaction. Telegram acknowledgement/image work follows commit.
- `services/lock_order.py`, `docs/lock-order.md`: economy operations take the `users` mutex first; multiple users lock in ascending ID order. Existing ranks then cover relationships, cards, items, wallets, progression. A castle implementation must extend this documented policy rather than invent an inconsistent order.

**Economic anchors, not production measurements:** `services/config_store.py` defaults include 5 apples per 300-second wake-word claim, friendly rewards of 10 apples with a 50/day cap, daily-chest apple rolls of 15–25, and card upgrade costs competing for normal apples. These are configuration defaults, not guaranteed actual income. A 500/day donation ceiling is a ceiling for saved wealth, not an assumption that casual members earn 500 daily.

### 2.3 Invasion v2: actual damage pipeline

1. `bot.py` registers `DbGroupPowerResolver` at startup.
2. `services/invasion_v2/scheduler.py` resolves average active Main Card power and creates an `Inv2Invasion` occurrence.
3. `services/invasion_v2/enemy.py` — `compute_enemy_hp`: `round(average card power × enemy multiplier × difficulty)`. Enemy multipliers are 1.0, 1.6, 3.0, 5.0, 8.0.
4. `handlers/inv2_join.py` — `handle_inv2_join`: locks the invasion, validates the originating group/message/deadline and one-attack-per-user rule, resolves the user's Main Card and `card_attributes(...).power`.
5. `services/invasion_v2/damage.py` — `compute_damage`: consumes two RNG draws; normal hit is `round(max(0, power) × uniform(0.85, 1.15) × crit)`, with 10% critical chance and critical multiplier 2.
6. The join handler doubles that rounded hit for optional consumed `sib_buff`, then calculates remaining HP, narrative, guarded HP compare-and-swap, attack insert, and terminal reward settlement in one transaction. The unique `(invasion_id, user_id)` constraint is a final duplicate gate.
7. `services/invasion_v2/presentation.py` handles delivery; `rewards_service.py` settles the outcome. `services/invasion_v2/simulation.py` independently samples `compute_damage` for admin estimates.

**Precise seam:** apply the castle multiplier in `handle_inv2_join` **after** the existing optional `sib_buff` multiplication and **before** remaining HP and `render_join`. Persist the resulting value in `Inv2Attack.damage`. Keep `card_power` as unbuffed power. Do not change the resolver or enemy HP calculation: doing so would make the boss stronger alongside the castle and cancel much of the benefit.

There is no recurring per-member attack loop here: joining produces one attack. A cannon every 30 seconds would introduce a new durable timed actor, scheduler/recovery rules, autonomous terminal settlement, and attribution rules. That is unnecessary scope for C1.

### 2.4 Existing daily rewards and UI patterns

- `services/daily_chest.py` — `utc_day`, `next_reset_at`: UTC calendar days, reset at 00:00 UTC, not rolling 24-hour cooldowns or server-local dates.
- `database/models/daily_chest.py`: unique `(user_id, claim_day)` durable gate plus reward snapshot. `DailyChestService._claim_once` locks the user, inserts the claim, and delivers with `AppleWalletService.earn`; failures roll both back. Handler/service race handling uses fresh transactions after rollback.
- `handlers/daily_chest.py`: shared group chest post lets each pressing member receive their own global daily reward; validates date-bearing callbacks and originating group context. This is **user-triggered**, not automatic payment to a roster.
- `services/sib_claim.py`: the wake-word apple reward is a separate **rolling cooldown** using ledger timestamps and a per-user mutex. Do not use it as the castle's daily-boundary rule.
- `services/marriage_art.py`: shared scene paths, Telegram `file_id` reuse, photo/caption delivery and text fallback. `services/telegram_ui.py` centralizes safe screen rendering. Reuse the delivery conventions without tying castle business rules to marriage.
- `services/panel.py`: personal owner-scoped panels exist. Use that policy for donation confirmations, not for the public shared castle menu.
- `handlers/__init__.py` — `register_handlers`: future dedicated castle router registration belongs here, before the broad start router. Exact text and namespaced callbacks must not intercept other games.

## 3. Proposed data design (not migrations)

All Telegram IDs are **signed BIGINT**. Amounts and cumulative counters proposed here use BIGINT; no floats. Use existing timestamps/mixin conventions, normalize UTC explicitly at application boundaries, and do not rely on MariaDB preserving a timezone offset simply because SQLAlchemy uses `timezone=True`.

### 3.1 Required table: `group_castles`

| Column | Proposed type / rule |
|---|---|
| `group_id` | BIGINT PK, FK `groups.id`; one castle per group |
| `level` | Small integer, not null, default 1; range 1–10 |
| `total_donated` | BIGINT not null default 0; cumulative accepted apples, never reset on upgrade; range 0–1,300,000 in this ruleset |
| `last_claimed_at` | Nullable UTC datetime: latest spring cycle whose recipient deliveries are all terminal for this group; **not a per-user payout gate** |
| `created_at`, `updated_at` | Existing timestamp conventions |

`level` is a cached derivative of thresholds and `total_donated`; change them together under lock. No stored spendable castle wallet: donated apples are an irreversible sink. Reject donations at maximum; do not burn apples for cosmetic leaderboard inflation. Keep the FK restrictive for deletion: deactivation must preserve the asset and its audit history.

### 3.2 Required table: `castle_donations`

Use **one immutable row per successful donation operation**, not one overwritten daily aggregate. Multiple 100-apple donations in a day must be allowed.

| Column | Proposed type / rule |
|---|---|
| `id` | BIGINT autoincrement PK (SQLite INTEGER variant in future tests, matching inv2 conventions) |
| `user_id` | BIGINT FK `users.id` |
| `group_id` | BIGINT FK `group_castles.group_id` |
| `amount` | BIGINT, positive actual accepted amount |
| `date` | UTC day represented as `YYYY-MM-DD` String(10), matching daily-chest convention; not server-local time |
| `request_key` | String(100), globally unique; durable server-generated confirmation nonce |
| `created_at` | Audit timestamp |

Indexes: `(user_id, date)` for the global daily user sum; `(group_id, date)` for the group daily sum; `(group_id, user_id)` for all-time donor aggregation. Rank donors by sum(amount) descending, user_id ascending for stable ties. No unique `(user_id, group_id, date)` constraint: that would incorrectly prohibit repeat donations.

Ledger debit: reason `castle_donation`, ref_type `castle_donation`, ref_id donation ID, key `castle:donate:<nonce>`. The donation row and ledger row must agree on amount/user/reference. A nonce is persisted by the personal confirmation/panel flow before use and bound to actor, group, exact resolved amount, UTC day, and short expiry. Replays return the saved receipt **before** recalculating a new Max amount; they never create another donation.

### 3.3 Additional durability needed for automatic Spring

The two requested tables alone cannot represent an automatic payout to many users safely. `last_claimed_at` cannot distinguish “already credited user A” from “still owe user B” after a crash.

Propose these additional tables in the implementation phase:

| Table | Minimal design |
|---|---|
| `castle_spring_cycles` | PK UTC `date`; status `planning/ready/complete`, fixed cutoff time and rules version. A singleton daily job gate coordinates workers and restart-safe planning. |
| `castle_spring_groups` | Unique `(group_id, date)`; level and per-user amount snapshots, recipient count, status/completion timestamp. Preserve zero-recipient cycles too. |
| `castle_spring_claims` | Unique `(user_id, date)` globally; selected group FK/day, snapshotted amount, status `pending/paid/skipped`, paid_at, skip reason. Index `(status, date, user_id)` for bounded workers. These are durable planned payments, not user-click claims. |

Plan all eligible groups/recipients for a day before setting the cycle `ready`. Planning is deterministic and resumable; no recipient may be paid from a partially planned cycle. The daily cycle row is the planning mutex; freeze group snapshots once and never resnapshot completed chunks on retry. A recipient visible in several castles is assigned the highest frozen castle level, then lowest signed group ID as a stable tie-breaker. Thus order of payout workers cannot choose a smaller reward or grant duplicates. A crash resumes the stored plan, not today's changed membership.

The recipient worker locks the User first, then its planned claim, validates eligibility, credits the wallet, and marks it paid in a single transaction. Key: `castle:spring:<user_id>:<YYYY-MM-DD>`; reason `castle_spring`, ref_type `castle_spring`, ref_id claim ID. The globally unique claim and strict ledger key are independent backstops. Failed credits remain pending through rollback. Never grant a fresh reward merely because a post failed.

### 3.4 Locking, caps, and all-or-nothing donation

Future lock policy extension: **User → castle row → donation/recipient domain records → wallet → progression**, retaining all existing relative resource ranks. Daily planners lock only cycle/snapshot records and must not acquire users while holding castle locks. Spring credits use User → recipient → wallet and do not lock the castle; final completion metadata updates happen separately. Invasion spawn snapshots read committed castle state without taking a castle lock; join uses only its stored invasion snapshot. Audit these rules against all existing settlement paths before implementation.

Donation transaction:

1. Validate exact callback shape, actual group/message, registered non-bot actor, active Group, membership, feature switch, day/expiry and personal confirmation ownership. Never trust a user/group ID or amount supplied by callback data alone.
2. Lock the User mutex first, then ensure/lock the one castle row. Handle concurrent lazy creation with a unique-key retry of the complete transaction.
3. Resolve consumed nonce first. For a new operation, read **current/locking** cap data after mutex acquisition, not a stale MariaDB REPEATABLE READ snapshot. Use indexed locking/current reads of donation rows before summing. User lock serializes donations to different castles; castle lock serializes different donors to this castle. Every writer must follow this protocol.
4. Compute user remainder `500 − all-group donations today`, group remainder `10,000 − this-group donations today`, wallet balance under the canonical wallet lock, and remaining lifetime cost `1,300,000 − total_donated`.
5. Fixed 100/500 requests are exact: if any limit/balance is short, refuse without debit and show the available Max. Max is the minimum of all four remainders, resolved on the server and frozen into the confirmation. If the frozen amount becomes unaffordable/stale before confirmation, require a fresh quote; never silently charge a different amount.
6. Insert donation, debit with `debit_once`, check `applied`, increment total exactly once, recompute level (including multi-threshold crossings), and consume confirmation. Commit together. A replay cannot increment progress, caps, or donor ranking.
7. Only after commit: acknowledge, refresh menu, optionally announce a new level. Coalesce refreshes; Telegram errors never undo a successful purchase.

Use bounded full-transaction retries for deadlocks/unique creation races, with the same nonce. UTC date is decided from an authoritative UTC clock at acceptance; a confirmation from yesterday expires, even if its original quote was affordable. The daily sum is not the concurrency primitive by itself; locks plus idempotency are essential.

## 4. Level progression and economic reasoning

### 4.1 Full table

“Cost to reach” is incremental from the previous level; “cumulative” is the required lifetime donation total. Level 1 is free so every newly opened group has the ruined asset and an immediate social goal. **The first paid upgrade costs 5,000; there is no hidden 5,000 creation fee.**

| Level | Name / state | Cost to reach | Cumulative apples | Next upgrade cost | Spring per member/day | Invasion bonus | Art |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | ویرانهٔ سیب — Ruined foothold | 0 | 0 | 5,000 | 2 | +2% | 1 |
| 2 | پناهگاه جوانه — Repaired refuge | 5,000 | 5,000 | 15,000 | 4 | +4% | 1 |
| 3 | دژ باغبانان — Orchard keep | 15,000 | 20,000 | 30,000 | 6 | +6% | 3 |
| 4 | باروی سرخ — Red rampart | 30,000 | 50,000 | 60,000 | 8 | +8% | 3 |
| 5 | قلعهٔ سنگی — Solid stone castle | 60,000 | 110,000 | 100,000 | 10 | +10% | 5 |
| 6 | ارگ شکوفه — Blossom citadel | 100,000 | 210,000 | 150,000 | 12 | +12% | 5 |
| 7 | دژ زمردی — Emerald fortress | 150,000 | 360,000 | 220,000 | 14 | +14% | 5 |
| 8 | ارگ آسمانی — Skybound citadel | 220,000 | 580,000 | 300,000 | 16 | +16% | 8 |
| 9 | تاج اوواریا — Crown of Ovarya | 300,000 | 880,000 | 420,000 | 18 | +18% | 8 |
| 10 | قلعهٔ سیب جاودان — Eternal floating castle | 420,000 | 1,300,000 | — MAX | 20 | +20% | 10 |

Formulas:

- `level = highest L whose cumulative threshold ≤ total_donated`.
- `spring(L) = 2 × L`; no compounding, percentage interest, or offline accumulation.
- `castle_bonus_basis_points(L) = 200 × L`; a group without an initialized castle contributes 0, not an implicit level-1 bonus.
- Level-local progress = `(total − threshold[L]) / (threshold[L+1] − threshold[L])`; clamp to [0,1]. At max show completion rather than divide by zero.

### 4.2 Why these numbers?

The early 5k/15k steps let groups see improvements; escalating late costs keep the rare visual milestones aspirational. All levels improve the numeric reward even when art is reused.

For N regular donors each averaging d apples/day, idealized daily progress is `min(N × min(d,500), 10,000)`. Time from zero is `ceil(1,300,000 / daily progress)` **donation days**. These are budget scenarios, not predictions from live data:

| Regular donors | Average donated per donor/day | Group/day | Ideal days to L10 |
|---:|---:|---:|---:|
| 5 | 500 | 2,500 | 520 |
| 10 | 500 | 5,000 | 260 |
| 20 | 500 | 10,000 | 130 |
| 100 | 500 | 10,000 (group cap) | 130 |
| 20 | 100 | 2,000 | 650 |
| 50 | 100 | 5,000 | 260 |

At 20 fully capped donors, level 5 takes 11 donation days and level 10 takes 130; at 10 fully capped donors, 22 and 260 days respectively. A lone stockpiled whale needs 2,600 donation days. At least 20 fully capped donors are needed to use the entire daily group budget. Per-user caps do not prevent Sybil accounts; the group cap bounds acceleration even if many accounts collaborate.

**Trade-off:** small casual groups may take years, not months. Do not hide that. Before release, inspect anonymized donation-affordability cohorts and actual faucet/sink usage. If the target is months for 5–10 casual donors as well as massive groups, these static costs cannot satisfy both without a separate size-normalization policy (which risks membership manipulation). Prefer adjust the global schedule based on evidence, not unverified group size discounts.

20 apples/day at maximum is four default wake-word claims and 4% of the personal donation cap: useful, but not a substitute for playing. For 20 eligible recipients the maximum spring faucet is 400/day; a static 1.3m donation sink has a naive max-level payback of 3,250 days. For 100 recipients it is 2,000/day and 650 days. These ignore lower-level earnings and changes in membership. Free level-1 castles and very large groups still create inflation; monitor active recipients, unique accounts, total spring minted, donation burn, net apples, and suspicious clusters. Global one-per-user/day prevents multiplying income by joining hundreds of castles, but it does not solve multi-account abuse.

Do not auto-reinvest spring income or debit passive income for castle upkeep. Freeze thresholds under a rules version for launch; future rebalances must explicitly preserve existing donated value and earned levels.

## 5. Apple Spring: daily passive distribution

**Recommended behavior: automatic, not a required claim button.** A read-only “چشمهٔ امروز” button reports personal payment/pending status; opening it does not grant another reward.

### Eligible members and cutoff

- At the daily 00:00 UTC cutoff, group must be active and castle initialized before the cutoff.
- Candidate is a registered human observed in that group within the previous 30 days and first observed at least 24 hours before cutoff (`Inv2GroupMember.created_at` can seed this tenure check). No Main Card and no donation required.
- Check current membership with Telegram `getChatMember` before granting; allow actual member/admin/creator and restricted users only when `is_member` is true. Left/kicked/bot users are skipped. Guaranteed checks for arbitrary users require suitable bot rights; groups without reliable membership checks should show spring as unavailable, not pay unverified users.
- Telegram errors/429s are retryable, not evidence of leaving; leave payment pending and use bounded concurrency/backoff. A confirmed departure skips today's planned payment; do not redirect it to another castle after planning.
- The 24-hour tenure prevents instant join-and-collect. The 30-day activity window prevents endless payments to old observed accounts. These are castle-owned proposed settings, not a dependency on invasion card settings.
- A group message or member interaction registers otherwise unknown players through existing onboarding; silent/unregistered members are not discoverable. Explain this in the help screen.

### Processing and recovery

At midnight, a durable daily job freezes levels and eligible recipients using §3.3. Payouts run in small per-recipient transactions, never one transaction locking every wallet in a group. A level upgrade after cutoff changes the next day's payout; it cannot increase a partially delivered cycle. A newly created castle starts spring on the next cutoff. Use yesterday's persisted snapshot after downtime; do not fabricate missed historical memberships/levels. If the bot was down before a day's plan existed, skip that day's spring rather than invent retroactive rewards. Resume already planned pending grants, with membership validation and active-group checks on recovery.

When all planned recipients for a group are paid/skipped, mark the group cycle complete and advance `last_claimed_at` monotonically in a separate short transaction. It describes completion, not financial authority. No per-recipient public message or DM is necessary: wallet credits are passive; menu can show a compact daily summary. Inactive groups pause pending payments until reactivated or an operator explicitly closes them as skipped, with audit reason.

**Alternative requiring product approval:** a shared “دریافت سهم امروز” button can reuse the chest's user-triggered model and avoid a bulk scheduler, but that is daily claimable income, not the automatic passive mechanic proposed here. Do not silently substitute it during implementation.

## 6. Invasion benefit: Castle Ward

### Rule and ordering

Choose **+2% per level**, not +5%. +50% at L10 would heavily stack with the already available 2× consumable; +20% is meaningful while preserving card progression and the current enemy roster.

Store proposed immutable `castle_level_snapshot` (0–10, default 0 for old invasions) and `castle_bonus_bps_snapshot` on `Inv2Invasion` when creating the occurrence in `scheduler.py`. It is fixed from scheduling onward, including time pending announcement. Later upgrades apply only to newly created occurrences. Keep the snapshot even if the castle/group is later deactivated; normal invasion availability checks still apply. Historical encounters must not retroactively acquire a bonus.

Damage calculation:

1. `D = compute_damage(power, rng)` exactly as today, retaining its crit result and RNG consumption.
2. If the player uses `sib_buff`, `D = 2 × D` exactly as today.
3. `final = floor((D × (10,000 + snapshot_bps) + 5,000) / 10,000)` — integer round-half-up, explicitly distinct from the existing base-hit Python rounding.
4. Subtract `final` using the existing HP CAS and store that same `final` in attack/narrative. HP remains clamped at zero; stored attack damage retains full overkill as the current pipeline does.

Example: power 100, random factor 1, no crit → 100 base. L5 yields 110; L10 yields 120. L10 plus `sib_buff` yields 240. With crit and `sib_buff`, it yields 480. There is no extra critical roll, additional attack, free consumable, or reward for a nonparticipant. Integer rounding can erase a tiny bonus on a very weak hit; do not add guaranteed flat damage.

### Integration constraints

- Keep invasion row lock, unique attack, timeout validation, HP CAS, item consumption, and terminal settlement unchanged in meaning. Buffed damage is determined before CAS; loss/retry cannot spend twice.
- Do not obtain castle locks inside join or settlement. Read only the invasion snapshot, avoiding a new lock inversion with economy operations.
- Show “🏰 نیروی قلعه: +۲۰٪” in announcement and attack presentation. An art/send failure does not modify combat.
- Update `simulation.py` to show/use the same snapshot/rule (for hypothetical encounters, current castle level clearly labeled). It currently samples ordinary hits and does not model the optional paid 2× consumable; label that limitation rather than accidentally double-counting it.
- `rewards_service.py` still grants rewards only to actual attackers using existing victory/loss rules. No castle cannon pseudo-user and no automatic kill while nobody participates.

## 7. UI/UX flow

### 7.1 Entry and public castle screen

Match **exact** `قلعه سیبی` only in `group`/`supergroup`. Do not implement substring matching, flexible word order, or Arabic-character normalization in this first contract. Private use gets a short group-only explanation. Anonymous sender-chat messages and bot actors can view an explanation but cannot spend a user's wallet.

Post one shared 16:9 banner plus a Persian caption and public keyboard. A proposed 30-second durable per-group post cooldown prevents spam across workers; reuse the chest's reservation idea under a separate namespace, not its exact state key. Refresh existing messages where practical, with short coalescing to avoid Telegram edit floods. Keep banner caption within the central Telegram caption limit; show top three donors and put longer lists behind a button.

Example at 35,000 total donations:

> 🏰 قلعه سیبی · دژ باغبانان  
> سطح ۳ از ۱۰  
> پیشرفت به سطح ۴: ۱۵٬۰۰۰ / ۳۰٬۰۰۰ 🍎  
> ▰▰▰▰▰▱▱▱▱▱ ۵۰٪  
> کل کمک‌ها: ۳۵٬۰۰۰ 🍎  
> چشمه: روزانه ۶ 🍎 برای هر عضو واجد شرایط  
> نیروی قلعه در یورش‌های جدید: +۶٪  
> برترین سازندگان: ۱) … ۲) … ۳) …  
> سقف روزانه گروه: ۱۰٬۰۰۰ 🍎 · بازنشانی ۰۰:۰۰ UTC

Public buttons:

- `🍎 کمک به ساخت` · `🌊 چشمهٔ امروز`
- `🏅 برترین سازندگان` · `🔄 تازه‌سازی`
- `ℹ️ قوانین قلعه`

Donor names must be escaped and truncated. Never publish user balances. Top donors aggregate **accepted** donations, not pending quotes or failed ledger writes. Show a friendly empty leaderboard for a new castle.

### 7.2 Donation interaction

1. Any real member presses Donate; the shared menu remains public and does not display that person's wallet.
2. Open an actor-bound, short-lived confirmation panel in the same group (or an actor-scoped equivalent supported by existing panel infrastructure). Explain: permanent contribution, normal apples only; show **that actor's** user/day remainder and group remainder. If same-group panels would expose balance, omit balance entirely and only show permitted donation amounts. No mandatory DM, since not every member has started the bot privately.
3. Buttons: `۱۰۰ 🍎`, `۵۰۰ 🍎`, `حداکثر`, `بازگشت`. Max respects wallet, global personal cap, group cap, and remaining lifetime target. Disable/refuse unavailable choices with specific private alerts.
4. Selecting an amount creates a server-side exact quote, with proposed 60-second expiry; show “کمک قطعی ۳۲۰ سیب؟ قابل برگشت نیست.” then Confirm/Cancel. Other members cannot confirm it. All confirmations expire at UTC rollover.
5. Confirm performs §3.4. A private success toast reports actual accepted amount; refresh shared progress only after commit. Expired/day-old/malformed/cross-group callbacks cannot debit.
6. At a new level, post at most one coalesced celebration showing new level, next target, spring effective next daily snapshot, and invasion bonus effective for new occurrences. Avoid repeating celebrations on callback replay.
7. At L10 hide/disable Donate, show completed progress and all-time donors. No further donations, no prestige reset in C1.

Expected refusals: insufficient apples, daily personal cap exhausted, daily group cap exhausted, quote changed/expired, inactive group, disabled feature, wrong panel owner, stale message, max castle. Failures before commit show no success. Failure after commit explains that the donation is saved and the display can be refreshed; never suggest paying again.

### 7.3 Spring/help screen

Explain automatic credit, UTC reset, 24-hour tenure, 30-day observed activity, membership verification, no donation requirement, and one castle payment per user/day. Show selected paying group when the user belongs to several castles and today's `pending/paid/skipped` state. This is read-only; opening a screen cannot trigger another payout.

## 8. Image asset plan — five generation prompts

**Only prompts are delivered in C1; do not generate or commit images now.** Proposed future directory: `assets/castle/`, JPEG deployment banners. Keep generation originals outside Git or within existing approved asset budgets. Proposed filenames: `castle_01.jpg`, `castle_03.jpg`, `castle_05.jpg`, `castle_08.jpg`, `castle_10.jpg`. Mapping: L1–2 → 01; L3–4 → 03; L5–7 → 05; L8–9 → 08; L10 → 10.

Shared art direction for every prompt: **one standalone 1280×720 landscape banner, 16:9**. Warm cinematic painterly fantasy of **Ovarya**, Persian-inspired stonework without claiming a real historical site; red apples, emerald leaves, copper/gold highlights, no modern technology. Consistent three-quarter elevated view of the same central keep with three recognizable towers, an apple-shaped crest carved into the gate, and an apple spring in the foreground. Keep key structures inside the central 80% safe area. No text, letters, numbers, watermarks, logos, UI, or collage. Use the selected first illustration as a composition/reference image for later milestones; maintain the horizon, silhouette, materials, palette, and apple motif rather than generating unrelated castles. Captions/level labels remain Telegram text, never baked into art.

### Prompt 1 — L1: ruined foothold

> A single 1280x720 landscape fantasy illustration in the world of Ovarya, warm painterly cinematic concept art. A small ruined apple castle on a grassy rocky hill, three recognizable broken towers surrounding a roofless central keep, collapsed stone walls, an ancient apple-shaped crest carved above the damaged gate. Wild apple trees grow through the rubble, a tiny clear spring trickles into a cracked stone basin in the foreground, scattered red apples and emerald leaves suggest hope. Soft dawn mist, restrained muted greens and weathered grey stone, one warm amber light inside the ruin. Elevated three-quarter view, readable strong silhouette, entire castle centered with generous breathing room, richly textured but clear at phone size. Persian-inspired fantasy masonry, inviting rather than horrific. No text, letters, numbers, watermark, logo, UI, or collage.

### Prompt 2 — L3: orchard keep

> A single 1280x720 landscape fantasy illustration of the same three-tower apple castle in Ovarya, using the approved ruined castle as a layout and silhouette reference. The central keep has been repaired with pale stone and timber scaffolding, three modest towers rise above new wooden palisades and young orchard terraces. Preserve the apple-shaped carved gate crest and foreground spring, now flowing into a repaired stone basin; show fruit baskets and modest footpaths suggesting communal rebuilding, without identifiable people. Warm morning sunlight, emerald leaves, ripe red apples, copper roof accents, distant misty mountains. Elevated three-quarter view, warm painterly cinematic fantasy, Persian-inspired architectural details, central safe composition readable on mobile. No text, letters, numbers, watermark, logo, UI, or collage.

### Prompt 3 — L5: solid stone castle

> A single 1280x720 landscape banner of the same apple castle in Ovarya at its solid stone milestone, preserve the established camera angle, three-tower silhouette, central keep, apple-crested gate and foreground spring. Strong continuous pale stone curtain walls, finished battlements, sturdy red-copper roofs, an arched bridge, flourishing terraced apple orchards, emerald vines tastefully framing carved masonry. The spring now fills a beautiful circular stone fountain with small apple-shaped carvings. Secure, prosperous, grounded and believable, no floating structures yet. Warm afternoon sun on textured stone, cinematic painterly fantasy, deep green valley and distant mountains, Persian-inspired geometric ornament, crisp visual hierarchy at phone size. No text, letters, numbers, watermark, logo, UI, or collage.

### Prompt 4 — L8: skybound citadel

> A single 1280x720 landscape fantasy banner of the same three-tower apple castle in Ovarya, now an enchanted skybound citadel. Preserve the central keep, apple-carved gate, foreground spring and elevated three-quarter camera. Monumental polished stone terraces, copper-gold tower crowns, luminous emerald runes rendered only as abstract non-letter ornament, mature red-apple orchards on elevated gardens. The rocky foundation has begun lifting slightly from the valley with visible earth below and thin waterfalls spilling from the apple spring; graceful roots and soft golden magic tether it to the landscape. Twilight violet sky, warm windows and vivid apple-red focal accents, majestic but not yet the final floating palace. Rich warm painterly cinematic fantasy, readable central silhouette and clear safe margins. No text, letters, numbers, watermark, logo, UI, or collage.

### Prompt 5 — L10: eternal floating castle

> A single 1280x720 landscape epic fantasy banner of the final Apple Castle of Ovarya, directly evolving the same established three-tower central keep, apple-crested gate and apple spring. An immense floating island high above a sea of clouds, crowned by radiant white stone ramparts, elegant copper-gold spires and suspended emerald orchard gardens filled with glowing red apples. The recognizable spring has become a luminous crystalline fountain sending gentle waterfalls over the island edges; ancient tree roots cradle the floating rock, subtle golden magic supports smaller garden terraces. Triumphant sunrise, sweeping distant Ovarya mountains beneath the clouds, grand scale without visual clutter, warm painterly cinematic texture, Persian-inspired carved geometry, vivid emerald, apple red and sunrise gold. Keep the entire main island and towers within the central safe area, readable on a phone. No text, letters, numbers, watermark, logo, UI, or collage.

Future delivery should reuse scene/file_id caching and fall back to the same functional text/keyboard if art is missing or Telegram upload fails. No image generation during gameplay; art is a curated static asset, not a financial dependency.

## 9. Implementation-phase verification plan (not executed in C1)

### Donation and economy

- Every threshold boundary, all 10 levels, multi-level crossing, and max-level zero-spend refusal; cumulative and incremental costs reconcile to 1,300,000.
- Fixed 100/500 and Max with low balances, 499/500 personal donations, 9,999/10,000 group donations, and a remaining lifetime target smaller than either fixed button.
- Two users compete for final group/day capacity; one user donates concurrently to two groups; simultaneous castle creation; stale Max quotes; UTC rollover; duplicate nonce with identical and conflicting parameters.
- Inject failures after donation insertion, wallet debit, and upgrade: nothing partially commits. Replay after commit changes no counters. Telegram failure after commit preserves debit/progress.
- Test actual parallel sessions on MariaDB, including REPEATABLE READ stale-snapshot risks; do not treat SQLite row-lock tests as equivalent.

### Spring

- No donor/card requirement; unregistered/silent-user limitation; 24-hour tenure; 30-day cutoff; left/kicked/restricted accounts; missing bot rights and retryable API errors.
- Global deduplication across multiple castles; deterministic highest-level selection; level changes after snapshot; old cycle resumed after downtime; missing historical plan grants no retroactive reward.
- Crash during planning, after recipient selection, during wallet credit, and before completion metadata; multiple workers deliver exactly one ledger credit per user/day.
- Inactive group pause/resume, zero-recipient completion, max wallet integer overflow refusal and operator recovery (current wallet is INTEGER; never wrap or silently truncate credits).

### Combat and UI

- No-castle/legacy snapshot leaves old damage unchanged; every castle level; weak/zero hits; crit; 2× consumable; rounding ties; overkill; simultaneous killing blows; deadline/CAS failures; reward invariants; simulation parity.
- Snapshot remains constant through upgrades and restart, and enemy HP is not castle-scaled.
- Exact Persian entry versus near-matches; group-only routing; callback ownership and cross-group forgery; HTML-escaped donor names; caption limits; missing image fallback; shared menu never exposes balances.

### Release gates and observability

Add a castle master switch plus separate donation/spring/buff switches in the future implementation, with clear rollback semantics: disable new mutations/payouts without deleting castles or modifying past grants/attack snapshots. Monitor donation debits versus accepted contributions, cumulative progress reconciliation, failed/replayed/conflicting keys, cap rejections, pending spring age, total minted/burned apples, and snapshot damage distribution. No admin “grant apples” bypass without canonical ledger audit.

Before implementation, approve the four explicit scope decisions: 10,000 group/day cap, free level 1, active-known-member automatic spring with global daily deduplication, and +2%/level combat bonus. Then implement migrations/services/tests/handlers as a separate phase. **No implementation or runtime tests are part of this report-only change.**
