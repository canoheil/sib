"""Telegram UX for the pet system: the «سیب شکار» encounter and naming.

All rules live in :mod:`sib.services.pet_service`; this module only decides
what the player sees.

Flow
----
1. ``/hunt`` or the plain-text aliases «سیب شکار» / «شکار سیبی» open an
   *encounter*: the service validates the cooldown and the pet caps and rolls
   which wild pet the player meets. The forest plate is sent and the
   multi-step *tracking mini-game* begins (Phase 4).
2. The tracking game asks three questions — which side the rustling comes
   from, where the footprints lead, when to throw the net. The bot has rolled
   a hidden correct path behind the scenes; every click edits the same
   message, and each correct choice adds 10 % to the base chance while each
   wrong one subtracts 10 % (see
   :func:`pet_service.tracking_adjusted_chance`).
3. The final click rolls :func:`pet_service.execute_hunt` with the adjusted
   chance and edits the message: the escaping-pet artwork on a miss, or the
   baby's artwork on success plus the naming prompt.
4. ``PetStates.waiting_for_name`` then takes the pet's name from a message
   that REPLIES to the bot's prompt (a group is shared, so any other message
   is refused with a gentle reminder). An invalid name keeps the state so the
   player can retry.
5. «سیب پت» (``/mypets``) posts the ONE menu message as PLAIN TEXT — no
   artwork, the forest plate belongs to the hunt — with the player's pets as
   buttons. Opening a pet card swaps that text message into the pet's photo
   with ``edit_message_media``; every other screen after it — the pet card
   with XP progress and skill, «🍖 غذا دادن» (spends one ``pet_food`` from
   the inventory and eventually evolves the pet), «✏️ تغییر نام» (the ONE
   paid rename, ``PetStates.waiting_for_rename``, same reply-to-prompt rule)
   — edits that same message in place (:func:`_edit_pet_screen`).
   «🔙 پت‌های من» cannot edit a photo back into text, so on a photo card it
   deletes the card and sends the text menu as a fresh message.
6. The profile explains the pet's skill: locked for a baby
   («🔒 ... با بالغ شدن فعال می‌شود»), name + level + what it does once the
   pet is an adult (see :func:`pet_skill_line`).

Only the player who opened the encounter may press its buttons: the tracking
state lives in their own FSM context, and the roll uses a server-side chance
rather than any client-visible number (see :func:`handle_hunt_track`).
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
    InlineKeyboardMarkup,
    Message,
)

from sib.core.logging import logger
from sib.core.text import format_html_caption
from sib.database.connection import get_session
from sib.database.models import PET_TYPES, Character
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services import pet_service
from sib.services.card_progression import card_attributes
from sib.services.player_cards import PlayerCardService
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import (
    safe_answer_callback,
    safe_edit_message_caption,
    safe_edit_message_media,
    safe_edit_message_text,
    safe_send_screen,
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

#: Callback payload of the tracking game: ``hunt_track:step{step}:{choice}``.
HUNT_TRACK_PREFIX = "hunt_track:"
PET_VIEW_PREFIX = "pet_view:"
PET_FEED_PREFIX = "pet_feed:"
PET_RENAME_PREFIX = "pet_rename:"

#: FUTURE EXPANSION — Group Raid (Boss Pet): a future phase will add a
#: shared raid encounter against a boss pet. When it lands, the tracking
#: mini-game below is the natural extension point: the boss raid would open
#: its own multi-step sequence (per-player contributions, a shared timer)
#: with the same edit-in-place UX and a ``hunt_raid:*`` callback prefix.
#: Nothing is implemented here on purpose — the encounter/cooldown service
#: (``pet_service``) is where the raid rules would live.

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
#: The same hunt block when the baby ALREADY has a name: it must be fed to
#: adulthood first — naming is done.
LIMIT_BABY_NAMED_TEXT = (
    "🐾 شما یک بچه پت ({name}) دارید که هنوز بزرگ نشده. "
    "اول بهش غذا بدهید تا بالغ شود، بعد دوباره شکار کنید."
)
#: The encounter screen. ``**`` renders as bold through ``format_html_caption``.
ENCOUNTER_CAPTION = (
    "🌲 وارد جنگل جادویی شدی!\n"
    "یک **{type_fa} وحشی** دیدی!\n"
    "💪 قدرت کارتت: {power}\n"
    "🎯 شانس شکار: {chance}٪"
)
FAILED_CAPTION = "💨 ای وای! {type_fa} فرار کرد. دفعه بعد شانس بهتری داری."
SUCCESS_CAPTION = (
    "🎉 شکار موفق! تونستی بچه {type_fa} رو بگیری.\n"
    "حالا یک اسم براش انتخاب کن و روی همین پیام ریپلای کن:"
)
HUNT_GONE_ALERT = "این شکار منقضی شده. دوباره «سیب شکار» بزن."

# ---------------------------------------------------------------------------
# The tracking mini-game (Phase 4)
# ---------------------------------------------------------------------------
#: The three questions of the tracking sequence, in order.
STEP1_CAPTION = "🌲 وارد جنگل شدی. صدای خش‌خش برگ‌ها از کدوم طرف میاد؟"
STEP2_CAPTION = "👣 رد پاها رو پیدا کردی. به کجا می‌رسه؟"
STEP3_CAPTION = "👀 پت رو دیدی! کِی تور رو پرتاب کنیم؟"
#: ``step -> {choice: button text}``; the choice keys are the callback payload
#: suffixes and the values the ``correct_path`` entries the service compares
#: against (see ``pet_service.correct_choice``).
TRACK_BUTTONS = {
    1: {
        "left": "⬅️ سمت چپ",
        "right": "➡️ سمت راست",
    },
    2: {
        "cave": "🕳️ غار تاریک",
        "river": "🌊 رودخانه",
    },
    3: {
        "now": "🥅 همین حالا!",
        "wait": "⏳ کمی صبر می‌کنم",
    },
}
NAMED_TEXT = (
    "✅ اسم {name} برای پت شما ثبت شد! "
    "از این به بعد با دستور «سیب پت» می‌تونید مدیریتش کنید."
)
#: Shown right after the name is saved: a baby pet still has a locked skill,
#: and the player should know it is worth growing.
SKILL_TEASER = (
    "💡 این پت وقتی بالغ بشه، اسکیل مخصوص خودش رو باز می‌کنه "
    "که توی بازی بهت کمک می‌کنه!"
)
NO_PET_TEXT = "🐾 پت موردنظر پیدا نشد. دوباره «سیب شکار» را امتحان کنید."

NO_PETS_TEXT = "شما هنوز هیچ پتی ندارید. با دستور «سیب شکار» برید شکار!"
PET_MENU_TITLE = "🐾 **پت‌های شما**\nبرای دیدن پروفایل هر پت روی اسمش بزن:"
#: The menu itself is TEXT-ONLY: the forest plate (``FOREST_IMAGE``) belongs to
#: the hunt, not to «سیب پت». Pet cards are the only photo screens of this
#: flow and are reached by editing the menu message into a photo.
PROFILE_CAPTION = (
    "{emoji} **{name}**\n"
    "🧬 نوع: {type_fa}\n"
    "{status_line}\n"
    "📊 XP: {xp}/{required} ({percent}٪)\n"
    "{skill_line}"
)
#: ``pet_type -> (skill name, what the skill actually does)``. Every species
#: unlocks its own ability when it grows up (see :func:`pet_skill_line`).
PET_SKILLS = {
    "dog": ("پارس هشدار", "محافظت در برابر دزدی"),
    "squirrel": ("برکت باغ", "سرعت بخشیدن به رشد باغ"),
    "cat": ("چشم شانس", "شانس بیشتر در کازینو و راکت"),
    "owl": ("خشم شب", "دمیج بیشتر در نبردها"),
}
#: Defensive fallback for an unknown species: still announce the skill.
FALLBACK_SKILL = ("اسکیل ویژه", "کمک به تو در بازی")
#: A baby's skill exists but is not usable yet.
SKILL_LOCKED_LINE = "🔒 اسکیل: {skill_name} (با بالغ شدن فعال می‌شود)"
#: An adult's skill: name, level and what it does.
SKILL_ACTIVE_LINE = "✨ اسکیل: {skill_name} - لول {level}\n{description}"
STATUS_BABY = "🍼 وضعیت: بچه"
STATUS_ADULT = "👑 وضعیت: بالغ"
FEED_BUTTON_TEXT = "🍖 غذا دادن"
#: One paid rename per pet (see ``pet_service.rename_pet``). The cost is shown
#: in Persian digits inside the button label.
RENAME_BUTTON_TEXT = f"✏️ تغییر نام ({fa_digits(pet_service.RENAME_COST_APPLES)} سیب)"
RENAME_PROMPT = "✏️ اسم جدید پت رو بنویسید و روی همین پیام ریپلای کنید:"
#: Shared by both naming states: the name only counts as a reply to the prompt.
REPLY_TO_PROMPT_REQUIRED = "لطفاً روی پیام بات ریپلای کنید و اسم پت رو بنویسید."
RENAME_ALREADY_USED_ALERT = "این پت قبلاً یک بار تغییر نام داده. هر پت فقط یک بار می‌تونه اسم عوض کنه."
RENAME_NO_FUNDS_ALERT = (
    "🍎 برای تغییر نام "
    + fa_digits(pet_service.RENAME_COST_APPLES)
    + " سیب لازم داری."
)
RENAMED_TEXT = "✅ اسم پت به **{name}** تغییر کرد."
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
    """FSM states of the hunt flow: the tracking steps and the naming step."""

    #: Active while the player is answering the tracking mini-game. Data:
    #: ``pet_type``, ``base_chance``, ``path`` (the hidden correct choices).
    tracking = State()
    #: After a successful catch, waiting for the pet's name.
    waiting_for_name = State()
    #: Waiting for the new name of an already-named pet (paid rename). Data:
    #: ``pet_id`` and ``prompt_message_id`` (the message to reply to).
    waiting_for_rename = State()


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


def replied_to_prompt(message, prompt_message_id) -> bool:
    """Strict reply-to check for the two naming states.

    In a group every message reaches the handler, so the pet's name must come
    from a reply to the exact bot prompt that asked for it — otherwise a
    friend's unrelated message would be saved as the name. When the prompt id
    was not recorded (a state created before the guard existed), any reply is
    still refused unless it actually replies to *some* message.
    """
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return False
    if prompt_message_id is None:
        return True
    try:
        return int(getattr(replied, "message_id", 0)) == int(prompt_message_id)
    except (TypeError, ValueError):
        return False


def _safe_pet_type(pet_type: str | None) -> str:
    """Keep the species inside the known artwork set (it becomes a file path)."""
    return pet_type if pet_type in PET_TYPES else PET_TYPES[0]


def adult_artwork(pet_type: str) -> Path:
    """The wild pet the player meets; the forest plate if the art is missing."""
    image = ASSET_DIR / f"{_safe_pet_type(pet_type)}_adult.jpg"
    return image if image.is_file() else FOREST_IMAGE


def baby_artwork(pet_type: str) -> Path:
    return ASSET_DIR / f"{_safe_pet_type(pet_type)}_baby.jpg"


def tracking_keyboard(step: int) -> InlineKeyboardMarkup:
    """The two buttons of one tracking step (``hunt_track:step{step}:{choice}``)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                primary_button(
                    text=text,
                    callback_data=f"{HUNT_TRACK_PREFIX}step{int(step)}:{choice}",
                )
                for choice, text in TRACK_BUTTONS[int(step)].items()
            ]
        ]
    )


