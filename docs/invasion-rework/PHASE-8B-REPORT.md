# Phase 8B — narrative pool rewrite

> **Phase 8C update:** `{n}` now works end-to-end. Production callers pass invasion
> IDs, and victory finalization passes the count of that invasion's stored attacks.
> VICTORY now has **18 variants: 6 with `{n}`, 12 without**; the full pool has **98**.
> All 92 Phase 8B variants are unchanged. Original inventory, example seeds and
> Phase 8B test results below are historical; see [PHASE-8C-REPORT.md](PHASE-8C-REPORT.md)
> for the current optional signatures, wiring, difficulty and 421-test validation.

## Pre-change inspection (recorded before modifying narrative.py)

Baseline: `628ea344e12d00b80aa68072a076fa0b6f821d27`.

The current module has seven public render functions, eight private five-entry pools (40 templates), and two private helpers. It already returns prose rather than the tabular example in the request. Its limitations are small pools, mechanical damage/HP wording, no recent-history avoidance, and no finishing-hit selection in render_join.

### Exact public signatures

```python
def render_join(*, username: str, card_name: str, damage: int, is_crit: bool, enemy_hp_remaining: int, rng: random.Random) -> str: ...
def render_high_damage(*, username: str, card_name: str, damage: int, enemy_hp_remaining: int, rng: random.Random) -> str: ...
def render_announcement(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random) -> str: ...
def render_finisher(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random) -> str: ...
def render_victory(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random) -> str: ...
def render_defeat(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random) -> str: ...
def render_expired(*, username: str, card_name: str, region_name: str, enemy_name: str, rng: random.Random) -> str: ...
```

All arguments are keyword-only. `rng` is supplied by callers. No signature accepts an invasion ID, group ID, participant count, or arbitrary context. `render_join` and `render_high_damage` also lack region/enemy-name arguments.

### Every old Persian template

These are the exact 40 base strings, before variable interpolation. The module can produce infinitely many strings through injected names and numbers; this inventories every fixed template, not every possible interpolation.

#### `_ANNOUNCEMENT`

1. بر فراز {region_name}، سپیده به رنگ خاکستر درآمده است. {username}، {card_name} را به گذرگاه ببر؛ {enemy_name} از مه بیرون آمده و به دروازه‌ها رحم نخواهد کرد.
2. آب‌های {region_name} بی‌صدا تیره شدند. {username}، در کنار {card_name} بایست؛ {enemy_name} پشت آخرین موج کمین کرده است.
3. زنگ‌های {region_name} بی‌آنکه دستی به آن‌ها برسد به صدا درآمدند. {username}، {card_name} را فرا بخوان؛ {enemy_name} بر آستانهٔ شهر ایستاده است.
4. بادِ {region_name} بوی آهن می‌دهد. {username} و {card_name} باید پیش از فروریختن نخستین برج برسند؛ رد پای {enemy_name} روی خاک مانده است.
5. فانوس‌های {region_name} رو به تاریکی می‌سوزند. {username}، {card_name} را کنار بارو نگه دار: نشانِ {enemy_name} در افق پیدا شده است.

#### `_JOIN`

1. {username} با {card_name} از میان غبار گذشت؛ ضربه‌ای به اندازهٔ {damage} بر زره شیاسیب نشست. هنوز {remaining} از جان دشمن مانده است.
2. {card_name} به اشارهٔ {username} پیش آمد و صف دشمن را شکافت. {damage} آسیب بر جای ماند و توان شیاسیب به {remaining} رسید.
3. در سکوت میدان، {username} راه را برای {card_name} گشود؛ تیغه با نیروی {damage} فرود آمد. شیاسیب با {remaining} جان عقب نشست.
4. {username} و {card_name} از پشت ستون‌ها بیرون جهیدند؛ سپر دشمن {damage} آسیب دید و هنوز {remaining} توان برایش باقی است.
5. خاک زیر پای {card_name} لرزید؛ {username} فرمان حمله داد و {damage} آسیب به شیاسیب رسید. جان باقی‌مانده‌اش {remaining} است.

