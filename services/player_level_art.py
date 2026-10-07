"""Level artwork for the personal profile (Level 1–30 art set).

One cohesive illustration per Account Level, stored under
``assets/player_levels/`` and committed to the repo as the zero-padded
files ``level_01.jpg`` … ``level_30.jpg`` — one image per level of
``PlayerXPService``'s Level 1–30 curve.

Design rules (art direction):
- art is TEXT-FREE; every word stays in the Persian caption/keyboard
- one warm painterly language shared with the rest of the Sib asset set

Presentation rule: this module is ADDITIVE ONLY. It never derives a Level
(that stays ``PlayerXPService.get_progress``), never touches XP or
progression, and a missing asset simply returns None so the caller degrades
to the photo/text screen it already used — presentation must never break
the flow.
"""

from pathlib import Path
from typing import Optional

# Resolved from THIS file, never from the process working directory: the
# bot, the passenger/WSGI entry point and pytest all run from different
# current directories.
LEVEL_ART_DIR = Path(__file__).resolve().parent.parent / "assets" / "player_levels"

MIN_LEVEL = 1
MAX_LEVEL = 30


def clamp_level(level) -> int:
    """Clamp any level value into the 1..30 range the art set covers.

    Defensive on purpose: a missing/garbage/None level renders the Level 1
    artwork instead of raising inside a profile screen.
    """
    try:
        bounded = int(level)
    except (TypeError, ValueError):
        return MIN_LEVEL
    return max(MIN_LEVEL, min(MAX_LEVEL, bounded))


def level_art_filename(level) -> str:
    """Zero-padded asset name: Level 9 → ``level_09.jpg``."""
    return f"level_{clamp_level(level):02d}.jpg"


def level_art_path(level) -> Optional[Path]:
    """Committed artwork path for a Level, or None when it is absent."""
    path = LEVEL_ART_DIR / level_art_filename(level)
    return path if path.is_file() else None


def level_art_photo(level):
    """Telegram photo argument (``FSInputFile``) for a Level, or None.

    Same local-file representation the profile flows already use for art
    (``services.character_art.photo_arg``): an ``FSInputFile`` that the
    send/edit helpers accept, or None when the asset is missing so the
    caller can fall back.
    """
    path = level_art_path(level)
    if path is None:
        return None

    from aiogram.types import FSInputFile

    return FSInputFile(path)


def profile_photo(progress, fallback_photo=None):
    """The profile's MAIN photo: the player's Level artwork.

    ``progress`` is the ``LevelProgress`` already produced by
    ``PlayerXPService.get_progress`` — the Level is read from it, never
    recomputed. ``fallback_photo`` is the caller's existing art (the
    Favorite Card), used only when the Level artwork cannot be resolved;
    None means the profile renders as plain text.
    """
    return level_art_photo(getattr(progress, "level", None)) or fallback_photo
