# Phase 0 — current invasion code state

This is a **source-only** snapshot. No DB connection, migration, restart, log inspection, or CPU measurement was performed. The operator reports that `system_config.invasion_enabled` is already `false`; this repository cannot verify that claim. The runtime gate fails closed when the row is missing or a read fails, and caches the result for 60 seconds. Explicit calls to domain service methods from other code are not a public Telegram entry point and should not bypass the guarded production call sites when new entry points are added.

## File inventory

Command (run from repository root):

```sh
find . -name "*invasion*" -not -path "*/node_modules/*" -exec wc -l {} \;
```

Output (includes local ignored `__pycache__` artifacts; directory names produce `wc` errors):

```text
0 ./.agents/tasks/moon-descendant-invasion
173 ./analysis/invasion_balance_report.md
5001 ./analysis/invasion_balance_scenario_matrix.csv
413 ./analysis/invasion_balance_simulation.py
0 ./assets/invasion
667 ./assets/invasion/moon_descendant_invasion.jpg
223 ./database/migrations/versions/026_invasion_persistence.py
67 ./database/migrations/versions/027_invasion_contribution_idempotency.py
117 ./database/migrations/versions/028_invasion_defense_actions.py
69 ./database/migrations/versions/031_invasion_four_wave_battles.py
302 ./database/models/invasion.py
59 ./database/models/invasion_contribution.py
122 ./database/models/invasion_defense_action.py
46 ./database/models/__pycache__/invasion_contribution.cpython-311.pyc
111 ./database/models/__pycache__/invasion.cpython-311.pyc
84 ./database/models/__pycache__/invasion_defense_action.cpython-311.pyc
341 ./docs/invasion-four-wave-battles.md
56 ./docs/invasion-telegram.md
0 ./docs/invasion-rework
872 ./handlers/invasion.py
461 ./handlers/__pycache__/invasion.cpython-311.pyc
144 ./keyboards/invasion.py
37 ./keyboards/__pycache__/invasion.cpython-311.pyc
2255 ./services/invasion.py
227 ./services/invasion_rewards.py
1012 ./services/invasion_telegram.py
1000 ./services/__pycache__/invasion.cpython-311.pyc
111 ./services/__pycache__/invasion_rewards.cpython-311.pyc
557 ./services/__pycache__/invasion_telegram.cpython-311.pyc
711 ./tests/test_invasion_defense_actions.py
289 ./tests/test_invasion_end_to_end.py
1122 ./tests/test_invasion_engine.py
1265 ./tests/test_invasion_four_wave.py
468 ./tests/test_invasion_maintenance_recovery.py
124 ./tests/test_invasion_migration_031.py
455 ./tests/test_invasion_models.py
106 ./tests/test_invasion_region_scheduler.py
377 ./tests/test_invasion_rewards.py
2656 ./tests/test_invasion_telegram.py
97 ./tests/test_invasion_feature_flag.py
40 ./tests/__pycache__/test_invasion_feature_flag.cpython-311-pytest-9.1.1.pyc
wc: ./.agents/tasks/moon-descendant-invasion: Is a directory
wc: ./assets/invasion: Is a directory
wc: ./docs/invasion-rework: Is a directory
```

## Persian text occurrences

All matches, with file/line numbers, from the requested `services/invasion*.py` and `services/maintenance/*.py` search, plus `handlers/invasion.py` (the user-facing invasion callback). This is a **literal grep snapshot**, so it includes snippets and comments, not only complete message templates. Multi-line templates span adjacent lines. English-only templates are not represented here. PCRE Unicode code-point range is used because this environment's locale rejects the basic `[آ-ی]` range with “Invalid collation character”.

```sh
grep -nP '[\x{0600}-\x{06FF}]' services/invasion*.py services/maintenance/*.py handlers/invasion.py
```