#### `_CRITICAL`

1. {username} لحظهٔ گشودن زره را دید؛ {card_name} همان‌جا فرود آمد و {damage} آسیب بر جای گذاشت. شیاسیب با {remaining} جان به دشواری ایستاده است.
2. نورِ {card_name} به فرمان {username} از شکاف تاج دشمن گذشت؛ ضربهٔ سهمگین {damage} از توان شیاسیب کاست و {remaining} برایش ماند.
3. {username} نفسش را حبس کرد. یک حرکتِ {card_name} با نیروی {damage} صف شیاسیب را از هم درید؛ جان او به {remaining} رسید.
4. درست هنگام فرود گام دشمن، {username} به {card_name} اشاره کرد؛ زره ترکید، {damage} آسیب ثبت شد و فقط {remaining} جان باقی ماند.
5. {card_name} از دل سایه بیرون آمد و فرمان {username} را به ضربه‌ای سهمگین بدل کرد؛ {damage} آسیب و {remaining} جانِ باقی‌مانده، بهای آن لحظه بود.

#### `_HIGH_DAMAGE`

1. {username} و {card_name} چنان به دروازهٔ سپاه کوبیدند که لولاها از دیوار کنده شد؛ {damage} آسیب رسید و شیاسیب با {remaining} جان عقب رفت.
2. موجی از نیروی {card_name} به فرمان {username} سراسر میدان را پیمود؛ صف دشمن زیر {damage} آسیب فرو ریخت و تنها {remaining} جان ماند.
3. {username} راه سنگر را گشود و {card_name} آوار را بر سپاه شیاسیب فرود آورد؛ {damage} آسیب به جا ماند، توان دشمن {remaining} شد.
4. با یک حرکتِ {card_name}، هزار بازتاب به یک فروغ بدل شدند؛ {username} شکست صف دشمن را دید: {damage} آسیب و فقط {remaining} جان.
5. {username} کنار {card_name} بر خاکِ لرزان ایستاد؛ برخوردی با نیروی {damage} سنگرهای شیاسیب را خاموش کرد و {remaining} جان برایش گذاشت.

#### `_FINISHER`

1. {username} تیغهٔ {card_name} را برای آخرین بار بالا آورد؛ نشانِ {enemy_name} در خاکِ {region_name} فرو رفت و دیگر دستی برای برداشتنش نماند.
2. به اشارهٔ {username}، {card_name} واپسین رشتهٔ تاریکی را در {region_name} برید. {enemy_name} خاموش شد و نور دوباره به زمین رسید.
3. ضربهٔ پایان را {card_name} در کنار {username} فرود آورد؛ پژواکِ {enemy_name} در {region_name} شکست و صدای نفس مدافعان شنیده شد.
4. {username} با {card_name} از میان آوار گذشت؛ آخرین جرقه بر زره {enemy_name} نشست و {region_name} از سایه بیرون آمد.
5. تاجِ {enemy_name} زیر ضربهٔ {card_name} فرو افتاد. {username} در {region_name} ایستاد تا چراغ نگهبانان دوباره روشن شود.

#### `_VICTORY`

1. {region_name} هنوز ایستاده است. {username}، رد تیغهٔ {card_name} بر سنگ دروازه مانده؛ مردم نام مدافعان را آهسته می‌گویند و {enemy_name} دیگر پیش نمی‌رود.
2. نخستین رهگذر پس از نبرد از {region_name} گذشت. {username} و {card_name} کنار راه ایستاده‌اند؛ آب دیگر تصویر {enemy_name} را بازنمی‌گرداند.
3. پرچم‌های {region_name} دوباره در آفتاب پیدا شدند. {username}، {card_name} را پایین آورد؛ شهر پیش از فریاد پیروزی نفس راحتی کشید و {enemy_name} گریخت.
4. سنگرهای {region_name} فرو نریختند. {username} و {card_name} به افق نگاه کردند؛ جای پای {enemy_name} هنوز تازه است، اما این خاک امروز از آنِ او نیست.
5. فانوس‌های {region_name} یکی‌یکی روشن شدند. {username}، {card_name} را کنار گذرگاه نشاند؛ کاروان‌ها در سکوت از کنار میدانِ رهاشده از {enemy_name} گذشتند.

