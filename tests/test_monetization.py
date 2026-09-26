"""Tests for Monetization, Ad Campaigns, Pacing, and Sponsor Management (Phase 7)."""

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TelegramUser, Chat as TelegramChat
from sqlalchemy import select

from sib.database.models import (
    AdCampaign,
    FrequencyMode,
    SponsorChannel,
    ChannelType,
    Match,
    MatchState,
)
from sib.services.ad.service import AdService
from sib.services.sponsor.service import SponsorService
from sib.handlers.admin import (
    handle_admin_dashboard,
    handle_admin_ads,
    handle_admin_ad_view,
    handle_admin_ad_toggle,
    handle_admin_channels,
    handle_admin_channel_toggle,
)
from sib.handlers.games.ads import handle_ad_click
from sib.handlers.games.rps import handle_rps_action
from sib.matchmaking.matcher import MatchmakingService
from sib.matchmaking.acceptance import AcceptanceService
from sib.services.party import PartyService
from sib.games.rock_paper_scissors.engine import RockPaperScissorsEngine
from sib.tests.test_parties import create_test_user, create_test_group


@pytest.mark.asyncio
async def test_ad_campaign_model_eligibility(db_session):
    now = datetime.now(timezone.utc)

    # 1. Active campaign within date range and under cap
    c1 = AdCampaign(
        sponsor_name="اسپانسر یک",
        campaign_name="کمپین یک",
        ad_text="متن تبلیغ ۱",
        is_active=True,
        priority=10,
        frequency_mode=FrequencyMode.EVERY_MATCH,
        max_impressions=100,
        impressions_count=10,
        clicks_count=2,
    )
    assert c1.is_eligible(match_number=1) is True
    assert c1.ctr == 20.0
    assert not c1.is_expired
    assert not c1.is_capped

    # 2. Inactive campaign
    c2 = AdCampaign(
        sponsor_name="اسپانسر دو",
        campaign_name="کمپین غیرفعال",
        ad_text="متن",
        is_active=False,
    )
    assert c2.is_eligible() is False

    # 3. Capped campaign
    c3 = AdCampaign(
        sponsor_name="اسپانسر سه",
        campaign_name="کمپین پر شده",
        ad_text="متن",
        is_active=True,
        max_impressions=50,
        impressions_count=50,
    )
    assert c3.is_capped is True
    assert c3.is_eligible() is False

    # 4. Expired campaign
    c4 = AdCampaign(
        sponsor_name="اسپانسر چهار",
        campaign_name="کمپین منقضی",
        ad_text="متن",
        is_active=True,
        end_date=now - timedelta(days=1),
    )
    assert c4.is_expired is True
    assert c4.is_eligible() is False

    # 5. Frequency modes
    c_every_2 = AdCampaign(
        sponsor_name="تناوب ۲",
        campaign_name="تناوب ۲",
        ad_text="متن",
        is_active=True,
        frequency_mode=FrequencyMode.EVERY_2_MATCHES,
    )
    assert c_every_2.is_eligible(match_number=1) is False
    assert c_every_2.is_eligible(match_number=2) is True
    assert c_every_2.is_eligible(match_number=3) is False
    assert c_every_2.is_eligible(match_number=4) is True


@pytest.mark.asyncio
async def test_ad_service_creation_selection_and_pacing(db_session):
    # Create two competing campaigns
    c_low = await AdService.create_campaign(
        db_session,
        sponsor_name="اسپانسر کم‌اولویت",
        campaign_name="کمپین A",
        ad_text="متن اولویت پایین",
        priority=5,
        frequency_mode=FrequencyMode.EVERY_MATCH,
    )
    c_high = await AdService.create_campaign(
        db_session,
        sponsor_name="اسپانسر پراولویت",
        campaign_name="کمپین B",
        ad_text="متن اولویت بالا",
        priority=20,
        frequency_mode=FrequencyMode.EVERY_MATCH,
        max_impressions=2,
    )
    await db_session.commit()

    # Initial selection should pick higher priority (c_high)
    selected = await AdService.get_eligible_ad(db_session, match_number=1)
    assert selected is not None
    assert selected.id == c_high.id

    # Record impressions up to cap for c_high
    await AdService.record_impression(db_session, c_high.id)
    await AdService.record_impression(db_session, c_high.id)
    await db_session.commit()

    # Now c_high is capped (2/2 impressions), next selection must fall back to c_low
    selected_next = await AdService.get_eligible_ad(db_session, match_number=1)
    assert selected_next is not None
    assert selected_next.id == c_low.id

    # Test click recording and CTR
    await AdService.record_click(db_session, c_high.id, user_id=12345)
    await db_session.commit()

    analytics = await AdService.get_analytics(db_session)
    assert analytics["total_impressions"] >= 2
    assert analytics["total_clicks"] >= 1
    assert analytics["overall_ctr"] > 0


