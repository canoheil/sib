"""Pet System phase 3: the «سیب پت» menu, feeding and evolution.

The service owns the rules (:mod:`sib.services.pet_service.feed_pet`) and the
handler only renders them, so both layers are exercised here against an
isolated SQLite database, plus the shop integration for ``pet_food``.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from aiogram.types import Chat, Message
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    AppleTransaction,
    AppleWallet,
    SystemConfig,
    User,
    UserItem,
    UserPet,
)
from sib.handlers import pets
from sib.handlers.pets import PetStates
from sib.services import pet_service
from sib.services.config_store import CONFIG_DEFAULTS
from sib.services.sib_claim import fa_digits
from sib.services.items import ITEM_CATALOG, ItemService

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
#: Telegram's answer to an edit of a message that no longer exists (fail-fast:
#: the retry helper does not retry it, so the test stays fast).
_GONE = TelegramBadRequest(method=None, message="message to edit not found")
FEED_COOLDOWN_KEY = "pet_feed_cooldown_hours"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def _clean_pending_hunts():
    pet_service._PENDING_HUNT_ATTEMPTS.clear()
    yield
    pet_service._PENDING_HUNT_ATTEMPTS.clear()


@pytest_asyncio.fixture
async def engine():
    """In-memory SQLite with only the tables this feature touches.

    SQLite autoincrements plain INTEGER primary keys only, so ``BigInteger``
    primary keys are narrowed for the DDL and restored right after (the same
    shim as ``tests/conftest.py``).
    """
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        User.__table__,
        SystemConfig.__table__,
        UserPet.__table__,
        UserItem.__table__,
        AppleWallet.__table__,
        AppleTransaction.__table__,
    ]
    narrowed = []
    for table in tables:
        for column in table.columns:
            if column.primary_key and type(column.type).__name__ == "BigInteger":
                narrowed.append((column, column.type))
                column.type = sa.Integer()
    try:
        async with eng.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: User.metadata.create_all(
                    sync_connection, tables=tables
                )
            )
    finally:
        for column, original in narrowed:
            column.type = original
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    """An open session whose database already knows users 1 and 2."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory.begin() as seed:
        seed.add(User(id=1, first_name="pet owner"))
        seed.add(User(id=2, first_name="someone else"))
    async with factory() as db:
        yield db


