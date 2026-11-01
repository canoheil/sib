# راهنمای جامع عملیاتی و استقرار پروداکشن ربات سیب (sib Runbook) 🍏📖

این سند راهنمای عملیاتی و اجرایی برای مدیر سیستم، ادمین‌ها و تیم فنی جهت استقرار، راه‌اندازی، نگهداری و عیب‌یابی ربات **سیب (sib)** در محیط پروداکشن است.

---

## ۱. راه‌اندازی و مدیریت فرآیندها (Process Management)

### الف) روش Systemd (پیشنهادی برای سرور لینوکس خام / اوبونتو / دبیان)

> ⚠️ واحد systemd فقط `bot.py` را اجرا می‌کند و `init_db()` با `create_all()` تغییرات
> طرحوارهٔ جدول‌های موجود را اعمال نمی‌کند. پیش از اجرای کد جدید، مایگریشن‌ها را
> طبق بخش ۳ با `alembic upgrade head` اعمال و نسخه را با `alembic current` بررسی
> کنید؛ در استقرار Invasion چهارموجی، کد به مایگریشن 031 نیاز دارد. اسکریپت
> `deploy/deploy.sh` و فرمان Docker مایگریشن را اجرا می‌کنند، اما `systemctl start`
> یا `restart` به‌تنهایی این کار را نمی‌کند.

1. کپی فایل سرویس به مسیر systemd:
   ```bash
   sudo cp /opt/sib/deploy/sib.service /etc/systemd/system/sib.service
   sudo systemctl daemon-reload
   ```

2. فعال‌سازی و شروع خودکار سرویس:
   ```bash
   sudo systemctl enable sib
   sudo systemctl start sib
   ```

3. بررسی وضعیت و لاگ‌ها:
   ```bash
   # مشاهده وضعیت اجرای سرویس
   sudo systemctl status sib

   # مشاهده زنده لاگ‌های سرویس در journalctl
   sudo journalctl -u sib -f -n 100
   ```

4. ری‌استارت یا توقف سرویس:
   ```bash
   sudo systemctl restart sib
   sudo systemctl stop sib
   ```

---

### ب) روش Docker و Docker Compose

1. ساخت ایمیج و اجرای کانتینرها در پس‌زمینه:
   ```bash
   cd /opt/sib
   docker compose up -d --build
   ```

2. مشاهده لاگ‌های زنده:
   ```bash
   docker compose logs -f bot
   ```

3. توقف یا راه‌اندازی مجدد:
   ```bash
   docker compose restart bot
   docker compose down
   ```

---

## ۲. بررسی لاگ‌ها و رویدادهای ساختاریافته (Structured Events)

ربات تمامی رویدادهای استاندارد را با پیشوند `[EVENT:...]` لاگ می‌کند. برای فیلتر کردن رویدادهای مهم:

```bash
# مشاهده مسابقات ایجاد شده
journalctl -u sib | grep "EVENT:MATCH_CREATED"

# مشاهده مسابقات پایان یافته و تغییرات الو
journalctl -u sib | grep "EVENT:RATING_UPDATED"

# مشاهده نمایش و کلیک‌های تبلیغات
journalctl -u sib | grep -E "EVENT:AD_DISPLAYED|EVENT:AD_CLICKED"

# مشاهده رویدادهای بازیابی استارت‌آپ
journalctl -u sib | grep "EVENT:SYSTEM_RESTART_RECOVERY"
```

---

## ۳. مدیریت پایگاه‌داده و مایگریشن‌ها (Alembic Migrations)

پایگاه داده MariaDB با طرحواره رسمی و ۵ مایگریشن امن مدیریت می‌شود:

1. **اعمال آخرین مایگریشن‌ها:**
   ```bash
   cd /opt/sib
   alembic upgrade head
   ```

2. **مشاهده نسخه فعلی دیتابیس:**
   ```bash
   alembic current
   ```

3. **مشاهده تاریخچه مایگریشن‌ها:**
   ```bash
   alembic history --verbose
   ```

