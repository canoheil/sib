"""Profile-skin persistence, artwork resolution, and Telegram UI coverage."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TelegramUser

from sib.database.models import PlayerProgression, User
from sib.keyboards.common import get_profile_keyboard, get_profile_skin_keyboard
from sib.services import player_level_art, profile_artwork
from sib.services.player_level_art import level_art_filename
from sib.services.player_xp import PlayerXPService
from sib.services.profile_artwork import ProfileArtworkResolver
from sib.services.profile_skin import (
    DEFAULT_PROFILE_SKIN,
    PROFILE_SKINS,
    ProfileSkin,
    ProfileSkinService,
    normalize_profile_skin,
)


@pytest.mark.parametrize("stored_value", [None, "", "future_skin", " RED_APPLE "])
def test_skin_normalization_is_safe_and_defaults_to_green(stored_value):
    expected = ProfileSkin.RED_APPLE if stored_value == " RED_APPLE " else DEFAULT_PROFILE_SKIN
    assert normalize_profile_skin(stored_value) is expected


def test_skin_picker_offers_all_four_and_marks_only_the_active_skin():
    profile_callbacks = {
        button.callback_data
        for row in get_profile_keyboard().inline_keyboard
        for button in row
    }
    assert "profile_skin" in profile_callbacks

    picker = get_profile_skin_keyboard(ProfileSkin.GOLDEN_APPLE)
    buttons = [button for row in picker.inline_keyboard for button in row]
    options = {
        button.callback_data: button
        for button in buttons
        if (button.callback_data or "").startswith("profile_skin_select:")
    }
    assert set(options) == {
        f"profile_skin_select:{skin.value}" for skin in PROFILE_SKINS
    }
    selected = options["profile_skin_select:golden_apple"]
    assert selected.style == "success"
    assert selected.text.startswith("✅ Golden Apple")
    assert sum(button.style == "success" for button in options.values()) == 1
    assert any(button.callback_data == "apple_profile" for button in buttons)


@pytest.mark.parametrize("level", range(1, 31))
def test_green_apple_still_resolves_the_exact_legacy_level_art(level):
    resolution = ProfileArtworkResolver.resolve(level, ProfileSkin.GREEN_APPLE)
    assert resolution.skin is ProfileSkin.GREEN_APPLE
    assert resolution.level == level
    assert resolution.stage is None
    assert resolution.path == player_level_art.LEVEL_ART_DIR / level_art_filename(level)
    assert resolution.path.is_file()


@pytest.mark.parametrize(
    "skin",
    [ProfileSkin.RED_APPLE, ProfileSkin.GOLDEN_APPLE, ProfileSkin.MOON_APPLE],
)
@pytest.mark.parametrize("level", range(1, 31))
def test_new_skin_stage_mapping_for_every_level(skin, level):
    expected_stage = (level - 1) // 5 + 1
    resolution = ProfileArtworkResolver.resolve(level, skin)
    assert resolution.skin is skin
    assert resolution.level == level
    assert resolution.stage == expected_stage
    assert resolution.path == (
        profile_artwork.PROFILE_SKIN_ART_DIR
        / skin.value
        / f"stage_{expected_stage:02d}.jpg"
    )


@pytest.mark.parametrize("level", [None, "not-a-level", 0, -5, 31, 1000])
def test_new_skin_level_input_is_clamped_by_existing_level_art_helper(level):
    resolved = ProfileArtworkResolver.resolve(level, ProfileSkin.MOON_APPLE)
    assert 1 <= resolved.level <= 30
    assert resolved.stage == (resolved.level - 1) // 5 + 1


@pytest.mark.asyncio
async def test_new_user_and_missing_saved_skin_default_to_green(db_session):
    user = User(id=83001, first_name="پروفایل", username="skin_default")
    db_session.add(user)
    await db_session.flush()

    assert user.profile_skin == "green_apple"
    assert await ProfileSkinService.get_selected_skin(db_session, user.id) is DEFAULT_PROFILE_SKIN
    assert await ProfileSkinService.get_selected_skin(db_session, 99999999) is DEFAULT_PROFILE_SKIN
    assert normalize_profile_skin(None) is DEFAULT_PROFILE_SKIN


@pytest.mark.asyncio
async def test_selected_skin_survives_session_reload_without_changing_xp(db_session):
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "پوسته‌دار")
    before = await PlayerXPService.get_progress(db_session, user.id)

    assert await ProfileSkinService.set_selected_skin(
        db_session, user.id, ProfileSkin.MOON_APPLE
    ) is ProfileSkin.MOON_APPLE
    await db_session.commit()

    # A separate AsyncSession models a process/request reload rather than
    # reading only the first session's identity-map state.
    from sib.database.connection import get_async_sessionmaker

    async with get_async_sessionmaker()() as reloaded_session:
        selected = await ProfileSkinService.get_selected_skin(
            reloaded_session, user.id
        )
        after = await PlayerXPService.get_progress(reloaded_session, user.id)

    assert selected is ProfileSkin.MOON_APPLE
    assert after == before


@pytest.mark.asyncio
async def test_invalid_persisted_skin_falls_back_to_green_art(db_session):
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "پوسته نامعتبر")
    user.profile_skin = "unknown_future_skin"
    await db_session.commit()

    selected = await ProfileSkinService.get_selected_skin(db_session, user.id)
    resolved = ProfileArtworkResolver.resolve(17, user.profile_skin)
    assert selected is ProfileSkin.GREEN_APPLE
    assert resolved.skin is ProfileSkin.GREEN_APPLE
    assert resolved.path.name == "level_17.jpg"


@pytest.mark.asyncio
async def test_level_changes_resolve_a_new_stage_for_the_same_saved_skin(db_session):
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "سطح‌دار")
    await ProfileSkinService.set_selected_skin(db_session, user.id, ProfileSkin.RED_APPLE)
    progression = await db_session.get(PlayerProgression, user.id)
    if progression is None:
        progression = PlayerProgression(user_id=user.id, total_xp=0)
        db_session.add(progression)
        await db_session.flush()

    for level, expected_stage in ((3, 1), (17, 4), (29, 6)):
        progression.total_xp = PlayerXPService.cumulative_xp_for_level(level)
        await db_session.flush()
        progress = await PlayerXPService.get_progress(db_session, user.id)
        assert progress.level == level
        selected = await ProfileSkinService.get_selected_skin(db_session, user.id)
        artwork = ProfileArtworkResolver.resolve(progress.level, selected)
        assert selected is ProfileSkin.RED_APPLE
        assert artwork.stage == expected_stage


@pytest.mark.asyncio
async def test_missing_new_skin_asset_falls_back_to_legacy_green_then_uses_added_asset(
    monkeypatch, tmp_path
):
    skin_root = tmp_path / "profile_skins"
    legacy_root = tmp_path / "player_levels"
    monkeypatch.setattr(profile_artwork, "PROFILE_SKIN_ART_DIR", skin_root)
    monkeypatch.setattr(player_level_art, "LEVEL_ART_DIR", legacy_root)

    legacy_art = legacy_root / "level_17.jpg"
    legacy_art.parent.mkdir(parents=True)
    legacy_art.write_bytes(b"test-only image placeholder")
    progress = SimpleNamespace(level=17)

    missing_skin_photo = ProfileArtworkResolver.profile_photo(
        progress, skin=ProfileSkin.RED_APPLE
    )
    assert str(missing_skin_photo.path).endswith("level_17.jpg")

    stage_art = skin_root / "red_apple" / "stage_04.jpg"
    stage_art.parent.mkdir(parents=True)
    stage_art.write_bytes(b"test-only image placeholder")
    selected_skin_photo = ProfileArtworkResolver.profile_photo(
        progress, skin=ProfileSkin.RED_APPLE
    )
    assert selected_skin_photo.path == stage_art


@pytest.mark.asyncio
async def test_skin_selection_callback_persists_choice_and_marks_it_active(db_session):
    from sib.handlers.start import handle_profile_skin_selection

    user_id = 83002
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "profile_skin_select:moon_apple"
    callback.from_user = TelegramUser(
        id=user_id, is_bot=False, first_name="پوسته‌دار", username="picker"
    )
    callback.message = MagicMock(spec=Message)
    callback.message.chat = Chat(id=-10083002, type="supergroup", title="پوسته‌ها")
    callback.message.message_id = 83002
    callback.message.content_type = "text"
    callback.message.text = "Change Skin"
    callback.message.reply_markup = None
    callback.answer = AsyncMock()

    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    before = PlayerXPService.get_level_progress(0)
    await handle_profile_skin_selection(callback, bot)

    db_session.expire_all()
    assert await ProfileSkinService.get_selected_skin(db_session, user_id) is ProfileSkin.MOON_APPLE
    assert await PlayerXPService.get_progress(db_session, user_id) == before

    edited_text = bot.edit_message_text.await_args.args[0]
    keyboard = bot.edit_message_text.await_args.kwargs["reply_markup"]
    assert "Change Skin / تغییر پوسته" in edited_text
    assert "Moon Apple" in edited_text
    marked = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("profile_skin_select:")
        and button.style == "success"
    ]
    assert len(marked) == 1
    assert marked[0].callback_data == "profile_skin_select:moon_apple"
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_panel_uses_selected_skin_stage_when_asset_is_available(
    db_session, monkeypatch, tmp_path
):
    from sib.handlers.start import handle_apple_profile

    user_id = 83003
    user = User(
        id=user_id,
        first_name="پوسته",
        username="skin_profile",
        profile_skin=ProfileSkin.RED_APPLE.value,
    )
    level = 17
    progression = PlayerProgression(
        user_id=user_id,
        total_xp=PlayerXPService.cumulative_xp_for_level(level),
    )
    db_session.add_all([user, progression])
    await db_session.commit()

    skin_root = tmp_path / "profile_skins"
    monkeypatch.setattr(profile_artwork, "PROFILE_SKIN_ART_DIR", skin_root)
    stage_art = skin_root / "red_apple" / "stage_04.jpg"
    stage_art.parent.mkdir(parents=True)
    stage_art.write_bytes(b"test-only image placeholder")

    callback = MagicMock(spec=CallbackQuery)
    callback.data = "apple_profile"
    callback.from_user = TelegramUser(
        id=user_id, is_bot=False, first_name="پوسته", username="skin_profile"
    )
    callback.message = MagicMock(spec=Message)
    callback.message.chat = Chat(id=-10083003, type="supergroup", title="پروفایل")
    callback.message.message_id = 83003
    callback.message.content_type = "text"
    callback.message.text = "منوی اصلی"
    callback.message.reply_markup = None
    callback.answer = AsyncMock()

    sent_profile = MagicMock(spec=Message)
    sent_profile.message_id = 83004
    sent_profile.chat = callback.message.chat
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=sent_profile)
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.edit_message_media = AsyncMock(return_value=True)

    await handle_apple_profile(callback, bot)

    assert bot.send_photo.await_count == 1
    assert bot.send_photo.await_args.args[1].path == stage_art
    assert "Red Apple" in bot.send_photo.await_args.kwargs["caption"]



def test_new_skin_assets_match_the_part_2_contract_and_resolve():
    """All 18 stage files exist at the paths selected by the central resolver."""
    skins = (
        ProfileSkin.RED_APPLE,
        ProfileSkin.GOLDEN_APPLE,
        ProfileSkin.MOON_APPLE,
    )
    expected_assets = set()

    for skin in skins:
        for stage in range(1, 7):
            path = (
                profile_artwork.PROFILE_SKIN_ART_DIR
                / skin.value
                / f"stage_{stage:02d}.jpg"
            )
            expected_assets.add(path)
            assert path.relative_to(Path(__file__).resolve().parents[1]).as_posix() == (
                f"assets/profile_skins/{skin.value}/stage_{stage:02d}.jpg"
            )
            assert path.is_file()
            level = (stage - 1) * 5 + 1
            resolution = ProfileArtworkResolver.resolve(level, skin)
            assert resolution.stage == stage
            assert resolution.path == path
            photo = ProfileArtworkResolver.profile_photo(
                SimpleNamespace(level=level), skin=skin
            )
            assert photo.path == path

    actual_assets = {
        path
        for skin in skins
        for path in (profile_artwork.PROFILE_SKIN_ART_DIR / skin.value).iterdir()
        if path.is_file()
    }
    assert actual_assets == expected_assets
