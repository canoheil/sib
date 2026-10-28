"""Shop handlers: exactly four sections (Cards / Items / Sell Items / Chests).

Store structure (spec):
    Shop
    ├── Cards        — weekly rotating character cards (Apples)
    ├── Items        — generic items, starting with Magic Powder (Apples)
    ├── Sell Items   — sell eligible inventory items for normal Apples
    └── Chests       — configured shop chests (Apples), see services/shop_chests.py

Item/chest/garden economic values are config-driven (item_buy_prices /
item_sell_prices / magic_powder_level_costs / garden_plot_prices), so
rebalancing never touches this file. Card prices are not: every card
costs the single unified ``ShopService.card_price()`` (300 Apples), and
the purchase button carries only an entry id + nonce - never a price.
"""

import secrets

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.core.logging import logger
from sib.core.text import escape_markdown
from sib.database.connection import get_session
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.lock_order import lock_user_row
from sib.services.items import (
    ITEM_CATALOG,
    ItemService,
    item_description,
    item_emoji,
    item_label,
)
from sib.services.shop import ShopError, ShopService
from sib.services.seedling import (
    SEEDLING_EMOJI,
    SEEDLING_ITEM_KEY,
    SEEDLING_LABEL,
    SeedlingService,
)
from sib.keyboards.common import (
    danger_button,
    primary_button,
    success_button,
)
from sib.services.panel import (
    LOCKED_MESSAGE,
    PanelAuthorizationError,
    resolve_personal_actor,
)
from sib.services.telegram_ui import (
    CAPTION_LIMIT, caption_length, safe_answer_callback, safe_edit_callback_message,
)

router = Router(name="shop")


async def _shop_actor(callback: CallbackQuery) -> int:
    """Resolve the authorized actor for a shop callback (defense in depth).

    The shop is a personal panel; only the opener may browse or buy.
    Service-layer checks (card ownership, wallet balance) are the final
    gate; this helper ensures that a forged/replayed callback on someone
    else's shop panel never reaches the service layer with the wrong
    user_id.
    """
    user = callback.from_user
    if user is None:
        return 0
    try:
        actor_id, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        try:
            await callback.answer(LOCKED_MESSAGE, show_alert=True)
        except Exception:  # pragma: no cover - defensive
            pass
        return 0
    return actor_id


# ---------------------------------------------------------------------------
# Shop root: the three sections
# ---------------------------------------------------------------------------


