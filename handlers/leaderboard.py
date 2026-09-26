"""Telegram handlers for Leaderboard and Statistics."""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery

from sib.database.connection import get_session
from sib.services.leaderboard import LeaderboardService
from sib.services.group import GroupService
from sib.services.user import UserService
from sib.keyboards.leaderboard import (
    get_leaderboard_keyboard,
    get_group_stats_keyboard,
    get_player_stats_keyboard,
)

router = Router(name="leaderboard")


def format_leaderboard_text(top_groups: list) -> str:
    """Format global leaderboard in Persian."""
    if not top_groups:
        return "🏆 **برترین گروه‌های سیب** 🍏\n\nهنوز مسابقه‌ای ثبت نشده است! اولین گروه قهرمان باشید!"

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 **برترین گروه‌های سیب** 🍏\n"]

    for rank, group, stats in top_groups:
        medal = medals.get(rank, f"**{rank}.**")
        lines.append(
            f"{medal} **{group.title}**\n"
            f"🏆 برد: `{stats.wins}` | ⚔️ مسابقات: `{stats.matches_played}` | "
            f"🔴 باخت: `{stats.losses}` | ⚪ مساوی: `{stats.draws}`"
        )
        lines.append("")  # empty spacing line

    return "\n".join(lines)


@router.message(Command("leaderboard", "top"))
async def cmd_leaderboard(message: Message):
    """Show global group leaderboard."""
    async with get_session() as session:
        top_groups = await LeaderboardService.get_top_groups(session, limit=10)
        text = format_leaderboard_text(top_groups)
        is_group = message.chat.type in ("group", "supergroup")
        await message.reply(text, reply_markup=get_leaderboard_keyboard(is_group=is_group), parse_mode="Markdown")


@router.callback_query(F.data == "show_leaderboard")
async def callback_show_leaderboard(callback: CallbackQuery):
    """Callback to display global leaderboard."""
    async with get_session() as session:
        top_groups = await LeaderboardService.get_top_groups(session, limit=10)
        text = format_leaderboard_text(top_groups)
        is_group = bool(callback.message and callback.message.chat.type in ("group", "supergroup"))
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(text, reply_markup=get_leaderboard_keyboard(is_group=is_group), parse_mode="Markdown")


@router.callback_query(F.data.in_(("group_rating_stats", "group_win_stats")))
async def callback_group_rating_stats(callback: CallbackQuery):
    """Show group win-based stats (legacy callback kept for old messages)."""
    chat = callback.message.chat if callback.message else None
    if not chat or chat.type not in ("group", "supergroup"):
        await callback.answer("این بخش مخصوص گروه‌هاست 🍏", show_alert=True)
        return

    async with get_session() as session:
        stats = await LeaderboardService.get_group_stats_and_rank(session, chat.id)
        if not stats:
            await GroupService.get_or_create_group(session, chat)
            stats = await LeaderboardService.get_group_stats_and_rank(session, chat.id)

        text = (
            f"📊 **شناسنامه گروه: {stats['title']}** 🍏\n\n"
            f"🏆 رتبه در جدول کل: `#{stats['rank']}`\n"
            f"🏆 تعداد بردها: `{stats['wins']}`\n"
            f"⚔️ تعداد کل مسابقات: `{stats['matches_played']}`\n"
            f"🟢 پیروزی‌ها: `{stats['wins']}` ({stats['win_rate']}٪)\n"
            f"🔴 شکست‌ها: `{stats['losses']}`\n"
            f"⚪ تساوی‌ها: `{stats['draws']}`"
        )
        await callback.answer()
        if callback.message:
            try:
                await callback.message.edit_text(
                    text,
                    reply_markup=get_group_stats_keyboard(),
                    parse_mode="Markdown",
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise


@router.message(Command("stats", "profile"))
async def cmd_stats(message: Message):
    """Show stats: group stats if in group, or personal player stats if in private."""
    user = message.from_user
    chat = message.chat

    if not user:
        return

    async with get_session() as session:
        if chat.type in ("group", "supergroup"):
            # Group stats
            stats = await LeaderboardService.get_group_stats_and_rank(session, chat.id)
            if not stats:
                await GroupService.get_or_create_group(session, chat)
                stats = await LeaderboardService.get_group_stats_and_rank(session, chat.id)

            text = (
                f"📊 **آمار گروه {chat.title}** 🍏\n\n"
                f"🏆 رتبه جهانی: `#{stats['rank']}`\n"
                f"🏆 تعداد بردها: `{stats['wins']}`\n"
                f"⚔️ مسابقات: `{stats['matches_played']}`\n"
                f"🟢 برد: `{stats['wins']}` ({stats['win_rate']}٪)\n"
                f"🔴 باخت: `{stats['losses']}`\n"
                f"⚪ تساوی: `{stats['draws']}`"
            )
            await message.reply(text, reply_markup=get_group_stats_keyboard(), parse_mode="Markdown")
        else:
            # Personal player stats in private chat
            p_stats = await LeaderboardService.get_player_stats(session, user.id)
            from sib.services.seedling import SeedlingService
            seedlings = await SeedlingService.get_total(session, user.id)
            name = user.first_name or "شما"
            text = (
                f"👤 **کارنامه بازیکن: {name}** 🍏\n\n"
                f"🎮 مجموع حضور در مسابقات: `{p_stats['matches_played']}`\n"
                f"🟢 پیروزی‌ها: `{p_stats['wins']}` ({p_stats['win_rate']}٪)\n"
                f"🔴 شکست‌ها: `{p_stats['losses']}`\n"
                f"⚪ تساوی‌ها: `{p_stats['draws']}`\n"
                f"🌱 نهال‌ها: `{seedlings}`\n\n"
                "ℹ️ جایگاه رقابتی سیب بر اساس برد گروه‌هاست؛ بازیکنان رکورد فردی خودشان را دارند."
            )
            await message.reply(text, reply_markup=get_player_stats_keyboard(), parse_mode="Markdown")
