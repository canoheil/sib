"""Telegram UX for the pet system: the «سیب شکار» encounter and naming.

All rules live in :mod:`sib.services.pet_service`; this module only decides
what the player sees.

Flow
----
1. ``/hunt`` or the plain-text aliases «سیب شکار» / «شکار سیبی» open an
   *encounter*: the service validates the cooldown and the pet caps and rolls
   which wild pet the player meets. The adult artwork is sent with the
   player's card power, the resulting chance and a single "🏹 شکار!" button.
2. The button's callback resolves the hunt (:func:`pet_service.execute_hunt`)
   and edits the same message: the escaping-pet artwork on a miss, or the
   baby's artwork on success plus the naming prompt.
3. ``PetStates.waiting_for_name`` then captures the next text message as the
   pet's name; an invalid name keeps the state so the player can retry.
4. «سیب پت» (``/mypets``) lists the player's pets as buttons; each one opens
   a profile with its artwork, XP progress and a «🍖 غذا دادن» button that
   spends one ``pet_food`` from the inventory and eventually evolves the pet.

Only the player who opened the encounter may press its button, and the roll
uses the server-side chance rather than the number echoed in the callback
data (see :func:`handle_hunt_callback`).
"""

import html
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sib.core.logging import logger
from sib.core.text import format_html_caption
from sib.database.connection import get_session
from sib.database.models import PET_TYPES, Character
from sib.services import pet_service
from sib.services.card_progression import card_attributes
from sib.services.player_cards import PlayerCardService
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import (
    safe_answer_callback,
    safe_edit_message_caption,
    safe_edit_message_media,
)

router = Router(name="pets")

ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "pets"
FOREST_IMAGE = ASSET_DIR / "hunt_forest.jpg"
FAIL_IMAGE = ASSET_DIR / "hunt_fail.jpg"

#: Power used when the player owns no card at all (mirrors Apple Battle).
BASE_UNARMED_POWER = 10

#: Persian (U+06CC) and Arabic (U+064A) Yeh.
_PERSIAN_YEH = "\u06cc"
_ARABIC_YEH = "\u064a"
#: The two plain-text aliases. Each is registered with both Yeh spellings too,
#: following the wake-word convention of handlers/theft.py and
#: handlers/disease.py (players' keyboards produce either letter).
_ALIAS_BASES = ("سیب شکار", "شکار سیبی")
HUNT_ALIASES = frozenset(
    base.replace(_PERSIAN_YEH, yeh)
    for base in _ALIAS_BASES
    for yeh in (_PERSIAN_YEH, _ARABIC_YEH)
)

#: Callback payload of the hunt button: ``pet_hunt:{user_id}:{pet_type}:{chance}``.
HUNT_CALLBACK_PREFIX = "pet_hunt:"
PET_VIEW_PREFIX = "pet_view:"
PET_FEED_PREFIX = "pet_feed:"

#: «سیب پت» / «پت من» / «پت», each in both Yeh spellings. These are matched
#: before the naming state, so they always open the menu instead of being
#: stored as a pet name (the same trade-off the hunt aliases already make).
_MENU_ALIAS_BASES = ("سیب پت", "پت من", "پت")
PET_MENU_ALIASES = frozenset(
    base.replace(_PERSIAN_YEH, yeh)
    for base in _MENU_ALIAS_BASES
    for yeh in (_PERSIAN_YEH, _ARABIC_YEH)
)

GENERIC_ERROR = "خطایی رخ داد. لطفاً دوباره تلاش کن."
COOLDOWN_TEXT = "⏳ برای شکار بعدی باید {remaining} صبر کنید."
LIMIT_TOTAL_TEXT = (
    "🐾 شما {max_total} پت دارید و ظرفیت شما پر است. "
    "اول یکی از پت‌ها را بزرگ کنید."
)
LIMIT_BABY_TEXT = (
    "🐾 شما یک بچه پت دارید که هنوز بزرگ نشده. "
    "اول اسمش را انتخاب کنید، بعد دوباره شکار کنید."
)
#: The encounter screen. ``**`` renders as bold through ``format_html_caption``.
ENCOUNTER_CAPTION = (
    "🌲 وارد جنگل جادویی شدی!\n"
    "یک **{type_fa} وحشی** دیدی!\n"
    "💪 قدرت کارتت: {power}\n"
    "🎯 شانس شکار: {chance}٪"
)
HUNT_BUTTON_TEXT = "🏹 شکار!"
FAILED_CAPTION = "💨 ای وای! {type_fa} فرار کرد. دفعه بعد شانس بهتری داری."
SUCCESS_CAPTION = (
    "🎉 شکار موفق! تونستی بچه {type_fa} رو بگیری.\n"
    "حالا یک اسم براش انتخاب کن و روی همین پیام ریپلای کن:"
)
NOT_YOUR_HUNT_ALERT = "این شکار مال شما نیست! خودت «سیب شکار» بزن."
HUNT_GONE_ALERT = "این شکار منقضی شده. دوباره «سیب شکار» بزن."

