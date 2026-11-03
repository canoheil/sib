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
from sib.core.text import format_html, strip_bold
from sib.database.connection import get_session
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services.apple_mine import (
    MAX_PICKAXE_LEVEL,
    WORK_COOLDOWN_SECONDS,
    CooldownError,
    PickaxeInsufficientFunds,
    PickaxeMaxLevelError,
    RewardRow,
    ShiftResult,
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
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import (
    APPLE_MINE_WORK_BUTTON_SUPPORTS_STYLE,
    mine_callback_data,
    safe_answer_callback,
    safe_edit_callback_message,
    safe_edit_message_caption,
    safe_edit_message_media,
    work_button_kb,
)
from sib.services.user import UserService

router = Router(name="apple_mine")

MINE_COMMAND_PATTERN = r"^\s*معدن\s+سیبی\s*$"
MINE_IMAGE_PATH = Path(__file__).resolve().parents[1] / "assets" / "apple_mine" / "mine.jpg"

MINE_FAILURE_TEXT = "معدن اکنون در دسترس نیست. کمی بعد دوباره تلاش کن."
MINE_NOT_OWNER_TEXT = "⚠️ این دکمه برای شما نیست!"
GOLDEN_HOUR_MINE_HINT = (
    "🕗 ساعت طلایی تهران (۲۰:۰۰ تا پیش از ۲۱:۰۰): سیب معمولی دوبرابر می‌شود."
)
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
# Captions (Persian, Markdown, fa_digits)
# ---------------------------------------------------------------------------

def _range_text(row: RewardRow) -> str:
    return f"{fa_digits(row.min_apples)} تا {fa_digits(row.max_apples)}"


def _pct_text(value: float) -> str:
    number = int(value) if float(value).is_integer() else value
    return f"{fa_digits(number)}٪"


def _stats_lines(row: RewardRow, *, bold_values: bool = False) -> list[str]:
    extraction = f"{_range_text(row)} سیب"
    chance = _pct_text(row.special_pct)
    if bold_values:
        extraction = f"**{extraction}**"
        chance = f"**{chance}**"
    return [
        f"⛏ استخراج: {extraction}",
        f"✨ شانس سیب ویژه: {chance}",
    ]


def _panel_caption(view: MineView, user_id: int) -> str:
    lines = [
        "⛏ **معدن سیبی**",
        f"🔨 کلنگ: **سطح {fa_digits(view.level)}**",
        f"🎁 هر شیفت: **{_range_text(view.reward)} سیب**",
        f"✨ شانس سیب ویژه: **{_pct_text(view.reward.special_pct)}**",
        GOLDEN_HOUR_MINE_HINT,
        "",
    ]
    if view.cooldown > 0:
        minutes, seconds = divmod(max(0, int(view.cooldown)), 60)
        cooldown = f"{fa_digits(minutes)} دقیقه و {fa_digits(seconds)} ثانیه"
        lines.append(f"⏳ شیفت بعدی: **{cooldown} دیگه**.")
    else:
        lines.append("✅ **معدن آماده‌ست! کلنگ بزن.**")
    return "\n".join(lines)


def _pickaxe_caption(view: MineView) -> str:
    lines = ["🔨 **ارتقای کلنگ**", ""]
    if view.level >= MAX_PICKAXE_LEVEL:
        lines.extend(
            [
                f"🏆 **کلنگ به سطح {fa_digits(MAX_PICKAXE_LEVEL)} رسید!**",
                "",
            ]
        )

    lines.extend(
        [
            f"📊 سطح فعلی: **{fa_digits(view.level)} از {fa_digits(MAX_PICKAXE_LEVEL)}**",
            *_stats_lines(view.reward, bold_values=True),
        ]
    )
    if view.level < MAX_PICKAXE_LEVEL and view.next_reward is not None:
        lines.extend(
            [
                "",
                f"⬆️ **سطح {fa_digits(view.level + 1)}:**",
                *_stats_lines(view.next_reward, bold_values=True),
                "",
                f"💰 هزینه ارتقا: **{fa_digits(view.upgrade_cost)} سیب**",
                f"🍎 موجودی شما: **{fa_digits(view.balance)} سیب**",
            ]
        )
    else:
        lines.extend(["", f"🍎 موجودی شما: **{fa_digits(view.balance)} سیب**"])
    lines.extend(["", GOLDEN_HOUR_MINE_HINT])
    return "\n".join(lines)


def _result_caption(result: ShiftResult) -> str:
    reward_types = {
        "apple_normal": "معمولی",
        "apple_gold": "طلایی",
        "apple_diamond": "الماسی",
        "apple_obsidian": "ابسیدینی",
    }
    reward_type = reward_types.get(result.item_code, "ویژه")
    minutes = WORK_COOLDOWN_SECONDS // 60
    return (
        f"{result.narrative}\n"
        f"⛏ **{fa_digits(result.amount)} سیب {reward_type}** گیرت اومد!\n"
        + ("🌿 یک گیاه ویژه پیدا کردی!\n" if result.plant_dropped else "")
        + f"🔨 سطح کلنگ: **{fa_digits(result.pickaxe_level)}**؛ "
        f"شیفت بعدی: **{fa_digits(minutes)} دقیقه دیگه**"
    )


def _insufficient_text(error: PickaxeInsufficientFunds) -> str:
    return f"🍎 سیب کافی نداری؛ **{fa_digits(error.missing)} سیب** دیگه لازم داری."


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Work (green) + pickaxe upgrade (blue), stacked in separate rows."""
    work_button = work_button_kb(
        supports_style=APPLE_MINE_WORK_BUTTON_SUPPORTS_STYLE,
        user_id=user_id,
    ).inline_keyboard[0][0]
    pickaxe_button = primary_button(
        text="🔨 ارتقا کلنگ",
        callback_data=mine_callback_data("mine:pickaxe", user_id),
    )
    return InlineKeyboardMarkup(inline_keyboard=[[work_button], [pickaxe_button]])


def _pickaxe_keyboard(view: MineView, user_id: int) -> InlineKeyboardMarkup:
    rows: list[list] = []
    if view.level < MAX_PICKAXE_LEVEL:
        rows.append(
            [
                success_button(
                    text=f"🍏 ارتقا به سطح {fa_digits(view.level + 1)}",
                    callback_data=mine_callback_data("mine:pickaxe:upgrade", user_id),
                )
            ]
        )
    rows.append(
        [
            danger_button(
                text="🔙 بازگشت",
                callback_data=mine_callback_data("mine:pickaxe:back", user_id),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

def _callback_owner_id(data: Optional[str]) -> Optional[int]:
    """Owner id embedded at the end of a mine callback (``mine:work:123``)."""
    if not data:
        return None
    owner = data.rsplit(":", 1)[-1]
    if not owner.isascii() or not owner.isdigit():
        return None
    return int(owner)


async def _reject_foreign_click(callback: CallbackQuery) -> bool:
    """Answer and reject a mine callback that does not belong to the presser.

    Telegram does not restrict who may press an inline button, so the owner
    id baked into the button data is the authorization boundary. Legacy
    buttons (no id) are rejected too; stale panels must be reopened.
    """
    presser = callback.from_user
    if presser is not None and _callback_owner_id(callback.data) == presser.id:
        return False
    await safe_answer_callback(callback, MINE_NOT_OWNER_TEXT, show_alert=True)
    return True


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
        await message.answer(format_html(text), reply_markup=reply_markup,
                             parse_mode="HTML")
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
                text=format_html(caption),
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        return await safe_edit_message_caption(
            bot,
            chat_id,
            message_id,
            format_html(caption),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    return await safe_edit_callback_message(
        callback,
        bot,
        format_html(caption),
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


async def _send_callback_message(callback: CallbackQuery, bot: Bot, text: str) -> None:
    message = callback.message
    try:
        if message is not None and hasattr(message, "answer"):
            await message.answer(format_html(text), parse_mode="HTML")
        else:
            await bot.send_message(
                callback.from_user.id, format_html(text), parse_mode="HTML"
            )
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

    keyboard = _main_keyboard(user.id)
    caption = _panel_caption(view, user.id)
    try:
        await message.answer_photo(
            photo=FSInputFile(str(MINE_IMAGE_PATH)),
            caption=format_html(caption),
            reply_markup=keyboard,
            parse_mode="HTML",
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


@router.callback_query(F.data.regexp(r"^mine:work(?::[0-9]+)?$"))
async def handle_mine_work(callback: CallbackQuery, bot: Bot) -> None:
    """Run the presser's shift directly (no card selection since M2-RECON)."""
    if await _reject_foreign_click(callback):
        return
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
                strip_bold(cooldown_message(cooldown, user.id)),
                show_alert=True,
            )
            return

        group_id = _group_id_for_chat(getattr(callback.message, "chat", None))
        async with get_session() as session:
            result = await perform_shift(session, user_id=user.id, group_id=group_id)
    except CooldownError as error:
        await safe_answer_callback(
            callback,
            strip_bold(cooldown_message(error.remaining_seconds, user.id)),
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


@router.callback_query(F.data.regexp(r"^mine:pickaxe(?::[0-9]+)?$"))
async def handle_mine_pickaxe(callback: CallbackQuery, bot: Bot) -> None:
    """Swap the pressed mine panel for the pickaxe screen, in place."""
    if await _reject_foreign_click(callback):
        return
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
            _pickaxe_keyboard(view, user.id),
            image_path=_pickaxe_image_path(view.level),
        ):
            await _send_callback_message(callback, bot, caption)
    except Exception:
        logger.exception("Apple Mine pickaxe rendering failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)


@router.callback_query(F.data.regexp(r"^mine:pickaxe:upgrade(?::[0-9]+)?$"))
async def handle_mine_pickaxe_upgrade(callback: CallbackQuery, bot: Bot) -> None:
    """Pay for one upgrade step and re-render with the new level's art."""
    if await _reject_foreign_click(callback):
        return
    user = callback.from_user
    if user is None:
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        async with get_session() as session:
            try:
                await upgrade_pickaxe(session, user_id=user.id)
            except PickaxeInsufficientFunds as error:
                await safe_answer_callback(
                    callback, strip_bold(_insufficient_text(error)), show_alert=True
                )
                return
            except PickaxeMaxLevelError:
                # Stale screen (the pickaxe already moved): re-render as-is.
                pass
            view = await _read_mine_view(session, user.id)
    except Exception:
        logger.exception("Apple Mine pickaxe upgrade failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
        return

    try:
        await safe_answer_callback(callback)
        caption = _pickaxe_caption(view)
        if not await _edit_screen(
            callback,
            bot,
            caption,
            _pickaxe_keyboard(view, user.id),
            image_path=_pickaxe_image_path(view.level),
        ):
            await _send_callback_message(callback, bot, caption)
    except Exception:
        logger.exception("Apple Mine upgrade rendering failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)


@router.callback_query(F.data.regexp(r"^mine:pickaxe:back(?::[0-9]+)?$"))
async def handle_mine_pickaxe_back(callback: CallbackQuery, bot: Bot) -> None:
    """Return to the main mine panel, editing the same message back."""
    if await _reject_foreign_click(callback):
        return
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
            _main_keyboard(user.id),
            image_path=MINE_IMAGE_PATH,
        ):
            await _send_callback_message(callback, bot, caption)
    except Exception:
        logger.exception("Apple Mine back rendering failed for user=%s", user.id)
        await safe_answer_callback(callback, MINE_FAILURE_TEXT, show_alert=True)
