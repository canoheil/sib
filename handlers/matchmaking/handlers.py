"""Telegram handlers for Matchmaking and Acceptance.

Bot-turn invariant (mirrors ``sib.bots.runner``): a DB transaction that
holds row locks (parties, matches, queue entries) must NEVER stay open
across a Telegram await. Every acknowledgement and board render below runs
AFTER the transaction has committed/rolled back; otherwise a flood-retry
edit (up to ~30s) would block the maintenance worker's per-cycle
``FOR UPDATE`` sweep and every bot turn waiting on the same rows - the
"bot sometimes takes 20-30 seconds" failure. Presentation stays best-effort.
"""

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


async def _persist_found_screen_ids(
    *,
    match_id: Optional[int] = None,
    group1_message_id: Optional[int] = None,
    group2_message_id: Optional[int] = None,
    queue_entry_id: Optional[int] = None,
    queue_message_id: Optional[int] = None,
) -> None:
    """Persist live message ids chosen during presentation (best effort).

    Runs in its own tiny transaction AFTER rendering, so the match/queue
    locks held by the caller's transaction are long released.
    """
    try:
        from sib.database.models import Match, MatchmakingQueue

        async with get_session() as session:
            if match_id and (group1_message_id or group2_message_id):
                match = await session.get(Match, match_id)
                if match is not None:
                    if group1_message_id:
                        match.group1_message_id = group1_message_id
                    if group2_message_id:
                        match.group2_message_id = group2_message_id
            if queue_entry_id and queue_message_id:
                entry = await session.get(MatchmakingQueue, queue_entry_id)
                if entry is not None:
                    entry.message_id = queue_message_id
            await session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not persist match-found message ids: {exc}")