async def _shop_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Shop ROOT screen: Cards / Items / Sell Items / Chests."""
    async with get_session() as session:
        balance = await AppleWalletService.get_balance(session, user_id)

    lines = [
        "🏪 **فروشگاه سیب** 🍏",
        f"💰 سیب‌های شما: `{balance}`",
        "",
        "دسته‌بندی مورد نظرت رو انتخاب کن 👇",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(text="🎴 کارت‌ها", callback_data="shop_cards"),
                primary_button(text="✨ آیتم‌ها", callback_data="shop_goods"),
            ],
            [
                primary_button(text="💰 فروش آیتم‌ها", callback_data="shop_sell"),
                primary_button(text="🎁 صندوقچه‌ها", callback_data="shop_chests"),
            ],
            [primary_button(text="🔄 بروزرسانی", callback_data="shop_refresh")],
            [danger_button(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")],
        ]
    )
    return "\n".join(lines), kb


@router.message(Command("shop"))
async def cmd_shop(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return
    from sib.services.banner import send_screen_reply
    from sib.services.panel import PanelOwnershipService

    text, kb = await _shop_view(user.id)
    sent = await send_screen_reply(message, "shop", text, kb)
    if sent is not None and sent.message_id:
        async with get_session() as session:
            await PanelOwnershipService.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="shop",
            )
            await session.commit()


@router.callback_query(F.data == "shop_refresh")
async def handle_shop_refresh(callback: CallbackQuery, bot: Bot):
    """Re-render the shop ROOT (also the Back target of every section)."""
    from sib.services.banner import render_banner

    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    text, kb = await _shop_view(actor)
    await safe_answer_callback(callback, "بروزرسانی شد 🍏")
    if callback.message:
        await render_banner(callback, bot, "shop", text, kb)


# ---------------------------------------------------------------------------
# Section 1: Cards (weekly rotation)
# ---------------------------------------------------------------------------


SHOP_CARDS_PAGE_SIZE = 5


async def _cards_view(user_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        entries = await ShopService.list_entries(session, user_id=user_id)
        balance = await AppleWalletService.get_balance(session, user_id)

    total_pages = max(1, (len(entries) + SHOP_CARDS_PAGE_SIZE - 1) // SHOP_CARDS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * SHOP_CARDS_PAGE_SIZE
    page_entries = entries[start : start + SHOP_CARDS_PAGE_SIZE]

    lines = [
        "🏪 **فروشگاه — کارت‌ها** 🎴",
        f"💰 سیب‌های شما: `{balance}`",
        "",
    ]
    if total_pages > 1:
        lines.extend([f"📖 صفحه {page + 1} از {total_pages}", ""])

    buttons: list[list[InlineKeyboardButton]] = []
    # ONE price for every card (present and future): never per-entry,
    # per-character or client-supplied. Render five at a time in a single column.
    price = ShopService.card_price()
    for entry, character, owned in page_entries:
        badge = f" {entry.badge}" if entry.badge else ""
        if owned:
            lines.append(f"✅ *{escape_markdown(character.name_fa)}* — (دارید)")
            buttons.append(
                [primary_button(text=f"✅ {character.name_fa} (خریداری‌شده)", callback_data=f"shop_owned:{entry.id}")]
            )
        else:
            lines.append(f"🍎 *{escape_markdown(character.name_fa)}* — {price} سیب{badge}")
            buttons.append(
                [primary_button(text=f"🛒 خرید {character.name_fa} ({price} 🍎){badge}", callback_data=f"shop_item:{entry.id}:{page}")]
            )

    if not entries:
        lines.append("این هفته کالایی در فروشگاه نیست.")

    if total_pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(
                primary_button(text="◀️ قبلی", callback_data=f"shop_cards:{page - 1}")
            )
        if page + 1 < total_pages:
            navigation.append(
                primary_button(text="▶️ بعدی", callback_data=f"shop_cards:{page + 1}")
            )
        buttons.append(navigation)

    buttons.append([primary_button(text="🔄 بروزرسانی", callback_data=f"shop_cards:{page}")])
    buttons.append([danger_button(text="↩️ بازگشت به فروشگاه", callback_data="shop_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("shop_cards"))
async def handle_shop_cards(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return

    page = 0
    if callback.data != "shop_cards":
        prefix, separator, value = callback.data.partition(":")
        if prefix != "shop_cards" or not separator or not value.isdecimal():
            await safe_answer_callback(callback, "این صفحهٔ فروشگاه معتبر نیست.", show_alert=True)
            return
        page = int(value)

    text, kb = await _cards_view(actor, page=page)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


async def _shop_item_view(user_id: int, entry_id: int, page: int = 0):
    """Full card detail: art + wiki description + stats + price + actions."""
    from sib.services.character_art import photo_arg, resolve_character_photo
    from sib.services.characters import render_character_card
    from sib.services.factions import FactionService

    async with get_session() as session:
        entry, character, owned = await ShopService.get_entry(session, entry_id, user_id=user_id)
        balance = await AppleWalletService.get_balance(session, user_id)
        faction = (
            await FactionService.get_for_character(session, character)
            if character is not None
            else None
        )
        art_photo = None
        if entry is not None and character is not None:
            file_id, fs_path = await resolve_character_photo(session, character)
            art_photo = photo_arg(file_id, fs_path)

    if entry is None:
        text = "کالای مورد نظر در فروشگاه این هفته نیست."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[danger_button(text="↩️ بازگشت به کارت‌ها", callback_data=f"shop_cards:{page}")]]
        )
        return text, kb, None

    price = ShopService.card_price()
    badge = f" {entry.badge}" if entry.badge else ""
    lines = [
        render_character_card(character, faction=faction),
        "",
        f"💰 سیب‌های شما: `{balance}`{badge}",
    ]
    if owned:
        action = [primary_button(text="✅ خریداری‌شده", callback_data=f"shop_owned:{entry.id}")]
    else:
        # One nonce per rendered page: a double tap reuses it (charged once),
        # while a fresh page mints a new one (a deliberate new purchase).
        nonce = secrets.token_urlsafe(9)
        action = [
            success_button(
                text=f"✅ تأیید خرید ({price} 🍎)",
                callback_data=f"shop_buy:{entry.id}:{nonce}",
            )
        ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            action,
            [danger_button(text="↩️ بازگشت به کارت‌ها", callback_data=f"shop_cards:{page}")],
        ]
    )
    return "\n".join(lines), kb, art_photo


@router.callback_query(F.data.startswith("shop_item:"))
async def handle_shop_item(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    entry_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdecimal() else 0
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    text, kb, art_photo = await _shop_item_view(actor, entry_id, page=page)
    if art_photo is not None and caption_length(text) > CAPTION_LIMIT:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [primary_button(
                text="📖 متن کامل کارت", callback_data=f"shop_item_full:{entry_id}:{page}",
            )],
            *kb.inline_keyboard,
        ])
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=art_photo)


@router.callback_query(F.data.startswith("shop_item_full:"))
async def handle_shop_item_full(callback: CallbackQuery, bot: Bot):
    """Unabridged shop description, shown only when the caption is too long."""
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    parts = callback.data.split(":")
    entry_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdecimal() else 0
    text, kb, art_photo = await _shop_item_view(actor, entry_id, page=page)
    if art_photo is not None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [primary_button(
                text="↩️ بازگشت به کارت", callback_data=f"shop_item:{entry_id}:{page}",
            )],
            *kb.inline_keyboard,
        ])
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("shop_owned:"))
async def handle_shop_owned(callback: CallbackQuery, bot: Bot):
    await safe_answer_callback(callback, "شما قبلاً این شخصیت را دارید!", show_alert=True)


@router.callback_query(F.data.startswith("shop_buy:"))
async def handle_shop_buy(callback: CallbackQuery, bot: Bot):
    """Card purchase. Callback data: ``shop_buy:{entry_id}:{nonce}``."""
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return

    from sib.services.player_xp import PlayerXPService
    from sib.services.shop import OPERATION_NONCE_RE, STALE_PURCHASE_TEXT

    parts = callback.data.split(":")
    try:
        entry_id = int(parts[1])
        nonce = parts[2] if len(parts) == 3 else ""
    except (IndexError, ValueError):
        await safe_answer_callback(callback, STALE_PURCHASE_TEXT, show_alert=True)
        return

    # Old nonce-less buttons (rendered before per-press nonces existed) and
    # malformed nonces are refused here, before any database work. The user
    # is told to re-open the shop; an empty nonce never authorizes a purchase.
    if not OPERATION_NONCE_RE.match(nonce or ""):
        await safe_answer_callback(callback, STALE_PURCHASE_TEXT, show_alert=True)
        return

    chat_id = getattr(getattr(callback.message, "chat", None), "id", None)
    # The refusal escapes the transaction block: whatever the purchase wrote
    # before refusing is rolled back, never committed.
    try:
        async with get_session() as session:
            PlayerXPService.set_notification_chat(session, chat_id)
            outcome = await ShopService.purchase(session, actor, entry_id, nonce=nonce)
    except ShopError as e:
        await safe_answer_callback(callback, str(e), show_alert=True)
        return

    await PlayerXPService.deliver_pending_notifications(bot, fallback_chat_id=chat_id)

    from sib.services.banner import render_banner

    text, kb = await _shop_view(actor)
    if outcome.replayed:
        toast = "این خرید قبلاً ثبت شده است؛ مبلغی دوباره کسر نشد. ✅"
    else:
        toast = "خرید انجام شد! کارت به مجموعه شما اضافه شد 🎉"
    await safe_answer_callback(callback, toast)
    if callback.message:
        await render_banner(callback, bot, "shop", text, kb)


# ---------------------------------------------------------------------------
# Section 2: Items (Magic Powder and future items)
# ---------------------------------------------------------------------------


def _buyable_keys(buy_prices: dict) -> list[str]:
    """Configured buyable keys in catalog order (stable UI ordering)."""
    ordered = [k for k in ITEM_CATALOG if k in buy_prices]
    ordered += [k for k in buy_prices if k not in ITEM_CATALOG]
    return ordered


async def _goods_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        balance = await AppleWalletService.get_balance(session, user_id)
        buy_prices = await ItemService.buy_prices(session)
        owned = await ItemService.list_items(session, user_id)

    lines = [
        "✨ **فروشگاه — آیتم‌ها**",
        f"💰 سیب‌های شما: `{balance}`",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for key in _buyable_keys(buy_prices):
        price = int(buy_prices[key])
        have = owned.get(key, 0)
        lines.append(f"{item_emoji(key)} {item_label(key)} — `{price}` سیب (موجودی شما: `{have}`)")
        buttons.append(
            [success_button(
                text=f"🛒 {item_emoji(key)} {item_label(key)} ({price} 🍎)",
                callback_data=f"shop_good:{key}",
            )]
        )

    if not buttons:
        lines.append("فعلاً آیتمی برای فروش نیست.")
    buttons.append([primary_button(text="🔄 بروزرسانی", callback_data="shop_goods")])
    buttons.append([danger_button(text="↩️ بازگشت به فروشگاه", callback_data="shop_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "shop_goods")
async def handle_shop_goods(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    text, kb = await _goods_view(actor)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


async def _good_view(user_id: int, item_key: str):
    """Item detail: bundled art + description + price + buy action."""
    from sib.services.assets import AssetService
    from sib.services.character_art import photo_arg

    async with get_session() as session:
        price = await ItemService.buy_price(session, item_key)
        have = await ItemService.get_total(session, user_id, item_key)
        balance = await AppleWalletService.get_balance(session, user_id)
        file_id, fs_path = await AssetService.resolve_photo(session, item_key)
        art_photo = photo_arg(file_id, fs_path)

    if price is None:
        text = "این آیتم در فروشگاه نیست."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[danger_button(text="🔙 بازگشت", callback_data="shop_goods")]]
        )
        return text, kb, None

    lines = [
        f"{item_emoji(item_key)} **{item_label(item_key)}**",
        "",
        item_description(item_key),
        "",
        f"💰 قیمت: `{price}` سیب 🍎",
        f"📦 موجودی شما: `{have}`",
        f"💰 سیب‌های شما: `{balance}`",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(text="🛒 خرید ۱", callback_data=f"shop:buy:{item_key}:1"),
                success_button(text="🛒 خرید ۵", callback_data=f"shop:buy:{item_key}:5"),
            ],
            [
                success_button(text="🛒 خرید ۱۰", callback_data=f"shop:buy:{item_key}:10"),
                success_button(text="🛒 خرید حداکثر", callback_data=f"shop:buy:{item_key}:max"),
            ],
            [danger_button(text="🔙 بازگشت", callback_data="shop_goods")],
        ]
    )
    return "\n".join(lines), kb, art_photo


@router.callback_query(F.data.startswith("shop_good:"))
async def handle_shop_good(callback: CallbackQuery, bot: Bot):
    item_key = callback.data.split(":", 1)[1]
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    text, kb, art_photo = await _good_view(actor, item_key)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=art_photo)


def _parse_good_buy_callback(data: str) -> tuple[str, str]:
    """Extract ``(item_key, raw_quantity)`` from a shop item purchase callback.

    Supports ``shop:buy:<item_code>:<quantity>`` as well as legacy
    ``shop_good_buy:<item_code>[:<quantity>]`` callbacks.
    """
    if data.startswith("shop:buy:"):
        rest = data[len("shop:buy:") :]
    elif data.startswith("shop_good_buy:"):
        rest = data[len("shop_good_buy:") :]
    else:
        return "", ""

    if ":" in rest:
        item_key, raw_qty = rest.rsplit(":", 1)
    else:
        item_key, raw_qty = rest, "1"
    return item_key, raw_qty


@router.callback_query(F.data.startswith("shop:buy:"))
@router.callback_query(F.data.startswith("shop_good_buy:"))
async def handle_shop_good_buy(callback: CallbackQuery, bot: Bot):
    if not callback.data:
        return
    item_key, raw_qty = _parse_good_buy_callback(callback.data)
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return

    is_max = raw_qty == "max"
    quantity = 0
    if not is_max:
        if not raw_qty.isdecimal() or not raw_qty.isascii():
            await safe_answer_callback(callback, "تعداد نامعتبر است.", show_alert=True)
            return
        quantity = int(raw_qty)
        if quantity < 1:
            await safe_answer_callback(callback, "تعداد باید حداقل ۱ باشد.", show_alert=True)
            return

    # Refusals and DB errors escape the transaction block (nothing commits).
    try:
        async with get_session() as session:
            if is_max:
                await lock_user_row(session, actor)
                if item_key not in ITEM_CATALOG:
                    raise ValueError("این آیتم در فروشگاه قابل خرید نیست.")
                item_price = await ItemService.buy_price(session, item_key)
                if item_price is None or item_price <= 0:
                    raise ValueError("این آیتم در فروشگاه قابل خرید نیست.")
                await ItemService._lock_inventory_row(session, actor, item_key)
                user_balance = await AppleWalletService.get_balance(
                    session, actor, for_update=True
                )
                max_qty = user_balance // item_price
                if max_qty == 0:
                    raise ValueError("موجودی سیب شما کافی نیست!")
                quantity = max_qty

            await ItemService.buy(session, actor, item_key, quantity)
    except ValueError as e:
        await safe_answer_callback(callback, str(e), show_alert=True)
        return
    except Exception:
        logger.exception(f"shop item purchase failed (user={actor}, item={item_key})")
        await safe_answer_callback(
            callback, "خطایی در خرید رخ داد. دوباره تلاش کنید.", show_alert=True
        )
        return

    item_name = item_label(item_key)
    success_msg = f"✅ با موفقیت {quantity} عدد {item_name} خریدید."
    await safe_answer_callback(callback, success_msg)
    detail_text, kb, art_photo = await _good_view(actor, item_key)
    text = f"{success_msg}\n\n{detail_text}"
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=art_photo)


# ---------------------------------------------------------------------------
# Section 3: Sell Items (special Apples -> normal Apples)
# ---------------------------------------------------------------------------
#
# Flow (spec):
#     Sell Items  ->  list of OWNED sellable items
#                 ->  item page (art + price + Sell 1 / Sell All)
#                 ->  Sell All confirmation  ->  back to the list
#
# Sellability and prices stay CONFIG-driven (``item_sell_prices``), so only
# the three special Apples are listed and normal Apples — the CURRENCY, a
# wallet balance and not an inventory row — can never enter this flow.
# Every screen is rendered from FRESH inventory data and every sale re-reads
# the owned quantity inside the transaction, so a stale callback, a rapid
# double click or a concurrent change can never overpay or go negative.


SELL_EMPTY_LINE = (
    "آیتم قابل‌فروشی نداری!\n"
    "سیب‌های ویژه 🌕💎🌑 را از برداشت درخت‌های باغ به دست بیاور."
)

# Seedlings are NOT ``user_items`` rows: they live in ``seedling_inventories``
# and only the ones above the garden's planting capacity are sellable (see
# ``SeedlingService``), so the Sell section gets its own entry + callbacks.
SEEDLING_SELL_CALLBACK = "shop_sell_seedling"
SEEDLING_SELL_ONE_CALLBACK = "shop_sell_seedling_one"
SEEDLING_SELL_ALL_CALLBACK = "shop_sell_seedling_all"
SEEDLING_SELL_ALL_OK_CALLBACK = "shop_sell_seedling_all_ok"

SELL_EMPTY_SEEDLING_HINT = (
    "🌱 نهال‌ها هم وقتی از ظرفیت باغت بیشتر بشن، همین‌جا برای فروش ظاهر می‌شن."
)


async def _sell_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Sell Items ROOT: one button per OWNED sellable item (name + qty)."""
    async with get_session() as session:
        balance = await AppleWalletService.get_balance(session, user_id)
        sellable = await ItemService.sellable_inventory(session, user_id)
        seedling_total, seedling_capacity, seedling_extra, seedling_price = (
            await SeedlingService.sell_summary(session, user_id)
        )

    lines = [
        "💰 **فروشگاه — فروش آیتم‌ها**",
        f"💰 سیب‌های شما: `{balance}`",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for key, have, _price in sellable:
        buttons.append(
            [primary_button(
                text=f"{item_emoji(key)} {item_label(key)} — {have}",
                callback_data=f"shop_sell_item:{key}",
            )]
        )

    # Seedling entry: only when selling is enabled AND the player owns more
    # seedlings than their garden can ever plant.
    seedling_lines: list[str] = []
    if seedling_price is not None and seedling_extra > 0:
        seedling_lines = [
            f"{SEEDLING_EMOJI} {SEEDLING_LABEL}: `{seedling_total}` "
            f"(قابل فروش: `{seedling_extra}` — بیشتر از ظرفیت باغ `{seedling_capacity}`)"
        ]
        buttons.append(
            [primary_button(
                text=f"{SEEDLING_EMOJI} {SEEDLING_LABEL} — {seedling_extra} قابل فروش",
                callback_data=SEEDLING_SELL_CALLBACK,
            )]
        )

    if buttons:
        lines.extend(seedling_lines)
        lines.append("آیتمی که می‌خوای بفروشی رو انتخاب کن 👇")
    else:
        lines.append(SELL_EMPTY_LINE)
        lines.append(SELL_EMPTY_SEEDLING_HINT)
    buttons.append([primary_button(text="🔄 بروزرسانی", callback_data="shop_sell")])
    buttons.append([danger_button(text="↩️ بازگشت به فروشگاه", callback_data="shop_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _sell_item_state(user_id: int, item_key: str):
    """Fresh (price, quantity, balance, photo) for one sellable item.

    ``price is None`` means "not sellable" (unknown/invalid item id);
    ``quantity == 0`` means the user no longer owns it.
    """
    from sib.services.assets import AssetService
    from sib.services.character_art import photo_arg

    async with get_session() as session:
        price = await ItemService.sell_price(session, item_key)
        if price is None:
            return None, 0, 0, None
        have = await ItemService.get_total(session, user_id, item_key)
        balance = await AppleWalletService.get_balance(session, user_id)
        # The ONE existing item -> image mapping (garden celebration art).
        asset = await ItemService.asset_key(session, item_key)
        file_id, fs_path = await AssetService.resolve_photo(session, asset)
        photo = photo_arg(file_id, fs_path)
    return price, have, balance, photo


async def _sell_item_view(user_id: int, item_key: str):
    """Item page: art, name, owned quantity, sell price, Sell 1 / Sell All."""
    price, have, balance, photo = await _sell_item_state(user_id, item_key)

    if price is None or have <= 0:
        text, kb = await _sell_view(user_id)
        return text, kb, None, False

    lines = [
        f"{item_emoji(item_key)} **{item_label(item_key)}**",
        "",
        item_description(item_key),
        "",
        f"📦 موجودی شما: `{have}`",
        f"💸 قیمت فروش: هر عدد `{price}` سیب 🍎",
        f"🧮 ارزش کل: `{have * price}` سیب 🍎",
        f"💰 سیب‌های شما: `{balance}`",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [success_button(
                text=f"💸 فروش یکی ({price} 🍎)",
                callback_data=f"shop_sell_one:{item_key}",
            )],
            [success_button(
                text=f"💸 فروش همه ({have * price} 🍎)",
                callback_data=f"shop_sell_all:{item_key}",
            )],
            [danger_button(text="↩️ بازگشت به فروش آیتم‌ها", callback_data="shop_sell")],
        ]
    )
    return "\n".join(lines), kb, photo, True


async def _sell_confirm_view(user_id: int, item_key: str):
    """Sell-All confirmation. The shown quantity is INFORMATIONAL only —
    the confirm handler re-reads the inventory before selling."""
    price, have, _balance, photo = await _sell_item_state(user_id, item_key)

    if price is None or have <= 0:
        text, kb = await _sell_view(user_id)
        return text, kb, None, False

    lines = [
        f"⚠️ **تأیید فروش همه**",
        "",
        f"مطمئنی می‌خوای همهٔ `{have}` عدد "
        f"{item_emoji(item_key)} «{item_label(item_key)}» رو بفروشی؟",
        "",
        f"💰 دریافتی تقریبی: `{have * price}` سیب 🍎",
        "",
        "این کار برگشت‌پذیر نیست.",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [success_button(
                text="✅ بله، همه را بفروش",
                callback_data=f"shop_sell_all_ok:{item_key}",
            )],
            [danger_button(
                text="❌ انصراف",
                callback_data=f"shop_sell_item:{item_key}",
            )],
        ]
    )
    return "\n".join(lines), kb, photo, True


async def _render_sell_list(callback: CallbackQuery, bot: Bot, user_id: int, answer=None) -> None:
    if answer is not None:
        await safe_answer_callback(callback, answer, show_alert=True)
    text, kb = await _sell_view(user_id)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data == "shop_sell")
async def handle_shop_sell(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    await safe_answer_callback(callback)
    await _render_sell_list(callback, bot, actor)


@router.callback_query(F.data.startswith("shop_sell_item:"))
async def handle_shop_sell_item(callback: CallbackQuery, bot: Bot):
    """Open (or re-open, e.g. after Cancel) one item's selling page."""
    item_key = callback.data.split(":", 1)[1]
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    text, kb, photo, ok = await _sell_item_view(actor, item_key)
    await safe_answer_callback(callback, None if ok else "این آیتم دیگر در انبار تو نیست.")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=photo)


@router.callback_query(F.data.startswith("shop_sell_one:"))
async def handle_shop_sell_one(callback: CallbackQuery, bot: Bot):
    """Sell exactly ONE unit, then refresh the item page with fresh data."""
    item_key = callback.data.split(":", 1)[1]
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return

    # Refusals (stale click, empty stock, not sellable) escape the
    # transaction block so nothing commits; the screen is rendered after it.
    error = None
    apples = 0
    try:
        async with get_session() as session:
            apples = await ItemService.sell(session, actor, item_key, 1)
    except ValueError as e:
        error = str(e)
    if error is not None:
        await _render_sell_list(callback, bot, actor, answer=error)
        return

    # Sold out -> _sell_item_view falls back to the (now shorter) LIST,
    # from which this item has disappeared; otherwise the item page is
    # re-rendered from fresh inventory data.
    text, kb, photo, _still_owned = await _sell_item_view(actor, item_key)
    await safe_answer_callback(callback, f"فروش انجام شد! `{apples}` سیب دریافت کردی 🍎")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=photo)


