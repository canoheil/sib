# A1 reconnaissance

## handlers/admin.py
22:async def handle_admin_command(message: Message):
36:async def handle_admin_main_menu(callback: CallbackQuery, state: FSMContext = None):
50:async def handle_admin_dashboard(callback: CallbackQuery):
82:async def handle_admin_ads(callback: CallbackQuery):
112:async def handle_admin_ad_view(callback: CallbackQuery):
157:async def handle_admin_ad_toggle(callback: CallbackQuery):
176:async def handle_admin_matches(callback: CallbackQuery):
## handlers/admin_center.py
40:class AdminUserFlow(StatesGroup):
45:class AdminChannelFlow(StatesGroup):
54:def _back_admin_main() -> list:
58:async def _resource_line(session, user_id: int) -> str:
64:async def _user_panel(session, user_id: int) -> tuple:
111:async def _send_user_panel(message: Message, user_id: int) -> None:
126:async def handle_admin_users(callback: CallbackQuery, state: FSMContext):
141:async def handle_admin_user_input(message: Message, state: FSMContext):
174:async def handle_admin_user_op(callback: CallbackQuery, state: FSMContext):
195:async def handle_admin_amount_input(message: Message, state: FSMContext, bot: Bot):
257:async def handle_admin_user_refresh(callback: CallbackQuery, state: FSMContext):
276:def _channel_row(ch) -> InlineKeyboardButton:
282:async def _channels_screen(session) -> tuple:
298:async def handle_admin_channels(callback: CallbackQuery):
306:async def _channel_detail_view(ch_id: str) -> tuple:
338:async def _render_channel_detail(message: Message, ch_id: str) -> None:
346:async def handle_admin_channel_detail(callback: CallbackQuery):
358:async def handle_admin_channel_toggle(callback: CallbackQuery):
375:async def handle_admin_channel_required(callback: CallbackQuery):
391:async def handle_admin_channel_type_picker(callback: CallbackQuery):
412:async def handle_admin_channel_set_type(callback: CallbackQuery):
433:async def handle_admin_channel_delete_confirm(callback: CallbackQuery):
453:async def handle_admin_channel_delete(callback: CallbackQuery):
469:def _parse_channel_ref(text: str):
484:async def handle_admin_channel_add_start(callback: CallbackQuery, state: FSMContext):
504:async def handle_admin_channel_add_input(message: Message, state: FSMContext, bot: Bot):
548:async def handle_admin_channel_add_finish(callback: CallbackQuery, state: FSMContext):
## admin/auth.py
12:def is_admin(user_id: int) -> bool:
17:class IsAdminFilter(BaseFilter):
20:    async def __call__(self, obj: TelegramObject) -> bool:
34:def admin_required(func: Callable) -> Callable:
37:    async def wrapper(*args: Any, **kwargs: Any):
## database/models/admin_log.py
10:class AdminLog(Base):
13:    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
14:    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
15:    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
16:    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
17:    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
## database/models/user.py
9:class User(Base, TimestampMixin):
13:    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
14:    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
15:    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
16:    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
17:    language_code: Mapped[str] = mapped_column(String(10), default="fa", nullable=False)
18:    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
19:    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
22:    profile_skin: Mapped[str] = mapped_column(
30:    def display_name(self) -> str:
36:    def __repr__(self) -> str:
## database/base.py
8:class Base(DeclarativeBase):
9:    """Base class for all SQLAlchemy database models."""
13:class TimestampMixin:
15:    created_at: Mapped[datetime] = mapped_column(
20:    updated_at: Mapped[datetime] = mapped_column(
## database/models/player_card.py
24:class PlayerCard(Base, TimestampMixin):
32:    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
33:    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
34:    character_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
38:    level: Mapped[int] = mapped_column(
42:    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
44:    def __repr__(self) -> str:
48:class PlayerCardLegacyStats(Base, TimestampMixin):
59:    card_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
60:    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
61:    character_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
62:    level: Mapped[int] = mapped_column(Integer, nullable=False)
63:    power: Mapped[int] = mapped_column(Integer, nullable=False)
64:    speed: Mapped[int] = mapped_column(Integer, nullable=False)
65:    intelligence: Mapped[int] = mapped_column(Integer, nullable=False)
66:    boosted_power: Mapped[int] = mapped_column(Integer, nullable=False)
67:    boosted_speed: Mapped[int] = mapped_column(Integer, nullable=False)
68:    boosted_intelligence: Mapped[int] = mapped_column(Integer, nullable=False)
## database/models/apple.py
14:class AppleWallet(Base, TimestampMixin):
19:    user_id: Mapped[int] = mapped_column(
23:    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
27:    def __repr__(self) -> str:
31:class AppleTransaction(Base, TimestampMixin):
39:    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
40:    user_id: Mapped[int] = mapped_column(
43:    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # signed
44:    reason: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
45:    ref_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
46:    ref_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
47:    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
48:    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
50:    def __repr__(self) -> str:
## database/models/seedling.py
15:class SeedlingInventory(Base, TimestampMixin):
20:    user_id: Mapped[int] = mapped_column(
24:    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
28:    def __repr__(self) -> str:
32:class SeedlingAward(Base, TimestampMixin):
47:    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
48:    match_id: Mapped[Optional[int]] = mapped_column(
51:    user_id: Mapped[int] = mapped_column(
54:    amount: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
55:    reason: Mapped[str] = mapped_column(String(50), default="match_win", nullable=False)
56:    source: Mapped[str] = mapped_column(String(20), default="match", nullable=False)
57:    source_ref_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
59:    def __repr__(self) -> str:
## database/models/player_xp.py
22:class PlayerProgression(Base, TimestampMixin):
38:    user_id: Mapped[int] = mapped_column(
44:    total_xp: Mapped[int] = mapped_column(
51:    def __repr__(self) -> str:
55:class PlayerXPGrant(Base, TimestampMixin):
89:    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
90:    user_id: Mapped[int] = mapped_column(
95:    source: Mapped[str] = mapped_column(String(40), nullable=False)
96:    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
98:    amount: Mapped[int] = mapped_column(Integer, nullable=False)
99:    awarded_at: Mapped[datetime] = mapped_column(
102:    level_before: Mapped[int] = mapped_column(Integer, nullable=False)
103:    level_after: Mapped[int] = mapped_column(Integer, nullable=False)
107:    notification_chat_id: Mapped[Optional[int]] = mapped_column(
110:    notification_claimed_at: Mapped[Optional[datetime]] = mapped_column(
113:    notification_sent_at: Mapped[Optional[datetime]] = mapped_column(
117:    def __repr__(self) -> str:
## database/models/apple_mine.py
11:class AppleMineShift(Base):
19:    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
20:    user_id: Mapped[int] = mapped_column(
23:    group_id: Mapped[int | None] = mapped_column(
26:    card_id: Mapped[int | None] = mapped_column(
29:    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
30:    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
31:    amount: Mapped[int] = mapped_column(Integer, nullable=False)
32:    worked_at: Mapped[datetime] = mapped_column(
35:    created_at: Mapped[datetime] = mapped_column(
39:    def __repr__(self) -> str:
## database/models/daily_chest.py
40:class DailyChestClaim(Base, TimestampMixin):
49:    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
50:    user_id: Mapped[int] = mapped_column(
54:    claim_day: Mapped[str] = mapped_column(String(10), nullable=False)
55:    reward_type: Mapped[str] = mapped_column(String(20), nullable=False)
56:    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
58:    ref_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
60:    def __repr__(self) -> str:
## services/sib_claim.py
42:def fa_digits(value) -> str:
47:def fa_count(value: int) -> str:
52:def format_cooldown_message(remaining_seconds: int, amount: int) -> str:
78:def format_claim_success(amount: int, cooldown_seconds: int) -> str:
87:class SibClaimService:
91:    async def _last_claim_at(cls, session: AsyncSession, user_id: int) -> Optional[object]:
101:    async def claim(cls, session: AsyncSession, user_id: int) -> Tuple[str, bool]:
126:    async def _claim_once(
## services/items.py
70:def item_description(key: str) -> str:
75:def item_label(key: str) -> str:
80:def item_emoji(key: str) -> str:
85:class ItemService:
89:    async def grant(
119:    async def get_total(
141:    async def list_items(session: AsyncSession, user_id: int) -> Dict[str, int]:
152:    async def summary_line(session: AsyncSession, user_id: int) -> str:
172:    async def consume(
210:    async def buy_price(session: AsyncSession, item_key: str) -> Optional[int]:
216:    async def sell_price(session: AsyncSession, item_key: str) -> Optional[int]:
222:    async def sell_prices(session: AsyncSession) -> Dict[str, int]:
228:    async def sellable_inventory(
251:    async def asset_key(session: AsyncSession, item_key: str) -> str:
264:    async def _lock_inventory_row(session: AsyncSession, user_id: int, item_key: str) -> None:
277:    async def buy(cls, session: AsyncSession, user_id: int, item_key: str, quantity: int = 1) -> int:
323:    async def sell(cls, session: AsyncSession, user_id: int, item_key: str, quantity: int = 1) -> int:
371:def roll_drop(
398:class DropService:
402:    async def entries(session: AsyncSession) -> Dict[str, Dict[str, Any]]:
407:    async def weights_for_level(cls, session: AsyncSession, magic_level: int) -> Dict[str, float]:
424:    async def roll(
aiogram requirement: requirements.txt:1 >=3.30,<4; Bot.copy_message supports native Telegram copies.
Audit writes currently inline: handlers/inv2_controls.py:44; no reusable general write helper found.

# Implementation report

## Additional reconnaissance and schema interpretation

- Panel navigation: `handlers/admin.py:37` (`admin_main` edits the existing
  message); main keyboard `keyboards/common.py:158`. User lookup originally
  `handlers/admin_center.py:125` (panel entry), `:141` (numeric ID/reply),
  `:174` (operation), `:195` (amount). New router is registered before the
  legacy router; old mutation keyboards are rejected as expired.
- Complete **pre-A1 User columns**: `id`, `first_name`, `last_name`, `username`,
  `language_code`, `is_bot`, `is_active`, `profile_skin`
  (`database/models/user.py:13-29`), inherited `created_at`, `updated_at`
  (`database/base.py:19-29`). A1 adds the four ban columns below.
- Normal apples: `database/models/apple.py:14`, `AppleWallet.balance` at `:25`;
  canonical ledger `AppleTransaction` at `:33`.
- Gold, diamond, obsidian: **not separate wallet models**. They are
  `UserItem.quantity`, keyed by `apple_gold`, `apple_diamond`, `apple_obsidian`;
  `database/models/asset.py:31-44`, catalog `services/items.py:36`.
  Also exposed magic powder, a crafting resource, through the same item service.
  Flowers and wedding rings are consumable items, not separate currency balances.
- Seedlings: `database/models/seedling.py:17`, total at `:26`;
  existing `SeedlingService.admin_adjust` reused.
- Card ownership: `database/models/player_card.py:25`; `user_id`, `character_id`,
  `level`, `is_favorite`. Character code is `database/models/character.py:50`.
  Main card uses `PlayerCard.is_favorite`, not a User column.
  Acquire and main selection reuse `services/player_cards.py:52,84`.
- XP: `PlayerProgression.total_xp`, `database/models/player_xp.py:53`.
  **No stored account-level column**; level derives from XP via
  `services/player_xp.py:163`. Level edits set the minimum cumulative XP for
  the requested level using the canonical curve at `:145`. Range: level 1–30,
  XP 0–87000. Per-card level is separate and deliberately not edited here.
- M1 cooldown: `database/models/apple_mine.py:13` (`apple_mine_shift`),
  `worked_at` at `:33`; `services/apple_mine.py:68` reads latest shift.
  There is no standalone cooldown row/field.
- Daily apple claim: **no cooldown table**; latest `AppleTransaction.created_at`
  with `reason='sib_claim'`, `services/sib_claim.py:91-97`.
  `daily_chest_claims` (`database/models/daily_chest.py:40`) is a different,
  daily chest reward gate and was not changed.
- Persian digits: existing `services/sib_claim.py:42` (`fa_digits`), reused.
- Audit: `database/models/admin_log.py:10`; prior writer is inline in
  `handlers/inv2_controls.py:44`, not a reusable general-purpose helper.
  New services write JSON details to the existing table in their transaction.
- aiogram requirement `requirements.txt:1`: >=3.30.0,<4.0.0.
  Installed/tested **3.31.0**; verified `hasattr(Bot, 'copy_message') == True`.
- No authoritative general user/group-membership count exists outside the
  invasion-specific membership subsystem; dashboard says `نامشخص` instead
  of using invasion membership or claiming a historical-match count is membership.

## Broadcast

Entry: `/broadcast` or `📢 پیام همگانی` (`admin_broadcast`).

1. Target picker: all active registered groups or one registered active group.
2. One-group path first requests a negative numeric chat ID.
3. Admin sends content; native `copy_message` creates the preview with controls.
4. Only a successful preview makes a draft sendable.
5. Confirmation consumes the draft before I/O, copies to targets, persists audit,
   and reports results. Edit invalidates preview; cancel clears state.

Callbacks:

```
bcast:target:all
bcast:target:one
bcast:send
bcast:edit
bcast:cancel
```

Drafts are held in `_pending_broadcast: dict[int, PendingBroadcast]`, bound to
admin and source chat, and expire after 600 seconds of inactivity (lazy cleanup).
Native copying preserves media, captions and entities for text/photo/video/
animation/document/sticker/voice/audio and other Telegram-copyable messages.

The service uses a shared process-wide lock and 0.05-second spacing, including
failed requests: conservative 20 requests/second instead of burst batches.
Flood waits sleep and retry exactly once. Forbidden/kicked/not-member/not-found
chats are logged and marked inactive; already inactive groups are never selected.
Summary includes failed targets (including inaccessible ones), but no per-group
error messages are sent during the broadcast. At most ten reason lines:

```
✅ ارسال شد: ۲۴
❌ ناموفق: ۱
دلایل:
-۱۰۰۱۲۳: ربات دسترسی ندارد یا پیام قابل ارسال نیست
```

Audit JSON: admin ID in `admin_logs.admin_id`; target type, optional chat ID,
media type, group count, success count, failure count in `details`.

## User management button tree

```
مدیریت کاربران / /users (numeric ID or reply)
├── 🍏 سیب‌ها
│   ├── 🍏 معمولی: ➕ / ➖
│   ├── 🥇 طلایی: ➕ / ➖
│   ├── 💎 الماسی: ➕ / ➖
│   ├── 🖤 ابسیدینی: ➕ / ➖
│   └── ✨ پودر جادویی: ➕ / ➖
├── 🃏 کارت‌ها: owned codes + main marker
│   ├── ➕ add by character code
│   ├── ➖ remove by character code (unreferenced cards only)
│   └── تنظیم کارت اصلی: character code
├── ⭐ سطح و تجربه: level ➕ / ➖; XP ➕ / ➖
├── 🚫 مسدودیت: ban/unban → confirmation
├── ⏱ ریست کول‌داون‌ها: mine / claim → explicit unsupported notice
├── 🌱 نهال: ➕ / ➖
├── 🔄 بروزرسانی
└── ❌ بستن
```

Callbacks follow `uadm:open`, `menu`, `add`, `sub`, `set`, `ban`, `unban`,
`reset`, `back`, `refresh`, `close` with the requested field/user suffixes.
Extra `uadm:confirm:<opaque-token>` binds a confirmation to the pending edit.
Pending input keys are `(admin_id, user_id, field, mode)`; edits have a
600-second timeout and a source chat/original panel binding. Every edit requires
confirmation; editing another admin requires a second confirmation. Tokens are
consumed before mutation to prevent duplicate delivery. All mutations log old/new
values and refresh the original panel. Negative balances are rejected.

Card acquisition/main selection use existing helpers. Deletion locks the card and
rejects **any** existing trade, battle, mine or invasion-attack reference, even
historical ones, rather than damaging history. Only reads of referenced gameplay
tables occur; no gameplay/history rows are changed. `_archive_inv1_*` is untouched.

## Ban migration and enforcement

Migration **044_user_is_banned**, parent `043_apple_mine_shift`:

- `is_banned BOOLEAN NOT NULL DEFAULT FALSE`
- `banned_at DATETIME NULL`
- `banned_by BIGINT NULL`, named FK `fk_users_banned_by` → `users.id`
- `ban_reason VARCHAR(255) NULL`

Ban sets metadata; unban restores false/null/null/null. No user data is deleted.
`UserBanMiddleware` blocks new message/callback actions for banned accounts,
without changing any gameplay business rules. Existing background activities
are not cancelled. Party removal is explicitly deferred in the confirmation,
result and audit note. Deploy the migration **before** starting the new bot code.

## Persian samples

Broadcast panel:

```
📢 پیام همگانی
مقصد را انتخاب کنید:
[🌍 همه گروه‌ها] [📍 یک گروه خاص]
[❌ لغو]
```

Content prompt:

```
پیام خود را بفرستید. می‌تواند متن، عکس، ویدیو، گیف، فایل، استیکر، ویس یا آهنگ باشد.
```

Dashboard (illustrative values):

```
👤 کاربر: سارا (۱۲۳۴۵)
نام کاربری: @sara
تاریخ ثبت: ۲۰۲۶-۱۰-۱۵
🍏 سیب معمولی: ۱۲۰
🥇 طلایی: ۳
💎 الماسی: ۲
🖤 ابسیدینی: ۰
✨ پودر جادویی: ۴
🌱 نهال: ۵
⭐ سطح: ۲
✨ تجربه: ۲۰۰
🃏 کارت‌ها: ۳
🚫 وضعیت: فعال
تعداد گروه‌ها: نامشخص
```

Apple editor:

```
گزینه موردنظر را انتخاب کنید:
[➕ 🍏 سیب معمولی] [➖ 🍏 سیب معمولی]
[➕ 🥇 طلایی] [➖ 🥇 طلایی]
[➕ 💎 الماسی] [➖ 💎 الماسی]
[➕ 🖤 ابسیدینی] [➖ 🖤 ابسیدینی]
مقدار مثبت را وارد کنید.
⚠️ تغییر 🍏 سیب معمولی برای کاربر ۱۲۳۴۵ به ۱۰ را تأیید می‌کنید؟
✅ تغییر ثبت شد.
```

Ban confirmation:

```
🚫 مسدودسازی کاربر ۱۲۳۴۵ را تأیید می‌کنید؟
هیچ داده‌ای حذف نمی‌شود. بررسی پارتی به تعویق می‌افتد.
[✅ تأیید] [❌ لغو]
```

## Validation

Command (SQLite only, mocked Telegram):

```
PYTHONPATH=/home/user:/home/user/sib \
sib_TEST_DATABASE_URL=sqlite+aiosqlite:////home/user/sib/.cache/sibcom_sib_test.db \
.venv/bin/pytest tests/admin tests/test_admin_auth.py tests/test_admin_center.py \
  tests/test_wallet.py tests/test_player_cards.py -q --tb=short
```

Result: **70 passed, 2 skipped** (31 new A1 tests + 39 existing tests passed).
The skips are existing MariaDB row-lock concurrency tests, not A1 functionality.
A1 tests cover authorization, target selection, native preview, cancel, expiry,
rate spacing, one flood retry, retry exhaustion, inactive/kicked filtering,
audit, summary truncation, currencies, negative prevention, card add/remove/main,
ban/unban/enforcement, admin second-confirm gate, original-panel refresh and
single-use confirms. Cooldown tests assert fail-closed behavior, **not successful
resets**. No live Telegram or MariaDB deployment was tested. `git diff --check`
passes. Initial environment needed a local ignored venv and `aiosqlite`.

## Limitations / incomplete requirements

1. **Mine and daily-claim reset are not implemented.** Neither has a separate
   cooldown field: they derive from immutable award/history timestamps. Rewriting
   or deleting those rows would misrepresent rewards. A proper override requires
   a reset marker consumed by the mine/claim services, which conflicts with this
   phase's explicit prohibition on changing those services/rules. Buttons return
   a Persian explanation and no mutation occurs. Daily chest is untouched.
2. Group count is unknown, as explained above. Only registered active groups can
   be broadcast targets; Telegram has no API to enumerate every membership.
3. Card removal is conservative: cards with historical references cannot be
   deleted. This prevents broken game/trade history but is narrower than arbitrary
   removal by code. Ban reason is currently fixed Persian admin text (no free-text
   reason prompt was specified).
4. Broadcast drafts, confirmation tokens and rate limiting are process-local;
   restart clears drafts, and multiple bot workers would need a shared limiter.
   Telegram messages that cannot be copied (e.g. service/protected-content
   restrictions) are rejected at preview. Albums are per-message, not bundled.
5. Ban blocks new user updates; active parties/background game settlement are
   not modified, per scope. Removal from parties is deferred and reported.
6. Migration is supplied but **not applied to a production database**. MariaDB
   upgrade/downgrade and real Telegram permissions require deployment validation.
7. `invasion_enabled` remains unchanged: seeded false in
   `database/migrations/versions/036_invasion_v2.py:131`; fail-closed default false
   in `services/maintenance/service.py:72`. No runtime production DB was contacted,
   so this is preservation of the supplied false state, not a live DB assertion.
   No invasion-v2, chest, mine, claim, garden, marriage, party, duel or trading
   business-rule files were edited. No archive tables were accessed.
