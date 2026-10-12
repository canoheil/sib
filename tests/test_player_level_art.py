"""Level artwork integration — the profile's main photo follows the Level.

Covers the reusable resolver (``services/player_level_art.py``) and the
profile renderers that use it:

* ``handlers/start.py::handle_apple_profile``  — the «👤 پروفایل من» panel
* ``handlers/start.py::_open_sender_profile``  — the «سیب پروفایل» wake word
* ``handlers/apple_battle.py::cmd_profile``    — the ``/profile`` command

The artwork is presentation only: nothing here derives a Level (that stays
``PlayerXPService``) and no XP amount, threshold or progression rule is
touched. Levels are set up by writing the cumulative XP the EXISTING curve
already defines for them.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat, FSInputFile, Message, User as TelegramUser

from sib.services import player_level_art
from sib.services.player_level_art import (
    LEVEL_ART_DIR,
    clamp_level,
    level_art_filename,
    level_art_path,
    level_art_photo,
    profile_photo,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

GROUP_ID = -1008801
GROUP_TITLE = "گروه آرت سطح"

FAVORITE_CARD_CODE = "sib_sorkh"
PROFILE_LEVEL = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _photo_path(photo) -> str:
    """Filesystem path carried by a Telegram photo argument."""
    return str(getattr(photo, "path", photo))


def _tg_user(user_id: int, first_name: str = "آرت‌باز") -> TelegramUser:
    return TelegramUser(id=user_id, is_bot=False, first_name=first_name)


def _sent(message_id: int = 950) -> MagicMock:
    sent = MagicMock(spec=Message)
    sent.message_id = message_id
    sent.chat = Chat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    return sent


def _profile_message(user: TelegramUser) -> MagicMock:
    """A wake-word message whose reply/reply_photo both succeed."""
    msg = MagicMock(spec=Message)
    msg.chat = Chat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    msg.from_user = user
    msg.text = "سیب پروفایل"
    msg.message_id = 900
    msg.reply = AsyncMock(return_value=_sent())
    msg.reply_photo = AsyncMock(return_value=_sent())
    return msg


def _profile_callback(user: TelegramUser) -> tuple[MagicMock, MagicMock]:
    """A panel callback + bot whose photo transition goes through send_photo."""
    cb = MagicMock(spec=CallbackQuery)
    cb.data = "apple_profile"
    cb.from_user = user
    cb.message = MagicMock(spec=Message)
    cb.message.chat = Chat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    cb.message.message_id = 901
    cb.message.text = "menu"
    cb.message.reply_markup = None
    cb.answer = AsyncMock()

    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=_sent())
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.edit_message_media = AsyncMock(return_value=True)
    return cb, bot


async def _set_level(session, user_id: int, level: int) -> None:
    """Put an account exactly on ``level`` using the EXISTING XP curve.

    No threshold or award logic is changed — the cumulative XP for the level
    is read from ``PlayerXPService.cumulative_xp_for_level`` itself.
    """
    from sib.database.models import PlayerProgression
    from sib.services.player_xp import PlayerXPService

    total_xp = PlayerXPService.cumulative_xp_for_level(level)
    row = await session.get(PlayerProgression, user_id)
    if row is None:
        session.add(PlayerProgression(user_id=user_id, total_xp=total_xp))
    else:
        row.total_xp = total_xp
    await session.commit()


async def _favorite_card_owner(db_session, name: str = "آرت‌باز"):
    """A user with a Favorite Card whose own framed card art exists."""
    from sib.database.models import ReleaseStatus
    from sib.services.characters import CharacterService
    from sib.services.player_cards import PlayerCardService
    from sib.tests.test_parties import create_test_user

    character = await CharacterService.create(
        db_session,
        code=FAVORITE_CARD_CODE,
        name_fa=f"قهرمان {FAVORITE_CARD_CODE}",
        lore_fa="توضیح آزمایشی شخصیت برای صفحه پروفایل.",
        power=22,
        speed=20,
        intelligence=18,
        passive_key="test_passive",
    )
    await CharacterService.set_release_status(
        db_session, character.id, ReleaseStatus.RELEASED
    )
    character.shop_enabled = True
    character.is_active = True

    user = await create_test_user(db_session, name)
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await PlayerCardService.set_favorite(db_session, user.id, card.id, True)
    await db_session.commit()
    return user, character


# ---------------------------------------------------------------------------
# 1. The resolver: Level → zero-padded artwork file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level, filename",
    [
        (1, "level_01.jpg"),
        (2, "level_02.jpg"),
        (9, "level_09.jpg"),
        (10, "level_10.jpg"),
        (29, "level_29.jpg"),
        (30, "level_30.jpg"),
    ],
)
def test_level_art_filename_is_zero_padded(level, filename):
    assert level_art_filename(level) == filename


@pytest.mark.parametrize("level", [1, 2, 9, 10, 30])
def test_level_art_path_is_the_committed_asset(level):
    path = level_art_path(level)
    assert path is not None and path.is_file()
    assert path == REPO_ROOT / "assets" / "player_levels" / level_art_filename(level)


def test_every_level_of_the_curve_has_artwork():
    """Levels 1–30 each resolve to a real file in the committed art set."""
    from sib.services.player_xp import MAX_LEVEL

    assert MAX_LEVEL == player_level_art.MAX_LEVEL
    missing = [level for level in range(1, MAX_LEVEL + 1) if level_art_path(level) is None]
    assert missing == [], f"missing Level artwork for: {missing}"


def test_level_art_dir_is_resolved_from_the_package_not_the_cwd():
    """The resolver must not depend on the process working directory."""
    assert LEVEL_ART_DIR == REPO_ROOT / "assets" / "player_levels"
    assert not LEVEL_ART_DIR.is_relative_to(Path.cwd()) or LEVEL_ART_DIR.is_dir()


# ---------------------------------------------------------------------------
# 2. Defensive clamping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level, expected",
    [
        (0, 1),
        (-7, 1),
        (1, 1),
        (30, 30),
        (31, 30),
        (999, 30),
    ],
)
def test_out_of_range_levels_are_clamped(level, expected):
    assert clamp_level(level) == expected
    assert level_art_filename(level) == f"level_{expected:02d}.jpg"


@pytest.mark.parametrize("level", [None, "abc", "", object(), float("nan")])
def test_garbage_levels_never_raise(level):
    """A missing/garbage Level renders the Level 1 artwork, not an exception."""
    assert clamp_level(level) == 1
    assert level_art_filename(level) == "level_01.jpg"
    assert level_art_path(level) is not None


def test_clamped_out_of_range_levels_resolve_to_real_files():
    assert level_art_path(0) == level_art_path(1)
    assert level_art_path(31) == level_art_path(30)
    assert level_art_path(-100) is not None and level_art_path(9999) is not None


# ---------------------------------------------------------------------------
# 3. Missing artwork → None (never a crash)
# ---------------------------------------------------------------------------


def test_missing_artwork_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(player_level_art, "LEVEL_ART_DIR", tmp_path)
    assert level_art_path(5) is None
    assert level_art_photo(5) is None


def test_profile_photo_falls_back_when_the_artwork_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(player_level_art, "LEVEL_ART_DIR", tmp_path)
    fallback = object()
    assert profile_photo(SimpleNamespace(level=5), fallback) is fallback
    assert profile_photo(SimpleNamespace(level=5)) is None


def test_level_art_photo_is_a_local_file_argument():
    """Same representation the flows already use for local art."""
    photo = level_art_photo(PROFILE_LEVEL)
    assert isinstance(photo, FSInputFile)
    assert _photo_path(photo).endswith(f"level_{PROFILE_LEVEL:02d}.jpg")


def test_profile_photo_prefers_level_art_over_the_favorite_card():
    fallback = FSInputFile(REPO_ROOT / "assets" / "characters" / f"{FAVORITE_CARD_CODE}.jpg")
    photo = profile_photo(SimpleNamespace(level=PROFILE_LEVEL), fallback)
    assert _photo_path(photo).endswith(f"level_{PROFILE_LEVEL:02d}.jpg")
    assert _photo_path(photo) != _photo_path(fallback)


def test_profile_photo_reads_the_level_from_the_progress_object():
    """The Level is read from PlayerXPService's LevelProgress, never recomputed."""
    from sib.services.player_xp import PlayerXPService

    progress = PlayerXPService.get_level_progress(
        PlayerXPService.cumulative_xp_for_level(PROFILE_LEVEL)
    )
    assert progress.level == PROFILE_LEVEL
    assert _photo_path(profile_photo(progress)).endswith(f"level_{PROFILE_LEVEL:02d}.jpg")


