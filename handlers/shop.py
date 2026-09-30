"""Shop handlers: three sections (Cards / Items / Sell Items).

Store structure (spec):
    Shop
    ├── Cards        — weekly rotating character cards (Apples)
    ├── Items        — generic items, starting with Magic Powder (Apples)
    └── Sell Items   — sell eligible inventory items for normal Apples

All economic values are config-driven (item_buy_prices /
item_sell_prices / magic_powder_level_costs / garden_plot_prices), so
rebalancing never touches this file.
"""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.database.connection import get_session
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import (
    ITEM_CATALOG,
    ItemService,
    item_description,
    item_emoji,
    item_label,
)
from sib.services.shop import ShopError, ShopService
from sib.keyboards.common import (
    danger_button,
    primary_button,
    success_button,
)
from sib.services.telegram_ui import (
    CAPTION_LIMIT, caption_length, safe_answer_callback, safe_edit_callback_message,
)

router = Router(name="shop")


# ---------------------------------------------------------------------------
# Shop root: the three sections
# ---------------------------------------------------------------------------


async def _shop_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Shop ROOT screen: Cards / Items / Sell Items."""
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
            [primary_button(text="💰 فروش آیتم‌ها", callback_data="shop_sell")],
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

    text, kb = await _shop_view(user.id)
    await send_screen_reply(message, "shop", text, kb)


@router.callback_query(F.data == "shop_refresh")
async def handle_shop_refresh(callback: CallbackQuery, bot: Bot):
    """Re-render the shop ROOT (also the Back target of every section)."""
    from sib.services.banner import render_banner

    user = callback.from_user
    if not user:
        return
    text, kb = await _shop_view(user.id)
    await safe_answer_callback(callback, "بروزرسانی شد 🍏")
    if callback.message:
        await render_banner(callback, bot, "shop", text, kb)


# ---------------------------------------------------------------------------
# Section 1: Cards (weekly rotation)
# ---------------------------------------------------------------------------


async def _cards_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        entries = await ShopService.list_entries(session, user_id=user_id)
        balance = await AppleWalletService.get_balance(session, user_id)

    lines = [
        "🏪 **فروشگاه — کارت‌ها** 🎴",
        f"💰 سیب‌های شما: `{balance}`",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for entry, character, owned in entries:
        price = ShopService.entry_price(entry, character)
        badge = f" {entry.badge}" if entry.badge else ""
        if owned:
            lines.append(f"✅ {character.name_fa} — (دارید)")
            buttons.append(
                [primary_button(text=f"✅ {character.name_fa} (خریداری‌شده)", callback_data=f"shop_owned:{entry.id}")]
            )
        else:
            lines.append(f"🍎 {character.name_fa} — {price} سیب{badge}")
            buttons.append(
                [primary_button(text=f"🛒 خرید {character.name_fa} ({price} 🍎){badge}", callback_data=f"shop_item:{entry.id}")]
            )

    if not entries:
        lines.append("این هفته کالایی در فروشگاه نیست.")
    buttons.append([primary_button(text="🔄 بروزرسانی", callback_data="shop_cards")])
    buttons.append([danger_button(text="↩️ بازگشت به فروشگاه", callback_data="shop_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "shop_cards")
async def handle_shop_cards(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    text, kb = await _cards_view(user.id)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


async def _shop_item_view(user_id: int, entry_id: int):
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
            inline_keyboard=[[danger_button(text="↩️ بازگشت به فروشگاه", callback_data="shop_refresh")]]
        )
        return text, kb, None

    price = ShopService.entry_price(entry, character)
    badge = f" {entry.badge}" if entry.badge else ""
    lines = [
        render_character_card(character, faction=faction),
        "",
        f"💰 سیب‌های شما: `{balance}`{badge}",
    ]
    if owned:
        action = [primary_button(text="✅ خریداری‌شده", callback_data=f"shop_owned:{entry.id}")]
    else:
        action = [
            success_button(
                text=f"✅ تأیید خرید ({price} 🍎)",
                callback_data=f"shop_buy:{entry.id}",
            )
        ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            action,
            [danger_button(text="↩️ بازگشت به فروشگاه", callback_data="shop_refresh")],
        ]
    )
    return "\n".join(lines), kb, art_photo


@router.callback_query(F.data.startswith("shop_item:"))
async def handle_shop_item(callback: CallbackQuery, bot: Bot):
    entry_id = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return
    text, kb, art_photo = await _shop_item_view(user.id, entry_id)
    if art_photo is not None and caption_length(text) > CAPTION_LIMIT:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [primary_button(
                text="📖 متن کامل کارت", callback_data=f"shop_item_full:{entry_id}",
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
    entry_id = int(callback.data.split(":")[1])
    text, kb, art_photo = await _shop_item_view(user.id, entry_id)
    if art_photo is not None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [primary_button(
                text="↩️ بازگشت به کارت", callback_data=f"shop_item:{entry_id}",
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
    entry_id = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        try:
            await ShopService.purchase(session, user.id, entry_id)
        except ShopError as e:
            await safe_answer_callback(callback, str(e), show_alert=True)
            return

    from sib.services.banner import render_banner

    text, kb = await _shop_view(user.id)
    await safe_answer_callback(callback, "خرید انجام شد! کارت به مجموعه شما اضافه شد 🎉")
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
        buy_prices = await ConfigService.get(session, "item_buy_prices") or {}
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
    text, kb = await _goods_view(user.id)
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
            inline_keyboard=[[danger_button(text="↩️ بازگشت به آیتم‌ها", callback_data="shop_goods")]]
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
            [success_button(
                text=f"✅ تأیید خرید ({price} 🍎)", callback_data=f"shop_good_buy:{item_key}",
            )],
            [primary_button(text="🔄 بروزرسانی", callback_data=f"shop_good:{item_key}")],
            [danger_button(text="↩️ بازگشت به آیتم‌ها", callback_data="shop_goods")],
        ]
    )
    return "\n".join(lines), kb, art_photo


@router.callback_query(F.data.startswith("shop_good:"))
async def handle_shop_good(callback: CallbackQuery, bot: Bot):
    item_key = callback.data.split(":", 1)[1]
    user = callback.from_user
    if not user:
        return
    text, kb, art_photo = await _good_view(user.id, item_key)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=art_photo)


@router.callback_query(F.data.startswith("shop_good_buy:"))
async def handle_shop_good_buy(callback: CallbackQuery, bot: Bot):
    item_key = callback.data.split(":", 1)[1]
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        try:
            total = await ItemService.buy(session, user.id, item_key, 1)
        except ValueError as e:
            await safe_answer_callback(callback, str(e), show_alert=True)
            return

    await safe_answer_callback(
        callback,
        f"خرید انجام شد! {item_emoji(item_key)} {item_label(item_key)} خریدی (موجودی: {total}) 🎉",
    )
    text, kb = await _goods_view(user.id)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Section 3: Sell Items (special Apples and future sellables)
# ---------------------------------------------------------------------------


async def _sell_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        balance = await AppleWalletService.get_balance(session, user_id)
        sell_prices = await ConfigService.get(session, "item_sell_prices") or {}
        owned = await ItemService.list_items(session, user_id)

    lines = [
        "💰 **فروشگاه — فروش آیتم‌ها**",
        f"💰 سیب‌های شما: `{balance}`",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for key in ITEM_CATALOG:
        price = sell_prices.get(key)
        if price is None or owned.get(key, 0) <= 0:
            continue
        price = int(price)
        have = owned[key]
        lines.append(f"{item_emoji(key)} {item_label(key)} ×{have} — هر عدد `{price}` سیب")
        row = [
            success_button(
                text=f"💸 فروش یکی ({price} 🍎)",
                callback_data=f"shop_sell_one:{key}",
            )
        ]
        if have > 1:
            row.append(
                success_button(
                    text=f"💸 فروش همه ({have * price} 🍎)",
                    callback_data=f"shop_sell_all:{key}",
                )
            )
        buttons.append(row)

    if not buttons:
        lines.append(
            "آیتم قابل‌فروشی نداری!\n"
            "سیب‌های ویژه 🥇💎🖤 را از برداشت درخت‌های باغ به دست بیاور."
        )
    buttons.append([primary_button(text="🔄 بروزرسانی", callback_data="shop_sell")])
    buttons.append([danger_button(text="↩️ بازگشت به فروشگاه", callback_data="shop_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "shop_sell")
async def handle_shop_sell(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    text, kb = await _sell_view(user.id)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


async def _sell_items(callback: CallbackQuery, bot: Bot, item_key: str, quantity: int) -> None:
    user = callback.from_user
    if not user:
        return
    async with get_session() as session:
        try:
            apples = await ItemService.sell(session, user.id, item_key, quantity)
        except ValueError as e:
            await safe_answer_callback(callback, str(e), show_alert=True)
            return

    await safe_answer_callback(
        callback, f"فروش انجام شد! `{apples}` سیب دریافت کردی 🍎"
    )
    text, kb = await _sell_view(user.id)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("shop_sell_one:"))
async def handle_shop_sell_one(callback: CallbackQuery, bot: Bot):
    item_key = callback.data.split(":", 1)[1]
    await _sell_items(callback, bot, item_key, 1)


@router.callback_query(F.data.startswith("shop_sell_all:"))
async def handle_shop_sell_all(callback: CallbackQuery, bot: Bot):
    item_key = callback.data.split(":", 1)[1]
    user = callback.from_user
    if not user:
        return
    async with get_session() as session:
        have = await ItemService.get_total(session, user.id, item_key)
    if have <= 0:
        await safe_answer_callback(callback, "از این آیتم نداری!", show_alert=True)
        return
    await _sell_items(callback, bot, item_key, have)
