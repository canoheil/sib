"""Banner art for major screens (warm painterly asset set).

One cohesive illustration per screen, stored under ``assets/banners/`` and
committed to the repo. Sends use photo+caption (Telegram shows the art above
the Persian text and inline keyboard); edits of such messages go through the
central photo/media transition path in ``render_callback_screen``.

Design rules (art direction):
- art is TEXT-FREE; every word stays in the Persian caption/keyboard
- one warm painterly language across all banners (honey gold / cream /
  sage green / apple red, gouache-watercolor brushwork)

Screen rule (media lifecycle): a banner screen OWNS its banner. Whether the
screen is opened by a wake word/command (``send_screen_reply``) or reached
by an inline button (``render_banner``), it always renders as its own
banner + caption + keyboard — one canonical rendering per screen.
"""

from pathlib import Path
from typing import Optional

from aiogram.types import FSInputFile, Message

from sib.core.logging import logger
from sib.services.telegram_ui import CAPTION_LIMIT, caption_length

_BANNER_DIR = Path(__file__).resolve().parent.parent / "assets" / "banners"

BANNERS = {
    "main_menu": "main_menu.jpg",
    "games": "games.jpg",
    "garden": "garden.jpg",
    "shop": "shop.jpg",
    "leaderboard": "leaderboard.jpg",
}

# Per-process file_id cache: after the first upload Telegram returns a
# file_id that makes later sends fast (no re-upload).
_file_id_cache: dict[str, str] = {}


def banner_path(key: str) -> Optional[Path]:
    path = _BANNER_DIR / BANNERS.get(key, "")
    if BANNERS.get(key) and path.is_file():
        return path
    return None


def remember_banner_file_id(key: str, message: Optional[Message]) -> None:
    """Cache the uploaded file_id from a sent/edited photo message."""
    if not key or message is None:
        return
    photos = getattr(message, "photo", None)
    if isinstance(photos, (list, tuple)) and photos:
        file_id = getattr(photos[-1], "file_id", None)
        if isinstance(file_id, str) and file_id:
            _file_id_cache[key] = file_id


def banner_photo(key: str):
    """Photo argument for the banner: cached file_id, else the file itself.

    Returns None when the banner asset is missing (screens then render as
    text — presentation must never break the flow).
    """
    path = banner_path(key)
    if path is None:
        return None
    cached = _file_id_cache.get(key)
    if cached:
        return cached
    return FSInputFile(path)


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
    if path is None or caption_length(text) > CAPTION_LIMIT:
        return await message.reply(text, reply_markup=reply_markup, parse_mode=parse_mode)

    media = banner_photo(key)
    try:
        sent = await message.reply_photo(
            media, caption=text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        remember_banner_file_id(key, sent)
        return sent
    except Exception as e:
        # Degrade gracefully to a text screen; never break navigation.
        logger.warning(f"Banner send failed for '{key}': {e}")
        return await message.reply(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def render_banner(
    callback,
    bot,
    key: str,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = "Markdown",
) -> bool:
    """Show a banner screen on a callback's message (canonical path).

    The message behind the button may be text or photo; the shared
    transition engine picks the right move (media swap / edit / replace)
    so the screen always ends up with exactly its own banner. Used by ALL
    banner screens (main menu, games, garden, shop, leaderboard) so that
    wake-word entries and button navigation render identically.
    """
    from sib.services.telegram_ui import render_callback_screen

    message = await render_callback_screen(
        callback,
        bot,
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        photo=banner_photo(key),
    )
    if message is not None:
        remember_banner_file_id(key, message)
    return message is not None
