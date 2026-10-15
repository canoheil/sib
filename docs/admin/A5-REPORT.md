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

## 3. The new section tree

One place, one list: `sib/services/help_content.py` → `HELP_SECTIONS`.
Adding a section later = adding one `HelpSection` to that tuple; the index
text, the index keyboard and the navigation are all derived from it.

| # | section_key | title_fa | body chars | page chars |
|---|---|---|---|---|
| 1 | `quick_start` | 🍏 شروع سریع | 515 | 528 |
| 2 | `world` | 🌍 دنیای اواریا | 512 | 528 |
| 3 | `cards` | 🃏 کارت‌ها | 605 | 616 |
| 4 | `invasion` | ⚔️ تهاجم شیاسیب | 695 | 712 |
| 5 | `mine` | ⛏️ معدن سیبی | 395 | 409 |
| 6 | `apples` | 🎁 سیب‌ها و صندوق‌ها | 748 | 769 |
| 7 | `social` | 🌱 باغ، ازدواج، پارتی و مبادله | 818 | 849 |
| 8 | `games` | 🎮 بازی‌های جانبی | 558 | 576 |
| 9 | `shortcuts` | ⌨️ میان‌برها | 691 | 705 |
| 10 | `support` | 📞 پشتیبانی | 263 | 275 |

* 10 top-level sections (the brief allows 8-10), the tree is **flat**: no
  section needed a sub-page, so the maximum depth is 1 from the index. The
  data structure still carries `children`, and the handler already renders a
  child page (`help:section:<key>:<child>`), so a future split is a data
  change only.
* Every body is under 900 characters; every page (title + body) is under
  900 too, i.e. far below the 1024-character limit.
* The index itself is **287** characters.

### 3.1 The rendered index (Persian)

```
📖 راهنمای بازی سیب 🍏

این راهنما کوتاه و کامل است؛ هر بخش را جدا باز کن. اگر تازه‌ای، از «شروع سریع» شروع کن.

• 🍏 شروع سریع
• 🌍 دنیای اواریا
• 🃏 کارت‌ها
• ⚔️ تهاجم شیاسیب
• ⛏️ معدن سیبی
• 🎁 سیب‌ها و صندوق‌ها
• 🌱 باغ، ازدواج، پارتی و مبادله
• 🎮 بازی‌های جانبی
• ⌨️ میان‌برها
• 📞 پشتیبانی
```

Index keyboard: one button per section (`help:section:<key>`), two per row,
plus a «🏠 منوی اصلی» row. A section page carries exactly one button:
«🔙 بازگشت به راهنما» (`help:index`).

