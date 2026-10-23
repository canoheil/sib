"""Structured logging and event definitions for sib."""

import logging
import sys
from typing import Any, Dict, Optional


class Events:
    """Standard structured audit events for sib bot."""
    USER_REGISTERED = "USER_REGISTERED"
    GROUP_REGISTERED = "GROUP_REGISTERED"
    MEMBERSHIP_VERIFIED = "MEMBERSHIP_VERIFIED"
    MEMBERSHIP_REJECTED = "MEMBERSHIP_REJECTED"
    
    PARTY_CREATED = "PARTY_CREATED"
    PARTY_JOINED = "PARTY_JOINED"
    PARTY_LEFT = "PARTY_LEFT"
    
    MATCHMAKING_STARTED = "MATCHMAKING_STARTED"
    MATCHMAKING_CANCELLED = "MATCHMAKING_CANCELLED"
    MATCHMAKING_TIMEOUT = "MATCHMAKING_TIMEOUT"
    
    MATCH_CREATED = "MATCH_CREATED"
    MATCH_FOUND = "MATCH_FOUND"
    MATCH_ACCEPTED = "MATCH_ACCEPTED"
    MATCH_REJECTED = "MATCH_REJECTED"
    MATCH_STARTED = "MATCH_STARTED"
    MOVE_PLAYED = "MOVE_PLAYED"
    MATCH_FINISHED = "MATCH_FINISHED"
    SEEDLING_AWARDED = "SEEDLING_AWARDED"
    PLAYER_XP_AWARDED = "PLAYER_XP_AWARDED"
    PLAYER_LEVEL_UP = "PLAYER_LEVEL_UP"
    APPLE_TRANSACTION = "APPLE_TRANSACTION"
    GARDEN_EVENT = "GARDEN_EVENT"
    ITEM_GRANTED = "ITEM_GRANTED"
    ITEM_CONSUMED = "ITEM_CONSUMED"
    ITEM_SOLD = "ITEM_SOLD"
    ITEM_PURCHASED = "ITEM_PURCHASED"
    SEEDLING_SOLD = "SEEDLING_SOLD"
    ASSET_REGISTERED = "ASSET_REGISTERED"
    CHARACTER_EVENT = "CHARACTER_EVENT"
    SHOP_EVENT = "SHOP_EVENT"
    TRADE_EVENT = "TRADE_EVENT"
    APPLE_BATTLE_EVENT = "APPLE_BATTLE_EVENT"
    MARRIAGE_PROPOSAL_EVENT = "MARRIAGE_PROPOSAL_EVENT"
    MARRIAGE_EVENT = "MARRIAGE_EVENT"
    LOVE_EVENT = "LOVE_EVENT"
    MATCH_CANCELLED = "MATCH_CANCELLED"

    # Friendly matches ("بازی با اعضای گروه") - a match type, not a game mode.
    PARTY_FRIENDLY_ENABLED = "PARTY_FRIENDLY_ENABLED"
    # Private radar panel delivery of a friendly Battleship match (ephemeral
    # message / DM fallback / delivery failure).
    BATTLESHIP_RADAR_EVENT = "BATTLESHIP_RADAR_EVENT"
    PARTY_BOTS_FILLED = "PARTY_BOTS_FILLED"
    FRIENDLY_MATCH_STARTED = "FRIENDLY_MATCH_STARTED"
    FRIENDLY_REMATCH = "FRIENDLY_REMATCH"
    FRIENDLY_REWARD_PAID = "FRIENDLY_REWARD_PAID"
    FRIENDLY_REWARD_CAPPED = "FRIENDLY_REWARD_CAPPED"
    
    RATING_UPDATED = "RATING_UPDATED"
    AD_DISPLAYED = "AD_DISPLAYED"
    AD_CLICKED = "AD_CLICKED"
    
    ADMIN_COMMAND_EXECUTED = "ADMIN_COMMAND_EXECUTED"
    SYSTEM_RESTART_RECOVERY = "SYSTEM_RESTART_RECOVERY"
    DAILY_CHEST_CLAIMED = "DAILY_CHEST_CLAIMED"


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Set up structured logging."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    
    root_logger = logging.getLogger("sib")
    root_logger.setLevel(numeric_level)
    
    # Avoid duplicate handlers if called multiple times
    if not root_logger.handlers:
        root_logger.addHandler(handler)
        
    return root_logger


logger = setup_logging()


def log_event(event_name: str, details: Optional[Dict[str, Any]] = None, level: int = logging.INFO) -> None:
    """Log a structured domain event."""
    extra_str = f" | {details}" if details else ""
    logger.log(level, f"[EVENT:{event_name}]{extra_str}")
