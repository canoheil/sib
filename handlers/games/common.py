"""Shared helper for game action dispatching and UI updates.

Design contract (Battleship reliability fix):
1. The database state is authoritative. The match result and the game
   state are committed BEFORE any Telegram presentation happens.
2. Presentation is best-effort: edits are retried and can degrade, but
   they can never roll back, corrupt or hide a committed move.
3. Every rendered board carries a "🔄 بروزرسانی صفحه" (refresh) button
   which re-renders the board from the authoritative DB state, so a
   stale board (e.g. the Battleship white-cell bug) is always recoverable.
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from aiogram import Bot, Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from sib.core.logging import logger, log_event, Events
from sib.database.connection import get_session
from sib.database.models import Match, MatchPlayer, MatchState, Group, AdCampaign
from sib.games.registry import get_game_engine
from sib.services.match_result import MatchResultService
from sib.services.ad.service import AdService
from sib.services.telegram_ui import (
    REFRESH_CALLBACK_PREFIX,
    safe_answer_callback,
    safe_edit_callback_message,
    safe_edit_screen,
    with_refresh_button,
)


async def _persist_board_message_id(match_id: int, group_id: int, message_id: int) -> None:
    """Store a REPLACED board message id (best-effort, presentation-only).

    When the transition engine replaces a board message (text↔photo), the
    match's stored id must follow the live message - a stale id must never
    outlive the replacement.
    """
    try:
        async with get_session() as session:
            match = await session.get(Match, match_id)
            if match is None:
                return
            if group_id == match.group1_id:
                match.group1_message_id = message_id
            elif group_id == match.group2_id:
                match.group2_message_id = message_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            f"Could not persist replaced board id for match {match_id}: {exc}"
        )
from sib.bots.runner import run_bot_turns


def get_rematch_keyboard(ad: Optional[AdCampaign] = None) -> InlineKeyboardMarkup:
    """Keyboard shown after game completion with optional sponsor CTA."""
    from sib.keyboards.common import primary_button, success_button

    buttons = [
        [
            success_button(
                text="➕ بازی جدید در گروه",
                callback_data="group_games_panel",
            )
        ]
    ]
    if ad:
        if ad.target_url:
            buttons.append([
                InlineKeyboardButton(
                    text=f"✨ {ad.cta_label or 'مشاهده اسپانسر'}",
                    url=ad.target_url,
                    style="primary",
                )
            ])
        else:
            buttons.append([
                primary_button(
                    text=f"✨ {ad.cta_label or 'درباره اسپانسر'}",
                    callback_data=f"ad_click:{ad.id}",
                )
            ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)



async def render_board_for(
    callback: CallbackQuery,
    bot: Bot,
    match: Match,
    state: Dict[str, Any],
    for_player_id: int,
    *,
    final: bool = False,
    selected_ad: Optional[AdCampaign] = None,
) -> None:
    """Best-effort board render for one viewer (used for self-healing too).

    When ``final`` is True the board is rendered as a finished match with
    the rematch keyboard, otherwise with the live playable keyboard.
    """
    engine = get_game_engine(match.game.code)
    message = callback.message
    chat = getattr(message, "chat", None) if message is not None else None
    chat_id = getattr(chat, "id", None) if chat is not None else None
    group_id = chat_id or match.group1_id or for_player_id

    text = engine.render_for_group(state, group_id, for_player_id)
    if final:
        outcome = ""
        if match.is_draw:
            outcome = "🤝 این مسابقه مساوی شد."
        elif match.winner_group_id:
            outcome = (
                "🎉 گروه شما پیروز شد!"
                if match.winner_group_id == group_id
                else "💔 گروه حریف پیروز شد."
            )
        text += f"\n\n⏱ **این مسابقه به پایان رسید.** {outcome}"
        kb = get_rematch_keyboard(selected_ad)
    else:
        kb = with_refresh_button(engine.get_keyboard(state, match.id, for_player_id), match.id)
    before_id = getattr(message, "message_id", None)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
    after_id = getattr(callback.message, "message_id", None)
    if before_id and after_id and after_id != before_id:
        if group_id == match.group1_id and match.group1_message_id == before_id:
            match.group1_message_id = after_id
        elif group_id == match.group2_id and match.group2_message_id == before_id:
            match.group2_message_id = after_id

async def process_game_action(
    callback: CallbackQuery,
    bot: Bot,
    match_id: int,
    action: Dict[str, Any],
) -> None:
    """Generic atomic handler for any game action."""
    user = callback.from_user
    if not user:
        return

    # ------------------------------------------------------------------
    # Phase A: authoritative state transition (pure DB work, no UI).
    # ------------------------------------------------------------------
    selected_ad: Optional[AdCampaign] = None
    render_payload: Optional[Dict[str, Any]] = None

    async with get_session() as session:
        # Fetch match with lock
        match_stmt = (
            select(Match)
            .where(Match.id == match_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.group1).selectinload(Group.stats),
                selectinload(Match.group2).selectinload(Group.stats),
                selectinload(Match.game),
            )
            .with_for_update()
        )
        match = await session.scalar(match_stmt)
        if not match:
            await safe_answer_callback(callback, "مسابقه پیدا نشد 🍏", show_alert=True)
            return

        if match.state != MatchState.ACTIVE:
            # Self-heal: the visible keyboard is stale (e.g. the match was
            # resolved by a turn timeout in the background). Reload the
            # authoritative state and render the true final board.
            await safe_answer_callback(
                callback,
                "این مسابقه دیگر در جریان نیست؛ آخرین وضعیت بازی بارگذاری شد. 🍏",
                show_alert=True,
            )
            if match.game_state:
                try:
                    state = json.loads(match.game_state)
                    await render_board_for(
                        callback, bot, match, state, user.id, final=True
                    )
                except Exception as render_err:
                    logger.warning(f"Stale-board recovery render failed: {render_err}")
            return

        engine = get_game_engine(match.game.code)

        # Deserialize or initialize game state
        if not match.game_state:
            state = engine.create_initial_state(match.players, match.mode)
        else:
            state = json.loads(match.game_state)

        # Validate action
        is_valid, reason = engine.validate_action(state, user.id, action)
        if not is_valid:
            # Self-heal: whatever the rejection (stale keyboard, wrong
            # player, repeated cell), the viewer immediately receives the
            # LATEST authoritative board - manual refresh is never required.
            await safe_answer_callback(
                callback,
                (reason or "حرکت نامعتبر است 🍏") + "\nآخرین وضعیت بازی بارگذاری شد.",
                show_alert=True,
            )
            try:
                await render_board_for(callback, bot, match, state, user.id)
            except Exception as render_err:
                logger.warning(f"Rejected-action recovery render failed: {render_err}")
            return

        # Apply action
        state = engine.apply_action(state, user.id, action)

        # Sync Match turn metadata before running the bot.
        if not engine.is_finished(state):
            match.current_turn_player_id = state.get("current_player_id")

        # If this is a bot match and the bot now has the turn,
        # execute the bot move before rendering the updated board.
        if match.is_bot_match and not engine.is_finished(state):
            state = await run_bot_turns(match, state)

        match.game_state = json.dumps(state, ensure_ascii=False)

        # Keep Match timeout metadata synchronized with the game engine state.
        current_player_id = state.get("current_player_id")
        if engine.is_finished(state):
            match.current_turn_player_id = None
            match.turn_deadline = None
        elif current_player_id:
            # Turn-based game: the deadline belongs to the current player.
            match.current_turn_player_id = current_player_id
            match.turn_deadline = (
                datetime.now(timezone.utc)
                + timedelta(seconds=engine.turn_timeout_seconds)
            )
        else:
            # Simultaneous game (e.g. RPS / High Card): keep the shared
            # deadline so TimeoutService can resolve an incomplete round.
            match.current_turn_player_id = None
            if match.turn_deadline is None:
                match.turn_deadline = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=engine.turn_timeout_seconds)
                )

        log_event(
            Events.MOVE_PLAYED,
            {"match_id": match.id, "user_id": user.id, "action": action, "game": match.game.code},
        )

        finished = engine.is_finished(state)
        result = None
        if finished:
            result = engine.get_result(state)
            # Apply the result (idempotent) BEFORE committing.
            await MatchResultService.apply_match_result(session, match, result)

            # Fetch eligible post-match ad safely (never affects match result if fails)
            try:
                match_num = 1
                if match.group1 and match.group1.stats:
                    match_num = match.group1.stats.matches_played

                selected_ad = await AdService.get_eligible_ad(
                    session,
                    game_id=match.game_id,
                    group_id=match.group1_id,
                    match_number=match_num,
                )
                if selected_ad:
                    await AdService.record_impression(session, selected_ad.id, match_id=match.id)
            except Exception as ad_err:
                logger.warning(f"Non-fatal error delivering post-match ad: {ad_err}")
                selected_ad = None

        # Commit the authoritative state before touching Telegram. From
        # this point on, presentation failures cannot affect the result.
        await session.commit()

        # Snapshot everything the presentation layer needs.
        render_payload = {
            "finished": finished,
            "state": state,
            "is_bot_match": match.is_bot_match,
            "group1_id": match.group1_id,
            "group2_id": match.group2_id,
            "group1_message_id": match.group1_message_id,
            "group2_message_id": match.group2_message_id,
            "group1_wins": match.group1.stats.wins if match.group1 and match.group1.stats else 0,
            "group2_wins": match.group2.stats.wins if match.group2 and match.group2.stats else 0,
            "game_id": match.game_id,
            "game_code": match.game.code,
            "players": [(p.user_id, p.group_id) for p in match.players],
        }

    # ------------------------------------------------------------------
    # Phase B: presentation (best effort, never authoritative).
    # ------------------------------------------------------------------
    assert render_payload is not None
    state = render_payload["state"]
    finished = render_payload["finished"]
    has_message_ids = bool(
        render_payload["group1_message_id"] or render_payload["group2_message_id"]
    )

    def get_group_target_player_id(target_group_id: Optional[int]) -> int:
        if target_group_id is None:
            return user.id
        current_pid = state.get("current_player_id")
        group_pids = [
            player_id
            for player_id, group_id in render_payload["players"]
            if group_id == target_group_id
        ]
        if current_pid and int(current_pid) in group_pids:
            return int(current_pid)
        return group_pids[0] if group_pids else user.id

    if not finished:
        await safe_answer_callback(callback, "حرکت شما ثبت شد! 🍏")

        if has_message_ids:
            for group_id, msg_id in (
                (render_payload["group1_id"], render_payload["group1_message_id"]),
                (render_payload["group2_id"], render_payload["group2_message_id"]),
            ):
                if group_id and msg_id:
                    target_pid = get_group_target_player_id(group_id)
                    engine = get_game_engine(render_payload["game_code"])
                    text = engine.render_for_group(state, group_id, target_pid)
                    kb = with_refresh_button(
                        engine.get_keyboard(state, match_id, target_pid), match_id
                    )
                    new_msg_id = await safe_edit_screen(
                        bot, chat_id=group_id, message_id=msg_id, text=text, reply_markup=kb
                    )
                    if new_msg_id is None:
                        logger.warning(
                            f"Game board refresh failed for match {match_id} "
                            f"in group {group_id}; refresh button remains available."
                        )
                    elif new_msg_id != msg_id:
                        await _persist_board_message_id(match_id, group_id, new_msg_id)
        else:
            target_pid = user.id
            message = callback.message
            chat = getattr(message, "chat", None) if message is not None else None
            group_id = getattr(chat, "id", None) or user.id
            engine = get_game_engine(render_payload["game_code"])
            text = engine.render_for_group(state, group_id, target_pid)
            kb = with_refresh_button(
                engine.get_keyboard(state, match_id, target_pid), match_id
            )
            if not await safe_edit_callback_message(callback, bot, text, reply_markup=kb):
                logger.warning(
                    f"Game board refresh failed for match {match_id} "
                    f"in private flow; refresh button remains available."
                )
        return

    # Match IS finished!
    await safe_answer_callback(callback, "بازی به پایان رسید! 🍏✨")

    rematch_kb = get_rematch_keyboard(selected_ad)
    engine = get_game_engine(render_payload["game_code"])

    def build_post_match_text(for_group_id: Optional[int]) -> str:
        target_pid = get_group_target_player_id(for_group_id) if for_group_id else user.id
        base_text = engine.render_for_group(state, for_group_id, target_pid)

        rating_summary = ""
        if not render_payload["is_bot_match"] and not render_payload.get("is_draw"):
            is_g1 = (for_group_id == render_payload["group1_id"])
            wins = (
                render_payload["group1_wins"]
                if is_g1
                else render_payload["group2_wins"]
            )
            rating_summary = (
                f"\n\n🏆 **برد‌های گروه شما:** `{wins}`"
            )

        ad_banner = ""
        if selected_ad:
            ad_banner = (
                f"\n\n📢 **اسپانسر سیب ({selected_ad.sponsor_name}):**\n"
                f"_{selected_ad.ad_text}_"
            )

        return base_text + rating_summary + ad_banner

    if has_message_ids:
        for group_id, msg_id in (
            (render_payload["group1_id"], render_payload["group1_message_id"]),
            (render_payload["group2_id"], render_payload["group2_message_id"]),
        ):
            if group_id and msg_id:
                text = build_post_match_text(group_id)
                new_msg_id = await safe_edit_screen(
                    bot,
                    chat_id=group_id,
                    message_id=msg_id,
                    text=text,
                    reply_markup=rematch_kb,
                )
                if new_msg_id and new_msg_id != msg_id:
                    await _persist_board_message_id(match_id, group_id, new_msg_id)
    else:
        message = callback.message
        chat = getattr(message, "chat", None) if message is not None else None
        chat_id = getattr(chat, "id", None)
        final_text = build_post_match_text(chat_id)
        await safe_edit_callback_message(callback, bot, final_text, reply_markup=rematch_kb)


async def handle_game_refresh(callback: CallbackQuery, bot: Bot) -> None:
    """Re-render a game board from the authoritative database state.

    Recovery path for stale boards (e.g. Battleship cells that stayed
    visually white after a transient Telegram edit failure). The board is
    rebuilt from ``match.game_state`` which was committed before any
    presentation, so this view can never disagree with the DB.
    """
    user = callback.from_user
    if not user or not callback.data:
        return

    try:
        match_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است.", show_alert=True)
        return

    async with get_session() as session:
        match_stmt = (
            select(Match)
            .where(Match.id == match_id)
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.game),
            )
        )
        match = await session.scalar(match_stmt)
        if not match:
            await safe_answer_callback(callback, "مسابقه پیدا نشد 🍏", show_alert=True)
            return

        is_participant = any(p.user_id == user.id for p in match.players)
        if not is_participant:
            await safe_answer_callback(
                callback, "فقط بازیکنان این مسابقه می‌توانند صفحه را بروزرسانی کنند.", show_alert=True
            )
            return

        engine = get_game_engine(match.game.code)
        state = json.loads(match.game_state) if match.game_state else None
        if state is None:
            await safe_answer_callback(
                callback, "این مسابقه هنوز شروع نشده است.", show_alert=True
            )
            return

        message = callback.message
        chat = getattr(message, "chat", None) if message is not None else None
        group_id = getattr(chat, "id", None) or user.id
        text = engine.render_for_group(state, group_id, user.id)

        if match.state == MatchState.ACTIVE:
            kb = with_refresh_button(engine.get_keyboard(state, match_id, user.id), match_id)
        else:
            kb = None

    await safe_answer_callback(callback, "صفحه از وضعیت رسمی بازی بروزرسانی شد. 🍏")
    before_id = getattr(message, "message_id", None)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
    after_id = getattr(callback.message, "message_id", None)
    if before_id and after_id and after_id != before_id:
        group_id = getattr(chat, "id", None) if chat is not None else None
        if group_id:
            await _persist_board_message_id(match_id, group_id, after_id)


router = Router(name="game_common")


@router.callback_query(F.data.startswith(REFRESH_CALLBACK_PREFIX))
async def handle_refresh_callback(callback: CallbackQuery, bot: Bot):
    """Entry point for the board refresh recovery button."""
    await handle_game_refresh(callback, bot)
