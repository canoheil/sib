"""Apple Mine (معدن سیبی) Telegram screens and callbacks."""

from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Character
from sib.keyboards.common import primary_button
from sib.services.apple_mine import (
    CooldownError,
    InvalidCardError,
    perform_shift,
    remaining_cooldown,
)
from sib.services.apple_mine_narrative import cooldown_message
from sib.services.group import GroupService
from sib.services.items import item_emoji, item_label
from sib.services.player_cards import PlayerCardService
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import (
    APPLE_MINE_WORK_BUTTON_SUPPORTS_STYLE,
    safe_answer_callback,
    safe_edit_callback_message,
    safe_edit_message_caption,
    work_button_kb,
)
from sib.services.user import UserService

router = Router(name="apple_mine")

MINE_COMMAND_PATTERN = r"^\s*معدن\s+سیبی\s*$"
MINE_IMAGE_PATH = Path(__file__).resolve().parents[1] / "assets" / "apple_mine" / "mine.jpg"

PANEL_CAPTION = (
    "داخل معدن تاریک است. صدای چکش از دور می‌آید. برای یافتن سیب‌های پنهان، "
    "یکی از کارت‌هایت را انتخاب کن."
)
CARD_PICK_CAPTION = "یکی از کارت‌هایت را برای کار در معدن انتخاب کن."
NO_CARDS_TEXT = "هنوز کارتی نداری. از فروشگاه یک کارت بگیر و دوباره به معدن بیا."
MINE_FAILURE_TEXT = "معدن اکنون در دسترس نیست. کمی بعد دوباره تلاش کن."
CARD_NOT_OWNED_TEXT = "این کارت برای شما نیست یا دیگر در دسترس نیست."
CARD_LIST_FAILURE_TEXT = "نمایش کارت‌ها ممکن نشد. دوباره تلاش کن."
_EMPTY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])


def _group_id_for_chat(chat) -> int | None:
    chat_type = getattr(chat, "type", None)
    chat_type = getattr(chat_type, "value", chat_type)
    if chat_type in ("group", "supergroup"):
        return getattr(chat, "id", None)
    return None


async def _register_user_and_group(session, user, chat) -> int | None:
    await UserService.get_or_create_user(session, user)
    group_id = _group_id_for_chat(chat)
    if group_id is not None:
        group = await GroupService.get_or_create_group(session, chat)
        return group.id
    return None


async def _safe_message_reply(message: Message, text: str, *, reply_markup=None) -> None:
    try:
        await message.answer(text, reply_markup=reply_markup, parse_mode=None)
    except Exception:
        logger.exception("Apple Mine could not send a message reply")


async def _send_panel(message: Message) -> None:
    user = message.from_user
    if user is None:
        await _safe_message_reply(message, MINE_FAILURE_TEXT)
        return

    keyboard = work_button_kb(
        supports_style=APPLE_MINE_WORK_BUTTON_SUPPORTS_STYLE
    )
    try:
        async with get_session() as session:
            await _register_user_and_group(session, user, message.chat)
    except Exception:
        logger.exception("Apple Mine panel registration failed for user=%s", user.id)
        await _safe_message_reply(message, MINE_FAILURE_TEXT)
        return

    try:
        await message.answer_photo(
            photo=FSInputFile(str(MINE_IMAGE_PATH)),
            caption=PANEL_CAPTION,
            reply_markup=keyboard,
            parse_mode=None,
        )
    except Exception:
        # Keep the feature usable if a local asset cannot be read or Telegram
        # rejects the photo, while never exposing an exception to the group.
        logger.exception("Apple Mine image panel failed; using a text panel")
        await _safe_message_reply(message, PANEL_CAPTION, reply_markup=keyboard)


@router.message(Command("mine"))
@router.message(F.text.regexp(MINE_COMMAND_PATTERN))
async def handle_mine_command(message: Message) -> None:
    """Open the mine panel from «معدن سیبی» or the /mine alias."""
    try:
        await _send_panel(message)
    except Exception:
        logger.exception("Apple Mine command failed")
        await _safe_message_reply(message, MINE_FAILURE_TEXT)


