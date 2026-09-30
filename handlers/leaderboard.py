"""Telegram handlers for Leaderboard and Statistics."""

from aiogram import Bot, Router, F
from aiogram.filters import Command
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
        return "🏆 **برترین گروه‌های سیب** 🍏\n\nهنوز مسابقه‌ای ثبت نشده! گروه شما می‌تونه اولین قهرمان باشه!"

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
        from sib.services.banner import send_screen_reply

        top_groups = await LeaderboardService.get_top_groups(session, limit=10)
        text = format_leaderboard_text(top_groups)
        is_group = message.chat.type in ("group", "supergroup")
        await send_screen_reply(
            message, "leaderboard", text, get_leaderboard_keyboard(is_group=is_group)
        )


@router.callback_query(F.data == "show_leaderboard")
async def callback_show_leaderboard(callback: CallbackQuery, bot: Bot):
    """Callback to display global leaderboard (own banner screen)."""
    from sib.services.banner import render_banner

    async with get_session() as session:
        top_groups = await LeaderboardService.get_top_groups(session, limit=10)
        text = format_leaderboard_text(top_groups)
        is_group = bool(callback.message and callback.message.chat.type in ("group", "supergroup"))
        await callback.answer()
        if callback.message:
            await render_banner(
                callback, bot, "leaderboard", text,
                get_leaderboard_keyboard(is_group=is_group),
            )


@router.callback_query(F.data.in_(("group_rating_stats", "group_win_stats")))
async def callback_group_rating_stats(callback: CallbackQuery, bot: Bot):
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
            from sib.services.telegram_ui import safe_edit_callback_message

            await safe_edit_callback_message(
                callback,
                bot,
                text,
                reply_markup=get_group_stats_keyboard(),
            )


@router.message(Command("stats"))
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
                f"⚔️ **مسابقه‌های واقعی**\n"
                f"بازی‌ها: `{p_stats['matches_played']}` | برد: `{p_stats['wins']}` | "
                f"باخت: `{p_stats['losses']}` | مساوی: `{p_stats['draws']}`\n\n"
                f"🤖 **بازی با سیب کوچولو**\n"
                f"بازی‌ها: `{p_stats['bot_matches_played']}` | برد: `{p_stats['bot_wins']}` | "
                f"باخت: `{p_stats['bot_losses']}` | مساوی: `{p_stats['bot_draws']}`\n\n"
                f"🌱 نهال‌ها: `{seedlings}`\n\n"
                "ℹ️ بازی با Bot روی آمار رقابتی گروه و نهال‌ها اثری نداره."
            )
            await message.reply(text, reply_markup=get_player_stats_keyboard(), parse_mode="Markdown")