def parse_hunt_track_callback(data: str | None) -> tuple[int, str] | None:
    """``hunt_track:step{step}:{choice}`` → ``(step, choice)``.

    Returns ``None`` for anything malformed: a missing prefix, a step outside
    ``1..3`` or a choice the step does not offer. The step is validated against
    ``TRACK_BUTTONS`` so a forged payload can never skip ahead.
    """
    if not isinstance(data, str) or not data.startswith(HUNT_TRACK_PREFIX):
        return None
    parts = data[len(HUNT_TRACK_PREFIX):].split(":")
    if len(parts) != 2:
        return None
    raw_step, choice = parts
    if not raw_step.startswith("step"):
        return None
    try:
        step = int(raw_step[len("step"):])
    except (TypeError, ValueError):
        return None
    if step not in TRACK_BUTTONS or choice not in TRACK_BUTTONS[step]:
        return None
    return step, choice


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


def pet_skill(pet_type: str) -> tuple[str, str]:
    """``(name, description)`` of the ability a species unlocks as an adult."""
    return PET_SKILLS.get(_safe_pet_type(pet_type), FALLBACK_SKILL)


def pet_skill_line(pet) -> str:
    """The skill line of the profile.

    A baby only sees WHAT it will unlock (locked, no numbers); an adult sees
    the active skill with its level and what it actually does — otherwise
    «⭐ اسکیل: ۱» told the player nothing about their pet's ability.
    """
    skill_name, description = pet_skill(pet.pet_type)
    if not pet.is_adult:
        return SKILL_LOCKED_LINE.format(skill_name=skill_name)
    return SKILL_ACTIVE_LINE.format(
        skill_name=skill_name,
        level=fa_digits(pet.skill_level),
        description=description,
    )