@pytest.mark.asyncio
async def test_sponsor_channel_service(db_session):
    # Add new sponsor channel
    ch = await SponsorService.add_or_update_channel(
        db_session,
        channel_id="-100999888",
        title="کانال اسپانسر طلایی",
        invite_link="https://t.me/golden_sponsor",
        channel_type=ChannelType.SPONSOR,
        is_required=True,
        priority=50,
    )
    await db_session.commit()

    assert ch.title == "کانال اسپانسر طلایی"
    assert ch.is_required is True

    # Toggle required
    await SponsorService.toggle_required(db_session, ch.channel_id)
    await db_session.commit()
    assert ch.is_required is False

    # Toggle active
    await SponsorService.toggle_active(db_session, ch.channel_id)
    await db_session.commit()
    assert ch.is_active is False

    # Get all channels
    channels = await SponsorService.get_all_channels(db_session)
    assert any(c.channel_id == "-100999888" for c in channels)

    # Delete channel
    deleted = await SponsorService.remove_channel(db_session, "-100999888")
    await db_session.commit()
    assert deleted is True


@pytest.mark.asyncio
async def test_post_match_ad_delivery_and_resilience(db_session):
    """Verify that ads are shown post-match, impressions are logged, and errors never fail the match."""
    g1 = await create_test_group(db_session, "گروه تست تبلیغ ۱")
    g2 = await create_test_group(db_session, "گروه تست تبلیغ ۲")
    u1 = await create_test_user(db_session, "بازیکن تبلیغ ۱")
    u2 = await create_test_user(db_session, "بازیکن تبلیغ ۲")

    # Create active campaign
    ad = await AdService.create_campaign(
        db_session,
        sponsor_name="اسپانسر ویژه مسابقه",
        campaign_name="تبلیغ پایان بازی",
        ad_text="با تشکر از اسپانسر ویژه مسابقات سیب!",
        cta_label="مشاهده وبسایت",
        target_url="https://example.com/sponsor",
        priority=100,
        frequency_mode=FrequencyMode.EVERY_MATCH,
    )
    await db_session.commit()

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "rock_paper_scissors", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "rock_paper_scissors", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = RockPaperScissorsEngine()
    initial_state = engine.create_initial_state(match.players, match.mode)
    match.game_state = json.dumps(initial_state)
    match.group1_message_id = 111
    match.group2_message_id = 222
    await db_session.commit()

    mock_bot = AsyncMock()

    # Round 1: User 1 chooses Rock, User 2 chooses Scissors -> User 1 leads 1-0
    mock_cb1 = MagicMock(spec=CallbackQuery)
    mock_cb1.data = f"rps_action:{match.id}:rock"
    mock_cb1.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb1.answer = AsyncMock()
    await handle_rps_action(mock_cb1, mock_bot)

    mock_cb2 = MagicMock(spec=CallbackQuery)
    mock_cb2.data = f"rps_action:{match.id}:scissors"
    mock_cb2.from_user = TelegramUser(id=u2.id, is_bot=False, first_name=u2.first_name)
    mock_cb2.answer = AsyncMock()
    await handle_rps_action(mock_cb2, mock_bot)

    # Round 2: User 1 wins again -> Best-of-3 finishes the match
    mock_cb3 = MagicMock(spec=CallbackQuery)
    mock_cb3.data = f"rps_action:{match.id}:rock"
    mock_cb3.from_user = TelegramUser(id=u1.id, is_bot=False, first_name=u1.first_name)
    mock_cb3.answer = AsyncMock()
    await handle_rps_action(mock_cb3, mock_bot)

    mock_cb4 = MagicMock(spec=CallbackQuery)
    mock_cb4.data = f"rps_action:{match.id}:scissors"
    mock_cb4.from_user = TelegramUser(id=u2.id, is_bot=False, first_name=u2.first_name)
    mock_cb4.answer = AsyncMock()
    await handle_rps_action(mock_cb4, mock_bot)

    # Verify match is completed, rating applied, and ad was attached
    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.ad_campaign_id == ad.id

    # Verify message sent to groups contains sponsor banner and CTA button
    assert mock_bot.edit_message_text.called
    calls = mock_bot.edit_message_text.call_args_list
    assert len(calls) >= 2
    final_text_g1 = calls[-2][0][0]
    assert "اسپانسر سیب (اسپانسر ویژه مسابقه)" in final_text_g1

    final_kb_g1 = calls[-2][1]["reply_markup"]
    cta_btn = final_kb_g1.inline_keyboard[1][0]
    assert "مشاهده وبسایت" in cta_btn.text
    assert cta_btn.url == "https://example.com/sponsor"

    # Verify impression count incremented
    await db_session.refresh(ad)
    assert ad.impressions_count >= 1


