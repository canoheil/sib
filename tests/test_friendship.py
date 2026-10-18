"""Tests for the Apple friendship lifecycle and profile integration."""
import uuid

import pytest
from sqlalchemy import select

from sib.database.models import AppleFriendship, FriendshipStatus, User
from sib.services.friendship import (
    FriendshipAlreadyExistsError,
    FriendshipPendingError,
    FriendshipService,
    profile_friend_count_line,
)

pytestmark = pytest.mark.asyncio


async def _make_user(session, label):
    user_id = 8_000_000_000_000_000_000 - uuid.uuid4().int % 1_000_000_000
    user = User(id=user_id, first_name=label, language_code="fa", is_bot=False)
    session.add(user)
    await session.flush()
    return user


async def test_friend_request_sent_and_accepted(db_session):
    sender = await _make_user(db_session, "سیب فرستنده")
    target = await _make_user(db_session, "سیب مخاطب")
    request = await FriendshipService.create_request(db_session, sender.id, target.id)
    assert request.status == FriendshipStatus.PENDING

    accepted = await FriendshipService.accept_request(db_session, request.id, target.id)
    assert accepted.status == FriendshipStatus.ACCEPTED
    friends, count = await FriendshipService.list_friends(db_session, sender.id)
    assert count == 1
    assert friends[0].id == target.id


async def test_friend_request_rejected(db_session):
    sender = await _make_user(db_session, "سیب فرستنده")
    target = await _make_user(db_session, "سیب مخاطب")
    request = await FriendshipService.create_request(db_session, sender.id, target.id)
    await FriendshipService.reject_request(db_session, request.id, target.id)
    assert await db_session.get(AppleFriendship, request.id) is None


async def test_cannot_send_request_to_existing_friend(db_session):
    sender = await _make_user(db_session, "سیب اول")
    target = await _make_user(db_session, "سیب دوم")
    request = await FriendshipService.create_request(db_session, sender.id, target.id)
    await FriendshipService.accept_request(db_session, request.id, target.id)
    with pytest.raises(FriendshipAlreadyExistsError):
        await FriendshipService.create_request(db_session, target.id, sender.id)


async def test_cannot_send_duplicate_pending_request_in_either_direction(db_session):
    sender = await _make_user(db_session, "سیب اول")
    target = await _make_user(db_session, "سیب دوم")
    await FriendshipService.create_request(db_session, sender.id, target.id)
    with pytest.raises(FriendshipPendingError):
        await FriendshipService.create_request(db_session, target.id, sender.id)


async def test_unfriend_instantly_removes_friendship(db_session):
    sender = await _make_user(db_session, "سیب اول")
    target = await _make_user(db_session, "سیب دوم")
    request = await FriendshipService.create_request(db_session, sender.id, target.id)
    await FriendshipService.accept_request(db_session, request.id, target.id)
    assert await FriendshipService.unfriend(db_session, sender.id, target.id)
    assert await db_session.scalar(select(AppleFriendship).where(AppleFriendship.id == request.id)) is None


async def test_profile_shows_friend_count(db_session):
    owner = await _make_user(db_session, "سیب صاحب پروفایل")
    friend = await _make_user(db_session, "سیب دوست")
    request = await FriendshipService.create_request(db_session, owner.id, friend.id)
    await FriendshipService.accept_request(db_session, request.id, friend.id)
    count = await FriendshipService.count_friends(db_session, owner.id)
    assert count == 1
    assert profile_friend_count_line(count) == "🍏 دوستان سیبی: ۱ نفر"


async def test_friend_list_pagination(db_session):
    owner = await _make_user(db_session, "سیب صاحب فهرست")
    targets = [await _make_user(db_session, f"دوست {index:02d}") for index in range(12)]
    for target in targets:
        request = await FriendshipService.create_request(db_session, owner.id, target.id)
        await FriendshipService.accept_request(db_session, request.id, target.id)

    first, total = await FriendshipService.list_friends(db_session, owner.id, offset=0, limit=10)
    second, total_again = await FriendshipService.list_friends(db_session, owner.id, offset=10, limit=10)
    assert total == total_again == 12
    assert len(first) == 10
    assert len(second) == 2
    assert {user.id for user in first}.isdisjoint({user.id for user in second})