@router.callback_query(F.data.startswith("shop_sell_all_ok:"))
async def handle_shop_sell_all_confirm(callback: CallbackQuery, bot: Bot):
    """Confirmed Sell All: re-read the CURRENT inventory and sell all of it."""
    item_key = callback.data.split(":", 1)[1]
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return

    error = None
    apples = 0
    try:
        async with get_session() as session:
            # User row first (lock policy rank 0), then the inventory row.
            await lock_user_row(session, actor)
            # Authoritative quantity read inside the selling transaction (the
            # quantity shown on the confirmation screen is never trusted); the
            # row lock makes a double click sell the remaining 0 units, i.e.
            # fail loudly instead of paying twice.
            have = await ItemService.get_total(session, actor, item_key, for_update=True)
            if have <= 0:
                raise ValueError("از این آیتم نداری!")
            apples = await ItemService.sell(session, actor, item_key, have)
    except ValueError as e:
        error = str(e)
    if error is not None:
        await _render_sell_list(callback, bot, actor, answer=error)
        return

    await safe_answer_callback(callback, f"فروش انجام شد! `{apples}` سیب دریافت کردی 🍎")
    await _render_sell_list(callback, bot, actor)


@router.callback_query(F.data.startswith("shop_sell_all:"))
async def handle_shop_sell_all(callback: CallbackQuery, bot: Bot):
    """Sell All asks first — no transaction happens on this screen."""
    item_key = callback.data.split(":", 1)[1]
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    text, kb, photo, ok = await _sell_confirm_view(actor, item_key)
    await safe_answer_callback(callback, None if ok else "از این آیتم نداری!")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=photo)


