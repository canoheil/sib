"""Friendly party composition, creator-only bot fill, and direct-start tests."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser
from sqlalchemy import select

from sib.core.errors import InvalidActionException, PartyNotFoundException
from sib.database.models import (
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
    Party,
    PartyMember,
    PartyStatus,
    User,
)
from sib.handlers.party import handlers as party_handlers
from sib.handlers.party.handlers import handle_fill_friendly_bots, handle_start_friendly
from sib.handlers.party.renderer import render_party_card
from sib.keyboards.party import get_match_type_keyboard, get_party_action_keyboard
from sib.matchmaking.matcher import MatchmakingService
from sib.services.friendly_match import FriendlyMatchService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user


async def _friendly_party(session, group, creator, mode="solo"):
    return await PartyService.create_party(
        session,
        group.id,
        creator.id,
        "connect_four",
        mode,
        match_type=MatchType.FRIENDLY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,side_size", [("solo", 1), ("2v2", 2), ("4v4", 4)])
async def test_friendly_humans_can_fill_both_sides(mode, side_size, db_session):
    group = await create_test_group(db_session, f"دوستانه {mode}")
    users = [await create_test_user(db_session, f"بازیکن {mode} {i}") for i in range(side_size * 2)]
    party = await _friendly_party(db_session, group, users[0], mode)

    assert party.match_type == MatchType.FRIENDLY
    assert party.max_players == side_size * 2
    assert party.current_players_count == 1
    assert party.status == PartyStatus.OPEN
    assert party.members[0].team == 1

    for index, user in enumerate(users[1:], start=1):
        party = await PartyService.join_party(
            db_session, party.id, user.id, team=1 if index % 2 == 0 else 2,
            group_id=group.id,
        )
    await db_session.commit()

    counts = {team: sum(member.team == team for member in party.members) for team in (1, 2)}
    assert counts == {1: side_size, 2: side_size}
    assert party.current_players_count == side_size * 2
    assert party.status == PartyStatus.FULL


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected_bots", [("solo", 1), ("2v2", 3), ("4v4", 7)])
async def test_creator_can_fill_friendly_slots_with_persisted_bots(mode, expected_bots, db_session):
    group = await create_test_group(db_session, f"بات‌های {mode}")
    creator = await create_test_user(db_session, f"سازنده {mode}")
    party = await _friendly_party(db_session, group, creator, mode)

    party = await PartyService.fill_bot_slots(
        db_session, party.id, creator.id, group_id=group.id
    )
    await db_session.commit()

    bots = [member for member in party.members if member.user.is_bot]
    humans = [member for member in party.members if not member.user.is_bot]
    assert len(bots) == expected_bots
    assert len(humans) == 1 and humans[0].user_id == creator.id
    assert all("بات" in member.user.display_name and "🤖" in member.user.display_name for member in bots)
    assert all(member.bot_profile for member in bots)
    assert all(json.loads(member.bot_profile)["kind"] == "friendly_party_bot" for member in bots)
    assert len({member.user_id for member in bots}) == expected_bots
    assert party.status == PartyStatus.FULL
    assert party.is_full

    counts = {team: sum(member.team == team for member in party.members) for team in (1, 2)}
    side_capacity = PartyService.MODE_SLOTS[mode]
    assert counts == {1: side_capacity, 2: side_capacity}

    with pytest.raises(InvalidActionException):
        await PartyService.fill_bot_slots(db_session, party.id, creator.id, group_id=group.id)


@pytest.mark.asyncio
async def test_friendly_bot_fill_rejects_non_creator_and_wrong_group(db_session):
    group = await create_test_group(db_session, "گروه بات امن")
    wrong_group = await create_test_group(db_session, "گروه دیگر")
    creator = await create_test_user(db_session, "سازنده بات")
    outsider = await create_test_user(db_session, "کاربر بیرونی")
    party = await _friendly_party(db_session, group, creator, "2v2")

    with pytest.raises(InvalidActionException):
        await PartyService.fill_bot_slots(db_session, party.id, outsider.id, group_id=group.id)
    with pytest.raises(PartyNotFoundException):
        await PartyService.fill_bot_slots(db_session, party.id, creator.id, group_id=wrong_group.id)

    unchanged = await db_session.scalar(
        select(Party).where(Party.id == party.id).execution_options(populate_existing=True)
    )
    assert unchanged.current_players_count == 1
    assert unchanged.status == PartyStatus.OPEN


@pytest.mark.asyncio
async def test_friendly_start_creates_one_same_group_active_match_and_is_stale_safe(db_session):
    group = await create_test_group(db_session, "گروه بازی دوستانه")
    users = [await create_test_user(db_session, f"هم‌گروهی {i}") for i in range(4)]
    party = await _friendly_party(db_session, group, users[0], "2v2")
    party = await PartyService.join_party(db_session, party.id, users[1].id, team=2, group_id=group.id)
    party = await PartyService.join_party(db_session, party.id, users[2].id, team=1, group_id=group.id)
    party = await PartyService.join_party(db_session, party.id, users[3].id, team=2, group_id=group.id)

    match = await FriendlyMatchService.start_party(
        db_session, party.id, users[0].id, group_id=group.id
    )
    await db_session.commit()

    assert match.match_type == MatchType.FRIENDLY
    assert match.state == MatchState.ACTIVE
    assert match.group1_id == group.id and match.group2_id is None
    assert match.party1_id == party.id and match.party2_id is None
    assert len(match.players) == 4
    assert {player.team for player in match.players} == {1, 2}
    assert {player.group_id for player in match.players} == {group.id}
    assert all(player.has_accepted for player in match.players)
    assert json.loads(match.game_state)["game"] == "connect_four"

    fresh_party = await db_session.scalar(
        select(Party).where(Party.id == party.id).execution_options(populate_existing=True)
    )
    assert fresh_party.status == PartyStatus.IN_MATCH
    with pytest.raises(InvalidActionException):
        await FriendlyMatchService.start_party(db_session, party.id, users[0].id, group_id=group.id)


@pytest.mark.asyncio
async def test_competitive_queue_rejects_friendly_party(db_session):
    group = await create_test_group(db_session, "گروه صف دوستانه")
    creator = await create_test_user(db_session, "بازیکن صف دوستانه")
    party = await _friendly_party(db_session, group, creator, "solo")

    with pytest.raises(InvalidActionException, match="دوستانه"):
        await MatchmakingService.enqueue_party(
            db_session, party.id, creator.id, group_id=group.id
        )


@pytest.mark.asyncio
async def test_friendly_party_keyboard_and_card_expose_team_slots_and_clear_actions(db_session):
    group = await create_test_group(db_session, "گروه نمایش تیم")
    creator = await create_test_user(db_session, "سازنده نمایش")
    party = await _friendly_party(db_session, group, creator, "2v2")
    all_buttons = [
        button for row in get_party_action_keyboard(party, is_member=False).inline_keyboard
        for button in row
    ]
    callbacks = {button.callback_data for button in all_buttons}
    assert f"join_party:{party.id}:1" in callbacks
    assert f"join_party:{party.id}:2" in callbacks
    assert f"fill_friendly_bots:{party.id}" not in callbacks
    creator_buttons = [
        button for row in get_party_action_keyboard(party, is_member=True, is_creator=True).inline_keyboard
        for button in row
    ]
    creator_callbacks = {button.callback_data for button in creator_buttons}
    assert f"fill_friendly_bots:{party.id}" in creator_callbacks
    assert f"leave_party:{party.id}" in creator_callbacks
    assert "تیم ۱" in render_party_card(party)
    assert "تیم ۲" in render_party_card(party)
    assert "جای خالی" in render_party_card(party)

    type_buttons = [
        button for row in get_match_type_keyboard(
            party.game, "2v2", back_to="group_active_games"
        ).inline_keyboard for button in row
    ]
    type_callbacks = {button.callback_data for button in type_buttons}
    assert f"create_party:{party.game.code}:2v2:competitive" in type_callbacks
    assert f"create_party:{party.game.code}:2v2:friendly" in type_callbacks


def _friendly_callback(user_id, group_id, party_id, data):
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = TelegramUser(id=user_id, is_bot=False, first_name="بازیکن")
    callback.message = MagicMock(spec=Message)
    callback.message.chat = TelegramChat(id=group_id, type="supergroup", title="گروه")
    callback.message.message_id = 4321
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_friendly_handlers_reject_wrong_group_before_rendering(db_session):
    group = await create_test_group(db_session, "گروه اصلی")
    another_group = await create_test_group(db_session, "گروه دیگر")
    creator = await create_test_user(db_session, "سازنده")
    party = await _friendly_party(db_session, group, creator)

    callback = _friendly_callback(creator.id, another_group.id, party.id, f"fill_friendly_bots:{party.id}")
    await handle_fill_friendly_bots(callback)
    callback.answer.assert_awaited()
    callback.message.edit_text.assert_not_awaited()

    callback = _friendly_callback(creator.id, another_group.id, party.id, f"start_friendly:{party.id}")
    await handle_start_friendly(callback, MagicMock())
    callback.answer.assert_awaited()
    callback.message.edit_text.assert_not_awaited()
    assert await db_session.scalar(select(Match.id).where(Match.party1_id == party.id)) is None


@pytest.mark.asyncio
async def test_start_friendly_handler_persists_one_shared_board_and_rejects_stale_second_tap(
    db_session, monkeypatch
):
    group = await create_test_group(db_session, "گروه شروع handler")
    users = [await create_test_user(db_session, f"شروع {i}") for i in range(2)]
    party = await _friendly_party(db_session, group, users[0], "solo")
    party = await PartyService.join_party(db_session, party.id, users[1].id, team=2, group_id=group.id)
    await db_session.commit()

    monkeypatch.setattr(party_handlers, "safe_edit_callback_message", AsyncMock(return_value=True))
    callback = _friendly_callback(users[0].id, group.id, party.id, f"start_friendly:{party.id}")
    bot = MagicMock()
    await handle_start_friendly(callback, bot)
    callback.message.edit_text.assert_not_awaited()

    matches = list((await db_session.scalars(select(Match).where(Match.party1_id == party.id))).all())
    assert len(matches) == 1
    assert matches[0].group1_message_id == callback.message.message_id
    callback.message_id = callback.message.message_id

    stale = _friendly_callback(users[0].id, group.id, party.id, f"start_friendly:{party.id}")
    await handle_start_friendly(stale, bot)
    assert len(list((await db_session.scalars(select(Match).where(Match.party1_id == party.id))).all())) == 1
    assert stale.message.edit_text.await_count == 0
    assert stale.answer.await_count == 1
