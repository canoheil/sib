# HW2 — Halloween Event Overhaul: UI, Balance, and Interactive Mechanics

Follow-up to `HW1-RECON.md` (the Sibomortis global boss raid). Playtest
feedback: the event was **too passive** (one private button tap), the rewards
were **too low**, the menu was **confusing**, and the boss **did not look
alive**. HW2 fixes all four. The feature flag
`system_config["halloween_enabled"]` still defaults to `False`.

## 1. Interactive attack — text trigger instead of a button (TASK 1)

`[ ⚔️ حمله به سیبومورتیس ]` is removed from `get_halloween_main_keyboard()`;
the menu now instructs:

> `⚔️ برای حمله، روی همین پیام ریپلای کنید و بنویسید «حمله»`

`handlers/halloween.py::handle_halloween_attack_trigger` fires on the **exact**
word `حمله` (`^\s*حمله\s*$`, re-checked inside the handler so the trigger cannot
be bypassed by a longer sentence) **and** only when the message is a reply to a
Sibomortis Boss Menu. The menu is recognised by the panel registry
(`panel_owners.panel_type == "halloween"`, authoritative), with the menu title
as a fallback so replies to pre-HW2 menus still bite.

A trigger spends **1 `pumpkin_seed`**, rolls Main-Card damage, writes the
`halloween_attacks` row, and refreshes the attacker's menu — all through the
same `_run_attack()` used by the legacy `hw:attack` callback, which is kept so
stale menu buttons do not die with an "old message" toast.

Security/ownership rules (unchanged in spirit from HW1):

* the actor is always `message.from_user` — replying to **someone else's**
  menu spends *your* seeds and deals *your* damage, never the owner's;
* only the panel **owner** gets the menu edited in place, so no cross-user
  state is written into another player's personal panel;
* a rejected hit (no seeds, boss dead, event off) rolls back its transaction
  **before** the handler replies, so the consumed seed is refunded.

## 2. Public attack message (TASK 2)

Every landed hit posts to the group first, then the private/panel feedback:

> `⚔️ **{user}** به سیبومورتیس **{damage} آسیب** زد!`

Extra lines are appended to the same message when they apply: `💥 ضربهٔ حیاتی!`
(crit), `🎃 سیبومورتیس دگرگون شد!` (phase crossed), `🏆 سیبومورتیس سقوط کرد!`
(kill). The name goes through `safe_name()` (Markdown-escaped, `یک سیبی`
fallback) and the damage through `fa_digits()`.

## 3. Balance and economy (TASK 3)

| Lever | HW1 | HW2 |
|---|---|---|
| Seed loot per claim (`claim_seed_loot`) | 1–3 | **3–6** (`SEED_LOOT_MIN/MAX`) |
| Group chest per winner (`claim_group_drop`) | 1–3 | **3–6** (chest copy updated in `LOOT_DROP_VARIANTS`) |
| Loot cooldown | 4 h, vague alert only | 4 h **and the menu shows the exact remaining time**: `⏳ زمان تا غنیمت بعدی: ۳ ساعت و ۱۵ دقیقه` (`HalloweenService.get_seed_loot_status`, `format_duration_fa`) |
| 75% milestone | 150 apples + 2 seeds | **500 apples + 3 seeds** |
| 50% milestone | 3 powder + 2 gold | **5 powder + 5 gold** |
| 25% milestone | Amber + 1 diamond | **Amber + 3 diamond** |
| 0% milestone | 500 apples + 1 obsidian (+1000 Top-3) | **1500 apples + 1 obsidian** (+ Top-3 rewards) |

Rewards live in one table now — `MILESTONE_REWARDS` in `services/halloween.py`
— and `claim_milestone()` plus every screen are generated from it
(`milestone_reward_line()`), so payout and copy cannot drift apart.

**Schema:** `database/migrations/versions/059_halloween_seed_loot_boost.py`
rewrites `ck_hw_drop_claims_seeds` from `BETWEEN 1 AND 3` to `BETWEEN 1 AND 6`
(via `batch_alter_table`, so SQLite and MariaDB both work). No data migration;
the `1` floor stays so pre-HW2 rows remain valid. `downgrade()` refuses while
any row pays more than 3.

## 4. UI and clarity (TASK 4)

Boss Menu (`format_boss_menu_caption`, ≤ ~500 chars of a 1024 caption budget):

```
🎃 **هالووین: سیبومورتیس**

🥀 **سیبومورتیس در حال فروپاشی‌ست؛ نورش رو به خاموشی است.**

💀 جون: **۴۱۹۰ از ۵۰۰۰** (**۸٪**)
`█░░░░░░░░░`
🎃 دونه کدو: **۷**
⏳ زمان تا غنیمت بعدی: ۳ ساعت و ۱۵ دقیقه
 هر غنیمت: **۳ تا ۶ دونه کدو** · ⌛ پایان: ۴ روز و ۱۱ ساعت

 آسیب بر پایهٔ **قدرت کارت اصلی** توست؛ ۱۰٪ شانس **ضربهٔ حیاتی** (دو برابر).

🏆 **برترین آسیب‌زنندگان**
🥇 سارا: ۱۸۴۵۰ آسیب
🥈 آرش: ۱۲۰۰ آسیب
 مهربان: ۰۰۱ آسیب

⚔️ برای حمله، روی همین پیام ریپلای کنید و بنویسید «حمله»
```

