"""Telegram handler and short animation for the Apple Rocket game."""

import asyncio
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, FSInputFile, InputMediaPhoto, Message

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services import rocket_service

router = Router(name="rocket")

ROCKET_PATTERN = r"^سیب\s+([0-9.]+)\s+([0-9]+)$"
_ROCKET_RE = re.compile(ROCKET_PATTERN)
ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "rocket"
LAUNCH_VIDEO = ASSET_DIR / "rocket_launch.mp4"
MAX_PROGRESS_EDITS = 4  # plus one final video-to-photo edit


def parse_rocket_command(text: str | None) -> tuple[Decimal, int] | None:
    """Parse ``سیب <multiplier> <bet>`` without accepting replies or aliases."""
    if not isinstance(text, str):
        return None
    match = _ROCKET_RE.fullmatch(text)
    if match is None:
        return None
    try:
        target = Decimal(match.group(1))
        bet = int(match.group(2))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if not target.is_finite():
        return None
    return target, bet


def _is_group(message: Message) -> bool:
    chat_type = getattr(getattr(message, "chat", None), "type", None)
    return getattr(chat_type, "value", chat_type) in ("group", "supergroup")


def _format_multiplier(value) -> str:
    try:
        result = format(Decimal(str(value)), "f")
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result


def _final_caption(game: dict, outcome: dict) -> str:
    crash_point = float(game["crash_point"])
    if outcome.get("status") == "win":
        target = _format_multiplier(game["target"])
        payout = int(outcome["payout"])
        return (
            f"💥 راکت در {crash_point:.1f}x ترکید! 🏆 شما با هدف {target}x "
            f"برنده شدید. (+{payout} سیب)"
        )
    if outcome.get("status") == "loss":
        return f"💥 راکت در {crash_point:.1f}x ترکید! 😭 شما باختید."
    return (
        f"💥 راکت در {crash_point:.1f}x ترکید! "
        "نتیجه بازی ثبت نشد؛ لطفاً پشتیبانی را خبر کن."
    )


async def _settle_round(user_id: int, game: dict) -> dict:
    async with get_session() as session:
        return await rocket_service.settle_rocket(
            session,
            user_id,
            game_id=game["game_id"],
            bet_amount=game["bet"],
            target_multiplier=game["target"],
            crash_point=game["crash_point"],
        )


async def run_rocket_animation(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    user_id: int,
    game: dict,
    sleep=asyncio.sleep,
) -> str:
    """Animate up to four caption updates, settle, then swap in the crash art.

    Telegram edit failures (including a deleted message) only disable further
    edits; the round is still settled so presentation cannot affect the wallet.
    ``sleep`` is injectable to keep handler tests fast.
    """
    crash_point = max(1.0, float(game["crash_point"]))
    current = 1.0
    step = (crash_point - current) / MAX_PROGRESS_EDITS
    progress_edits = 0
    message_editable = True
    last_caption = "🚀 در حال اوج‌گیری... (1.0x)"

    while current < crash_point and progress_edits < MAX_PROGRESS_EDITS:
        await sleep(1)
        progress_edits += 1
        current = (
            crash_point
            if progress_edits == MAX_PROGRESS_EDITS
            else min(crash_point, current + step)
        )
        if not message_editable:
            break
        progress_caption = f"🚀 در حال اوج‌گیری... ({current:.1f}x)"
        if progress_caption == last_caption:
            continue
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=progress_caption,
                parse_mode=None,
            )
            last_caption = progress_caption
        except Exception:
            logger.info(
                "Apple Rocket progress edit failed (chat=%s message=%s)",
                chat_id,
                message_id,
                exc_info=True,
            )
            message_editable = False
            break

    try:
        outcome = await _settle_round(user_id, game)
    except Exception:
        logger.exception(
            "Apple Rocket settlement failed (user=%s game=%s)",
            user_id,
            game.get("game_id"),
        )
        outcome = {"status": "error"}

    caption = _final_caption(game, outcome)
    if message_editable:
        try:
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaPhoto(
                    media=BufferedInputFile(
                        rocket_service.generate_boom_image(crash_point),
                        filename="rocket_boom.jpg",
                    ),
                    caption=caption,
                    parse_mode=None,
                ),
            )
            return caption
        except Exception:
            logger.info(
                "Apple Rocket crash media edit failed (chat=%s message=%s)",
                chat_id,
                message_id,
                exc_info=True,
            )

    # If the message was deleted or cannot be edited, still show the settled
    # result once as text. This never affects the wallet outcome.
    try:
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode=None)
    except Exception:
        logger.info(
            "Apple Rocket final text fallback failed (chat=%s)",
            chat_id,
            exc_info=True,
        )
    return caption


def _refusal_text(result: dict) -> str:
    status = result.get("status")
    if status == "invalid_target":
        return "ضریب هدف باید بین 1.1x تا 100x باشد."
    if status == "invalid_bet":
        minimum = result.get("min_bet", 50)
        maximum = result.get("max_bet", 5000)
        return f"مبلغ شرط باید بین {minimum} تا {maximum} سیب باشد."
    if status == "insufficient_funds":
        return "موجودی کافی نیست. 🍎"
    if status == "cooldown":
        remaining = result.get("remaining_seconds", 1)
        return (
            f"⏳ کول‌داون فعال است؛ {remaining} ثانیه دیگر تلاش کن."
        )
    if status == "not_found":
        return "ابتدا با /start ثبت‌نام کن."
    return (
        "بازی راکت فعلاً در دسترس نیست؛ کمی بعد دوباره تلاش کن."
    )


@router.message(F.text.regexp(ROCKET_PATTERN))
async def handle_rocket(message: Message, bot: Bot) -> None:
    """Start a group round from ``سیب 2.5 200`` (never from a reply)."""
    if not _is_group(message) or message.reply_to_message is not None:
        return
    sender = message.from_user
    if sender is None or getattr(sender, "is_bot", False):
        return

    parsed = parse_rocket_command(message.text)
    if parsed is None:
        await message.reply(
            "فرمت دستور درست نیست. نمونه: سیب 2.5 200", parse_mode=None
        )
        return
    target, bet = parsed

    try:
        async with get_session() as session:
            game = await rocket_service.play_rocket(
                session,
                sender.id,
                target_multiplier=target,
                bet_amount=bet,
            )
    except Exception:
        logger.exception("Apple Rocket could not start (user=%s)", sender.id)
        await message.reply(
            "بازی راکت فعلاً در دسترس نیست؛ کمی بعد دوباره تلاش کن.",
            parse_mode=None,
        )
        return

    if game.get("status") != "started":
        await message.reply(_refusal_text(game), parse_mode=None)
        return

    try:
        launch_message = await message.reply_video(
            video=FSInputFile(str(LAUNCH_VIDEO)),
            caption="🚀 در حال اوج‌گیری... (1.0x)",
            parse_mode=None,
        )
    except Exception:
        # A send timeout can occur after Telegram accepted the upload. The
        # idempotent settlement key makes it safe to resolve even if the
        # caller cannot learn the message id and show the animation.
        logger.exception("Apple Rocket launch video send failed (user=%s)", sender.id)
        try:
            outcome = await _settle_round(sender.id, game)
            await message.reply(_final_caption(game, outcome), parse_mode=None)
        except Exception:
            logger.exception("Apple Rocket fallback settlement failed (user=%s)", sender.id)
        return

    await run_rocket_animation(
        bot,
        chat_id=launch_message.chat.id,
        message_id=launch_message.message_id,
        user_id=sender.id,
        game=game,
    )
