"""Dispatcher and handler regression tests for /cards, /trade, and core commands.

Verifies that:
- ``/cards`` unpacks ``CollectionService.render_collection`` via the shared
  ``_open_sender_cards`` / ``_cards_screen`` flow, sends the interactive
  «کارت‌های من» panel (never a tuple ``ValidationError`` or generic error),
  and claims ``cards`` panel ownership.
- ``/trade`` renders the «مبادله کارت» hub, claims ``trade`` panel ownership
  without raising ``NameError: PanelOwnershipService``, and never triggers the
  global generic error fallback.
- Other core commands (``/profile``, ``/garden``, ``/shop``, ``/battle``)
  route cleanly through the full ``register_all_handlers`` dispatcher for both
  registered and first-time group users without filter conflicts or unhandled
  exceptions.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    MessageEntity,
    Update,
    User as TelegramUser,
)
from pydantic import TypeAdapter

from sib.core.errors import MESSAGES
from sib.database.models import ReleaseStatus
from sib.handlers import register_all_handlers
from sib.handlers.start import handle_apple_cards
from sib.handlers.trading import cmd_trade, handle_trade_new
from sib.handlers.wiki import cmd_cards
from sib.middleware.group_membership import GroupMembershipMiddleware
from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.middleware.user_ban import UserBanMiddleware
from sib.services.characters import CharacterService
from sib.services.panel import LOCKED_MESSAGE, PanelOwnershipService
from sib.services.player_cards import PlayerCardService
from sib.services.trading import TradeService
from sib.tests.test_parties import create_test_user


@contextmanager
def _full_dispatcher_context():
    """Build a ``Dispatcher`` with all production routers and restore router parents after."""
    probe = Dispatcher()
    routers = []
    with patch.object(Dispatcher, "include_router", side_effect=routers.append):
        register_all_handlers(probe)

    saved_parents = [(router, router._parent_router) for router in routers]
    for router, _parent in saved_parents:
        router._parent_router = None

    dp = Dispatcher()
    dp.message.outer_middleware(UserBanMiddleware())
    dp.message.outer_middleware(GroupMembershipMiddleware())
    register_all_handlers(dp)
    try:
        yield dp
    finally:
        dp.sub_routers.clear()
        for router, parent in saved_parents:
            router._parent_router = parent


def _make_mocked_bot():
    """Return ``(bot, sent_calls)`` capturing every outgoing Telegram API call."""
    bot = Bot(token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    sent_calls: list[dict] = []

    async def fake_make_request(bot_instance, method, timeout=None):
        msg_id = 5000 + len(sent_calls) + 1
        chat_id = getattr(method, "chat_id", -100777)
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        reply_markup = getattr(method, "reply_markup", None)
        sent_calls.append(
            {
                "method": type(method).__name__,
                "message_id": msg_id,
                "chat_id": chat_id,
                "text": text or "",
                "reply_markup": reply_markup,
            }
        )
        chat_type = "private" if isinstance(chat_id, int) and chat_id > 0 else "supergroup"
        raw = {
            "message_id": msg_id,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {
                "id": chat_id,
                "type": chat_type,
                **({"title": "گروه تست"} if chat_type != "private" else {}),
            },
            "from": {"id": 123456, "is_bot": True, "first_name": "SibBot"},
            "text": getattr(method, "text", None) or "ok",
        }
        return TypeAdapter(method.__returning__).validate_python(raw)

    bot.session.make_request = fake_make_request
    return bot, sent_calls


def _command_update(
    command_text: str,
    *,
    user_id: int,
    first_name: str = "بازیکن",
    chat_id: int = -100777,
    chat_type: str = "supergroup",
    update_id: int = 1,
) -> Update:
    cmd_token = command_text.split()[0]
    chat_kwargs = {"id": chat_id, "type": chat_type}
    if chat_type in ("group", "supergroup"):
        chat_kwargs["title"] = "گروه تست"
    return Update(
        update_id=update_id,
        message=Message(
            message_id=100 + update_id,
            date=datetime.now(timezone.utc),
            chat=TelegramChat(**chat_kwargs),
            from_user=TelegramUser(id=user_id, is_bot=False, first_name=first_name),
            text=command_text,
            entities=[MessageEntity(type="bot_command", offset=0, length=len(cmd_token))],
        ),
    )


def _callback_buttons(reply_markup) -> list[str]:
    if reply_markup is None or not getattr(reply_markup, "inline_keyboard", None):
        return []
    return [
        btn.callback_data
        for row in reply_markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]


async def _grant_released_card(db_session, user_id: int, code: str):
    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"شخصیت {code}",
        power=22,
        speed=20,
        intelligence=18,
        passive_key="test_passive",
    )
    await CharacterService.set_release_status(
        db_session, character.id, ReleaseStatus.RELEASED
    )
    await db_session.commit()
    card = await PlayerCardService.acquire(db_session, user_id, character.id)
    await db_session.commit()
    return card


# ---------------------------------------------------------------------------
# /cards dispatcher & handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_cards_command_empty_and_owned_cards(db_session):
    """Simulates a user sending /cards through the full dispatcher."""
    user = await create_test_user(db_session, "کارت‌باز")
    await db_session.commit()

    bot, sent_calls = _make_mocked_bot()
    try:
        with _full_dispatcher_context() as dp:
            # 1) Empty collection in a group chat
            await dp.feed_update(
                bot,
                _command_update(
                    "/cards",
                    user_id=user.id,
                    first_name=user.first_name,
                    chat_id=-100801,
                    chat_type="supergroup",
                    update_id=1,
                ),
            )
            assert len(sent_calls) == 1
            empty_reply = sent_calls[0]
            assert MESSAGES["generic_error"] not in empty_reply["text"]
            assert "کارت‌های من" in empty_reply["text"]
            assert "هنوز کارتی نداری" in empty_reply["text"]
            empty_cbs = _callback_buttons(empty_reply["reply_markup"])
            assert "apple_wiki:apple_cards" in empty_cbs
            assert "main_menu" in empty_cbs

            await db_session.rollback()
            owner_id, panel_type = await PanelOwnershipService.get_panel_context(
                db_session, -100801, empty_reply["message_id"], user.id
            )
            assert owner_id == user.id and panel_type == "cards"

            # 2) Player acquires cards + sets a Main Card, then sends /cards in private chat
            card_a = await _grant_released_card(db_session, user.id, "cmd_cards_a")
            card_b = await _grant_released_card(db_session, user.id, "cmd_cards_b")
            await PlayerCardService.set_favorite(db_session, user.id, card_a.id)
            await db_session.commit()

            sent_calls.clear()
            await dp.feed_update(
                bot,
                _command_update(
                    "/cards",
                    user_id=user.id,
                    first_name=user.first_name,
                    chat_id=user.id,
                    chat_type="private",
                    update_id=2,
                ),
            )
            assert len(sent_calls) == 1
            owned_reply = sent_calls[0]
            assert MESSAGES["generic_error"] not in owned_reply["text"]
            assert "کارت‌های من" in owned_reply["text"]
            assert "شخصیت cmd\\_cards\\_a" in owned_reply["text"] or "cmd_cards_a" in owned_reply["text"]
            owned_cbs = _callback_buttons(owned_reply["reply_markup"])
            assert f"card_panel:{card_a.id}" in owned_cbs
            assert f"card_panel:{card_b.id}" in owned_cbs
            assert "main_menu" in owned_cbs
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_cmd_cards_matches_profile_cards_screen(db_session):
    """/cards renders the exact same screen and buttons as the apple_cards callback."""
    user = await create_test_user(db_session, "همگام")
    card = await _grant_released_card(db_session, user.id, "cmd_cards_parity")

    msg = MagicMock(spec=Message)
    msg.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
    msg.chat = TelegramChat(id=-100802, type="supergroup", title="تست")
    sent = MagicMock(message_id=777, chat=msg.chat, photo=None)
    msg.reply = AsyncMock(return_value=sent)
    msg.reply_photo = AsyncMock(return_value=sent)

    await cmd_cards(msg, MagicMock())
    assert msg.reply.await_count == 1
    cmd_text = msg.reply.await_args.args[0]
    cmd_kb = msg.reply.await_args.kwargs["reply_markup"]

    cb = MagicMock(spec=CallbackQuery)
    cb.data = "apple_cards"
    cb.from_user = msg.from_user
    cb.message = MagicMock(spec=Message)
    cb.message.chat = msg.chat
    cb.message.message_id = 778
    cb.message.photo = None
    cb.message.text = "prev"
    cb.message.edit_text = AsyncMock(return_value=True)
    cb.answer = AsyncMock()
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    await handle_apple_cards(cb, bot)
    edit_call = cb.message.edit_text.call_args or bot.edit_message_text.call_args
    cb_text = edit_call.args[0] if edit_call.args else edit_call.kwargs["text"]
    cb_kb = edit_call.kwargs["reply_markup"]

    assert cmd_text == cb_text
    assert _callback_buttons(cmd_kb) == _callback_buttons(cb_kb)
    assert f"card_panel:{card.id}" in _callback_buttons(cmd_kb)


# ---------------------------------------------------------------------------
# /trade dispatcher & handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_trade_command_with_cards_and_pending_trade(db_session):
    """Simulates a user sending /trade through the full dispatcher."""
    proposer = await create_test_user(db_session, "معامله‌گر اول")
    partner = await create_test_user(db_session, "معامله‌گر دوم")
    await db_session.commit()

    p_card_1 = await _grant_released_card(db_session, proposer.id, "cmd_trade_p1")
    p_card_2 = await _grant_released_card(db_session, proposer.id, "cmd_trade_p2")
    r_card_1 = await _grant_released_card(db_session, partner.id, "cmd_trade_r1")
    trade = await TradeService.create(
        db_session,
        proposer_id=proposer.id,
        recipient_id=partner.id,
        proposer_card_id=p_card_2.id,
        recipient_card_id=r_card_1.id,
    )
    await db_session.commit()

    bot, sent_calls = _make_mocked_bot()
    try:
        with _full_dispatcher_context() as dp:
            await dp.feed_update(
                bot,
                _command_update(
                    "/trade",
                    user_id=proposer.id,
                    first_name=proposer.first_name,
                    chat_id=-100803,
                    chat_type="supergroup",
                    update_id=10,
                ),
            )

            # Must send ONLY the trade hub (never a second generic_error message)
            assert len(sent_calls) == 1
            reply = sent_calls[0]
            assert MESSAGES["generic_error"] not in reply["text"]
            assert "مبادله کارت" in reply["text"]
            assert f"#{trade.id}" in reply["text"]

            cbs = _callback_buttons(reply["reply_markup"])
            assert "trade_new" in cbs
            assert f"trade_ok:{trade.id}" in cbs
            assert f"trade_cancel:{trade.id}" in cbs
            assert "trade_refresh" in cbs
            assert "main_menu" in cbs

            await db_session.rollback()
            owner_id, panel_type = await PanelOwnershipService.get_panel_context(
                db_session, -100803, reply["message_id"], proposer.id
            )
            assert owner_id == proposer.id and panel_type == "trade"
            assert p_card_1.id is not None
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_cmd_trade_claims_panel_and_locks_out_other_users(db_session):
    """/trade claims panel ownership so non-owners are blocked by PanelGuardMiddleware."""
    owner = await create_test_user(db_session, "صاحب پنل")
    intruder = await create_test_user(db_session, "مزاحم")
    await _grant_released_card(db_session, owner.id, "cmd_trade_lock")

    msg = MagicMock(spec=Message)
    msg.from_user = TelegramUser(id=owner.id, is_bot=False, first_name=owner.first_name)
    msg.chat = TelegramChat(id=-100804, type="supergroup", title="گروه قفل")
    sent = MagicMock(message_id=888, chat=msg.chat)
    msg.reply = AsyncMock(return_value=sent)

    await cmd_trade(msg, MagicMock())

    guard = PanelGuardMiddleware()
    downstream = AsyncMock(return_value="handled")

    intruder_cb = MagicMock(spec=CallbackQuery)
    intruder_cb.data = "trade_new"
    intruder_cb.from_user = TelegramUser(
        id=intruder.id, is_bot=False, first_name=intruder.first_name
    )
    intruder_cb.message = MagicMock(spec=Message)
    intruder_cb.message.chat = msg.chat
    intruder_cb.message.message_id = 888
    intruder_cb.answer = AsyncMock()

    result = await guard(downstream, intruder_cb, {})
    assert result is None
    downstream.assert_not_awaited()
    intruder_cb.answer.assert_awaited_once_with(LOCKED_MESSAGE, show_alert=True)

    owner_cb = MagicMock(spec=CallbackQuery)
    owner_cb.data = "trade_new"
    owner_cb.from_user = msg.from_user
    owner_cb.message = intruder_cb.message
    owner_cb.message.edit_text = AsyncMock(return_value=True)
    owner_cb.answer = AsyncMock()

    await guard(lambda ev, data: handle_trade_new(ev, MagicMock()), owner_cb, {})
    owner_cb.answer.assert_awaited()


# ---------------------------------------------------------------------------
# Sanity check: /profile, /garden, /shop, /battle (registered & unregistered)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_snippet", "expected_panel_type"),
    [
        ("/cards", "کارت‌های من", "cards"),
        ("/trade", "مبادله کارت", "trade"),
        ("/profile", "پروفایل", "profile"),
        ("/garden", "باغ سیب", "garden"),
        ("/shop", "فروشگاه سیب", "shop"),
        ("/battle", "نبرد سیبی", "apple_battle"),
    ],
)
async def test_core_commands_sanity_registered_and_unregistered(
    db_session, command: str, expected_snippet: str, expected_panel_type: str
):
    """All six core commands succeed without generic_error for both new and existing users."""
    registered = await create_test_user(db_session, "ثبت‌نام‌شده")
    await db_session.commit()
    unregistered_id = registered.id + 500_000

    bot, sent_calls = _make_mocked_bot()
    try:
        with _full_dispatcher_context() as dp:
            for idx, (uid, name) in enumerate(
                [(registered.id, registered.first_name), (unregistered_id, "تازه‌وارد")],
                start=1,
            ):
                sent_calls.clear()
                chat_id = -100900 - idx
                await dp.feed_update(
                    bot,
                    _command_update(
                        command,
                        user_id=uid,
                        first_name=name,
                        chat_id=chat_id,
                        chat_type="supergroup",
                        update_id=50 + idx,
                    ),
                )
                assert len(sent_calls) == 1, f"{command} sent {len(sent_calls)} messages: {sent_calls}"
                reply = sent_calls[0]
                assert MESSAGES["generic_error"] not in reply["text"]
                assert expected_snippet in reply["text"]
                assert reply["reply_markup"] is not None

                await db_session.rollback()
                owner_id, panel_type = await PanelOwnershipService.get_panel_context(
                    db_session, chat_id, reply["message_id"], uid
                )
                assert owner_id == uid
                assert panel_type == expected_panel_type
    finally:
        await bot.session.close()
