"""Handlers for Friendly matches («بازی با اعضای گروه») — a MATCH TYPE.

Game -> Game Mode -> Match Type:

    create_friendly_party:{code}:{mode}  create the group-internal party
    make_friendly:{party_id}             solo party -> friendly 1v1 party
    friendly_fill_bots:{party_id}        host fills free slots with bots
    friendly_start:{party_id}            start the friendly match (no queue)
    friendly_rematch:{match_id}          restart a finished friendly match

Friendly matches reuse the existing engines, Match model and bot system.
They never touch rank/ELO/MMR, group wins or seedlings; they pay Apples.
"""

import json
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from sib.core.errors import (
    ActiveSessionExistsException,
    InvalidActionException,
    PartyFullException,
    PartyNotFoundException,
    sibException,
)
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Match, MatchState
from sib.games.registry import get_game_engine
from sib.handlers.party.renderer import render_party_card
from sib.keyboards.party import (
    get_friendly_rematch_keyboard,
    get_shared_friendly_party_keyboard,
)
from sib.services.friendly import (
    FriendlyMatchService,
    shared_board_viewer,
    shape_friendly_final_text,
    viewer_audience,
)
from sib.services.group import GroupService
from sib.services.party import PartyService
from sib.services.telegram_ui import (
    safe_edit_callback_message,
    with_refresh_button,
)
from sib.services.user import UserService

router = Router(name="friendly")

_FRIENDLY_PARTY_ERRORS = (
    ActiveSessionExistsException,
    InvalidActionException,
    PartyFullException,
    PartyNotFoundException,
)


async def render_friendly_board(
    callback: CallbackQuery,
    bot: Optional[Bot],
    match,
    viewer_id: int,
) -> bool:
    """Render the (single, shared) friendly board onto the pressed message.

    The board belongs to the whole group, so it is rendered from the acting
    player's side (their radar/cells and a live keyboard for the player who
    must move). Finished boards show the neutral team-named result plus the
    Rematch action.
    """
    if not match or not match.game_state:
        return False
    try:
        state = json.loads(match.game_state)
    except (TypeError, ValueError):
        return False

    engine = get_game_engine(match.game.code)
    try:
        # The board is shared by both teams: render it from the perspective of
        # whoever is on turn (the presser when the game has no current player).
        viewer = shared_board_viewer(match, state, viewer_id) or viewer_id
        audience = viewer_audience(match, state, viewer) or match.group1_id
        text = engine.render_for_group(state, audience, viewer)
    except Exception:  # pragma: no cover - defensive
        return False

    if match.state == MatchState.ACTIVE:
        kb = with_refresh_button(engine.get_keyboard(state, match.id, viewer), match.id)
    else:
        text = shape_friendly_final_text(text, match, state)
        kb = get_friendly_rematch_keyboard(match.id)

    return await safe_edit_callback_message(
        callback, bot, text, reply_markup=kb, parse_mode="Markdown"
    )


async def _remember_friendly_board(match_id: int, group_id: int, message_id: int) -> None:
    """Persist the live board message id of a friendly match (best effort).

    Friendly matches have group1 == group2 and ONE shared board message, so
    both stored ids point at the same live message; the timeout/recovery sync
    and the refresh button then always edit the right message.
    """
    try:
        async with get_session() as session:
            match = await session.get(Match, match_id)
            if match is None:
                return
            match.group1_message_id = message_id
            if match.group2_id == match.group1_id:
                match.group2_message_id = message_id
            await session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not persist friendly board id for match {match_id}: {exc}")


async def _show_friendly_party(callback: CallbackQuery, bot: Optional[Bot], party) -> None:
    """Render the friendly party card onto the shared GROUP message.

    The card belongs to the whole group (it is one message for both teams),
    so its keyboard is rendered for the group - team joins included - and
    is never tailored to the acting host. Tailoring it to the host (the old
    behaviour) left every other member without the join buttons until they
    pressed Refresh.
    """
    text = render_party_card(party)
    kb = get_shared_friendly_party_keyboard(party)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("create_friendly_party:"))
async def handle_create_friendly_party(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Create the friendly party of the chosen game/mode (both teams, one group)."""
    parts = callback.data.split(":")
    game_code = parts[1]
    mode = parts[2] if len(parts) > 2 else "solo"
    user = callback.from_user
    chat = callback.message.chat if callback.message else None

    if not user or not chat:
        return

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        await GroupService.get_or_create_group(session, chat)

        try:
            party = await PartyService.create_friendly_party(
                session=session,
                group_id=chat.id,
                creator_user_id=user.id,
                game_code=game_code,
                mode=mode,
            )
        except _FRIENDLY_PARTY_ERRORS as e:
            await callback.answer(e.message, show_alert=True)
            return

        await callback.answer("پارتی رفیقانه ساخته شد! 🎉🍏")
        await _show_friendly_party(callback, bot, party)


@router.callback_query(F.data.startswith("make_friendly:"))
async def handle_make_friendly(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Solo party card action: play a friendly 1v1 with a group member."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        try:
            party = await PartyService.enable_friendly(session, party_id, user.id)
        except _FRIENDLY_PARTY_ERRORS as e:
            await callback.answer(e.message, show_alert=True)
            return

        await callback.answer("پارتی رفیقانهٔ ۱ به ۱ آماده شد! 🎉🍏")
        await _show_friendly_party(callback, bot, party)


@router.callback_query(F.data.startswith("friendly_fill_bots:"))
async def handle_friendly_fill_bots(callback: CallbackQuery, bot: Optional[Bot] = None):
    """Host action: fill every empty friendly-party slot with a bot."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        try:
            party = await PartyService.fill_empty_slots_with_bots(
                session, party_id, user.id
            )
        except _FRIENDLY_PARTY_ERRORS as e:
            await callback.answer(e.message, show_alert=True)
            return

        await callback.answer("جای‌های خالی با بات پر شد! 🤖🍏")
        await _show_friendly_party(callback, bot, party)


@router.callback_query(F.data.startswith("friendly_start:"))
async def handle_friendly_start(callback: CallbackQuery, bot: Bot):
    """Start a full friendly party as an ACTIVE match (no queue/acceptance)."""
    party_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        await UserService.get_or_create_user(session, user)
        try:
            match = await FriendlyMatchService.start_friendly_match(
                session, party_id, user.id
            )
        except _FRIENDLY_PARTY_ERRORS as e:
            await callback.answer(e.message, show_alert=True)
            return

        await callback.answer("بازی رفیقانه شروع شد! 🎉🍏")

    if callback.message:
        group_id = callback.message.chat.id
        await render_friendly_board(callback, bot, match, user.id)
        board_id = getattr(callback.message, "message_id", None)
        if board_id:
            await _remember_friendly_board(match.id, group_id, board_id)


@router.callback_query(F.data.startswith("friendly_rematch:"))
async def handle_friendly_rematch(callback: CallbackQuery, bot: Bot):
    """Rematch a finished friendly match - same game/mode, always friendly."""
    match_id = int(callback.data.split(":")[1])
    user = callback.from_user

    if not user:
        return

    async with get_session() as session:
        try:
            match = await FriendlyMatchService.rematch(session, match_id, user.id)
        except sibException as e:
            await callback.answer(e.message, show_alert=True)
            return

        await callback.answer("بازی مجدد رفیقانه شروع شد! 🎉🍏")

    if callback.message:
        group_id = callback.message.chat.id
        await render_friendly_board(callback, bot, match, user.id)
        board_id = getattr(callback.message, "message_id", None)
        if board_id:
            await _remember_friendly_board(match.id, group_id, board_id)
