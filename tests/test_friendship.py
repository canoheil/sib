"""Tests for the Apple friendship lifecycle and profile integration."""
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select

from sib.database.models import AppleFriendship, FriendshipStatus, User
from sib.handlers.friendship import (
    _ASSET,
    _FRIEND_LIST_SHORTCUT,
    _FRIEND_REQUEST,
    _card_text,
    _resolve_friend_callback,
    handle_friend_list_shortcut,
    handle_friend_request,
    router as friendship_router,
)
from sib.keyboards.friendship import (
    FRIEND_ACCEPT,
    FRIEND_REJECT,
    get_friend_list_keyboard,
    get_friend_request_keyboard,
)
from sib.services.help_content import HELP_SECTIONS
from sib.services.friendship import (
    FriendshipAlreadyExistsError,
    FriendshipPendingError,
    FriendshipService,
    profile_friend_count_line,
)



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


def test_friend_request_card_is_short_markdown_and_escapes_names():
    text = _card_text("آرین_الف", "سارا_ب", 42)

    assert "<b>" not in text and "<a " not in text
    assert "**آرین\\_الف**" in text
    assert "**سارا\\_ب**" in text
    assert "**[سارا\\_ب](tg://user?id=42)**" in text
    assert sum(text.count(mark) for mark in ".!؟") <= 2


async def test_friend_acceptance_uses_markdown_and_plain_alert(db_session, monkeypatch):
    sender = await _make_user(db_session, "آرین_الف")
    target = await _make_user(db_session, "سارا_ب")
    request = await FriendshipService.create_request(db_session, sender.id, target.id)
    await db_session.commit()
    answer = AsyncMock()
    edit = AsyncMock()
    monkeypatch.setattr("sib.handlers.friendship.safe_answer_callback", answer)
    monkeypatch.setattr("sib.handlers.friendship.safe_edit_callback_message", edit)
    callback = SimpleNamespace(
        data=f"{FRIEND_ACCEPT}:{request.id}",
        from_user=target,
        bot=SimpleNamespace(),
    )

    await _resolve_friend_callback(callback, FRIEND_ACCEPT)

    text = edit.await_args.args[2]
    assert text == "🤝 **آرین\\_الف** و **سارا\\_ب** با هم رفیق شدن!"
    assert edit.await_args.kwargs["parse_mode"] == "Markdown"
    assert "**" not in answer.await_args.args[1]
    await db_session.refresh(request)
    assert request.status == FriendshipStatus.ACCEPTED


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


def test_friend_request_image_exists_and_is_correct_size():
    assert _ASSET == Path(__file__).resolve().parents[1] / "assets" / "social" / "friend_request.jpg"
    assert _ASSET.is_file()
    with Image.open(_ASSET) as image:
        assert image.format == "JPEG"
        assert image.size == (1024, 1024)


def test_friendship_alias_triggers_request():
    assert _FRIEND_REQUEST.fullmatch("دوستی سیبی")
    assert _FRIEND_REQUEST.fullmatch("رفاقت سیبی")
    # Both phrases are handled by the same registered message handler.
    assert any(entry.callback is handle_friend_request for entry in friendship_router.message.handlers)


async def test_direct_friend_list_shortcut(db_session):
    owner = await _make_user(db_session, "سیب صاحب فهرست")
    friend = await _make_user(db_session, "سیب دوست")
    request = await FriendshipService.create_request(db_session, owner.id, friend.id)
    await FriendshipService.accept_request(db_session, request.id, friend.id)
    await db_session.commit()

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=owner.id, first_name=owner.first_name),
        answer=AsyncMock(),
    )
    assert _FRIEND_LIST_SHORTCUT.fullmatch("لیست دوستان")
    assert _FRIEND_LIST_SHORTCUT.fullmatch("دوستام")
    await handle_friend_list_shortcut(message)
    message.answer.assert_awaited_once()
    assert "سیب دوست" in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs["reply_markup"] is not None


async def test_direct_friend_list_empty_state(db_session):
    owner = await _make_user(db_session, "سیب تنها")
    await db_session.commit()
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=owner.id, first_name=owner.first_name),
        answer=AsyncMock(),
    )
    await handle_friend_list_shortcut(message)
    assert message.answer.await_args.args[0] == (
        "شما هنوز هیچ دوست سیبی ندارید! روی پیام یک نفر ریپلای کنید و بنویسید «دوستی سیبی»."
    )


def test_help_mentions_new_shortcuts():
    shortcuts = next(section for section in HELP_SECTIONS if section.section_key == "shortcuts")
    assert "رفاقت سیبی" in shortcuts.body_fa
    assert "لیست دوستان" in shortcuts.body_fa


# --- F1.6: glass colors on the friend-request card ------------------------


def test_friend_request_buttons_are_styled():
    """Accept is green, Reject is red — the card is never transparent.

    Same semantic pair the marriage proposal card uses
    (``keyboards/marriage.py``): success = confirm, danger = refuse.
    """
    keyboard = get_friend_request_keyboard(42)
    by_callback = {
        button.callback_data: button
        for row in keyboard.inline_keyboard
        for button in row
    }

    accept = by_callback[f"{FRIEND_ACCEPT}:42"]
    reject = by_callback[f"{FRIEND_REJECT}:42"]

    assert accept.text == "✅ قبول میکنم"
    assert accept.style == "success"
    assert reject.text == "❌ رد میکنم"
    assert reject.style == "danger"
    # One row, two buttons: labels and callback contract unchanged.
    assert [button.text for button in keyboard.inline_keyboard[0]] == [
        "✅ قبول میکنم",
        "❌ رد میکنم",
    ]


def test_friend_list_buttons_are_styled():
    """Pagination is blue (navigation); «back to profile» is red."""
    keyboard = get_friend_list_keyboard(1, 3)
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert [button.style for button in buttons] == ["primary", "primary", "danger"]
    assert [button.callback_data for button in buttons] == [
        "profile:friends:0",
        "profile:friends:2",
        "apple_profile",
    ]


def test_friend_request_keyboard_carries_no_unstyled_button():
    """Every button on both friend surfaces picks one of the three colors."""
    keyboards = [
        get_friend_request_keyboard(7),
        get_friend_list_keyboard(0, 1),
        get_friend_list_keyboard(0, 4),
        get_friend_list_keyboard(3, 4),
    ]
    for keyboard in keyboards:
        for row in keyboard.inline_keyboard:
            for button in row:
                assert button.style in {"primary", "success", "danger"}, (
                    button.text,
                    button.callback_data,
                    button.style,
                )