```text
services/invasion_telegram.py:101:_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
services/invasion_telegram.py:116:        return "نامشخص"
services/invasion_telegram.py:122:        pieces.append(f"{_fa(hours)} ساعت")
services/invasion_telegram.py:124:        pieces.append(f"{_fa(minutes)} دقیقه")
services/invasion_telegram.py:126:        pieces.append(f"{_fa(seconds)} ثانیه")
services/invasion_telegram.py:127:    return " و ".join(pieces)
services/invasion_telegram.py:146:        title = "🌙 تهاجم در راه است"
services/invasion_telegram.py:147:        wave_label = f"{_fa(1)} از {_fa(snapshot.wave_count)} پس از آغاز"
services/invasion_telegram.py:149:            "⏳ زمان تا آغاز: "
services/invasion_telegram.py:153:        title = "⚔️ تهاجم آغاز شده است"
services/invasion_telegram.py:156:            f"موج {_fa(sequence)} از {_fa(snapshot.wave_count)}"
services/invasion_telegram.py:158:            else f"موج {_fa(snapshot.wave_count)} از {_fa(snapshot.wave_count)}"
services/invasion_telegram.py:161:            "⏳ زمان باقی‌مانده دفاع: "
services/invasion_telegram.py:165:        title = "🏁 نتیجه نهایی تهاجم"
services/invasion_telegram.py:167:            wave_label = "موج‌ها: پس از قطع طولانی ربات، بدون نتیجه بسته شد"
services/invasion_telegram.py:169:            wave_label = "موج‌ها: بدون آغاز لغو شد"
services/invasion_telegram.py:172:                f"موج‌ها: {_fa(snapshot.wave_count)} از "
services/invasion_telegram.py:173:                f"{_fa(snapshot.wave_count)} تکمیل شد"
services/invasion_telegram.py:175:        timing_label = "⏳ زمان دفاع: پایان‌یافته"
services/invasion_telegram.py:179:        f"⚔️ مهاجم: {faction}",
services/invasion_telegram.py:180:        f"🗺 منطقه هدف: {region or 'مشخص نشده'}",
services/invasion_telegram.py:181:        f"💢 قدرت دشمن: {_fa(snapshot.enemy_power)}",
services/invasion_telegram.py:182:        f"🌊 موج فعلی: {wave_label}",
services/invasion_telegram.py:183:        f"🛡 دفاع ثبت‌شده: {_fa(snapshot.defense_total)}",
services/invasion_telegram.py:184:        f"👥 شرکت‌کنندگان: {_fa(snapshot.participant_count)}",
services/invasion_telegram.py:189:            f"🛡 دفاع این موج: {_fa(snapshot.current_wave_defense_total)}"
services/invasion_telegram.py:196:        lines.append(f"🕒 آغاز یورش: {_utc_label(snapshot.started_at)}")
services/invasion_telegram.py:199:            f"👤 سهم شما در یورش: {_fa(snapshot.viewer_contribution_total or 0)}"
services/invasion_telegram.py:203:                action_state = "Brace در دسترس است"
services/invasion_telegram.py:205:                action_state = "Brace را در این موج انجام دادی"
services/invasion_telegram.py:207:                action_state = "Brace در این موج در دسترس نیست"
services/invasion_telegram.py:208:            lines.append(f"🛡 کنش دفاعی این موج: {action_state}")
services/invasion_telegram.py:210:        lines.append(f"🕒 زمان برنامه‌ریزی‌شده: {_utc_label(snapshot.scheduled_at)}")
services/invasion_telegram.py:213:            InvasionOutcome.TOTAL_VICTORY: "پیروزی کامل",
services/invasion_telegram.py:214:            InvasionOutcome.NARROW_DEFENSE: "دفاع نزدیک",
services/invasion_telegram.py:215:            InvasionOutcome.DEFEAT: "شکست در دفاع",
services/invasion_telegram.py:218:            "🏆 نتیجه: "
services/invasion_telegram.py:219:            f"{outcome_labels.get(snapshot.outcome, 'ثبت‌نشده')}"
services/invasion_telegram.py:222:            lines.append(f"🕒 پایان: {_utc_label(snapshot.ended_at)}")
services/invasion_telegram.py:229:    InvasionOutcome.TOTAL_VICTORY: "پیروزی کامل",
services/invasion_telegram.py:230:    InvasionOutcome.NARROW_DEFENSE: "دفاع نزدیک",
services/invasion_telegram.py:231:    InvasionOutcome.DEFEAT: "شکست در دفاع",
services/invasion_telegram.py:239:    to defenders only, as inventory items (🌕 سیب طلایی), not wallet Apples.
services/invasion_telegram.py:242:        return "🎁 پاداشی تعلق نمی‌گیرد."
services/invasion_telegram.py:245:            f"🎁 پاداش: {_fa(snapshot.reward_golden_apples)} 🌕 سیب طلایی "
services/invasion_telegram.py:246:            "برای هر مدافع واجد شرایط"
services/invasion_telegram.py:249:        return "🎁 پاداشی ثبت نشد؛ مدافع واجد شرایطی وجود نداشت."
services/invasion_telegram.py:250:    return "🎁 پاداش در حال ثبت است؛ به‌زودی به حساب شما اضافه می‌شود."
services/invasion_telegram.py:270:    return f"⚔️ مهاجم: {snapshot.enemy_faction_symbol or '🌙'} {faction_name}"
services/invasion_telegram.py:275:    return f"🗺 منطقه هدف: {region or 'مشخص نشده'}"
services/invasion_telegram.py:284:            f"🌊 موج {_fa(sequence)} از {_fa(snapshot.wave_count)} آغاز شد",
services/invasion_telegram.py:287:            f"⏳ زمان دفاع این موج: {_remaining_label(snapshot.current_wave_deadline, now)}",
services/invasion_telegram.py:288:            f"🛡 دفاع این موج: {_fa(snapshot.current_wave_defense_total)}",
services/invasion_telegram.py:289:            f"👥 شرکت‌کنندگان: {_fa(snapshot.participant_count)}",
services/invasion_telegram.py:290:            "هر شرکت‌کننده در هر موج یک بار می‌تواند دفاع کند؛ دکمه 🛡 Brace را بزن.",
services/invasion_telegram.py:297:    lines = ["🏁 یورش به پایان رسید", _faction_line(snapshot), _region_line(snapshot)]
services/invasion_telegram.py:303:                "⛔️ این یورش به‌دلیل قطع طولانی ربات بدون نتیجه بسته شد؛ "
services/invasion_telegram.py:304:                "نه پیروزی ثبت شد نه شکست، و پاداشی تعلق نمی‌گیرد."
services/invasion_telegram.py:307:            lines.append("🚫 این یورش بدون دفاع لغو شد؛ پاداشی تعلق نمی‌گیرد.")
services/invasion_telegram.py:311:            f"🏆 نتیجه: {_OUTCOME_LABELS.get(snapshot.outcome, 'ثبت‌نشده')}",
services/invasion_telegram.py:312:            f"🛡 دفاع کل: {_fa(snapshot.defense_total)} از {_fa(snapshot.enemy_power)} قدرت دشمن",
services/invasion_telegram.py:313:            f"👥 شرکت‌کنندگان: {_fa(snapshot.participant_count)}",
services/maintenance/service.py:177:                        else "حریف سیبی"
services/maintenance/service.py:180:                        "حریفی پیدا نشد.\n\n"
services/maintenance/service.py:181:                        f"برای اینکه بیشتر منتظر نمونی، این بار با «{bot_name}» بازی می‌کنی 🍏"
services/maintenance/service.py:185:                        "پارتی حریفی پیدا نشد.\n\n"
services/maintenance/service.py:186:                        "برای اینکه منتظر نمونید، با تیم «🍎 تیم سیب کوچولو» بازی می‌کنید."
services/maintenance/service.py:510:            # the UI falls back to "مشخص نشده" (preserves the pre-fix shape).
services/maintenance/service.py:625:                        # the "مشخص نشده" rendering used by older records.
services/maintenance/service.py:1246:                    suffix="⏱ زمان نوبت به پایان رسید.",
handlers/invasion.py:56:INVALID_CALLBACK_TEXT = "این دکمه معتبر نیست."
handlers/invasion.py:57:INVALID_GROUP_TEXT = "این دکمه در این گروه معتبر نیست."
handlers/invasion.py:58:STALE_ANNOUNCEMENT_TEXT = "این اعلان قدیمی است؛ وضعیت از اعلان تازه نمایش داده شد."
handlers/invasion.py:59:STALE_WAVE_TEXT = "این موج دیگر فعال نیست؛ وضعیت موج فعلی تازه شد و دفاعی ثبت نشد."
handlers/invasion.py:60:INVASION_UNAVAILABLE_TEXT = "این یورش دیگر در دسترس نیست."
handlers/invasion.py:61:INVASION_REWORK_TEXT = "⚔️ سیستم تهاجم موقتاً برای بازنویسی غیرفعال شده است."
handlers/invasion.py:62:JOIN_PENDING_TEXT = "⏳ درخواست پیوستن در حال بررسی است؛ نتیجه همین‌جا نمایش داده می‌شود."
handlers/invasion.py:63:STATUS_PENDING_TEXT = "⏳ وضعیت یورش در حال تازه‌شدن است."
handlers/invasion.py:64:BRACE_PENDING_TEXT = "⏳ دفاع در حال ثبت است."
handlers/invasion.py:66:    "⚔️ به یورش پیوستی. در هر موج، یک‌بار با 🛡 Brace دفاع کن؛ "
handlers/invasion.py:67:    "پیوستن به‌تنهایی دفاعی ثبت نمی‌کند."
handlers/invasion.py:70:_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
handlers/invasion.py:217:        "⚔️ به یورش پیوستی. در هر موج، یک‌بار با 🛡 Brace دفاع کن؛ "
handlers/invasion.py:218:        "پیوستن به‌تنهایی دفاعی ثبت نمی‌کند."
handlers/invasion.py:222:            "🎴 کارت اصلی: انتخاب نشده؛ بدون کارت اصلی به جمع مدافعان پیوستی."
handlers/invasion.py:229:        or "شخصیت نامشخص"
handlers/invasion.py:233:            f"🎴 کارت اصلی: {character_name}",
handlers/invasion.py:234:            f"⭐ سطح: {_fa(participant.card_level_snapshot)}",
handlers/invasion.py:236:                f"💪 قدرت: {_fa(participant.power_snapshot)} | "
handlers/invasion.py:237:                f"⚡ سرعت: {_fa(participant.speed_snapshot)} | "
handlers/invasion.py:238:                f"🧠 هوش: {_fa(participant.intelligence_snapshot)}"
handlers/invasion.py:248:            return "⏳ دفاع هنوز آغاز نشده؛ بعد از شروع یورش دوباره تلاش کن."
handlers/invasion.py:250:            return "🏁 دفاع این یورش بسته شده و دیگر امکان پیوستن نیست."
handlers/invasion.py:251:        return "🛡 در حال حاضر امکان پیوستن به این دفاع نیست."
handlers/invasion.py:253:        return "🎴 وضعیت کارت اصلی‌ات تغییر کرد؛ یک‌بار دیگر پیوستن را بزن."
handlers/invasion.py:255:        return "حساب بازیکن پیدا نشد؛ دوباره از دکمه پیوستن استفاده کن."
handlers/invasion.py:259:        return "این دفاع دیگر برای پیوستن در دسترس نیست."
handlers/invasion.py:260:    return "ثبت پیوستن انجام نشد؛ کمی بعد دوباره تلاش کن."
handlers/invasion.py:346:            "وضعیت یورش فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن.",
handlers/invasion.py:458:        result_text = "ثبت پیوستن انجام نشد؛ کمی بعد دوباره تلاش کن."
handlers/invasion.py:523:            "وضعیت یورش ثبت‌شده است، اما نتونستم این اعلان را تازه کنم.",
handlers/invasion.py:553:        "↩️ این درخواست قبلاً ثبت شده است."
handlers/invasion.py:555:        else "🛡 دفاع پذیرفته شد."
handlers/invasion.py:560:            f"➕ سهم این کنش: {_fa(result.contribution_amount)}",
handlers/invasion.py:561:            f"👤 سهم کل شما: {_fa(result.participant_total_after)}",
handlers/invasion.py:563:                f"🌊 دفاع موج {_fa(result.wave_sequence)}: "
handlers/invasion.py:652:            "برای دفاع از این یورش، اول به جمع مدافعان بپیوند.",
handlers/invasion.py:666:            "✅ Brace را در این موج قبلاً انجام دادی؛ در موج بعدی دوباره در دسترس است.",
handlers/invasion.py:699:            "این یورش فعال نیست و دفاع تازه‌ای ثبت نشد.",
handlers/invasion.py:706:            "این درخواست با کنش دیگری تداخل دارد؛ وضعیت را تازه کن و دوباره تلاش کن.",
handlers/invasion.py:723:            "این دفاع دیگر برای انجام کنش در دسترس نیست.",
handlers/invasion.py:761:                "ثبت دفاع انجام نشد؛ کمی بعد دوباره تلاش کن.",
handlers/invasion.py:817:            "این دکمه فقط در گروه مربوط به یورش معتبر است.",
```

