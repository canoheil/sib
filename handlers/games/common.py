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
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
from aiogram import Bot, Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from sib.bots.runner import schedule_bot_turn
from sib.bots.service import GameBotService
from sib.core.logging import logger, log_event, Events
from sib.database.connection import get_session
from sib.database.models import Match, MatchPlayer, MatchState, Group, AdCampaign
from sib.games.registry import get_game_engine
from sib.keyboards.party import get_friendly_rematch_keyboard
from sib.services.battleship_radar import (
    edit_panel,
    ephemeral_message_id,
    is_private_message,
    is_radar_participant,
    panel_view,
    public_view,
    present_panel,
    supports_private_radar,
)
from sib.services.friendly import (
    FriendlyOutcomeView,
    is_friendly,
    shape_friendly_final_text,
    state_viewer_id,
    viewer_audience,
)
from sib.services.match_result import MatchResultService
from sib.services.player_xp import PlayerXPService
from sib.services.ad.service import AdService
from sib.services.telegram_ui import (
    REFRESH_CALLBACK_PREFIX,
    board_refresh_style,
    safe_answer_callback,
    safe_edit_callback_message,
    safe_edit_board,
    safe_edit_screen,
    with_refresh_button,
)


async def _edit_game_screen(
    bot: Bot, chat_id: int, message_id: int, text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[int]:
    """Board transport with fresh-message recovery; caller persists a new id."""
    target = SimpleNamespace(
        bot=bot, message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_id=message_id
        )
    )
    if await safe_edit_board(target, text, reply_markup=reply_markup, parse_mode="Markdown"):
        return message_id
    try:
        sent = await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown"
        )
        return sent.message_id
    except Exception as exc:
        logger.warning("Board replacement failed message_id=%s: %s", message_id, exc)
        return None


async def _edit_game_callback(callback: CallbackQuery, bot: Bot, text: str, *, reply_markup=None) -> bool:
    message = callback.message
    if message is None:
        return False
    new_id = await _edit_game_screen(
        bot, message.chat.id, message.message_id, text, reply_markup
    )
    if new_id is None:
        return False
    if new_id != message.message_id:
        # Preserve the callback's message type and all other metadata.
        replacement = message.model_copy(update={"message_id": new_id})
        object.__setattr__(callback, "message", replacement)
    return True


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
                if match.group2_id == match.group1_id:
                    # Single-group friendly match: both stored boards are
                    # the same live message.
                    match.group2_message_id = message_id
            elif group_id == match.group2_id:
                match.group2_message_id = message_id
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            f"Could not persist replaced board id for match {match_id}: {exc}"
        )


def get_rematch_keyboard(
    ad: Optional[AdCampaign] = None, match: Optional[Match] = None
) -> InlineKeyboardMarkup:
    """Keyboard shown after game completion with optional sponsor CTA.

    FINISHED friendly matches get the friendly Rematch action instead of the
    normal group ad/new-game row: rematch recreates the same game/mode as a
    friendly match (never a ranked one).
    """
    from sib.keyboards.common import primary_button, success_button

    if match is not None and is_friendly(match):
        return get_friendly_rematch_keyboard(match.id)

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



STALE_PANEL_NOTICE = "⚠️ این صفحه قدیمی بود؛ وضعیت رسمی بازی بارگذاری شد."


def _is_private_surface(message: Any) -> bool:
    """True when the pressed message is a private panel (ephemeral or DM).

    Ephemeral messages carry ``message_id == 0`` and a DM is a private chat;
    neither may ever be treated as the shared group board.
    """
    return bool(ephemeral_message_id(message)) or is_private_message(message)