#### `_DEFEAT`

1. دروازهٔ {region_name} شکست. {username}، {card_name} را از زیر باران سنگ بیرون کشید؛ {enemy_name} این خاک را گرفت، اما صدای بازماندگان خاموش نشده است.
2. وقتی {username} و {card_name} به کرانه رسیدند، {region_name} دیگر نور را نمی‌پذیرفت. {enemy_name} بر فراز آن پرچم زده؛ راه بازگشت باید دوباره گشوده شود.
3. برج‌های {region_name} یک‌به‌یک تار شدند. {username}، {card_name} را پشت آخرین دیوار نگه داشت؛ زمین به دست {enemy_name} افتاد، نه سوگند نگهبانانش.
4. باد در {region_name} ایستاد و دیوار فرو ریخت. {username} و {card_name} آخرین نفرات را به راه امن بردند؛ {enemy_name} زمین را گرفت، نه امیدشان را.
5. چراغِ {region_name} خاموش شد. {username}، {card_name} را از گذرگاه شکسته گذراند؛ {enemy_name} اکنون بر ویرانه‌ها نگهبانی می‌دهد.

#### `_EXPIRED`

1. وقتی خبر به {region_name} رسید، پنجرهٔ نبرد گذشته بود. {username} و {card_name} کنار دروازه ایستادند؛ از ساعت‌های بی‌خبر در برابر {enemy_name} هنوز روایتی قطعی در دست نیست.
2. پیامِ {username} و {card_name} دیر از مهِ {region_name} گذشت. زمان فراخوان پایان یافته بود و هیچ‌کس سرانجام رویارویی با {enemy_name} را از سکوت نمی‌خواند.
3. در {region_name}، {username} نامِ {card_name} را بر سنگ سرد نوشت؛ پیغام به‌موقع نرسید و خبری از پایان راهِ {enemy_name} در دست نیست.
4. {username} و {card_name} فانوسِ {region_name} را تا واپسین لحظه روشن نگه داشتند. با بازگشت صدا، فرصت پاسخ به {enemy_name} گذشته بود؛ روایت هنوز ناتمام است.
5. {username} تصویرِ {card_name} را کنار گذرگاهِ بی‌خبر گذاشت. {region_name} از خاموشی پیام بیرون آمد، اما ساعت‌های رویارویی با {enemy_name} بازنمی‌گردند.

### Private helpers and current behavior

- `_fa(number: int) -> str` maps ASCII digits using `۰۱۲۳۴۵۶۷۸۹`; it adds no other Persian prose.
- `_render(templates: tuple[str, ...], rng: random.Random, **values: object) -> str` uses `rng.randrange` then `str.format`; there is no length bound or repetition memory.
- `render_join` chooses only normal/critical by `is_crit`, even if remaining HP is zero.
- `render_high_damage` uses its separate five-entry pool. `render_finisher` must be called explicitly.
- Actual call sites are the join handler and presentation layer, not imaging/compose.py. Those files will remain unchanged in this phase.

## New pool inventory

**98 variants currently: 21 reference paragraphs + 71 Phase 8B additions + 6 Phase 8C counted-victory additions.**
All seven requested minimum counts are exceeded or met. Each list starts with
exactly its first three supplied samples, preserving wording, spelling and
punctuation (only presentation line wrapping was removed). The fourth/fifth join
samples in the prompt were references, not required verbatim entries.

For the otherwise-unspecified rhythm ranges, this phase uses **short <100**,
**medium 100–179**, and **long 180–899** characters in the source template.
Every category has at least two of each. These counts include placeholder names;
tests separately render every variant with normal and very long injected values.

