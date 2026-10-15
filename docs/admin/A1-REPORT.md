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