@router.callback_query(F.data.startswith("find_match:"))
async def handle_find_match(callback: CallbackQuery, bot: Bot):
    """Start matchmaking search for a full party."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user
    chat = callback.message.chat if callback.message else None

    if not user or not chat:
        return

    # Phase A - DB only. The transaction locks party/queue rows (FOR UPDATE);
    # no Telegram await may happen inside it.
    error_message: Optional[str] = None
    queue_text: Optional[str] = None
    found_g1_text: Optional[str] = None
    found_g2_text: Optional[str] = None
    kb_queue = None
    kb_g1 = None
    kb_g2 = None
    queue_entry_id: Optional[int] = None
    match_id: Optional[int] = None
    match_group1_id: Optional[int] = None
    match_group2_id: Optional[int] = None
    found = False

    async with get_session() as session:
        try:
            queue_entry = await MatchmakingService.enqueue_party(session, party_id, user.id)
            match = await MatchmakingService.find_and_create_match(session, party_id)
        except (InvalidActionException, PartyNotFoundException) as e:
            await session.rollback()
            error_message = e.message
            queue_entry = None
            match = None
        if error_message is None:
            queue_entry_id = queue_entry.id if queue_entry else None
            if match is None:
                # The mode-specific deadline drives the automatic bot fallback.
                wait_seconds = max(
                    1, int((queue_entry.expires_at - queue_entry.entered_at).total_seconds())
                )
                wait_fa = str(wait_seconds).translate(
                    str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
                )
                queue_text = (
                    "🔍 **در حال جستجوی حریف...** 🍏\n\n"
                    f"حدود {wait_fa} ثانیه منتظر می‌مونیم. اگر حریف واقعی پیدا نشه، "
                    "بازی خودکار با تیم سیب شروع می‌شه."
                )
                kb_queue = get_queue_keyboard(party_id)
            else:
                found = True
                # Match was found immediately! Render data is computed while
                # the loaded match is guaranteed attached; the sends happen
                # after this transaction closes.
                match_id = match.id
                match_group1_id = match.group1_id
                match_group2_id = match.group2_id
                found_g1_text = render_match_found_for_group(match, match.group1_id)
                found_g2_text = render_match_found_for_group(match, match.group2_id)
                kb_g1 = get_acceptance_keyboard(match.id, has_accepted=False)
                kb_g2 = get_acceptance_keyboard(match.id, has_accepted=False)

    # Phase B - presentation (best effort, no DB locks held).
    if error_message is not None:
        await callback.answer(error_message, show_alert=True)
        return

    if found:
        # Match was found immediately!
        await callback.answer("حریف پیدا شد! 🍏✨")

        # 1. Update/Send to Group 1 (via the central renderer: the queue
        #    message may be text or photo; the match-found screen always
        #    renders as its own message and the stored id follows it).
        g1_msg_id = None
        if callback.message and callback.message.chat.id == match_group1_id:
            msg1 = await render_callback_screen(
                callback, bot, found_g1_text, reply_markup=kb_g1
            )
        else:
            msg1 = await safe_send_screen(
                bot, match_group1_id, found_g1_text, reply_markup=kb_g1
            )
        if msg1 is not None:
            g1_msg_id = msg1.message_id

        # 2. Send to Group 2
        msg2 = await safe_send_screen(
            bot, match_group2_id, found_g2_text, reply_markup=kb_g2
        )
        g2_msg_id = msg2.message_id if msg2 is not None else None

        await _persist_found_screen_ids(
            match_id=match_id,
            group1_message_id=g1_msg_id,
            group2_message_id=g2_msg_id,
        )
        return

    # Still searching: show the queue screen; the mode-specific deadline
    # drives the automatic bot fallback (the maintenance worker starts the
    # bot match and schedules its opening turn - never this callback).
    await callback.answer("جستجوی حریف شروع شد 🍏")
    queue_message_id = None
    if callback.message and queue_text is not None:
        await safe_edit_callback_message(
            callback,
            bot,
            queue_text,
            reply_markup=kb_queue,
        )
        message_id = getattr(callback.message, "message_id", None)
        if isinstance(message_id, int):
            queue_message_id = message_id
    await _persist_found_screen_ids(
        queue_entry_id=queue_entry_id,
        queue_message_id=queue_message_id,
    )


@router.callback_query(F.data.startswith("cancel_matchmaking:"))
async def handle_cancel_matchmaking(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Cancel waiting in matchmaking queue."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    error_message: Optional[str] = None
    text: Optional[str] = None
    kb = None
    async with get_session() as session:
        try:
            await MatchmakingService.cancel_matchmaking(session, party_id, user.id)
            party = await PartyService.get_party_by_id(session, party_id)
        except (InvalidActionException, PartyNotFoundException) as e:
            await session.rollback()
            error_message = e.message
            party = None
        if error_message is None:
            await session.commit()
            if party:
                text = render_party_card(party)
                kb = get_party_action_keyboard(
                    party,
                    is_member=True,
                    is_host=party.creator_user_id == user.id,
                )

    if error_message is not None:
        await callback.answer(error_message, show_alert=True)
        return

    await callback.answer("جستجوی حریف لغو شد 🍏")
    if text is not None and callback.message:
        await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("accept_match:"))
async def handle_accept_match(callback: CallbackQuery, bot: Bot):
    """Player accepts match for their side."""
    match_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    # Phase A - acceptance + game-state initialisation, committed BEFORE any
    # Telegram call. The acceptance transition locks the Match row; the
    # board renders below must not run while that lock is held.
    error_message: Optional[str] = None
    board_payload = None
    waiting_payload = None
    async with get_session() as session:
        try:
            match, both_accepted = await AcceptanceService.accept_match(session, match_id, user.id)
        except (InvalidActionException, MatchNotFoundException) as e:
            await session.rollback()
            error_message = e.message
            match = None
            both_accepted = False

        if error_message is None and both_accepted:
            # Both accepted! Initialize game state (the board renders below).
            import json

            from sib.games.registry import get_game_engine

            engine = get_game_engine(match.game.code)
            # Preserve a pre-seeded game state (e.g. Apple Battle's
            # precomputed plan) exactly like process_game_action does;
            # otherwise build it.
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

            await session.commit()
            board_payload = {
                "game_code": match.game.code,
                "state": initial_state,
                "match_id": match.id,
                "targets": [
                    (
                        group_id,
                        msg_id,
                        next(
                            (
                                p.user_id
                                for p in match.players
                                if p.group_id == group_id
                            ),
                            0,
                        ),
                    )
                    for group_id, msg_id in (
                        (match.group1_id, match.group1_message_id),
                        (match.group2_id, match.group2_message_id),
                    )
                ],
            }
        elif error_message is None:
            # This side accepted, waiting for the other side. Commit the
            # acceptance first; the keyboard refresh happens outside.
            await session.commit()
            waiting_payload = {"match_id": match_id}

    # Phase B - presentation (best effort).
    if error_message is not None:
        await callback.answer(error_message, show_alert=True)
        return

    if waiting_payload is not None:
        await callback.answer("مسابقه رو قبول کردی! منتظر تأیید حریف... 🍏")
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=get_acceptance_keyboard(match_id, has_accepted=True)
            )
        return

    await callback.answer("مسابقه آغاز شد! 🍏🎉")

    from sib.games.registry import get_game_engine

    engine = get_game_engine(board_payload["game_code"])
    state = board_payload["state"]

    # Render interactive board for both groups. The message behind
    # a match-found screen can be a photo (e.g. Apple Battle card
    # art); the central renderer converts/replaces as needed and
    # the stored message ids always follow the live message.
    new_ids = {1: None, 2: None}
    for index, (group_id, msg_id, p_id) in enumerate(
        ((g, m, p) for g, m, p in board_payload["targets"]), start=1
    ):
        if group_id and msg_id:
            board_text = engine.render_for_group(state, group_id, p_id)
            kb = engine.get_keyboard(state, board_payload["match_id"], p_id)
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
                new_ids[index] = new_msg_id
    if new_ids[1] or new_ids[2]:
        await _persist_accept_board_ids(board_payload["match_id"], new_ids[1], new_ids[2])

    # Optional per-game hook (e.g. Apple Battle schedules its staged
    # narration). Default engines implement this as a no-op - and schedule
    # (never await their render loops), so this stays a non-blocking call.
    after_start = getattr(engine, "after_start", None)
    if after_start is not None:
        await after_start(bot, board_payload["match_id"])


async def _persist_accept_board_ids(match_id: int, g1: Optional[int], g2: Optional[int]) -> None:
    """Store board message ids that a text↔photo replacement produced."""
    try:
        from sib.database.models import Match

        async with get_session() as session:
            match = await session.get(Match, match_id)
            if match is None:
                return
            if g1:
                match.group1_message_id = g1
            if g2:
                match.group2_message_id = g2
            await session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not persist accepted-match board ids: {exc}")


@router.callback_query(F.data.startswith("reject_match:"))
async def handle_reject_match(callback: CallbackQuery, bot: Bot):
    """Player rejects match before gameplay."""
    match_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    error_message: Optional[str] = None
    targets = []
    async with get_session() as session:
        try:
            match = await AcceptanceService.reject_match(session, match_id, user.id)
        except (InvalidActionException, MatchNotFoundException) as e:
            await session.rollback()
            error_message = e.message
            match = None
        if error_message is None:
            await session.commit()
            targets = [
                (group_id, msg_id, which)
                for group_id, msg_id, which in (
                    (match.group1_id, match.group1_message_id, 1),
                    (match.group2_id, match.group2_message_id, 2),
                )
            ]

    if error_message is not None:
        await callback.answer(error_message, show_alert=True)
        return

    reject_text = (
        "❌ **این مسابقه توسط یکی از طرفین رد شد.**\n\n"
        "پارتی‌ها مجدداً بازگشتند و هیچ تغییری در ریتینگ رخ نداد 🍏"
    )
    await callback.answer("مسابقه لغو شد 🍏")

    new_ids = {1: None, 2: None}
    for group_id, msg_id, which in targets:
        if group_id and msg_id:
            new_msg_id = await safe_edit_screen(
                bot, group_id, msg_id, reject_text, None, parse_mode="Markdown"
            )
            if new_msg_id and new_msg_id != msg_id:
                new_ids[which] = new_msg_id
    if new_ids[1] or new_ids[2]:
        await _persist_accept_board_ids(match_id, new_ids[1], new_ids[2])
