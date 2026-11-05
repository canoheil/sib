# CASINO-THRILL — قمارخانه سیبی: Design Proposal (No Code)

**Phase:** game design / brainstorm only — **zero code changed in this phase**
**Date:** 2026-11-05
**Branch:** `arena/efb5ca4c-sib` · **Baseline:** `1096d663` (`main`)
**Alembic head verified:** `078_wanted_decay` (single head — 78 revisions, checked by
walking `revision`/`down_revision` over `database/migrations/versions/`)
**Runtime:** aiogram `>=3.31.0`, Bot API 10.3 glass-button styles, MySQL/MariaDB +
SQLite (tests), Pillow + `arabic-reshaper` + `python-bidi` already in `requirements.txt`
**Skill used:** `.agents/skills/create-game-assets/SKILL.md` (art-direction brief,
asset manifest, quality gates). Persian copy follows `.agents/skills/persian-writing`
rules (محاوره‌نویسی برای متن‌های درون‌بازی، اعداد فارسی در حباب‌های گفت‌وگو).

Paths are relative to the repository root. Everything labelled *proposed* is future
work; **idea B is now implemented** (see the status line below), ideas A, C and D are
not.

> **Status (2026-11-05):** **Idea B — Near-Miss & Critical Hit Theatre — SHIPPED.**
> `handlers/casino.py` presents every round as three beats (anticipation line →
> `answer_dice` → tiered verdict: fumble / so close / win / jackpot), with slots
> decoding its three reels so a genuine two-of-a-kind earns the «🍒 خیلی نزدیک بود»
> line. Economy, odds, multipliers, cooldowns, FSM and the Alembic head
> (`078_wanted_decay`) are untouched.

> **Idea A — «زنجیره سیبی» (Apple Chain) — ALSO SHIPPED** (same PR, after B).
> A winning round now *offers* the pot instead of crediting it: green
> «💰 برداشت» banks it, red «🔥 ادامه بده» rolls again for the next rung, and the
> 10-second window always auto-banks on timeout. Built on a new FSM state
> (`waiting_for_chain_decision`), a per-rung token (stale and double taps cannot
> settle twice), a per-user lock, and an interruptible countdown so a tap during
> the clock is honoured. Still no migration, still head `078_wanted_decay`.
>
> **The ladder in §4A below was WRONG and was not shipped as written.** It is
> EV-safe in the narrow "never exceeds the first rung" sense, but a simulation
> showed that a player who always pushes returns just **1–2% of base RTP** on
> dice (a 98.9% house edge): technically safe, practically a trap. The shipped
> ladder keeps pushing an honest bad bet rather than a certain theft — corrected
> table below.

---

## 0. Executive summary

Rocket feels thrilling because of one structural property our Casino lacks: **the
result is decided before the player is allowed to see it, and the interval between
"committed" and "revealed" is filled with visible, changing information** (`handlers/rocket.py:120-215`:
four caption edits `🚀 در حال اوج‌گیری... (2.4x)` before the crash art lands).

The Casino does the opposite. `handlers/casino.py:245-253` fires `answer_dice`, then
`await asyncio.sleep(3.5)` in total silence, then prints one flat sentence. There is no
rising action, no near-miss, no second chance, no public spectacle, and the payout
resolution is *less* interesting than the animation the player already watched.

**Four candidate redesigns follow.** They are not mutually exclusive (a recommended
combination is in §6), but each is a standalone shippable feature with its own
economy math, so we can pick one and start.

| # | Working name | One-line pitch | Thrill source | Effort |
|---|---|---|---|---|
| **A** | **زنجیره سیبی — Apple Streak** | Win → your pot escalates up a fixed ladder and a "bank or push" button appears; one loss burns it. | Loss aversion on a *building* pot, plus a live climbing number that mirrors Rocket's `(2.4x)`. | **M** (largest single build) |
| **B** | **نزدیک بود! — Near-Miss & Critical Hit Theatre** | Rewrite the 3.5s of silence into a scripted 3-beat broadcast: anticipation → roll → *judged* verdict with critical-hit / near-miss tiers. | Suspense framing + "so close it hurts" drama, at zero economic cost. | **S** (presentation only) |
| **C** | **قرص شانس — Lucky Chip** | A scarce chip that can be spent mid-round for one extra roll (variant C5: a private pre-roll you may burn to replace). | Agency under uncertainty — the player makes a decision while the outcome is live. | **L** (state + new round flow) |
| **D** | **جکپات گروه — Group Jackpot & Streak Board** | A per-group, publicly visible jackpot that grows on every loss and can be popped; plus a group streak leaderboard. | Collective spectacle, communal rooting, and a persistent chase. | **M–L** (new table + locking; no scheduler) |

**Recommendation if we ship only one:** **B**, then **A**. B is a near-zero-risk
presentation layer that fixes the loudest complaint ("it doesn't feel exciting")
without touching the wallet, cooldowns, or any migration — and it is also the
*substrate* A, C and D all need for their drama. A is the real retention mechanic.
See §6 for the recommended combination and §7 for the risk list.