@pytest_asyncio.fixture
async def db(engine, monkeypatch):
    """Session factory shared with the handler through a patched ``get_session``."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def get_session():
        async with factory.begin() as db_session:
            yield db_session

    monkeypatch.setattr(pets, "get_session", get_session)
    async with factory.begin() as db_session:
        db_session.add(User(id=1, first_name="pet owner"))
        db_session.add(User(id=2, first_name="someone else"))
    yield factory


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


async def _seed_pet(
    session,
    *,
    user_id: int = 1,
    pet_type: str = "cat",
    is_adult: bool = False,
    custom_name: str | None = "پشمک",
    xp: int = 0,
    last_fed_at: datetime | None = None,
    skill_level: int = 1,
) -> UserPet:
    pet = UserPet(
        user_id=user_id,
        pet_type=pet_type,
        is_adult=is_adult,
        custom_name=custom_name,
        xp=xp,
        skill_level=skill_level,
        last_fed_at=last_fed_at,
    )
    session.add(pet)
    await session.flush()
    return pet


async def _grant_food(session, user_id: int = 1, quantity: int = 1) -> None:
    await ItemService.grant(session, user_id, pet_service.PET_FOOD_ITEM, quantity)


async def _food_count(session, user_id: int = 1) -> int:
    return await ItemService.get_total(session, user_id, pet_service.PET_FOOD_ITEM)


async def _config(session, key: str, value) -> None:
    session.add(SystemConfig(key=key, value=str(value)))
    await session.flush()


# ---------------------------------------------------------------------------
# Item and shop integration
# ---------------------------------------------------------------------------

def test_pet_food_is_in_the_item_catalog():
    entry = ITEM_CATALOG["pet_food"]
    assert entry["emoji"] == "🍖"
    assert entry["label"] == "غذای پت"
    assert entry["description"]


def test_pet_food_price_comes_from_its_own_config():
    assert CONFIG_DEFAULTS["pet_food_price"] == 100


async def test_pet_food_is_buyable_in_the_shop(session):
    session.add(AppleWallet(user_id=1, balance=250))
    await session.flush()

    assert await ItemService.buy_price(session, "pet_food") == 100
    assert "pet_food" in await ItemService.buy_prices(session)

    total = await ItemService.buy(session, 1, "pet_food", 2)

    assert total == 2
    assert await _food_count(session) == 2
    wallet = await session.get(AppleWallet, 1)
    await session.refresh(wallet)
    assert wallet.balance == 50  # 250 - 2 x 100

    ledger = list((await session.scalars(sa.select(AppleTransaction))).all())
    assert [(tx.amount, tx.reason) for tx in ledger] == [(-200, "item_purchase")]


async def test_pet_food_purchase_is_refused_without_apples(session):
    session.add(AppleWallet(user_id=1, balance=40))
    await session.flush()

    with pytest.raises(ValueError):
        await ItemService.buy(session, 1, "pet_food", 1)

    assert await _food_count(session) == 0
    wallet = await session.get(AppleWallet, 1)
    await session.refresh(wallet)
    assert wallet.balance == 40


async def test_pet_food_price_is_operator_configurable(session):
    await _config(session, "pet_food_price", 25)
    assert await ItemService.buy_price(session, "pet_food") == 25


async def test_pet_food_is_not_sellable():
    """A consumable: buy-only, like Magic Powder."""
    assert CONFIG_DEFAULTS["item_sell_prices"].get("pet_food") is None


# ---------------------------------------------------------------------------
# feed_pet
# ---------------------------------------------------------------------------

async def test_feed_pet_without_food(session):
    pet = await _seed_pet(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "no_food"
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.xp == 0
    assert stored.last_fed_at is None


async def test_feed_pet_too_soon(session):
    pet = await _seed_pet(session, last_fed_at=NOW - timedelta(minutes=20))
    await _grant_food(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "too_soon"
    assert result["remaining_seconds"] == 40 * 60
    # Nothing was spent.
    assert await _food_count(session) == 1
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.xp == 0


async def test_feed_pet_adds_xp_and_consumes_one_food(session):
    pet = await _seed_pet(session, xp=100)
    await _grant_food(session, quantity=3)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "fed"
    assert result["xp"] == 150  # 100 + 50
    assert result["required"] == 500
    assert result["xp_added"] == 50
    assert result["remaining_food"] == 2

    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.xp == 150
    assert stored.is_adult is False
    assert stored.last_fed_at.replace(tzinfo=timezone.utc) == NOW
    assert await _food_count(session) == 2  # exactly one was eaten


async def test_feed_pet_evolves_at_the_threshold(session):
    pet = await _seed_pet(session, xp=460)  # 460 + 50 >= 500
    await _grant_food(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "evolved"
    assert result["xp"] == 510
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.is_adult is True
    assert stored.xp == 510
    assert await _food_count(session) == 0


async def test_feed_pet_does_not_evolve_one_xp_below(session):
    pet = await _seed_pet(session, xp=401)  # 401 + 50 = 451 < 500
    await _grant_food(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "fed"
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.is_adult is False


async def test_feed_pet_satiates_an_adult_without_adding_xp(session):
    """A grown pet still eats: the meal resets the hunger clock, not the XP."""
    pet = await _seed_pet(
        session, is_adult=True, xp=500, last_fed_at=NOW - timedelta(hours=30)
    )
    await _grant_food(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "satiated"
    assert result["xp_added"] == 0
    assert result["xp"] == 500  # never climbs past the adult threshold
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.xp == 500
    assert stored.is_adult is True
    assert stored.last_fed_at.replace(tzinfo=timezone.utc) == NOW
    assert pet_service.get_satiety_status(stored.last_fed_at, now=NOW) == "سیر"
    assert await _food_count(session) == 0  # the meal was consumed


async def test_feed_pet_refuses_another_players_pet(session):
    pet = await _seed_pet(session, user_id=2)
    await _grant_food(session, user_id=1)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "not_found"
    stored = await session.get(UserPet, pet.id)
    await session.refresh(stored)
    assert stored.xp == 0
    assert await _food_count(session, 1) == 1


@pytest.mark.parametrize("bad_id", [9999, None, "abc"])
async def test_feed_pet_refuses_a_bad_pet_id(session, bad_id):
    await _grant_food(session)
    assert (await pet_service.feed_pet(session, 1, bad_id, now=NOW))["status"] == "not_found"


async def test_feed_cooldown_is_one_hour_by_default(session):
    pet = await _seed_pet(session, last_fed_at=NOW - timedelta(minutes=59))
    await _grant_food(session)

    assert (await pet_service.feed_pet(session, 1, pet.id, now=NOW))["status"] == "too_soon"

    pet.last_fed_at = NOW - timedelta(hours=1)
    await session.flush()
    assert (await pet_service.feed_pet(session, 1, pet.id, now=NOW))["status"] == "fed"


async def test_feed_cooldown_is_configurable(session):
    await _config(session, FEED_COOLDOWN_KEY, 3)
    pet = await _seed_pet(session, last_fed_at=NOW - timedelta(hours=2))
    await _grant_food(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)
    assert result["status"] == "too_soon"
    assert result["remaining_seconds"] == 3600


async def test_feed_cooldown_zero_disables_the_spam_guard(session):
    await _config(session, FEED_COOLDOWN_KEY, 0)
    pet = await _seed_pet(session, last_fed_at=NOW - timedelta(seconds=5))
    await _grant_food(session)

    assert (await pet_service.feed_pet(session, 1, pet.id, now=NOW))["status"] == "fed"


async def test_feed_xp_amount_is_configurable(session):
    await _config(session, "pet_feed_xp_amount", 120)
    pet = await _seed_pet(session)
    await _grant_food(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["xp"] == 120
    assert result["xp_added"] == 120


async def test_adult_threshold_is_configurable(session):
    await _config(session, "pet_adult_xp_required", 50)  # default is 500
    pet = await _seed_pet(session)
    await _grant_food(session)

    result = await pet_service.feed_pet(session, 1, pet.id, now=NOW)

    assert result["status"] == "evolved"
    assert result["required"] == 50


async def test_feed_pet_only_spends_the_callers_food(session):
    pet = await _seed_pet(session, pet_type="owl")
    await _grant_food(session, user_id=2, quantity=5)

    assert (await pet_service.feed_pet(session, 1, pet.id, now=NOW))["status"] == "no_food"
    # The other player's inventory keeps its food and does not feed this pet.
    pet.last_fed_at = None
    await _grant_food(session, user_id=1, quantity=1)
    assert (await pet_service.feed_pet(session, 1, pet.id, now=NOW))["status"] == "fed"
    assert await _food_count(session, 2) == 5


# ---------------------------------------------------------------------------
# Reading helpers
# ---------------------------------------------------------------------------

async def test_list_pets_is_scoped_per_user(session):
    await _seed_pet(session, user_id=1, pet_type="cat", custom_name="ملوس")
    await _seed_pet(session, user_id=1, pet_type="owl", custom_name="پشمک")
    await _seed_pet(session, user_id=2, pet_type="dog")

    mine = await pet_service.list_pets(session, 1)
    assert [pet.pet_type for pet in mine] == ["cat", "owl"]
    assert len(await pet_service.list_pets(session, 2)) == 1


def test_pet_display_name_falls_back_for_an_unnamed_pet():
    assert pet_service.pet_display_name(SimpleNamespace(custom_name="ملوس")) == "ملوس"
    assert (
        pet_service.pet_display_name(SimpleNamespace(custom_name=None))
        == pet_service.UNNAMED_PET_FA
    )


def test_xp_progress_is_capped():
    pet = SimpleNamespace(xp=150)
    assert pet_service.xp_progress(pet, 500) == {"xp": 150, "required": 500, "percent": 30}
    assert pet_service.xp_progress(SimpleNamespace(xp=900), 500)["percent"] == 100
    assert pet_service.xp_progress(SimpleNamespace(xp=0), 0)["required"] == 1


# ---------------------------------------------------------------------------
# Handlers — the menu
# ---------------------------------------------------------------------------

async def test_my_pets_command_is_registered_with_aliases():
    registered = [
        handler
        for handler in pets.router.message.handlers
        if handler.callback is pets.handle_my_pets
    ]
    assert registered

    for text in ("/mypets", "سیب پت", "پت من", "پت", "سیب پت"):
        assert text == "/mypets" or text in pets.PET_MENU_ALIASES
        event = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=1, type="private"),
            from_user=TelegramUser(id=1, first_name="player", is_bot=False),
            text=text,
        )
        matched = False
        for entry in registered:
            if all([await flt.call(event, bot=None) for flt in entry.filters]):
                matched = True
                break
        assert matched, f"no filter matched {text!r}"


def test_pets_router_is_registered_in_the_dispatcher():
    from sib.handlers import register_all_handlers

    included = []
    with patch.object(Dispatcher, "include_router", side_effect=included.append):
        register_all_handlers(Dispatcher())
    assert pets.router in included


def _menu_message(user_id: int = 1):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
        reply=AsyncMock(),
    )


async def test_menu_without_pets_says_to_hunt(db, state):
    message = _menu_message()
    await pets.handle_my_pets(message, state)

    message.answer.assert_awaited_once()
    message.answer_photo.assert_not_awaited()
    assert "سیب شکار" in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs.get("reply_markup") is None


async def test_menu_lists_every_pet_as_a_text_only_message(db, state):
    """«سیب پت» sends ONE TEXT-ONLY message (``answer``, never ``answer_photo``)
    carrying the pet list as buttons — the hunt forest plate stays with hunting."""
    async with db.begin() as session:
        session.add(UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=10))
        session.add(UserPet(user_id=1, pet_type="cat", custom_name=None, xp=0))

    message = _menu_message()
    await pets.handle_my_pets(message, state)

    # ONE text message: no photo is attached to the menu.
    message.answer.assert_awaited_once()
    message.answer_photo.assert_not_awaited()
    sent = message.answer.await_args
    assert "پت‌های شما" in sent.args[0]
    assert sent.kwargs.get("parse_mode") == "HTML"
    keyboard = sent.kwargs["reply_markup"]
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert len(buttons) == 2
    assert buttons[0].text == "🦉 جغد (پشمک)"
    assert buttons[1].text == "🐱 گربه (بی‌نام)"
    assert [b.callback_data for b in buttons] == ["pet_view:1", "pet_view:2"]


async def test_menu_needs_no_photo_sender_at_all(db, state):
    """The menu is text-only even on a handle without ``answer_photo``."""
    async with db.begin() as session:
        session.add(UserPet(user_id=1, pet_type="owl", custom_name="پشمک"))

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        answer=AsyncMock(),
        reply=AsyncMock(),
    )
    await pets.handle_my_pets(message, state)

    message.answer.assert_awaited_once()
    assert "پت‌های شما" in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs["reply_markup"] is not None


async def test_menu_clears_a_half_finished_naming_state(db, state):
    await state.set_state(PetStates.waiting_for_name)
    message = _menu_message()
    await pets.handle_my_pets(message, state)
    assert await state.get_state() is None


# ---------------------------------------------------------------------------
# Handlers — the pet profile
# ---------------------------------------------------------------------------

def _callback(data: str, *, clicker_id: int = 1, message_id: int = 5, with_photo=True):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=clicker_id, is_bot=False),
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=1),
            photo=[SimpleNamespace(file_id="f")] if with_photo else None,
            answer=AsyncMock(),
            answer_photo=AsyncMock(),
        ),
    )


def _bot():
    return SimpleNamespace(
        edit_message_media=AsyncMock(),
        edit_message_caption=AsyncMock(),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(return_value=True),
        send_message=AsyncMock(),
    )


async def test_pet_view_shows_the_baby_profile(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=150)
        session.add(pet)
        await session.flush()
        pet_id = pet.id

    callback = _callback(f"pet_view:{pet_id}")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "owl_baby.jpg")
    caption = edited["media"].caption
    assert "پشمک" in caption
    assert "جغد" in caption
    assert f"{fa_digits(150)}/{fa_digits(500)}" in caption
    assert "بچه" in caption
    # The feed button points at this pet.
    feed_row = edited["reply_markup"].inline_keyboard[0][0]
    assert feed_row.text == pets.FEED_BUTTON_TEXT
    assert feed_row.callback_data == f"pet_feed:{pet_id}"


async def test_pet_view_shows_the_adult_art_and_keeps_the_feed_button(db, state):
    """Adults keep «🍖 غذا دادن» — their meal resets the hunger clock — and
    gain «💎 ارتقای اسکیل» while below the skill cap."""
    async with db.begin() as session:
        pet = UserPet(
            user_id=1, pet_type="cat", custom_name="ملوس", xp=520, is_adult=True
        )
        session.add(pet)
        await session.flush()
        pet_id = pet.id

    callback = _callback(f"pet_view:{pet_id}")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    edited = bot.edit_message_media.await_args.kwargs
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "cat_adult.jpg")
    assert "بالغ" in edited["media"].caption
    buttons = [b for row in edited["reply_markup"].inline_keyboard for b in row]
    by_callback = {b.callback_data: b for b in buttons}
    assert f"pet_feed:{pet_id}" in by_callback
    assert by_callback[f"pet_skill_up:{pet_id}"].text == pets.SKILL_UP_BUTTON_TEXT


async def test_pet_view_refuses_another_players_pet(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=2, pet_type="dog", custom_name="rex")
        session.add(pet)
        await session.flush()
        pet_id = pet.id

    callback = _callback(f"pet_view:{pet_id}", clicker_id=1)
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.parametrize("raw", ["9999", "abc"])
async def test_pet_view_handles_unknown_pets(db, state, raw):
    callback = _callback(f"pet_view:{raw}")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_pet_view_menu_back_from_a_photo_card_deletes_and_resends_text(
    db, state
):
    """«🔙 پت‌های من» on a PHOTO card: Telegram cannot edit a photo into a
    text message, so the card is DELETED and the text-only menu is sent as a
    fresh message."""
    async with db.begin() as session:
        session.add(UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=10))

    callback = _callback("pet_view:menu")  # with_photo=True (a pet card)
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    # No edit is attempted on the photo message: it is deleted and replaced.
    bot.edit_message_media.assert_not_awaited()
    bot.edit_message_caption.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    bot.delete_message.assert_awaited_once()
    assert bot.delete_message.await_args.args == (1, 5)
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args
    assert sent.args[0] == 1  # chat_id
    assert "پت‌های شما" in sent.args[1]  # the text-only menu
    buttons = [b for row in sent.kwargs["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["pet_view:1"]
    # The old callback message was not answered with another send.
    callback.message.answer.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_pet_view_menu_back_on_a_text_menu_edits_in_place(db, state):
    """«🔙 پت‌های من» on an already-TEXT message (the menu itself) is a plain
    ``edit_message_text`` — still no new message."""
    async with db.begin() as session:
        session.add(UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=10))

    callback = _callback("pet_view:menu", with_photo=False)
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    bot.edit_message_text.assert_awaited_once()
    edited = bot.edit_message_text.await_args
    assert edited.args[0]  # text is passed positionally
    assert "پت‌های شما" in edited.args[0]
    assert edited.kwargs["message_id"] == 5
    buttons = [b for row in edited.kwargs["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["pet_view:1"]
    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_pet_view_menu_back_without_pets_on_a_photo_card_resends_text(
    db, state
):
    """Without pets the list is plain text: from a photo card it is deleted
    and re-sent as text with the hunt hint and no keyboard."""
    callback = _callback("pet_view:menu")  # photo card, but the owner has no pets
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    bot.delete_message.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args
    assert "سیب شکار" in sent.args[1]
    assert sent.kwargs["reply_markup"] is None
    callback.message.answer.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_pet_view_menu_back_without_pets_on_text_edits_the_text(db, state):
    """Without pets and on a TEXT message the hint is edited in place."""
    callback = _callback("pet_view:menu", with_photo=False)
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    bot.edit_message_text.assert_awaited_once()
    assert "سیب شکار" in bot.edit_message_text.await_args.args[0]
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None
    bot.delete_message.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


# ---------------------------------------------------------------------------
# Handlers — the skill line of the profile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pet_type, skill_name, description",
    [
        ("dog", "پارس هشدار", "محافظت در برابر دزدی"),
        ("squirrel", "برکت باغ", "سرعت بخشیدن به رشد باغ"),
        ("cat", "چشم شانس", "شانس بیشتر در کازینو و راکت"),
        ("owl", "خشم شب", "دمیج بیشتر در نبردها"),
    ],
)
def test_baby_profile_shows_its_locked_skill(pet_type, skill_name, description):
    caption = pets.pet_profile_caption(
        SimpleNamespace(
            pet_type=pet_type, custom_name="پشمک", xp=0, is_adult=False, skill_level=1
        ),
        500,
    )

    assert f"🔒 اسکیل: {skill_name} (با بالغ شدن فعال می‌شود)" in caption
    # A baby has no usable skill yet: no level, no description.
    assert description not in caption
    assert "لول" not in caption


@pytest.mark.parametrize(
    "pet_type, skill_name, description",
    [
        ("dog", "پارس هشدار", "محافظت در برابر دزدی"),
        ("squirrel", "برکت باغ", "سرعت بخشیدن به رشد باغ"),
        ("cat", "چشم شانس", "شانس بیشتر در کازینو و راکت"),
        ("owl", "خشم شب", "دمیج بیشتر در نبردها"),
    ],
)
def test_adult_profile_explains_its_active_skill(pet_type, skill_name, description):
    caption = pets.pet_profile_caption(
        SimpleNamespace(
            pet_type=pet_type, custom_name="پشمک", xp=500, is_adult=True, skill_level=2
        ),
        500,
    )

    assert f"✨ اسکیل: {skill_name} - لول {fa_digits(2)}" in caption
    assert description in caption
    assert "🔒" not in caption


def test_profile_skill_line_uses_the_pet_level():
    adult = SimpleNamespace(pet_type="owl", is_adult=True, skill_level=3)
    assert "لول ۳" in pets.pet_skill_line(adult)


def test_unknown_species_still_gets_a_skill_line():
    pet = SimpleNamespace(pet_type="dragon", is_adult=True, skill_level=1)
    assert "✨ اسکیل" in pets.pet_skill_line(pet)


async def test_pet_view_shows_the_locked_skill_of_a_baby(db, state):
    async with db.begin() as session:
        session.add(UserPet(user_id=1, pet_type="dog", custom_name="رکس", xp=0))

    callback = _callback("pet_view:1")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    caption = bot.edit_message_media.await_args.kwargs["media"].caption
    assert "🔒 اسکیل: پارس هشدار" in caption
    assert "محافظت در برابر دزدی" not in caption


async def test_pet_view_shows_the_active_skill_of_an_adult(db, state):
    async with db.begin() as session:
        session.add(
            UserPet(
                user_id=1,
                pet_type="cat",
                custom_name="ملوس",
                xp=520,
                is_adult=True,
                skill_level=2,
            )
        )

    callback = _callback("pet_view:1")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    caption = bot.edit_message_media.await_args.kwargs["media"].caption
    assert "✨ اسکیل: چشم شانس - لول ۲" in caption
    assert "شانس بیشتر در کازینو و راکت" in caption


async def test_pet_view_never_sends_a_new_message(db, state):
    """Opening a card EDITS the menu message (no chat spam)."""
    async with db.begin() as session:
        session.add(UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=10))

    callback = _callback("pet_view:1")
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    bot.edit_message_media.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_pet_view_turns_the_text_menu_into_a_photo_card(db, state):
    """Opening a pet from the TEXT-ONLY menu is an ``edit_message_media``
    (text -> photo): the pet's artwork replaces the menu, no new message."""
    async with db.begin() as session:
        session.add(UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=10))

    callback = _callback("pet_view:1", with_photo=False)  # the text menu message
    bot = _bot()
    await pets.handle_pet_view(callback, state, bot)

    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    assert edited["message_id"] == 5
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "owl_baby.jpg")
    assert "پشمک" in edited["media"].caption
    bot.edit_message_text.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
    callback.message.answer_photo.assert_not_awaited()


