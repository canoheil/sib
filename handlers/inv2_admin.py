"""Admin-only offline artwork preview and read-only HP simulation."""
import asyncio
import hashlib

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from sib.admin.auth import IsAdminFilter
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Character
from sib.services.character_art import artwork_path
from sib.services.config_store import ConfigService, DEFAULT_INVASION_DIFFICULTY_MULTIPLIER
from sib.services.invasion_v2.enemy import compute_enemy_hp, enemy_spec
from sib.services.invasion_v2.enums import EnemyType
from sib.services.invasion_v2.farsi import fa_number
from sib.services.invasion_v2.imaging.cache import cached_image
from sib.services.invasion_v2.imaging import (
    compose_announcement_image, compose_join_image, compose_result_image,
)
from sib.services.invasion_v2.imaging.compose import load_card_image_bytes
from sib.services.invasion_v2.power_resolver_impl import DbGroupPowerResolver
from sib.services.invasion_v2.presentation import _telegram
from sib.services.player_cards import PlayerCardService

router = Router(name="inv2_admin")
router.message.filter(IsAdminFilter())


def _private(message: Message) -> bool:
    chat_type = getattr(getattr(message, "chat", None), "type", None)
    return getattr(chat_type, "value", chat_type) == "private"


@router.message(Command("inv2_preview"))
async def preview(message: Message) -> None:
    if not _private(message):
        return  # Never reply or send an album to a group, even for an admin.
    try:
        args = (message.text or "").split()
        enemy = EnemyType(args[1]) if len(args) == 2 else None
        if enemy is None:
            raise ValueError("type")
        card_path = None
        async with get_session() as session:
            card = await PlayerCardService.get_main_card(session, message.from_user.id)
            character = await session.get(Character, card.character_id) if card else None
            if character:
                card_path = artwork_path(character)
                if card_path is None:
                    raise FileNotFoundError(f"Main Card local artwork missing for {character.code}")
        region, _faction = "درّه ماه", "فرزندان ماه"
        card_bytes = await asyncio.to_thread(load_card_image_bytes, card_path,
                                             placeholder=card_path is None)
        announcement_args = (enemy.value, region, enemy_spec(enemy).name_fa)
        # Explicit preview-only example HP; no invasion row or reward is created.
        join_args = (enemy.value, card_bytes, message.from_user.first_name, 42, False, 958)
        specs = [
            (("compose-v3", "preview", "announcement", *announcement_args),
                lambda: compose_announcement_image(*announcement_args)),
            (("compose-v3", "preview", "join", message.from_user.id, join_args[0],
              hashlib.sha256(card_bytes).hexdigest(), *join_args[2:]),
                lambda: compose_join_image(*join_args)),
            (("compose-v3", "preview", "victory", enemy.value, region),
                lambda: compose_result_image("victory", region, enemy.value, ())),
            (("compose-v3", "preview", "defeat", enemy.value, region),
                lambda: compose_result_image("defeat", region, enemy.value, ())),
        ]
        paths = [await asyncio.to_thread(cached_image, key, render) for key, render in specs]
        album = [InputMediaPhoto(media=FSInputFile(path), caption=caption, parse_mode=None) for path, caption in zip(
            paths, ("⚔️ فراخوان نگهبانان", "⚔️ ضربهٔ نگهبان", "🏆 پیروزی", "🌑 شکست"))]
        # Admin private chat: use the same exception-safe Telegram wrapper, but
        # no GROUP circuit state exists to mutate; preview performs no DB writes.
        await _telegram(None, message.answer_media_group, media=album)
    except (ValueError, IndexError):
        species = [f"{enemy.value} ({enemy_spec(enemy).name_fa.removeprefix('شیاسیب ')})"
                   for enemy in EnemyType]
        await _telegram(None, message.answer,
                        text="گونهٔ دشمن را بنویسید: " + "، ".join(species[:-1]) + " یا " + species[-1] + ".")
    except Exception:
        logger.exception("inv2 admin preview failed")
        await _telegram(None, message.answer, text="پیش‌نمایش آماده نشد؛ تصویر کارت یا قلم فارسی را بررسی کنید.")


async def simulate(message: Message) -> None:
    if not _private(message):
        return
    try:
        args = (message.text or "").split()
        if len(args) != 4:
            raise ValueError("args")
        group_id = int(args[1])
        enemy = EnemyType(args[2])
        count = int(args[3])
        if not 1 <= count <= 10000:
            raise ValueError("count")
        async with get_session() as session:
            average = await DbGroupPowerResolver().average_main_card_power(group_id=group_id, session=session)
            difficulty = await ConfigService.get(session, "invasion_difficulty_multiplier")
        if average is None:
            await _telegram(None, message.answer, text="توان کارت‌های اصلی این گروه نامشخص است؛ شبیه‌سازی ایمن ممکن نیست.")
            return
        hp = compute_enemy_hp(average, enemy, float(difficulty or DEFAULT_INVASION_DIFFICULTY_MULTIPLIER))
        low, high = round(average * .85), round(average * 1.15 * 2)
        likely = "محتمل" if count * average >= hp else "دور از انتظار"
        await _telegram(None, message.answer, text=(
            f"⚔️ پیش‌بینی نبرد، بدون ثبت در پایگاه داده\n"
            f"جان دشمن: {fa_number(hp)}\nدامنهٔ ضربهٔ هر نگهبان: {fa_number(low)} تا {fa_number(high)}\n"
            f"با {fa_number(count)} نگهبان، پیروزی {likely} است؛ این تنها برآورد است."))
    except (ValueError, IndexError):
        await _telegram(None, message.answer, text="نمونه: /inv2_simulate -100 soldier 5")
    except Exception:
        logger.exception("inv2 simulation failed")
        await _telegram(None, message.answer, text="شبیه‌سازی ممکن نشد؛ داده‌ها را بررسی کنید.")