---

## 1. What Rocket does right (the bar we are chasing)

Read against `handlers/rocket.py` and `services/rocket_service.py`:

1. **Commit-before-reveal.** `crash_point` is rolled and the wager debited at
   `play_rocket` time (`services/rocket_service.py:232-249`); the animation is pure
   theatre on an already-settled fact. Player agency is spent *before* suspense begins.
2. **A rising number.** 4 caption edits, monotonic, each ~1s apart
   (`handlers/rocket.py:24,138-152`). Channel editing is throttled and capped
   (`MAX_PROGRESS_EDITS = 4`) — an existing, proven pattern we can reuse.
3. **A player-chosen target.** `سیب 2.5 200` lets the player decide their own line
   between hope and safety. Casino bets are binary — same stake, same odds, always.
4. **Asymmetric payoff art.** Win → parachuting apple, loss → explosion
   (`rocket_service.generate_boom_image`), with the multiplier burned into the image.
5. **A visible cost for greed.** Chasing 20x means failing 95% of the time; the drama
   is self-generating.

## 2. What the Casino does today (honest audit)

| Aspect | Current behaviour | Player-facing symptom |
|---|---|---|
| Timing | `answer_dice` → `sleep(3.5)` (4.0 for slots), silent | Dead air; the wait feels like a loading spinner, not tension |
| Odds | 1-in-6 pays ×5 (EV 0.83); slots 4 values in 64 pay ×15 (EV 0.94) | Rarely win, and wins are predictable in size — no variance spikes |
| Feedback | One line: `🏆 بردی! تاس 6 آورد و 50 سیب گرفتی.` | The 1-in-6 win feels identical to a lucky coin-flip; no tiering |
| Stakes | Fixed `MIN_BET=10 … MAX_BET=5000`, fixed ×5/×15 | No risk dial — the "target multiplier" analogue is missing |
| Progression | None. `COOLDOWN = 10` is the only per-player state | Nothing accumulates across rounds; no reason to come back |
| Social | Round is attached to the player's own message; result is one line | No group spectacle, no one cheering, no shared pot |
| Economy | Bet debited at start; payout credited after `sleep` via `_credit` (`handlers/casino.py:203-208`) | No faucets/sinks beyond the house edge (10–17%) |
| Art | One static `assets/casino/casino_menu.jpg` (1024×1024, 358 KB), painterly "Ovarya tavern" style | Menus are handsome; **in-round** moments have zero art |

Two structural notes that constrain every proposal below:

- **The dice value is already public when it lands.** Telegram renders the animation
  client-side; the bot only learns the value from the `Message` it sent. There is no
  way to "roll in secret and reveal." Any suspense must live **before** the send
  (anticipation), **around** the reveal (framing), or **after** it (consequences).
- **`answer_dice` is one message, immutably animated.** We cannot edit its caption
  like Rocket edits a photo caption. Drama therefore needs *either* a companion
  message we own (edit it freely) *or* a switch from `answer_dice` to a photo/MP4
  "stage" we control — trade-off analysed in §5.2.

---

## 3. New configuration (all four ideas)

Every number below must be a `system_config` key with a `CONFIG_DEFAULTS` entry in
`services/config_store.py`, following the `075_rocket_game` seed pattern. **No
thresholds hardcoded at import time** (the repo already enforces this convention).

```
casino_thrill_enabled            true     master kill-switch (seeded OFF would be safer, see §7)
casino_streak_enabled            true     idea A
casino_streak_max               5        streak depth cap (bank is forced at n = max)
casino_chain_ladder_dice         [5, 28, 150, 800, 4000]  pot multiplier after n wins (§4A)
casino_chain_ladder_slots        [15, 220, 3000]         idem, slots (p = 4/64, shallower)
casino_streak_bonus_ladder       [0, 0, 20, 60, 150]      flat bonus at n = 1..5, paid from the pot pool
casino_bank_window_seconds       6        "hold for a bigger multiplier" decision window
casino_hicrit_enabled            true     idea B critical-hit tier (cosmetic unless pot-funded)
casino_hicrit_pot_share_pct      0        % of the group pot paid on a crit; 0 = cosmetic
casino_hicrit_chance_pct         20       roll on an already-winning round only
casino_nearmiss_enabled          true     idea B near-miss copy
casino_reveal_tail_ms            1200     measured animation tail before the verdict
casino_chip_enabled              true     idea C
casino_chip_daily_grants         1        free chips per player per UTC day
casino_chip_max_held             3        wallet cap, prevents hoarding
casino_chip_re_roll_seconds      5        window between the two dice messages
casino_jackpot_enabled           true     idea D
casino_jackpot_contribution_pct  2        % of every losing bet routed into the group pot
casino_jackpot_base_seed         500      floor so the pot never looks empty
casino_jackpot_cap              100000   hard ceiling so it cannot inflate the economy
```

---

## 4. The four proposals

### Idea A — زنجیره سیبی / **Apple Streak** (recommended core retention mechanic)

