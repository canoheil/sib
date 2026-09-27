"""Character artwork resolution for all screens.

Resolution order (no DB writes, no migrations):

1. ``characters.artwork`` as an /setasset asset key → file_id from AssetService.
2. Bundled artwork file: ``assets/characters/{code}.jpg`` (the framed cards
   built by ``scripts/frame_card.py``).
3. None → screens fall back to text-only rendering.

Art lives in the repository (versioned, permanent). The AssetService
registry stays available for runtime replacement via /setasset.
"""

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "characters"


def artwork_path(character) -> Path | None:
    """Bundled card file for a character, if any."""
    code = getattr(character, "code", None) or ""
    if not code:
        return None
    path = ASSETS_DIR / f"{code}.jpg"
    return path if path.exists() else None


def character_photo_key(character) -> str | None:
    """Asset key when the artwork column points at an uploaded asset."""
    return getattr(character, "artwork", None) or None


async def resolve_character_photo(session, character) -> tuple[str | None, object | None]:
    """Return (file_id, fs_path) — exactly one is set when art exists.

    ``file_id`` wins (runtime override via /setasset); otherwise the bundled
    file is returned as a path for ``FSInputFile``.
    """
    key = character_photo_key(character)
    if key:
        from sib.services.assets import AssetService

        file_id = await AssetService.get_file_id(session, key)
        if file_id:
            return file_id, None
    path = artwork_path(character)
    if path is not None:
        return None, path
    return None, None


def photo_arg(file_id, fs_path):
    """Telegram photo argument (file_id str or FSInputFile) or None."""
    if file_id:
        return file_id
    if fs_path is not None:
        from aiogram.types import FSInputFile

        return FSInputFile(fs_path)
    return None