4. **نکتهٔ مایگریشن ۰۴۲ (سنجاق خودکار فراخوان هجوم):**
   این مایگریشن ستون nullable به نام `pinned_at` به جدول `inv2_invasions` اضافه
   می‌کند تا ربات بداند کدام پیام فراخوان را خودش سنجاق کرده است. برای کارکرد
   این قابلیت، ربات باید در گروه **ادمین با دسترسی «سنجاق کردن پیام‌ها»** باشد؛
   در غیر این صورت چیزی خراب نمی‌شود و فقط در لاگ هشدار
   `inv2 pin_chat_message rejected group=…` ثبت می‌شود.

5. **ایجاد نسخه پشتیبان دوره‌ای (Backup):**
   ```bash
   /opt/sib/deploy/backup_db.sh
   ```
   نسخه‌های فشرده `.sql.gz` در مسیر `/opt/sib/backups/` ذخیره شده و بکاپ‌های بیش از ۱۴ روز به صورت خودکار حذف می‌شوند.

---

## ۴. مدیریت ادمین‌ها (Admin Access Control)

- ادمین‌ها بر اساس شناسه‌های عددی تلگرام بدون هیچ هاردکدینگی در سورس کد مشخص می‌شوند.
- برای افزودن ادمین جدید:
  1. فایل `.env` را باز کنید:
     ```env
     TELEGRAM_ADMIN_IDS=123456789,987654321,555555555
     ```
  2. سرویس را ری‌استارت کنید:
     ```bash
     sudo systemctl restart sib
     ```
- ادمین با ارسال دستور `/admin` در پیوی ربات به داشبورد مدیریتی دسترسی خواهد داشت.

---

## ۵. مدیریت کانال‌های اسپانسر و دروازه عضویت (Sponsor Channels)

1. **از طریق پنل ادمین تلگرام:**
   - دستور `/admin` را در چت شخصی با ربات ارسال کنید.
   - گزینه «🤝 کانال‌ها و گیت اسپانسر» را لمس کنید.
   - فهرست تمامی کانال‌ها به همراه وضعیت (فعال/غیرفعال) و الزامی/اختیاری بودن در دروازه عضویت نمایش داده می‌شود و با کلیک روی هر کانال وضعیت آن تغییر می‌کند.

2. **تنظیم اولیه کانال رسمی در `.env`:**
   ```env
   OFFICIAL_CHANNEL_ID=-1001234567890
   OFFICIAL_CHANNEL_LINK=https://t.me/sibbot_channel
   CHECK_MEMBERSHIP_REQUIRED=True
   ```

> **دروازهٔ نرم (از پچ هدیهٔ شروع):** گیت کانال دیگر `/start` را مسدود نمی‌کند.
> منوی پیوی و هدیهٔ شروع همیشه تحویل داده می‌شوند و در صورت نبود عضویت فقط یک
> ردیف «📢 عضویت در کانال سیب» به کیبورد اضافه می‌شود. تأیید عضویت آن ردیف را
> برمی‌دارد و چیز دیگری را باز نمی‌کند.

### هدیهٔ شروع (Starter Pack)

- هر حساب **دقیقاً یک‌بار** در اولین `/start` یک کارت منتشرشدهٔ فروشگاه
  (به‌صورت خودکار کارت اصلی) و ۱۰۰ سیب می‌گیرد
  (`services/onboarding.py`). نشانهٔ «قبلاً گرفته» ردیف دفتر کل با کلید
  `starter_pack:<user_id>` است؛ برای همین هیچ مایگریشن یا ستون جدیدی لازم نیست.
- برای بررسی: `SELECT * FROM apple_transactions WHERE idempotency_key LIKE 'starter_pack:%';`
- برای خاموش‌کردن اضطراری این هدیه (مثلاً در تست بارگذاری)، مقدار
  `STARTER_APPLES` را در کد صفر کنید؛ حذف ردیف دفتر کل باعث پرداخت دوباره می‌شود.
- **توجه:** بازیکنان قدیمی هم در `/start` بعدی‌شان هدیه را می‌گیرند (بازگشت‌شان
  خواسته است). اگر نمی‌خواهید، پیش از دیپلوی بگویید تا شرط «فقط حساب تازه» اضافه شود.

### لیگ هفتگی گروه‌ها (پچ رقابت گروهی)