| Category | Count | Short | Medium | Long | Maximum template characters |
|---|---:|---:|---:|---:|---:|
| `ANNOUNCEMENT` | 12 | 2 | 8 | 2 | 214 |
| `JOIN_NORMAL` | 22 | 2 | 18 | 2 | 226 |
| `JOIN_CRIT` | 16 | 2 | 12 | 2 | 223 |
| `JOIN_KILL` | 10 | 2 | 6 | 2 | 219 |
| `VICTORY` | 18 | 2 | 14 | 2 | 217 |
| `DEFEAT` | 12 | 2 | 8 | 2 | 228 |
| `EXPIRED` | 8 | 3 | 3 | 2 | 229 |

Additional passages vary defense at a gate, bridge, alley, shelter, mill, tower,
caravan and mountain pass. Victory emphasizes return to ordinary life; defeat
emphasizes shelter and the resolve to return. No new copy describes gore, blood,
dismemberment, screams of pain or graphic death. New announcement passages address
defenders collectively. New attack passages use third-person players and naturally
include both user and card. Numerical damage/remaining strength appears only in
some prose, not mechanically in every entry. Added openings were reviewed for
variety, and adjacent final verbs were edited to avoid repetition.

## API mapping and rendering

Phase 8B preserved all public signatures exactly. Phase 8C retains every original
argument and appends optional `invasion_id=None` to all seven functions, plus
`participant_count=None` to victory; announcement/join/result callers are now wired.
The inventory above records the original signatures, not the Phase 8C additions.

| Function / condition | Pool |
|---|---|
| `render_announcement` | `ANNOUNCEMENT` |
| `render_join`, remaining HP <= 0 (including critical hits) | `JOIN_KILL` |
| `render_join`, positive HP and `is_crit=True` | `JOIN_CRIT` |
| `render_join`, positive HP and normal hit | `JOIN_NORMAL` |
| `render_high_damage`, positive HP | `JOIN_CRIT` (strong-impact prose) |
| `render_high_damage`, remaining HP <= 0 | `JOIN_KILL` |
| `render_finisher` | `JOIN_KILL` |
| `render_victory` | `VICTORY` |
| `render_defeat` | `DEFEAT` |
| `render_expired` | `EXPIRED` |

`render_high_damage` retains its legacy purpose as an explicitly selected
strong-impact event; sharing that pool does **not** set or infer a critical flag.
`render_finisher` remains callable separately. The join handler already passes the
calculated remaining HP, so finishing-hit selection now works without changing it.
No mechanics, damage calculation or persistence behavior is introduced here.

Values are inserted once using `str.format`; braces inside a supplied display name
remain data, not a second format pass. Damage/HP use the preserved `_fa` numeral
conversion. Display values are kept inline and clipped at 96 characters (including
an ellipsis when needed), keeping unusually long names from overwhelming captions.
A final 899-character guard also protects future template additions. Existing Latin
characters in user-provided names are not transliterated or silently removed;
no-English tests mask injected values before inspecting rendered narrative copy.
All new fixed narrative copy is Persian, apart from the allowed placeholder names.

## Anti-repetition design

Public picker signature:

```python
def pick(pool: list[str], *, key: str, history_size: int = 5) -> str
```

- A module-local dictionary records a per-key pick counter and the last-used
  position of each distinct template. This is equivalent to checking the last N
  picks without keeping an ever-growing list of events. It also handles callers
  changing `history_size` without losing relevant past positions.
- The next selection excludes every template seen in the last five picks for the
  same key by default. Exact duplicate strings in an input pool count as one
  variant, so duplicate indices cannot evade exclusion.
- If the distinct pool size is **at most** the history size, strict exclusion is
  mathematically unsustainable; the constraint is skipped for that call. This
  includes equality, not only smaller pools. All supplied production pools have
  more than five variants, so their normal path uses strict exclusion.
- Size zero disables exclusion but records the pick; negative history size and
  an empty pool raise clear `ValueError`s. A singleton pool always works.