NAMED_TEXT = (
    "✅ اسم {name} برای پت شما ثبت شد! "
    "از این به بعد با دستور «سیب پت» می‌تونید مدیریتش کنید."
)
NO_PET_TEXT = "🐾 پت موردنظر پیدا نشد. دوباره «سیب شکار» را امتحان کنید."

NO_PETS_TEXT = "شما هنوز هیچ پتی ندارید. با دستور «سیب شکار» برید شکار!"
PET_MENU_TITLE = "🐾 **پت‌های شما**\nبرای دیدن پروفایل هر پت روی اسمش بزن:"
PROFILE_CAPTION = (
    "{emoji} **{name}**\n"
    "🧬 نوع: {type_fa}\n"
    "{status_line}\n"
    "📊 XP: {xp}/{required} ({percent}٪)\n"
    "⭐ اسکیل: {skill}"
)
STATUS_BABY = "🍼 وضعیت: بچه"
STATUS_ADULT = "👑 وضعیت: بالغ"
FEED_BUTTON_TEXT = "🍖 غذا دادن"
FEED_NO_FOOD_ALERT = "شما غذای پت ندارید! از فروشگاه بخرید."
FEED_TOO_SOON_ALERT = "پت شما سیر است! ۱ ساعت دیگر دوباره تلاش کنید."
FEED_ALREADY_ADULT_ALERT = "این پت بزرگ شده و دیگه غذا لازم نداره."
PET_GONE_ALERT = "این پت پیدا نشد."
FEED_DONE_CAPTION = "🍖 **{name}** غذا خورد! +{xp_added} تجربه"
FEED_EVOLVED_CAPTION = (
    "🎉 تبریک! پت شما بالغ شد و اسکیلش فعال شد!\n"
    "👑 **{name}** حالا یک {type_fa} بالغه."
)

NAME_ERRORS = {
    "empty": "❗️ اسم نمی‌تواند خالی باشد.",
    "too_short": "❗️ اسم باید حداقل ۲ حرف باشد.",
    "too_long": "❗️ اسم باید حداکثر ۲۰ حرف باشد.",
    "invalid_chars": "❗️ اسم فقط می‌تواند از حروف، عدد و فاصله تشکیل شود.",
    "digits_only": "❗️ اسم نمی‌تواند فقط عدد باشد.",
    "not_found": NO_PET_TEXT,
}


class PetStates(StatesGroup):
    """FSM state of the naming step."""

    waiting_for_name = State()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_remaining(seconds: int) -> str:
    """Persian ``۲ ساعت و ۱۵ دقیقه`` style duration for the cooldown reply."""
    remaining = max(0, int(seconds))
    hours, rest = divmod(remaining, 3600)
    minutes = rest // 60
    if hours and minutes:
        return fa_digits(f"{hours} ساعت و {minutes} دقیقه")
    if hours:
        return fa_digits(f"{hours} ساعت")
    if minutes:
        return fa_digits(f"{minutes} دقیقه")
    return fa_digits(f"{max(1, rest)} ثانیه")


def _is_bot(sender) -> bool:
    return bool(getattr(sender, "is_bot", False))


def _safe_pet_type(pet_type: str | None) -> str:
    """Keep the species inside the known artwork set (it becomes a file path)."""
    return pet_type if pet_type in PET_TYPES else PET_TYPES[0]


def adult_artwork(pet_type: str) -> Path:
    """The wild pet the player meets; the forest plate if the art is missing."""
    image = ASSET_DIR / f"{_safe_pet_type(pet_type)}_adult.jpg"
    return image if image.is_file() else FOREST_IMAGE