# ---------------------------------------------------------------------------
# Section 3b: Sell EXTRA seedlings (above the garden's planting capacity)
# ---------------------------------------------------------------------------
#
# Flow (same shape as the items above):
#     Sell Items  ->  «نهال» (art + capacity + price)
#                 ->  Sell 1 / Sell All -> confirmation -> back to the list
#
# Seedlings are not ``user_items`` rows, so they get their own service call
# (``SeedlingService.sell``) and callbacks, but the SAME currency (normal
# Apples, wallet + ledger, reason ``seedling_sold``) as every other sale.
# Only seedlings above ``garden.capacity_plots`` are sellable: a planted tree
# is never removed, so those extra seedlings can never be planted.


async def _seedling_sell_state(user_id: int):
    """Fresh (price, total, capacity, extra, balance, photo) for the page."""
    from sib.services.assets import AssetService
    from sib.services.character_art import photo_arg

    async with get_session() as session:
        total, capacity, extra, price = await SeedlingService.sell_summary(
            session, user_id
        )
        balance = await AppleWalletService.get_balance(session, user_id)
        file_id, fs_path = await AssetService.resolve_photo(session, SEEDLING_ITEM_KEY)
        photo = photo_arg(file_id, fs_path)
    return price, total, capacity, extra, balance, photo