## Production entry points and wiring

- `bot.py:main` starts `RecoveryService.recover_on_startup` **before** the maintenance worker, then schedules `MaintenanceService.run_forever` and starts aiogram polling.
- `services/recovery/service.py:RecoveryService.recover_on_startup` commits general recovery and, **only when the flag is enabled**, calls `MaintenanceService.run_invasion_cycle(now=now, schedule_occurrences=False)` to resume due lifecycle transitions and settle rewards. No occurrence scheduling at recovery.
- `services/maintenance/service.py:MaintenanceService.run_forever` logs the cached flag once on startup and calls `run_once` every five seconds. `run_once` gates both the DB-only `run_invasion_cycle` and the Telegram adapter `InvasionTelegramService.publish_maintenance_updates`. Its invasion-only scheduling call inside `run_invasion_cycle` is gated separately. The domain step activates eligible slots, advances due battles and settles pending rewards; the presentation step sends/edits/pins stage announcements and retries pending messages.
- `handlers/__init__.py:register_all_handlers` registers `handlers/invasion.py:router`. The `invasion:` callback router is the only invasion user-facing handler found in `services/` and `handlers/` (no separate invasion command handler). Its top-level handler gates join, Brace, and status callbacks before parsing or performing any invasion reads/writes, answering with a Persian disabled notice. Enabled joins and Brace actions call `InvasionService`; status and post-action refreshes use `InvasionTelegramService`.
- `services/invasion.py` owns occurrence identity, joining, wave/action transitions and contribution logic; `services/invasion_rewards.py` settles eligible defenders; `services/invasion_telegram.py` loads persisted snapshots, renders Persian messages and publishes/pins announcements; `keyboards/invasion.py` builds/parses callbacks. Invasion model files live in `database/models/invasion*.py`.
- There is **no separate invasion scheduler/job**: slot scheduling happens only in the existing maintenance loop. Direct calls to `run_invasion_cycle` by tests/tools remain possible; production callers are gated. A newly added caller must check `_invasion_enabled()` before invoking domain/presentation services.

The maintenance file is `services/maintenance/service.py`, not `sib/services/maintenance/service.py`; `sib.*` in imports refers to the installed package namespace, not a same-named on-disk path for these modules.