def baby_artwork(pet_type: str) -> Path:
    return ASSET_DIR / f"{_safe_pet_type(pet_type)}_baby.jpg"


def hunt_button(user_id: int, pet_type: str, chance: int) -> InlineKeyboardMarkup:
    """The single «🏹 شکار!» button carrying the encounter's identity."""
    payload = f"{HUNT_CALLBACK_PREFIX}{int(user_id)}:{pet_type}:{int(chance)}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=HUNT_BUTTON_TEXT, callback_data=payload)]
        ]
    )


def parse_hunt_callback(data: str | None) -> tuple[int, str, int] | None:
    """``pet_hunt:{user_id}:{pet_type}:{chance}`` → ``(user_id, pet_type, chance)``.

    Returns ``None`` for anything malformed. The ``chance`` is informational:
    the roll itself uses a value recomputed on the server.
    """
    if not isinstance(data, str) or not data.startswith(HUNT_CALLBACK_PREFIX):
        return None
    parts = data[len(HUNT_CALLBACK_PREFIX):].split(":")
    if len(parts) != 3:
        return None
    raw_user, pet_type, raw_chance = parts
    try:
        user_id = int(raw_user)
        chance = int(raw_chance)
    except (TypeError, ValueError):
        return None
    if user_id <= 0 or pet_type not in PET_TYPES:
        return None
    return user_id, pet_type, chance


async def main_card_power(session, user_id: int) -> int:
    """Power of the player's Main Card, else any owned card, else unarmed."""
    main_card = await PlayerCardService.get_main_card(session, user_id)
    if main_card is None:
        owned = await PlayerCardService.list_cards(session, user_id)
        main_card = owned[0] if owned else None
    if main_card is None:
        return BASE_UNARMED_POWER

    character = await session.get(Character, main_card.character_id)
    if character is None:
        return BASE_UNARMED_POWER
    return max(1, int(card_attributes(main_card, character).power))


def pet_emoji(pet_type: str) -> str:
    """Species emoji for the menu buttons."""
    return {"dog": "🐶", "squirrel": "🐿️", "cat": "🐱", "owl": "🦉"}.get(
        _safe_pet_type(pet_type), "🐾"
    )


def pet_profile_caption(pet, required: int) -> str:
    """The pet profile caption (name, species, status, XP progress, skill)."""
    pet_type = _safe_pet_type(pet.pet_type)
    progress = pet_service.xp_progress(pet, required)
    return PROFILE_CAPTION.format(
        emoji=pet_emoji(pet_type),
        name=html.escape(pet_service.pet_display_name(pet)),
        type_fa=pet_service.pet_type_fa(pet_type),
        status_line=STATUS_ADULT if pet.is_adult else STATUS_BABY,
        xp=fa_digits(progress["xp"]),
        required=fa_digits(progress["required"]),
        percent=fa_digits(progress["percent"]),
        skill=fa_digits(pet.skill_level),
    )