async def _seedling_sell_view(user_id: int):
    """Seedling page: art, capacity, owned total, sell price, Sell 1 / All."""
    price, total, capacity, extra, balance, photo = await _seedling_sell_state(user_id)

    if price is None or extra <= 0:
        text, kb = await _sell_view(user_id)
        return text, kb, None, False

    lines = [
        f"{SEEDLING_EMOJI} **{SEEDLING_LABEL}**",
        "",
        "هر نهال توی باغ به یک درخت تبدیل می‌شه؛ پس فقط نهال‌های بیشتر از "
        "ظرفیت باغت قابل فروشاند.",
        "",
        f"📦 نهال‌های شما: `{total}`",
        f"🌳 ظرفیت باغ: `{capacity}` قطعه",
        f"💸 قابل فروش: `{extra}`",
        f"💰 قیمت فروش هر نهال: `{price}` سیب 🍎",
        f"🧮 ارزش کل: `{extra * price}` سیب 🍎",
        f"💰 سیب‌های شما: `{balance}`",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [success_button(
                text=f"💸 فروش یکی ({price} 🍎)",
                callback_data=SEEDLING_SELL_ONE_CALLBACK,
            )],
            [success_button(
                text=f"💸 فروش همه ({extra * price} 🍎)",
                callback_data=SEEDLING_SELL_ALL_CALLBACK,
            )],
            [danger_button(text="↩️ بازگشت به فروش آیتم‌ها", callback_data="shop_sell")],
        ]
    )
    return "\n".join(lines), kb, photo, True


