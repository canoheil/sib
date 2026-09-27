"""Banner art for major screens (warm painterly asset set).

One cohesive illustration per screen, stored under ``assets/banners/`` and
committed to the repo. Sends use photo+caption (Telegram shows the art above
the Persian text and inline keyboard); edits of such messages go through the
caption path in ``safe_edit_callback_message``.

Design rules (art direction):
- art is TEXT-FREE; every word stays in the Persian caption/keyboard
- one warm painterly language across all banners (honey gold / cream /
  sage green / apple red, gouache-watercolor brushwork)
"""

from pathlib import Path
from typing import Optional

from aiogram.types import FSInputFile, Message

from sib.core.logging import logger

_BANNER_DIR = Path(__file__).resolve().parent.parent / "assets" / "banners"

BANNERS = {
    "main_menu": "main_menu.jpg",
    "games": "games.jpg",
    "garden": "garden.jpg",
    "shop": "shop.jpg",
    "leaderboard": "leaderboard.jpg",
}

# Telegram caption hard limit.
CAPTION_LIMIT = 1024

# Per-process file_id cache: after the first upload Telegram returns a
# file_id that makes later sends fast (no re-upload).
_file_id_cache: dict[str, str] = {}


def banner_path(key: str) -> Optional[Path]:
    path = _BANNER_DIR / BANNERS.get(key, "")
    if BANNERS.get(key) and path.is_file():
        return path
    return None


async def send_screen_reply(
    message: Message,
    key: str,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = "Markdown",
) -> Optional[Message]:
    """Reply with the screen's banner + caption + keyboard.

    Falls back to a plain text reply when the banner is missing or the
    caption would exceed Telegram's limit — presentation must never break
    the flow.
    """
    path = banner_path(key)
    if path is None or len(text) > CAPTION_LIMIT:
        return await message.reply(text, reply_markup=reply_markup, parse_mode=parse_mode)

    media = _file_id_cache.get(key)
    try:
        if media:
            sent = await message.reply_photo(
                media, caption=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        else:
            sent = await message.reply_photo(
                FSInputFile(path),
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            photos = getattr(sent, "photo", None)
            if photos:
                _file_id_cache[key] = photos[-1].file_id
        return sent
    except Exception as e:
        # Degrade gracefully to a text screen; never break navigation.
        logger.warning(f"Banner send failed for '{key}': {e}")
        return await message.reply(text, reply_markup=reply_markup, parse_mode=parse_mode)