async def test_pet_view_reposts_only_when_the_message_is_gone(db, state):
    """Every edit failing (deleted message) is the only case that re-sends."""
    async with db.begin() as session:
        session.add(UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=10))

    callback = _callback("pet_view:1")
    bot = _bot()
    bot.edit_message_media.side_effect = _GONE
    bot.edit_message_caption.side_effect = _GONE
    bot.edit_message_text.side_effect = _GONE
    await pets.handle_pet_view(callback, state, bot)

    callback.message.answer_photo.assert_awaited_once()
    assert str(callback.message.answer_photo.await_args.args[0].path) == str(
        pets.ASSET_DIR / "owl_baby.jpg"
    )


# ---------------------------------------------------------------------------
# Handlers — feeding
# ---------------------------------------------------------------------------

async def test_feed_callback_without_food(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس")
        session.add(pet)
        await session.flush()
        pet_id = pet.id

    callback = _callback(f"pet_feed:{pet_id}")
    bot = _bot()
    await pets.handle_pet_feed(callback, state, bot)

    assert callback.answer.await_args.args[0] == pets.FEED_NO_FOOD_ALERT
    assert callback.answer.await_args.kwargs["show_alert"] is True
    bot.edit_message_media.assert_not_awaited()


async def test_feed_callback_when_the_pet_is_full(db, state):
    async with db.begin() as session:
        pet = UserPet(
            user_id=1,
            pet_type="cat",
            custom_name="ملوس",
            # The handler runs on the real clock, so anchor to it.
            last_fed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key="pet_food", quantity=2))

    callback = _callback(f"pet_feed:{pet_id}")
    bot = _bot()
    await pets.handle_pet_feed(callback, state, bot)

    assert callback.answer.await_args.args[0] == pets.FEED_TOO_SOON_ALERT
    bot.edit_message_media.assert_not_awaited()
    async with db.begin() as session:
        assert await ItemService.get_total(session, 1, "pet_food") == 2


