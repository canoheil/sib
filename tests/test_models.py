"""Tests for database models and relationships."""

import uuid
import pytest
from sqlalchemy import select
from sib.database.models import User, Group, Game, Match, MatchPlayer, GroupStats, SponsorChannel, ChannelType


@pytest.mark.asyncio
async def test_user_creation_and_properties(db_session):
    """Test User model creation and display_name property."""
    unique_id = int(uuid.uuid4().int % 1000000000)
    user = User(
        id=unique_id,
        first_name="علیرضا",
        last_name="سیب‌ای",
        username=f"alireza_{unique_id}",
    )
    db_session.add(user)
    await db_session.commit()

    saved_user = await db_session.scalar(select(User).where(User.id == unique_id))
    assert saved_user is not None
    assert saved_user.display_name == "علیرضا سیب‌ای"
    assert saved_user.username == f"alireza_{unique_id}"
    assert saved_user.is_active is True


@pytest.mark.asyncio
async def test_group_and_stats_relationship(db_session):
    """Test Group model and 1-to-1 GroupStats relationship (no Elo)."""
    unique_gid = -int(uuid.uuid4().int % 1000000000)
    group = Group(
        id=unique_gid,
        title="تیم ابرقدرت سیب",
    )
    stats = GroupStats(
        group_id=group.id,
        matches_played=10,
        wins=7,
        losses=2,
        draws=1,
    )
    group.stats = stats
    db_session.add(group)
    await db_session.commit()

    saved_group = await db_session.scalar(select(Group).where(Group.id == unique_gid))
    assert saved_group is not None
    assert saved_group.stats is not None
    assert saved_group.stats.wins == 7
    assert not hasattr(saved_group, "rating")


@pytest.mark.asyncio
async def test_sponsor_channel_model(db_session):
    """Test SponsorChannel model attributes."""
    unique_ch_id = f"@test_sponsor_{uuid.uuid4().hex[:6]}"
    channel = SponsorChannel(
        channel_id=unique_ch_id,
        title="اسپانسر تست",
        invite_link=f"https://t.me/{unique_ch_id[1:]}",
        channel_type=ChannelType.SPONSOR,
        is_required=False,
    )
    db_session.add(channel)
    await db_session.commit()

    saved = await db_session.scalar(select(SponsorChannel).where(SponsorChannel.channel_id == unique_ch_id))
    assert saved is not None
    assert saved.channel_type == ChannelType.SPONSOR
    assert saved.is_required is False