def pet_profile_caption(pet, required: int) -> str:
    """The pet profile caption (name, species, status, XP progress, skill).

    The caption is returned raw (``**bold**`` markers included): the caller
    renders it through ``format_html_caption`` exactly once, which escapes
    the name. Pre-escaping here would double-escape it.
    """
    pet_type = _safe_pet_type(pet.pet_type)
    progress = pet_service.xp_progress(pet, required)
    return PROFILE_CAPTION.format(
        emoji=pet_emoji(pet_type),
        name=pet_service.pet_display_name(pet),
        type_fa=pet_service.pet_type_fa(pet_type),
        status_line=STATUS_ADULT if pet.is_adult else STATUS_BABY,
        xp=fa_digits(progress["xp"]),
        required=fa_digits(progress["required"]),
        percent=fa_digits(progress["percent"]),
        skill_line=pet_skill_line(pet),
    )


def pet_profile_keyboard(pet) -> InlineKeyboardMarkup:
    """Feed (baby), rename, and the back button to the pet list.

    Colors: feeding and renaming are positive/main actions (green and blue),
    «پت‌های من» is navigation back (red per the bot-wide convention). The
    rename button disappears once the single paid rename has been used.
    """
    rows = []
    if not pet.is_adult:
        rows.append(
            [
                success_button(
                    text=FEED_BUTTON_TEXT,
                    callback_data=f"{PET_FEED_PREFIX}{int(pet.id)}",
                )
            ]
        )
    if not getattr(pet, "has_renamed", False):
        rows.append(
            [
                primary_button(
                    text=RENAME_BUTTON_TEXT,
                    callback_data=f"{PET_RENAME_PREFIX}{int(pet.id)}",
                )
            ]
        )
    rows.append(
        [
            danger_button(
                text="🔙 پت‌های من", callback_data=PET_VIEW_PREFIX + "menu"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pet_menu_keyboard(pets) -> InlineKeyboardMarkup:
    """One button per pet: ``🦉 جغد (پشمک)``."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                primary_button(
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
            # A baby blocks the hunt, and the block message depends on whether
            # the baby already has a name: name it first, or feed it first.
            baby_name = None
            if status == "limit" and reason != "total":
                owned = await pet_service.list_pets(session, sender.id)
                baby = next((p for p in owned if not p.is_adult), None)
                if baby is not None:
                    baby_name = baby.custom_name
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
        elif baby_name is None:
            # The baby has no name yet: name it first.
            await message.reply(LIMIT_BABY_TEXT)
        else:
            # The baby is named: it has to be fed to adulthood first.
            await message.reply(LIMIT_BABY_NAMED_TEXT.format(name=baby_name))
        return

    if status != "ok":
        await message.reply(GENERIC_ERROR)
        return

    pet_type = _safe_pet_type(pet_type)
    # A new encounter abandons any half-finished tracking or naming step and
    # rolls the hidden correct path of the tracking mini-game.
    path = pet_service.roll_tracking_path()
    await state.clear()
    await state.set_state(PetStates.tracking)
    await state.update_data(
        pet_type=pet_type, base_chance=chance, path=path, choices=[]
    )
    await _reply_photo(
        message,
        FOREST_IMAGE,
        format_html_caption(
            ENCOUNTER_CAPTION.format(
                type_fa=pet_service.pet_type_fa(pet_type),
                power=fa_digits(card_power),
                chance=fa_digits(chance),
            )
            + "\n\n"
            + STEP1_CAPTION
        ),
        reply_markup=tracking_keyboard(1),
    )


# ---------------------------------------------------------------------------
# The tracking mini-game — «hunt_track:step{step}:{choice}»
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(HUNT_TRACK_PREFIX))
async def handle_hunt_track(
    callback: CallbackQuery, state: FSMContext, bot
) -> None:
    clicker = callback.from_user
    parsed = parse_hunt_track_callback(getattr(callback, "data", None))
    if clicker is None or parsed is None:
        await safe_answer_callback(callback, HUNT_GONE_ALERT, show_alert=True)
        return

    step, choice = parsed
    # The tracking state is per-user FSM: a stale or expired state (a finished
    # hunt, a new encounter, the naming step) can never resume the game, and a
    # group member pressing the buttons sees their own (empty) context.
    if await state.get_state() != PetStates.tracking.state:
        await safe_answer_callback(callback, HUNT_GONE_ALERT, show_alert=True)
        return

    data = await state.get_data()
    pet_type = data.get("pet_type")
    base_chance = data.get("base_chance")
    path = data.get("path")
    choices_made = data.get("choices")
    if (
        pet_type not in PET_TYPES
        or not isinstance(base_chance, int)
        or not isinstance(path, list)
        or not isinstance(choices_made, list)
    ):
        await state.clear()
        await safe_answer_callback(callback, HUNT_GONE_ALERT, show_alert=True)
        return

    # Steps are answered in order, exactly once: a stale button press or a
    # forged step number is refused instead of skipping ahead.
    if step != len(choices_made) + 1:
        await safe_answer_callback(callback, HUNT_GONE_ALERT, show_alert=True)
        return

    choices = choices_made + [choice]
    await state.update_data(choices=choices)
    await safe_answer_callback(callback)

    if step < max(TRACK_BUTTONS):
        await _edit_tracking_step(callback, bot, step + 1)
        return

    # Final shot: the hidden path adjusts the base chance (±10 % per choice).
    final_chance = pet_service.tracking_adjusted_chance(base_chance, choices, path)
    try:
        async with get_session() as session:
            result = await pet_service.execute_hunt(
                session, clicker.id, pet_type, final_chance
            )
            status = result.get("status")
            pet_id = None
            if status == "success" and result.get("pet") is not None:
                pet_id = int(result["pet"].id)
            remaining = int(result.get("remaining_seconds") or 0)
    except Exception:
        logger.exception("pet tracking finale failed (user=%s)", clicker.id)
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

    await state.clear()

    if status == "success" and pet_id is not None:
        await state.set_state(PetStates.waiting_for_name)
        await state.update_data(pet_id=pet_id)
        prompt_message_id = await _edit_encounter(
            callback,
            bot,
            baby_artwork(pet_type),
            SUCCESS_CAPTION.format(type_fa=pet_service.pet_type_fa(pet_type)),
        )
        # The name must be written as a reply to THAT prompt (see
        # ``handle_pet_name``), so its id is part of the state.
        await state.update_data(prompt_message_id=prompt_message_id)
        return

    await _edit_encounter(
        callback,
        bot,
        FAIL_IMAGE,
        FAILED_CAPTION.format(type_fa=pet_service.pet_type_fa(pet_type)),
    )


async def _edit_tracking_step(callback: CallbackQuery, bot, step: int) -> None:
    """Move the tracking message to ``step``: new question and buttons."""
    caption = {2: STEP2_CAPTION, 3: STEP3_CAPTION}[step]
    edited = await _edit_hunt_screen(
        callback, bot, FOREST_IMAGE, caption, tracking_keyboard(step)
    )
    if edited:
        return
    # The message may have been deleted: keep the game going with a fresh one.
    message = getattr(callback, "message", None)
    answer = getattr(message, "answer", None)
    if answer is not None:
        await answer(
            format_html_caption(caption),
            reply_markup=tracking_keyboard(step),
            parse_mode="HTML",
        )


async def _edit_hunt_screen(
    callback: CallbackQuery, bot, image: Path, caption: str, keyboard=None
) -> bool:
    """Replace a hunt-screen message in place (photo + caption + buttons).

    Returns ``True`` when the message content is current. The photo is kept
    when only the caption changes; the media is swapped when it does not.
    """
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
            return True
        logger.info(
            "pet screen media edit failed (chat=%s message=%s)", chat_id, message_id
        )
    if bot is not None and chat_id and message_id and has_photo:
        edited = await safe_edit_message_caption(
            bot, chat_id, message_id, text=text, reply_markup=keyboard, parse_mode="HTML"
        )
        if edited:
            return True
        logger.info(
            "pet screen caption edit failed (chat=%s message=%s)", chat_id, message_id
        )
    return False


async def _edit_encounter(
    callback: CallbackQuery, bot, image: Path, caption: str
) -> int | None:
    """Replace the encounter artwork and caption in place, removing the button.

    Returns the ``message_id`` of the message now carrying ``caption`` — the
    prompt a naming reply must point at (``None`` when nothing could be sent).
    """
    edited = await _edit_hunt_screen(callback, bot, image, caption)
    if edited:
        message = getattr(callback, "message", None)
        return getattr(message, "message_id", None)
    # The message may have been deleted, or there is no editable photo: still
    # show the outcome once as a plain reply.
    message = getattr(callback, "message", None)
    answer = getattr(message, "answer", None)
    if answer is not None:
        sent = await answer(format_html_caption(caption), parse_mode="HTML")
        return getattr(sent, "message_id", None)
    return None


# ---------------------------------------------------------------------------
# «سیب پت» — the pet menu
# ---------------------------------------------------------------------------

async def _pet_menu_content(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """``(text, keyboard)`` of the pet list menu.

    The menu is TEXT-ONLY — no plate, the forest art belongs to the hunt.
    An empty list has no keyboard: the player is told to hunt.
    """
    async with get_session() as session:
        pets = await pet_service.list_pets(session, user_id)

    if not pets:
        return NO_PETS_TEXT, None
    return PET_MENU_TITLE, pet_menu_keyboard(pets)


async def _send_pet_menu(message, user_id: int) -> None:
    """Open the pet list — a TEXT-ONLY message, the ONE fresh send of the flow.

    No photo is attached (the ``hunt_forest.jpg`` plate belongs to hunting):
    every screen after it (a pet card, «🔙 پت‌های من») is rendered through
    :func:`_edit_pet_screen`, which swaps this text message into a photo for a
    card and, on the way back, deletes a photo card to show this text menu
    again — so browsing pets never spams the chat with a new message per click.
    """
    caption, keyboard = await _pet_menu_content(user_id)
    await message.answer(
        format_html_caption(caption), reply_markup=keyboard, parse_mode="HTML"
    )


async def _show_pet_menu(bot, callback, user_id: int) -> bool:
    """Put the text-only pet list back («پت‌های من»)."""
    caption, keyboard = await _pet_menu_content(user_id)
    return await _edit_pet_screen(bot, callback, caption, keyboard, image=None)


async def _safe_delete_message(bot, chat_id: int, message_id: int) -> bool:
    """Delete a message without ever raising (best effort).

    When the delete itself fails (the message is already gone, or the bot has
    no delete rights in the chat) the caller still sends the fresh screen, so
    the button never looks dead.
    """
    try:
        return bool(await bot.delete_message(chat_id, message_id))
    except Exception as exc:
        logger.info(
            "could not delete message %s in chat %s: %s", message_id, chat_id, exc
        )
        return False


async def _edit_pet_screen(
    bot, callback, caption: str, keyboard=None, image: Path | None = None
) -> bool:
    """Render a pet screen on the message behind ``callback`` (edit, never send).

    * Photo screen (``image`` given — a pet card, a feed result): one
      ``edit_message_media`` swaps the artwork of a photo message AND turns
      the text-only menu into the pet's photo, so opening a card never sends
      a new message. If the media edit fails, the chain degrades instead of
      duplicating the message: caption edit for a photo message, then a plain
      ``edit_message_text`` for a text message.
    * Text screen (``image is None`` — the menu): a plain ``edit_message_text``
      when the current message is already text. Telegram cannot edit a PHOTO
      into a text message, so on a photo card the photo is deleted and the
      text menu is sent as a fresh message (the menu's one send).

    A fresh send happens ONLY when the message is gone (deleted) and every
    edit failed. Returns ``True`` when the screen is shown on the flow's
    message — or, for the deleted-photo case, on the fresh menu message.
    """
    message = getattr(callback, "message", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    text = format_html_caption(caption)
    has_photo = bool(getattr(message, "photo", None))
    wants_photo = image is not None and image.is_file()

    if bot is not None and chat_id and message_id:
        if wants_photo:
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
                return True
            logger.info(
                "pet screen media edit failed (chat=%s message=%s)",
                chat_id,
                message_id,
            )
            if has_photo and await safe_edit_message_caption(
                bot,
                chat_id,
                message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            ):
                return True
            if await safe_edit_message_text(
                bot,
                chat_id,
                message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            ):
                return True
        elif has_photo:
            # Photo -> text cannot be edited in place: delete the pet's photo
            # message and post the text-only menu as a fresh message.
            await _safe_delete_message(bot, chat_id, message_id)
            await safe_send_screen(
                bot, chat_id, text, reply_markup=keyboard, parse_mode="HTML"
            )
            return True
        elif await safe_edit_message_text(
            bot,
            chat_id,
            message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        ):
            return True
        logger.info("pet screen edit failed (chat=%s message=%s)", chat_id, message_id)

    # Nothing was editable (the message is gone): show the screen once, so the
    # button never looks dead.
    answer_photo = getattr(message, "answer_photo", None)
    if wants_photo and answer_photo is not None:
        await answer_photo(
            FSInputFile(str(image)),
            caption=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return False
    answer = getattr(message, "answer", None)
    if answer is not None:
        await answer(text, reply_markup=keyboard, parse_mode="HTML")
    return False


@router.message(Command("mypets"))
@router.message(F.text.in_(PET_MENU_ALIASES))
async def handle_my_pets(message: Message, state: FSMContext) -> None:
    sender = message.from_user
    if sender is None or _is_bot(sender):
        return

    # Opening the menu abandons a half-finished naming step, like a new hunt.
    await state.clear()
    try:
        await _send_pet_menu(message, sender.id)
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
        # «🔙 پت‌های من»: back to the list in the SAME message.
        await safe_answer_callback(callback)
        try:
            await _show_pet_menu(bot, callback, sender.id)
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
    await _edit_pet_screen(bot, callback, caption, keyboard, image)


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
        await _edit_pet_screen(
            bot,
            callback,
            FEED_EVOLVED_CAPTION.format(name=name, type_fa=type_fa)
            + "\n"
            + pet_profile_caption(pet, required),
            pet_profile_keyboard(pet),
            adult_artwork(pet_type),
        )
        return

    await safe_answer_callback(callback, f"🍖 +{fa_digits(result.get('xp_added') or 0)} XP")
    # One line about the meal, then the refreshed profile (no duplicated XP).
    # NOTE: the caption is passed to ``_edit_pet_screen`` raw (``**bold**``
    # markers included): that helper renders it through ``format_html_caption``
    # exactly once. Pre-rendering here would escape the ``<b>`` tags into
    # ``&lt;b&gt;`` and the feed caption would show raw HTML.
    caption = FEED_DONE_CAPTION.format(
        name=name,
        xp_added=fa_digits(result.get("xp_added") or 0),
    )
    await _edit_pet_screen(
        bot,
        callback,
        caption + "\n\n" + pet_profile_caption(pet, required),
        pet_profile_keyboard(pet),
        baby_artwork(pet_type),
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
    # The state is per-user FSM but the CHAT is shared: without the reply
    # guard any message in the group would be stored as this pet's name.
    if not replied_to_prompt(message, data.get("prompt_message_id")):
        await message.reply(REPLY_TO_PROMPT_REQUIRED)
        return

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
        NAMED_TEXT.format(name=html.escape(str(result.get("name") or "")))
        + "\n"
        + SKILL_TEASER,
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# «✏️ تغییر نام» — the one paid rename (pet_rename:{pet_id})
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith(PET_RENAME_PREFIX))
async def handle_pet_rename(callback: CallbackQuery, state: FSMContext, bot) -> None:
    """Ask for the new name: the reply to THIS prompt carries it."""
    sender = callback.from_user
    if sender is None:
        return
    raw_pet_id = (callback.data or "")[len(PET_RENAME_PREFIX):]

    try:
        async with get_session() as session:
            pet = await pet_service.get_pet(session, sender.id, raw_pet_id)
    except Exception:
        logger.exception("pet rename prompt failed (user=%s pet=%s)", sender.id, raw_pet_id)
        await safe_answer_callback(callback, GENERIC_ERROR, show_alert=True)
        return

    if pet is None:
        await safe_answer_callback(callback, PET_GONE_ALERT, show_alert=True)
        return
    if pet.has_renamed:
        await safe_answer_callback(
            callback, RENAME_ALREADY_USED_ALERT, show_alert=True
        )
        return

    await safe_answer_callback(callback)
    # A half-finished hunt or naming step is abandoned: the FSM now waits for
    # a name that must be a reply to the prompt sent right below.
    await state.clear()
    message = getattr(callback, "message", None)
    answer = getattr(message, "answer", None)
    prompt_message_id = None
    if answer is not None:
        sent = await answer(RENAME_PROMPT)
        prompt_message_id = getattr(sent, "message_id", None)

    await state.set_state(PetStates.waiting_for_rename)
    await state.update_data(
        pet_id=int(raw_pet_id), prompt_message_id=prompt_message_id
    )


@router.message(PetStates.waiting_for_rename, F.text, ~F.text.startswith("/"))
async def handle_pet_rename_name(message: Message, state: FSMContext) -> None:
    """Apply the new name once the player replies to the rename prompt."""
    sender = message.from_user
    if sender is None or _is_bot(sender):
        return

    data = await state.get_data()
    if not replied_to_prompt(message, data.get("prompt_message_id")):
        await message.reply(REPLY_TO_PROMPT_REQUIRED)
        return

    pet_id = data.get("pet_id")
    if pet_id is None:
        await state.clear()
        await message.reply(NO_PET_TEXT)
        return

    try:
        async with get_session() as session:
            result = await pet_service.rename_pet(
                session, sender.id, int(pet_id), message.text
            )
    except Exception:
        logger.exception("pet rename failed (user=%s pet=%s)", sender.id, pet_id)
        await message.reply(GENERIC_ERROR)
        return

    status = str(result.get("status"))
    if status == "already_renamed":
        await state.clear()
        await message.reply(RENAME_ALREADY_USED_ALERT)
        return
    if status == "insufficient_funds":
        # Keep the state: a player who earns the apples can retry right away.
        await message.reply(RENAME_NO_FUNDS_ALERT)
        return
    if status != "renamed":
        await message.reply(NAME_ERRORS.get(status, GENERIC_ERROR))
        return

    await state.clear()
    await message.reply(
        format_html_caption(
            RENAMED_TEXT.format(name=html.escape(str(result.get("name") or "")))
        ),
        parse_mode="HTML",
    )
