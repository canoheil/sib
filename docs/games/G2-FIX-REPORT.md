# گزارش G2-FIX — نمایش امن راندهای رفیقانه RPS و High Card

**تاریخ:** ۲۰۲۶-۱۰-۱۷

**وضعیت:** پیاده‌سازی و آزمون‌های هدفمند انجام شد.

## محدوده

این تغییر فقط رفتار سنگ، کاغذ، قیچی و کارت بالا را برای نمایش گروهی/رفیقانه اصلاح می‌کند؛ قوانین بازی‌ها عوض نشده‌اند. منطق timeout و پاک‌سازی مسابقه همچنان از سرویس‌های موجود استفاده می‌کند. تغییر مشترک در `handlers/games/common.py` فقط به مسیرهای RPS و High Card محدود است؛ موتورهای بازی‌های دیگر و invasion v2 دست‌کاری نشده‌اند.

## TASK 0 — تأیید قابلیت پیام موقت

قابلیت پیش از شروع تغییر منطق بازی با **aiogram 3.31.0** بررسی شد. `Bot.send_message` پارامتر مستقیم `receiver_user_id` و پارامتر `ephemeral_message_parameters` را می‌پذیرد؛ مدل `EphemeralMessageParameters` نیز در همین نسخه قابل import و سریال‌سازی است. در این پیاده‌سازی از شکل مستقیم زیر استفاده شده است:

```python
await bot.send_message(
    chat_id=target_group_id,
    text=private_card_text,
    receiver_user_id=user.id,
    parse_mode=None,
)
```

این بررسی، آزمون قابلیت کتابخانه و مدل درخواست بود؛ پیام زنده‌ای به Telegram ارسال نشده است. حداقل نسخهٔ وابستگی در `requirements.txt` و README به `aiogram>=3.31.0,<4.0.0` تغییر کرد.

## سنگ، کاغذ، قیچی

- تا زمانی که مسابقه فعال است، صفحه‌کلید مشترک سه دکمهٔ عمومی «سنگ»، «کاغذ» و «قیچی» را نشان می‌دهد؛ پس از انتخاب اول متن/دکمهٔ انتخاب‌شده روی صفحهٔ عمومی جایگزین نمی‌شود.
- انتخاب اول در وضعیت بازی ذخیره می‌شود، اما صفحهٔ مشترک دست‌نخورده می‌ماند و بازیکن این پاسخ را دریافت می‌کند: `✅ انتخاب شما ثبت شد. منتظر حریف...`
- تنها پس از ثبت انتخاب دوم، هر دو انتخاب و برنده/مساوی راند روی صفحهٔ گروه آشکار می‌شوند. نتیجهٔ راند قبل از پاک‌سازی وضعیت راند بعد ذخیره می‌شود.
- انتخاب تکراری با اعتبارسنجی وضعیت بازی و قفل ردیف مسابقه رد می‌شود؛ به‌روزرسانی صفحه برای کلیک تکراری انجام نمی‌شود.

## کارت بالا

- کارت اولیه و کارت جایگزینِ حاصل از Swap پس از commit، با پیام موقت به همان کاربر و در همان گروه فرستاده می‌شوند. متن گروه و دکمه‌های عمومی مقدار رتبه/خال را نشان نمی‌دهند.
- وضعیت عمومی فقط نشان می‌دهد چه کسی کارت کشیده یا تصمیمش را ثبت کرده است. اطلاعات کارت‌های راند در snapshot جداگانه نگه داشته می‌شود و تنها پس از قفل‌کردن هر دو بازیکن، هر دو کارت، نتیجهٔ راند و امتیاز نمایش داده می‌شوند.
- اگر ارسال پیام موقت ناموفق شود، همان کارت از طریق هشدار خصوصی callback نمایش داده می‌شود:

```python
await safe_answer_callback(
    callback,
    render_payload["private_card_text"],  # «🃏 کارت شما: ...»
    show_alert=True,
)
```

این wrapper در نهایت `callback.answer(text, show_alert=True)` را فراخوانی می‌کند. خطای ارسال پیام موقت، حرکت commit‌شده را لغو نمی‌کند و پردازش صفحهٔ عمومیِ خنثی ادامه می‌یابد.

## تکرار کلیک و timeout

قفل ردیف مسابقه و اعتبارسنجی موتور بازی حفظ شده‌اند؛ کلیک تکراری RPS یا High Card وضعیت را دوباره اعمال نمی‌کند. آزمون timeout نشان می‌دهد اگر فقط یک بازیکن کارت بکشد و قفل کند، پایان مسابقه و دکمهٔ rematch همگام می‌شوند، اما کارت ناقص در صفحهٔ گروه افشا نمی‌شود. مسیر تعیین نتیجهٔ بازیکن قفل‌شده همان مسیر موجود `TimeoutService` است.

## آزمون‌ها

آزمون‌های G2 در `tests/games/test_g2_friendly_reveal.py` شامل شش نام درخواستی هستند:

- `test_rps_choice_not_leaked_in_keyboard`
- `test_rps_reveals_both_on_second_commit`
- `test_high_card_draw_sends_ephemeral_message`
- `test_high_card_group_message_neutral_during_draw`
- `test_high_card_reveals_both_after_lock`
- `test_high_card_fallback_to_alert_if_ephemeral_fails`

همچنین آزمون ارسال peek جدید پس از Swap، رد کلیک تکراری و timeout/cleanup اضافه شده است.

آزمون‌های هدفمند بازی‌ها و مسیرهای مرتبط با فرمان زیر اجرا شدند و همگی موفق بودند: **۱۱۷ passed in 20.13s**.

```bash
PYTHONPATH=/tmp/sib-g2-test-deps:/home/user \
  sib_TEST_DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test_g2.db \
  python -m pytest -q \
  tests/games/test_g2_friendly_reveal.py \
  tests/test_high_card.py tests/test_rps_gameplay.py tests/test_rps_handlers.py \
  tests/test_friendly_party.py tests/test_simultaneous_timeout_regression.py \
  tests/games/test_g1_board_recovery.py tests/test_glass_button_styles.py \
  tests/test_stale_and_duplicate_callbacks.py
```

اجرای کل مجموعه نیز **۲۵۵۵ passed، ۲۷ failed و ۲۵ skipped** در ۲۹۶٫۸۷ ثانیه گزارش کرد. شکست‌ها خارج از آزمون‌های هدفمند G2 هستند؛ از جمله آزمون‌های تصویر/ICC و manifest در invasion v2، آزمون‌های دارایی/پروفایل، یک انتظار stale-turn برای Tic-Tac-Toe و آزمون‌های نامرتبط زمان‌بندی/تراکنش. در محدودهٔ خواسته‌شده هیچ آزمون بازی RPS یا High Card شکست نخورد.