- Selection and history update share a short `threading.Lock` critical section.
  There is no await or I/O inside it. Tests cover concurrent worker-thread picks,
  stronger than the normal single-event-loop requirement.
- A private `ContextVar` context carries the existing caller-supplied `rng` into
  `pick`, without changing its signature, reseeding global randomness, or sharing
  one caller's RNG with another. It is reset in `finally`. Public standalone picks
  use the module random source.
- Given the same seed **and starting history**, output sequences replay exactly.
  Reusing a seed alone intentionally no longer returns the same recent paragraph;
  that would contradict anti-repetition. Existing replay tests were updated to
  make starting history explicit rather than disabling the new feature.

### Key scope (Phase 8C resolves the original caller limitation)

With `invasion_id`, renderers now use the requested event/invasion keys:
`announcement`, `join_normal`, `join_crit`, `join_kill`, `victory`, `defeat`, `expired`.
Without an ID, the exact original player/card or region/enemy hash fallback remains.
Victory uses only counted variants when `participant_count` is supplied (including
zero), otherwise only the original uncounted variants. `{n}` is formatted in Persian
digits. Presentation counts that invasion's attack rows once on victory, not its
reward rows. Six counted variants support the existing recent-five exclusion.

The original lack of ID/count inputs is no longer a production caller limitation.
Process-local history still resets on restart, is not shared across workers, records
selection rather than successful delivery and grows with the number of context keys.
Legacy callers that omit the ID still share the broader Phase 8B fallback scope.

## Seven example rendered outputs

The requested six-example chat summary is supplemented here with all seven
categories, including expiry. These were produced through the public render APIs,
with an empty history per example and the documented RNG seed. Names are داریوش,
سیب سرخ, درّه ماه, شیاسیب سایه; when used, damage is 123 and remaining HP is 456
(or zero for the finisher).

### ANNOUNCEMENT (seed 7, entry 6)

پیش از آنکه زنگ آخر در درّه ماه بنوازد، خود را به بارو برسانید. شیاسیب سایه راه کاروان‌ها را بسته است؛ پشت این دیوار، کودکانی چشم به راه بازگشت خانواده‌هایشان دارند. مدافعان، نگذارید آن چشم‌انتظاری به ترس بدل شود.

### JOIN_NORMAL (seed 18, entry 6)

زیر باران خاک، داریوش کنار سیب سرخ زانو زد و تکیه‌گاه سپرها را محکم کرد. هجوم شیاسیب به دیوار دفاع خورد؛ ۱۲۳ از توانش کاسته شد، اما هنوز ۴۵۶ نیرو برای پیشروی داشت. راه پشت سنگر برای مردم باز ماند.

### JOIN_CRIT (seed 23, entry 10)

سکوتی به اندازهٔ یک دم؛ سپس فروغِ سیب سرخ. داریوش ضربه را بر نگهدارندهٔ سپر دشمن نشاند و صف فشردهٔ شیاسیب از هم فاصله گرفت؛ محاصره دیگر حلقه‌ای بسته نبود.

### JOIN_KILL (seed 3, entry 4)

کنار دروازه، داریوش با سیب سرخ واپسین یورش را شکست. راه خانه‌ها آزاد شد.

### VICTORY (seed 9, entry 8)

آب دوباره از جوی‌های درّه ماه گذشت. داریوش و سیب سرخ کنار پل ماندند تا آخرین خانواده عبور کند؛ این خاک امروز زیر نگاه پاسدارانش آرام گرفت.

### DEFEAT (seed 9, entry 8)

دروازه تاب نیاورد. داریوش و سیب سرخ مردم را از راه فرعی درّه ماه بیرون بردند؛ دشمن سنگ‌های شهر را گرفت، اما نتوانست نام خانه را از یاد آنان ببرد.

### EXPIRED (seed 9, entry 8)

بر سنگ دیده‌بانی تنها غبار مانده است. فرصت پاسخ گذشته و هیچ خبری سرانجام نبرد را روشن نمی‌کند؛ مدافعان گوش به راه تازه دارند، بی‌آنکه این سکوت را نشانهٔ پیروزی بدانند.

