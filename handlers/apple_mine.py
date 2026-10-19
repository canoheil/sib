"""Apple Mine (معدن سیبی) Telegram screens and callbacks.

M2-RECON rework (docs/games/M2-RECON.md): card selection is gone. The main
menu has exactly two buttons — work (shift runs immediately, rewards scale
with the pickaxe level) and the pickaxe upgrade screen. Every screen change
edits the pressed message in place (no message spam).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.keyboards.common import danger_button, success_button
from sib.services.apple_mine import (
    MAX_PICKAXE_LEVEL,
    WORK_COOLDOWN_SECONDS,
    CooldownError,
    PickaxeInsufficientFunds,
    PickaxeMaxLevelError,
    RewardRow,
    ShiftResult,
    UpgradeResult,
    get_pickaxe_level,
    perform_shift,
    remaining_cooldown,
    reward_table_for_level,
    upgrade_cost_for_level,
    upgrade_pickaxe,
)
from sib.services.apple_mine_narrative import cooldown_message
from sib.services.apple_wallet import AppleWalletService
from sib.services.group import GroupService
from sib.services.items import item_emoji, item_label
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import (
    APPLE_MINE_WORK_BUTTON_SUPPORTS_STYLE,
    mine_menu_kb,
    safe_answer_callback,
    safe_edit_callback_message,
    safe_edit_message_caption,
    safe_edit_message_media,
)
from sib.services.user import UserService

router = Router(name="apple_mine")

MINE_COMMAND_PATTERN = r"^\s*معدن\s+سیبی\s*$"
MINE_IMAGE_PATH = Path(__file__).resolve().parents[1] / "assets" / "apple_mine" / "mine.jpg"

MINE_FAILURE_TEXT = "معدن اکنون در دسترس نیست. کمی بعد دوباره تلاش کن."
_EMPTY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])


def _pickaxe_image_path(level: int) -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "apple_mine"
        / f"pickaxe_{int(level)}.jpg"
    )


# ---------------------------------------------------------------------------
# Session state (display-only reads; mutations live in services/apple_mine)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MineView:
    """Everything one mine screen needs, computed from a single read."""

    level: int
    reward: RewardRow
    next_reward: Optional[RewardRow]
    upgrade_cost: Optional[int]
    balance: int
    cooldown: int


async def _read_mine_view(session, user_id: int) -> MineView:
    level = await get_pickaxe_level(session, user_id)
    reward = await reward_table_for_level(session, level)
    next_reward = (
        await reward_table_for_level(session, level + 1)
        if level < MAX_PICKAXE_LEVEL
        else None
    )
    return MineView(
        level=level,
        reward=reward,
        next_reward=next_reward,
        upgrade_cost=await upgrade_cost_for_level(session, level),
        balance=await AppleWalletService.get_balance(session, user_id),
        cooldown=await remaining_cooldown(session, user_id),
    )


# ---------------------------------------------------------------------------
# Captions (Persian, plain text, fa_digits — same policy as M1)
# ---------------------------------------------------------------------------

def _range_text(row: RewardRow) -> str:
    return f"{fa_digits(row.min_apples)} تا {fa_digits(row.max_apples)}"


def _pct_text(value: float) -> str:
    number = int(value) if float(value).is_integer() else value
    return f"{fa_digits(number)}٪"


def _stats_lines(row: RewardRow) -> list[str]:
    return [
        f"⛏ سیب معمولی هر شیفت: {_range_text(row)}",
        f"🍎 شانس سیب ویژه: {_pct_text(row.special_pct)}",
    ]


def _panel_caption(view: MineView, user_id: int) -> str:
    lines = [
        "⛏ معدن سیبی",
        "",
        "داخل معدن تاریک است. صدای چکش از دور می‌آید.",
        "",
        f"🔨 کلنگ شما: سطح {fa_digits(view.level)}",
        f"⛏ هر شیفت: {_range_text(view.reward)} سیب معمولی",
        f"🍎 شانس سیب ویژه: {_pct_text(view.reward.special_pct)}",
        "",
    ]
    if view.cooldown > 0:
        lines.append(cooldown_message(view.cooldown, user_id))
    else:
        lines.append("⏳ معدن آماده‌ی کار است!")
    return "\n".join(lines)


def _pickaxe_caption(view: MineView, *, success: bool = False) -> str:
    if success:
        lines = [f"🎉 کلنگ به سطح {fa_digits(view.level)} ارتقا یافت!", ""]
    else:
        lines = ["🔨 ارتقا کلنگ", ""]
        if view.level >= MAX_PICKAXE_LEVEL:
            lines.extend(
                [
                    f"🏆 کلنگ شما به بالاترین سطح ({fa_digits(MAX_PICKAXE_LEVEL)}) "
                    "رسیده است!",
                    "",
                ]
            )

    lines.extend(
        [
            f"📊 سطح فعلی: {fa_digits(view.level)} از {fa_digits(MAX_PICKAXE_LEVEL)}",
            *_stats_lines(view.reward),
        ]
    )
    if view.level < MAX_PICKAXE_LEVEL and view.next_reward is not None:
        lines.extend(
            [
                "",
                f"⬆️ با ارتقا به سطح {fa_digits(view.level + 1)}:",
                *_stats_lines(view.next_reward),
                "",
                f"💰 هزینه ارتقا: {fa_digits(view.upgrade_cost)} سیب 🍎",
                f"💰 سیب‌های شما: {fa_digits(view.balance)}",
            ]
        )
    elif success:
        lines.extend(["", f"💰 سیب‌های شما: {fa_digits(view.balance)}"])
    return "\n".join(lines)


def _result_caption(result: ShiftResult) -> str:
    if result.item_code == "apple_normal":
        emoji, display_name = "🍎", "سیب معمولی"
    else:
        emoji = item_emoji(result.item_code)
        display_name = item_label(result.item_code)
    minutes, seconds = divmod(WORK_COOLDOWN_SECONDS, 60)
    return (
        f"{result.narrative}\n\n"
        f"{emoji} +{fa_digits(result.amount)} {display_name}\n"
        f"🔨 کلنگ سطح {fa_digits(result.pickaxe_level)}\n"
        f"⏳ شیفت بعدی: {fa_digits(minutes)} دقیقه و {fa_digits(seconds)} ثانیه"
    )


def _insufficient_text(error: PickaxeInsufficientFunds) -> str:
    return f"سیب کافی نداری 🍎 به {fa_digits(error.missing)} سیب دیگر نیاز داری."


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _main_keyboard() -> InlineKeyboardMarkup:
    """Work (green) + pickaxe upgrade (blue), one row."""
    return mine_menu_kb(supports_style=APPLE_MINE_WORK_BUTTON_SUPPORTS_STYLE)


def _pickaxe_keyboard(view: MineView) -> InlineKeyboardMarkup:
    rows: list[list] = []
    if view.level < MAX_PICKAXE_LEVEL:
        rows.append(
            [
                success_button(
                    text=(
                        f"🍏 ارتقا به سطح {fa_digits(view.level + 1)} "
                        f"(هزینه: {fa_digits(view.upgrade_cost)} سیب)"
                    ),
                    callback_data="mine:pickaxe:upgrade",
                )
            ]
        )
    rows.append([danger_button(text="🔙 بازگشت", callback_data="mine:pickaxe:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

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


async def _edit_screen(
    callback: CallbackQuery,
    bot: Bot,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
    image_path: Optional[Path] = None,
) -> bool:
    """Edit the pressed message in place; True when it is guaranteed current.

    Photo messages keep their type: a new ``image_path`` swaps the photo
    (media edit), otherwise the caption is edited. Text messages (the M1
    image-fallback panels) are edited as text. A missing asset file degrades
    to a caption-only edit instead of breaking the screen.
    """
    message = callback.message
    if message is None:
        return False

    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    if not chat_id or not message_id:
        return False

    if getattr(message, "photo", None):
        if image_path is not None and image_path.exists():
            return await safe_edit_message_media(
                bot,
                chat_id,
                message_id,
                photo=FSInputFile(str(image_path)),
                text=caption,
                reply_markup=reply_markup,
                parse_mode=None,
            )
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


# ---------------------------------------------------------------------------
# Entry point and routes
# ---------------------------------------------------------------------------

async def _send_panel(message: Message) -> None:
    user = message.from_user
    if user is None:
        await _safe_message_reply(message, MINE_FAILURE_TEXT)
        return

    try:
        async with get_session() as session:
            await _register_user_and_group(session, user, message.chat)
            view = await _read_mine_view(session, user.id)
    except Exception:
        logger.exception("Apple Mine panel registration failed for user=%s", user.id)
        await _safe_message_reply(message, MINE_FAILURE_TEXT)
        return

    keyboard = _main_keyboard()
    caption = _panel_caption(view, user.id)
    try:
        await message.answer_photo(
            photo=FSInputFile(str(MINE_IMAGE_PATH)),
            caption=caption,
            reply_markup=keyboard,
            parse_mode=None,
        )
    except Exception:
        # Keep the feature usable if a local asset cannot be read or Telegram
        # rejects the photo, while never exposing an exception to the group.
        logger.exception("Apple Mine image panel failed; using a text panel")
        await _safe_message_reply(message, caption, reply_markup=keyboard)


@router.message(Command("mine"))
@router.message(F.text.regexp(MINE_COMMAND_PATTERN))
async def handle_mine_command(message: Message) -> None:
    """Open the mine panel from «معدن سیبی» or the /mine alias."""
    try:
        await _send_panel(message)
    except Exception:
        logger.exception("Apple Mine command failed")
        await _safe_message_reply(message, MINE_FAILURE_TEXT)


@router.callback_query(F.data == "mine:work")
async def handle_mine_work(callback: CallbackQuery, bot: Bot) -> None:
    """Run the presser's shift directly (no card selection since M2-RECON)."""
    user = callback.from_user
    if user is None:
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        # Session 1: registration + a cheap pre-check for the alert path.
        # The authoritative cooldown gate stays inside perform_shift's
        # user-row lock: a same-session pre-read would pin a REPEATABLE READ
        # snapshot and could miss a concurrent committed shift (M1 note).
        async with get_session() as session:
            await _register_user_and_group(
                session,
                user,
                getattr(callback.message, "chat", None),
            )
            cooldown = await remaining_cooldown(session, user.id)
    except Exception:
        logger.exception("Apple Mine work pre-check failed for user=%s", user.id)
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

        group_id = _group_id_for_chat(getattr(callback.message, "chat", None))
        async with get_session() as session:
            result = await perform_shift(session, user_id=user.id, group_id=group_id)
    except CooldownError as error:
        await safe_answer_callback(
            callback,
            cooldown_message(error.remaining_seconds, user.id),
            show_alert=True,
        )
        return
    except Exception:
        logger.exception("Apple Mine shift failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        await safe_answer_callback(callback)
        caption = _result_caption(result)
        if not await _edit_screen(callback, bot, caption, _EMPTY_KEYBOARD):
            await _send_callback_message(callback, bot, caption)
    except Exception:
        logger.exception("Apple Mine result rendering failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)


@router.callback_query(F.data == "mine:pickaxe")
async def handle_mine_pickaxe(callback: CallbackQuery, bot: Bot) -> None:
    """Swap the pressed mine panel for the pickaxe screen, in place."""
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
            view = await _read_mine_view(session, user.id)
    except Exception:
        logger.exception("Apple Mine pickaxe screen failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        await safe_answer_callback(callback)
        caption = _pickaxe_caption(view)
        if not await _edit_screen(
            callback,
            bot,
            caption,
            _pickaxe_keyboard(view),
            image_path=_pickaxe_image_path(view.level),
        ):
            await _send_callback_message(callback, bot, caption)
    except Exception:
        logger.exception("Apple Mine pickaxe rendering failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)


@router.callback_query(F.data == "mine:pickaxe:upgrade")
async def handle_mine_pickaxe_upgrade(callback: CallbackQuery, bot: Bot) -> None:
    """Pay for one upgrade step and re-render with the new level's art."""
    user = callback.from_user
    if user is None:
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    outcome: UpgradeResult | None = None
    try:
        async with get_session() as session:
            try:
                outcome = await upgrade_pickaxe(session, user_id=user.id)
            except PickaxeInsufficientFunds as error:
                await safe_answer_callback(
                    callback, _insufficient_text(error), show_alert=True
                )
                return
            except PickaxeMaxLevelError:
                # Stale screen (the pickaxe already moved): re-render as-is.
                outcome = None
            view = await _read_mine_view(session, user.id)
    except Exception:
        logger.exception("Apple Mine pickaxe upgrade failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        await safe_answer_callback(callback)
        caption = _pickaxe_caption(view, success=outcome is not None)
        if not await _edit_screen(
            callback,
            bot,
            caption,
            _pickaxe_keyboard(view),
            image_path=_pickaxe_image_path(view.level),
        ):
            await _send_callback_message(callback, bot, caption)
    except Exception:
        logger.exception("Apple Mine upgrade rendering failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)


@router.callback_query(F.data == "mine:pickaxe:back")
async def handle_mine_pickaxe_back(callback: CallbackQuery, bot: Bot) -> None:
    """Return to the main mine panel, editing the same message back."""
    user = callback.from_user
    if user is None:
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        async with get_session() as session:
            view = await _read_mine_view(session, user.id)
    except Exception:
        logger.exception("Apple Mine back navigation failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        await safe_answer_callback(callback)
        caption = _panel_caption(view, user.id)
        if not await _edit_screen(
            callback,
            bot,
            caption,
            _main_keyboard(),
            image_path=MINE_IMAGE_PATH,
        ):
            await _send_callback_message(callback, bot, caption)
    except Exception:
        logger.exception("Apple Mine back rendering failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
