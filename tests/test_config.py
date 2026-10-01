"""Tests for configuration parsing and validation."""

import pytest
from sib.core.config import Settings


def test_settings_default_values():
    """Verify default values meet master prompt specifications."""
    s = Settings(
        BOT_TOKEN="123:test_token",
        ADMIN_IDS=[111, 222],
    )
    assert s.SOLO_MATCHMAKING_TIMEOUT_SECONDS == 25
    assert s.TEAM_MATCHMAKING_TIMEOUT_SECONDS == 30
    assert s.MATCHMAKING_TIMEOUT_SECONDS == 600
    assert s.ACCEPTANCE_TIMEOUT_SECONDS == 120
    assert s.HIGH_CARD_TIMEOUT == 60
    assert s.ROCK_PAPER_SCISSORS_TIMEOUT == 60
    assert s.TIC_TAC_TOE_TIMEOUT == 60
    assert s.CONNECT_FOUR_TIMEOUT == 60
    assert s.BATTLESHIP_TIMEOUT == 90
    assert s.OFFICIAL_CHANNEL_ID == "@sib_official"
    assert s.MEMBERSHIP_CHECK_ENABLED is True


def test_admin_ids_parsing_from_string():
    """Verify parsing comma-separated string of Telegram admin IDs."""
    s = Settings(
        ADMIN_IDS="12345, 67890, 99999",
    )
    assert s.ADMIN_IDS == [12345, 67890, 99999]


def test_admin_ids_parsing_empty_string():
    """Verify parsing empty admin IDs string returns empty list."""
    s = Settings(
        ADMIN_IDS="",
    )
    assert s.ADMIN_IDS == []


def test_admin_ids_parsing_single_int():
    """Verify parsing single integer admin ID."""
    s = Settings(
        ADMIN_IDS=12345,
    )
    assert s.ADMIN_IDS == [12345]