async def test_feed_callback_updates_the_caption(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس", xp=100)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key="pet_food", quantity=1))

    callback = _callback(f"pet_feed:{pet_id}")
    bot = _bot()
    await pets.handle_pet_feed(callback, state, bot)

    # Art stays the baby's; the caption shows the new XP.
    bot.edit_message_media.assert_awaited_once()
    edited = bot.edit_message_media.await_args.kwargs
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "cat_baby.jpg")
    assert f"{fa_digits(150)}/{fa_digits(500)}" in edited["media"].caption
    assert "🍖" in edited["media"].caption

    async with db.begin() as session:
        assert await ItemService.get_total(session, 1, "pet_food") == 0
        stored = await session.get(UserPet, pet_id)
        assert stored.xp == 150


async def test_feed_callback_caption_renders_bold_html(db, state):
    """The feed caption keeps real ``<b>`` tags — never ``&lt;b&gt;``.

    Regression: the caption used to be rendered twice (once in the handler,
    once in the card renderer), which escaped the ``<b>`` tags into raw
    HTML text in the UI.
    """
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="cat", custom_name="ملوس", xp=100)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key="pet_food", quantity=1))

    callback = _callback(f"pet_feed:{pet_id}")
    bot = _bot()
    await pets.handle_pet_feed(callback, state, bot)

    caption = bot.edit_message_media.await_args.kwargs["media"].caption
    assert "<b>ملوس</b>" in caption
    assert "&lt;b&gt;" not in caption
    assert "&amp;lt;" not in caption
    # The media edit is sent with an explicit HTML parse mode.
    assert bot.edit_message_media.await_args.kwargs["media"].parse_mode == "HTML"


