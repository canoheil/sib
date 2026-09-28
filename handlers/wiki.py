"""Wiki & collection handlers (Phase 12).

«ویکی شخصیت‌ها» — browse all released characters.
«کارت‌های من» — the player's own cards.
"""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.database.connection import get_session
from sib.services.telegram_ui import (
    CAPTION_LIMIT, caption_length, safe_answer_callback, safe_edit_callback_message,
)
from sib.services.nav import back_label_for, back_target, parse_callback
from sib.services.wiki import CollectionService, WikiService
from sib.database.models import Character
from sib.services.character_art import photo_arg, resolve_character_photo

router = Router(name="wiki")

WIKI_PAGE_SIZE = 6


def _wiki_keyboard(characters, page: int, origin: str = None) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    start = page * WIKI_PAGE_SIZE
    for index, character in enumerate(characters[start : start + WIKI_PAGE_SIZE]):
        row.append(
            InlineKeyboardButton(
                text=f"🍎 {character.name_fa}",
                callback_data=f"wiki_char:{character.id}" + (f":{origin}" if origin else ""),
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="▶️ قبلی",
                callback_data=f"wiki_page:{page - 1}" + (f":{origin}" if origin else ""),
            )
        )
    if start + WIKI_PAGE_SIZE < len(characters):
        nav.append(
            InlineKeyboardButton(
                text="◀️ بعدی",
                callback_data=f"wiki_page:{page + 1}" + (f":{origin}" if origin else ""),
            )
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 بروزرسانی",
                callback_data=f"wiki_page:{page}" + (f":{origin}" if origin else ""),
            )
        ]
    )
    # Shared navigation: Back goes to the screen this wiki was opened from.
    buttons.append(
        [InlineKeyboardButton(text=back_label_for(origin), callback_data=back_target(origin))]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _wiki_text_and_kb(
    page: int = 0, origin: str = None
) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        characters = await WikiService.list_wiki(session)
    if not characters:
        return (
            "📚 **ویکی شخصیت‌ها**\n\nهنوز شخصیتی منتشر نشده است.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=back_label_for(origin),
                            callback_data=back_target(origin),
                        )
                    ]
                ]
            ),
        )
    page = max(0, min(page, (len(characters) - 1) // WIKI_PAGE_SIZE))
    text = (
        f"📚 **ویکی شخصیت‌ها** ({len(characters)} شخصیت)\n\n"
        "همه شخصیت‌های منتشرشده را اینجا ببینید؛ توانایی‌ها و ویژگی‌ها کاملاً شفاف."
    )
    return text, _wiki_keyboard(characters, page, origin)


@router.message(Command("wiki"))
async def cmd_wiki(message: Message, bot: Bot):
    text, kb = await _wiki_text_and_kb(0)
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data.startswith("apple_wiki"))
async def handle_apple_wiki(callback: CallbackQuery, bot: Bot):
    _base, origin = parse_callback(callback.data)
    text, kb = await _wiki_text_and_kb(0, origin)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback,
        bot,
        text,
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("wiki_page:"))
async def handle_wiki_page(callback: CallbackQuery, bot: Bot):
    base, origin = parse_callback(callback.data)
    page = int(base.split(":")[1])
    text, kb = await _wiki_text_and_kb(page, origin)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("wiki_char:"))
async def handle_wiki_char(callback: CallbackQuery, bot: Bot):
    base, origin = parse_callback(callback.data)
    character_id = int(base.split(":")[1])
    async with get_session() as session:
        page_text = await WikiService.wiki_page(session, character_id)
        art_photo = None
        characters = []
        if page_text:
            character = await session.get(Character, character_id)
            file_id, fs_path = await resolve_character_photo(session, character)
            art_photo = photo_arg(file_id, fs_path)
            characters = await WikiService.list_wiki(session)
    if not page_text:
        await safe_answer_callback(callback, "این شخصیت در دسترس نیست.", show_alert=True)
        return

    # Move between actual card screens directly: returning to the text-only
    # index and selecting another card would force PHOTO -> TEXT -> PHOTO.
    index = next((i for i, char in enumerate(characters) if char.id == character_id), None)
    if index is None:
        await safe_answer_callback(callback, "این شخصیت در دسترس نیست.", show_alert=True)
        return
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(
            text="▶️ قبلی",
            callback_data=f"wiki_char:{characters[index - 1].id}" + (f":{origin}" if origin else ""),
        ))
    if index + 1 < len(characters):
        nav.append(InlineKeyboardButton(
            text="◀️ بعدی",
            callback_data=f"wiki_char:{characters[index + 1].id}" + (f":{origin}" if origin else ""),
        ))
    rows = [nav] if nav else []
    if art_photo is not None and caption_length(page_text) > CAPTION_LIMIT:
        # Telegram cannot display the entire lore in a photo caption. Keep
        # the card in place for navigation, but don't make its full text
        # inaccessible behind the shortened preview.
        rows.append([InlineKeyboardButton(
            text="📖 متن کامل کارت",
            callback_data=f"wiki_char_full:{character_id}" + (f":{origin}" if origin else ""),
        )])
    rows.append([
        InlineKeyboardButton(
            text="📚 بازگشت به ویکی",
            callback_data=f"wiki_page:{index // WIKI_PAGE_SIZE}" + (f":{origin}" if origin else ""),
        )
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, page_text, reply_markup=kb, photo=art_photo)


@router.callback_query(F.data.startswith("wiki_char_full:"))
async def handle_wiki_char_full(callback: CallbackQuery, bot: Bot):
    """Opt in to the unabridged lore when it will not fit in a photo."""
    base, origin = parse_callback(callback.data)
    character_id = int(base.split(":")[1])
    async with get_session() as session:
        text = await WikiService.wiki_page(session, character_id)
    if text is None:
        await safe_answer_callback(callback, "این شخصیت در دسترس نیست.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="↩️ بازگشت به کارت",
            callback_data=f"wiki_char:{character_id}" + (f":{origin}" if origin else ""),
        )
    ]])
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.message(Command("cards"))
async def cmd_cards(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return
    async with get_session() as session:
        text = await CollectionService.render_collection(session, user.id)
    await message.reply(text, parse_mode="Markdown")
