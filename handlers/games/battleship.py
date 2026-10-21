"""Telegram gameplay handlers for Battleship.

Two surfaces (friendly matches):

* the GROUP message is a neutral public board; its only game action is
  «🎯 رادار من» (``bs_radar:{match_id}``) which opens a PRIVATE panel - an
  ephemeral message only the presser can see;
* the PRIVATE panel carries the shooting grid (``bs_shot:{match}:{r}:{c}``).

Normal (non-friendly) matches keep their per-group boards, where the radar is
private anyway because each team has its own chat.
"""

from typing import Any, Dict, Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Match
from sib.games.registry import get_game_engine
from sib.handlers.games.common import process_game_action
from sib.services.battleship_radar import (
    CLOSE_CALLBACK_PREFIX,
    LEGACY_SHOT_CALLBACK_PREFIX,
    SHOT_CALLBACK_PREFIX,
    RADAR_CALLBACK_PREFIX,
    close_panel,
    load_state,
    panel_alert_hint,
    parse_match_id,
    parse_shot,
    present_panel,
    supports_private_radar,
)
from sib.services.friendly import is_friendly
from sib.services.telegram_ui import safe_answer_callback

router = Router(name="game_battleship")

RADAR_UNAVAILABLE_TEXT = "این بازی رادار خصوصی ندارد."
RADAR_PARTICIPANTS_ONLY_TEXT = "فقط بازیکن‌های همین مسابقه می‌توانند رادار باز کنند."
RADAR_NO_MATCH_TEXT = "این مسابقه پیدا نشد."
RADAR_NOT_STARTED_TEXT = "این مسابقه هنوز شروع نشده."
INVALID_CALLBACK_TEXT = "درخواست نامعتبر است."


class _RadarContext:
    """Primitive snapshot of a match, usable after its DB session closes."""

    __slots__ = (
        "match_id",
        "game_code",
        "state",
        "player_ids",
        "group_id",
        "friendly",
    )

    def __init__(
        self,
        match_id: int,
        game_code: str,
        state: Optional[Dict[str, Any]],
        player_ids: Tuple[int, ...],
        group_id: Optional[int],
        friendly: bool,
    ) -> None:
        self.match_id = match_id
        self.game_code = game_code
        self.state = state
        self.player_ids = player_ids
        self.group_id = group_id
        self.friendly = friendly


async def _load_radar_context(match_id: int) -> Optional[_RadarContext]:
    """Read-only match snapshot (no lock, no Telegram call inside the session)."""
    async with get_session() as session:
        match = await session.scalar(
            select(Match)
            .where(Match.id == match_id)
            .options(selectinload(Match.players), selectinload(Match.game))
        )
        if match is None:
            return None
        return _RadarContext(
            match_id=match.id,
            game_code=match.game.code if match.game else "",
            state=load_state(match.game_state),
            player_ids=tuple(p.user_id for p in match.players),
            group_id=match.group1_id or match.group2_id,
            friendly=is_friendly(match),
        )


def _group_chat_id(callback: CallbackQuery, context: _RadarContext) -> Optional[int]:
    """Chat the ephemeral panel must be sent into (a group, never a DM)."""
    message = getattr(callback, "message", None)
    chat = getattr(message, "chat", None) if message is not None else None
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and getattr(chat, "type", None) in (
        "group",
        "supergroup",
    ):
        return int(chat_id)
    return context.group_id


@router.callback_query(F.data.startswith(RADAR_CALLBACK_PREFIX))
async def handle_bs_radar(callback: CallbackQuery, bot: Bot):
    """Open the caller's PRIVATE radar panel («🎯 رادار من»)."""
    user = callback.from_user
    match_id = parse_match_id(callback.data)
    if not user:
        return
    if match_id is None:
        await safe_answer_callback(
            callback, INVALID_CALLBACK_TEXT, show_alert=True
        )
        return

    context = await _load_radar_context(match_id)
    if context is None:
        await safe_answer_callback(
            callback, RADAR_NO_MATCH_TEXT, show_alert=True
        )
        return
    if not context.state:
        await safe_answer_callback(
            callback, RADAR_NOT_STARTED_TEXT, show_alert=True
        )
        return

    if not context.game_code or not context.friendly:
        # Normal matches still render a per-team board in their own group.
        await safe_answer_callback(
            callback, RADAR_UNAVAILABLE_TEXT, show_alert=True
        )
        return

    engine = get_game_engine(context.game_code)
    if not supports_private_radar(engine):
        await safe_answer_callback(
            callback, RADAR_UNAVAILABLE_TEXT, show_alert=True
        )
        return
    if user.id not in context.player_ids:
        # Spectators never receive a board: the radar belongs to one team.
        await safe_answer_callback(
            callback, RADAR_PARTICIPANTS_ONLY_TEXT, show_alert=True
        )
        return

    delivery = await present_panel(
        bot,
        callback,
        chat_id=_group_chat_id(callback, context),
        player_id=user.id,
        engine=engine,
        match_id=context.match_id,
        state=context.state,
    )
    if delivery.delivered:
        # The panel IS the acknowledgement; keep the toast silent.
        await safe_answer_callback(callback)
        return

    logger.warning(
        "Radar panel unavailable for match %s/user %s: %s",
        context.match_id,
        user.id,
        delivery.error,
    )
    await safe_answer_callback(
        callback,
        panel_alert_hint(context.state, user.id),
        show_alert=True,
    )


async def _apply_shot(callback: CallbackQuery, bot: Bot) -> None:
    """Commit one shot through the shared atomic dispatcher.

    The dispatcher commits first and then presents: the shooter's private
    panel, then the neutral group board. Stale taps are rejected there.
    """
    parsed = parse_shot(callback.data)
    if parsed is None:
        await safe_answer_callback(
            callback, INVALID_CALLBACK_TEXT, show_alert=True
        )
        return
    match_id, r, c = parsed
    await process_game_action(callback, bot, match_id, {"r": r, "c": c})


@router.callback_query(F.data.startswith(SHOT_CALLBACK_PREFIX))
async def handle_bs_shot(callback: CallbackQuery, bot: Bot):
    """A cell was tapped inside the player's private radar panel."""
    await _apply_shot(callback, bot)


@router.callback_query(F.data.startswith(LEGACY_SHOT_CALLBACK_PREFIX))
async def handle_bs_action(callback: CallbackQuery, bot: Bot):
    """Legacy grid buttons of boards delivered before the radar change.

    The tap is treated as a shot (so an in-flight match stays playable), but
    the reply is rendered by the new presentation: neutral group board plus a
    private panel - never a grid in the group.
    """
    await _apply_shot(callback, bot)


@router.callback_query(F.data.startswith(CLOSE_CALLBACK_PREFIX))
async def handle_bs_close(callback: CallbackQuery, bot: Bot):
    """«✖️ بستن» inside a private panel."""
    user = callback.from_user
    if not user:
        return
    message = getattr(callback, "message", None)
    closed = await close_panel(bot, message, player_id=user.id)
    await safe_answer_callback(
        callback,
        None if closed else "این پنل قبلاً بسته شده است.",
        show_alert=not closed,
    )
