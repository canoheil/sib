"""Ad Campaign and Monetization Service."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from goje.core.logging import logger, log_event, Events
from goje.database.models import AdCampaign, FrequencyMode, Match, Rating


class AdService:
    """Service managing monetization, campaign rotation, pacing, and analytics."""

    @classmethod
    async def create_campaign(
        cls,
        session: AsyncSession,
        sponsor_name: str,
        campaign_name: str,
        ad_text: str,
        cta_label: Optional[str] = None,
        target_url: Optional[str] = None,
        image_url: Optional[str] = None,
        is_active: bool = True,
        priority: int = 0,
        frequency_mode: str = FrequencyMode.SMART,
        max_impressions: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        target_game_id: Optional[int] = None,
        target_group_id: Optional[int] = None,
    ) -> AdCampaign:
        """Create a new sponsor ad campaign."""
        campaign = AdCampaign(
            sponsor_name=sponsor_name,
            campaign_name=campaign_name,
            ad_text=ad_text,
            cta_label=cta_label,
            target_url=target_url,
            image_url=image_url,
            is_active=is_active,
            priority=priority,
            frequency_mode=frequency_mode,
            max_impressions=max_impressions,
            start_date=start_date,
            end_date=end_date,
            target_game_id=target_game_id,
            target_group_id=target_group_id,
        )
        session.add(campaign)
        await session.flush()
        return campaign

    @classmethod
    async def get_eligible_ad(
        cls,
        session: AsyncSession,
        game_id: Optional[int] = None,
        group_id: Optional[int] = None,
        match_number: int = 1,
    ) -> Optional[AdCampaign]:
        """
        Dynamically select the highest-priority eligible ad campaign.
        Avoids repetitive ads by evaluating frequency and impression pacing.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            select(AdCampaign)
            .where(AdCampaign.is_active == True)
            .order_by(AdCampaign.priority.desc(), AdCampaign.impressions_count.asc())
        )
        candidates = list((await session.scalars(stmt)).all())

        for c in candidates:
            if c.is_eligible(game_id=game_id, group_id=group_id, match_number=match_number):
                return c

        return None

    @classmethod
    async def record_impression(
        cls,
        session: AsyncSession,
        campaign_id: int,
        match_id: Optional[int] = None,
    ) -> None:
        """Atomically record an ad impression and link to match if provided."""
        stmt = select(AdCampaign).where(AdCampaign.id == campaign_id).with_for_update()
        campaign = await session.scalar(stmt)
        if not campaign:
            return

        campaign.impressions_count += 1

        if match_id:
            m_stmt = select(Match).where(Match.id == match_id).with_for_update()
            match = await session.scalar(m_stmt)
            if match:
                match.ad_campaign_id = campaign_id

        log_event(
            Events.AD_DISPLAYED,
            {"campaign_id": campaign.id, "sponsor": campaign.sponsor_name, "match_id": match_id},
        )

    @classmethod
    async def record_click(
        cls,
        session: AsyncSession,
        campaign_id: int,
        user_id: Optional[int] = None,
    ) -> Optional[AdCampaign]:
        """Atomically record an ad click."""
        stmt = select(AdCampaign).where(AdCampaign.id == campaign_id).with_for_update()
        campaign = await session.scalar(stmt)
        if not campaign:
            return None

        campaign.clicks_count += 1

        log_event(
            Events.AD_CLICKED,
            {"campaign_id": campaign.id, "sponsor": campaign.sponsor_name, "user_id": user_id},
        )
        return campaign

    @classmethod
    async def get_all_campaigns(cls, session: AsyncSession) -> List[AdCampaign]:
        """Fetch all ad campaigns for admin management."""
        stmt = select(AdCampaign).order_by(AdCampaign.priority.desc(), AdCampaign.id.desc())
        return list((await session.scalars(stmt)).all())

    @classmethod
    async def get_campaign(cls, session: AsyncSession, campaign_id: int) -> Optional[AdCampaign]:
        """Fetch single campaign by ID."""
        return await session.scalar(select(AdCampaign).where(AdCampaign.id == campaign_id))

    @classmethod
    async def toggle_campaign_active(cls, session: AsyncSession, campaign_id: int) -> Optional[AdCampaign]:
        """Toggle campaign active/inactive status."""
        stmt = select(AdCampaign).where(AdCampaign.id == campaign_id).with_for_update()
        campaign = await session.scalar(stmt)
        if campaign:
            campaign.is_active = not campaign.is_active
            await session.flush()
        return campaign

    @classmethod
    async def get_analytics(cls, session: AsyncSession) -> Dict[str, Any]:
        """Get aggregate monetization analytics."""
        campaigns = await cls.get_all_campaigns(session)
        total_impressions = sum(c.impressions_count for c in campaigns)
        total_clicks = sum(c.clicks_count for c in campaigns)
        overall_ctr = round((total_clicks / total_impressions * 100.0), 2) if total_impressions > 0 else 0.0

        return {
            "total_campaigns": len(campaigns),
            "active_campaigns": sum(1 for c in campaigns if c.is_active and not c.is_expired and not c.is_capped),
            "total_impressions": total_impressions,
            "total_clicks": total_clicks,
            "overall_ctr": overall_ctr,
            "campaigns": campaigns,
        }