async def test_feed_callback_caption_renders_bold_html_when_evolving(db, state):
    """The evolution caption keeps real ``<b>`` tags too."""
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=470)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key="pet_food", quantity=1))

    callback = _callback(f"pet_feed:{pet_id}")
    bot = _bot()
    await pets.handle_pet_feed(callback, state, bot)

    caption = bot.edit_message_media.await_args.kwargs["media"].caption
    assert "<b>پشمک</b>" in caption
    assert "&lt;b&gt;" not in caption
    assert "تبریک" in caption


async def test_feed_callback_evolves_and_swaps_the_art(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=1, pet_type="owl", custom_name="پشمک", xp=470)
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key="pet_food", quantity=1))

    callback = _callback(f"pet_feed:{pet_id}")
    bot = _bot()
    await pets.handle_pet_feed(callback, state, bot)

    edited = bot.edit_message_media.await_args.kwargs
    assert str(edited["media"].media.path) == str(pets.ASSET_DIR / "owl_adult.jpg")
    caption = edited["media"].caption
    assert "تبریک" in caption
    assert "بالغ شد" in caption
    # The pet is now an adult: feeding stays (hunger) and the skill upgrade
    # button appears (skill_level 1 < 3).
    buttons = [b for row in edited["reply_markup"].inline_keyboard for b in row]
    callbacks = [b.callback_data for b in buttons]
    assert f"pet_feed:{pet_id}" in callbacks
    assert f"pet_skill_up:{pet_id}" in callbacks

    async with db.begin() as session:
        stored = await session.get(UserPet, pet_id)
        assert stored.is_adult is True