**The fantasy:** *"I'm on fire. Do I cash in now, or push for one more?"*

**Mechanics**

1. Streak counter `n ∈ [0, 5]` per player, incremented on every winning round,
   reset to 0 on any loss. The streak only counts rounds played **inside one chain**
   (see 4) — an abandoned chain resets it.
2. The pot is **not** a product of escalating multipliers. It is a single number that
   follows `casino_streak_pot_ladder_<game>[n] × stake` — the ladder in §3, which is
   chosen so that `p(win) × ladder[n]` stays safely below 1.0 at every depth (dice
   `p = 1/6`, slots `p = 4/64`). At `n = 1` the ladder reproduces today's ×5 / ×15
   exactly, so a first-round win pays what it pays now.
3. **After a win, the round does not end.** The bot edits its verdict message into a
   *collect-or-push* prompt with a live countdown:

   > 🎲 تاس ۶ آورد — بردی! **زنجیره: ۳** · گلدان **×۵۰**
   > 🍎 روی میز: **۱٬۰۰۰ سیب** (شرط ۲۰)
   > ↳ «💰 برداشت» بزن، یا ۶ ثانیه صبر کن تا با همون شرط ادامه بدی
   > `[💰 برداشت]` `[🎲 ادامه با تاس]` `[🎯 ادامه با دارت]`

4. If the window expires (or the player hits "ادامه"), the next round re-bets **the
   original stake automatically** and rolls without a new bet prompt. If the player
   banks, the pot is credited and the chain ends. This is the crucial mechanical change:
   the loop between rounds drops from ~8 seconds of typing a number to **one tap**,
   which is what makes a streak feel like a streak and not a form.
5. A 5-win dice chain pays `stake × 4000` (capped by `MAX_BET`), i.e. a Rocket-Neptune
   moment — but the road there is `(1/6)^5 ≈ 1 in 7,776`, which is why it can afford to
   be that big.

**Why it creates suspense:** the pot is *visible and rising* (exactly Rocket's
`(2.4x)` trick, in apples instead of a multiplier), but unlike Rocket the player has a
**second decision point after every success** — the "push your luck" beat that Rocket
lacks. Loss aversion on an accrued pot is measurably stronger than loss aversion on a
fresh bet.

**Hook to the existing code (design level):**
`CasinoService.calculate_result` gains optional `streak` / `streak_pot` arguments and
returns `{"is_win", "payout", "value", "streak", "pot", "forced_bank"}` where `payout`
now means *what would be banked now*, not what is auto-credited.
`place_bet` keeps the debit at round start; between rounds the pot lives in **FSM
data** and is credited exactly once, idempotently, on bank
(`casino_payout:<chain_id>`, the existing key shape in `handlers/casino.py:203-208`).
A restart inside the window must not lose the pot: the chain id and pot are also
persisted in `user_casino_streaks` (one row per user, together with `streak`,
`game`, `stake`, `expires_at`), so a cold FSM can recover or force-bank the chain.
**Decision needed before implementation** (§7, Q2).

**Economy guard rails (mandatory — this is the part that must not be hand-waved):**
- `EV(n) = p(win) × ladder[n]` must stay < 1.0 for every `n`, both games:

  **Shipped ladder** (`services/casino_service.CHAIN_LADDERS`), with the
  simulated return of a player who pushes to that rung and then banks:

  | n | dice ladder | dice RTP (rung) | push-then-bank | slots ladder | slots RTP (rung) | push-then-bank |
  |---|---|---|---|---|---|---|
  | 1 | ×5 | 0.833 | 0.837 | ×15 | 0.938 | 0.941 |
  | 2 | ×28 | 0.778 | 0.788 (94%) | ×220 | 0.859 | 0.856 (91%) |
  | 3 | ×150 | 0.694 | 0.658 (79%) | ×3000 | 0.732 | 0.520 (55%) |
  | 4 | ×800 | 0.617 | 0.560 (67%) | — | — | — |
  | 5 | ×4000 | 0.514 | 0.533 (64%) | — | — | — |

  Two properties, both unit-tested: every rung stays below the first rung's
  return (chain length can never beat the house), and **no rung falls below half
  of base RTP** so a push stays an honest gamble. Slots stops at three rungs
  because 1-in-16 cannot support a deeper honest ladder.
- The earlier draft (`5, 10, 22, 50, 120` / `15, 35, 90, 250, 700`) passed the
  first test and failed the second: always-push returned 1–2% of base. The guard
  is `tests/test_casino.py::test_pushing_is_a_real_bet_not_a_punishment`.
- Flat bonus ladders were dropped entirely for this ship: they are minted apples
  and would need the §4D pot pool to fund them. No new faucet exists.
- Hard cap: a chain pot is clamped to `MAX_BET × 120`, and each family's ladder
  length forces a bank at its own top rung.
- Streak is per-player, per-game-type (a dice streak never carries into slots).

