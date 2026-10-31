"""Tests for the /leave_party command and Persian text shortcuts."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Chat as TelegramChat
from aiogram.types import Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from sib.database.models import (
    Match,
    MatchPlayer,
    MatchState,
    MatchmakingQueue,
    Party,
    PartyMember,
    PartyStatus,
)
from sib.handlers.party import party_router
from sib.handlers.party.handlers import (
    LEADER_TRANSFERRED_TEXT,
    LEFT_PARTY_TEXT,
    NOT_IN_PARTY_TEXT,
    PARTY_DISBANDED_TEXT,
    cmd_leave_party,
)
from sib.matchmaking.matcher import MatchmakingService
from sib.services.bot_commands import PUBLIC_COMMANDS
from sib.services.party import PartyService
from sib.services.session import SessionService
from sib.tests.test_parties import create_test_group, create_test_user


def _mock_message(user, group, text: str = "/leave_party") -> Message:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.chat = TelegramChat(id=group.id, type="supergroup", title=group.title)
    msg.from_user = TelegramUser(
        id=user.id, is_bot=False, first_name=user.first_name
    )
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_leave_party_when_not_in_party(db_session):
    """Replying when not in any active party informs the user."""
    group = await create_test_group(db_session, "گروه بدون پارتی")
    user = await create_test_user(db_session, "کاربر آزاد")

    msg = _mock_message(user, group, "/leave_party")
    await cmd_leave_party(msg)

    msg.reply.assert_awaited_once_with(NOT_IN_PARTY_TEXT)


@pytest.mark.asyncio
async def test_leave_party_as_regular_member(db_session):
    """Regular member leaves party; party reopens and leader stays unchanged."""
    group = await create_test_group(db_session, "گروه خروج عضو")
    leader = await create_test_user(db_session, "لیدر")
    member = await create_test_user(db_session, "عضو عادی")

    party = await PartyService.create_party(
        db_session, group.id, leader.id, "connect_four", "2v2"
    )
    await PartyService.join_party(db_session, party.id, member.id)
    await db_session.commit()

    msg = _mock_message(member, group, "/leave_party")
    await cmd_leave_party(msg)

    msg.reply.assert_awaited_once_with(LEFT_PARTY_TEXT)

    refreshed = await db_session.scalar(
        select(Party)
        .where(Party.id == party.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed.status == PartyStatus.OPEN
    assert refreshed.current_players_count == 1
    assert refreshed.creator_user_id == leader.id
    assert await SessionService.has_active_session(db_session, member.id) is False
    assert await SessionService.has_active_session(db_session, leader.id) is True


@pytest.mark.asyncio
async def test_leave_party_as_leader_transfers_leadership_to_oldest_member(db_session):
    """Leader leaving with other members transfers leadership in join order."""
    group = await create_test_group(db_session, "گروه انتقال لیدری")
    u1 = await create_test_user(db_session, "لیدر اول")
    u2 = await create_test_user(db_session, "نفر دوم")
    u3 = await create_test_user(db_session, "نفر سوم")

    party = await PartyService.create_party(
        db_session, group.id, u1.id, "connect_four", "4v4"
    )
    await PartyService.join_party(db_session, party.id, u2.id)
    await PartyService.join_party(db_session, party.id, u3.id)
    await db_session.commit()

    # Original leader u1 leaves -> leadership transfers to u2
    msg1 = _mock_message(u1, group, "/leave_party")
    await cmd_leave_party(msg1)

    msg1.reply.assert_awaited_once_with(LEADER_TRANSFERRED_TEXT)

    refreshed = await db_session.scalar(
        select(Party)
        .where(Party.id == party.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed.status == PartyStatus.OPEN
    assert refreshed.current_players_count == 2
    assert refreshed.creator_user_id == u2.id
    assert await SessionService.has_active_session(db_session, u1.id) is False

    # New leader u2 leaves -> leadership transfers to u3
    msg2 = _mock_message(u2, group, "ترک پارتی")
    await cmd_leave_party(msg2)

    msg2.reply.assert_awaited_once_with(LEADER_TRANSFERRED_TEXT)

    refreshed = await db_session.scalar(
        select(Party)
        .where(Party.id == party.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed.status == PartyStatus.OPEN
    assert refreshed.current_players_count == 1
    assert refreshed.creator_user_id == u3.id

    # Last remaining member/leader u3 leaves -> party disbanded
    msg3 = _mock_message(u3, group, "خروج از پارتی")
    await cmd_leave_party(msg3)

    msg3.reply.assert_awaited_once_with(PARTY_DISBANDED_TEXT)

    refreshed = await db_session.scalar(
        select(Party)
        .where(Party.id == party.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed.status == PartyStatus.CANCELLED
    assert refreshed.current_players_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["solo", "2v2"])
async def test_leave_party_as_solo_leader_disbands_party(db_session, mode):
    """Sole member/leader leaving disbands (cancels) the party."""
    group = await create_test_group(db_session, f"گروه انحلال {mode}")
    leader = await create_test_user(db_session, f"تک‌نفر {mode}")

    party = await PartyService.create_party(
        db_session, group.id, leader.id, "connect_four", mode
    )
    await db_session.commit()

    msg = _mock_message(leader, group, "/leave_party")
    await cmd_leave_party(msg)

    msg.reply.assert_awaited_once_with(PARTY_DISBANDED_TEXT)

    refreshed = await db_session.scalar(
        select(Party)
        .where(Party.id == party.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed.status == PartyStatus.CANCELLED
    assert refreshed.current_players_count == 0
    assert await SessionService.has_active_session(db_session, leader.id) is False


@pytest.mark.asyncio
async def test_leave_party_cleans_up_active_matchmaking_queue(db_session):
    """Leaving a queued party deactivates its matchmaking queue entry and session."""
    group = await create_test_group(db_session, "گروه صف")
    u1 = await create_test_user(db_session, "بازیکن صف ۱")
    u2 = await create_test_user(db_session, "بازیکن صف ۲")

    party = await PartyService.create_party(
        db_session, group.id, u1.id, "connect_four", "2v2"
    )
    await PartyService.join_party(db_session, party.id, u2.id)
    queue_entry = await MatchmakingService.enqueue_party(db_session, party.id, u1.id)
    await db_session.commit()

    assert queue_entry.is_active is True

    msg = _mock_message(u2, group, "/leave_party")
    await cmd_leave_party(msg)

    msg.reply.assert_awaited_once_with(LEFT_PARTY_TEXT)

    refreshed_party = await db_session.scalar(
        select(Party)
        .where(Party.id == party.id)
        .execution_options(populate_existing=True)
    )
    refreshed_queue = await db_session.scalar(
        select(MatchmakingQueue)
        .where(MatchmakingQueue.id == queue_entry.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed_party.status == PartyStatus.OPEN
    assert refreshed_queue.is_active is False
    assert await SessionService.has_active_session(db_session, u2.id) is False


@pytest.mark.asyncio
async def test_leave_party_friendly_with_bots_skips_bots_for_leadership(db_session):
    """Friendly party bots never inherit leadership; bot-only parties disband."""
    group = await create_test_group(db_session, "گروه رفیقانه بات")
    host = await create_test_user(db_session, "میزبان")
    guest = await create_test_user(db_session, "مهمان")

    # 1) Host + bots only -> disbands when host leaves
    solo_friendly = await PartyService.create_friendly_party(
        db_session, group.id, host.id, "connect_four", "solo"
    )
    await PartyService.fill_empty_slots_with_bots(
        db_session, solo_friendly.id, host.id
    )
    await db_session.commit()

    msg = _mock_message(host, group, "/leave_party")
    await cmd_leave_party(msg)
    msg.reply.assert_awaited_once_with(PARTY_DISBANDED_TEXT)

    refreshed_solo = await db_session.scalar(
        select(Party)
        .where(Party.id == solo_friendly.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed_solo.status == PartyStatus.CANCELLED
    assert refreshed_solo.current_players_count == 0

    # 2) Host + bots + human guest -> transfers leadership to human guest
    team_friendly = await PartyService.create_friendly_party(
        db_session, group.id, host.id, "connect_four", "2v2"
    )
    await PartyService.join_party(db_session, team_friendly.id, guest.id, team=2)
    await PartyService.fill_empty_slots_with_bots(
        db_session, team_friendly.id, host.id
    )
    await db_session.commit()

    msg2 = _mock_message(host, group, "/leave_party")
    await cmd_leave_party(msg2)
    msg2.reply.assert_awaited_once_with(LEADER_TRANSFERRED_TEXT)

    refreshed_team = await db_session.scalar(
        select(Party)
        .where(Party.id == team_friendly.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed_team.status == PartyStatus.OPEN
    assert refreshed_team.creator_user_id == guest.id


@pytest.mark.asyncio
async def test_leave_party_leaves_all_active_parties_and_preserves_live_match(db_session):
    """User in multiple open parties leaves all of them; active matches stay intact."""
    g1 = await create_test_group(db_session, "گروه ۱")
    g2 = await create_test_group(db_session, "گروه ۲")
    user = await create_test_user(db_session, "چندگروهی")
    other = await create_test_user(db_session, "هم‌تیمی")

    p1 = Party(
        group_id=g1.id,
        game_id=1,
        mode="solo",
        max_players=1,
        current_players_count=1,
        status=PartyStatus.FULL,
        creator_user_id=user.id,
    )
    p2 = Party(
        group_id=g2.id,
        game_id=4,
        mode="2v2",
        max_players=2,
        current_players_count=2,
        status=PartyStatus.FULL,
        creator_user_id=other.id,
    )
    db_session.add_all([p1, p2])
    await db_session.flush()
    db_session.add_all(
        [
            PartyMember(party_id=p1.id, user_id=user.id, team=1),
            PartyMember(party_id=p2.id, user_id=other.id, team=1),
            PartyMember(party_id=p2.id, user_id=user.id, team=1),
        ]
    )
    await db_session.commit()

    msg = _mock_message(user, g1, "/leave_party")
    await cmd_leave_party(msg)

    reply_text = msg.reply.await_args.args[0]
    assert PARTY_DISBANDED_TEXT in reply_text
    assert LEFT_PARTY_TEXT in reply_text
    assert await SessionService.has_active_session(db_session, user.id) is False

    # Now verify a party currently in an ACTIVE match is never detached
    live_party = Party(
        group_id=g1.id,
        game_id=1,
        mode="solo",
        max_players=1,
        current_players_count=1,
        status=PartyStatus.IN_MATCH,
        creator_user_id=user.id,
    )
    db_session.add(live_party)
    await db_session.flush()
    db_session.add(PartyMember(party_id=live_party.id, user_id=user.id, team=1))
    live_match = Match(
        game_id=1,
        mode="solo",
        state=MatchState.ACTIVE,
        group1_id=g1.id,
        group2_id=g2.id,
        party1_id=live_party.id,
    )
    db_session.add(live_match)
    await db_session.flush()
    db_session.add(
        MatchPlayer(
            match_id=live_match.id,
            user_id=user.id,
            group_id=g1.id,
            team=1,
            turn_order=1,
            is_active=True,
        )
    )
    await db_session.commit()

    msg_in_match = _mock_message(user, g1, "/leave_party")
    await cmd_leave_party(msg_in_match)
    msg_in_match.reply.assert_awaited_once_with(NOT_IN_PARTY_TEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger_text,as_reply",
    [
        ("/leave_party", False),
        ("ترک پارتی", True),
        ("خروج از پارتی", False),
    ],
)
async def test_leave_party_dispatcher_routing(db_session, trigger_text, as_reply):
    """Full Dispatcher routing for /leave_party, 'ترک پارتی' (reply), and 'خروج از پارتی'."""
    group = await create_test_group(db_session, "گروه روتینگ")
    user = await create_test_user(db_session, "تستر روتینگ")
    other = await create_test_user(db_session, "گوینده قبلی")

    party = await PartyService.create_party(
        db_session, group.id, user.id, "rock_paper_scissors", "solo"
    )
    await db_session.commit()

    prev_parent = party_router._parent_router
    party_router._parent_router = None
    dp = Dispatcher()
    dp.include_router(party_router)
    bot = Bot(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")

    sent_calls = []

    async def fake_make_request(self, bot_obj, method, timeout=None):
        sent_calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=999,
                date=datetime.now(timezone.utc),
                chat=TelegramChat(id=group.id, type="supergroup", title=group.title),
                text=method.text,
            )
        return True

    reply_to = None
    if as_reply:
        reply_to = Message(
            message_id=10,
            date=datetime.now(timezone.utc),
            chat=TelegramChat(id=group.id, type="supergroup", title=group.title),
            from_user=TelegramUser(
                id=other.id, is_bot=False, first_name=other.first_name
            ),
            text="بیاید بازی",
        )

    incoming = Message(
        message_id=11,
        date=datetime.now(timezone.utc),
        chat=TelegramChat(id=group.id, type="supergroup", title=group.title),
        from_user=TelegramUser(
            id=user.id, is_bot=False, first_name=user.first_name
        ),
        text=trigger_text,
        reply_to_message=reply_to,
    )

    try:
        with patch.object(bot.session, "make_request", new=fake_make_request.__get__(bot.session)):
            await dp.feed_update(bot=bot, update=Update(update_id=1, message=incoming))
            # Non-matching text should not trigger leave_party
            unrelated = Message(
                message_id=12,
                date=datetime.now(timezone.utc),
                chat=TelegramChat(id=group.id, type="supergroup", title=group.title),
                from_user=TelegramUser(
                    id=user.id, is_bot=False, first_name=user.first_name
                ),
                text="ترک پارتی الان نه",
            )
            await dp.feed_update(bot=bot, update=Update(update_id=2, message=unrelated))
    finally:
        party_router._parent_router = prev_parent
        await bot.session.close()

    send_messages = [m for m in sent_calls if isinstance(m, SendMessage)]
    assert len(send_messages) == 1
    assert send_messages[0].text == PARTY_DISBANDED_TEXT

    refreshed = await db_session.scalar(
        select(Party)
        .where(Party.id == party.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed.status == PartyStatus.CANCELLED


def test_leave_party_in_public_commands():
    """The /leave_party command is published in the Telegram command menu."""
    names = [name for name, _desc in PUBLIC_COMMANDS]
    assert "leave_party" in names