## Tests and validation

- Narrative-focused suite: **181 passed** (33 updated existing tests plus 148 new
  parametrized pool/formatting/picker cases).
- Full scoped suite: **349 passed, 0 failed**, 14.48 seconds:
  `tests/invasion_v2` plus `tests/test_invasion_feature_flag.py`.
- Tests cover every minimum count, all 92 source and rendered lengths (normal and
  oversized values), exact first-three reference copy, placeholder vocabulary and
  availability, Persian-only fixed text, numeral rendering, protected tone checks,
  short/medium/long balance, original openings, pool routing including negative HP
  and critical finishers, public signature preservation, stable context keys,
  RNG isolation/replay, independent keys, dynamic history windows, small/duplicate/
  empty pools and thread-safe recent exclusion.
- The E2E test now also checks that the persisted winning attack belongs to
  `JOIN_KILL`. The first full run exposed its pre-existing 30ms sleep assumption:
  `delivered_at` was checked before detached delivery completed (348 passed,
  1 failed). The same test passed in isolation. Replaced fixed sleeps **in the test
  only** with bounded waits for actual join/finalize completion; production handlers,
  presentation and scheduling code were not modified. The full rerun above passed.
- Previous dry-format assumptions (every line includes every field; seed alone
  yields identical repeated text; every differently named input yields unique
  text) were replaced with selected-pool membership, history-aware replay and
  recent-exclusion assertions. Mandatory supplied paragraphs omit some fields,
  so adding those fields back would violate the verbatim instruction.

Reproduce from the checkout root:

```bash
PYTHONPATH="$(dirname "$PWD"):$PWD" \
  sib_TEST_DATABASE_URL="sqlite+aiosqlite:///$PWD/tmp/sibcom_sib_test.db" \
  .venv/bin/pytest tests/invasion_v2 tests/test_invasion_feature_flag.py -q
```

## Copy exceptions and remaining limitations

1. The explicit verbatim-first-three rule takes precedence over contradictory
   general style rules for those 21 entries. For example, two supplied join/kill
   openings both start with `{user}`; one kill reference omits `{card}`; some
   announcements do not directly address the group; the third victory addresses
   a singular reader; some expiry samples assert enemy disappearance rather than
   uncertainty. Those were preserved, not silently rewritten. New expiry copy
   avoids declaring an unobserved victory, defeat or territory change.
2. **Resolved in Phase 8C:** optional ID/count arguments were authorized and wired.
   Only legacy calls omitting the new arguments retain context-based anti-repetition
   and uncounted victory prose. No participant count is fabricated.
3. Existing persisted `Inv2Attack.narrative` rows are not rewritten. New renders
   use the new pools; replay of old stored attack text naturally retains old copy.
4. Imaging text, button labels, separate reward summaries/critical caption suffixes,
   and other Persian strings outside this narrative module are unchanged, as
   required. This phase does not rewrite the entire bot's messaging.
5. A native-reader editorial acceptance pass is still useful for aesthetic taste;
   automated checks cannot establish literary quality. No live Telegram or MariaDB
   exercise was performed, and no whole-repository green-suite claim is made.

## Safety and commits

- `system_config.invasion_enabled` was not written or enabled. Its supplied
  production state remains false; no live DB query was performed to re-audit it.
  Test flag toggles occur solely inside existing disposable SQLite fixtures.
- No production `_archive_inv1_*` tables were accessed. Existing archive migration
  tests use isolated test databases only.
- Imaging, compose.py, scheduler, recovery, circuit breaker, resolvers, join handler,
  presentation, admin handlers, migrations and Phase 8A assets are unchanged from
  the Phase 8B baseline.
- Changes are restricted to `narrative.py`, new `narrative_pool.py`, narrative/E2E
  tests, and this phase report. No dependency was added.
- Four requested commit chunks use the `[phase-8b]` prefix on the session branch.
  No push, merge, deployment or feature activation was performed.
