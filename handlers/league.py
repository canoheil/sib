"""«🏆 لیگ هفته» — the weekly group competition panel.

Two entry points, one rendering: the ``/league`` command and the
«🏆 لیگ هفته» button (on the leaderboard panel). In a group the panel also
shows where *this* group stands; in private chat it shows the table and how to
join the race («بات را به گروهت اضافه کن»).

The panel never writes anything: all scoring is derived from finished matches
(see ``services/group_league.py``), so opening it a hundred times costs one
bounded query and changes nothing.
"""

from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.keyboards.league import get_league_keyboard
from sib.services.group_league import (
    GroupLeagueService,
    format_panel,
    week_key,
)

router = Router(name="league")

LEAGUE_DISABLED_TEXT = (
    "🏆 **لیگ هفتگی گروه‌ها**\n\n"
    "لیگ فعلاً غیرفعاله. جدول رتبه‌بندی همیشگی از دکمهٔ پایین در دسترسه."
)
LEAGUE_ERROR_TEXT = (
    "🏆 **لیگ هفتگی گروه‌ها**\n\n"
    "خوندن جدول لیگ این لحظه ممکن نشد. چند لحظه بعد دوباره امتحان کن."
)
PRIVATE_INVITE_LINE = (
    "ℹ️ برای رقابت در لیگ، بات را به گروهت اضافه کن و اونجا `/league` رو بزن."
)


def _is_group_chat(chat) -> bool:
    return getattr(chat, "type", None) in ("group", "supergroup")


async def _render_panel(chat) -> tuple[str, InlineKeyboardMarkup]:
    """Build the league screen for a chat (group or private)."""
    now = datetime.now(timezone.utc)
    is_group = _is_group_chat(chat)
    key = week_key(now)

    async with get_session() as session:
        conf = await GroupLeagueService.settings(session)
        if not conf["enabled"]:
            return LEAGUE_DISABLED_TEXT, get_league_keyboard(
                is_group=is_group, enabled=False
            )

        rows = await GroupLeagueService.standings(session, key)
        own = None
        group_id = getattr(chat, "id", None)
        if is_group and group_id:
            own = next((row for row in rows if row.group_id == group_id), None)

        podium = await GroupLeagueService.recent_podium(session, weeks=1)

    text = format_panel(
        now=now,
        key=key,
        rows=rows,
        own=own,
        rewards=conf["rewards"],
        last_week=podium[0].week_key if podium else None,
        last_awards=podium,
        is_group=is_group,
        min_points=conf["min_points"],
    )
    if not is_group:
        text = f"{text}\n\n{PRIVATE_INVITE_LINE}"
    return text, get_league_keyboard(is_group=is_group, enabled=True)


@router.message(Command("league"))
async def cmd_league(message: Message, bot: Bot) -> None:
    """«/league» — show the weekly league table."""
    from sib.services.banner import send_screen_reply

    try:
        text, keyboard = await _render_panel(message.chat)
    except Exception:
        logger.exception("league panel failed chat=%s", getattr(message.chat, "id", None))
        text, keyboard = LEAGUE_ERROR_TEXT, get_league_keyboard(enabled=False)

    await send_screen_reply(message, "league", text, keyboard)


@router.callback_query(F.data == "league_open")
async def callback_league(callback: CallbackQuery, bot: Bot) -> None:
    """Open (or refresh) the league panel from a button."""
    from sib.services.banner import render_banner

    chat = callback.message.chat if callback.message else None
    try:
        text, keyboard = await _render_panel(chat)
    except Exception:
        logger.exception("league panel failed chat=%s", getattr(chat, "id", None))
        text, keyboard = LEAGUE_ERROR_TEXT, get_league_keyboard(enabled=False)

    await callback.answer()
    if callback.message is not None:
        await render_banner(callback, bot, "league", text, keyboard)
