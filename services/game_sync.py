"""Post-transition UI synchronization for matches.

State transitions that happen OUTSIDE a button handler (turn-timeout
forfeits, startup recovery, background resolution) must still bring the
visible Telegram boards up to date. The database is authoritative; this
module re-renders both board messages from ``match.game_state`` after
such transitions so the UI never keeps showing a dead interactive board.
"""

import json
from typing import Any, Dict, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.core.logging import logger
from sib.database.models import Match, MatchState
from sib.games.registry import get_game_engine
from sib.services.telegram_ui import safe_edit_message_text, with_refresh_button


def final_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ بازی جدید در گروه",
                    callback_data="group_games_panel",
                )
            ]
        ]
    )


async def sync_match_messages(
    bot: Bot,
    match: Match,
    *,
    suffix: Optional[str] = None,
) -> None:
    """Re-render both recorded board messages from the authoritative state.

    Used after out-of-band transitions (timeout forfeits, recovery). If the
    match is over, the boards are rendered as final with a rematch entry
    point instead of the (dead) play keyboard.
    """
    if not match.game_state:
        return

    try:
        state: Dict[str, Any] = json.loads(match.game_state)
    except (TypeError, ValueError):
        logger.warning(f"Cannot sync messages for match {match.id}: invalid game_state")
        return

    try:
        engine = get_game_engine(match.game.code)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Cannot sync messages for match {match.id}: {exc}")
        return

    match_over = match.state != MatchState.ACTIVE
    players_by_group = {p.group_id: p.user_id for p in match.players}

    for group_id, msg_id in (
        (match.group1_id, match.group1_message_id),
        (match.group2_id, match.group2_message_id),
    ):
        if not group_id or not msg_id:
            continue
        viewer_id = players_by_group.get(group_id, 0)
        try:
            if match_over:
                outcome = ""
                if match.is_draw:
                    outcome = "🤝 این مسابقه مساوی شد."
                elif match.winner_group_id:
                    outcome = (
                        "🎉 گروه شما پیروز شد!"
                        if match.winner_group_id == group_id
                        else "💔 گروه حریف پیروز شد."
                    )
                text = (
                    engine.render_for_group(state, group_id, viewer_id)
                    + f"\n\n⏱ **این مسابقه به پایان رسید.** {outcome}"
                )
                if suffix:
                    text += f"\n{suffix}"
                kb = final_keyboard()
            else:
                text = engine.render_for_group(state, group_id, viewer_id)
                if suffix:
                    text += f"\n\n{suffix}"
                kb = with_refresh_button(
                    engine.get_keyboard(state, match.id, viewer_id), match.id
                )
            await safe_edit_message_text(
                bot, chat_id=group_id, message_id=msg_id, text=text, reply_markup=kb
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"Failed to sync board message {msg_id} in {group_id} "
                f"for match {match.id}: {exc}"
            )
