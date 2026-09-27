"""Central asset registry (spec #15-#17).

Assets are telegram files (photos/stickers/...) registered under a
stable KEY. Features call assets by key (``golden_apple``), never by raw
file_id - replacing the artwork is a single row update.

Adding a new asset (admin):
    send/reply to the photo or sticker with  /setasset <key>
"""

from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import BotAsset


class AssetService:
    """Registry of telegram file_ids under stable keys."""

    @staticmethod
    async def get(session: AsyncSession, key: str) -> Optional[BotAsset]:
        return await session.scalar(select(BotAsset).where(BotAsset.key == key))

    @staticmethod
    async def file_id(session: AsyncSession, key: str) -> Optional[str]:
        asset = await AssetService.get(session, key)
        return asset.file_id if asset else None

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

    @staticmethod
    async def send(
        session: AsyncSession,
        bot,
        chat_id: int,
        key: str,
        caption: Optional[str] = None,
        parse_mode: Optional[str] = "Markdown",
    ) -> bool:
        """Send a registered asset if present.

        Graceful degradation: missing asset or failed send never breaks
        the feature flow - the caller's text message is the real content.
        """
        asset = await AssetService.get(session, key)
        if asset is None:
            return False
        try:
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
        except Exception as exc:
            logger.warning(f"Asset {key!r} send failed (degrading gracefully): {exc}")
            return False