**UX/visuals:** streak counter rendered as apples/lit lamps in the caption
`🍎🍎🍎⚪⚪`; a 6-second drain bar is not possible in Telegram text, so we use a
**decrementing number** in an edited caption (`⏳ ۳…۲…۱`) at 1s intervals, then a final
edit — 6 edits per round is the same order of cost as Rocket's 4 progress edits plus
its final media swap (`handlers/rocket.py:24,138-215`), which the bot already sustains. At `n ≥ 4` the message gains a
"🔥" prefix and the button style flips to `danger` (red) on the push action to
signal risk (semantics per `keyboards/common.py:1-18` — colour carries meaning, never
decoration).

**Effort:** M. New FSM state `waiting_for_bank`, one config table, no migration if the
bank logic is ledger-derived; one migration if we persist streak state (recommended,
so a restart doesn't wipe a player's 4-streak).

---

### Idea B — نزدیک بود! / **Near-Miss & Critical Hit Theatre** (start here)

**The fantasy:** *"Something is happening, and the bot is reacting to me."*

Presentation-only. **No change to odds, payouts, cooldowns, or the wallet.** All three
beats are additive messages/edits around the existing `answer_dice` call.

**Mechanics / UX — the 3-beat script**

Beat 1 — **Anticipation** (sent *before* the dice, ~0.4s earlier so the animation
begins against a live caption):

> 🎲 تاسِ {player_mention} در حال چرخشه…
> شرط: **۲۰۰ سیب** · شانس برد: **۱ از ۶**
> _نفس‌ت رو حبس کن…_

Beat 2 — **The roll** (`answer_dice`, unchanged, still a reply to the player's bet
message so the existing privacy/threading guarantees hold).

Beat 3 — **The verdict**, tiered. This is where the thrill actually comes from —
the same 1-in-6 outcome is now *classified*, and the near-misses are narrated:

| Tier | Condition | Copy (محاوره) | Art |
|---|---|---|---|
| 💀 Fumble | `dice == 1` | «هنوز شروع نشده تموم شد! 😩» | — (text only) |
| 😐 Nothing | `2 ≤ dice ≤ 4` | «تاس {v} آورد. نه، اینبار نه.» | — |
| 😱 **Near miss** | `dice == 5` | «😱 **نزدیک بود!** یه دونه کم آورد… حقت برد بود!» | `verdict_nearmiss.jpg` |
| 🎉 Win | `dice == 6` | «🏆 **شش!** {mention} زد و برد — **{payout} سیب**» | `verdict_win.jpg` |
| ⚡ **Critical hit** | `dice == 6` **and** `secrets.randbelow(100) < 20` | «⚡ **ضربه بحرانی!** تاس شش + کرتیکال — **{payout} سیب**» | `verdict_crit.jpg` |
| 🎰 Jackpot | slots ∈ {1,22,43,64} | «🎰🎰🎰 **جکپات!** سه‌تا جور شد…» | `verdict_jackpot.jpg` |

Notes:
- **Critical hit is cosmetic by default.** Multiplying payouts is an economy change;
  the on-by-default version only re-labels a win. If we want it to pay, it must pay
  **from the funded pot pool** of idea D, never from minted apples (§5.3).
- **Near-miss is honest.** We never fake a "you almost won" on a genuinely hopeless
  roll; `dice == 5` on a 6-only game *is* a near miss. This matters ethically and for
  long-term trust — and it is the cheapest dopamine in the document.
- **Darts and bowling get their own near-miss lines** (`دارت به ۵ خورد، یک خط تا
  هدف!` / `بولینگ ۵ پین، یکی مونده بود!`), because the current single generic loss
  line wastes the richest narrative moment in each game.
- **Slots never leaks the raw 1–64 value** — the existing test
  `test_slots_loss_never_shows_the_number` (`tests/test_casino.py:257`) is a product
  rule, not an implementation detail. Slot theatre must speak in **reels**
  (`🍒🍒🍇 — دو تا جور شد!`), which conveniently makes near-miss *stronger*: a 2-of-3
  match is viscerally "almost".

**Extra suspense beat (optional, cheap):** a 1-second "suspense edit" between the
animation landing and the verdict:
`⏳ نتیجه داره بررسی می‌شه…` → verdict. One extra edit, and it converts the flat
`sleep` tail into a held breath. (Bot API animation length is client-side; we should
measure real landing time per emoji once and keep a configurable tail, e.g.
`casino_reveal_tail_ms`.)

**UX/visuals:** verdicts are sent as **photo replies** (1280×720, matching the Ovarya
style already used for Castle/Rocket) so each tier is a distinct glanceable card in the
group. Losing verdicts stay plain text to keep chat noise down; only
near-miss / win / crit / jackpot get art.

**Economy:** none by default. Optionally the near-miss carries a *free, non-monetary*
hook: "😱 نزدیک بود — دفعه بعد شانست ×۲ میشه" → a one-round **luck token** that does
not change EV (it just re-rolls the *display*... no). See §5.3 for the honest version:
near-miss grants a **cosmetic** badge or, at most, a `casino_chip` (idea C) — a
re-roll is the only genuinely exciting non-cash reward, and it belongs to idea C.

