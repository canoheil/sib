"""The Telegram command menu (the «/» button in every chat).

Until now the bot never called ``set_my_commands`` — the README told operators
to set the list by hand in BotFather — so a brand-new player who pressed the
«/» button saw an empty menu and had to guess that the game is played with
Persian phrases like «سیب» and «سیب باغ». This module registers the same short
public list from code, once at startup.

Only commands that exist in ``handlers/`` are listed; admin-only commands
(``/admin``, ``/daily_digest``, ``/users``, ``/broadcast``, ``/setasset``,
``/inv2_*``) are
deliberately absent — they are announced to admins through the admin centre,
never to players.
"""

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeDefault

from sib.core.logging import logger

#: Player-facing commands, in the order Telegram shows them.
PUBLIC_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "🍏 شروع بازی و منوی اصلی"),
    ("games", "🎮 بازی‌ها و پارتی"),
    ("battle", "⚔️ نبرد سیبی"),
    ("mine", "⛏️ معدن سیب"),
    ("chest", "🎁 صندوق روزانه"),
    ("profile", "👤 پروفایل من"),
    ("cards", "🃏 کارت‌های من"),
    ("garden", "🌳 باغ من"),
    ("shop", "🛒 فروشگاه"),
    ("trade", "🤝 مبادله کارت"),
    ("leaderboard", "🏆 جدول رتبه‌بندی"),
    ("league", "🛡️ لیگ هفتگی گروه‌ها"),
    ("wiki", "📚 ویکی کاراکترها"),
    ("help", "❓ راهنمای بازی"),
    ("disease", "🦠 وضعیت بیماری و درمان"),
    ("doctor", "🩺 پنل پزشک روز"),
    ("wanted", "⭐ وضعیت تعقیب و جرم"),
    ("use_antidote", "💊 مصرف پادزهر"),
)


async def register_bot_commands(bot: Bot) -> bool:
    """Publish the player command menu; never fail startup over it.

    Returns True when Telegram accepted the list. A failure (revoked token
    scope, an old API build, a network blip) is logged and swallowed: the bot
    still works, the menu is just not published.
    """
    try:
        commands = [BotCommand(command=name, description=description) for name, description in PUBLIC_COMMANDS]
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    except Exception:  # pragma: no cover - depends on Telegram, not on us
        logger.warning("could not publish the bot command menu", exc_info=True)
        return False
    return True