# ---------------------------------------------------------------------------
# 4. Both profile entry paths use the LEVEL artwork as the main photo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_panel_uses_level_artwork_not_favorite_card(db_session):
    """«👤 پروفایل من» panel: main photo = Level art, not the Favorite Card."""
    from sib.handlers.start import handle_apple_profile

    user, character = await _favorite_card_owner(db_session)
    await _set_level(db_session, user.id, PROFILE_LEVEL)
    cb, bot = _profile_callback(_tg_user(user.id))

    await handle_apple_profile(cb, bot)

    assert bot.send_photo.await_count == 1
    photo = bot.send_photo.await_args.args[1]
    assert _photo_path(photo).endswith(f"level_{PROFILE_LEVEL:02d}.jpg")
    assert f"{character.code}.jpg" not in _photo_path(photo)


@pytest.mark.asyncio
async def test_profile_wake_word_uses_level_artwork_not_favorite_card(db_session):
    """«سیب پروفایل» wake word: main photo = Level art, not the Favorite Card."""
    from sib.handlers.start import handle_sib_profile_word

    user, character = await _favorite_card_owner(db_session)
    await _set_level(db_session, user.id, PROFILE_LEVEL)
    msg = _profile_message(_tg_user(user.id))

    await handle_sib_profile_word(msg, MagicMock())

    assert msg.reply_photo.await_count == 1
    photo = msg.reply_photo.await_args.args[0]
    assert _photo_path(photo).endswith(f"level_{PROFILE_LEVEL:02d}.jpg")
    assert f"{character.code}.jpg" not in _photo_path(photo)