async def test_feed_callback_refuses_another_players_pet(db, state):
    async with db.begin() as session:
        pet = UserPet(user_id=2, pet_type="dog", custom_name="rex")
        session.add(pet)
        await session.flush()
        pet_id = pet.id
        session.add(UserItem(user_id=1, item_key="pet_food", quantity=1))

    callback = _callback(f"pet_feed:{pet_id}", clicker_id=1)
    bot = _bot()
    await pets.handle_pet_feed(callback, state, bot)

    bot.edit_message_media.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True

    async with db.begin() as session:
        stored = await session.get(UserPet, pet_id)
        assert stored.xp == 0
        assert await ItemService.get_total(session, 1, "pet_food") == 1


async def test_feed_callback_is_registered():
    registered = [
        handler
        for handler in pets.router.callback_query.handlers
        if handler.callback is pets.handle_pet_feed
    ]
    assert registered
    event = SimpleNamespace(data="pet_feed:1")
    assert all([await flt.call(event, bot=None) for flt in registered[0].filters])


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------

def test_pet_food_artwork_is_bundled_and_full_bleed():
    """The shop card resolves ``assets/items/pet_food.jpg`` (1024², no frame)."""
    from PIL import Image

    from sib.services.assets import bundled_item_path

    path = bundled_item_path("pet_food")
    assert path is not None
    assert path.suffix == ".jpg"

    with Image.open(path) as image:
        assert image.size == (1024, 1024)
        assert image.mode == "RGB"
        pixels = image.load()
        edges = [pixels[x, 0] for x in range(0, 1024, 64)]
        edges += [pixels[x, 1023] for x in range(0, 1024, 64)]
        edges += [pixels[0, y] for y in range(0, 1024, 64)]
        edges += [pixels[1023, y] for y in range(0, 1024, 64)]
    assert not any(min(pixel) > 238 and max(pixel) - min(pixel) < 10 for pixel in edges)
