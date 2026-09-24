"""Shared helper for game action dispatching and UI updates."""

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from sib.core.logging import logger, log_event, Events
from sib.database.connection import get_session
from sib.database.models import Match, MatchPlayer, MatchState, Group, AdCampaign
from sib.games.registry import get_game_engine
from sib.services.rating import RatingService
from sib.services.ad.service import AdService


def get_rematch_keyboard(ad: Optional[AdCampaign] = None) -> InlineKeyboardMarkup:
    """Keyboard shown after game completion with optional sponsor CTA."""
    buttons = [
        [
            InlineKeyboardButton(
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
                )
            ])
        else:
            buttons.append([
                InlineKeyboardButton(
                    text=f"✨ {ad.cta_label or 'درباره اسپانسر'}",
                    callback_data=f"ad_click:{ad.id}",
                )
            ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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

    async with get_session() as session:
        # Fetch match with lock
        match_stmt = (
            select(Match)
            .where(Match.id == match_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.group1).selectinload(Group.rating),
                selectinload(Match.group2).selectinload(Group.rating),
                selectinload(Match.game),
            )
            .with_for_update()
        )
        match = await session.scalar(match_stmt)
        if not match:
            await callback.answer("مسابقه پیدا نشد 🍏", show_alert=True)
            return

        if match.state != MatchState.ACTIVE:
            await callback.answer("این مسابقه در جریان نیست یا قبلاً به پایان رسیده 🍏", show_alert=True)
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
            await callback.answer(reason or "حرکت نامعتبر است 🍏", show_alert=True)
            return

        # Apply action
        state = engine.apply_action(state, user.id, action)
        match.game_state = json.dumps(state)

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

        # Helper to get the relevant player for a given group
        def get_group_target_player_id(target_group_id: int) -> int:
            current_pid = state.get("current_player_id")
            group_pids = [p.user_id for p in match.players if p.group_id == target_group_id]
            if current_pid and current_pid in group_pids:
                return current_pid
            return group_pids[0] if group_pids else user.id

        has_message_ids = bool(match.group1_message_id or match.group2_message_id)

        # Check if match is finished
        if not engine.is_finished(state):
            await session.flush()
            await callback.answer("حرکت شما ثبت شد! 🍏")

            if has_message_ids:
                for group_id, msg_id in (
                    (match.group1_id, match.group1_message_id),
                    (match.group2_id, match.group2_message_id),
                ):
                    if group_id and msg_id:
                        target_pid = get_group_target_player_id(group_id)
                        text = engine.render_for_group(state, group_id, target_pid)
                        kb = engine.get_keyboard(state, match.id, target_pid)
                        try:
                            await bot.edit_message_text(
                                text,
                                chat_id=group_id,
                                message_id=msg_id,
                                reply_markup=kb,
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            logger.warning(f"Failed to edit game message in group {group_id}: {e}")
            elif callback.message:
                target_pid = user.id
                group_id = callback.message.chat.id
                text = engine.render_for_group(state, group_id, target_pid)
                kb = engine.get_keyboard(state, match.id, target_pid)
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return

        # Match IS finished!
        result = engine.get_result(state)

        # 1. Apply rating and record statistics atomically and idempotently
        await RatingService.apply_match_result(session, match, result)
        await session.commit()
        await callback.answer("بازی به پایان رسید! 🍏✨")

        # 2. Fetch eligible post-match ad safely (never affects match result if fails)
        selected_ad: Optional[AdCampaign] = None
        try:
            match_num = 1
            if match.group1 and match.group1.rating:
                match_num = match.group1.rating.matches_played

            selected_ad = await AdService.get_eligible_ad(
                session,
                game_id=match.game_id,
                group_id=match.group1_id,
                match_number=match_num,
            )
            if selected_ad:
                await AdService.record_impression(session, selected_ad.id, match_id=match.id)
                await session.commit()
        except Exception as ad_err:
            logger.warning(f"Non-fatal error delivering post-match ad: {ad_err}")

        rematch_kb = get_rematch_keyboard(selected_ad)

        def build_post_match_text(for_group_id: int) -> str:
            target_pid = get_group_target_player_id(for_group_id)
            base_text = engine.render_for_group(state, for_group_id, target_pid)
            is_g1 = (for_group_id == match.group1_id)
            delta = match.group1_rating_change if is_g1 else match.group2_rating_change
            delta_str = f"+{delta:g}" if delta > 0 else f"{delta:g}"

            group_obj = match.group1 if is_g1 else match.group2
            current_rating = group_obj.rating.rating if group_obj and group_obj.rating else 1500.0

            rating_summary = (
                f"\n\n📊 **تغییر ریتینگ گروه شما:** `{delta_str}`\n"
                f"🏆 **ریتینگ جدید گروه:** `{current_rating:g}`"
            )
            ad_banner = ""
            if selected_ad:
                ad_banner = f"\n\n📢 **اسپانسر سیب ({selected_ad.sponsor_name}):**\n_{selected_ad.ad_text}_"

            return base_text + rating_summary + ad_banner

        if has_message_ids:
            text_g1 = build_post_match_text(match.group1_id)
            text_g2 = build_post_match_text(match.group2_id)

            if match.group1_id and match.group1_message_id:
                try:
                    await bot.edit_message_text(
                        text_g1,
                        chat_id=match.group1_id,
                        message_id=match.group1_message_id,
                        reply_markup=rematch_kb,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.warning(f"Failed to edit group1 final message: {e}")

            if match.group2_id and match.group2_message_id:
                try:
                    await bot.edit_message_text(
                        text_g2,
                        chat_id=match.group2_id,
                        message_id=match.group2_message_id,
                        reply_markup=rematch_kb,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.warning(f"Failed to edit group2 final message: {e}")
        elif callback.message:
            final_text = build_post_match_text(callback.message.chat.id)
            await callback.message.edit_text(final_text, reply_markup=rematch_kb, parse_mode="Markdown")