* **Reward Track**: one block per milestone — `**۷۵٪** — 🟢 آمادهٔ دریافت` then
  the reward line; badges are exactly `🟢 آمادهٔ دریافت` / `🔒 قفل` /
  `✅ دریافت شده`, with a legend line so the three states are self-explanatory.
* **Leaderboard**: Top-3 damage dealers from `HalloweenService.get_leaderboard`
  (medals 🥇🥈🥉; an explicit "first hit can be yours" line when nobody has
  attacked yet).
* **Damage basis hint**: Main Card power with a 10% crit chance
  (`ATTACK_CRIT_CHANCE`), shown in one line.
* The attack instruction disappears once the boss is dead, replaced by the
  claim-your-reward line.

## 5. Boss evolution (TASK 5)

Three new 1024×1024 RGB JPEGs next to the original in `assets/halloween/`:

| File | Form |
|---|---|
| `sibomortis_75.jpg` | slightly damaged, cracks + brighter glowing eyes |
| `sibomortis_50.jpg` | enraged, bark armour broken with molten seams, darker aura |
| `sibomortis_25.jpg` | near death, hollow torso, broken crown, severed arm, weak glow |

`BOSS_ART` maps `phase_for_hp()` (already the HW1 1..5 phase helper) to
`(asset_key, bundled_path)`:

| HP | phase | artwork |
|---|---|---|
| > 75% | 1 | `sibomortis.jpg` |
| ≤ 75% | 2 | `sibomortis_75.jpg` |
| ≤ 50% | 3 | `sibomortis_50.jpg` |
| ≤ 25% | 4 | `sibomortis_25.jpg` |
| 0 | 5 | `sibomortis_25.jpg` |

`_resolve_boss_photo(session, state)` resolves
`halloween_boss[_75|_50|_25]` through `AssetService` first, so each form stays
runtime-replaceable with `/setasset`, and degrades to the base portrait when a
file is missing. Because the menu is re-rendered on open, on `hw:menu`, after a
loot claim and after every attack, the transformation is visible the moment the
threshold crosses; `AttackOutcome.phase_before/phase_after` drive the public
`دگرگون شد!` line. `BOSS_PHASE_TAUNTS` gives the boss a voice per phase.

## 6. Hype launch broadcast (TASK 6)

`/hw_start` (non-`--test`) now broadcasts `START_ANNOUNCEMENT` — static text,
no `max_hp` placeholder:

```
🚨 **سیبومورتیس از راه رسید!** 🎃
تاریکی اواریا را فرا گرفته. برای حمله به این هیولای افسانه‌ای، کلمه «هالووین» را در گروه بزنید و بجنگید!
جایزه نهایی: کارت افسانه‌ای امبر! 🎴
```

`/hw_start … --test` stays silent (private confirmation only), as does
`/hw_end --test`.

## 7. Files touched

| File | Change |
|---|---|
| `services/halloween.py` | `SEED_LOOT_MIN/MAX`, `ATTACK_CRIT_CHANCE`, `MILESTONE_REWARDS` + `milestone_reward_line()`, `BOSS_ART`/`BOSS_PHASE_TAUNTS`, `SeedLootStatus` + `get_seed_loot_status()`, `format_duration_fa()`, `seed_loot_range_fa()`, `AttackOutcome.phase_before/after` + `evolved`, new `START_ANNOUNCEMENT`, chest copy 3–6 |
| `handlers/halloween.py` | reply-trigger attack, `_run_attack()` (public line + owner-only menu refresh), phase-aware `_resolve_boss_photo()`, new menu/track formatting, legacy `hw:attack` routed through the same flow |
| `keyboards/halloween.py` | ⚔️ attack button removed (2 rows remain) |
| `database/models/halloween.py` | `ck_hw_drop_claims_seeds` → `BETWEEN 1 AND 6` |
| `database/migrations/versions/059_halloween_seed_loot_boost.py` | new revision (head) |
| `handlers/admin.py` | broadcast uses the static hype text |
| `assets/halloween/sibomortis_{75,50,25}.jpg` | new boss-evolution artwork |
| `tests/test_halloween_event.py`, `tests/test_halloween_foundation.py`, `tests/invasion_v2/test_phase8c.py` | TASK 7 (see below) |

## 8. Tests (TASK 7)

`tests/test_halloween_event.py` was rewritten around the new contract (24
tests): menu gate + new layout, no attack button, exact loot timer + Top-3
board, `format_duration_fa`, reply attack (seed spent, HP down, public line,
owner-only refresh), silence rules (no reply / unrelated reply / longer
sentence / wrong word / flag off), cross-user safety, 3–6 loot with the 4 h
cooldown, milestone table + all four claim payouts, Reward Track badges and
labels, legacy button path, evolution artwork per threshold, `evolved`
reporting, `/hw_start` hype broadcast, `--test` silence, `/hw_end`, Top-3
finalisation once, Day-7 weakening (now also proving phase 4), and group chests
paying 3–6.

`tests/test_halloween_foundation.py` gained migration 059 (constraint swap,
`6` accepted / `7` refused, indexes and uniques preserved, downgrade guard) and
asserts every phase's bundled artwork exists and decodes as 1024×1024 RGB.
`tests/invasion_v2/test_phase8c.py` pins the new migration head `059`.

Full suite on the sandbox SQLite test DB: identical failure set to the base
commit (28 pre-existing failures in unrelated asset-dimension and party-state
tests, 0 new).