def _friendly_board_target(
    state: Dict[str, Any],
    players: Any,
    actor_id: int,
    fallback_group_id: Optional[int],
) -> Tuple[Optional[int], Optional[int]]:
    """(viewer_player_id, audience_group_id) for a friendly shared board.

    Only used by engines WITHOUT a private radar (RPS / High Card), where the
    single shared board is genuinely neutral or blind-commit. Battleship never
    flips the board perspective any more - it renders ``render_public`` in the
    group and a private panel for each player.

    ``players`` is the ``[(user_id, group_id), ...]`` snapshot taken before
    the DB session closed; bot players have ``group_id=None``.
    """
    human_ids = {pid for pid, gid in players if gid is not None}
    viewer = state_viewer_id(state, human_ids, actor_id)
    info = (state.get("players") or {}).get(str(viewer)) if viewer else None
    audience = info.get("group_id") if info else None
    if audience is None:
        audience = fallback_group_id
    return viewer, (int(audience) if audience is not None else None)


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
    refresh_style = board_refresh_style(engine.game_code)
    message = callback.message
    chat = getattr(message, "chat", None) if message is not None else None
    chat_id = getattr(chat, "id", None) if chat is not None else None
    group_id = chat_id or match.group1_id or for_player_id

    if is_friendly(match) and supports_private_radar(engine):
        # Battleship: the group board is neutral and the radar panel is
        # private, so a rejected/stale tap heals both surfaces without ever
        # putting a grid in the group message.
        await _recover_friendly_radar_board(
            callback, bot, match, state, for_player_id, final=final
        )
        return

    if is_friendly(match):
        # Friendly boards of other engines are shared by both teams in one
        # group: render from the current player's team perspective.
        friendly_viewer = state_viewer_id(
            state, {p.user_id for p in match.players if p.group_id is not None}, for_player_id
        )
        audience = viewer_audience(match, state, friendly_viewer) or group_id
        text = engine.render_for_group(state, audience, friendly_viewer)
        if final:
            text = shape_friendly_final_text(text, match, state)
            kb = get_rematch_keyboard(match=match)
        else:
            kb = with_refresh_button(
                engine.get_keyboard(state, match.id, friendly_viewer),
                match.id,
                refresh_style=refresh_style,
            )
        before_id = getattr(message, "message_id", None)
        await _edit_game_callback(callback, bot, text, reply_markup=kb)
        after_id = getattr(callback.message, "message_id", None)
        if before_id and after_id and after_id != before_id and match.group1_message_id == before_id:
            match.group1_message_id = after_id
            match.group2_message_id = after_id
            await _persist_board_message_id(match.id, group_id, after_id)
        return

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
        kb = get_rematch_keyboard(selected_ad, match=match)
    else:
        kb = with_refresh_button(
            engine.get_keyboard(state, match.id, for_player_id),
            match.id,
            refresh_style=refresh_style,
        )
    before_id = getattr(message, "message_id", None)
    await _edit_game_callback(callback, bot, text, reply_markup=kb)
    after_id = getattr(callback.message, "message_id", None)
    if before_id and after_id and after_id != before_id:
        if group_id == match.group1_id and match.group1_message_id == before_id:
            match.group1_message_id = after_id
        elif group_id == match.group2_id and match.group2_message_id == before_id:
            match.group2_message_id = after_id
        await _persist_board_message_id(match.id, group_id, after_id)


async def _recover_friendly_radar_board(
    callback: CallbackQuery,
    bot: Bot,
    match: Match,
    state: Dict[str, Any],
    player_id: int,
    *,
    final: bool = False,
    panel_notice: Optional[str] = STALE_PANEL_NOTICE,
) -> None:
    """Re-render a private-radar friendly match from the authoritative state.

    Used after a rejected/stale tap and by the 🔄 button. The caller's private
    panel (when they pressed inside one) is repaired privately; the shared
    group message always receives the neutral public board.
    """
    engine = get_game_engine(match.game.code)
    tapped = getattr(callback, "message", None)
    group_id = match.group1_id or match.group2_id

    if _is_private_surface(tapped) and is_radar_participant(engine, state, player_id):
        panel_text, panel_kb = panel_view(
            engine, match.id, state, player_id, notice=panel_notice
        )
        await edit_panel(
            bot, tapped, player_id=player_id, text=panel_text, keyboard=panel_kb
        )

    text, kb = public_view(engine, match.id, state, finished=final)
    message_id = match.group1_message_id or match.group2_message_id
    if message_id and group_id:
        new_msg_id = await _edit_game_screen(
            bot, chat_id=group_id, message_id=message_id, text=text, reply_markup=kb
        )
        if new_msg_id and new_msg_id != message_id:
            await _persist_board_message_id(match.id, group_id, new_msg_id)
        elif new_msg_id is None:
            logger.warning(
                f"Public friendly board recovery failed for match {match.id} "
                f"in group {group_id}; refresh button remains available."
            )
        return

    if _is_private_surface(tapped):
        return
    await _edit_game_callback(callback, bot, text, reply_markup=kb)


