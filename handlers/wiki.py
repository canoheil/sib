"""Wiki & collection handlers (Phase 12).

«ویکی شخصیت‌ها» — browse all released characters.
«کارت‌های من» — the player's own cards.
"""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.database.connection import get_session
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.wiki import CollectionService, WikiService

router = Router(name="wiki")

WIKI_PAGE_SIZE = 6


def _wiki_keyboard(characters, page: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    start = page * WIKI_PAGE_SIZE
    for index, character in enumerate(characters[start : start + WIKI_PAGE_SIZE]):
        row.append(
            InlineKeyboardButton(
                text=f"🍎 {character.name_fa}",
                callback_data=f"wiki_char:{character.id}",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="▶️ قبلی", callback_data=f"wiki_page:{page - 1}"))
    if start + WIKI_PAGE_SIZE < len(characters):
        nav.append(InlineKeyboardButton(text="◀️ بعدی", callback_data=f"wiki_page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data=f"wiki_page:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _wiki_text_and_kb(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        characters = await WikiService.list_wiki(session)
    if not characters:
        return (
            "📚 **ویکی شخصیت‌ها**\n\nهنوز شخصیتی منتشر نشده است.",
            InlineKeyboardMarkup(inline_keyboard=[]),
        )
    page = max(0, min(page, (len(characters) - 1) // WIKI_PAGE_SIZE))
    text = (
        f"📚 **ویکی شخصیت‌ها** ({len(characters)} شخصیت)\n\n"
        "همه شخصیت‌های منتشرشده را اینجا ببینید؛ توانایی‌ها و ویژگی‌ها کاملاً شفاف."
    )
    return text, _wiki_keyboard(characters, page)


@router.message(Command("wiki"))
async def cmd_wiki(message: Message, bot: Bot):
    text, kb = await _wiki_text_and_kb(0)
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("wiki_page:"))
async def handle_wiki_page(callback: CallbackQuery, bot: Bot):
    page = int(callback.data.split(":")[1])
    text, kb = await _wiki_text_and_kb(page)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("wiki_char:"))
async def handle_wiki_char(callback: CallbackQuery, bot: Bot):
    character_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        page_text = await WikiService.wiki_page(session, character_id)
    if not page_text:
        await safe_answer_callback(callback, "این شخصیت در دسترس نیست.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 بازگشت به ویکی", callback_data="wiki_page:0")]
        ]
    )
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, page_text, reply_markup=kb)


@router.message(Command("cards"))
async def cmd_cards(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return
    async with get_session() as session:
        text = await CollectionService.render_collection(session, user.id)
    await message.reply(text, parse_mode="Markdown")
