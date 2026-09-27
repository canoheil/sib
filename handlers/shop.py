"""Weekly shop handlers (Phase 11 UI) + card upgrade panel (Phase 10 UI)."""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.database.connection import get_session
from sib.services.apple_wallet import AppleWalletService
from sib.services.player_cards import CardError, PlayerCardService
from sib.services.shop import ShopError, ShopService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message

router = Router(name="shop")


async def _shop_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        entries = await ShopService.list_entries(session, user_id=user_id)
        balance = await AppleWalletService.get_balance(session, user_id)

    lines = [
        "🏪 **فروشگاه هفتگی سیب** 🍏",
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
                [
                    InlineKeyboardButton(
                        text=f"✅ {character.name_fa} (خریداری‌شده)",
                        callback_data=f"shop_owned:{entry.id}",
                    )
                ]
            )
        else:
            lines.append(f"🍎 {character.name_fa} — {price} سیب{badge}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🛒 خرید {character.name_fa} ({price} 🍎){badge}",
                        callback_data=f"shop_buy:{entry.id}",
                    )
                ]
            )

    if not entries:
        lines.append("این هفته کالایی در فروشگاه نیست.")
    buttons.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="shop_refresh")])
    buttons.append([InlineKeyboardButton(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("shop"))
async def cmd_shop(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return
    text, kb = await _shop_view(user.id)
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "shop_refresh")
async def handle_shop_refresh(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    text, kb = await _shop_view(user.id)
    await safe_answer_callback(callback, "بروزرسانی شد 🍏")
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

    text, kb = await _shop_view(user.id)
    await safe_answer_callback(callback, "خرید انجام شد! کارت به مجموعه شما اضافه شد 🎉")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Card upgrade panel (Phase 10 UI)
# ---------------------------------------------------------------------------


def _allocation_keyboard(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💪 +10 قدرت", callback_data=f"card_up:{card_id}:p"),
                InlineKeyboardButton(text="⚡ +10 سرعت", callback_data=f"card_up:{card_id}:s"),
                InlineKeyboardButton(text="🧠 +10 هوش", callback_data=f"card_up:{card_id}:i"),
            ],
            [
                InlineKeyboardButton(text="💪 +20 / ⚡ +20 / 🧠 +20", callback_data=f"card_up:{card_id}:b"),
            ],
            [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data=f"card_panel:{card_id}")],
            [InlineKeyboardButton(text="↩️ بازگشت به کارت‌های من", callback_data="apple_cards")],
        ]
    )


async def _card_panel_view(user_id: int, card_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        from sib.services.wiki import CollectionService

        detail = await CollectionService.render_card_detail(session, user_id, card_id)
        cost = await PlayerCardService.upgrade_cost(session, 1)
        card = await PlayerCardService.get_card(session, card_id)
        if card:
            cost = await PlayerCardService.upgrade_cost(session, card.level)
        balance = await AppleWalletService.get_balance(session, user_id)

    if not detail:
        return "کارت پیدا نشد.", InlineKeyboardMarkup(inline_keyboard=[])
    text = (
        f"{detail}\n\n"
        f"💰 هزینه ارتقای بعدی: `{cost}` سیب 🍎 (موجودی: `{balance}`)\n"
        "برای ارتقا، توزیع امتیازهای جدید را انتخاب کنید (بودجه جدید به‌طور مساوی پیشنهاد شده):"
    )
    return text, _allocation_keyboard(card_id)


@router.callback_query(F.data.startswith("card_panel:"))
async def handle_card_panel(callback: CallbackQuery, bot: Bot):
    card_id = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return
    text, kb = await _card_panel_view(user.id, card_id)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)

    from sib.services.panel import PanelOwnershipService

    chat = getattr(callback.message, "chat", None)
    msg_id = getattr(callback.message, "message_id", None)
    if chat is not None and msg_id:
        async with get_session() as session:
            await PanelOwnershipService.claim(
                session, chat.id, msg_id, user.id, panel_type="card_panel"
            )
            await session.commit()


@router.callback_query(F.data.startswith("card_up:"))
async def handle_card_upgrade(callback: CallbackQuery, bot: Bot):
    _, _, card_id_s, plan = callback.data.split(":")
    card_id = int(card_id_s)
    user = callback.from_user
    if not user:
        return

    # Simple suggested allocations: the gained points split per plan.
    async with get_session() as session:
        card = await PlayerCardService.get_card(session, card_id)
        if not card:
            await safe_answer_callback(callback, "کارت پیدا نشد.", show_alert=True)
            return
        current_budget = await PlayerCardService.level_budget(session, card.level)
        new_budget = await PlayerCardService.level_budget(session, card.level + 1)
        gained = new_budget - current_budget

        half = gained // 2
        boosts = {
            "p": (gained, 0, 0),
            "s": (0, gained, 0),
            "i": (0, 0, gained),
            "b": (half, half, gained - 2 * half),
        }[plan]

        try:
            await PlayerCardService.upgrade(
                session,
                user.id,
                card_id,
                boost_power=boosts[0],
                boost_speed=boosts[1],
                boost_intelligence=boosts[2],
            )
        except CardError as e:
            await safe_answer_callback(callback, str(e), show_alert=True)
            return

    text, kb = await _card_panel_view(user.id, card_id)
    await safe_answer_callback(callback, "کارت ارتقا یافت! 🎉")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