**Effort:** S–M. Roughly: a `services/casino_theatre.py` (pure functions
`verdict(emoji, value, bet, rng) -> {tier, text, art}`), a handful of new strings,
and 4–6 generated 1280×720 assets. Fully unit-testable without Telegram.

---

### Idea C — قرص شانس / **Lucky Chip** (agency under uncertainty)

**The fantasy:** *"I can see the bad roll coming and I get one save."*

**Mechanics**
1. Every player earns **1 free Lucky Chip per UTC day** (cap 3 held), plus 1 chip as a
   rare drop (5%) from a *winning* round — a faucet that costs the house nothing
   because chips only re-roll presentation of an outcome the player already paid for.
2. Chips are **spent before the result is judged**: the flow is
   `bet → dice #1 (public) → 5-second window → [🍀 استفاده از قرص شانس]`.
3. If the first roll loses, the chip sends **dice #2** and the round is judged on the
   better of the two. If the first roll wins, the chip is not offered (no value lost).
4. One chip per round, hard.

**Why it creates suspense:** it is the only proposal where the player **acts during
uncertainty**. The 5-second window is a real, tense decision under a clock — the exact
emotional shape of Rocket's pre-crash moment, compressed. It also converts the most
frustrating event in the casino (a 1-in-6 game saying "no") into a second chance the
player *earned*.

**Economy math (must be respected):** a re-roll on a 1-in-6 game raises the effective
win probability to `1 - (5/6)² = 30.6%`, i.e. EV 1.53 on a ×5 payout — **wildly
profitable**. Chips therefore cannot be "free re-rolls on the same odds." The
defensible designs, pick one:
- **(C1) Chip is a *safety net*, not a re-roll:** the chip refunds 50% of the stake on
  a loss (EV ≈ 0.83 + 0.5×0.83 ≈ 1.25 — still too high) → no.
- **(C2) Chip converts the game to a *guaranteed-win-at-reduced-payout*:** dice #2 is
  free, but the payout on a re-roll win is ×2.5, not ×5 → EV = 0.167×5 + 0.306×2.5 ≈
  0.83 + 0.76 = 1.6 → still too high.
- **(C3) Chip is a paid consumable, not a free re-roll:** buy a chip for 25% of the
  stake; the re-roll wins pay ×5 but the chip cost is sunk. EV ≈ 0.306×5 − 0.25 ≈ 1.28
  → still high.
- **(C4) Chip narrows the field instead of re-rolling:** the chip changes the winning
  condition to "6 **or** 5" for one round at ×2 payout → EV = (2/6)×2 = 0.67. Safe but
  feels like a downgrade, not a thrill.
- **(C5, recommended) Chip *reveals* rather than re-rolls:** the chip is spent **before**
  the dice to buy a **pre-roll**, i.e. the player sees the outcome privately first
  (via a bot DM / ephemeral message), then chooses: take it, or burn 50% of the stake
  to force a re-roll. EV can be tuned exactly to 1.0 via the 50% burn price. This is
  the only version that keeps the ×5 advertised odds intact *and* creates a genuine
  hidden-information rush — but it needs the ephemeral-message plumbing already used by
  Battleship (`docs/games/BS1-IMPL-REPORT.md`).

> **Design note:** I recommend **not** shipping idea C until the economy owner picks a
> variant from C1–C5, and I flag that C5 has the highest delight per apple but is the
> only one requiring the ephemeral API. If we want the *feeling* with none of the
> balance risk, idea B + A deliver most of it for a fraction of the risk.

**UX/visuals:** chip balance lives on the casino menu caption (`🍀 قرص شانس: ۲`); the
in-round chip button is `success`-styled, the re-roll confirmation is `primary`. A
"chip burn" animation is unnecessary — the second `answer_dice` message *is* the
animation, and its being visibly a *second* dice (with `🍀` in the anticipation line)
tells the whole story.