- امتیازها از جدول `matches` حساب می‌شوند (فقط نبرد واقعی و تمام‌شده؛ برد بات و
  بازی دوستانه هرگز) و در دو جدول `group_league_weeks` / `group_league_awards`
  فقط «هفتهٔ تسویه‌شده» و «سکوی نهایی» ذخیره می‌شود.
- هفته از **شنبه ۰۰:۰۰ به وقت تهران** شروع می‌شود.
- خاموش/روشن کردن بدون دیپلوی:
  ```sql
  UPDATE system_config SET value = 'false' WHERE `key` = 'league_enabled';
  ```
- تنظیم جایزه (سیب به ازای هر بازیکنِ بازی‌کردهٔ هر رتبه):
  ```sql
  UPDATE system_config SET value = '{"1": 300, "2": 150, "3": 100}'
   WHERE `key` = 'league_rewards';
  UPDATE system_config SET value = '3' WHERE `key` = 'league_min_points';
  ```
- `league_first_week` (که مهاجرت ۰۶۴ با هفتهٔ خودش پر می‌کند) جلوی جایزهٔ عقب‌افتاده
  برای هفته‌های قبل از وجود لیگ را می‌گیرد؛ دست نزنید مگر اینکه بدانید چه می‌کنید.
- تسویه از چرخهٔ نگهداری، ساعتی یک بار، یک‌بار برای هر هفته انجام می‌شود.
- بررسی وضعیت:
  ```sql
  SELECT week_key, winner_group_id, apples_paid, finalized_at
    FROM group_league_weeks ORDER BY week_key DESC LIMIT 5;
  SELECT week_key, rank, group_id, points, players_paid, apples_per_player
    FROM group_league_awards ORDER BY week_key DESC, rank ASC LIMIT 9;
  ```
- برای تسویهٔ دستی یک هفتهٔ خاص (مثلاً بعد از قطعی طولانی): از پنل ادمین استفاده
  نکنید؛ فقط `league_enabled` را روشن بگذارید — چرخهٔ بعدی خودش هفتهٔ گذشته را
  تسویه می‌کند و به‌خاطر کلید idempotency دوباره پرداخت نمی‌شود.

### استریک روزانه و یادآوری‌ها (پچ بازگشت بازیکن)

**استریک صندوق روزانه**
- از ردیف‌های `daily_chest_claims` محاسبه می‌شود؛ جدول یا ستون جدیدی ندارد.
- پله‌ها در `system_config.daily_streak_rewards` است (پیش‌فرض `{"3": 30, "7": 100, "14": 250}`).
- هر پاداش با کلید `daily_streak:<user_id>:<day>` پرداخت می‌شود؛ دستکاری دستی این ردیف باعث پرداخت دوباره می‌شود.

**یادآوری‌ها** (تنها پیام‌هایی که ربات بدون درخواست می‌فرستد)
- مایگریشن `063_reminders` جدول `user_reminder_states` و کلیدهای تنظیمات را می‌سازد.
- خاموش/روشن کردن بدون دیپلوی:
  ```sql
  UPDATE system_config SET value = 'false' WHERE `key` = 'reminders_enabled';
  ```
  (یا `true` برای روشن)
- سقف‌ها: حداکثر یک پیام به هر بازیکن در `reminders_min_gap_hours` (پیش‌فرض ۲۰ ساعت)،
  حداکثر `reminders_batch_size` (۲۵) پیام در هر اسکن، و فقط بین ساعت‌های
  `reminders_window_start_hour` تا `reminders_window_end_hour` به وقت تهران.
- کسانی که بات را بلاک کرده‌اند در `user_reminder_states.blocked_at` ثبت می‌شوند و پیام نمی‌گیرند
  تا وقتی خودشان از «پروفایل → 🔔 یادآوری‌ها» دوباره روشن کنند (فشردن همان دکمه ثابت می‌کند
  بات می‌تواند به آن‌ها پیام بدهد، پس `blocked_at` صفر می‌شود).
- بازیکن از «پروفایل → 🔔 یادآوری‌ها» می‌تواند خودش خاموش کند.
- بررسی وضعیت:
  ```sql
  SELECT COUNT(*) FROM user_reminder_states WHERE opted_out = 1;
  SELECT COUNT(*) FROM user_reminder_states WHERE blocked_at IS NOT NULL;
  SELECT last_kind, COUNT(*) FROM user_reminder_states GROUP BY last_kind;
  ```

