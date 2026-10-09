"""Scene art for Marriage & Love moments (warm painterly asset set).

One cohesive illustration per moment, stored under ``assets/marriage_love/``
and committed to the repo. Sends use photo+caption (Telegram shows the art
above the Persian text and inline keyboard); edits of such messages go
through the central photo/media transition path in ``render_callback_screen``.

Design rules:
- art is TEXT-FREE; every word stays in the Persian caption/keyboard
- one warm painterly language shared with the rest of the Sib asset set

Presentation rule: this module is ADDITIVE ONLY. It never decides a
Marriage/Love outcome, never touches the domain, and every send degrades to
the plain text message the flow already used. A missing asset or a failed
upload must never change what the flow does - only whether art is shown.
"""

from pathlib import Path
from typing import Optional

from aiogram.types import Message

from sib.core.logging import logger
from sib.services.telegram_ui import CAPTION_LIMIT, caption_length

_SCENE_DIR = Path(__file__).resolve().parent.parent / "assets" / "marriage_love"

SCENES = {
    "proposal": "proposal.jpg",
    "wedding": "wedding.jpg",
    "divorce": "divorce.jpg",
    "flower_gift": "flower_gift.jpg",
}

# Scenes for other Sib features that reuse this delivery path (photo +
# caption, file_id cache, text fallback). They live under assets/ in their
# own folder and stay out of ``SCENES``, which is the marriage flow contract.
GROUP_SCENES = {
    "hug": "sib_hug/hug.jpg",  # Sib Hug («بغل سیبی»); art arrives in Phase 4
    "kiss": "sib_kiss/kiss.jpg",  # Sib Kiss («بوس سیبی»); art arrives in Phase 4
    "daily_chest": "daily_chest/chest.jpg",  # Daily Sib Treasure Chest group post («صندوقچه گنج روزانه»)
}
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

# Per-process file_id cache: after the first upload Telegram returns a
# file_id that makes later sends fast (no re-upload).
_file_id_cache: dict[str, str] = {}


def scene_path(key: Optional[str]) -> Optional[Path]:
    """Path of a committed scene asset, or None when it is unknown/absent."""
    if not key:
        return None
    if key in SCENES:
        path = _SCENE_DIR / SCENES[key]
    elif key in GROUP_SCENES:
        path = _ASSETS_DIR / GROUP_SCENES[key]
    else:
        return None
    if path.is_file():
        return path
    return None


def remember_scene_file_id(key: str, message: Optional[Message]) -> None:
    """Cache the uploaded file_id from a sent/edited photo message."""
    if not key or message is None:
        return
    photos = getattr(message, "photo", None)
    if isinstance(photos, (list, tuple)) and photos:
        file_id = getattr(photos[-1], "file_id", None)
        if isinstance(file_id, str) and file_id:
            _file_id_cache[key] = file_id


def scene_photo(key: Optional[str]):
    """Photo argument for a scene: cached file_id, else the file itself.

    Returns None when the asset is missing (screens then render as text -
    presentation must never break the flow).
    """
    from aiogram.types import FSInputFile

    path = scene_path(key)
    if path is None:
        return None
    cached = _file_id_cache.get(key)
    if cached:
        return cached
    return FSInputFile(path)


async def send_scene_reply(
    message: Message,
    key: str,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = "Markdown",
) -> Optional[Message]:
    """Reply with the scene art + caption + keyboard.

    Falls back to a plain text reply when the asset is missing, the caption
    would exceed Telegram's limit, or the upload fails - presentation must
    never break the flow.
    """
    media = scene_photo(key) if caption_length(text) <= CAPTION_LIMIT else None
    if media is None:
        return await message.reply(text, reply_markup=reply_markup, parse_mode=parse_mode)

    try:
        sent = await message.reply_photo(
            media, caption=text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        remember_scene_file_id(key, sent)
        return sent
    except Exception as e:
        # Degrade gracefully to a text screen; never break the flow.
        logger.warning(f"Marriage scene '{key}' send failed: {e}")
        return await message.reply(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def send_scene_answer(
    message: Optional[Message],
    key: Optional[str],
    text: str,
) -> Optional[Message]:
    """Post a short scene-carrying follow-up in the group.

    With ``key=None`` (or a missing asset / failed upload) this is exactly
    the plain ``message.answer(text)`` the flow already used.
    """
    if message is None:
        return None

    media = scene_photo(key) if key and caption_length(text) <= CAPTION_LIMIT else None
    if media is None:
        return await message.answer(text)

    try:
        sent = await message.answer_photo(media, caption=text)
        remember_scene_file_id(key, sent)
        return sent
    except Exception as e:
        logger.warning(f"Marriage scene '{key}' group post failed: {e}")
        return await message.answer(text)