@pytest.mark.asyncio
async def test_profile_command_uses_level_artwork_not_favorite_card(db_session):
    """/profile command: main photo = Level art, not the Favorite Card."""
    from sib.handlers.apple_battle import cmd_profile

    user, character = await _favorite_card_owner(db_session)
    await _set_level(db_session, user.id, PROFILE_LEVEL)
    msg = _profile_message(_tg_user(user.id))

    await cmd_profile(msg, MagicMock())

    assert msg.reply_photo.await_count == 1
    photo = msg.reply_photo.await_args.args[0]
    assert _photo_path(photo).endswith(f"level_{PROFILE_LEVEL:02d}.jpg")
    assert f"{character.code}.jpg" not in _photo_path(photo)


@pytest.mark.asyncio
async def test_profile_artwork_tracks_the_players_current_level(db_session):
    """A different Level shows a different artwork in both entry paths."""
    from sib.handlers.start import handle_apple_profile, handle_sib_profile_word

    user, _character = await _favorite_card_owner(db_session)
    tg_user = _tg_user(user.id)

    for level in (1, 9, 10, 30):
        await _set_level(db_session, user.id, level)

        msg = _profile_message(tg_user)
        await handle_sib_profile_word(msg, MagicMock())
        assert _photo_path(msg.reply_photo.await_args.args[0]).endswith(
            f"level_{level:02d}.jpg"
        ), f"wake word showed the wrong artwork for Level {level}"

        cb, bot = _profile_callback(tg_user)
        await handle_apple_profile(cb, bot)
        assert _photo_path(bot.send_photo.await_args.args[1]).endswith(
            f"level_{level:02d}.jpg"
        ), f"panel showed the wrong artwork for Level {level}"