---

## ۶. ایجاد و مدیریت کمپین‌های تبلیغاتی (Monetization & Ads)

1. **تنظیم و فعال‌سازی در پنل ادمین:**
   - در پنل `/admin`، گزینه «📢 تبلیغات و مانیتایزیشن» را انتخاب کنید.
   - آمار لحظه‌ای ایمپرشن‌ها، کلیک‌ها و درصد CTR نمایش داده می‌شود.
   - می‌توانید هر کمپین را با یک کلیک فعال یا غیرفعال کنید.

2. **ویژگی‌های پشتیبانی‌شده در هر کمپین:**
   - نام اسپانسر و عنوان کمپین
   - متن فارسی پیام تبلیغاتی
   - برچسب دکمه CTA و لینک مقصد (URL)
   - سقف نمایش (Max Impressions)
   - زمان‌بندی تاریخ شروع و پایان (Scheduling)
   - حالت‌های تناوب نمایش: `every_match`، `every_2_matches`، `every_3_matches` و `smart` (پیش‌فرض).

---

## ۷. مدیریت مسابقات معلق و نشست‌های گیرکرده (Hung Sessions & Recovery)

ربات سیب دارای سیستم خودکار بازیابی در استارت‌آپ و پردازش تایم‌اوت‌ها است:

1. **بازیابی خودکار هنگام راه‌اندازی (`RecoveryService`):**
   - به محض راه‌اندازی ربات، متد `recover_on_startup` اجرا شده و:
     - مسابقات بلاتکلیفی که در انتظار تایید مانده‌اند را لغو و پارتی‌ها را آزاد می‌کند.
     - صف‌های معلق مسابقه‌یابی را پاک‌سازی می‌کند.
     - مسابقات فعالی که ددلاین نوبت‌شان در زمان قطعی گذشته را خاتمه می‌دهد.
2. **سرویس تایم‌اوت خودکار (`TimeoutService`):**
   - مسابقات در انتظار پذیرش پس از ۱۲۰ ثانیه به طور خودکار لغو می‌شوند.
   - در مسابقات فعال، در صورت گذشت مهلت نوبت بازیکن، مسابقه به نفع تیم مقابل با سلب صلاحیت (Forfeit) پایان یافته و ریتینگ الو اعمال می‌شود.

---

## ۸. بررسی سلامت و سنجش سیستم (Health Check)

اسکریپت سلامت‌سنجی یکپارچه وضعیت اتصال دیتابیس، مایگریشن‌ها و تست‌ها را ارزیابی می‌کند:

```bash
cd /opt/sib
./deploy/check_health.sh
```

خروجی استاندارد:
```
🍏 --- بررسی سلامت سیستم ربات سیب (sib Health Check) ---
1. اتصال به دیتابیس MariaDB: 🟢 متصل و آماده
2. وضعیت مایگریشن‌های دیتابیس (Alembic): 🟢 آخرین نسخه مایگریشن (005_monetization_and_ads (head))
3. فرآیند ربات سیب: 🟢 در حال اجرا
4. وضعیت تست‌های جامع: 🟢 تمام ۹۷ آزمون بدون خطا پاس می‌شوند
🍏 کلیه شاخص‌های سلامت سیستم تایید شد! ✅
```

### Automatic spam jail

The first message/callback middleware shares a single per-user sliding window
across chats and event types: at most 3 events per 2 seconds. Each dropped
over-limit event is a strike; 3 strikes within 60 seconds silently jail the user
for 2 hours. Jailed events do not reach database-backed middleware or handlers.
No warnings are sent, to avoid amplifying floods.

This state is process-local and expires lazily; restarting the bot clears bans.
Use one polling process. Before deploying multiple workers, replace the local
state with an atomic shared cache (such as Redis), otherwise limits and bans
will not be shared. No database migration is required; the Alembic head remains
`075_rocket_game`.

Rocket also reserves one active round per user across chats, releases it in a
`finally` block, awaits animations directly, and silently ignores cooldowns.