async def _seedling_sell_confirm_view(user_id: int):
    """Sell-All confirmation. The shown count is informational only — the
    confirm handler re-reads the garden capacity and the inventory first."""
    price, total, capacity, extra, balance, photo = await _seedling_sell_state(user_id)

    if price is None or extra <= 0:
        text, kb = await _sell_view(user_id)
        return text, kb, None, False

    lines = [
        "⚠️ **تأیید فروش نهال‌ها**",
        "",
        f"مطمئنی می‌خوای همهٔ `{extra}` نهال اضافه رو بفروشی؟",
        f"🌳 ظرفیت باغت `{capacity}` قطعه‌ست و این نهال‌ها به باغت اضافه نمی‌شن.",
        "",
        f"💰 دریافتی تقریبی: `{extra * price}` سیب 🍎",
        "",
        "این کار برگشت‌پذیر نیست.",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [success_button(
                text="✅ بله، همه را بفروش",
                callback_data=SEEDLING_SELL_ALL_OK_CALLBACK,
            )],
            [danger_button(text="❌ انصراف", callback_data=SEEDLING_SELL_CALLBACK)],
        ]
    )
    return "\n".join(lines), kb, photo, True


@router.callback_query(F.data == SEEDLING_SELL_CALLBACK)
async def handle_shop_sell_seedling(callback: CallbackQuery, bot: Bot):
    """Open the seedling selling page (or the list when nothing is extra)."""
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    text, kb, photo, ok = await _seedling_sell_view(actor)
    await safe_answer_callback(
        callback, None if ok else "فعلاً نهال اضافه‌ای برای فروش نداری."
    )
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=photo)


