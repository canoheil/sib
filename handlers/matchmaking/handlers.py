"""Telegram handlers for Matchmaking and Acceptance."""

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
            # Still in queue waiting for an opponent
            queue_text = (
                "🔍 **در حال جستجوی حریف از گروه‌های تلگرام...** 🍏\n\n"
                "سیب در حال جستجوی یک حریف متناسب با ریتینگ گروه شماست.\n"
                "با گذشت زمان، دامنه جستجو به‌طور خودکار گسترش می‌یابد.\n\n"
                "⏳ لطفاً منتظر بمانید..."
            )
            await callback.answer("وارد صف مسابقات شدی! 🍏")
            if callback.message:
                await callback.message.edit_text(
                    queue_text,
                    reply_markup=get_queue_keyboard(party_id),
                    parse_mode="Markdown",
                )
            return

        # Match was found immediately!
        await callback.answer("حریف پیدا شد! 🍏✨")

        # 1. Update/Send to Group 1
        text_g1 = render_match_found_for_group(match, match.group1_id)
        kb_g1 = get_acceptance_keyboard(match.id, has_accepted=False)
        if callback.message and callback.message.chat.id == match.group1_id:
            msg1 = await callback.message.edit_text(text_g1, reply_markup=kb_g1, parse_mode="Markdown")
            match.group1_message_id = msg1.message_id
        else:
            msg1 = await bot.send_message(match.group1_id, text_g1, reply_markup=kb_g1, parse_mode="Markdown")
            match.group1_message_id = msg1.message_id

        # 2. Send to Group 2
        text_g2 = render_match_found_for_group(match, match.group2_id)
        kb_g2 = get_acceptance_keyboard(match.id, has_accepted=False)
        msg2 = await bot.send_message(match.group2_id, text_g2, reply_markup=kb_g2, parse_mode="Markdown")
        match.group2_message_id = msg2.message_id

        await session.flush()


@router.callback_query(F.data.startswith("cancel_matchmaking:"))
async def handle_cancel_matchmaking(callback: CallbackQuery):
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
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


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
            initial_state = engine.create_initial_state(match.players, match.mode)
            match.game_state = json.dumps(initial_state)

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

            # Render interactive board for both groups
            for group_id, msg_id in ((match.group1_id, match.group1_message_id), (match.group2_id, match.group2_message_id)):
                if group_id and msg_id:
                    player = next((p for p in match.players if p.group_id == group_id), None)
                    p_id = player.user_id if player else 0
                    board_text = engine.render_for_group(initial_state, group_id, p_id)
                    kb = engine.get_keyboard(initial_state, match.id, p_id)
                    try:
                        await bot.edit_message_text(board_text, chat_id=group_id, message_id=msg_id, reply_markup=kb, parse_mode="Markdown")
                    except Exception as e:
                        logger.warning(f"Failed to edit board message in group {group_id}: {e}")
        else:
            # This side accepted, waiting for other side
            await callback.answer("مسابقه توسط شما پذیرفته شد! در انتظار تایید حریف... 🍏")
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

        for group_id, msg_id in ((match.group1_id, match.group1_message_id), (match.group2_id, match.group2_message_id)):
            if group_id and msg_id:
                try:
                    await bot.edit_message_text(reject_text, chat_id=group_id, message_id=msg_id, parse_mode="Markdown")
                except Exception:
                    pass
