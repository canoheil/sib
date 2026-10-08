"""Card screens: view-only detail, Card Upgrade screen, callbacks, navigation."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import InlineKeyboardMarkup
from PIL import Image
from sqlalchemy import select

from sib.database.models import AppleBattleQueueEntry, PlayerCard
from sib.handlers import cards as cards_handlers
from sib.handlers.cards import (
    handle_card_main_set,
    handle_card_panel,
    handle_card_upgrade_do,
    handle_card_upgrade_screen,
)
from sib.handlers.start import handle_apple_cards
from sib.keyboards.cards import get_card_detail_keyboard, get_card_upgrade_keyboard
from sib.services.card_progression import MAX_CARD_LEVEL
from sib.services.card_upgrade import CardUpgradeService
from sib.services.card_upgrade_render import ART_KEY, render_upgrade_screen
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.tests.test_parties import create_test_user
from sib.services.panel import OWNER_SCOPED_CALLBACKS, is_owner_scoped_callback
from sib.tests.test_card_upgrade import _fund, _holdings, _level, _new_card
from sib.tests.test_nav_root_fixes import _all_buttons, _callback, _rows

REPO = Path(__file__).resolve().parents[1]
CAPTION_LIMIT = 1024


def _shown(edit: AsyncMock):
    """(text, keyboard, photo) of the last screen handed to the edit helper."""
    call = edit.call_args
    text = call.args[2] if len(call.args) > 2 else call.kwargs["text"]
    return text, call.kwargs.get("reply_markup"), call.kwargs.get("photo")


@pytest.fixture
def edit():
    with patch.object(cards_handlers, "safe_edit_callback_message", new=AsyncMock()) as m:
        yield m


async def _press(handler, user_id, data):
    cb = _callback(user_id, data)
    await handler(cb, bot=AsyncMock())
    return cb


def _texts(kb):
    return [b.text for b in _all_buttons(kb)]


# ---------------------------------------------------------------------------
# Card detail: Main Card selection, upgrade and navigation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_card_detail_shows_main_card_selector_and_upgrade(db_session, edit):
    user, _c, card = await _new_card(db_session)
    cb = await _press(handle_card_panel, user.id, f"card_panel:{card.id}")
    cb.answer.assert_awaited()
    text, kb, _photo = _shown(edit)

    assert "سطح" in text and "`22`" in text and "`20`" in text and "`18`" in text
    assert "کارت اصلی هنوز انتخاب نشده" in text
    main_buttons = [b for b in _all_buttons(kb) if b.callback_data.startswith("card_main_set:")]
    assert [(b.text, b.callback_data) for b in main_buttons] == [
        ("⭐ انتخاب به‌عنوان کارت اصلی", f"card_main_set:{card.id}:0")
    ]
    upgrade = [b for b in _all_buttons(kb) if b.callback_data.startswith("card_upgrade")]
    assert [(b.text, b.callback_data) for b in upgrade] == [
        ("⬆️ ارتقای کارت", f"card_upgrade:{card.id}")
    ]
    assert len(_all_buttons(kb)) == 3  # Main Card, upgrade, back; no allocation controls
    assert _rows(kb)[-1] == ["apple_cards"]


@pytest.mark.asyncio
async def test_selected_main_card_status_has_no_redundant_selector(db_session, edit):
    user, _character, card = await _new_card(db_session, code="main_ui_selected")
    await PlayerCardService.set_favorite(
        db_session, user.id, card.id, expected_main_card_id=0
    )
    await db_session.commit()

    await _press(handle_card_panel, user.id, f"card_panel:{card.id}")
    text, kb, _photo = _shown(edit)
    assert "این کارت، کارت اصلی شماست" in text
    assert not any(
        button.callback_data.startswith("card_main_set:") for button in _all_buttons(kb)
    )
    assert len(_all_buttons(kb)) == 2  # upgrade + back


@pytest.mark.asyncio
async def test_main_card_callback_duplicate_is_idempotent(db_session, edit):
    user, _character, card = await _new_card(db_session, code="main_ui_duplicate")
    callback_data = f"card_main_set:{card.id}:0"

    first = await _press(handle_card_main_set, user.id, callback_data)
    second = await _press(handle_card_main_set, user.id, callback_data)
    assert first.answer.await_args.args[0] == "کارت اصلی شما به‌روزرسانی شد ⭐"
    assert second.answer.await_args.args[0] == "کارت اصلی شما به‌روزرسانی شد ⭐"

    await db_session.refresh(card)
    assert card.is_favorite is True
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == card.id
    text, kb, _photo = _shown(edit)
    assert "این کارت، کارت اصلی شماست" in text
    assert not any(
        button.callback_data.startswith("card_main_set:") for button in _all_buttons(kb)
    )


@pytest.mark.asyncio
async def test_main_card_callback_rejects_stale_selection(db_session, edit):
    user, _character, current = await _new_card(db_session, code="main_ui_stale_a")
    next_character = await CharacterService.create(
        db_session,
        code="main_ui_stale_b",
        name_fa="قهرمان انتخاب قدیمی",
        power=20,
        speed=20,
        intelligence=20,
    )
    stale_target = await PlayerCardService.acquire(db_session, user.id, next_character.id)
    await db_session.commit()
    await PlayerCardService.set_favorite(
        db_session, user.id, current.id, expected_main_card_id=0
    )
    await db_session.commit()

    callback = await _press(
        handle_card_main_set,
        user.id,
        f"card_main_set:{stale_target.id}:0",
    )
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert "از وقتی این صفحه را باز کردی تغییر کرده" in callback.answer.await_args.args[0]

    await db_session.refresh(current)
    await db_session.refresh(stale_target)
    assert current.is_favorite is True
    assert stale_target.is_favorite is False


@pytest.mark.asyncio
async def test_main_card_callback_rejects_unowned_card(db_session, edit):
    owner, _character, card = await _new_card(db_session, code="main_ui_unowned")
    stranger = await create_test_user(db_session, "کارت‌ندار")

    callback = await _press(
        handle_card_main_set,
        stranger.id,
        f"card_main_set:{card.id}:0",
    )
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert "متعلق به شما نیست" in callback.answer.await_args.args[0]
    assert await PlayerCardService.get_main_card(db_session, owner.id) is None
    assert await PlayerCardService.get_main_card(db_session, stranger.id) is None


@pytest.mark.asyncio
async def test_main_card_callback_rejects_missing_card(db_session, edit):
    user, _character, _card = await _new_card(db_session, code="main_ui_missing")
    callback = await _press(
        handle_card_main_set,
        user.id,
        "card_main_set:999999999:0",
    )
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert "متعلق به شما نیست" in callback.answer.await_args.args[0]
    assert await PlayerCardService.get_main_card(db_session, user.id) is None


@pytest.mark.asyncio
async def test_detail_keyboard_has_no_legacy_controls():
    kb = get_card_detail_keyboard(7)
    data = [b.callback_data for b in _all_buttons(kb)]
    assert not any(d.startswith("card_up:") for d in data)
    assert not any("reallocate" in d or "boost" in d for d in data)

    selected = get_card_detail_keyboard(7, current_main_card_id=7)
    assert not any(
        button.callback_data.startswith("card_main_set:") for button in _all_buttons(selected)
    )
    large_ids = get_card_detail_keyboard(
        9_223_372_036_854_775_807,
        current_main_card_id=9_223_372_036_854_775_806,
    )
    assert all(
        len(button.callback_data.encode()) <= 64 for button in _all_buttons(large_ids)
    )


@pytest.mark.asyncio
async def test_my_cards_list_opens_the_view_only_screen(db_session):
    user, _c, card = await _new_card(db_session)
    cb = _callback(user.id, "apple_cards")
    with patch("sib.handlers.start.safe_edit_callback_message", new=AsyncMock()) as e:
        await handle_apple_cards(cb, bot=AsyncMock())
    kb = e.call_args.kwargs["reply_markup"]
    assert f"card_panel:{card.id}" in {b.callback_data.split("|")[0] for b in _all_buttons(kb)}


# ---------------------------------------------------------------------------
# Upgrade screen
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_upgrade_screen_shows_everything_the_spec_requires(db_session, edit):
    user, character, card = await _new_card(db_session)
    await _fund(db_session, user, apples=70, gold=1)
    await _press(handle_card_upgrade_screen, user.id, f"card_upgrade:{card.id}")
    text, kb, photo = _shown(edit)

    assert character.name_fa in text                      # card name
    assert "`1` ◀️ `2`" in text                           # level N -> N+1
    for label, now, after in (("💪 قدرت", 22, 32), ("⚡ سرعت", 20, 30), ("🧠 هوش", 18, 28)):
        assert f"{label}: `{now}`" in text and f"{label}: `{after}`" in text
    assert "📊 مجموع: `60`" in text and "📊 مجموع: `90`" in text
    assert "🍎 سیب معمولی × `100`" in text                # exact cost
    assert "🪙" in text and "× `3`" in text
    assert "موجودی تو" in text and "`70`" in text and "`1`" in text  # own resources
    assert "⚠️" in text and "`30`" in text and "`2`" in text       # exact missing
    assert photo is not None and len(text) <= CAPTION_LIMIT

    assert [(b.text, b.callback_data) for b in _all_buttons(kb)] == [
        ("⬆️ ارتقای کارت", f"card_upgrade_do:{card.id}:1"),
        ("↩️ بازگشت", f"card_panel:{card.id}"),
        ("🏠 منوی اصلی", "main_menu"),
    ]


@pytest.mark.asyncio
async def test_affordable_screen_has_no_missing_section(db_session, edit):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=500, gold=10)
    await _press(handle_card_upgrade_screen, user.id, f"card_upgrade:{card.id}")
    text, _kb, _p = _shown(edit)
    assert "منابع کم‌شده" not in text


@pytest.mark.asyncio
async def test_every_level_fits_a_photo_caption(db_session):
    user, _c, card = await _new_card(db_session)
    for level in range(1, MAX_CARD_LEVEL + 1):
        await db_session.execute(
            PlayerCard.__table__.update().where(PlayerCard.id == card.id).values(level=level)
        )
        await db_session.commit()
        preview = await CardUpgradeService.preview(db_session, user.id, card.id)
        assert len(render_upgrade_screen(preview)) <= CAPTION_LIMIT


@pytest.mark.asyncio
async def test_level_10_screen_is_a_max_level_message_without_upgrade_button(db_session, edit):
    user, _c, card = await _new_card(db_session)
    await db_session.execute(
        PlayerCard.__table__.update().where(PlayerCard.id == card.id).values(level=MAX_CARD_LEVEL)
    )
    await db_session.commit()
    await _press(handle_card_upgrade_screen, user.id, f"card_upgrade:{card.id}")
    text, kb, _p = _shown(edit)

    assert "بیشترین سطح" in text and "هزینه" not in text
    assert [b.callback_data for b in _all_buttons(kb)] == [f"card_panel:{card.id}", "main_menu"]
    assert not any(t.startswith("⬆️") for t in _texts(kb))


@pytest.mark.asyncio
async def test_stale_do_button_on_a_level_10_card_is_refused_and_free(db_session, edit):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=9_999, gold=50, diamond=50, obsidian=50)
    await db_session.execute(
        PlayerCard.__table__.update().where(PlayerCard.id == card.id).values(level=MAX_CARD_LEVEL)
    )
    await db_session.commit()
    before = await _holdings(db_session, user)
    cb = await _press(handle_card_upgrade_do, user.id, f"card_upgrade_do:{card.id}:9")
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert await _holdings(db_session, user) == before
    assert await _level(db_session, card.id) == MAX_CARD_LEVEL


# ---------------------------------------------------------------------------
# Upgrade action
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_insufficient_resources_stays_clickable_and_shows_exact_missing(db_session, edit):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=40, gold=2)
    before = await _holdings(db_session, user)

    cb = await _press(handle_card_upgrade_do, user.id, f"card_upgrade_do:{card.id}:1")
    args, kwargs = cb.answer.await_args
    alert = args[0]
    assert kwargs["show_alert"] is True
    assert "منابع کافی نیست" in alert
    assert "🍎 سیب معمولی: 60" in alert and "🪙" in alert and ": 1" in alert
    assert len(alert) <= 200

    assert await _holdings(db_session, user) == before
    assert await _level(db_session, card.id) == 1
    # Screen re-rendered, the button is still there for the next try.
    _t, kb, _p = _shown(edit)
    assert f"card_upgrade_do:{card.id}:1" in [b.callback_data for b in _all_buttons(kb)]


@pytest.mark.asyncio
async def test_successful_upgrade_updates_the_screen_and_charges_once(db_session, edit):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=500, gold=10)
    db_session.add(AppleBattleQueueEntry(
            user_id=user.id, card_id=card.id, level=1, queued_at=datetime.now(timezone.utc)
        ))
    await db_session.commit()

    cb = await _press(handle_card_upgrade_do, user.id, f"card_upgrade_do:{card.id}:1")
    assert "سطح 2" in cb.answer.await_args.args[0]
    text, kb, _p = _shown(edit)
    assert "`2` ◀️ `3`" in text
    assert f"card_upgrade_do:{card.id}:2" in [b.callback_data for b in _all_buttons(kb)]
    assert await _level(db_session, card.id) == 2
    assert await _holdings(db_session, user) == (400, {"apple_gold": 7, "apple_diamond": 0, "apple_obsidian": 0})

    # The double-tapped (old) button cannot buy a second level.
    again = await _press(handle_card_upgrade_do, user.id, f"card_upgrade_do:{card.id}:1")
    assert again.answer.await_args.kwargs.get("show_alert") in (False, None)
    assert await _level(db_session, card.id) == 2
    assert await _holdings(db_session, user) == (400, {"apple_gold": 7, "apple_diamond": 0, "apple_obsidian": 0})


@pytest.mark.asyncio
async def test_not_the_owner_cannot_use_the_handlers(db_session, edit):
    _owner, _c, card = await _new_card(db_session)
    thief = await create_test_user(db_session, "مزاحم")
    await _fund(db_session, thief, apples=9_999, gold=50)
    before = await _holdings(db_session, thief)
    cb = await _press(handle_card_upgrade_do, thief.id, f"card_upgrade_do:{card.id}:1")
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert await _holdings(db_session, thief) == before
    assert await _level(db_session, card.id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, data",
    [
        (handle_card_panel, "card_panel:abc"),
        (handle_card_panel, "card_panel:"),
        (handle_card_main_set, "card_main_set:x:0"),
        (handle_card_main_set, "card_main_set:1:-1"),
        (handle_card_main_set, "card_main_set:9223372036854775808:0"),
        (handle_card_upgrade_screen, "card_upgrade:1:2"),
        (handle_card_upgrade_do, "card_upgrade_do:1"),
        (handle_card_upgrade_do, "card_upgrade_do:x:y"),
    ],
)
async def test_malformed_callbacks_are_answered_and_harmless(db_session, edit, handler, data):
    user, _c, _card = await _new_card(db_session)
    cb = await _press(handler, user.id, data)
    cb.answer.assert_awaited()
    assert cb.answer.await_args.kwargs.get("show_alert") is True


# ---------------------------------------------------------------------------
# Navigation safety
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_viewing_and_navigating_changes_no_game_state(db_session, edit):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=300, gold=9)
    db_session.add(AppleBattleQueueEntry(
            user_id=user.id, card_id=card.id, level=1, queued_at=datetime.now(timezone.utc)
        ))
    await db_session.commit()
    before = await _holdings(db_session, user)

    for handler, data in (
        (handle_card_panel, f"card_panel:{card.id}"),
        (handle_card_upgrade_screen, f"card_upgrade:{card.id}"),
        (handle_card_panel, f"card_panel:{card.id}"),  # "Back"
    ):
        await _press(handler, user.id, data)

    assert await _holdings(db_session, user) == before
    assert await _level(db_session, card.id) == 1
    queue = await db_session.scalars(select(AppleBattleQueueEntry))
    assert [e.card_id for e in queue] == [card.id]  # loadout untouched


def test_upgrade_keyboard_is_small_and_uses_the_shared_back_helper():
    for level, can in ((1, True), (9, True), (10, False)):
        kb = get_card_upgrade_keyboard(123456789, level, can)
        assert isinstance(kb, InlineKeyboardMarkup)
        assert all(len(row) <= 2 for row in kb.inline_keyboard)
        for b in _all_buttons(kb):
            assert len(b.callback_data.encode()) <= 64
        assert _rows(kb)[-1] == ["main_menu"]
        assert _rows(kb)[-2] == ["card_panel:123456789"]


def test_new_callbacks_are_owner_scoped_and_old_ones_are_gone():
    for prefix in ("card_panel:", "card_main_set:", "card_upgrade:", "card_upgrade_do:"):
        assert prefix in OWNER_SCOPED_CALLBACKS
        assert is_owner_scoped_callback(prefix + "1")
    assert "card_up:" not in OWNER_SCOPED_CALLBACKS
    assert not is_owner_scoped_callback("card_up:1:1:1:1")


# ---------------------------------------------------------------------------
# Old flow is gone from the code base
# ---------------------------------------------------------------------------
def test_old_allocation_flow_is_removed_from_the_source_tree():
    offenders = []
    needles = ("card_up:", "boosted_", "level_budget", "reallocate", "boost_power", "_allocation_keyboard")
    phrases = ("نحوه توزیع", "توزیع امتیاز", "تخصیص امتیاز", "امتیاز قابل توزیع")
    for folder in ("handlers", "keyboards", "services", "database/models", "middleware"):
        for path in (REPO / folder).rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            for needle in (*needles, *phrases):
                if needle in src:
                    offenders.append((path.name, needle))
    # The archive model keeps the retired column names on purpose.
    offenders = [o for o in offenders if o[0] != "player_card.py"]
    assert offenders == []


def test_card_handler_no_longer_lives_in_the_shop_module():
    from sib.handlers import shop

    assert not hasattr(shop, "handle_card_upgrade")
    assert not hasattr(shop, "handle_card_panel")


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------
def test_upgrade_artwork_is_a_real_square_asset():
    path = REPO / "assets" / "items" / "card_upgrade.jpg"
    assert ART_KEY == "card_upgrade"
    assert path.is_file() and path.stat().st_size > 20_000
    with Image.open(path) as img:
        assert img.width == img.height >= 512


@pytest.mark.asyncio
async def test_artwork_resolves_through_the_asset_service(db_session):
    from sib.services.assets import AssetService

    file_id, fs_path = await AssetService.resolve_photo(db_session, ART_KEY)
    assert file_id is None
    assert fs_path is not None and Path(fs_path).name == "card_upgrade.jpg"