@router.callback_query(F.data == SEEDLING_SELL_ONE_CALLBACK)
async def handle_shop_sell_seedling_one(callback: CallbackQuery, bot: Bot):
    """Sell exactly ONE extra seedling, then refresh the page with fresh data."""
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return

    error = None
    apples = 0
    try:
        async with get_session() as session:
            apples = await SeedlingService.sell(session, actor, 1)
    except ValueError as e:
        error = str(e)
    if error is not None:
        await _render_sell_list(callback, bot, actor, answer=error)
        return

    # No extras left -> _seedling_sell_view falls back to the (now shorter)
    # list; otherwise the page is re-rendered from fresh inventory data.
    text, kb, photo, _still_available = await _seedling_sell_view(actor)
    await safe_answer_callback(callback, f"فروش انجام شد! `{apples}` سیب دریافت کردی 🍎")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=photo)


@router.callback_query(F.data == SEEDLING_SELL_ALL_CALLBACK)
async def handle_shop_sell_seedling_all(callback: CallbackQuery, bot: Bot):
    """Sell All asks first — no transaction happens on this screen."""
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return
    text, kb, photo, ok = await _seedling_sell_confirm_view(actor)
    await safe_answer_callback(
        callback, None if ok else "نهال اضافه‌ای برای فروش نداری!"
    )
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=photo)


@router.callback_query(F.data == SEEDLING_SELL_ALL_OK_CALLBACK)
async def handle_shop_sell_seedling_all_confirm(callback: CallbackQuery, bot: Bot):
    """Confirmed Sell All: re-read capacity + inventory, sell every extra."""
    user = callback.from_user
    if not user:
        return
    actor = await _shop_actor(callback)
    if not actor or actor != user.id:
        return

    error = None
    apples = 0
    try:
        async with get_session() as session:
            # User row first (lock policy rank 0), then the seedling row.
            await lock_user_row(session, actor)
            _total, _capacity, extra, _price = await SeedlingService.sell_summary(
                session, actor
            )
            if extra <= 0:
                raise ValueError("نهال اضافه‌ای برای فروش نداری!")
            apples = await SeedlingService.sell(session, actor, extra)
    except ValueError as e:
        error = str(e)
    if error is not None:
        await _render_sell_list(callback, bot, actor, answer=error)
        return

    await safe_answer_callback(callback, f"فروش انجام شد! `{apples}` سیب دریافت کردی 🍎")
    await _render_sell_list(callback, bot, actor)


# ---------------------------------------------------------------------------
# Section 4: Chests (configured shop chests, paid in Apples)
# ---------------------------------------------------------------------------
#
# Callbacks: ``shop_chests`` (list), ``shop_chest:<id>`` (detail) and
# ``shop_chest_open:<id>:<version>:<nonce>`` (open). The version is the chest
# version the page was rendered from; a changed chest refuses the stale page
# before any write (see ``ShopChestService.open_chest``). The nonce is minted per rendered
# detail page (see services/shop_chests.py): a double tap reuses it and is
# charged once; a freshly rendered page starts a new opening.

CHEST_BACK_CALLBACK = "shop_chests"
USER_CHEST_UNAVAILABLE_TEXT = "این صندوقچه فعلاً در دسترس نیست."


