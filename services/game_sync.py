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
from aiogram.types import InlineKeyboardMarkup

from sib.core.logging import logger
from sib.database.models import Match, MatchState
from sib.games.registry import get_game_engine
from sib.keyboards.common import success_button
from sib.keyboards.party import get_friendly_rematch_keyboard
from sib.services.battleship_radar import public_view, supports_private_radar
from sib.services.friendly import (
    is_friendly,
    shape_friendly_final_text,
    state_viewer_id,
    viewer_audience,
)
from sib.services.telegram_ui import board_refresh_style, safe_edit_screen, with_refresh_button


def final_keyboard(match: Optional[Match] = None) -> InlineKeyboardMarkup:
    """Final-board keyboard; friendly matches get their Rematch action."""
    if match is not None and is_friendly(match):
        return get_friendly_rematch_keyboard(match.id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(
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

    refresh_style = board_refresh_style(engine.game_code)
    match_over = match.state != MatchState.ACTIVE
    players_by_group = {p.group_id: p.user_id for p in match.players}

    if is_friendly(match) and supports_private_radar(engine):
        # Battleship: out-of-band transitions (bot moves, timeouts, recovery)
        # only touch the NEUTRAL public group board. Private radar panels are
        # per-player ephemeral messages that can't be addressed from here -
        # they self-heal on the player's next tap.
        msg_id = match.group1_message_id or match.group2_message_id
        if match.group1_id and msg_id:
            text, kb = public_view(engine, match.id, state, finished=match_over)
            if suffix:
                text = f"{text}\n\n{suffix}"
            new_msg_id = await safe_edit_screen(
                bot,
                chat_id=match.group1_id,
                message_id=msg_id,
                text=text,
                reply_markup=kb,
            )
            if new_msg_id and new_msg_id != msg_id:
                match.group1_message_id = new_msg_id
                if match.group2_id == match.group1_id:
                    match.group2_message_id = new_msg_id
        return

    if is_friendly(match):
        # A first RPS choice is a hidden commit. Leave the shared public board
        # untouched until the other player commits (unless a timeout has
        # already ended the match, in which case the terminal board must sync).
        rps_players = list((state.get("players") or {}).values())
        if (
            not match_over
            and engine.game_code == "rock_paper_scissors"
            and rps_players
            and not state.get("reveal_pending")
            and any(player.get("locked") for player in rps_players)
        ):
            return

        # Friendly matches hold BOTH teams in ONE group and share ONE board
        # message (group1 == group2): sync it exactly once, from the current
        # player's team perspective, with the neutral team-named finale.
        human_ids = {p.user_id for p in match.players if p.group_id is not None}
        viewer = state_viewer_id(state, human_ids, None)
        audience = viewer_audience(match, state, viewer) or match.group1_id
        msg_id = match.group1_message_id or match.group2_message_id
        if match.group1_id and msg_id:
            text = engine.render_for_group(state, audience, viewer)
            kb = with_refresh_button(
                engine.get_keyboard(state, match.id, viewer),
                match.id,
                refresh_style=refresh_style,
            )
            if match_over:
                text = shape_friendly_final_text(text, match, state)
                kb = get_friendly_rematch_keyboard(match.id)
            if suffix:
                text = f"{text}\n\n{suffix}"
            new_msg_id = await safe_edit_screen(
                bot,
                chat_id=match.group1_id,
                message_id=msg_id,
                text=text,
                reply_markup=kb,
            )
            if new_msg_id and new_msg_id != msg_id:
                match.group1_message_id = new_msg_id
                if match.group2_id == match.group1_id:
                    match.group2_message_id = new_msg_id
        return

    for group_id, msg_id, which in (
        (match.group1_id, match.group1_message_id, 1),
        (match.group2_id, match.group2_message_id, 2),
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
                    engine.get_keyboard(state, match.id, viewer_id),
                    match.id,
                    refresh_style=refresh_style,
                )
            new_msg_id = await safe_edit_screen(
                bot, chat_id=group_id, message_id=msg_id, text=text, reply_markup=kb
            )
            if new_msg_id and new_msg_id != msg_id:
                # The renderer replaced the message: keep the match's stored
                # board id pointing at the live message (never a stale one).
                if which == 1:
                    match.group1_message_id = new_msg_id
                else:
                    match.group2_message_id = new_msg_id
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"Failed to sync board message {msg_id} in {group_id} "
                f"for match {match.id}: {exc}"
            )