DUPLICATE_MOVE_MESSAGE = "صفحه‌ی بازی تازه شد؛ حرکت شما قبلاً ثبت شده بود."
STALE_TURN_MESSAGE = "صفحه‌ی بازی تازه شد؛ اکنون نوبت بازیکن دیگری است."


def _duplicate_feedback(reason: Optional[str], participant: bool) -> Optional[str]:
    if not participant:
        return None
    if reason and "قبلاً" in reason:
        return DUPLICATE_MOVE_MESSAGE
    # Sequential engines check turn ownership before repeated-cell checks.
    # Without action ids we cannot prove a duplicate: use neutral stale-turn text.
    if reason == "هنوز نوبت شما نشده است.":
        return STALE_TURN_MESSAGE
    return None


async def process_game_action(
    callback: CallbackQuery,
    bot: Bot,
    match_id: int,
    action: Dict[str, Any],
    *,
    ephemeral_card: bool = False,
    blind_commit: bool = False,
) -> None:
    """Generic atomic handler for any game action."""
    user = callback.from_user
    if not user:
        return

    # ------------------------------------------------------------------
    # Phase A: authoritative state transition (pure DB work, no UI).
    #
    # Invariant: this transaction locks the match row (FOR UPDATE), so the
    # block must contain NO Telegram await at all - every acknowledgement and
    # every board render (error paths included) happens AFTER Phase A has
    # committed. A render held under the row lock would make a bot's
    # ``_apply_one_bot_move`` wait for the whole presentation - flood-retry
    # sleeps included (up to ~30s).
    # ------------------------------------------------------------------
    selected_ad: Optional[AdCampaign] = None
    render_payload: Optional[Dict[str, Any]] = None
    early_exit: Optional[Dict[str, Any]] = None
    private_card_text: Optional[str] = None
    private_card_group_id: Optional[int] = None

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
            early_exit = {"kind": "not_found", "match": None, "state": None}
        elif match.state != MatchState.ACTIVE:
            # Self-heal: the visible keyboard is stale (e.g. the match was
            # resolved by a turn timeout in the background). The recovery
            # render happens below - after this row lock is released.
            early_exit = {
                "kind": "inactive",
                "match": match,
                "state": (
                    json.loads(match.game_state) if match.game_state else None
                ),
            }

        if early_exit is None:
            engine = get_game_engine(match.game.code)

            # Deserialize or initialize game state
            if not match.game_state:
                state = engine.create_initial_state(match.players, match.mode)
            else:
                state = json.loads(match.game_state)

            # Validate action
            is_valid, reason = engine.validate_action(state, user.id, action)
            if not is_valid:
                participant = any(p.user_id == user.id for p in match.players)
                feedback = _duplicate_feedback(reason, participant)
                player_state = (state.get("players") or {}).get(str(user.id), {})
                repeated_high_card_draw = (
                    match.game.code == "high_card"
                    and action.get("draw")
                    and player_state.get("card_rank") is not None
                )
                if repeated_high_card_draw:
                    feedback = DUPLICATE_MOVE_MESSAGE

                # A double-click on one of these friendly simultaneous games
                # must not re-render the shared board or expose any staged
                # private card/choice. The match row is still locked above, so
                # the duplicate is rejected against committed state.
                silent_duplicate = bool(
                    is_friendly(match)
                    and participant
                    and (
                        (match.game.code == "rock_paper_scissors"
                         and player_state.get("locked"))
                        or (match.game.code == "high_card"
                            and ("قبلاً" in (reason or "") or repeated_high_card_draw))
                    )
                )
                early_exit = {
                    "kind": "silent_duplicate" if silent_duplicate else "invalid",
                    "reason": reason,
                    "feedback": feedback,
                    "match": match,
                    "state": state,
                }
            else:
                # Apply action
                state = engine.apply_action(state, user.id, action)

                if ephemeral_card and match.game.code == "high_card":
                    player_state = (state.get("players") or {}).get(str(user.id), {})
                    if player_state.get("card_rank") is not None:
                        private_card_text = f"🃏 کارت شما: {engine._card_str(player_state)}"
                        private_card_group_id = next(
                            (p.group_id for p in match.players
                             if p.user_id == user.id and p.group_id is not None),
                            None,
                        )
                        if private_card_group_id is None and callback.message:
                            private_card_group_id = callback.message.chat.id

                match.game_state = json.dumps(state, ensure_ascii=False)

                # Keep Match timeout metadata synchronized with the game
                # engine state.
                GameBotService.sync_turn_metadata(match, state, engine)

                log_event(
                    Events.MOVE_PLAYED,
                    {
                        "match_id": match.id,
                        "user_id": user.id,
                        "action": action,
                        "game": match.game.code,
                    },
                )

                finished = engine.is_finished(state)
                result = None
                if finished:
                    result = engine.get_result(state)
                    # Apply the result (idempotent) BEFORE committing.
                    await MatchResultService.apply_match_result(session, match, result)

                    # Fetch eligible post-match ad safely (never affects the
                    # match result if it fails) - DB-only work.
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
                            await AdService.record_impression(
                                session, selected_ad.id, match_id=match.id
                            )
                    except Exception as ad_err:
                        logger.warning(
                            f"Non-fatal error delivering post-match ad: {ad_err}"
                        )
                        selected_ad = None

                # Commit the authoritative state before touching Telegram.
                # From this point on, presentation failures cannot affect the
                # result - and the match row lock no longer exists while the
                # board is rendered or a bot turn task runs.
                await session.commit()

                # Snapshot everything the presentation layer needs.
                render_payload = {
                    "finished": finished,
                    "state": state,
                    "match_id": match.id,
                    "is_friendly": is_friendly(match),
                    "is_draw": match.is_draw,
                    "winner_team": match.winner_team,
                    "match_type": match.match_type,
                    "is_bot_match": match.is_bot_match,
                    "group1_id": match.group1_id,
                    "group2_id": match.group2_id,
                    "group1_message_id": match.group1_message_id,
                    "group2_message_id": match.group2_message_id,
                    "group1_wins": (
                        match.group1.stats.wins
                        if match.group1 and match.group1.stats
                        else 0
                    ),
                    "group2_wins": (
                        match.group2.stats.wins
                        if match.group2 and match.group2.stats
                        else 0
                    ),
                    "game_id": match.game_id,
                    "game_code": match.game.code,
                    "players": [(p.user_id, p.group_id) for p in match.players],
                    "private_card_text": private_card_text,
                    "private_card_group_id": private_card_group_id,
                    "blind_commit": blind_commit,
                }

        if early_exit is not None:
            # Nothing was written on the error paths. Commit the read-only
            # transaction (NOT rollback - rollback would expire the loaded
            # match and its eager-loaded relations, which the recovery render
            # below must still read): the FOR UPDATE lock is released before
            # any presentation await.
            await session.commit()

    if early_exit is not None:
        if early_exit["kind"] == "silent_duplicate":
            # Clear the callback spinner without a toast/alert or a board edit.
            await safe_answer_callback(callback)
            return
        if early_exit["kind"] == "not_found":
            await safe_answer_callback(callback, "مسابقه پیدا نشد 🍏", show_alert=True)
            return
        if early_exit["kind"] == "invalid":
            try:
                await render_board_for(
                    callback, bot, early_exit["match"], early_exit["state"], user.id
                )
            except Exception as render_err:
                logger.warning(f"Rejected-action recovery render failed: {render_err}")
            feedback = early_exit.get("feedback")
            await safe_answer_callback(
                callback,
                feedback or ((early_exit["reason"] or "حرکت نامعتبر است 🍏")
                             + "\nآخرین وضعیت بازی بارگذاری شد."),
                show_alert=not bool(feedback),
            )
            return
        # Inactive/stale board: answer, then render the true final board.
        await safe_answer_callback(
            callback,
            "این مسابقه دیگر در جریان نیست؛ آخرین وضعیت بازی بارگذاری شد. 🍏",
            show_alert=not any(p.user_id == user.id for p in early_exit["match"].players),
        )
        if early_exit["state"] is not None:
            try:
                await render_board_for(
                    callback,
                    bot,
                    early_exit["match"],
                    early_exit["state"],
                    user.id,
                    final=True,
                )
            except Exception as render_err:
                logger.warning(f"Stale-board recovery render failed: {render_err}")
        return

    # ------------------------------------------------------------------
    # Phase B: presentation (best effort, never authoritative).
    # ------------------------------------------------------------------
    assert render_payload is not None
    state = render_payload["state"]
    finished = render_payload["finished"]
    callback_answered = False
    private_card_delivered = False

    if render_payload.get("private_card_text"):
        target_group_id = render_payload.get("private_card_group_id")
        try:
            if target_group_id is None:
                raise ValueError("No group chat is available for the private card")
            await bot.send_message(
                chat_id=target_group_id,
                text=render_payload["private_card_text"],
                receiver_user_id=user.id,
                parse_mode=None,
            )
            private_card_delivered = True
        except Exception as exc:
            # Ephemeral delivery is explicitly best-effort. A callback alert
            # is the private fallback; the committed move and group render
            # continue regardless of the Bot API send failure.
            logger.warning(
                "Ephemeral High Card delivery failed for match %s/user %s: %s",
                match_id,
                user.id,
                exc,
            )
            await safe_answer_callback(
                callback,
                render_payload["private_card_text"],
                show_alert=True,
            )
            callback_answered = True

    has_message_ids = bool(
        render_payload["group1_message_id"] or render_payload["group2_message_id"]
    )

    # The human move is committed above. If the turn now belongs to a bot,
    # hand the delay to a detached bot-turn task: the humanization sleep must
    # never run on this request, nor inside the transaction it just closed.
    # The task re-reads the state itself and exits when no bot must act.
    if render_payload["is_bot_match"] and not finished:
        schedule_bot_turn(render_payload["match_id"], bot=bot)

    # RPS is simultaneous: in a friendly match, leave the public board and
    # its generic choice keyboard untouched until the second player commits.
    if (
        render_payload.get("is_friendly")
        and render_payload.get("blind_commit")
        and render_payload["game_code"] == "rock_paper_scissors"
        and not finished
    ):
        rps_players = list((state.get("players") or {}).values())
        if (
            rps_players
            and not state.get("reveal_pending")
            and any(player.get("locked") for player in rps_players)
        ):
            await safe_answer_callback(
                callback,
                "✅ انتخاب شما ثبت شد. منتظر حریف...",
                show_alert=False,
            )
            return

    # Friendly matches share ONE board message between both teams (group1 and
    # group2 are the same group): render exactly one board, from the team of
    # the player who is (or should be) on turn.
    if render_payload.get("is_friendly"):
        await _present_friendly_board(
            callback,
            bot,
            render_payload,
            user.id,
            callback_answered=callback_answered,
            private_card_delivered=private_card_delivered,
        )
        if finished:
            await PlayerXPService.deliver_pending_notifications(
                bot,
                fallback_chat_id=render_payload["group1_id"] or render_payload["group2_id"],
            )
        return

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
        if not callback_answered:
            acknowledgement = (
                "✅ کارت به‌صورت خصوصی برای شما ارسال شد."
                if private_card_delivered
                else "حرکت شما ثبت شد! 🍏"
            )
            await safe_answer_callback(callback, acknowledgement, show_alert=False)

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
                        engine.get_keyboard(state, match_id, target_pid),
                        match_id,
                        refresh_style=board_refresh_style(engine.game_code),
                    )
                    new_msg_id = await _edit_game_screen(
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
                engine.get_keyboard(state, match_id, target_pid),
                match_id,
                refresh_style=board_refresh_style(engine.game_code),
            )
            if not await _edit_game_callback(callback, bot, text, reply_markup=kb):
                logger.warning(
                    f"Game board refresh failed for match {match_id} "
                    f"in private flow; refresh button remains available."
                )
        return

    # Match IS finished!
    if not callback_answered:
        await safe_answer_callback(callback, "بازی به پایان رسید! 🍏✨")

    # (Friendly finals returned earlier through _present_friendly_board.)
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
                new_msg_id = await _edit_game_screen(
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
        await _edit_game_callback(callback, bot, final_text, reply_markup=rematch_kb)

    await PlayerXPService.deliver_pending_notifications(bot)


async def _present_friendly_public_board(
    callback: CallbackQuery,
    bot: Bot,
    payload: Dict[str, Any],
    actor_id: int,
    engine: Any,
    *,
    callback_answered: bool = False,
) -> None:
    """Friendly match of an engine with a PRIVATE radar (Battleship).

    Order matters: the shooter's ephemeral panel is refreshed FIRST, because
    an ephemeral message must be sent/edited within 15 seconds of the tap and
    the group-board edit below can spend seconds in flood-wait retries. The
    group message then receives the neutral public board - never a radar.
    """
    state = payload["state"]
    finished = payload["finished"]
    match_id = payload["match_id"]
    group_id = payload["group1_id"] or payload["group2_id"]
    is_participant = any(pid == actor_id for pid, _gid in payload["players"])

    panel = None
    if is_participant:
        # A tap on the group board opens the panel; a tap INSIDE a panel edits
        # that same panel in place (see ``present_panel``).
        panel = await present_panel(
            bot,
            callback,
            chat_id=group_id,
            player_id=actor_id,
            engine=engine,
            match_id=match_id,
            state=state,
        )

    if not callback_answered:
        if panel is not None and not panel.delivered:
            # No private channel worked: a ≤200-char alert, never board text.
            await safe_answer_callback(
                callback,
                "حرکت ثبت شد، ولی پنل رادار باز نشد. «🔄 بروزرسانی» را بزن.",
                show_alert=True,
            )
        elif finished:
            await safe_answer_callback(callback, "بازی رفیقانه به پایان رسید! 🍏✨")
        else:
            await safe_answer_callback(callback, "حرکت شما ثبت شد! 🍏")
        callback_answered = True

    await _edit_public_friendly_board(
        callback,
        bot,
        payload,
        engine,
        state,
        finished=finished,
        group_id=group_id,
    )


async def _edit_public_friendly_board(
    callback: CallbackQuery,
    bot: Bot,
    payload: Dict[str, Any],
    engine: Any,
    state: Dict[str, Any],
    *,
    finished: bool,
    group_id: Optional[int],
) -> None:
    match_id = payload["match_id"]
    text, kb = public_view(engine, match_id, state, finished=finished)
    message_id = payload["group1_message_id"] or payload["group2_message_id"]

    if message_id and group_id:
        new_msg_id = await _edit_game_screen(
            bot, chat_id=group_id, message_id=message_id, text=text, reply_markup=kb
        )
        if new_msg_id and new_msg_id != message_id:
            await _persist_board_message_id(match_id, group_id, new_msg_id)
        elif new_msg_id is None:
            logger.warning(
                f"Public friendly board refresh failed for match {match_id} "
                f"in group {group_id}; refresh button remains available."
            )
        return

    if _is_private_surface(getattr(callback, "message", None)):
        # No stored group board and the tap came from a private panel: never
        # write the public board into that panel (it would hide the radar and
        # confuse the owner). The group board is refreshed by 🔄 / next action.
        logger.warning(
            f"Public friendly board has no stored message for match {match_id}; "
            "skipping the render."
        )
        return

    if not await _edit_game_callback(callback, bot, text, reply_markup=kb):
        logger.warning(
            f"Public friendly board render failed for match {match_id} "
            "in the callback message; refresh button remains available."
        )


async def _present_friendly_board(
    callback: CallbackQuery,
    bot: Bot,
    payload: Dict[str, Any],
    actor_id: int,
    *,
    callback_answered: bool = False,
    private_card_delivered: bool = False,
) -> None:
    """Present the shared board of a friendly match (best effort)."""
    state = payload["state"]
    finished = payload["finished"]
    engine = get_game_engine(payload["game_code"])
    refresh_style = board_refresh_style(engine.game_code)

    if supports_private_radar(engine):
        await _present_friendly_public_board(
            callback,
            bot,
            payload,
            actor_id,
            engine,
            callback_answered=callback_answered,
        )
        return
    group_id = payload["group1_id"] or payload["group2_id"]
    viewer, audience = _friendly_board_target(
        state, payload["players"], actor_id, group_id
    )
    outcome_view = FriendlyOutcomeView(
        payload["match_id"], payload.get("is_draw") or False, payload.get("winner_team")
    )

    if finished:
        if not callback_answered:
            await safe_answer_callback(callback, "بازی رفیقانه به پایان رسید! 🍏✨")
        text = shape_friendly_final_text(
            engine.render_for_group(state, audience, viewer), outcome_view, state
        )
        kb = get_friendly_rematch_keyboard(payload["match_id"])
    else:
        if not callback_answered:
            acknowledgement = (
                "✅ کارت به‌صورت خصوصی برای شما ارسال شد."
                if private_card_delivered
                else "حرکت شما ثبت شد! 🍏"
            )
            await safe_answer_callback(callback, acknowledgement, show_alert=False)
        text = engine.render_for_group(state, audience, viewer)
        kb = with_refresh_button(
            engine.get_keyboard(state, payload["match_id"], viewer),
            payload["match_id"],
            refresh_style=refresh_style,
        )

    message_id = payload["group1_message_id"] or payload["group2_message_id"]
    if message_id and group_id:
        new_msg_id = await _edit_game_screen(
            bot, chat_id=group_id, message_id=message_id, text=text, reply_markup=kb
        )
        if new_msg_id and new_msg_id != message_id:
            await _persist_board_message_id(payload["match_id"], group_id, new_msg_id)
        elif new_msg_id is None:
            logger.warning(
                f"Friendly board refresh failed for match {payload['match_id']} "
                f"in group {group_id}; refresh button remains available."
            )
        return

    if not await _edit_game_callback(callback, bot, text, reply_markup=kb):
        logger.warning(
            f"Friendly board refresh failed for match {payload['match_id']} "
            f"in private flow; refresh button remains available."
        )


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

    radar_refresh = False
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

        message = callback.message
        chat = getattr(message, "chat", None) if message is not None else None
        chat_id = getattr(chat, "id", None)

        is_participant = any(p.user_id == user.id for p in match.players)
        # The friendly board is a SHARED group message: any member of that
        # group may refresh it (it is public group information), while normal
        # matches stay participant-only.
        if not is_participant and not (is_friendly(match) and chat_id == match.group1_id):
            await safe_answer_callback(
                callback, "فقط بازیکنان این مسابقه می‌توانند صفحه را بروزرسانی کنند.", show_alert=True
            )
            return

        engine = get_game_engine(match.game.code)
        refresh_style = board_refresh_style(engine.game_code)
        state = json.loads(match.game_state) if match.game_state else None
        if state is None:
            await safe_answer_callback(
                callback, "این مسابقه هنوز شروع نشده.", show_alert=True
            )
            return

        group_id = chat_id or user.id

        if is_friendly(match) and supports_private_radar(engine):
            # Battleship: the group board is public and the radar is private.
            # Both are re-rendered from the authoritative state after this
            # session closes (never inside it).
            radar_refresh = True
            text = kb = None
        elif is_friendly(match):
            viewer = state_viewer_id(
                state, {p.user_id for p in match.players if p.group_id is not None}, user.id
            )
            audience = viewer_audience(match, state, viewer) or group_id
            text = engine.render_for_group(state, audience, viewer)
            if match.state == MatchState.ACTIVE:
                kb = with_refresh_button(
                    engine.get_keyboard(state, match_id, viewer),
                    match_id,
                    refresh_style=refresh_style,
                )
            else:
                text = shape_friendly_final_text(text, match, state)
                kb = get_friendly_rematch_keyboard(match.id)
        else:
            text = engine.render_for_group(state, group_id, user.id)
            if match.state == MatchState.ACTIVE:
                kb = with_refresh_button(
                    engine.get_keyboard(state, match_id, user.id),
                    match_id,
                    refresh_style=refresh_style,
                )
            else:
                kb = None

    if radar_refresh:
        await safe_answer_callback(callback, "صفحه از وضعیت رسمی بازی بروزرسانی شد. 🍏")
        await _recover_friendly_radar_board(
            callback,
            bot,
            match,
            state,
            user.id,
            final=match.state != MatchState.ACTIVE,
            panel_notice=None,
        )
        return

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
