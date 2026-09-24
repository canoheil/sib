"""Telegram handlers for Ad clicks and interactions."""

from aiogram import Router, F
from aiogram.types import CallbackQuery
from sib.database.connection import get_session
from sib.services.ad.service import AdService

router = Router(name="game_ads")


@router.callback_query(F.data == "noop")
async def handle_noop_callback(callback: CallbackQuery):
    """Silently answer disabled or inactive button clicks."""
    await callback.answer()


@router.callback_query(F.data.startswith("ad_click:"))
async def handle_ad_click(callback: CallbackQuery):
    """Handle click on an ad banner button."""
    campaign_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id if callback.from_user else None

    async with get_session() as session:
        campaign = await AdService.record_click(session, campaign_id, user_id=user_id)
        await session.commit()

        if campaign:
            info_text = f"✨ **اسپانسر:** {campaign.sponsor_name}\n\n{campaign.ad_text}"
            if campaign.target_url:
                info_text += f"\n\n🔗 لینک: {campaign.target_url}"
            await callback.answer(f"حمایت شما از {campaign.sponsor_name} ثبت شد! 🍅✨", show_alert=True)
        else:
            await callback.answer("اطلاعات اسپانسر یافت نشد 🍅", show_alert=True)