**Effort:** L. New table `user_casino_chips` (or a `casino_chip`-reason ledger row —
cheaper, and stronger for auditing; I'd use the ledger), a new FSM window, a new
callback namespace `casino_chip:<game_id>`, and idempotency care so a chip cannot be
spent twice by a double-tap (`_ACTIVE_USERS` already models this pattern,
`handlers/casino.py:38-42`).

---

### Idea D — جکپات گروه / **Group Jackpot + Streak Board** (social spectacle)

**The fantasy:** *"The whole group is watching this pot, and it's about to pop."*

**Mechanics**
1. Each group gets a persistent **pot**. Every losing casino bet routes
   `casino_jackpot_contribution_pct` (2%) of the stake into it; the pot is seeded at
   `casino_jackpot_base_seed` (500) the first time a group plays and capped at
   `casino_jackpot_cap`.
2. The pot is **visible on every round's anticipation line**:
   `💰 گلدانِ گروه: ۱۲٬۴۵۰ سیب`
3. **Population rules** (all tunable):
   - 1% chance on any dice/darts/bowling win to pop the pot;
   - 100% chance on a slots three-of-a-kind 64 (`🎰` jackpot face).
4. Pot is paid to the winner **from the pot itself** (a real, funded pool, not minted
   apples), resets to the base seed, and posts a group-wide **celebration card**
   (`verdict_jackpot.jpg`, 1280×720) mentioning the winner.
5. **Streak Board:** a lightweight per-group top-5 of current streaks and best-ever
   streaks, posted only when someone *breaks* a group record (never on a timer — no
   cron, no scheduler risk). It gives the group a reason to watch someone else's round.

**Why it creates suspense:** shared money changes the audience. In Rocket every player
runs their own private calculus; here, a 12,000-apple pot makes *other people's* rolls
worth reading. It also creates the only true "event" in the casino: a pot pop is
chat-worthy, screenshot-worthy, and rare enough (1%) to stay special.

**Economy:** funded, not minted — 2% of losses is a *reduction* in house revenue, not
an inflation source, as long as the pop probability keeps `E[pot payout] <
contribution rate × total losing volume`. With a 1% pop rate on wins
(p(win)=1/6 → p(pop)=1/600 per round) and a 100,000 cap, the worst case is bounded by
the cap. The cap is therefore not cosmetic: **it is the inflation ceiling** and must be
enforced at contribution time, not at payout time.

**Population tuning note:** a 1% pop *on a win* means `1/600` per round, so in a busy
group the pot mostly just glows. If engagement says the pot never pops, raise it to
2–3% before touching the cap — the cap is the safety rail, the pop rate is the dial.

**Risks:** cross-group farming (a ring of accounts feeding a pot in a private group).
Mitigation: contributions only count from bets that were actually lost,
`casino_jackpot_min_group_members` (e.g. 5) before a pot can pop, and a per-user daily
contribution cap. Also: **pot accounting must be transactional** (one row, `SELECT …
FOR UPDATE`, following `services/lock_order.py`), or two simultaneous wins pop the same
pot twice.

**Effort:** M–L. Needs `group_casino_pots` (or `casino_jackpots`) table + migration
`079_…` on top of `078_wanted_decay`, locking discipline, and a celebration renderer.

---

## 5. Cross-cutting design

### 5.1 Sequence comparison (before / after)

```
TODAY                                     IDEA A + B (recommended pair)
─────────────────────────────────────     ─────────────────────────────────────
player types 200                          player types 200
                                          📣 «تاس در حال چرخشه… شانس ۱ از ۶»   [photo caption, own message]
🎲 answer_dice                             🎲 answer_dice
…3.5s of nothing…                          …1s… ⏳ «نتیجه داره بررسی می‌شه…»
😭 باختی! تاس 5 آورد.                      😱 «نزدیک بود! یه دونه کم آورد…» + art  [5 → near-miss]
                                            ↳ streak 0/5 · pot 12,450
(if win)                                   (if win)
🏆 بردی! تاس 6 آورد و 50 سیب گرفتی.        🏆 «شش!» + 🍎×3 streak · ⏳ ۶…۵…۴
                                            [💰 برداشت] [🎲 ادامه]
                                          → one tap, next round starts (no typing)
```

### 5.2 Where the drama physically lives (technical trade-off, design-level)

Telegram gives us three levers, and each idea above must name which one it uses:

| Lever | What it buys | Constraint |
|---|---|---|
| **Edits to a message we own** (photo caption / text) | unlimited-ish rising numbers, countdowns, verdicts — Rocket's proven path (`handlers/rocket.py:138-215`, 4 edits) | edit rate limits; `telegram_ui.safe_edit_message_*` already handles retry/degradation |
| **`answer_dice`** | zero-effort, native, un-fakeable animation | caption immutable, ~3.5–4s, no control over landing moment |
| **Switching to an MP4 "stage"** (`reply_animation`, like `rocket_launch.mp4`) | full control: we can draw the dice, the streak lamps, the pot, the multiplier in one moving image | we lose Telegram's *trusted* randomness UI; needs `assets/casino/*.mp4`; higher cost per round; and the outcome is now our RNG to justify — **not recommended** for a wagering game |

**Design decision:** keep `answer_dice` as the **source of truth** (trust + zero cost)
and put all theatre in companion messages we own. This is idea B's whole thesis, and it
is why B is the safest first ship.

### 5.3 Trust & fairness rules (non-negotiable)

1. The dice value from Telegram stays authoritative; we never re-roll silently, never
   "improve" a result after the fact.
2. Every payout change is a *pre-declared* rule visible **before** the wager (menu
   caption and bet prompt), exactly as `MENU_CAPTION` does today
   (`handlers/casino.py:50-54`) and `test_menu_explains_the_odds` enforces
   (`tests/test_casino.py:363-371`).
3. Near-miss copy is only used on genuinely near outcomes (dice 5, darts 5, bowling 5,
   slots 2-of-3). No fabricated suspense.
4. Any new faucet (chips, jackpots, streak bonuses) must be **bounded and tunable**
   from `system_config`, with a default that does not raise casino EV above 1.0.
5. Existing invariants stay intact: `_ACTIVE_USERS` single-round lock, reply-to-prompt
   check, idempotent ledger keys, prompt deletion, and the slots-no-raw-number rule.

### 5.4 Visual system brief (per `create-game-assets`)

Following the skill's "lock the technical frame → name the visual system → manifest →
approve one target → family with a reference → normalise → validate" pipeline.

**Technical frame**
- Native size **1280×720** (the established Telegram landscape banner size for
  Castle/Clash/Rocket, e.g. `docs/games/C1-RECON.md:282`), JPEG q≈92, ~150–250 KB each.
- No transparency needed (full-bleed scenes). Captions/labels are **Telegram text,
  never baked in** — they must stay localisable and RTL-correct.
- Camera: three-quarter elevated, single warm key light from upper-left, consistent
  with the existing `casino_menu.jpg` tavern (wood, green baize, stained glass,
  violet-gold slot cabinet, apple protagonist in a red coat).

**Visual system (continuing the existing direction, not replacing it)**
| Axis | Definition |
|---|---|
| Shape language | Rounded, chunky, hand-inked tavern props; sharp *only* at the moment of danger (crit/near-miss sparks) |
| Palette roles | Warm wood/amber = neutral stage; **emerald** = win/safe; **apple red** = stake/danger; **violet-gold** = jackpot/rare |
| Edge | Painterly ink line, slightly uneven width, visible paper grain (matches the current menu) |
| Light | Warm candle key + violet slot-machine rim; high contrast behind the verdict zone |
| Detail density | Simple centre 60% (the verdict lives there), dense corners (tavern clutter) |
| Motion character | Snappy, elastic; sparks overshoot, then settle |

**Asset manifest (proposed — none generated in this phase)**

| File | Size | Purpose | Notes |
|---|---|---|---|
| `assets/casino/roll_dice.jpg` | 1280×720 | idea B anticipation backdrop | empty dice tray, dramatic top light, no dice drawn (the animation is Telegram's) |
| `assets/casino/roll_darts.jpg` | 1280×720 | darts anticipation | dartboard, empty flight line |
| `assets/casino/roll_bowling.jpg` | 1280×720 | bowling anticipation | lane vanishing point, pins silhouetted |
| `assets/casino/roll_slots.jpg` | 1280×720 | slots anticipation | three blank reels, glowing |
| `assets/casino/verdict_nearmiss.jpg` | 1280×720 | near-miss tier | die showing 6 with one pip dimmed / bullseye with dart embedded at the wire / last pin wobbling |
| `assets/casino/verdict_crit.jpg` | 1280×720 | critical hit | red-violet shockwave, sparks, apple crest |
| `assets/casino/verdict_win.jpg` | 1280×720 | standard win | apple silhouette lifting a cup, emerald light |
| `assets/casino/verdict_jackpot.jpg` | 1280×720 | slots jackpot / pot pop | violet-gold burst, coins, three matching reels |
| `assets/casino/streak_lamps.png` | 320×64 | optional streak HUD strip (if we ever switch stages) | 5 lamps, on/off states, must be readable at 32px |

**Quality gates** before shipping art: readable at phone size over the real Telegram
chat background; silhouette of each tier distinguishable in grayscale; palette roles
consistent across all eight; no text/logos/UI chrome baked in; provenance recorded next
to the manifest; one approved hero asset (`verdict_nearmiss.jpg`) before generating the
family.

### 5.5 Copy / register

- In-chat game copy: **محاوره** (`در حال چرخشه`, `نفس‌ت رو حبس کن`, `نزدیک بود`), Persian
  digits for counts, ZWNJ in `می‌شه`/`نمی‌شه`, no bookish `می‌باشد`.
- Player names always go through `mention()` + `fix_bidi` (the existing pattern at
  `handlers/casino.py:61-77`, `core/bidi.py`) — Latin names in a Persian sentence are the #1 way these
  cards break.
- Numbers in the *arithmetic* stay ASCII if they must parse; numbers in *prose* are
  Persian. (Existing tests assert ASCII payout digits, e.g. `50 سیب گرفتی`.)

### 5.6 Config, migration and rollout plan (shape only)

- **One migration** when (and only when) an idea needs persistence:
  `079_casino_thrill` on top of `078_wanted_decay`, seeding the keys in §3 and creating
  `user_casino_streaks` (A) and/or `group_casino_pots` (D). Migration must be
  idempotent (`_has_table` guard, `ON CONFLICT`/`ON DUPLICATE KEY` seeding) exactly like
  `075_rocket_game.py`.
- **Kill switch** `casino_thrill_enabled` default **false** on first deploy, flipped by
  an operator after a staging run — the same pattern as `inv2_events_enabled`
  (`services/config_store.py:43-47`).
- **Backwards compatibility:** all new strings behind the flag; with the flag off the
  handler must behave byte-identically to today, so `tests/test_casino.py` keeps
  passing unchanged.
- **New tests to add** (mirrors the existing suite's style): tier classification
  table for B, near-miss only on honest values, streak EV ceiling assertion (a
  regression test that fails if anyone edits the multiplier table into a >1.0 EV),
  chip double-spend idempotency, jackpot locking, and "flag off = old behaviour".

---

## 6. Recommendation

**Ship in this order:**

1. **B (Near-Miss Theatre)** — highest delight per unit of risk. No wallet, no odds, no
   migration, fully testable, and it immediately makes every round feel authored. It is
   also the vocabulary layer the other ideas reuse.
2. **A (Apple Streak)** with the EV-gated ladder of §4A — this is the retention engine and
   the closest thing to Rocket's "push your luck" moment. Needs one economy sign-off
   and one migration.
3. **D (Group Jackpot)** if group engagement is a KPI — it is the only idea that turns
   a private wager into a public event.
4. **C (Lucky Chip)** only after the economy owner picks a variant (C5 if we want the
   real rush; otherwise skip).

**If we must pick exactly one thing to build first:** B. It is the answer to the literal
complaint ("lacks the thrill"), costs the least, and cannot break the economy.

**If we want the single biggest behavioural change:** A, because it is the only proposal
that gives players a **reason to play a second round immediately** — and repeated rounds
are what "exciting" actually means in a Telegram group chat.

---

## 7. Risks and open questions

| Risk | Mitigation |
|---|---|
| Streak ladder breaks EV ≥ 1.0 | the §4A table is the guard value; a unit test asserts EV < 1.0 for every streak depth and game |
| Extra messages per round → chat spam in busy groups | only *winning*/notable tiers get art; near-miss gets text unless the group is quiet; `casino_noise_level` config (`minimal`/`normal`/`loud`) |
| Edit-rate limits when 3 edits + 1 photo per round | reuse `telegram_ui.safe_edit_message_*` (3 attempts + plain-text fallback), cap edits like `MAX_PROGRESS_EDITS` |
| Double-spend / double-pop on rapid taps | keep `_ACTIVE_USERS`, add `FOR UPDATE` on pot and chip rows, idempotent ledger keys |
| Jackpot farming via alt-account rings | min group members, per-user daily contribution cap, pot cap |
| Gambling optics (real-money-adjacent framing) | apples only, no Stars/cash purchase path for chips, keep the existing "بازی سرگرمی" framing; add a config to disable the whole casino if regulators/Telegram require |
| `sleep(3.5)` is a guess at animation length | measure, put `casino_reveal_tail_ms` in config |

**Questions for you before implementation:**

- **Q1 — Which idea(s)?** B alone, B+A, or the full set?
- **Q2 — Streak pot persistence:** is a *pending/accrued* pot acceptable in the ledger
  (extra reason `casino_streak_bank`), or must every round settle immediately?
- **Q3 — Economy appetite:** may the casino EV rise toward ~0.95–1.0 (more frequent
  wins, thinner house edge), or must the current 10–17% edge hold?
- **Q4 — Chip variant** if C ships: C5 (private pre-roll + 50% burn, needs ephemeral
  messages) or nothing?
- **Q5 — Group jackpot** seeded/owned per Telegram group (`groups` table) — fine to add
  a table, or must it stay ledger-only?
- **Q6 — Art budget:** generate the full 8-image family (§5.4; the 9th row is an
  optional HUD strip, not required), or approve one hero
  (`verdict_nearmiss.jpg`) first?

---

## 8. Appendix — exact code seams this touches (for the implementation phase)

| Concern | File / symbol | Note |
|---|---|---|
| Round flow, dice send, sleep, verdict text | `handlers/casino.py:211-265` (`place_bet`) | B rewrites the post-`answer_dice` block; A adds a bank state after it |
| Win/loss copy | `handlers/casino.py:121-141` (`win_line`, `loss_line`, `_GAME_LINES`) | B replaces with tiered verdicts |
| Menu odds copy | `handlers/casino.py:50-54` (`MENU_CAPTION`) | must declare any new rule before wagering |
| Odds & payout | `services/casino_service.py:16-24, 61-77` | A/D add streak/pot arguments; keep int math |
| Isolation & cooldown | `services/casino_service.py:28-59` | untouched by B; A reuses `BET_REASON` rows for the chain |
| Crediting | `handlers/casino.py:203-208` (`_credit`) | new reasons: `casino_jackpot`, `casino_chip_*` |
| Config | `services/config_store.py` `CONFIG_DEFAULTS` | seed via migration, never hardcode |
| Telegram-safe edits | `services/telegram_ui.py:326-400` | reuse rather than raw `bot.edit_message_*` |
| Buttons/colour semantics | `keyboards/common.py:1-18, 245-270` | push = `danger`, bank = `success` |
| Tests to extend | `tests/test_casino.py` (505 lines; `test_round` 201, slots guard 257, EV/slots tables 488-505) | add tier, EV, idempotency, flag-off cases |
| Migration head | `database/migrations/versions/078_wanted_decay.py` | next would be `079_casino_thrill` |

**No files other than this document were created or modified in this phase.**
