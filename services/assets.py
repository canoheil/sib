"""Central asset registry (spec #15-#17).

Assets are telegram files (photos/stickers/...) registered under a
stable KEY. Features call assets by key (e.g. 'golden_apple'), never by raw
file_id - replacing the artwork is a single row update.

Resolution order for media keys:
1. ``bot_assets`` row (runtime replacement via /setasset),
2. bundled file ``assets/items/<key>.jpg`` (versioned in the repo),
3. None -> callers fall back to text-only rendering.

Adding a new asset (admin):
    send/reply to the photo or sticker with  /setasset <key>
"""

from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import BotAsset

_BUNDLED_ITEM_DIR = Path(__file__).resolve().parent.parent / "assets" / "items"


def bundled_item_path(key: str) -> Optional[Path]:
    """Bundled artwork file for an item key, if present."""
    if not key or "/" in key or ".." in key:
        return None
    path = _BUNDLED_ITEM_DIR / f"{key}.jpg"
    return path if path.is_file() else None


class AssetService:
    """Registry of telegram file_ids under stable keys."""

    @staticmethod
    async def get(session: AsyncSession, key: str) -> Optional[BotAsset]:
        return await session.scalar(select(BotAsset).where(BotAsset.key == key))

    @staticmethod
    async def file_id(session: AsyncSession, key: str) -> Optional[str]:
        asset = await AssetService.get(session, key)
        return asset.file_id if asset else None

    @classmethod
    async def resolve_photo(cls, session: AsyncSession, key: str) -> Tuple[Optional[str], Optional[Path]]:
        """(file_id, fs_path) for a media key - exactly one is set, or none.

        DB registration wins (runtime override); otherwise the bundled
        ``assets/items/<key>.jpg`` file is used.
        """
        file_id = await cls.file_id(session, key)
        if file_id:
            return file_id, None
        path = bundled_item_path(key)
        if path is not None:
            return None, path
        return None, None

    @staticmethod
    async def register(
        session: AsyncSession,
        key: str,
        file_id: str,
        type: str = "photo",
        description: Optional[str] = None,
    ) -> BotAsset:
        """Upsert an asset key (replaceable artwork, spec #17)."""
        asset = await AssetService.get(session, key)
        if asset is None:
            asset = BotAsset(key=key, file_id=file_id, type=type, description=description)
            session.add(asset)
        else:
            asset.file_id = file_id
            asset.type = type
            if description is not None:
                asset.description = description
        await session.flush()
        log_event(
            Events.ASSET_REGISTERED,
            {"key": key, "type": type, "description": description},
        )
        return asset

    @classmethod
    async def send(
        cls,
        session: AsyncSession,
        bot,
        chat_id: int,
        key: str,
        caption: Optional[str] = None,
        parse_mode: Optional[str] = "Markdown",
    ) -> bool:
        """Send a registered or bundled asset if present.

        Graceful degradation: missing asset or failed send never breaks
        the feature flow - the caller's text message is the real content.
        """
        try:
            asset = await cls.get(session, key)
            if asset is not None:
                if asset.type == "sticker":
                    await bot.send_sticker(chat_id, sticker=asset.file_id)
                    return True
                if asset.type == "animation":
                    await bot.send_animation(
                        chat_id, animation=asset.file_id, caption=caption, parse_mode=parse_mode
                    )
                    return True
                await bot.send_photo(
                    chat_id, photo=asset.file_id, caption=caption, parse_mode=parse_mode
                )
                return True

            # Bundled file fallback (no DB row needed).
            path = bundled_item_path(key)
            if path is not None:
                from aiogram.types import FSInputFile

                await bot.send_photo(
                    chat_id, photo=FSInputFile(path), caption=caption, parse_mode=parse_mode
                )
                return True
            return False
        except Exception as exc:
            logger.warning(f"Asset {key!r} send failed (degrading gracefully): {exc}")
            return False