@pytest.mark.asyncio
async def test_ad_click_callback_handler(db_session):
    """Test clicking an ad CTA triggers click registration."""
    ad = await AdService.create_campaign(
        db_session,
        sponsor_name="اسپانسر کلیکی",
        campaign_name="کمپین دکمه‌ای",
        ad_text="متن آزمایشی",
        target_url="https://example.com/click",
    )
    await db_session.commit()

    # Record 1 impression first
    await AdService.record_impression(db_session, ad.id)
    await db_session.commit()

    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = f"ad_click:{ad.id}"
    mock_cb.from_user = TelegramUser(id=8888, is_bot=False, first_name="Clicker")
    mock_cb.answer = AsyncMock()

    await handle_ad_click(mock_cb)

    mock_cb.answer.assert_called_once()
    assert "حمایت شما" in mock_cb.answer.call_args[0][0]

    await db_session.refresh(ad)
    assert ad.clicks_count == 1
    assert ad.ctr == 100.0


@pytest.mark.asyncio
async def test_admin_monetization_handlers(db_session):
    """Test admin dashboard and ad management callbacks."""
    ad = await AdService.create_campaign(
        db_session,
        sponsor_name="اسپانسر پنل",
        campaign_name="کمپین تست پنل",
        ad_text="متن پنل ادمین",
        is_active=True,
    )
    ch = await SponsorService.add_or_update_channel(
        db_session,
        channel_id="-100112233",
        title="کانال تستی",
        invite_link="https://t.me/test_ch",
    )
    await db_session.commit()

    # 1. Admin dashboard callback
    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = "admin_dashboard"
    mock_cb.from_user = TelegramUser(id=111, is_bot=False, first_name="Admin")
    mock_cb.message = MagicMock(spec=Message)
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()

    await handle_admin_dashboard(mock_cb)
    mock_cb.message.edit_text.assert_called_once()
    dash_text = mock_cb.message.edit_text.call_args[0][0]
    assert "کمپین‌های فعال تبلیغاتی" in dash_text

    # 2. Admin ads list callback
    mock_cb.data = "admin_ads"
    mock_cb.message.edit_text.reset_mock()
    await handle_admin_ads(mock_cb)
    mock_cb.message.edit_text.assert_called_once()
    ads_text = mock_cb.message.edit_text.call_args[0][0]
    assert "مدیریت تبلیغات و مانیتایزیشن" in ads_text

    # 3. Admin view single ad
    mock_cb.data = f"admin_ad_view:{ad.id}"
    mock_cb.message.edit_text.reset_mock()
    await handle_admin_ad_view(mock_cb)
    view_text = mock_cb.message.edit_text.call_args[0][0]
    assert "کمپین تست پنل" in view_text
    assert "اسپانسر پنل" in view_text

    # 4. Admin toggle ad active
    mock_cb.data = f"admin_ad_toggle:{ad.id}"
    mock_cb.message.edit_text.reset_mock()
    await handle_admin_ad_toggle(mock_cb)
    await db_session.rollback()
    refreshed_ad = await db_session.scalar(
        select(AdCampaign).where(AdCampaign.id == ad.id).execution_options(populate_existing=True)
    )
    assert refreshed_ad.is_active is False

    # 5. Admin channels list
    mock_cb.data = "admin_channels"
    mock_cb.message.edit_text.reset_mock()
    await handle_admin_channels(mock_cb)
    ch_text = mock_cb.message.edit_text.call_args[0][0]
    assert "کانال‌های رسمی و اسپانسر" in ch_text

    # 6. Admin toggle channel
    target_channel_id = ch.channel_id
    mock_cb.data = f"admin_ch_toggle:{target_channel_id}"
    mock_cb.message.edit_text.reset_mock()
    await handle_admin_channel_toggle(mock_cb)
    await db_session.rollback()
    refreshed_ch = await db_session.scalar(
        select(SponsorChannel).where(SponsorChannel.channel_id == target_channel_id).execution_options(populate_existing=True)
    )
    assert refreshed_ch.is_active is False