# ---------------------------------------------------------------------------
# 5. Graceful degradation + preserved profile content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_level_artwork_still_renders_the_profile(db_session, monkeypatch, tmp_path):
    """No Level asset → the Favorite Card art is used and nothing raises."""
    from sib.handlers.start import handle_sib_profile_word

    monkeypatch.setattr(player_level_art, "LEVEL_ART_DIR", tmp_path)
    user, character = await _favorite_card_owner(db_session)
    await _set_level(db_session, user.id, PROFILE_LEVEL)
    msg = _profile_message(_tg_user(user.id))

    await handle_sib_profile_word(msg, MagicMock())

    assert msg.reply_photo.await_count == 1
    # The fallback photo is the Favorite Card art, not a Level artwork.
    assert _photo_path(msg.reply_photo.await_args.args[0]).endswith(f"{character.code}.jpg")
    caption = msg.reply_photo.await_args.kwargs["caption"]
    assert "سطح حساب" in caption and character.name_fa in caption


@pytest.mark.asyncio
async def test_missing_level_artwork_without_favorite_card_falls_back_to_text(
    db_session, monkeypatch, tmp_path
):
    """No Level asset and no Favorite Card → plain text profile, no crash."""
    from sib.handlers.start import handle_sib_profile_word
    from sib.tests.test_parties import create_test_user

    monkeypatch.setattr(player_level_art, "LEVEL_ART_DIR", tmp_path)
    user = await create_test_user(db_session, "بی‌آرت")
    await _set_level(db_session, user.id, PROFILE_LEVEL)
    msg = _profile_message(_tg_user(user.id))

    await handle_sib_profile_word(msg, MagicMock())

    assert msg.reply_photo.await_count == 0
    assert msg.reply.await_count == 1
    assert "سطح حساب" in msg.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_level_artwork_does_not_change_the_profile_text(db_session):
    """The profile keeps its Level/XP, wallet, seedling, card, favorite,
    battle and marriage content alongside the selected-skin line."""
    from sib.handlers.start import handle_sib_profile_word

    user, character = await _favorite_card_owner(db_session)
    await _set_level(db_session, user.id, PROFILE_LEVEL)
    msg = _profile_message(_tg_user(user.id))

    await handle_sib_profile_word(msg, MagicMock())

    caption = msg.reply_photo.await_args.kwargs["caption"]
    assert "پروفایل سیبی" in caption
    assert "سطح حساب" in caption          # Level
    assert "XP" in caption                  # XP progress
    assert "🍎 سیب:" in caption             # apple balance
    assert "🌱 نهال:" in caption            # seedlings
    assert "تعداد کارت‌ها" in caption       # card count
    assert character.name_fa in caption     # favorite character
    assert "نبردهای سیبی" in caption        # battle statistics


@pytest.mark.asyncio
async def test_level_artwork_is_read_only_for_xp(db_session):
    """Rendering the profile never writes XP/progression state."""
    from sqlalchemy import func, select

    from sib.database.models import PlayerProgression, PlayerXPGrant
    from sib.handlers.start import handle_sib_profile_word

    user, _character = await _favorite_card_owner(db_session)
    await _set_level(db_session, user.id, PROFILE_LEVEL)
    before = (await db_session.get(PlayerProgression, user.id)).total_xp
    grants_before = await db_session.scalar(select(func.count(PlayerXPGrant.id)))

    await handle_sib_profile_word(_profile_message(_tg_user(user.id)), MagicMock())

    after = (await db_session.get(PlayerProgression, user.id)).total_xp
    grants_after = await db_session.scalar(select(func.count(PlayerXPGrant.id)))
    assert after == before
    assert grants_after == grants_before
