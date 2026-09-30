"""Telegram handlers for Matchmaking and Acceptance."""

from typing import Optional

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.core.errors import InvalidActionException, MatchNotFoundException, PartyNotFoundException
from sib.core.logging import logger, log_event, Events
from sib.database.connection import get_session
from sib.handlers.party.renderer import render_party_card
from sib.keyboards.party import get_party_action_keyboard
from sib.keyboards.matchmaking import get_queue_keyboard, get_acceptance_keyboard
from sib.handlers.matchmaking.renderer import render_match_found_for_group
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.services.party import PartyService
from sib.services.telegram_ui import (
    render_callback_screen,
    safe_edit_callback_message,
    safe_edit_screen,
    safe_send_screen,
)

router = Router(name="matchmaking")


@router.callback_query(F.data.startswith("find_match:"))
async def handle_find_match(callback: CallbackQuery, bot: Bot):
    """Start matchmaking search for a full party."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user
    chat = callback.message.chat if callback.message else None

    if not user or not chat:
        return

    async with get_session() as session:
        try:
            queue_entry = await MatchmakingService.enqueue_party(session, party_id, user.id)
            match = await MatchmakingService.find_and_create_match(session, party_id)
        except (InvalidActionException, PartyNotFoundException) as e:
            await callback.answer(e.message, show_alert=True)
            return

        if not match:
            # The mode-specific deadline drives the automatic bot fallback.
            wait_seconds = max(1, int((queue_entry.expires_at - queue_entry.entered_at).total_seconds()))
            wait_fa = str(wait_seconds).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
            queue_text = (
                "🔍 **در حال جستجوی حریف...** 🍏\n\n"
                f"حدود {wait_fa} ثانیه منتظر می‌مونیم. اگر حریف واقعی پیدا نشه، "
                "بازی خودکار با تیم سیب شروع می‌شه."
            )
            await callback.answer("جستجوی حریف شروع شد 🍏")
            if callback.message:
                await safe_edit_callback_message(
                    callback,
                    bot,
                    queue_text,
                    reply_markup=get_queue_keyboard(party_id),
                )
                message_id = getattr(callback.message, "message_id", None)
                if isinstance(message_id, int):
                    queue_entry.message_id = message_id
            return

        # Match was found immediately!
        await callback.answer("حریف پیدا شد! 🍏✨")

        # 1. Update/Send to Group 1 (via the central renderer: the queue
        #    message may be text or photo; the match-found screen always
        #    renders as its own message and the stored id follows it).
        text_g1 = render_match_found_for_group(match, match.group1_id)
        kb_g1 = get_acceptance_keyboard(match.id, has_accepted=False)
        if callback.message and callback.message.chat.id == match.group1_id:
            msg1 = await render_callback_screen(
                callback, bot, text_g1, reply_markup=kb_g1
            )
        else:
            msg1 = await safe_send_screen(
                bot, match.group1_id, text_g1, reply_markup=kb_g1
            )
        if msg1 is not None:
            match.group1_message_id = msg1.message_id

        # 2. Send to Group 2
        text_g2 = render_match_found_for_group(match, match.group2_id)
        kb_g2 = get_acceptance_keyboard(match.id, has_accepted=False)
        msg2 = await safe_send_screen(
            bot, match.group2_id, text_g2, reply_markup=kb_g2
        )
        if msg2 is not None:
            match.group2_message_id = msg2.message_id

        await session.flush()


@router.callback_query(F.data.startswith("cancel_matchmaking:"))
async def handle_cancel_matchmaking(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Cancel waiting in matchmaking queue."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        try:
            await MatchmakingService.cancel_matchmaking(session, party_id, user.id)
            party = await PartyService.get_party_by_id(session, party_id)
        except (InvalidActionException, PartyNotFoundException) as e:
            await callback.answer(e.message, show_alert=True)
            return

        await callback.answer("جستجوی حریف لغو شد 🍏")
        if party and callback.message:
            text = render_party_card(party)
            kb = get_party_action_keyboard(party, is_member=True)
            await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("accept_match:"))
async def handle_accept_match(callback: CallbackQuery, bot: Bot):
    """Player accepts match for their side."""
    match_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        try:
            match, both_accepted = await AcceptanceService.accept_match(session, match_id, user.id)
        except (InvalidActionException, MatchNotFoundException) as e:
            await callback.answer(e.message, show_alert=True)
            return

        if both_accepted:
            # Both accepted! Initialize game state and render board
            import json
            from sib.games.registry import get_game_engine
            
            engine = get_game_engine(match.game.code)
            # Preserve a pre-seeded game state (e.g. Apple Battle's precomputed
            # plan) exactly like process_game_action does; otherwise build it.
            if match.game_state:
                initial_state = json.loads(match.game_state)
            else:
                initial_state = engine.create_initial_state(match.players, match.mode)
            from sib.services.match_setup import attach_team_names

            initial_state = attach_team_names(initial_state, match)
            match.game_state = json.dumps(initial_state, ensure_ascii=False)

            # Start the first gameplay timeout immediately.
            # For turn-based games this tracks the current player;
            # for simultaneous games current_player_id remains None,
            # but the shared deadline still prevents an abandoned match.
            from datetime import datetime, timedelta, timezone

            current_player_id = initial_state.get("current_player_id")
            match.current_turn_player_id = current_player_id
            match.turn_deadline = datetime.now(timezone.utc) + timedelta(
                seconds=engine.turn_timeout_seconds
            )

            await session.flush()

            await callback.answer("مسابقه آغاز شد! 🍏🎉")

            # Render interactive board for both groups. The message behind
            # a match-found screen can be a photo (e.g. Apple Battle card
            # art); the central renderer converts/replaces as needed and
            # the stored message ids always follow the live message.
            for group_id, msg_id, which in (
                (match.group1_id, match.group1_message_id, 1),
                (match.group2_id, match.group2_message_id, 2),
            ):
                if group_id and msg_id:
                    player = next((p for p in match.players if p.group_id == group_id), None)
                    p_id = player.user_id if player else 0
                    board_text = engine.render_for_group(initial_state, group_id, p_id)
                    kb = engine.get_keyboard(initial_state, match.id, p_id)
                    new_msg_id = await safe_edit_screen(
                        bot,
                        group_id,
                        msg_id,
                        board_text,
                        kb,
                        parse_mode="Markdown",
                    )
                    if new_msg_id is None:
                        logger.warning(f"Failed to edit board message in group {group_id}")
                    elif new_msg_id != msg_id:
                        if which == 1:
                            match.group1_message_id = new_msg_id
                        else:
                            match.group2_message_id = new_msg_id

            # Optional per-game hook (e.g. Apple Battle schedules its staged
            # narration). Default engines implement this as a no-op.
            after_start = getattr(engine, "after_start", None)
            if after_start is not None:
                await after_start(bot, match.id)
        else:
            # This side accepted, waiting for other side
            await callback.answer("مسابقه رو قبول کردی! منتظر تأیید حریف... 🍏")
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=get_acceptance_keyboard(match.id, has_accepted=True))


@router.callback_query(F.data.startswith("reject_match:"))
async def handle_reject_match(callback: CallbackQuery, bot: Bot):
    """Player rejects match before gameplay."""
    match_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        try:
            match = await AcceptanceService.reject_match(session, match_id, user.id)
        except (InvalidActionException, MatchNotFoundException) as e:
            await callback.answer(e.message, show_alert=True)
            return

        reject_text = (
            "❌ **این مسابقه توسط یکی از طرفین رد شد.**\n\n"
            "پارتی‌ها مجدداً بازگشتند و هیچ تغییری در ریتینگ رخ نداد 🍏"
        )
        await callback.answer("مسابقه لغو شد 🍏")

        for group_id, msg_id, which in (
            (match.group1_id, match.group1_message_id, 1),
            (match.group2_id, match.group2_message_id, 2),
        ):
            if group_id and msg_id:
                new_msg_id = await safe_edit_screen(
                    bot, group_id, msg_id, reject_text, None, parse_mode="Markdown"
                )
                if new_msg_id and new_msg_id != msg_id:
                    if which == 1:
                        match.group1_message_id = new_msg_id
                    else:
                        match.group2_message_id = new_msg_id