async def _card_keyboard(session, cards) -> InlineKeyboardMarkup:
    character_ids = sorted({card.character_id for card in cards})
    characters = {}
    if character_ids:
        rows = await session.execute(
            select(Character.id, Character.name_fa).where(Character.id.in_(character_ids))
        )
        characters = {character_id: name for character_id, name in rows.all()}

    buttons = []
    for card in cards:
        name = characters.get(card.character_id, "کارت")
        # Telegram button labels are short; card identity and ownership are
        # revalidated by the service, never inferred from this display text.
        if len(name) > 42:
            name = f"{name[:41]}…"
        label = f"🎴 {name} · سطح {fa_digits(card.level)}"
        # Every card is a playable choice in this panel: blue glass.
        buttons.append(
            [
                primary_button(
                    text=label,
                    callback_data=f"mine:card:{card.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _edit_panel(
    callback: CallbackQuery,
    bot: Bot,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> bool:
    message = callback.message
    if message is None:
        return False

    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    if not chat_id or not message_id:
        return False

    if getattr(message, "photo", None):
        return await safe_edit_message_caption(
            bot,
            chat_id,
            message_id,
            caption,
            reply_markup=reply_markup,
            parse_mode=None,
        )
    return await safe_edit_callback_message(
        callback,
        bot,
        caption,
        reply_markup=reply_markup,
        parse_mode=None,
    )


async def _send_callback_message(callback: CallbackQuery, bot: Bot, text: str) -> None:
    message = callback.message
    try:
        if message is not None and hasattr(message, "answer"):
            await message.answer(text, parse_mode=None)
        else:
            await bot.send_message(callback.from_user.id, text, parse_mode=None)
    except Exception:
        logger.exception("Apple Mine could not send a callback message")


@router.callback_query(F.data == "mine:work")
async def handle_mine_work(callback: CallbackQuery, bot: Bot) -> None:
    """Show the presser's own cards, or alert with their global cooldown."""
    user = callback.from_user
    if user is None:
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        async with get_session() as session:
            await _register_user_and_group(
                session,
                user,
                getattr(callback.message, "chat", None),
            )
            cooldown = await remaining_cooldown(session, user.id)
            cards = []
            keyboard = None
            if cooldown == 0:
                cards = await PlayerCardService.list_cards(session, user.id)
                if cards:
                    keyboard = await _card_keyboard(session, cards)
    except Exception:
        logger.exception("Apple Mine card list failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        if cooldown > 0:
            await safe_answer_callback(
                callback,
                cooldown_message(cooldown, user.id),
                show_alert=True,
            )
            return

        if not cards:
            await safe_answer_callback(callback)
            await _send_callback_message(callback, bot, NO_CARDS_TEXT)
            return

        await safe_answer_callback(callback)
        if not await _edit_panel(callback, bot, CARD_PICK_CAPTION, keyboard):
            await _send_callback_message(callback, bot, CARD_LIST_FAILURE_TEXT)
    except Exception:
        logger.exception("Apple Mine work callback failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)


def _result_caption(result) -> str:
    if result.item_code == "apple_normal":
        emoji, display_name = "🍎", "سیب معمولی"
    else:
        emoji = item_emoji(result.item_code)
        display_name = item_label(result.item_code)
    return (
        f"{result.narrative}\n\n"
        f"{emoji} +{fa_digits(result.amount)} {display_name}"
    )


@router.callback_query(F.data.startswith("mine:card:"))
async def handle_mine_card(callback: CallbackQuery, bot: Bot) -> None:
    """Perform the shift for one owned card and edit the same panel caption."""
    user = callback.from_user
    if user is None:
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        card_id = int((callback.data or "").split(":", 2)[2])
        if card_id <= 0:
            raise ValueError("card id must be positive")
    except (IndexError, ValueError):
        await safe_answer_callback(callback, CARD_NOT_OWNED_TEXT, show_alert=True)
        return

    try:
        # The opening command registered the user and group. Do not perform a
        # normal database read before perform_shift's user-row lock: under
        # MariaDB REPEATABLE READ that could pin a pre-lock snapshot and let a
        # second concurrent tap miss the first committed shift.
        group_id = _group_id_for_chat(
            getattr(callback.message, "chat", None)
        )
        async with get_session() as session:
            result = await perform_shift(
                session,
                user_id=user.id,
                group_id=group_id,
                card_id=card_id,
            )
    except CooldownError as error:
        await safe_answer_callback(
            callback,
            cooldown_message(error.remaining_seconds, user.id),
            show_alert=True,
        )
        return
    except InvalidCardError:
        await safe_answer_callback(callback, CARD_NOT_OWNED_TEXT, show_alert=True)
        return
    except Exception:
        logger.exception("Apple Mine shift failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        caption = _result_caption(result)
        await safe_answer_callback(callback)
        if not await _edit_panel(callback, bot, caption, _EMPTY_KEYBOARD):
            await _send_callback_message(callback, bot, caption)
    except Exception:
        logger.exception("Apple Mine result rendering failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
