"""Telegram UX for the pet system (Phase 2): «سیب شکار» hunting and naming.

All rules live in :mod:`sib.services.pet_service`; this module only decides
what the player sees.

Flow
----
1. ``/hunt`` or the plain-text aliases «سیب شکار» / «شکار سیبی» roll one hunt
   (cooldown and pet caps are enforced by the service).
2. On success the baby's artwork is sent and the FSM waits for a name:
   ``PetStates.waiting_for_name`` keeps the new ``pet_id`` in the state data.
3. The next text message is validated and stored as ``UserPet.custom_name``;
   an invalid name keeps the state so the player can simply try again.

The hunt handler is registered before the naming handler so the aliases always
start a new hunt instead of being taken as a pet name.
"""

import html
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, Message

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import PET_TYPES, Character
from sib.services import pet_service
from sib.services.card_progression import card_attributes
from sib.services.player_cards import PlayerCardService
from sib.services.sib_claim import fa_digits

router = Router(name="pets")

ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "pets"

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
# Verbatim player-facing copy from the Phase 2 spec.
FAILED_TEXT = "🏹 تلاش برای شکار ناموفق بود! شانس شما: {chance}٪. دوباره تلاش کنید."
SUCCESS_CAPTION = (
    "🎉 شما یک {type_fa} بچه شکار کردید!\n"
    "حالا یک اسم براش انتخاب کنید و روی همین پیام ریپلای کنید:"
)
NAMED_TEXT = (
    "✅ اسم {name} برای پت شما ثبت شد! "
    "از این به بعد با دستور «سیب پت» می‌تونید مدیریتش کنید."
)
NO_PET_TEXT = "🐾 پت موردنظر پیدا نشد. دوباره «سیب شکار» را امتحان کنید."

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


async def _send_photo(message: Message, image: Path, caption: str):
    """Reply with the baby artwork, falling back to text if it is missing."""
    if image.is_file():
        return await message.reply_photo(
            FSInputFile(str(image)), caption=caption, parse_mode="HTML"
        )
    logger.warning("missing pet artwork: %s", image)
    return await message.reply(caption, parse_mode="HTML")


# ---------------------------------------------------------------------------
# «سیب شکار» — hunt for a baby pet
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
            result = await pet_service.hunt_pet(session, sender.id, card_power)
            # Read everything the reply needs before the session closes.
            status = result.get("status")
            pet_id = None
            pet_type = result.get("pet_type")
            if status == "success":
                pet_id = int(result["pet"].id)
                pet_type = result["pet"].pet_type
    except Exception:
        logger.exception("pet hunt failed (user=%s)", sender.id)
        await message.reply(GENERIC_ERROR)
        return

    if status == "cooldown":
        await message.reply(
            COOLDOWN_TEXT.format(
                remaining=format_remaining(int(result.get("remaining_seconds") or 0))
            )
        )
        return

    if status == "limit":
        if result.get("reason") == "total":
            await message.reply(
                LIMIT_TOTAL_TEXT.format(
                    max_total=fa_digits(result.get("max_total") or pet_service.MAX_PETS_TOTAL)
                )
            )
        else:
            await message.reply(LIMIT_BABY_TEXT)
        return

    if status == "failed":
        await message.reply(
            FAILED_TEXT.format(chance=fa_digits(int(result.get("chance") or 0)))
        )
        return

    if status != "success" or pet_id is None:
        await message.reply(GENERIC_ERROR)
        return

    # A brand-new baby replaces any half-finished naming step.
    await state.clear()
    await state.set_state(PetStates.waiting_for_name)
    await state.update_data(pet_id=pet_id)
    # The species is the only value interpolated into a path: keep it inside
    # the known artwork set even if a row was written by hand.
    if pet_type not in PET_TYPES:
        pet_type = PET_TYPES[0]
    await _send_photo(
        message,
        ASSET_DIR / f"{pet_type}_baby.jpg",
        SUCCESS_CAPTION.format(type_fa=pet_service.pet_type_fa(pet_type)),
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
