# Phase A5 — Complete help section rewrite

Recon for the help rewrite, the Persian copy rules that were applied, the
new section tree, the before/after of the guide, the test results and the
limitations.

Line numbers are from the tree at commit `728f498` (pre-A5) unless stated
otherwise.

---

## 1. Recon findings

### 1.1 The current help handler and its data structure

| Piece | Location (pre-A5) | What it was |
|---|---|---|
| `HELP_TOPICS` | `handlers/start.py:144` | A flat dict of **8** topics → `(label, body)`: `rps`, `c4`, `ttt`, `battleship`, `high_card`, `apple_battle`, `cards`, `garden`. No Ovarya, no Invasion v2, no Apple Mine, no chests, no shortcuts. |
| `_help_keyboard()` | `handlers/start.py:201` | Two buttons per row, one per topic, `help_page:<topic>` callbacks, plus a «🏠 منوی اصلی» row. |
| `_help_menu_screen()` | `handlers/start.py:223` | The index text («🎮 **راهنمای بازی‌های سیب** 🍏») + the keyboard. One builder for the button, the callback and the «سیب راهنما» shortcut. |
| `handle_help_menu()` | `handlers/start.py:239` | Callback `help_menu` → edits the message with the index. |
| `handle_help_page()` | `handlers/start.py:258` | Callback `help_page:<topic>` → edits the message with the topic page; back button `help_menu` + a «🏠 منوی اصلی» row. |
| «📖 راهنما» button | `keyboards/common.py:151` (group menu), `keyboards/common.py:96` (private menu) | `callback_data="help_menu"`. |
| «سیب راهنما» shortcut | `handlers/start.py:1038` (`_open_help_menu`) → `handlers/start.py:1077` (`handle_sib_shortcut_word`) | Opens `_help_menu_screen()` — the same index as the button. |
| Legacy `HELP_TOPICS` readers | `tests/test_apple_battle.py:1407`, `tests/test_nav_root_fixes.py:406` | Two tests read the old dict directly. |

### 1.2 Every text shortcut currently registered

All of them live in `handlers/start.py` (router `start`, registered LAST in
`handlers/__init__.py`), as whole-message matches (`^\\s*…\\s*$`).

| Wake word | Pattern (pre-A5) | Handler | Opens |
|---|---|---|---|
| «سیب منو» / «سیب بازی» | `r"^\s*سیب\s+(بازی\|منو)\s*$"` | `handle_sib_games_word` `:719` | Main menu / games menu |
| «سیبام» | `r"^\s*سیبام\s*$"` | `handle_sib_balance_word` `:735` | The sender's apple balance |
| «سیب پروفایل» / «سیب من» | `r"^\s*سیب\s+(پروفایل\|من)\s*$"` | `handle_sib_profile_word` `:879` | The sender's profile |
| «سیب باغ» | `r"^\s*سیب\s+باغ\s*$"` | `handle_sib_garden_word` `:917` | The sender's garden |
| «باغ من» | `r"^\s*باغ\s+من\s*$"` | `handle_my_garden_word` `:923` | The sender's garden |
| «سیب شاپ» | `SIB_SHORTCUT_PATTERN` `:953` (map `SIB_SHORTCUT_TARGETS` `:940`) | `handle_sib_shortcut_word` `:1077` | `/shop` |
| «سیب کارت» | same | same | «کارت‌های من» |
| «سیب اسکین» | same | same | «تغییر پوسته» |
| «سیب لیدربرد» / «سیب لیدربورد» | same | same | `/leaderboard` |
| «سیب راهنما» | same | same | The guide index |
| «سیب» | `r"^\s*سیب\s*$"` | `handle_sib_wake_word` `:1096` | **Apple claim** (groups only) |

Other wake words owned by other modules (they are documented in the guide
too): «معدن سیبی» (`handlers/apple_mine.py:41`), «سیب صندوق»
(`handlers/daily_chest.py:127`), «ازدواج سیبی» / «طلاق سیبی» / «گل سیبی»
(`handlers/marriage.py:130/141/149`), «بغل سیبی» (`handlers/sib_hug.py:37`),
«بوس سیبی» (`handlers/sib_kiss.py:36`).

### 1.3 The exact Apple Mine reward table

`services/apple_mine.py:19` — `WORK_COOLDOWN_SECONDS = 1800` (30 minutes);
`services/apple_mine.py:25` — `OUTCOMES` (weights out of 100):

| Outcome | Weight | Chance | Item | Amount |
|---|---|---|---|---|
| `normal` | 60 | ۶۰٪ | `apple_normal` (wallet currency) | ۱۵ |
| `golden` | 25 | ۲۵٪ | `apple_gold` | ۳ |
| `diamond` | 12 | ۱۲٪ | `apple_diamond` | ۱ |
| `obsidian` | 3 | ۳٪ | `apple_obsidian` | ۱ |