def pet_profile_keyboard(pet) -> InlineKeyboardMarkup:
    """Feed button for a baby, plus a back button to the pet list."""
    rows = []
    if not pet.is_adult:
        rows.append(
            [
                InlineKeyboardButton(
                    text=FEED_BUTTON_TEXT,
                    callback_data=f"{PET_FEED_PREFIX}{int(pet.id)}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔙 پت‌های من", callback_data=PET_VIEW_PREFIX + "menu")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pet_menu_keyboard(pets) -> InlineKeyboardMarkup:
    """One button per pet: ``🦉 جغد (پشمک)``."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"{pet_emoji(pet.pet_type)} "
                        f"{pet_service.pet_type_fa(_safe_pet_type(pet.pet_type))} "
                        f"({pet_service.pet_display_name(pet)})"
                    ),
                    callback_data=f"{PET_VIEW_PREFIX}{int(pet.id)}",
                )
            ]
            for pet in pets
        ]
    )


async def _reply_photo(message: Message, image: Path, caption: str, **kwargs):
    """Reply with artwork, falling back to text when the file is missing."""
    if image.is_file():
        return await message.reply_photo(
            FSInputFile(str(image)), caption=caption, parse_mode="HTML", **kwargs
        )
    logger.warning("missing pet artwork: %s", image)
    return await message.reply(caption, parse_mode="HTML", **kwargs)


# ---------------------------------------------------------------------------
# «سیب شکار» — open an encounter
# ---------------------------------------------------------------------------

@router.message(Command("hunt"))
@router.message(F.text.in_(HUNT_ALIASES))
async def handle_hunt(message: Message, state: FSMContext) -> None:
    sender = message.from_user
    if sender is None or _is_bot(sender):
        return

    try:
        async with get_session() as session:
            card_power = await main_card_power(session, sender.id)
            result = await pet_service.get_encounter(
                session, sender.id, card_power
            )
            status = result.get("status")
            pet_type = result.get("pet_type")
            chance = int(result.get("chance") or 0)
            remaining = int(result.get("remaining_seconds") or 0)
            reason = result.get("reason")
            max_total = result.get("max_total")
    except Exception:
        logger.exception("pet encounter failed (user=%s)", sender.id)
        await message.reply(GENERIC_ERROR)
        return

    if status == "cooldown":
        await message.reply(COOLDOWN_TEXT.format(remaining=format_remaining(remaining)))
        return

    if status == "limit":
        if reason == "total":
            await message.reply(
                LIMIT_TOTAL_TEXT.format(
                    max_total=fa_digits(max_total or pet_service.MAX_PETS_TOTAL)
                )
            )
        else:
            await message.reply(LIMIT_BABY_TEXT)
        return

    if status != "ok":
        await message.reply(GENERIC_ERROR)
        return

    pet_type = _safe_pet_type(pet_type)
    # A new encounter abandons any half-finished naming step.
    await state.clear()
    await _reply_photo(
        message,
        adult_artwork(pet_type),
        format_html_caption(
            ENCOUNTER_CAPTION.format(
                type_fa=pet_service.pet_type_fa(pet_type),
                power=fa_digits(card_power),
                chance=fa_digits(chance),
            )
        ),
        reply_markup=hunt_button(sender.id, pet_type, chance),
    )


# ---------------------------------------------------------------------------
# «🏹 شکار!» — resolve the encounter
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(HUNT_CALLBACK_PREFIX))
async def handle_hunt_callback(
    callback: CallbackQuery, state: FSMContext, bot
) -> None:
    clicker = callback.from_user
    parsed = parse_hunt_callback(getattr(callback, "data", None))
    if clicker is None or parsed is None:
        await safe_answer_callback(callback, HUNT_GONE_ALERT, show_alert=True)
        return

    owner_id, pet_type, _displayed_chance = parsed
    # Anyone in the group sees the button; only the hunter may press it.
    if int(clicker.id) != owner_id:
        await safe_answer_callback(callback, NOT_YOUR_HUNT_ALERT, show_alert=True)
        return

    # The roll uses a server-side chance: the value in the callback data is
    # player-editable and is only kept for the caption the player already saw.
    try:
        async with get_session() as session:
            card_power = await main_card_power(session, owner_id)
            chance = pet_service.hunt_chance(card_power)
            result = await pet_service.execute_hunt(
                session, owner_id, pet_type, chance
            )
            status = result.get("status")
            pet_id = None
            if status == "success" and result.get("pet") is not None:
                pet_id = int(result["pet"].id)
            remaining = int(result.get("remaining_seconds") or 0)
    except Exception:
        logger.exception("pet hunt callback failed (user=%s)", owner_id)
        await safe_answer_callback(callback, GENERIC_ERROR, show_alert=True)
        return

    if status == "cooldown":
        await safe_answer_callback(
            callback,
            COOLDOWN_TEXT.format(remaining=format_remaining(remaining)),
            show_alert=True,
        )
        return
    if status == "limit":
        await safe_answer_callback(callback, LIMIT_BABY_TEXT, show_alert=True)
        return
    if status not in ("success", "failed"):
        await safe_answer_callback(callback, HUNT_GONE_ALERT, show_alert=True)
        return

    await safe_answer_callback(callback)
    await state.clear()

    if status == "success" and pet_id is not None:
        await state.set_state(PetStates.waiting_for_name)
        await state.update_data(pet_id=pet_id)
        await _edit_encounter(
            callback,
            bot,
            baby_artwork(pet_type),
            SUCCESS_CAPTION.format(type_fa=pet_service.pet_type_fa(pet_type)),
        )
        return

    await _edit_encounter(
        callback,
        bot,
        FAIL_IMAGE,
        FAILED_CAPTION.format(type_fa=pet_service.pet_type_fa(pet_type)),
    )


async def _edit_encounter(callback: CallbackQuery, bot, image: Path, caption: str) -> None:
    """Replace the encounter artwork and caption in place, removing the button."""
    message = getattr(callback, "message", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    text = format_html_caption(caption)

    if bot is not None and chat_id and message_id and image.is_file():
        edited = await safe_edit_message_media(
            bot,
            chat_id,
            message_id,
            photo=FSInputFile(str(image)),
            text=text,
            reply_markup=None,
            parse_mode="HTML",
        )
        if edited:
            return

    # The message may have been deleted, or there is no editable photo: still
    # show the outcome once as a plain reply.
    if bot is not None and chat_id and message_id and image.is_file():
        logger.info(
            "pet encounter media edit failed (chat=%s message=%s)", chat_id, message_id
        )
    answer = getattr(message, "answer", None)
    if answer is not None:
        await answer(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# «سیب پت» — the pet menu
# ---------------------------------------------------------------------------

async def _send_pet_menu(answer, user_id: int) -> None:
    """Render the pet list (``answer`` is ``message.answer`` or a reply)."""
    async with get_session() as session:
        pets = await pet_service.list_pets(session, user_id)

    if not pets:
        await answer(NO_PETS_TEXT, parse_mode="HTML")
        return

    await answer(
        format_html_caption(PET_MENU_TITLE),
        reply_markup=pet_menu_keyboard(pets),
        parse_mode="HTML",
    )


@router.message(Command("mypets"))
@router.message(F.text.in_(PET_MENU_ALIASES))
async def handle_my_pets(message: Message, state: FSMContext) -> None:
    sender = message.from_user
    if sender is None or _is_bot(sender):
        return

    # Opening the menu abandons a half-finished naming step, like a new hunt.
    await state.clear()
    try:
        await _send_pet_menu(message.answer, sender.id)
    except Exception:
        logger.exception("pet menu failed (user=%s)", sender.id)
        await message.reply(GENERIC_ERROR)


# ---------------------------------------------------------------------------
# Pet profile (pet_view:{pet_id} or pet_view:menu)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(PET_VIEW_PREFIX))
async def handle_pet_view(callback: CallbackQuery, state: FSMContext, bot) -> None:
    sender = callback.from_user
    if sender is None:
        return
    raw = (callback.data or "")[len(PET_VIEW_PREFIX):]

    if raw == "menu":
        await safe_answer_callback(callback)
        try:
            await _send_pet_menu(callback.message.answer, sender.id)
        except Exception:
            logger.exception("pet menu from callback failed (user=%s)", sender.id)
            await safe_answer_callback(callback, GENERIC_ERROR, show_alert=True)
        return

    try:
        async with get_session() as session:
            pet = await pet_service.get_pet(session, sender.id, raw)
            if pet is None:
                await safe_answer_callback(callback, PET_GONE_ALERT, show_alert=True)
                return
            required = await pet_service.adult_xp_required(session)
            caption = pet_profile_caption(pet, required)
            keyboard = pet_profile_keyboard(pet)
            image = (
                adult_artwork(pet.pet_type)
                if pet.is_adult
                else baby_artwork(pet.pet_type)
            )
    except Exception:
        logger.exception("pet profile failed (user=%s pet=%s)", sender.id, raw)
        await safe_answer_callback(callback, GENERIC_ERROR, show_alert=True)
        return

    await safe_answer_callback(callback)
    await _show_pet_card(bot, callback, image, caption, keyboard)


async def _show_pet_card(bot, callback, image: Path, caption: str, keyboard) -> None:
    """Replace the current message with a pet card (photo + caption + buttons)."""
    message = getattr(callback, "message", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    text = format_html_caption(caption)

    has_photo = bool(getattr(message, "photo", None))
    if bot is not None and chat_id and message_id and has_photo and image.is_file():
        edited = await safe_edit_message_media(
            bot,
            chat_id,
            message_id,
            photo=FSInputFile(str(image)),
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        if edited:
            return
    if bot is not None and chat_id and message_id and has_photo:
        edited = await safe_edit_message_caption(
            bot, chat_id, message_id, text=text, reply_markup=keyboard, parse_mode="HTML"
        )
        if edited:
            return

    # No editable photo message (the menu is text-only): send the card fresh.
    photo_sender = getattr(message, "answer_photo", None)
    if photo_sender is not None and image.is_file():
        await photo_sender(
            FSInputFile(str(image)),
            caption=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return
    answer = getattr(message, "answer", None)
    if answer is not None:
        await answer(text, reply_markup=keyboard, parse_mode="HTML")


# ---------------------------------------------------------------------------
# «🍖 غذا دادن» (pet_feed:{pet_id})
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(PET_FEED_PREFIX))
async def handle_pet_feed(callback: CallbackQuery, state: FSMContext, bot) -> None:
    sender = callback.from_user
    if sender is None:
        return
    raw_pet_id = (callback.data or "")[len(PET_FEED_PREFIX):]

    try:
        async with get_session() as session:
            result = await pet_service.feed_pet(session, sender.id, raw_pet_id)
    except Exception:
        logger.exception("pet feeding failed (user=%s pet=%s)", sender.id, raw_pet_id)
        await safe_answer_callback(callback, GENERIC_ERROR, show_alert=True)
        return

    status = result.get("status")
    pet = result.get("pet")

    if status == "no_food":
        await safe_answer_callback(callback, FEED_NO_FOOD_ALERT, show_alert=True)
        return
    if status == "too_soon":
        await safe_answer_callback(callback, FEED_TOO_SOON_ALERT, show_alert=True)
        return
    if status == "already_adult":
        await safe_answer_callback(callback, FEED_ALREADY_ADULT_ALERT, show_alert=True)
        return
    if status != "fed" and status != "evolved":
        await safe_answer_callback(callback, PET_GONE_ALERT, show_alert=True)
        return

    pet_type = _safe_pet_type(pet.pet_type)
    type_fa = pet_service.pet_type_fa(pet_type)
    name = pet_service.pet_display_name(pet)
    required = int(result.get("required") or 0)

    if status == "evolved":
        await safe_answer_callback(callback, "🎉 پت شما بالغ شد!")
        await _show_pet_card(
            bot,
            callback,
            adult_artwork(pet_type),
            format_html_caption(
                FEED_EVOLVED_CAPTION.format(
                    name=html.escape(name), type_fa=type_fa
                )
                + "\n"
                + pet_profile_caption(pet, required)
            ),
            pet_profile_keyboard(pet),
        )
        return

    await safe_answer_callback(callback, f"🍖 +{fa_digits(result.get('xp_added') or 0)} XP")
    # One line about the meal, then the refreshed profile (no duplicated XP).
    caption = FEED_DONE_CAPTION.format(
        name=html.escape(name),
        xp_added=fa_digits(result.get("xp_added") or 0),
    )
    await _show_pet_card(
        bot,
        callback,
        baby_artwork(pet_type),
        format_html_caption(caption + "\n\n" + pet_profile_caption(pet, required)),
        pet_profile_keyboard(pet),
    )


# ---------------------------------------------------------------------------
# Naming the new baby
# ---------------------------------------------------------------------------

@router.message(PetStates.waiting_for_name, F.text, ~F.text.startswith("/"))
async def handle_pet_name(message: Message, state: FSMContext) -> None:
    sender = message.from_user
    if sender is None or _is_bot(sender):
        return

    data = await state.get_data()
    pet_id = data.get("pet_id")
    if pet_id is None:
        await state.clear()
        await message.reply(NO_PET_TEXT)
        return

    try:
        async with get_session() as session:
            result = await pet_service.name_pet(
                session, sender.id, int(pet_id), message.text
            )
    except Exception:
        logger.exception("pet naming failed (user=%s pet=%s)", sender.id, pet_id)
        await message.reply(GENERIC_ERROR)
        return

    if result.get("status") != "named":
        # Keep the state: the player can retype a valid name immediately.
        await message.reply(NAME_ERRORS.get(str(result.get("status")), GENERIC_ERROR))
        return

    await state.clear()
    await message.reply(
        NAMED_TEXT.format(name=html.escape(str(result.get("name") or ""))),
        parse_mode="HTML",
    )