async def _chests_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Chest LIST: name, price and a button per valid active chest."""
    from sib.services.shop_chests import ShopChestConfigError, ShopChestService

    async with get_session() as session:
        balance = await AppleWalletService.get_balance(session, user_id)
        valid = []
        for chest in await ShopChestService.list_active(session):
            try:
                await ShopChestService.validate_chest(session, chest)
            except ShopChestConfigError as exc:
                # A misconfigured chest is hidden from players, never sold.
                logger.error(f"hiding misconfigured shop chest {chest.code}: {exc}")
                continue
            valid.append(chest)

    lines = [
        "🎁 **صندوقچه‌های فروشگاه**",
        f"💰 سیب‌های شما: `{balance}`",
        "",
    ]
    if not valid:
        lines.append("فعلاً صندوقچه‌ای برای فروش نیست.")
    else:
        lines.append("صندوقچه‌ی مورد نظرت رو انتخاب کن 👇")
        for chest in valid:
            lines.append(f"• {chest.name} — `{chest.price_apples}` سیب 🍎")
    rows = [
        [primary_button(text=f"🎁 {chest.name} ({chest.price_apples} 🍎)", callback_data=f"shop_chest:{chest.id}")]
        for chest in valid
    ]
    rows.append([danger_button(text="↩️ بازگشت به فروشگاه", callback_data="shop_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _chest_detail_view(user_id: int, chest_id: int):
    """Chest DETAIL: image, probabilities from weights, open action.

    Returns ``(text, kb, photo)`` or ``None`` when the chest is gone/inactive.
    """
    from sib.services.assets import AssetService
    from sib.services.character_art import photo_arg
    from sib.services.shop_chests import (
        ShopChestConfigError,
        ShopChestService,
        format_probability,
    )

    async with get_session() as session:
        chest = await ShopChestService.get_chest(session, chest_id)
        if chest is None or not chest.is_active:
            return None
        balance = await AppleWalletService.get_balance(session, user_id)
        try:
            await ShopChestService.validate_chest(session, chest)
            table = await ShopChestService.describe(session, chest)
            configured = True
        except ShopChestConfigError as exc:
            logger.error(f"shop chest {chest.code} is misconfigured: {exc}")
            table, configured = [], False
        photo = None
        if chest.image_key:
            file_id, fs_path = await AssetService.resolve_photo(session, chest.image_key)
            photo = photo_arg(file_id, fs_path)
        name, price = chest.name, chest.price_apples
        duplicate_apples = chest.duplicate_apples

    lines = [f"🎁 **{name}**", "", f"💰 قیمت: `{price}` سیب 🍎", f"💰 سیب‌های شما: `{balance}`"]
    rows = []
    if configured:
        lines += ["", "🎲 **جوایز و احتمال هر جایزه:**"]
        for _spec, probability, label in table:
            lines.append(f"• {label} — {format_probability(probability)}")
        if duplicate_apples:
            lines.append(f"🔁 کارت تکراری ← `{duplicate_apples}` سیب 🍎")
        # One nonce per rendered page (double taps share it; a new page is a new buy).
        nonce = secrets.token_urlsafe(9)
        rows.append([
            success_button(
                text=f"🎁 باز کردن ({price} 🍎)",
                callback_data=f"shop_chest_open:{chest.id}:{chest.version}:{nonce}",
            )
        ])
    else:
        lines += ["", USER_CHEST_UNAVAILABLE_TEXT]
    rows.append([danger_button(text="↩️ بازگشت به صندوقچه‌ها", callback_data=CHEST_BACK_CALLBACK)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), photo


def _opened_text(result, balance_note: str = "") -> str:
    """Immediate result screen for one opening (rendered after commit)."""
    lines = []
    if result.replayed:
        lines.append("این باز کردن قبلاً ثبت شده بود؛ مبلغی دوباره کسر نشد.")
        lines.append("")
    lines.append(f"🎁 از **{result.chest_name}** باز شد!")
    lines.append("")
    if result.reward_kind == "apples":
        lines.append(f"🍎 دریافت شد: `{result.apples_awarded}` سیب")
    elif result.reward_kind == "item":
        lines.append(f"{item_emoji(result.item_key)} دریافت شد: {item_label(result.item_key)}")
    elif result.duplicate_converted:
        lines.append(f"🔁 کارت «{result.character_name or ''}» را از قبل داشتی.")
        if result.compensation_apples:
            lines.append(f"🍎 به‌جایش `{result.compensation_apples}` سیب گرفتی.")
    else:
        lines.append(f"🎴 کارت جدید: **{result.character_name or ''}** به مجموعه‌ات اضافه شد!")
    lines.append("")
    lines.append(f"💸 هزینه: `{result.price_apples}` سیب")
    lines.append(f"💰 موجودی: `{result.balance_after}` سیب 🍎")
    if balance_note:
        lines.append(balance_note)
    return "\n".join(lines)


@router.callback_query(F.data == "shop_chests")
async def handle_shop_chests(callback: CallbackQuery, bot: Bot):
    actor = await _shop_actor(callback)
    if not actor:
        return
    text, kb = await _chests_view(actor)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("shop_chest:"))
async def handle_shop_chest(callback: CallbackQuery, bot: Bot):
    actor = await _shop_actor(callback)
    if not actor:
        return
    try:
        chest_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await safe_answer_callback(callback, "این صندوقچه پیدا نشد.", show_alert=True)
        return
    view = await _chest_detail_view(actor, chest_id)
    if view is None:
        text, kb = await _chests_view(actor)
        await safe_answer_callback(callback, "این صندوقچه دیگر در فروشگاه نیست.", show_alert=True)
        await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
        return
    text, kb, photo = view
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=photo)


CHEST_UNKNOWN_OUTCOME_TEXT = "نتیجه باز کردن صندوقچه نامشخص است. موجودی را بررسی کن و اگر تغییری ندیدی دوباره امتحان کن."


@router.callback_query(F.data.startswith("shop_chest_open:"))
async def handle_shop_chest_open(callback: CallbackQuery, bot: Bot):
    """Open a chest. Callback data: ``shop_chest_open:{chest_id}:{version}:{nonce}``.

    A callback in any other shape (including the pre-version form) is treated
    as a stale page: nothing is charged and nothing is granted.
    """
    from sib.services.shop_chests import STALE_OPEN_TEXT, ShopChestError, ShopChestService

    actor = await _shop_actor(callback)
    if not actor:
        return
    parts = callback.data.split(":")
    try:
        if len(parts) != 4:
            raise ValueError("unversioned open callback")
        chest_id = int(parts[1])
        version = int(parts[2])
        nonce = parts[3]
    except (IndexError, ValueError):
        await safe_answer_callback(callback, STALE_OPEN_TEXT, show_alert=True)
        return

    try:
        result = await ShopChestService.open_chest_reliably(
            actor, chest_id, nonce, expected_version=version
        )
    except ShopChestError as exc:
        await safe_answer_callback(callback, str(exc), show_alert=True)
        return
    except Exception:
        # Unexpected failure: the outcome is unknown to the user, so say that.
        # Never claim success here; the stored opening (if any) is shown on the
        # next open, which replays the same key.
        logger.exception(f"shop chest open failed (user={actor}, chest={chest_id})")
        await safe_answer_callback(callback, CHEST_UNKNOWN_OUTCOME_TEXT, show_alert=True)
        return

    nonce_again = secrets.token_urlsafe(9)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [success_button(
                text=f"🎁 باز کردن دوباره ({result.price_apples} 🍎)",
                callback_data=f"shop_chest_open:{chest_id}:{result.chest_version}:{nonce_again}",
            )],
            [danger_button(text="↩️ بازگشت به صندوقچه‌ها", callback_data=CHEST_BACK_CALLBACK)],
        ]
    )
    await safe_answer_callback(callback, "صندوقچه باز شد! 🎉")
    await safe_edit_callback_message(callback, bot, _opened_text(result), reply_markup=kb)