One shift = exactly one reward. The selected card is **checked for
ownership but neither consumed nor a bonus** (`services/apple_mine.py:96`
and the docstring at `:88`).

### 1.4 The five shop chests (price + rewards)

Data files (the CLI writes them into the DB; the bot reads the DB):
`data/shop_chests/green_apple_chest.json` and
`data/shop_chests/additional_chests.json`; rules in
`services/shop_chests.py` and `docs/shop-chests.md`.

| Chest | Code | Price | Duplicate compensation | Rewards (weight) |
|---|---|---|---|---|
| صندوق سیبی | `green_apple_chest` | ۱۵۰ | ۵۰ | سیب سرخ ۱۵، سیب سبز ۱۵، سیب طلایی ۱۵، سیب ماهی ۱۵، حلقه ازدواج ۱، ۱۰۰ سیب ۳۹ |
| صندوق باغ افسون | `enchanted_garden_chest` | ۸۰ | ۰ | پودر جادویی ۴۰، سیب طلایی ۳۰، سیب الماسی ۸، سیب ابسیدینی ۲، ۴۰ سیب ۲۰ |
| صندوق امپراتوری | `empire_chest` | ۲۰۰ | ۶۰ | سیب سرخ ۱۴، فیلو ۱۴، گوجه ۱۴، ایگریس ۱۴، زاروش ۱۴، گل ۱۹، حلقه ازدواج ۱، ۱۲۰ سیب ۱۰ |
| صندوق فرزانگان | `sages_chest` | ۲۰۰ | ۶۰ | سیب سبز ۱۴، سیب طلایی ۱۴، شوالیه سبز ۱۴، پادشاه فرزانگان ۱۴، ترینی ۱۴، پودر جادویی ۱۵، سیب طلایی ۵، ۱۲۰ سیب ۱۰ |
| صندوق مهتاب | `moonlight_chest` | ۲۰۰ | ۶۰ | سیب ماهی ۱۴، امونگ ۱۴، شوالیه ماه ۱۴، لیلی ماه ۱۴، جادوگر ۱۴، سیب الماسی ۱۵، سیب ابسیدینی ۵، ۱۲۰ سیب ۱۰ |

A card the player already owns is converted to the compensation in Apples,
with no collection XP (`services/shop_chests.py:11`).

The **daily chest** (`صندوقچهٔ گنج روزانه`, `services/daily_chest.py`) is a
different system: once per UTC day, one weighted roll over سیب معمولی،
نهال، سیب طلایی، الماسی، ابسیدینی and a collectible card.

### 1.5 The exact Invasion v2 reward rules

| Rule | Where | Value |
|---|---|---|
| Join | `handlers/inv2_join.py:36` | One button («⚔️ پیوستن به یورش») on the announcement; one attack per player per invasion (`Inv2Attack` uniqueness) |
| Precondition | `handlers/inv2_join.py:72` | The player needs a **main card**; without it the join is refused |
| Damage | `services/invasion_v2/damage.py:6` | `round(power × uniform(0.85, 1.15) × (2 if crit else 1))`; crit chance **۱۰٪** |
| Enemy HP | `services/invasion_v2/enemy.py:50` | `round(avg group card power × enemy multiplier × difficulty)`; enemy multipliers: پیاده ۱٫۰، سایه ۱٫۶، سردار ۳٫۰، سنگین ۵٫۰، پادشاه ۸٫۰ |
| Join window | `services/config_store.py:151-169` | Occurrence interval 4 h; a battle is 4 waves × 5 min; the window closes when the invasion is finalized |
| Victory reward | `services/invasion_v2/rewards.py:8` + `rewards_service.py:20` | Every attacker gets **۳ سیب طلایی**, plus **۱۰٪** chance of ۱ سیب الماسی and **۲٪** chance of ۱ سیب ابسیدینی. Granted **only on a victory** (`settle_outcome` → `grant_victory_rewards`); a defeat pays nothing in v2 |

### 1.6 The exact daily-claim rules

`services/sib_claim.py:108` — the bare «سیب» in a **group** credits the
sender's wallet:

* amount: `sib_claim_amount` = **۵** سیب (`services/config_store.py:257`);
* cooldown: `sib_claim_cooldown_seconds` = **۳۰۰** s = ۵ minutes
  (`services/config_store.py:258`);
* the award is ledger-idempotent (`reason='sib_claim'`) and row-locked.

### 1.7 The four apple types and where they are used

