"""Apple Battle (نبرد سیبی) handlers — Phases 14+15 UI.

Queue, matchmaking, narrated results, battle leaderboard and the
player's battle profile.
"""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.database.connection import get_session
from sib.database.models import AppleBattle, AppleBattleStatus, Character, PlayerCard
from sib.services.apple_battle import AppleBattleService, BattleError
from sib.services.player_cards import PlayerCardService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message

router = Router(name="apple_battle")


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _queue_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        cards = await PlayerCardService.list_cards(session, user_id)
        battle_count = await session.scalar(
            __import__("sqlalchemy").select(
                __import__("sqlalchemy").func.count(AppleBattle.id)
            ).where(
                ((AppleBattle.player1_id == user_id) | (AppleBattle.player2_id == user_id))
            )
        )

    if not cards:
        return (
            "🍎 **نبرد سیبی** ⚔️\n\n"
            "برای شرکت در نبرد به کارت نیاز دارید! ابتدا از فروشگاه 🏪 یک شخصیت بخرید.",
            _kb([[InlineKeyboardButton(text="🏪 فروشگاه", callback_data="shop_refresh")]]),
        )

    lines = [
        "🍎 **نبرد سیبی** ⚔️",
        "«یک مسابقه، یک برنده، یک نهال!»",
        "",
        f"کارت‌های شما: {len(cards)} | نبردهای ثبت‌شده: {battle_count or 0}",
        "",
        "کارت خود را برای ورود به صف انتخاب کنید (فقط هم‌سطح‌ها با هم می‌جنگند):",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for card in cards[:10]:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"⚔️ نبرد با کارت {card.id} (سطح {card.level})",
                    callback_data=f"ab_join:{card.id}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="🏆 لیدربورد نبرد", callback_data="ab_board")]
    )
    buttons.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="ab_refresh")])
    return "\n".join(lines), _kb(buttons)


@router.message(Command("battle"))
async def cmd_battle(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return
    text, kb = await _queue_view(user.id)
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "ab_refresh")
async def handle_ab_refresh(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    text, kb = await _queue_view(user.id)
    await safe_answer_callback(callback, "بروزرسانی شد ⚔️")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("ab_join:"))
async def handle_ab_join(callback: CallbackQuery, bot: Bot):
    card_id = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        try:
            await AppleBattleService.enqueue(session, user.id, card_id)
        except BattleError as e:
            await safe_answer_callback(callback, str(e), show_alert=True)
            return
        battle = await AppleBattleService.try_match(session)
        if battle is not None:
            battle = await AppleBattleService.compute_and_persist_result(session, battle.id)
            battle = await AppleBattleService.complete(session, battle.id)
            story = await AppleBattleService.narrate(session, battle.id)
            winner_id = battle.winner_id
            text = (
                "🍎 **نبرد سیبی — نتیجه** ⚔️\n\n"
                f"{story}\n\n"
                f"🏆 برنده: `{winner_id}` | 🌱 یک نهال به برنده تعلق گرفت!"
            )
            await safe_answer_callback(callback, "نبرد انجام شد!")
            await safe_edit_callback_message(callback, bot, text, parse_mode="Markdown")
            return

    await safe_answer_callback(
        callback, "در صف نبرد قرار گرفتید! به‌محض پیدا شدن حریف هم‌سطح، نبرد آغاز می‌شود. ⚔️",
        show_alert=True,
    )


@router.callback_query(F.data == "ab_board")
async def handle_ab_board(callback: CallbackQuery, bot: Bot):
    async with get_session() as session:
        board = await AppleBattleService.battle_leaderboard(session)
        names = []
        for user_id, wins, total in board[:15]:
            names.append(f"🏆 `{user_id}` — {wins} برد از {total} نبرد")

    text = "🏆 **لیدربورد نبرد سیبی**\n\n"
    text += "\n".join(names) if names else "هنوز نبردی ثبت نشده است."
    kb = _kb([[InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="ab_board")]])
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Profile (Phase 15)
# ---------------------------------------------------------------------------


@router.message(Command("profile"))
async def cmd_profile(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return

    from sqlalchemy import func, select

    from sib.services.apple_wallet import AppleWalletService
    from sib.services.seedling import SeedlingService

    async with get_session() as session:
        cards = await PlayerCardService.list_cards(session, user.id)
        seedlings = await SeedlingService.get_total(session, user.id)
        apples = await AppleWalletService.get_balance(session, user.id)
        wins = await session.scalar(
            select(func.count(AppleBattle.id))
            .where(AppleBattle.winner_id == user.id)
            .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
        )
        battles = await session.scalar(
            select(func.count(AppleBattle.id)).where(
                ((AppleBattle.player1_id == user.id) | (AppleBattle.player2_id == user.id))
                & (AppleBattle.status == AppleBattleStatus.COMPLETED)
            )
        )
        favorite = next((c for c in cards if c.is_favorite), None)
        fav_text = ""
        if favorite:
            character = await session.get(Character, favorite.character_id)
            name = character.name_fa if character else "?"
            fav_text = f"⭐ کارت اصلی: **{name}** (سطح {favorite.level})\n"

    lines = [
        f"👤 **پروفایل {user.full_name}**",
        "",
        f"🌱 نهال‌ها: `{seedlings}` | 🍎 سیب‌ها: `{apples}`",
        f"🎴 کارت‌ها: `{len(cards)}`",
        fav_text.strip() if fav_text else "⭐ کارت اصلی: انتخاب نشده",
        f"⚔️ نبردهای سیبی: `{battles or 0}` | برد: `{wins or 0}`",
    ]
    kb = _kb(
        [
            [InlineKeyboardButton(text="🎴 کارت‌های من", callback_data="ab_refresh")],
            [InlineKeyboardButton(text="🌳 باغ من", callback_data="garden_refresh")],
        ]
    )
    await message.reply("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