> **One Telegram detail:** the support handle contains an underscore, and
> Telegram's legacy Markdown parser treats an unpaired `_` as a reserved
> character (the whole page would fail with «can't parse entities»). The
> copy keeps the raw handle (that is the value a player copies) and the
> RENDERED page escapes it through the project's own
> `sib.core.text.escape_markdown` — `@sibgame\_official` renders as
> `@sibgame_official`.

### 3.2 One sample page (Persian) — «⚔️ تهاجم شیاسیب»

```
⚔️ تهاجم شیاسیب

شیاسیب هر چند ساعت به یکی از منطقه‌های اواریا یورش می‌آورد. رباتم یورش را در گروه اعلام می‌کند و دکمهٔ «⚔️ پیوستن به یورش» را زیر همان پیام می‌گذارد.

**چطور بجنگم؟** روی همان دکمه بزن؛ کارت اصلی‌ات یک ضربه می‌زند و جان شیاسیب کم می‌شود. هر نفر در هر یورش فقط یک ضربه می‌زند.

**قدرت ضربه:** نیروی کارت اصلی با ضریبی میان ۰٫۸۵ تا ۱٫۱۵ ضرب می‌شود و ۱۰٪ شانس ضربهٔ بحرانی داری؛ آن ضربه آسیب را دو برابر می‌کند. جان شیاسیب از میانگین قدرت کارت‌های گروه، ضربدر ضریب نوع دشمن و سختی یورش به دست می‌آید.

**پاداش:** اگر یورش را ببری، هر مدافع ۳ سیب طلایی می‌گیرد؛ با ۱۰٪ شانس یک سیب الماسی و با ۲٪ شانس یک سیب ابسیدینی هم اضافه می‌شود.

بدون کارت اصلی وارد یورش نمی‌شوی، پس اول کارت اصلی را انتخاب کن.
```

---

## 4. Before / after

**The old index** (one line, no structure):

```
🎮 **راهنمای بازی‌های سیب** 🍏

بخش مورد نظرت را انتخاب کن تا کوتاه و ساده توضیحش را ببینی:
```

with 8 topic buttons: 🎯 سنگ، کاغذ، قیچی · 🔴 چهار در یک ردیف · ❌ دوز ·
🚢 کشتی جنگی · 🃏 کارت بالا · ⚔️ نبرد سیبی · 🃏 کارت‌ها · 🌳 باغ.

**The new index**: a short introduction plus the ten section titles (see
§3.1) — the world, the cards, the invasion, the mine, the apples and the
chests, the social systems, the mini-games, the shortcuts and the support.

| | Old | New |
|---|---|---|
| Sections | 8 flat topics (mini-games only) | 10 sections covering every active feature |
| Ovarya / شیاسیب | ✗ | ✓ (a whole section) |
| Invasion v2 (join, HP, rewards) | ✗ | ✓ |
| Apple Mine (cooldown, reward table) | ✗ | ✓ |
| Chests (5) + the 4 apple types | ✗ | ✓ |
| Text shortcuts | ✗ | ✓ (every wake word) |
| Garden / Marriage / Party / Trade | one short line | one paragraph each |
| Support handle | ✗ | ✓ (@sibgame_official) |
| Callback scheme | `help_menu`, `help_page:<topic>` | `help:index`, `help:section:<key>` (legacy kept as an alias) |
| Section page buttons | 2 (back + home) | 1 («🔙 بازگشت به راهنما») |
| Copy style | mixed colloquial, one line per game, no ZWNJ discipline | ZWNJ, «هٔ», «گیومه», Persian digits, no bureaucratic/AI tells |

---

## 5. Tests

`tests/help/test_help_sections.py` — **13 tests, all passing**:

| Required test | Result |
|---|---|
| `test_help_index_renders_all_expected_sections` | PASS |
| `test_every_section_body_is_non_empty` | PASS |
| `test_every_section_body_is_persian_only` | PASS |
| `test_every_section_body_under_1024_chars` | PASS |
| `test_help_section_callback_edits_message` | PASS |
| `test_help_back_button_returns_to_index` | PASS |
| `test_sib_raahnamaa_opens_help_index` | PASS |
| `test_help_mentions_all_text_shortcuts` | PASS |
| `test_help_mentions_invasion` | PASS |
| `test_help_mentions_apple_mine` | PASS |
| `test_help_mentions_chests` | PASS |
| `test_help_mentions_support_handle` | PASS |
| `test_help_menu_button_opens_the_same_index` (extra) | PASS |

The Persian-only test masks the injected values (`INJECTED_VALUES` = the
support handle) and then asserts: no Latin letter, no ASCII digit, no
Arabic ي/ك, no Arabic-Indic digit, no em dash, no ASCII quote, and balanced
«گیومه» in every body.

### 5.1 Full suite (SQLite test database, no MariaDB / Telegram needed)

| Tree | Result |
|---|---|
| baseline `728f498` (untouched, same environment) | the same 7 failures (MariaDB-only tests, missing image assets, the alembic head check) |
| A5 tree | **7 failed, 2485 passed, 25 skipped** (255 s) |

The 7 failures are the pre-existing environment ones and are NOT related to
A5 (they also fail on the untouched baseline, verified in a separate
`git worktree` of `728f498`):

* `tests/invasion_v2/test_phase8c.py::test_migration_chain_head` (the
  alembic head is `045_group_welcomed_at`, the test still expects `043`);
* `tests/test_character_art.py` × 4 (missing image assets);
* `tests/test_navigation.py::test_shop_and_battle_and_garden_and_trade_have_back_to_main`
  (needs MariaDB);
* `tests/test_transaction_regressions.py::test_E_daily_chest_retry_keeps_the_callers_user_row`
  (needs MariaDB row locks).

Targeted re-runs of everything the rewrite touches:

```
tests/help  tests/shortcuts  tests/test_nav_root_fixes.py
tests/test_group_menu_games_guide.py  tests/test_apple_battle.py
tests/test_handlers.py  tests/test_a3_start_texts.py  tests/test_main_menu_layout.py
tests/test_main_menu_style.py  tests/test_screen_transitions.py
→ 119 passed
```

### 5.2 The Persian copy was checked with the skill's own scripts

```
python3 .agents/skills/persian-writing/scripts/persian_cleanup.py --edit --in copy.md --out copy.out.md
  → no change (the copy is already normalized)
python3 .agents/skills/persian-writing/scripts/fa_lint.py --check copy.md
  → 0 issues
```

---

## 6. Guardrails and limitations

**Guardrails respected**

* `invasion_enabled` is **untouched**: still seeded `'false'` in
  `database/migrations/versions/036_invasion_v2.py:131` and written only by
  `services/invasion_v2/admin_controls.py:65`. No file under
  `services/invasion_v2/` and no migration was modified.
* No invasion v2 logic, no business rule, no feature behaviour was changed:
  the diff is help text + help navigation only.
* No handler was deleted: `handle_help_menu` was rewritten, and
  `handle_help_page` still answers the old `help_page:<topic>` callbacks
  (mapped to the sections that replaced them).
* No `_archive_inv1_*` table was touched.
* Every user-facing string of the new guide is Persian; the only Latin
  string is the injected support handle.
* The commit messages all start with `[a5]`.

**Limitations**

* **The chest prices are a snapshot of the seed data**
  (`data/shop_chests/*.json`). They are the prices the shipped chests are
  applied with, but an administrator can change a price in the database;
  the guide text then drifts until it is edited. Making the guide read the
  live chest rows would mean a DB query on every page render, so the
  prices are kept as data in `help_content.py` (one place to edit).
* **The invasion numbers are the shipped rules** (10٪ crit, ×0.85-1.15,
  3 golden apples, 10٪ / 2٪ bonus). They come from
  `services/invasion_v2/damage.py` and `rewards.py`; a balance change
  would need the same edit. The guide deliberately does NOT mention the
  occurrence interval or the wave length, because those are runtime
  configuration keys (`invasion_occurrence_interval_seconds`,
  `invasion_wave_count`, `invasion_wave_duration_seconds`).
* **The section page has no «🏠 منوی اصلی» button** (the brief asks for a
  single «🔙 بازگشت به راهنما» button). The way home is one more press
  away, on the index, which keeps the «🏠 منوی اصلی» row.
* **The legacy `help_page:` callbacks are kept as an alias**, so old pinned
  messages keep working, but they are not shown anywhere any more.
* The `tests/help/` folder has no `__init__.py`, like every other test
  folder of the repository.
* The guide is **text only**: there is no banner art for the `help` key
  (`services/banner.py` ships `main_menu`, `games`, `garden`, `shop`,
  `leaderboard`), so the pages render as plain text screens, exactly like
  before.

---

## 7. Commits

```
[a5] recon for help rewrite            — the recon + the Persian rules
[a5] structured help data               — sib/services/help_content.py
[a5] rewrite help handler and navigation — handlers/start.py + the legacy tests
[a5] tests and report                  — tests/help/ + this report
```