| Apple | Where it lives | Where it is used |
|---|---|---|
| 🍎 سیب معمولی | The **wallet** (`apple_wallets`), never an inventory item (`services/items.py:30`) | The currency: shop, chests, card upgrades, garden, trading |
| 🌕 سیب طلایی | `user_items` (`apple_gold`) | Card upgrades (levels 1→4), Apple Mine, shop chests, daily chest, invasion rewards |
| 💎 سیب الماسی | `user_items` (`apple_diamond`) | Card upgrades (levels 4→7), Apple Mine, shop chests, daily chest, invasion rewards |
| 🌑 سیب ابسیدینی | `user_items` (`apple_obsidian`) | Card upgrades (levels 7→10), Apple Mine, shop chests, daily chest, invasion rewards |

The upgrade cost table (`services/config_store.py:64`): 100/150/250 سیب + 3/5/8
سیب طلایی (1→4), 400/600/800 سیب + 4/6/9 سیب الماسی (4→7), 1000/1300/1600 سیب
+ 4/6/8 سیب ابسیدینی (7→10). Cards are level 1..10 and every level adds
+10 to each of the three attributes (`services/card_progression.py:27`).

### 1.8 The legacy mini-games still active

`games/registry.py` + `handlers/games/*` + `keyboards/party.py:27`
(`GAME_ICONS`):

| Game | Code |
|---|---|
| کارت بالا | `high_card` |
| سنگ، کاغذ، قیچی | `rock_paper_scissors` |
| دوز | `tic_tac_toe` |
| چهار در یک ردیف | `connect_four` |
| کشتی جنگی | `battleship` |

Plus the flagship card duel **نبرد سیبی** (`apple_battle`), which has its
own entry in the games menu (`keyboards/party.py:85`).

### 1.9 The support handle string

`handlers/start.py:33` — `SUPPORT_HANDLE = "@sibgame_official"`, used in
both /start texts (`handlers/start.py:51` and `:79`).

---

## 2. Persian writing skill rules applied

The repo's own skill (`.agents/skills/persian-writing/`) was read before
any copy was written, and its mechanical scripts were run over the final
copy (`scripts/persian_cleanup.py --edit` → no change;
`scripts/fa_lint.py --check` → 0 issues).

| Rule (skill) | How it was applied |
|---|---|
| **ZWNJ (نیم‌فاصله)** in every compound | بازی‌ها، کارت‌ها، سیب‌ها، میان‌برها، می‌خری، می‌شود، تازه‌ای، روزانه‌ات، یکی‌یکی، سایه‌ها، بی‌دقت … |
| **Persian characters only** (ی U+06CC, ک U+06A9) | No Arabic ي/ك anywhere; the copy is generated as UTF-8 Persian, and the test asserts it |
| **Persian digits** (۰۱۲۳۴۵۶۷۸۹) | Every number in the copy is a Persian digit, produced through the existing helper `sib.services.sib_claim.fa_digits`; no ASCII digit survives in a body |
| **Persian decimal separator** | «۰٫۸۵» / «۱٫۱۵» (U+066B), not `0.85` |
| **Persian punctuation** (، ؛ ؟) | No ASCII comma/semicolon/question mark; the comma is «،» and the semicolon is «؛» |
| **«گیومه» for quotes** | Every quote is «...»; no `"` anywhere |
| **هکسره / ezafe «هٔ»** | «سایهٔ شیاسیب»، «دکمهٔ «⚔️ پیوستن به یورش»»، «ضربهٔ تو»، «حلقهٔ ازدواج»، «صندوقچهٔ گنج روزانه»، «مادهٔ ارتقا»، «پایهٔ هر شخصیت»، «مسابقهٔ دوستانه»، «تغییر پوستهٔ پروفایل» — never «ه» as a fake ezafe |
| **No em/en dash** | Restructured into «،» / «؛» everywhere (e.g. «در این سرزمین …»); the test asserts no `—` |
| **One space between words, no double spaces** | The linter reports 0 issues |
| **Bureaucratic tells banned** (می‌باشد، لازم به ذکر است، در راستای) | None of them; the copy is direct («تو کارت اصلی‌ات را انتخاب کن») |
| **AI tells banned** (rule-of-three triads, «در نهایت می‌توان گفت») | No triplets, no «در نهایت», no «کارشناسان معتقدند»; the sentences vary in length |
| **One point of view, direct address** | The second person «تو» throughout, like the rest of the bot's voice (`_private_start_text`) |
| **Numbers as words vs digits** | Statistics and prices are digits; the small counts inside a sentence are spelled out where it reads better («هر نفر یک ضربه») |
| **Register** | Telegram copy → colloquial-written Persian, with the epic register of the invasion announcements in the «دنیای اواریا» and «تهاجم شیاسیب» sections only |
| **Only the verified bullet glyph** | «•» (the skill lists • and · as the only glyphs verified in the bundled Vazirmatn/Lalezar) |

---

*(Sections 3-6 — the new tree, the before/after, the tests and the
limitations — are added in the final A5 commit.)*
